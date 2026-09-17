import copy
import json
import uuid
from pathlib import Path

import pytest
import yaml

from reeve import recovery_inventory as ri
from reeve.core import Ledger
from reeve.worker import dispatch


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    sites = tmp_path / 'sites'; sites.mkdir()
    ledger = Ledger(tmp_path / 'jobs.db', sites=sites)
    row = ledger.submit(str(uuid.uuid4()), {'name': 'demo', 'domain': 'demo.example.com'})
    ledger.update(row['id'], 'succeeded', 'published'); row = ledger.get(row['id'])
    root = sites / 'demo'; root.mkdir()
    (root / 'html').mkdir(); (root / 'conf').mkdir()
    (root / 'conf/nginx.conf').write_text('private-config-value')
    (root / 'hosting.yaml').write_text('runtime: static\n')
    spec = {'image': 'sha256:original', 'user': '30000:30000', 'read_only': True,
            'volumes': [f'{root}/html:/site:ro', f'{root}/conf/nginx.conf:/etc/nginx/nginx.conf:ro'],
            'networks': ['ingress']}
    compose = {'name': 'hosting-site-demo', 'services': {'web': spec},
               'networks': {'ingress': {'name': 'hosting-ingress-demo'}}}
    (root / 'compose.yml').write_text(yaml.safe_dump(compose))
    container = {'Name': '/web', 'Id': 'original-container', 'Image': 'sha256:original',
        'Config': {'Image': 'sha256:original', 'User': '30000:30000', 'Env': ['PASSWORD=never-show-this'],
                   'Cmd': ['private command'], 'Labels': {'com.docker.compose.project': 'hosting-site-demo',
                                                         'com.docker.compose.service': 'web', 'private': 'hidden-label'}},
        'HostConfig': {'ReadonlyRootfs': True, 'Tmpfs': {'/tmp': ''}},
        'State': {'Status': 'running', 'Health': {'Status': 'healthy', 'Log': ['private-health-output']}},
        'NetworkSettings': {'Networks': {'hosting-ingress-demo': {}}},
        'Mounts': [{'Type': 'bind', 'Source': str(root / 'html'), 'Destination': '/site', 'RW': False},
                   {'Type': 'bind', 'Source': str(root / 'conf/nginx.conf'), 'Destination': '/etc/nginx/nginx.conf', 'RW': False}]}
    image = {'Os': 'linux', 'Architecture': 'amd64', 'RepoDigests': ['registry/image@sha256:original'],
             'Config': {'Env': ['SECRET=private-image-value'], 'Volumes': {}}}
    containers = [container]; calls = []
    def docker(args):
        calls.append(args)
        if args[:2] == ['container', 'ls']: return [c['Id'] for c in containers]
        if args[:2] == ['container', 'inspect']: return copy.deepcopy(containers)
        if args[:2] == ['image', 'inspect']: return [copy.deepcopy(image)]
        if args[:2] == ['network', 'inspect']: return [{'Internal': True, 'Driver': 'bridge'}]
        raise AssertionError('Unexpected Docker operation: ' + str(args))
    monkeypatch.setattr(ri, 'SITES', sites)
    monkeypatch.setattr(ri, 'STORE', tmp_path / 'inventory')
    monkeypatch.setattr(ri, 'VOLUMES', tmp_path / 'volumes')
    monkeypatch.setattr(ri, 'docker', docker)
    # Root-only helpers have separate real VM acceptance; exercise discovery as a normal user.
    monkeypatch.setattr(ri, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(ri.ci, 'regular', lambda p: Path(p).read_bytes())
    monkeypatch.setattr('reeve.host.trusted', lambda *a, **k: None)
    monkeypatch.setattr('reeve.database_site.state', lambda r: None)
    return ledger, row, root, containers, image, calls


def test_discovery_is_read_only_and_cannot_claim_recovery_or_disclose_secrets(inventory):
    ledger, row, root, containers, image, calls = inventory
    result = ri.scan(ledger, row)
    assert not result['error']
    info = result['inventory']
    assert info['coverage'] == 'incomplete' and info['restore'] == 'not verified'
    assert len(info['services']) == 1 and len(info['storage']) == 3
    assert info['services'][0]['temporary_mounts'] == ['/tmp']
    assert not {'writable-layer', 'mounts-changed'} & {g['code'] for g in info['gaps']}
    assert {'owner-inventory', 'consistency', 'reconstruction', 'restore-test'} <= {g['code'] for g in info['gaps']}
    serialized = json.dumps(result)
    for secret in ('never-show-this', 'private command', 'hidden-label', 'private-health-output', 'private-config-value', 'private-image-value'):
        assert secret not in serialized
    assert all(c[:2] in (['container', 'ls'], ['container', 'inspect'], ['image', 'inspect'], ['network', 'inspect']) for c in calls)
    assert not (ri.STORE / (row['id'] + '.json')).stat().st_mode & 0o077


def test_unknown_mount_image_storage_and_writable_layers_are_not_silently_covered(inventory):
    ledger, row, root, containers, image, _ = inventory
    containers[0]['HostConfig']['ReadonlyRootfs'] = False
    containers[0]['Mounts'].append({'Type': 'volume', 'Name': 'unexpected-state', 'Source': '/secret/host/path',
                                     'Destination': '/queue', 'RW': True})
    image['Config']['Volumes'] = {'/missing-data': {}}
    result = ri.collect(ledger, row)
    codes = {g['code'] for g in result['gaps']}
    assert {'writable-layer', 'mounts-changed', 'storage-unavailable', 'image-storage'} <= codes
    volume = next(s for s in result['storage'] if s['kind'] == 'volume')
    assert not volume['declared'] and volume['method'] == 'not configured'
    assert '/secret/host/path' not in json.dumps(result)


def test_missing_service_preserves_declared_storage_in_inventory(inventory):
    ledger, row, root, containers, _, _ = inventory
    containers.clear()
    info = ri.collect(ledger, row)
    assert info['expected_services'] == 1 and not info['services']
    assert any(g['code'] == 'missing-service' for g in info['gaps'])
    assert len(info['storage']) == 3


def test_changed_configuration_invalidates_revision_but_restarts_do_not(inventory):
    ledger, row, root, containers, _, _ = inventory
    first = ri.scan(ledger, row)['inventory']
    containers[0]['Id'] = 'recreated-container'
    containers[0]['State']['Status'] = 'exited'
    second = ri.scan(ledger, row)['inventory']
    assert first['revision'] == second['revision'] and not second['changed']
    (root / 'conf/nginx.conf').write_text('new-private-config')
    third = ri.scan(ledger, row)['inventory']
    assert third['changed'] and third['previous_revision'] == first['revision']
    job = ledger.submit_domains(str(uuid.uuid4()), row['id'], ['demo.example.com', 'preview.example.com'])
    ledger.finish_domains(job)
    fourth = ri.scan(ledger, row)['inventory']
    assert fourth['changed'] and fourth['domains'] == ['demo.example.com', 'preview.example.com']


def test_failed_inspection_retains_prior_observation_and_redacts_diagnostics(inventory, monkeypatch):
    ledger, row, *_ = inventory
    first = ri.scan(ledger, row)['inventory']
    def failure(args): raise RuntimeError('PASSWORD=do-not-return-me')
    monkeypatch.setattr(ri, 'docker', failure)
    result = ri.scan(ledger, row)
    assert result['inventory'] == first and result['error']
    assert result['last_attempt'] >= first['observed_at']
    assert 'do-not-return-me' not in json.dumps(result)
    assert ri.read(row) == result


def test_symlink_boundary_is_not_followed(tmp_path):
    outside = tmp_path / 'outside'; outside.mkdir()
    linked = tmp_path / 'linked'; linked.symlink_to(outside, target_is_directory=True)
    assert ri.metadata(linked) == {'state': 'symlink; needs review'}
    assert ri.metadata(linked / 'secret') == {'state': 'symlink; needs review'}


def test_rpc_has_no_arbitrary_paths_or_commands(inventory):
    ledger, row, *_ = inventory
    with pytest.raises(ValueError, match='Unsupported operation'):
        dispatch({'op': 'recovery-inspect', 'site_id': row['id'], 'path': '/etc/shadow'}, ledger, None)
    with pytest.raises(ValueError, match='Unknown operation'):
        dispatch({'op': 'recovery-inspect', 'site_id': str(uuid.uuid4())}, ledger, None)
    assert not dispatch({'op': 'recovery-status', 'site_id': row['id']}, ledger, None)['inventory']
    assert dispatch({'op': 'recovery-inspect', 'site_id': row['id']}, ledger, None)['inventory']['coverage'] == 'incomplete'


def test_adopted_volume_and_database_candidates_need_methods(inventory, monkeypatch):
    ledger, row, root, containers, _, _ = inventory
    from reeve import compose_adopt as ca
    saved = root.parent.parent / 'adoption'; saved.mkdir()
    monkeypatch.setattr(ca, 'plan_path', lambda ident: saved)
    plan = {'project_name': 'hosting-site-demo', 'sources': [], 'images': {'web': 'sha256:original'},
        'route': {'web_service': 'web'}, 'summary': {'services': [{'name': 'web', 'database': 'postgres'}]},
        'volumes': {'db-data': {'name': 'retained-database', 'external': True}},
        'model': {'services': {'web': {'image': 'sha256:original', 'user': '30000:30000', 'read_only': True,
            'networks': {}, 'volumes': [{'type': 'volume', 'source': 'db-data', 'target': '/var/lib/postgresql'}]}}}}
    for filename, text in [('plan.json', json.dumps(plan)), ('resolved.compose.json', '{}'), ('compose.hosting.yaml', '')]:
        (saved / filename).write_text(text)
    containers[0]['Mounts'] = [{'Type': 'volume', 'Name': 'retained-database', 'Destination': '/var/lib/postgresql', 'RW': True}]
    volume = ri.VOLUMES / 'retained-database/_data'; volume.mkdir(parents=True)
    row = dict(row, payload=json.dumps(dict(json.loads(row['payload']), runtime='compose')))
    info = ri.collect(ledger, row)
    assert any(g['code'] == 'database-method' for g in info['gaps'])
    assert not any(g['code'] in ('mounts-changed', 'networks-changed') for g in info['gaps'])
    mount = next(s for s in info['storage'] if s['kind'] == 'volume')
    assert mount['source'] == 'retained-database' and mount['declared']
    assert mount['boundary']['state'] == 'present' and mount['classification'] == 'unclassified'
