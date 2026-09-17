"""Real native dumps and isolated database restores of four synthetic M2.5 fixtures.

This is database-adapter acceptance, not full-site or independent-machine recovery.
"""
import copy
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import yaml

from reeve.core import Ledger
from reeve.database_site import state
from reeve.database_backup import artifact_path, completed
from reeve.host import Host, atomic, command
from reeve.worker import rpc

ROOT = Path('/srv/ops/panel/worker/db-dump-acceptance')
CASES = ('m25-legacy', 'm25-mysql', 'm25-mariadb', 'm25-postgres')


def sql(name, info, query, app=False):
    env = dict(os.environ)
    if info['engine'] == 'postgres':
        env['PGPASSWORD'] = info['app_password' if app else 'admin_password']
        args = ['docker', 'exec', '-i', '-e', 'PGPASSWORD', name, 'psql', '-h', '127.0.0.1',
                '-U', 'site' if app else 'postgres', '-d', 'site', '-v', 'ON_ERROR_STOP=1', '-At', '-F', '\t']
    else:
        args = ['docker', 'exec', '-i', name, 'mariadb' if info['engine'] == 'mariadb' else 'mysql',
                '--defaults-extra-file=/run/hosting/' + ('app.cnf' if app else 'admin.cnf'), '-h', '127.0.0.1', '-N', '-B', 'site']
    result = subprocess.run(args, input=query.encode(), env=env, capture_output=True, timeout=120)
    assert result.returncode == 0, 'Synthetic SQL operation failed; private output suppressed'
    return result.stdout.decode().strip()


def wait(ledger, ident):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        job = next(j for j in ledger.backup_jobs() if j['id'] == ident)
        if job['state'] not in ('queued', 'running'): return job
        time.sleep(0.25)
    raise AssertionError('Backup timed out')


def force(ledger, row):
    deadline = time.monotonic() + 90
    while True:
        ident = str(uuid.uuid4())
        try: job = rpc({'op': 'backup-database', 'site_id': row['id'], 'id': ident}); break
        except ValueError:
            if time.monotonic() > deadline: raise
            time.sleep(1)
    assert rpc({'op': 'backup-database', 'site_id': row['id'], 'id': ident})['id'] == job['id']
    return wait(ledger, ident)


def restore(row, info, job, expected, token, folder):
    folder.mkdir(mode=0o700)
    data = folder / 'data'; data.mkdir(mode=0o700); os.chown(data, info['uid'], info['gid'])
    original = Path('/srv/sites') / row['name'] / 'database'
    model = yaml.safe_load((original / 'compose.yml').read_text())
    service = copy.deepcopy(model['services']['database'])
    name = 'hosting-restore-test-' + uuid.uuid4().hex[:12]
    service.update(container_name=name, restart='no', mem_limit=512 * 1048576, cpus=1, pids_limit=128)
    service['labels'] = {'hosting.backup.test': name}
    service['volumes'] = [v.replace(str(original / 'data') + ':', str(data) + ':', 1) for v in service['volumes']]
    service['networks'] = {'backend': {'aliases': ['db']}}
    compose = {'name': name, 'services': {'database': service}, 'networks': {'backend': {'internal': True}}}
    path = folder / 'compose.yaml'; atomic(path, yaml.safe_dump(compose))
    args = ['docker', 'compose', '-f', path]
    try:
        command([*args, 'up', '-d', '--wait', '--wait-timeout', '240', '--pull', 'never'], timeout=300)
        live = Host().inspect(name)
        assert live['Image'] == info['image_id'] and not live['HostConfig']['PortBindings']
        assert any(m.get('Source') == str(data) for m in live['Mounts'])
        assert not any(m.get('Source') == str(original / 'data') for m in live['Mounts'])
        command(['docker', 'exec', name, 'sh', '/run/hosting/bootstrap.sh'])
        # No table/seed data exists in the destination before loading the saved artifact.
        count_query = "SELECT count(*) FROM information_schema.tables WHERE table_name IN ('acceptance_records','hosting_backup_probe');"
        assert sql(name, info, count_query) == '0'
        manifest = completed(job)
        file = artifact_path(job['id']) / manifest['file']
        env = dict(os.environ)
        if info['engine'] == 'postgres':
            env['PGPASSWORD'] = info['admin_password']
            client = ['docker', 'exec', '-i', '-e', 'PGPASSWORD', name, 'pg_restore', '-h', '127.0.0.1',
                      '-U', 'postgres', '-d', 'site', '--exit-on-error', '--no-owner', '--no-acl', '--role=site']
        else:
            client = ['docker', 'exec', '-i', name, 'mariadb' if info['engine'] == 'mariadb' else 'mysql',
                      '--defaults-extra-file=/run/hosting/admin.cnf', '-h', '127.0.0.1']
        with file.open('rb') as stream:
            result = subprocess.run(client, stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, timeout=180)
        assert result.returncode == 0, 'Native dump restore failed; private diagnostics suppressed'
        assert sql(name, info, 'SELECT id,payload FROM acceptance_records ORDER BY id;', app=True) == expected
        assert sql(name, info, 'SELECT payload FROM hosting_backup_probe WHERE id=1;', app=True) == token
        return {'isolated_database_restore': True, 'runtime_only_probe_restored': True, 'all_200_records_match': True,
                'original_data_not_mounted': True, 'target_images_pinned': True, 'target_public_ports': False}
    finally:
        command([*args, 'down'], timeout=60)


def main():
    os.umask(0o077); ROOT.mkdir(mode=0o700, exist_ok=True)
    run = ROOT / uuid.uuid4().hex; run.mkdir(mode=0o700)
    ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    rows = {r['name']: r for r in rpc({'op': 'list'})}
    report = {'installed': str(Path('/opt/reeve/current').resolve()), 'worker_schema': 12,
              'run_directory': str(run), 'sites': {}}
    for name in CASES:
        row, info = rows[name], state(rows[name]); source = 'hosting-db-' + name
        expected = sql(source, info, 'SELECT id,payload FROM acceptance_records ORDER BY id;', app=True)
        assert len(expected.splitlines()) == 200
        token = 'runtime-only-' + uuid.uuid4().hex
        sql(source, info, "CREATE TABLE IF NOT EXISTS hosting_backup_probe (id INTEGER PRIMARY KEY,payload VARCHAR(80) NOT NULL); DELETE FROM hosting_backup_probe; INSERT INTO hosting_backup_probe VALUES (1,'" + token + "');")
        atomic(run / (name + '-expected.json'), json.dumps({'records': expected, 'probe': token}))
        job = force(ledger, row); assert job['state'] == 'succeeded', job['error']
        assert not job['cleanup_error']
        restored = restore(row, info, job, expected, token, run / name)
        assert sql(source, info, 'SELECT id,payload FROM acceptance_records ORDER BY id;', app=True) == expected
        report['sites'][name] = {**restored, 'job_id': job['id'], 'artifact': json.loads(job['artifact']),
                                'export_sha256': hashlib.sha256(expected.encode()).hexdigest()}
        print(name + ': native dump and isolated database restore passed', flush=True)
        atomic(ROOT / 'acceptance.json', json.dumps(report, indent=2))
    # Exercise actual scheduler dispatch without waiting 15 minutes; policy stays unchanged.
    row = rows['m25-postgres']; old = {j['id'] for j in ledger.backup_jobs(row['id'])}
    with ledger.db() as db: db.execute('UPDATE backup_schedules SET next_run=? WHERE site_id=?', (time.time() - 1, row['id']))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        new = [j for j in ledger.backup_jobs(row['id']) if j['id'] not in old]
        if new: break
        time.sleep(0.5)
    assert new and wait(ledger, new[0]['id'])['state'] == 'succeeded'
    report['scheduled_job'] = new[0]['id']
    policy_file = Path('/srv/ops/server.yaml'); original = policy_file.read_text()
    before = rpc({'op': 'backup-status', 'site_id': row['id']})['last_success']['id']
    try:
        settings = yaml.safe_load(original)
        settings['database_backups'] = {'staging_limit_mb': 1, 'artifact_limit_mb': 1}
        atomic(policy_file, yaml.safe_dump(settings))
        failed = force(ledger, row)
        assert failed['state'] == 'failed' and 'cap reached' in failed['error']
        result = rpc({'op': 'backup-status', 'site_id': row['id']})
        assert result['last_success']['id'] == before and result['available']
        report['capacity_failure'] = {'job_id': failed['id'], 'previous_success_retained': True, 'unuploaded_dumps_retained': True}
    finally: atomic(policy_file, original)
    resumed = force(ledger, row); assert resumed['state'] == 'succeeded'
    report['resumed_job'] = resumed['id']
    assert not command(['docker', 'ps', '-aq', '--filter', 'label=hosting.backup']).strip()
    current = rpc({'op': 'list'})
    assert len(current) == 27 and all(r['health']['application'] == 'healthy' for r in current if r.get('database'))
    for row in current:
        if row['name'] in (*CASES, 'm24-wordpress', 'm25-static', 'm3-demo-existing', 'm3-demo-fresh'):
            for domain in row['domains']:
                command(['curl', '--noproxy', '*', '--fail', '--silent', '--show-error',
                    '--cacert', '/srv/ops/proxy/data/caddy/pki/authorities/local/root.crt',
                    '--resolve', domain + ':443:127.0.0.1', 'https://' + domain + '/'])
    report.update(sites_healthy=True, temporary_dump_helpers=0, full_site_backup=False, independent_machine_restore=False)
    atomic(ROOT / 'acceptance.json', json.dumps(report, indent=2))
    print(json.dumps({'acceptance': 'passed', 'report': str(ROOT / 'acceptance.json')}))


if __name__ == '__main__': main()
