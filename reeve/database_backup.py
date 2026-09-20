"""Native database clients in disposable, identified containers; bounded private output."""
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import yaml

from .core import request_id
from .host import OPS, ENV, atomic, trusted
from . import compose_inspect as ci

from .host import BACKUPS
STAGING = BACKUPS / 'staging/db'
DEFAULT_POLICY = {'staging_limit_mb': 10240, 'artifact_limit_mb': 1024,
                  'reserve_free_mb': 2048, 'timeout_seconds': 900}


class DumpFailed(Exception): pass


def policy():
    config = OPS / 'server.yaml'
    values = yaml.safe_load(ci.regular(config)).get('database_backups', {}) if config.exists() else {}
    if not isinstance(values, dict) or values.keys() - DEFAULT_POLICY.keys():
        raise ValueError('Invalid database backup policy')
    result = {**DEFAULT_POLICY, **values}
    for key, value in result.items():
        if type(value) is not int or not 1 <= value <= (3600 if key == 'timeout_seconds' else 1048576):
            raise ValueError('Invalid database backup limit')
    if result['artifact_limit_mb'] > result['staging_limit_mb']:
        raise ValueError('Artifact limit must fit inside staging limit')
    return result


def artifact_path(ident, partial=False):
    request_id(ident)
    return STAGING / ('.' + ident + '.partial' if partial else ident)


def usage():
    if not STAGING.exists(): return 0
    trusted(STAGING, directory=True)
    total = 0
    for directory, dirs, files in os.walk(STAGING, followlinks=False):
        for name in dirs + files:
            info = (Path(directory) / name).lstat()
            if stat.S_ISLNK(info.st_mode): raise ValueError('Unexpected link in backup staging')
            if stat.S_ISREG(info.st_mode): total += info.st_size
    return total


def free_bytes():
    info = os.statvfs(STAGING)
    return info.f_bavail * info.f_frsize


def capacity(settings):
    maximum = settings['artifact_limit_mb'] * 1048576
    if usage() + maximum + 65536 > settings['staging_limit_mb'] * 1048576:
        raise DumpFailed('Backup staging cap reached. Completed dumps awaiting remote copy were retained; increase capacity or configure remote archival.')
    if free_bytes() < maximum + settings['reserve_free_mb'] * 1048576:
        raise DumpFailed('Insufficient free backup space. Existing dumps were retained.')
    return maximum


def helper_name(ident):
    request_id(ident)
    return 'hosting-dump-' + ident


def cleanup(host, job):
    name = helper_name(job['id'])
    live = host.inspect(name)
    if live:
        if live['Config'].get('Labels', {}).get('hosting.backup') != job['id']:
            raise DumpFailed('Backup helper identity conflict; operator review required.')
        ci.run(['docker', 'rm', '--force', name], raw=True)
    partial = artifact_path(job['id'], partial=True)
    if partial.exists() or partial.is_symlink():
        trusted(partial, directory=True)
        shutil.rmtree(partial)


def checksum(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024): result.update(block)
    return result.hexdigest()


def completed(job):
    root = artifact_path(job['id'])
    if not root.exists(): return None
    trusted(root, directory=True)
    manifest = json.loads(ci.regular(root / 'manifest.json'))
    if manifest.get('schema') != 1 or manifest.get('operation') != job['id'] or manifest.get('site_id') != job['site_id'] or manifest.get('file') not in ('database.sql', 'database.dump'):
        raise DumpFailed('Completed dump manifest needs operator review; artifact retained.')
    path = root / manifest['file']; trusted(path)
    if not path.is_file() or path.stat().st_size != manifest['bytes'] or checksum(path) != manifest['sha256']:
        raise DumpFailed('Completed dump checksum failed; artifact retained for review.')
    return manifest


def client_args(info, user='postgres', database='site', credentials='--defaults-extra-file=/run/backup/admin.cnf'):
    if info['engine'] == 'postgres':
        return ['pg_dump', '--host=127.0.0.1', '--username=' + user, '--dbname=' + database, '--no-password', '--format=custom']
    if info['engine'] not in ('mysql', 'mariadb'): raise DumpFailed('Unsupported database engine; an explicit adapter is required.')
    return [('mariadb-dump' if info['engine'] == 'mariadb' else 'mysqldump'),
            credentials, '--host=127.0.0.1', '--lock-all-tables',
            '--quick', '--routines', '--events', '--triggers', '--hex-blob', '--no-tablespaces',
            *(['--set-gtid-purged=OFF'] if info['engine'] == 'mysql' else []), '--databases', database]


def stream_dump(name, output, maximum, settings):
    """Bound bytes/time/free space. SQL output and stderr never enter diagnostics/logging."""
    with output.open('xb') as stream, open(os.devnull, 'wb') as diagnostic:
        os.chmod(output, 0o600)
        proc = subprocess.Popen(['/usr/bin/prlimit', f'--fsize={maximum}:{maximum}', '--',
            'docker', 'start', '--attach', name], stdout=stream, stderr=diagnostic,
            env=ENV, cwd='/', start_new_session=True)
        deadline = time.monotonic() + settings['timeout_seconds']
        try:
            while proc.poll() is None:
                if time.monotonic() >= deadline: raise DumpFailed('Database dump timed out; no completed artifact was published.')
                if free_bytes() < settings['reserve_free_mb'] * 1048576:
                    raise DumpFailed('Free-space reserve reached; no completed artifact was published.')
                time.sleep(0.1)
            if proc.returncode or output.stat().st_size >= maximum:
                raise DumpFailed('Database client failed or exceeded the artifact size cap. No completed artifact was published.')
            if output.stat().st_size == 0: raise DumpFailed('Database client produced an empty dump.')
            stream.flush(); os.fsync(stream.fileno())
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait()


def dump(host, row, job):
    if json.loads(row['payload']).get('runtime') == 'compose': return dump_package(host, row, job)
    started_at = time.time()
    from .database_site import state, paths
    info = state(row)
    if not info or info.get('stage') != 'ready':
        raise DumpFailed('A ready managed database is required.')
    settings = policy()
    for folder in (STAGING.parent.parent, STAGING.parent, STAGING):
        trusted(folder, directory=True)
    maximum = capacity(settings)
    live = host.inspect('hosting-db-' + row['name'])
    if not live or live['Image'] != info['image_id'] or not live['State']['Running'] or live['Config'].get('Labels', {}).get('hosting.operation') != row['id']:
        raise DumpFailed('Managed database identity or running state differs from its saved configuration.')
    backend = 'hosting-backend-' + row['name']
    if set(live['NetworkSettings']['Networks']) != {backend}:
        raise DumpFailed('Database network differs from the managed private backend.')
    # The job exists before creation. Recovery removes only this labelled helper.
    name = helper_name(job['id'])
    if host.inspect(name): raise DumpFailed('Backup helper already exists; recover the previous attempt first.')
    root = artifact_path(job['id'], partial=True); root.mkdir(mode=0o700)
    dbroot, private = paths(row)
    args = ['docker', 'create', '--name', name, '--label', 'hosting.backup=' + job['id'],
        '--network', 'container:' + live['Id'], '--user', f"{info['uid']}:{info['gid']}", '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--log-driver', 'none',
        '--memory', '256m', '--cpus', '1', '--pids-limit', '64', '--restart', 'no',
        '--tmpfs', '/tmp:rw,nosuid,nodev,size=16m', '--storage-opt', 'size=0']
    if info['engine'] == 'postgres':
        # Native image entrypoint is bypassed. Password is never in argv or returned output.
        atomic(root / 'client.env', 'PGPASSWORD=' + info['admin_password'] + '\nPGCONNECT_TIMEOUT=15\n')
        args += ['--env-file', str(root / 'client.env')]
    else:
        config = dbroot / 'conf/admin.cnf'; trusted(config)
        args += ['--mount', f'type=bind,src={config},dst=/run/backup/admin.cnf,readonly']
    client = client_args(info)
    ci.run([*args, '--entrypoint', client[0], info['image_id'], *client[1:]], raw=True)
    filename = 'database.dump' if info['engine'] == 'postgres' else 'database.sql'
    stream_dump(name, root / filename, maximum, settings)
    finished = host.inspect(name)
    if not finished or finished['State']['Running'] or finished['State']['ExitCode'] != 0:
        raise DumpFailed('Database client did not complete successfully.')
    # Remove private client credentials before publishing a durable, self-describing artifact.
    (root / 'client.env').unlink(missing_ok=True)
    manifest = {'schema': 1, 'kind': 'local-database-dump', 'operation': job['id'], 'site_id': row['id'],
        'site_name': row['name'], 'database': 'site', 'engine': info['engine'], 'version': info['version'],
        'image_id': info['image_id'], 'image': info['image'], 'file': filename,
        'bytes': (root / filename).stat().st_size, 'sha256': checksum(root / filename),
        'started_at': started_at, 'completed_at': time.time(), 'remote_uploaded': False,
        'consistency': 'pg_dump database snapshot' if info['engine'] == 'postgres' else 'global read lock during dump',
        'scope': 'Database site only; excludes files, other databases, roles and host configuration.',
        'application_consistent': False, 'restore_verified': False}
    atomic(root / 'manifest.json', json.dumps(manifest, sort_keys=True))
    os.rename(root, artifact_path(job['id']))
    fd = os.open(STAGING, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return manifest


def dump_package(host, row, job):
    """Same native clients and bounds for a package site's one recognised Compose database service."""
    started_at = time.time()
    from .package_deploy import dump_target
    from . import compose_adopt as ca
    target = dump_target(row) if json.loads(row['payload']).get('package_id') else None
    if not target: raise DumpFailed('This application has no recognised database service for native dumps.')
    settings = policy()
    for folder in (STAGING.parent.parent, STAGING.parent, STAGING):
        trusted(folder, directory=True)
    maximum = capacity(settings)
    live = [c for c in ca.project_containers(ca.read(row))
            if c['Config']['Labels'].get('com.docker.compose.service') == target['service']]
    if len(live) != 1 or live[0]['Image'] != target['image_id'] or not live[0]['State']['Running']:
        raise DumpFailed('Database service identity or running state differs from its saved deployment.')
    live = live[0]
    name = helper_name(job['id'])
    if host.inspect(name): raise DumpFailed('Backup helper already exists; recover the previous attempt first.')
    root = artifact_path(job['id'], partial=True); root.mkdir(mode=0o700)
    user = target['user'] if re.fullmatch('[0-9]+(?::[0-9]+)?', target['user']) else '65534:65534'
    args = ['docker', 'create', '--name', name, '--label', 'hosting.backup=' + job['id'],
        '--network', 'container:' + live['Id'], '--user', user, '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--log-driver', 'none',
        '--memory', '256m', '--cpus', '1', '--pids-limit', '64', '--restart', 'no',
        '--tmpfs', '/tmp:rw,nosuid,nodev,size=16m', '--storage-opt', 'size=0']
    # Native image entrypoint is bypassed. Password is never in argv or returned output.
    variable = 'PGPASSWORD' if target['engine'] == 'postgres' else 'MYSQL_PWD'
    atomic(root / 'client.env', variable + '=' + target['password'] + '\nPGCONNECT_TIMEOUT=15\n')
    args += ['--env-file', str(root / 'client.env')]
    client = client_args(target, user=target['user'], database=target['database'], credentials='--user=root')
    ci.run([*args, '--entrypoint', client[0], target['image_id'], *client[1:]], raw=True)
    filename = 'database.dump' if target['engine'] == 'postgres' else 'database.sql'
    stream_dump(name, root / filename, maximum, settings)
    finished = host.inspect(name)
    if not finished or finished['State']['Running'] or finished['State']['ExitCode'] != 0:
        raise DumpFailed('Database client did not complete successfully.')
    (root / 'client.env').unlink(missing_ok=True)
    manifest = {'schema': 1, 'kind': 'local-database-dump', 'operation': job['id'], 'site_id': row['id'],
        'site_name': row['name'], 'database': target['database'], 'engine': target['engine'],
        'version': target['image'].split(':')[-1] if ':' in target['image'] else 'unknown',
        'image_id': target['image_id'], 'image': target['image'], 'file': filename,
        'bytes': (root / filename).stat().st_size, 'sha256': checksum(root / filename),
        'started_at': started_at, 'completed_at': time.time(), 'remote_uploaded': False,
        'consistency': 'pg_dump database snapshot' if target['engine'] == 'postgres' else 'global read lock during dump',
        'scope': 'Compose service ' + target['service'] + ' database only; excludes files, other databases, roles and host configuration.',
        'application_consistent': False, 'restore_verified': False}
    atomic(root / 'manifest.json', json.dumps(manifest, sort_keys=True))
    os.rename(root, artifact_path(job['id']))
    fd = os.open(STAGING, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return manifest


SELECTS = re.compile(rb'^\s*(?:USE\s+|CREATE\s+DATABASE\s+(?:/\*!\d+\s+IF\s+NOT\s+EXISTS\s*\*/\s*|IF\s+NOT\s+EXISTS\s+)?)`?([^`;\s]+)`?', re.I)


def client_restore(engine, filename):
    """The site database is the client's default: a plain single-database dump has no USE statement,
    while the panel's own dumps (taken with --databases) select `site` themselves."""
    client = 'mariadb' if engine == 'mariadb' else 'mysql'
    return client + ' --defaults-extra-file=/run/backup/admin.cnf --host=127.0.0.1 site < /restore/' + filename


def foreign_databases(path, expected='site'):
    """Database names a MySQL/MariaDB dump selects or creates other than the site's own."""
    found = set()
    with open(path, 'rb') as stream:
        for line in stream:
            match = SELECTS.match(line)
            if match and match.group(1).decode('utf-8', 'replace') != expected: found.add(match.group(1).decode('utf-8', 'replace'))
    return sorted(found)


def restore_managed(host, row, dump_file, dump, ident):
    """Restore a captured native dump into this site's managed database through the same image and identity."""
    import signal
    from .database_site import state, paths
    info = state(row)
    if not info or info.get('stage') != 'ready': raise ValueError('The managed database is not ready for restore.')
    if info['engine'] != dump['engine']: raise ValueError('Captured dump engine differs from the site database.')
    live = host.inspect('hosting-db-' + row['name'])
    if not live or not live['State']['Running']: raise ValueError('The managed database container is not running.')
    dbroot, private = paths(row)
    name = 'hosting-restore-' + ident + '-database'
    if host.inspect(name): ci.run(['docker', 'rm', '--force', name], raw=True)
    # The snapshot is root-only; the client runs as the database identity, so it reads a private copy it owns.
    staged = dbroot / 'conf' / ('restore-' + ident + Path(dump_file).suffix)
    shutil.copyfile(dump_file, staged); os.chmod(staged, 0o400); os.chown(staged, info['uid'], info['gid'])
    args = ['docker', 'run', '--rm', '--name', name, '--label', 'hosting.restore=' + ident,
            '--network', 'container:' + live['Id'], '--user', f"{info['uid']}:{info['gid']}", '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--log-driver', 'none',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=16m', '--mount', f'type=bind,src={staged},dst=/restore/{Path(dump_file).name},readonly']
    envfile = private.parent / ('restore-' + ident + '.env')
    if info['engine'] == 'postgres':
        atomic(envfile, 'PGPASSWORD=' + info['admin_password'] + '\nPGCONNECT_TIMEOUT=15\n')
        args += ['--env-file', str(envfile), '--entrypoint', 'pg_restore', info['image_id'], '--host=127.0.0.1', '--username=postgres',
                 '--dbname=site', '--clean', '--if-exists', '--no-privileges', '--exit-on-error', '/restore/' + Path(dump_file).name]
    else:
        config = dbroot / 'conf/admin.cnf'; trusted(config)
        args += ['--mount', f'type=bind,src={config},dst=/run/backup/admin.cnf,readonly', '--entrypoint', 'sh', info['image_id'],
                 '-c', client_restore(info['engine'], Path(dump_file).name)]
    logs = OPS / 'panel/worker/site-backup-logs'; logs.mkdir(mode=0o700, exist_ok=True)
    try:
        with (logs / (ident + '.log')).open('ab') as output:
            os.fchmod(output.fileno(), 0o600)
            proc = subprocess.Popen(['/usr/bin/prlimit', '--fsize=8388608:8388608', '--', *args], stdout=output, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=ENV, cwd='/', start_new_session=True)
            try: code = proc.wait(policy()['timeout_seconds'])
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait()
                raise ValueError('Database restore timed out; its private diagnostic is retained.') from None
    finally:
        envfile.unlink(missing_ok=True); staged.unlink(missing_ok=True)
    if code: raise ValueError('Database restore failed; its private diagnostic is retained.')


def retention_tick(ledger, now=None):
    """Remove dumps outside the policy window. The newest dump per site always stays, and while a
    remote destination is configured a dump that has not been copied off-machine is never removed."""
    from .retention import policy as retention_policy, keep_dumps
    from .remote_backup import verified_everywhere
    now = time.time() if now is None else now
    days = retention_policy()['database_days']
    with ledger.db() as db:
        rows = [dict(r) for r in db.execute("SELECT id, site_id, created, artifact FROM backup_jobs WHERE state='succeeded' AND cleanup_error=''")]
    verified = verified_everywhere(ledger)   # None: no destination enabled, nothing to wait for
    keep = keep_dumps(rows, now, days)
    for job in rows:
        if job['id'] in keep: continue
        if verified is not None and job['id'] not in verified: continue
        root = artifact_path(job['id'])
        manifest = json.loads(job['artifact']) if job['artifact'] else {}
        with ledger.db() as db:
            db.execute("UPDATE backup_jobs SET state='pruned', updated=? WHERE id=?", (now, job['id']))
        if root.exists():
            trusted(root, directory=True)
            for name in (manifest.get('file', ''), 'manifest.json'):
                if name: (root / name).unlink(missing_ok=True)
            if not any(root.iterdir()): root.rmdir()


def perform(ledger, host, job):
    ledger.finish_backup(job['id'], 'running')
    artifact, error, cleanup_error = None, '', ''
    try:
        artifact = dump(host, ledger.get(job['site_id']), job)
    except DumpFailed as exc: error = str(exc)
    except Exception: error = 'Database dump failed; inspect database availability and backup configuration. Private diagnostics were suppressed.'
    try: cleanup(host, job)
    except Exception: cleanup_error = 'Backup helper cleanup needs recovery; the primary dump result is retained.'
    ledger.finish_backup(job['id'], 'succeeded' if artifact else 'failed', error, artifact, cleanup_error)


def recover(ledger, host):
    for job in ledger.backup_jobs(active=True):
        if job['state'] == 'queued': continue
        artifact = json.loads(job['artifact']) if job['artifact'] else None
        error, cleanup_error = job['error'], ''
        try:
            cleanup(host, job)
            artifact = completed(job)
            if not artifact and not error: error = 'Worker interrupted the database dump; no completed artifact was published.'
        except Exception: cleanup_error = 'Backup recovery needs operator review; existing artifacts were retained.'
        ledger.finish_backup(job['id'], 'succeeded' if artifact else 'failed', error, artifact, cleanup_error)
