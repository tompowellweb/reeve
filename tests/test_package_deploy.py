import copy
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from reeve import package_deploy as pd, compose_adopt as ca, compose_inspect as ci, host as hm, backup_jobs
from reeve.application_package import review
from reeve.auth import Auth
from reeve.core import DEFAULTS, Ledger
from reeve.web import create_app
from test_application_intake import archive, DATA

MODEL = {'services': {'web': {'build': '.', 'ports': ['8999:8000'], 'container_name': 'original', 'volumes': ['./code:/app:ro']}}}
# A supplied stack as an author would write it: no users, root images, depends_on, a named volume.
STACK = {'services': {
    'web': {'build': '.', 'ports': ['8999:8000'], 'depends_on': ['db'], 'volumes': ['./code:/app']},
    'db': {'image': 'mysql:5.7', 'environment': {'MYSQL_ROOT_PASSWORD': 'rootpw', 'MYSQL_DATABASE': 'app'},
           'volumes': ['db_data:/var/lib/mysql'], 'restart': 'unless-stopped'},
    'mail': {'image': 'mailhog/mailhog'}},
    'volumes': {'db_data': {}}}


def package(model=None, files=None, links=None):
    return archive({'compose.yaml': yaml.safe_dump(model or MODEL).encode(),
                    'Dockerfile': b'FROM python:3.12-slim\n', 'code/main.py': b'original data',
                    **(files or {})}, links=links)


@pytest.fixture
def context(tmp_path, monkeypatch):
    sites = tmp_path / 'sites'; sites.mkdir()
    for module in (pd, ca):
        monkeypatch.setattr(module, 'SITES', sites)
        monkeypatch.setattr(module, 'STORE', tmp_path / ('packages' if module is pd else 'adoptions'))
        monkeypatch.setattr(module, 'PROXY', tmp_path / 'proxy')
        monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(pd, 'INTAKE', tmp_path / 'imports')
    for module in (ci, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    # Local tests run as the developer; VM acceptance exercises root-owned control reads.
    monkeypatch.setattr(ci, 'regular', lambda path: Path(path).read_bytes())
    monkeypatch.setattr(pd, 'preflight', lambda: None)
    monkeypatch.setattr(pd, 'apply_quota', lambda *a: None)
    return Ledger(tmp_path / 'jobs.db', sites), SimpleNamespace(defaults=DEFAULTS, inspect=lambda name: None)


def submit(context, body=None):
    ledger, host = context; ident = str(uuid.uuid4()); body = body or package()
    path = pd.INTAKE / ident; path.mkdir(parents=True); (path / 'package').write_bytes(body)
    checksum = hashlib.sha256(body).hexdigest()
    return pd.submit(ledger, host, ident, DATA, checksum)


def extracted(tmp_path, body):
    p = tmp_path / 'archive'; p.write_bytes(body); target = tmp_path / 'extract'; target.mkdir()
    report = review(p, DATA); pd.extract(p, target, report)
    return target, report


def resolve(args):
    """Stand in for `docker compose config --format json` on the private definition."""
    model = json.loads(Path(args[args.index('--file') + 1]).read_text())
    root = Path(args[args.index('--project-directory') + 1])
    for spec in model['services'].values():
        mounts = []
        for value in spec.get('volumes', []):
            source, target = value.split(':')[:2]
            if source.startswith('.'): mounts.append({'type': 'bind', 'source': str(root / source[2:]), 'target': target, 'read_only': value.endswith(':ro')})
            else: mounts.append({'type': 'volume', 'source': source, 'target': target})
        if mounts: spec['volumes'] = mounts
        spec['networks'] = {'default': {}}
    model['networks'] = {'default': {'name': model['name'] + '_default'}}
    return model


def mock_runtime(monkeypatch, calls):
    def execute(args, **kwargs):
        calls.append(args)
        if 'config' in args: return resolve(args)
    monkeypatch.setattr(ci, 'run', execute)
    monkeypatch.setattr(ca, 'command', lambda args, timeout=120: calls.append(args))
    monkeypatch.setattr(pd, 'image_command', lambda args, *a, **k: calls.append(args))
    monkeypatch.setattr(ca, 'docker', lambda args, **k: [{'Id': 'sha256:' + args[-1], 'Config': {}}] if args[:2] == ['image', 'inspect'] else calls.append(['docker', *args]))


def test_worker_copy_is_immutable_and_refuses_swapped_files(context):
    row = submit(context); state = pd.load(row)
    saved = pd.worker_copy(row, state)
    (pd.INTAKE / row['id'] / 'package').write_bytes(b'changed')
    assert pd.worker_copy(row, state).read_bytes() == saved.read_bytes()
    saved.unlink()
    with pytest.raises(ValueError, match='changed'): pd.worker_copy(row, state)
    assert not saved.exists()
    incoming = pd.INTAKE / row['id'] / 'package'; incoming.unlink(); incoming.symlink_to('/etc/passwd')
    with pytest.raises(OSError): pd.worker_copy(row, state)


def test_duplicate_and_conflicting_deploys_preserve_identity(context):
    ledger, host = context; row = submit(context); state = pd.load(row)
    assert pd.submit(ledger, host, row['id'], DATA, state['sha256'])['id'] == row['id']
    with pytest.raises(ValueError, match='different package'):
        pd.submit(ledger, host, row['id'], DATA, 'f' * 64)
    with pytest.raises(ValueError, match='already reserved'):
        pd.submit(ledger, host, str(uuid.uuid4()), DATA, state['sha256'])
    assert len(ledger.list()) == 1
    ledger.update(row['id'], 'running', 'building'); ledger.interrupted()
    assert ledger.get(row['id'])['state'] == 'recovery-needed'
    assert ledger.retry(row['id'])['project'] == row['project']
    with ledger.db() as db: assert db.execute('PRAGMA user_version').fetchone()[0] == 17


def test_extraction_preserves_internal_links_and_removes_special_modes(context, tmp_path):
    target, _ = extracted(tmp_path, package(links={'code/current': 'main.py'}))
    assert (target / 'code/current').read_bytes() == b'original data'
    assert (target / 'code/main.py').stat().st_mode & 0o777 == 0o644
    assert (target / 'code').stat().st_mode & 0o755 == 0o755


@pytest.mark.parametrize('change,match', [
    ({'build': {'context': '.', 'privileged': True}}, 'build supports'),
    ({'build': {'context': '.', 'cache_to': ['type=local,dest=/etc']}}, 'build supports'),
    ({'privileged': True}, 'reaches outside'),
    ({'cap_add': ['SYS_ADMIN']}, 'reaches outside'),
    ({'network_mode': 'host'}, 'reaches outside'),
    ({'pid': 'host'}, 'reaches outside'),
    ({'devices': ['/dev/sda:/dev/sda']}, 'reaches outside'),
    ({'security_opt': ['seccomp=unconfined']}, 'security options'),
    ({'storage_opt': {'overlay2.mountopt': 'bad'}}, 'writable-layer'),
])
def test_host_hazards_fail_before_execution(context, tmp_path, change, match):
    model = copy.deepcopy(MODEL); model['services']['web'].update(change)
    target, report = extracted(tmp_path, package(model))
    with pytest.raises(ValueError, match=match): pd.prepare_model(target, report, DATA, 'package-test')


def test_shared_networks_and_docker_socket_are_refused(context, tmp_path):
    model = copy.deepcopy(MODEL); model['networks'] = {'shared': {'external': True}}
    target, report = extracted(tmp_path, package(model))
    with pytest.raises(ValueError, match='reach outside'): pd.prepare_model(target, report, DATA, 'package-test')
    model = copy.deepcopy(MODEL); model['services']['web']['volumes'].append('/var/run/docker.sock:/var/run/docker.sock')
    p = tmp_path / 'socket'; p.write_bytes(package(model))
    report = review(p, DATA)
    assert report['state'] == 'needs_preparation' and pd.eligibility(report)


def test_supplied_stack_is_accepted_as_written(context, tmp_path):
    target, report = extracted(tmp_path, package(STACK, files={'dumps/db.sql.gz': b'\x1f\x8bhistorical'}))
    model = pd.prepare_model(target, report, DATA, 'package-test')
    web, db = model['services']['web'], model['services']['db']
    assert 'ports' not in web and 'container_name' not in web and 'user' not in web and 'user' not in db
    assert web['image'] == 'hosting-package-test:web' and web['depends_on'] == ['db']
    assert model['volumes']['db_data']['name'] == 'package-test_db_data'
    assert 'healthcheck' not in db and 'cap_drop' not in db
    found = pd.databases(model, target)
    assert found == {'db': {'engine': 'mysql', 'dump': 'dumps/db.sql.gz', 'restored': False}}
    assert pd.credentials('mysql', db) == {'user': 'root', 'password': 'rootpw', 'database': 'app'}
    (target / 'dumps/other.sql').write_bytes(b'x')
    with pytest.raises(ValueError, match='does not name'): pd.databases(model, target)
    (target / 'dumps/other.sql').unlink(); (target / 'dumps/db.sql').write_bytes(b'x')
    with pytest.raises(ValueError, match='one dump'): pd.databases(model, target)
    with pytest.raises(ValueError, match='root password'): pd.credentials('mysql', {'environment': {'MYSQL_DATABASE': 'app'}})
    assert pd.credentials('postgres', {'environment': {'POSTGRES_PASSWORD': 'pw'}}) == {'user': 'postgres', 'password': 'pw', 'database': 'postgres'}
    assert pd.credentials('postgres', {'environment': ['POSTGRES_USER=app', 'POSTGRES_PASSWORD=pw', 'POSTGRES_DB=data']})['database'] == 'data'


def test_restore_commands_use_native_clients_once_per_fresh_database():
    creds = {'user': 'app', 'password': 'pw', 'database': 'data'}
    assert pd.restore_command('postgres', creds, 'db.dump').startswith('pg_restore --host=127.0.0.1 --username=app --dbname=data --no-owner --no-privileges --single-transaction')
    assert 'ON_ERROR_STOP=1' in pd.restore_command('postgres', creds, 'db.sql.gz') and pd.restore_command('postgres', creds, 'db.sql.gz').startswith('gzip -t')
    assert pd.restore_command('mysql', creds, 'db.sql.gz') == 'gzip -t /restore/db.sql.gz && gzip -dc /restore/db.sql.gz | mysql --host=127.0.0.1 --user=root --database=data'
    assert 'mariadb' in pd.restore_command('mariadb', creds, 'db.sql')
    assert 'pw' not in pd.restore_command('mysql', creds, 'db.sql')
    assert pd.readiness('postgres', creds)[1] == ['--host=127.0.0.1', '--username=app']


def test_retry_after_runtime_failure_never_reextracts_data_or_rebuilds(context, monkeypatch):
    ledger, host = context; row = submit(context); calls = []
    mock_runtime(monkeypatch, calls)
    attempts = []
    def runtime(host, row, step, before_up=None):
        attempts.append(1)
        path = pd.SITES / row['name'] / 'code/main.py'
        if len(attempts) == 1:
            path.write_bytes(b'new runtime state')
            raise ValueError('synthetic route failure')
        assert path.read_bytes() == b'new runtime state'
    monkeypatch.setattr(ca, 'perform', runtime)
    with pytest.raises(ValueError, match='synthetic'): pd.perform(host, row, lambda s: None)
    pd.perform(host, row, lambda s: None)
    assert len([a for a in calls if 'build' in a]) == 1
    plan = ca.read(row)
    assert plan['images'] == {'web': 'sha256:hosting-package-' + row['id'] + ':web'}
    assert plan['summary']['services'][0]['user'] == 'Image default'
    assert pd.load(row)['stage'] == 'published' and pd.load(row)['databases'] == {}


def test_database_restore_runs_once_before_application_start_and_enables_dumps(context, monkeypatch):
    ledger, host = context
    row = submit(context, package(STACK, files={'dumps/db.sql.gz': b'\x1f\x8bhistorical'}))
    calls = []; restores = []; steps = []
    mock_runtime(monkeypatch, calls)
    monkeypatch.setattr(pd, 'wait_ready', lambda *a, **k: calls.append(['ready']))
    monkeypatch.setattr(pd, 'bounded', lambda args, log, timeout, failure: restores.append(args))
    monkeypatch.setattr(ca, 'project_containers', lambda plan: [
        {'Id': 'dbcontainer', 'Config': {'Labels': {'com.docker.compose.service': 'db'}}, 'State': {'Running': True, 'Status': 'running'}}])
    monkeypatch.setattr(ca, 'perform', lambda host, row, step, before_up=None: before_up(ca.read(row), ['docker', 'compose', 'test']))
    pd.perform(host, row, steps.append)
    assert len(restores) == 1
    command = restores[0]
    assert '--network' in command and 'container:dbcontainer' in command and 'sha256:mysql:5.7' in command
    assert any(a.startswith('type=bind,src=') and a.endswith('/dumps/db.sql.gz,dst=/restore/db.sql.gz,readonly') for a in command)
    assert 'rootpw' not in ' '.join(command)
    started = [c for c in calls if 'up' in c][0]
    assert started[-1] == 'db' and '--no-build' in started
    assert calls.index(started) < calls.index(['ready'])
    assert any('restoring db from dumps/db.sql.gz' in s for s in steps)
    state = pd.load(row)
    assert state['databases']['db']['restored'] and state['stage'] == 'published'
    assert not (pd.state_path(row['id']) / 'restore-db.env').exists()
    plan = ca.read(row)
    assert set(plan['images']) == {'web', 'db', 'mail'} and plan['volumes'] == {'db_data': {'name': 'package-' + row['id'] + '_db_data', 'external': False}}
    assert {s['name']: s['database'] for s in plan['summary']['services']} == {'web': None, 'db': 'MySQL', 'mail': None}
    assert {p['kind'] for p in plan['summary']['persistence']} == {'Bind mount', 'Named volume'}
    # A retry after publication never replays the dump.
    pd.perform(host, row, steps.append)
    assert len(restores) == 1
    # The one recognised database service is a native dump target on the ordinary schedule.
    target = pd.dump_target(row)
    assert target['service'] == 'db' and target['database'] == 'app' and target['image_id'] == 'sha256:mysql:5.7'
    ledger.update(row['id'], 'succeeded', 'published'); row = ledger.get(row['id'])
    assert backup_jobs.supported(row)
    job = ledger.submit_backup(str(uuid.uuid4()), row['id'])
    assert job['site_id'] == row['id']
    assert 'Compose service "db"' in backup_jobs.status(ledger, row)['scope']
    assert pd.public(row)['databases']['db']['restored']


def test_package_overlay_runs_images_as_shipped(context):
    ledger, host = context
    plan = {'name': 'demo', 'project_name': 'package-x', 'route': {'web_service': 'web'}, 'images': {'web': 'sha256:web'},
            'model': {'services': {'web': {'image': 'i'}}, 'networks': {}}, 'volumes': {}, 'package_id': 'x'}
    row = {'id': str(uuid.uuid4()), 'name': 'demo'}
    service = ca.overlay(row, plan)['services']['web']
    assert 'cap_drop' not in service and 'security_opt' not in service and service['pull_policy'] == 'never'
    # The retired host-adopted path added cap_drop/no-new-privileges; no plan gets that now.
    del plan['package_id']
    assert 'cap_drop' not in ca.overlay(row, plan)['services']['web']


def test_unowned_staging_is_preserved(context):
    ledger, host = context; row = submit(context)
    stage = pd.SITES / ('.package-' + row['id']); stage.mkdir(); (stage / 'keep').write_text('retained')
    with pytest.raises(ValueError, match='unowned staging'): pd.perform(host, row, lambda s: None)
    assert (stage / 'keep').read_text() == 'retained'


def test_partial_move_retries_contents_without_renaming_quota_root(context, monkeypatch):
    ledger, host = context; row = submit(context); moves = []; interrupted = [False]
    rename = Path.rename
    def move(source, target):
        assert not source.name.startswith('.package-'), 'XFS quota root cannot be renamed into its parent'
        if source.parent.name.startswith('.package-'):
            if moves and not interrupted[0]:
                interrupted[0] = True
                raise OSError('simulated interruption between file moves')
            moves.append(source.name)
        return rename(source, target)
    monkeypatch.setattr(Path, 'rename', move)
    def compile(row, *args):
        ca.plan_path(row['id']).mkdir(parents=True)
        ca.save(row, {'stage': 'reviewed'})
    monkeypatch.setattr(pd, 'compile_runtime', compile)
    monkeypatch.setattr(ca, 'perform', lambda *args, **kwargs: None)
    with pytest.raises(OSError, match='simulated interruption'): pd.perform(host, row, lambda s: None)
    assert pd.load(row)['stage'] == 'extracted'
    pd.perform(host, row, lambda s: None)
    assert len(moves) == len(set(moves))
    assert (pd.SITES / row['name'] / 'code/main.py').read_bytes() == b'original data'
    assert not (pd.SITES / ('.package-' + row['id'])).exists()


def test_only_operator_can_deploy_and_status_does_not_leak_private_fields(tmp_path):
    auth = Auth(tmp_path / 'auth.db'); auth.set_password('strong-test-password-123')
    token = auth.new_api_token('intake only'); calls = []
    def call(message):
        calls.append(message)
        if message['op'] == 'package-status': return None
        if message['op'] == 'package-deploy': return {'name': DATA['name']}
        return []
    client = TestClient(create_app(tmp_path / 'auth.db', call=call))
    ident = str(uuid.uuid4())
    response = client.post('/api/v1/imports', params=DATA, content=package(), headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/octet-stream', 'Idempotency-Key': ident})
    assert response.status_code == 201
    assert client.post('/imports/' + ident + '/deploy', headers={'Authorization': 'Bearer ' + token}).status_code == 403
    login = client.get('/login'); csrf = re.search('name="csrf" value="([^"]+)"', login.text)[1]
    client.post('/login', data={'csrf': csrf, 'password': 'strong-test-password-123'})
    page = client.get('/imports/' + ident); csrf = re.search('name="csrf" value="([^"]+)"', page.text)[1]
    assert client.post('/imports/' + ident + '/deploy', data={'csrf': 'wrong'}).status_code == 403
    assert not any(c['op'] == 'package-deploy' for c in calls)
    response = client.post('/imports/' + ident + '/deploy', data={'csrf': csrf}, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/sites/example'
    assert len([c for c in calls if c['op'] == 'package-deploy']) == 1


def test_build_lane_prepares_images_beside_the_serial_worker(context, monkeypatch):
    from reeve import worker
    ledger, host = context; row = submit(context, package(STACK)); calls = []
    mock_runtime(monkeypatch, calls)
    monkeypatch.setattr(hm, 'preflight', lambda: None)
    builder = {'thread': None}
    # Lane busy: the row stays queued and untouched.
    assert worker.package_prebuild(ledger, host, row, {'thread': SimpleNamespace(is_alive=lambda: True)}) is None
    assert ledger.get(row['id'])['state'] == 'queued'
    ran = []
    assert worker.package_prebuild(ledger, host, row, builder, spawn=lambda fn: (ran.append(1), fn())) is True
    current = ledger.get(row['id'])
    assert current['state'] == 'queued' and current['step'] == 'images ready' and ran == [1]
    state = pd.load(row)
    assert state['stage'] == 'extracted' and state['images_ready'] and set(state['images']) == {'web', 'db', 'mail'}
    assert len([a for a in calls if 'build' in a]) == 1 and len([a for a in calls if a[:2] == ['docker', 'pull']]) == 2
    assert (pd.SITES / ('.package-' + row['id']) / 'code/main.py').exists()
    # With images ready the serial path runs without building again.
    assert worker.package_prebuild(ledger, host, row, builder) is False
    monkeypatch.setattr(ca, 'perform', lambda host, row, step, before_up=None: None)
    pd.perform(host, row, lambda s: None)
    assert len([a for a in calls if 'build' in a]) == 1 and pd.load(row)['stage'] == 'published'
    # The database inventory persists even though no image was built in the serial path.
    assert pd.load(row)['databases']['db']['engine'] == 'mysql' and pd.dump_target(row)['service'] == 'db'


def test_build_lane_failure_is_recoverable_and_retry_rebuilds(context, monkeypatch):
    from reeve import worker
    ledger, host = context; row = submit(context); calls = []
    mock_runtime(monkeypatch, calls)
    monkeypatch.setattr(hm, 'preflight', lambda: None)
    def failing(args, *a, **k):
        calls.append(args)
        if len([a for a in calls if 'build' in a]) == 1: raise ValueError('Image preparation failed. synthetic')
    monkeypatch.setattr(pd, 'image_command', failing)
    assert worker.package_prebuild(ledger, host, row, {'thread': None}, spawn=lambda fn: fn()) is True
    current = ledger.get(row['id'])
    assert current['state'] == 'recovery-needed' and 'synthetic' in current['error'] and not pd.load(row).get('images_ready')
    ledger.retry(row['id'])
    assert worker.package_prebuild(ledger, host, ledger.get(row['id']), {'thread': None}, spawn=lambda fn: fn()) is True
    assert ledger.get(row['id'])['step'] == 'images ready' and pd.load(row)['images_ready']
    assert len([a for a in calls if 'build' in a]) == 2
