import json
import uuid

import pytest
import yaml

from reeve import database_site as ds, host as hm, php_settings as ps, profile as pf
from reeve.core import DEFAULTS, Ledger
from reeve.database_jobs import validate_database


def test_profile_reads_server_yaml_and_defaults_to_standard(tmp_path, monkeypatch):
    ops = tmp_path / 'ops'; ops.mkdir(); monkeypatch.setattr(pf, 'OPS', ops)
    assert pf.name() == 'standard' and pf.settings()['php_workers'] == 8
    (ops / 'server.yaml').write_text('schema: 1\n')
    assert pf.name() == 'standard'
    (ops / 'server.yaml').write_text('profile: small\n')
    assert pf.settings() == {'name': 'small', **pf.PROFILES['small']}
    (ops / 'server.yaml').write_text('profile: huge\n')
    with pytest.raises(ValueError): pf.name()


def test_the_profile_sets_php_budget_site_memory_and_database_usage(monkeypatch):
    monkeypatch.setattr(pf, 'name', lambda: 'small')
    assert ps.budget({}) == (3, 256) and ps.defaults({})['memory_limit_mb'] == 256
    assert ps.budget({'memory_mb': 4096}) == (3, 256)  # a capped site stays within the profile's ceiling
    assert ps.budget({'memory_mb': 256}) == (1, 176)
    assert validate_database({'engine': 'mariadb'})['usage'] == 'light'
    assert validate_database({'engine': 'mariadb', 'usage': 'high'})['usage'] == 'high'
    with pytest.raises(ValueError): validate_database({'engine': 'mariadb', 'usage': 'huge'})
    monkeypatch.setattr(pf, 'name', lambda: 'large')
    assert ps.budget({}) == (8, 512) and validate_database({'engine': 'postgres'})['usage'] == 'high'


def test_host_defaults_take_the_profile_cap_unless_server_yaml_sets_one(tmp_path, monkeypatch):
    ops = tmp_path / 'ops'; ops.mkdir()
    for module in (hm, pf): monkeypatch.setattr(module, 'OPS', ops)
    monkeypatch.setattr(hm, 'trusted', lambda *a, **k: None)
    (ops / 'server.yaml').write_text(yaml.safe_dump({'schema': 1, 'profile': 'small', 'defaults': {'data_mb': 512, 'memory_mb': None}}))
    assert hm.Host().defaults == {**DEFAULTS, 'data_mb': 512, 'memory_mb': 768}
    (ops / 'server.yaml').write_text(yaml.safe_dump({'schema': 1, 'profile': 'small', 'defaults': {'memory_mb': 2048}}))
    assert hm.Host().defaults['memory_mb'] == 2048
    (ops / 'server.yaml').write_text(yaml.safe_dump({'schema': 1, 'defaults': {}}))
    assert hm.Host().defaults['memory_mb'] is None


def test_usage_renders_server_flags_for_every_engine():
    light = ds.server_options('mariadb', None, 'light')
    assert '--innodb-buffer-pool-size=64M' in light and '--max-connections=40' in light and '--skip-log-bin' in light and not any(f.startswith('--performance-schema') for f in light)
    high = ds.server_options('mysql', 'mysql_native_password', 'high')
    assert '--innodb-buffer-pool-size=1024M' in high and '--performance-schema=ON' in high and high[-1] == '--default-authentication-plugin=mysql_native_password'
    assert ds.server_options('postgres', None, 'light') == ['postgres', '-c', 'shared_buffers=32MB', '-c', 'max_connections=40']
    assert ds.server_options('mysql', None, None)[2] == '--innodb-buffer-pool-size=256M'  # an older record without a usage is standard


@pytest.fixture
def database(tmp_path, monkeypatch):
    for module in (ds, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(ds, 'OPS', tmp_path / 'ops'); monkeypatch.setattr(ds, 'USAGE_SAVED', tmp_path / 'ops/panel/worker/database-usage')
    ledger = Ledger(tmp_path / 'jobs.db', sites=tmp_path / 'sites')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'shop', 'domain': 'shop.example.com', 'database': {'engine': 'mariadb', 'usage': 'standard'}})
    root = tmp_path / 'sites/shop/database'; root.mkdir(parents=True)
    saved = tmp_path / 'ops/panel/worker/databases' / (row['id'] + '.json'); saved.parent.mkdir(parents=True)
    monkeypatch.setattr(ds, 'paths', lambda r: (root, saved))
    info = {'schema': 1, 'operation_id': row['id'], 'spec': {'engine': 'mariadb', 'usage': 'standard'}, 'engine': 'mariadb', 'stage': 'ready', 'auth_plugin': None}
    saved.write_text(json.dumps(info))
    monkeypatch.setattr(ds, 'state', lambda r: json.loads(saved.read_text()))
    (root / 'compose.yml').write_text(yaml.safe_dump({'name': 'hosting-db-shop', 'services': {'database': {'image': 'mariadb', 'command': ds.server_options('mariadb', None, 'standard')}}}))
    calls = []
    monkeypatch.setattr(ds, 'command', lambda args, timeout=120: calls.append([str(a) for a in args]) or '')
    return ledger, row, root, saved, calls


def test_usage_change_rewrites_the_compose_command_and_restarts_and_rolls_back(database):
    ledger, row, root, saved, calls = database
    ident = str(uuid.uuid4())
    assert ds.apply_usage(None, row, {'usage': 'light'}, ident) == {'usage': 'light'}
    compose = yaml.safe_load((root / 'compose.yml').read_text())
    assert '--innodb-buffer-pool-size=64M' in compose['services']['database']['command'] and json.loads(saved.read_text())['spec']['usage'] == 'light'
    assert [c[:3] for c in calls] == [['docker', 'compose', '-f'], ['docker', 'update', '--pids-limit']] and '--force-recreate' in calls[0]
    assert json.loads((ds.USAGE_SAVED / (ident + '.json')).read_text())['spec']['usage'] == 'standard'
    # A restart that fails puts the previous command and record back.
    calls.clear()
    def failing(args, timeout=120):
        calls.append([str(a) for a in args])
        if args[0] == 'docker' and args[1] == 'compose' and len([c for c in calls if c[1] == 'compose']) == 1: raise RuntimeError('unhealthy')
        return ''
    import reeve.database_site as module
    module.command = failing
    with pytest.raises(RuntimeError, match='unhealthy'): ds.apply_usage(None, row, {'usage': 'high'})
    assert json.loads(saved.read_text())['spec']['usage'] == 'light' and '--innodb-buffer-pool-size=64M' in yaml.safe_load((root / 'compose.yml').read_text())['services']['database']['command']
    with pytest.raises(ValueError): ds.validate_usage({'usage': 'huge'})
    # The interrupted-job rollback restores what was saved for that job.
    module.command = lambda args, timeout=120: calls.append([str(a) for a in args]) or ''
    ds.rollback_usage(None, row, ident)
    assert json.loads(saved.read_text())['spec']['usage'] == 'standard' and '--innodb-buffer-pool-size=256M' in yaml.safe_load((root / 'compose.yml').read_text())['services']['database']['command']
