import json
import time
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
import yaml

from reeve import php_updates as pu, php_runtime as pr, host as hm


def test_policy_and_due_window(tmp_path, monkeypatch):
    monkeypatch.setattr(pu, 'OPS', tmp_path); monkeypatch.setattr(pu, 'REPORTS', tmp_path / 'rebuilds'); monkeypatch.setattr(pu, 'trusted', lambda *a, **k: None); monkeypatch.setattr(hm, 'trusted', lambda *a, **k: None)
    assert pu.policy() == {'hour': 4, 'every_days': 7}
    (tmp_path / 'server.yaml').write_text(yaml.safe_dump({'updates': {'hour': 2, 'every_days': 14}}))
    assert pu.policy() == {'hour': 2, 'every_days': 14}
    for bad in ({'hour': 24}, {'every_days': 0}, {'x': 1}):
        (tmp_path / 'server.yaml').write_text(yaml.safe_dump({'updates': bad}))
        with pytest.raises(ValueError): pu.policy()
    (tmp_path / 'server.yaml').write_text(yaml.safe_dump({'updates': {'hour': 4, 'every_days': 7}}))
    at = lambda h: datetime(2026, 9, 24, h, 30).timestamp()
    assert pu.due(at(4)) and not pu.due(at(5))  # never run: due in the window only
    pu.mark_started(at(4))
    assert not pu.due(datetime(2026, 9, 30, 4, 30).timestamp()) and pu.due(datetime(2026, 10, 1, 4, 10).timestamp())
    assert datetime.fromtimestamp(pu.next_window(at(5))).strftime('%Y-%m-%d %H') == '2026-10-01 04'


def test_package_comparison():
    diff = pu.compare(['php8.3-fpm\t8.3.33-1', 'curl\t8.0-1', 'gone\t1'], ['php8.3-fpm\t8.3.34-1', 'curl\t8.0-1', 'new\t2'])
    assert diff == {'changed': [{'package': 'php8.3-fpm', 'from': '8.3.33-1', 'to': '8.3.34-1'}], 'added': ['new'], 'removed': ['gone']}


@pytest.fixture
def world(tmp_path, monkeypatch):
    ops = tmp_path / 'ops'; (ops / 'panel/worker').mkdir(parents=True)
    for module in (pu, pr, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(pu, 'OPS', ops); monkeypatch.setattr(pu, 'REPORTS', ops / 'panel/worker/php-rebuilds'); monkeypatch.setattr(pu, 'SITES', tmp_path / 'sites')
    monkeypatch.setattr(pr, 'CATALOG', ops / 'panel/worker/php-runtimes.json'); monkeypatch.setattr(pu, 'CATALOG', ops / 'panel/worker/php-runtimes.json')
    monkeypatch.setattr(pu, 'rebuild_infrastructure', lambda host, ledger, step: {'mail': {'skipped': 'not set up'}, 'sftp': {'skipped': 'not set up'}})
    old = {'image': 'hosting-php:8.3-abc', 'image_id': 'sha256:old83', 'recipe': 'r', 'php_version': '8.3.33', 'extensions': sorted(pr.REQUIRED_EXTENSIONS), 'packages': ['php8.3-fpm\t8.3.33-1', 'curl\t8.0-1']}
    old70 = {'image': 'hosting-php:7.0-abc', 'image_id': 'sha256:old70', 'recipe': 'r', 'php_version': '7.0.33', 'extensions': sorted(pr.REQUIRED_EXTENSIONS), 'packages': ['php7.0-fpm\t7.0.33-1']}
    (pr.CATALOG).write_text(json.dumps({'schema': 1, 'runtimes': {'8.3': old, '7.0': old70}}))
    fresh = {'8.3': ['php8.3-fpm\t8.3.34-1', 'curl\t8.0-1'], '7.0': ['php7.0-fpm\t7.0.33-1']}
    calls = []
    def fake_command(args, timeout=120):
        calls.append(args)
        if args[:2] == ['docker', 'build']: return ''
        if args[:3] == ['docker', 'image', 'inspect']: return 'sha256:new' + args[3].split(':')[1].split('-')[0].replace('.', '')
        if args[:2] == ['docker', 'run'] and args[-1].startswith('echo json_encode'):
            branch = next(a for a in args if a.startswith('hosting-php:')).split(':')[1].split('-')[0]
            return json.dumps({'php_version': {'8.3': '8.3.34', '7.0': '7.0.33'}[branch], 'extensions': sorted(pr.REQUIRED_EXTENSIONS)})
        if args[:2] == ['docker', 'run'] and args[-1].endswith('packages.tsv'):
            branch = next(a for a in args if a.startswith('hosting-php:')).split(':')[1].split('-')[0]
            return '\n'.join(fresh[branch])
        return ''
    monkeypatch.setattr(pu, 'command', fake_command)
    sites = tmp_path / 'sites'
    rows = []
    for name, branch, image in (('shop', '8.3', 'sha256:old83'), ('legacy', '7.0', 'sha256:old70'), ('done', '8.3', 'sha256:new83')):
        (sites / name).mkdir(parents=True)
        (sites / name / 'hosting.yaml').write_text(yaml.safe_dump({'runtime': 'php', 'php_version': branch, 'php_runtime': {'image_id': image}}))
        rows.append({'id': str(uuid.uuid4()), 'name': name, 'state': 'succeeded', 'payload': json.dumps({'runtime': 'php', 'php_version': branch})})
    rows.append({'id': str(uuid.uuid4()), 'name': 'pages', 'state': 'succeeded', 'payload': json.dumps({'runtime': 'static'})})
    submitted = []
    ledger = SimpleNamespace(list=lambda: rows, runtime_branch=lambda row: json.loads(row['payload'])['php_version'],
                             submit_runtime=lambda ident, kind, site_id, payload: (submitted.append((kind, site_id, payload)) or {'id': ident}) if site_id != rows[0]['id'] or not submitted else (_ for _ in ()).throw(ValueError('busy')))
    return ops, ledger, rows, calls, submitted


def test_rebuild_replaces_changed_branches_keeps_the_previous_image_and_rolls_sites(world):
    ops, ledger, rows, calls, submitted = world
    report = pu.rebuild(ledger, None, {'id': 'job1'}, lambda s: None)
    builds = [c for c in calls if c[:2] == ['docker', 'build']]
    assert len(builds) == 2 and all('--no-cache' in b for b in builds)
    runtimes = pr.catalog()
    assert runtimes['8.3']['image_id'] == 'sha256:new83' and runtimes['8.3']['php_version'] == '8.3.34' and runtimes['8.3']['previous'] == {'image': 'hosting-php:8.3-abc', 'image_id': 'sha256:old83', 'php_version': '8.3.33', 'built_at': None}
    assert runtimes['7.0']['image_id'] == 'sha256:old70' and 'previous' not in runtimes['7.0']  # unchanged packages: kept
    assert ['docker', 'image', 'rm', next(b for b in builds if 'PHP_VERSION=7.0' in b)[builds[0].index('--tag') + 1]] in calls or any(c[:3] == ['docker', 'image', 'rm'] for c in calls)
    assert report['branches']['8.3']['changed'] == [{'package': 'php8.3-fpm', 'from': '8.3.33-1', 'to': '8.3.34-1'}] and report['branches']['7.0']['kept']
    # shop is behind on 8.3 and gets a same-branch switch; done is current; legacy's branch did not change; static sites are not PHP.
    assert submitted == [('switch', rows[0]['id'], {'branch': '8.3'})] and [s['name'] for s in report['sites']] == ['shop']
    assert (pu.REPORTS / 'latest.json').exists() and pu.schedule()['last_started'] > 0
    view = pu.overview(ledger)
    assert view['branches']['8.3']['sites_behind'] == ['shop'] and view['branches']['8.3']['previous']['php_version'] == '8.3.33' and view['latest']['id'] == 'job1'
    # A second run with nothing new keeps everything and reports it.
    calls.clear()
    fresh_calls = []
    report2 = pu.rebuild(ledger, None, {'id': 'job2'}, lambda s: None)
    assert report2['branches']['8.3']['kept'] and pr.catalog()['8.3']['image_id'] == 'sha256:new83'
