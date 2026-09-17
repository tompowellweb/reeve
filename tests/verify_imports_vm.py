"""M2.5: export only known synthetic tables, then verify retained VM state.

Run as root with the installed panel Python, from the checkout. This is acceptance
tooling, not the panel backup implementation. No customer DB or arbitrary name input.
"""
import argparse
import hashlib
import json
import os
import pwd
import subprocess
import time
from pathlib import Path

from reeve.core import Ledger
from reeve.database_site import state
from reeve.host import Host, atomic, command, project_id, quota_record
from reeve.schedules import list_schedules
from reeve.worker import rpc
from tests.verify_databases_vm import info
from tests.verify_php_vm import request

CASES = {
    'm25-legacy': 'm2-unlimited-php70',
    'm25-mysql': 'm2-db-mysql',
    'm25-mariadb': 'm2-db-mariadb',
    'm25-postgres': 'm2-db-postgres',
}
INPUT = Path('/var/lib/hosting-browser/m25-inputs')
RESULT = Path('/srv/ops/panel/worker/imports-acceptance.json')


def prepare():
    rows = {r['name']: r for r in rpc({'op': 'list'})}
    assert not any(name in rows for name in [*CASES, 'm25-static']), 'Use existing inputs to resume; do not overwrite an earlier run'
    assert not INPUT.exists(), 'Retain existing export inputs'
    account = pwd.getpwnam('hosting-browser')
    INPUT.mkdir(mode=0o700)
    os.chown(INPUT, account.pw_uid, account.pw_gid)
    report = {'exported_at': time.time(), 'sources': {}}
    for name, source in CASES.items():
        before = info(source)
        db = state(rows[source])
        engine = db['engine']
        if engine == 'postgres':
            variable = 'PGPASSWORD'
            client = ['pg_dump', '-h', '127.0.0.1', '-U', 'site', '--no-password', '--no-owner', '--no-privileges', '--table=acceptance_records', 'site']
        else:
            variable = 'MYSQL_PWD'
            client = ['mariadb-dump' if engine == 'mariadb' else 'mysqldump', '-h', '127.0.0.1', '-u', 'site', '--single-transaction', '--no-tablespaces']
            if engine == 'mysql': client.append('--set-gtid-purged=OFF')
            client.extend(['site', 'acceptance_records'])
        env = dict(os.environ, **{variable: db['app_password']})
        dump = subprocess.run(['docker', 'exec', '-e', variable, 'hosting-db-'+source, *client], env=env, capture_output=True, timeout=120)
        assert dump.returncode == 0, dump.stderr.decode().replace(db['app_password'], '[REDACTED]')
        path = INPUT/(name+'.sql')
        path.write_bytes(dump.stdout)
        path.chmod(0o600); os.chown(path, account.pw_uid, account.pw_gid)
        assert info(source) == before
        report['sources'][name] = {'source': source, 'source_id': rows[source]['id'], 'engine': engine,
            'version': db['version'], 'image': db['image'], 'bytes': len(dump.stdout),
            'sha256': hashlib.sha256(dump.stdout).hexdigest(), 'export': before, 'client': client}
    path = INPUT/'sources.json'; path.write_text(json.dumps(report, indent=2)); path.chmod(0o600)
    os.chown(path, account.pw_uid, account.pw_gid)
    atomic(RESULT, json.dumps({'source_exports': report}, indent=2))
    print(json.dumps({'native_exports': len(CASES), 'inputs': str(INPUT)}))


def final():
    rows = rpc({'op': 'list'})
    assert len(rows) == 25
    assert all(r['state'] == 'succeeded' and r['health']['application'] == 'healthy' for r in rows)
    assert all(r['database']['health'] == 'healthy' for r in rows if r['database'])
    https = {}
    for row in rows:
        for domain in row['domains']:
            code, _ = request(dict(row, domain=domain))
            assert code in (200, 301, 302, 308), (domain, code)
            https[domain] = code
    original = {r['name']: info(r['name']) for r in rows if r['database'] and r['name'] != 'm24-wordpress' and r['name'] not in CASES}
    assert len(original) == 5
    fixtures = {}
    for row in rows:
        if row['name'] not in CASES: continue
        db = state(row); root = Path('/srv/sites')/row['name']; data = root/'database/data'
        assert data.stat().st_uid == db['uid'] != row['uid']
        assert project_id(data)[0] == row['project']
        for path in (root/'html').rglob('*'):
            assert path.stat().st_uid == row['uid'], str(path)
            assert project_id(path)[0] == row['project'], str(path)
        for prefix in ('hosting-site-', 'hosting-php-', 'hosting-db-'):
            live = Host().inspect(prefix+row['name'])
            assert live['HostConfig']['Memory'] == 0 and live['HostConfig']['NanoCpus'] == 0
            assert command(['docker', 'exec', prefix+row['name'], 'cat', '/sys/fs/cgroup/pids.max']).strip() == 'max'
            assert not live['HostConfig']['PortBindings']
        fixtures[row['name']] = {'uid': row['uid'], 'database_uid': db['uid'], 'project': row['project'],
            'quota': quota_record(row['project']), 'caps_unlimited': True, 'ports_unpublished': True}
    ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    assert not any(j['state'] in ('queued', 'running', 'recovery-needed') and j['kind'] != 'tool' for j in ledger.content_jobs())
    # Schedules continue running: inspect pending jobs, rather than assuming a quiet instant.
    with ledger.db() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 10
    wp = next(r for r in rows if r['name'] == 'm24-wordpress')
    schedule = next(s for s in list_schedules(ledger, wp['id']) if s['name'] == 'wordpress')
    assert schedule['enabled'] and schedule['last_state'] == 'succeeded', schedule
    pending = [j for j in ledger.content_jobs() if j['state'] in ('queued', 'running', 'recovery-needed')]
    assert all(j['site_id'] == wp['id'] and j['kind'] == 'tool' and j['state'] != 'recovery-needed' for j in pending)
    temporary = command(['docker', 'ps', '-a', '--filter', 'label=hosting.content', '--format', '{{.Names}}']).split()
    assert all(name in ['hosting-tool-'+j['id'] for j in pending] for name in temporary), temporary
    toolboxes = command(['docker', 'ps', '-a', '--filter', 'name=hosting-toolbox-', '--format', '{{.Names}}']).split()
    assert not toolboxes, toolboxes
    failed_units = command(['systemctl', '--failed', '--no-legend']).strip()
    assert not failed_units, failed_units
    checkout = Path(__file__).resolve().parents[1]
    report = json.loads(RESULT.read_text())
    report['final'] = {'checked_at': time.time(), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'installed': str(Path('/opt/reeve/current').resolve()),
        'checkout': command(['git', '-c', 'safe.directory='+str(checkout), '-C', checkout, 'rev-parse', 'HEAD']).strip(),
        'sites': len(rows), 'databases': sum(bool(r['database']) for r in rows), 'all_healthy': True,
        'caddy_https': https, 'original_exports': original, 'fixtures': fixtures, 'wordpress_schedule': schedule,
        'pending_jobs': [{'id': j['id'], 'state': j['state'], 'site_id': j['site_id']} for j in pending],
        'temporary_tools': temporary, 'toolboxes': toolboxes, 'failed_units': failed_units,
        'available_bytes': os.statvfs('/srv').f_bavail * os.statvfs('/srv').f_frsize}
    atomic(RESULT, json.dumps(report, indent=2))
    print(json.dumps({'sites': len(rows), 'databases': report['final']['databases'], 'all_healthy': True, 'original_exports_preserved': len(original)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('mode', choices=['prepare', 'final'])
    globals()[parser.parse_args().mode]()
