"""Complete site backup, fresh-folder restore and protected deletion for both hosting modes.

Mode 2 (package) sites: the site folder (all bind mounts and inputs), non-database named volumes,
the effective Compose/plan and a fresh native dump. Restore submits the captured files as a new
package with the fresh dump in dumps/ and repopulates volumes before the first start.

Mode 1 (managed static/PHP) sites: the site folder minus the live database data directory, the
private database record, schedules, owner notes, the Create settings and a fresh native dump.
Restore runs an ordinary Create with the same settings and pinned database identity, then a
durable second phase puts the content, dump, schedules and routing profile back.

Delete requires a successful final backup before anything is removed. A nightly schedule keeps
the newest `keep` scheduled/manual backups per site; final backups are never pruned.
"""
import hashlib
import json
import os
import shutil
import tarfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import compose_adopt as ca, compose_inspect as ci, package_deploy as pd
from .core import request_id, validate_create
from .retention import policy as retention_policy, describe
from .host import BACKUPS, OPS, SITES, PROXY, atomic, trusted, command

STAGING = BACKUPS / 'staging/site'
RESERVE_BYTES = 2 * 1024**3
DUMP_EXTENSION = {'database.sql': '.sql', 'database.dump': '.dump'}
DEFAULT_POLICY = {'hour': 3}
MANAGED_EXCLUDES = ('./database/data', './.tools')
EXPORTS = OPS / 'panel/web/downloads'
UPLOADS = OPS / 'panel/web/backup-uploads'
EXPORT_TTL = 6 * 3600
PRUNABLE = ('scheduled', 'manual', 'pre-restore')
STOPS = OPS / 'panel/worker/site-backup-stops'


class SiteBackupFailed(Exception): pass


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS site_backups (
        id TEXT PRIMARY KEY, site_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, step TEXT NOT NULL,
        error TEXT NOT NULL, manifest TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS site_deletes (
        id TEXT PRIMARY KEY, site_id TEXT NOT NULL, backup_id TEXT NOT NULL, state TEXT NOT NULL,
        step TEXT NOT NULL, error TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS site_restores (
        id TEXT PRIMARY KEY, site_id TEXT NOT NULL, snapshot TEXT NOT NULL, state TEXT NOT NULL,
        step TEXT NOT NULL, error TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
    db.execute('CREATE TABLE IF NOT EXISTS site_backup_schedules (site_id TEXT PRIMARY KEY, next_run REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS site_backup_options (site_id TEXT PRIMARY KEY, quiesce INTEGER NOT NULL DEFAULT 0)')
    columns = {r[1] for r in db.execute('PRAGMA table_info(site_restores)')}
    if 'scope' not in columns: db.execute("ALTER TABLE site_restores ADD COLUMN scope TEXT NOT NULL DEFAULT 'full'")
    if 'safety_backup' not in columns: db.execute("ALTER TABLE site_restores ADD COLUMN safety_backup TEXT NOT NULL DEFAULT ''")


def policy(document=None):
    config = OPS / 'server.yaml'
    values = (document if document is not None else (yaml.safe_load(ci.regular(config)) if config.exists() else {})).get('site_backups', {})
    if not isinstance(values, dict) or values.keys() - {'hour', 'keep'}: raise ValueError('Invalid site backup policy')
    result = {'hour': values.get('hour', DEFAULT_POLICY['hour'])}  # `keep` is superseded by the retention policy and ignored.
    if type(result['hour']) is not int or not 0 <= result['hour'] <= 23: raise ValueError('Invalid site backup hour')
    return result


def artifact_path(ident, partial=False):
    request_id(ident)
    return STAGING / ('.' + ident + '.partial' if partial else ident)


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1048576): digest.update(block)
    return digest.hexdigest()


def tree_bytes(root):
    total = 0
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in files:
            total += (Path(folder) / name).lstat().st_size
    return total


def kind_of(row):
    payload = json.loads(row['payload'])
    if payload.get('runtime') == 'compose':
        return 'package' if payload.get('package_id') else None
    return 'managed'


def supported(row):
    return row['state'] == 'succeeded' and kind_of(row) is not None


def site_for(ledger, site_id):
    row = ledger.get(site_id)
    if kind_of(row) is None:
        raise ValueError('Complete site backups, restore and delete cover managed sites and Compose packages only.')
    return row


def staging():
    for folder in (STAGING.parent.parent, STAGING.parent): trusted(folder, directory=True)
    STAGING.mkdir(mode=0o700, exist_ok=True); trusted(STAGING, directory=True)


def run_tar(args, ident, timeout):
    """Archives are large, so no output-size guard applies; tar's own diagnostics stay root-private."""
    import signal
    import subprocess
    from .host import ENV
    logs = OPS / 'panel/worker/site-backup-logs'; logs.mkdir(mode=0o700, exist_ok=True); trusted(logs, directory=True)
    with (logs / (ident + '.log')).open('ab') as output:
        os.fchmod(output.fileno(), 0o600)
        proc = subprocess.Popen(['tar', *args], stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=ENV, cwd='/', start_new_session=True)
        try: code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            raise SiteBackupFailed('Archiving timed out; its private diagnostic is retained.') from None
    if code: raise SiteBackupFailed('tar exited with status ' + str(code) + '; its private diagnostic is retained.')


def archive_tree(source, target, timeout=1800, ident='archive', excludes=()):
    """Numeric ownership and modes; hard links dereferenced so the archive is a plain package."""
    run_tar(['--numeric-owner', '--hard-dereference', '--one-file-system', '--xattrs',
             *['--exclude=' + e for e in excludes], '-cf', str(target), '-C', str(source), '.'], ident, timeout)
    with tarfile.open(target) as archive:
        entries = 0
        for member in archive:
            if not (member.isfile() or member.isdir() or member.issym()):
                raise SiteBackupFailed('The site folder contains special files that a package cannot carry: ' + member.name)
            entries += 1
    return entries


def fresh_dump(ledger, host, row, partial, manifest, step):
    """A normal dump job, so the remote uploader and the site page see it like any other."""
    from .database_backup import dump, cleanup, DumpFailed, artifact_path as dump_path
    from .backup_jobs import supported as dump_supported
    if not dump_supported(row): return
    service = 'database'
    if kind_of(row) == 'package': service = pd.dump_target(row)['service']
    step('taking a fresh native dump of ' + service)
    dump_id = str(uuid.uuid4()); now = time.time()
    with ledger.db() as db:
        db.execute("INSERT INTO backup_jobs VALUES (?,?,'running','','','',?,?)", (dump_id, row['id'], now, now))
    job = {'id': dump_id, 'site_id': row['id']}
    artifact, error = None, ''
    try: artifact = dump(host, row, job)
    except DumpFailed as exc: error = str(exc)
    except Exception: error = 'Database dump failed; private diagnostics were suppressed.'
    try: cleanup(host, job)
    except Exception: pass
    ledger.finish_backup(dump_id, 'succeeded' if artifact else 'failed', error, artifact)
    if not artifact: raise SiteBackupFailed('The fresh database dump failed: ' + error)
    folder = partial / 'dumps' / service; folder.mkdir(parents=True, mode=0o700)
    for name in (artifact['file'], 'manifest.json'):
        shutil.copyfile(dump_path(dump_id) / name, folder / name); (folder / name).chmod(0o600)
    manifest['dumps'][service] = {'job_id': dump_id, 'file': 'dumps/' + service + '/' + artifact['file'],
                                  'bytes': artifact['bytes'], 'sha256': artifact['sha256'], 'engine': artifact['engine'],
                                  'database': artifact['database'], 'consistency': artifact['consistency']}


def options(ledger, row):
    with ledger.db() as db:
        found = db.execute('SELECT quiesce FROM site_backup_options WHERE site_id=?', (row['id'],)).fetchone()
    return {'quiesce': bool(found and found[0])}


def set_options(ledger, row, quiesce):
    if type(quiesce) is not bool: raise ValueError('Choose whether to pause the site during backups.')
    with ledger.db() as db:
        db.execute('INSERT OR REPLACE INTO site_backup_options VALUES (?,?)', (row['id'], int(quiesce)))
    return options(ledger, row)


def writers(host, row, kind):
    """Running containers that write application state: everything except the database service."""
    if kind == 'managed':
        names = ['hosting-site-' + row['name'], 'hosting-php-' + row['name']]
        found = [host.inspect(n) for n in names]
        return [c['Id'] for c in found if c and c['State']['Running']]
    plan = ca.read(row); state = pd.load(row)
    return [c['Id'] for c in ca.project_containers(plan)
            if c['State']['Running'] and c['Config']['Labels'].get('com.docker.compose.service') not in state.get('databases', {})]


def quiesce_stop(host, row, job, kind):
    """Stop the site's writers for the copy. The intent is durable before the first stop so a dead
    worker can start them again; only containers that were running are ever started."""
    STOPS.mkdir(mode=0o700, exist_ok=True); trusted(STOPS, directory=True)
    ids = writers(host, row, kind)
    atomic(STOPS / (job['id'] + '.json'), json.dumps({'site_id': row['id'], 'containers': ids, 'started_at': time.time()}))
    for ident in ids: command(['docker', 'stop', '--timeout', '30', ident])
    return ids


def quiesce_resume(job):
    marker = STOPS / (job['id'] + '.json')
    if not marker.exists(): return []
    trusted(marker)
    record = json.loads(marker.read_text())
    for ident in record['containers']: command(['docker', 'start', ident])
    marker.unlink()
    return record['containers']


def copy_private(source, target):
    if source.exists() and not source.is_symlink():
        trusted(source); shutil.copyfile(source, target); target.chmod(0o600); return True
    return False


def capture(ledger, host, row, job, step=lambda value: None):
    started = time.time()
    kind = kind_of(row)
    if row['state'] != 'succeeded' or kind is None: raise SiteBackupFailed('Only a published site can be backed up.')
    staging()
    root = SITES / row['name']; trusted(root, directory=True)
    payload = json.loads(row['payload'])
    manifest = {'schema': 1, 'kind': 'site-backup', 'site_kind': kind, 'operation': job['id'], 'backup_kind': job['kind'],
                'site_id': row['id'], 'site_name': row['name'], 'domains': ledger.domains(row), 'quota_mb': payload['data_mb'],
                'volumes': {}, 'dumps': {}, 'started_at': started, 'application_consistent': False, 'restore_verified': False}
    volumes = {}
    if kind == 'package':
        plan = ca.read(row); state = pd.load(row)
        if state.get('stage') != 'published': raise SiteBackupFailed('Only a published package site can be backed up.')
        for key, volume in plan['volumes'].items():
            record = ca.volume_record(volume['name'])
            if not record: raise SiteBackupFailed('Named volume ' + volume['name'] + ' is missing; nothing was written.')
            consumers = [s for s, spec in plan['model']['services'].items()
                         if any(m.get('type') == 'volume' and m.get('source') == key for m in spec.get('volumes', []))]
            databases = [s for s in consumers if s in state.get('databases', {})]
            volumes[key] = {'name': volume['name'], 'mountpoint': record['Mountpoint'], 'consumers': consumers,
                            'method': 'logical dump' if databases and databases == consumers else 'raw copy (not quiesced)'}
        manifest.update(package_id=row['id'], project_name=plan['project_name'], route=plan['route'], images=plan['images'],
                        image_refs={s['name']: s['image'] for s in plan['summary']['services']}, volumes=volumes,
                        consistency='files and non-database volumes copied live; databases from a fresh native dump taken during this backup')
        excludes = ()
    else:
        from .database_site import state as db_state
        from .requests_site import public as web_settings
        from .php_settings import public as php_settings
        from .sftp import public as sftp
        from .mail import extra_senders
        info = db_state(row)
        access = sftp(row)
        from .php_settings import stored as php_stored
        manifest.update(managed={'runtime': payload.get('runtime', 'static'), 'payload': payload,
                                 'php_branch': ledger.runtime_branch(row) if payload.get('runtime') == 'php' else None,
                                 'web_settings': web_settings(row), 'php_settings': php_stored(row), 'sftp_access': {'secondary': access['secondary']} if access else None, 'mail_senders': extra_senders(row) if payload.get('runtime') == 'php' else [],
                                 'database': {k: info[k] for k in ('engine', 'version', 'series', 'image', 'image_id', 'spec') if k in info} if info else None},
                        consistency='site files copied live; the database from a fresh native dump taken during this backup; the live database data directory is excluded')
        excludes = MANAGED_EXCLUDES
    needed = tree_bytes(root) + sum(tree_bytes(v['mountpoint']) for v in volumes.values() if v['method'] != 'logical dump')
    free = os.statvfs(STAGING); free = free.f_bavail * free.f_frsize
    if free < needed * 1.2 + RESERVE_BYTES:
        raise SiteBackupFailed('Not enough free backup space for this site; nothing was written.')
    partial = artifact_path(job['id'], partial=True)
    if partial.exists(): shutil.rmtree(partial)
    partial.mkdir(mode=0o700)
    quiesce = options(ledger, row)['quiesce']
    stopped = []
    try:
        if quiesce:
            step('pausing the site for a consistent copy')
            stopped = quiesce_stop(host, row, job, kind)
            manifest['application_consistent'] = True
            manifest['consistency'] = ('application containers stopped for the copy (' + str(len(stopped)) + ' stopped); '
                                       + 'database dumped and files archived while stopped, then restarted')
        fresh_dump(ledger, host, row, partial, manifest, step)
        step('archiving the site folder')
        entries = archive_tree(root, partial / 'files.tar', ident=job['id'], excludes=excludes)
    finally:
        if quiesce:
            step('resuming the site')
            quiesce_resume(job)
    (partial / 'files.tar').chmod(0o600)
    manifest['files'] = {'file': 'files.tar', 'bytes': (partial / 'files.tar').stat().st_size,
                         'sha256': checksum(partial / 'files.tar'), 'entries': entries, 'source': str(root), 'excluded': list(excludes)}
    for key, volume in volumes.items():
        if volume['method'] == 'logical dump': continue
        step('archiving volume ' + volume['name'])
        (partial / 'volumes').mkdir(mode=0o700, exist_ok=True)
        archive_tree(Path(volume['mountpoint']), partial / 'volumes' / (key + '.tar'), ident=job['id'])
        (partial / 'volumes' / (key + '.tar')).chmod(0o600)
        volume.update(file='volumes/' + key + '.tar', bytes=(partial / 'volumes' / (key + '.tar')).stat().st_size,
                      sha256=checksum(partial / 'volumes' / (key + '.tar')))
    step('recording configuration')
    config = partial / 'config'; config.mkdir(mode=0o700)
    if kind == 'package':
        for name in ('plan.json', 'resolved.compose.json', 'compose.hosting.yaml'):
            shutil.copyfile(ca.plan_path(row['id']) / name, config / name); (config / name).chmod(0o600)
        shutil.copyfile(pd.state_path(row['id']) / 'state.json', config / 'package-state.json'); (config / 'package-state.json').chmod(0o600)
    else:
        from .database_site import STATE as DB_STATE
        from .recovery_context import STORE as NOTES
        recorded = {'database': copy_private(DB_STATE / (row['id'] + '.json'), config / 'database.json'),
                    'schedules': copy_private(OPS / 'panel/worker/schedules' / (row['id'] + '.json'), config / 'schedules.json'),
                    'notes': copy_private(NOTES / (row['id'] + '.json'), config / 'notes.json')}
        manifest['managed']['recorded'] = recorded
    manifest['coverage'] = 'complete' if all(v['method'] == 'logical dump' or v.get('file') for v in volumes.values()) else 'incomplete'
    manifest['completed_at'] = time.time()
    manifest['installed'] = str(Path('/opt/reeve/current').resolve()) if Path('/opt/reeve/current').exists() else ''
    atomic(partial / 'manifest.json', json.dumps(manifest, sort_keys=True))
    os.rename(partial, artifact_path(job['id']))
    fd = os.open(STAGING, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    return manifest


def completed(job):
    root = artifact_path(job['id'])
    if not root.exists(): return None
    trusted(root, directory=True)
    manifest = json.loads(ci.regular(root / 'manifest.json'))
    if manifest.get('schema') != 1 or manifest.get('operation') != job['id'] or manifest.get('site_id') != job['site_id']:
        raise SiteBackupFailed('Completed site backup manifest needs operator review; artifact retained.')
    files = root / manifest['files']['file']
    if not files.is_file() or files.stat().st_size != manifest['files']['bytes'] or checksum(files) != manifest['files']['sha256']:
        raise SiteBackupFailed('Completed site backup checksum failed; artifact retained for review.')
    return manifest


def perform(ledger, host, job):
    ledger.finish_site_backup(job['id'], 'running')
    manifest, error = None, ''
    try:
        row = site_for(ledger, job['site_id'])
        manifest = capture(ledger, host, row, job, lambda value: ledger.finish_site_backup(job['id'], 'running', step=value))
    except (SiteBackupFailed, ValueError) as exc: error = str(exc)
    except Exception: error = 'Site backup failed; private diagnostics were suppressed. Existing backups were retained.'
    if not manifest:
        partial = artifact_path(job['id'], partial=True)
        if partial.exists(): shutil.rmtree(partial)
    ledger.finish_site_backup(job['id'], 'succeeded' if manifest else 'failed', error, manifest)
    return manifest


def recover(ledger):
    # A dead worker may have left a site paused: start its writers before anything else.
    if STOPS.is_dir():
        for marker in sorted(STOPS.glob('*.json')):
            try: quiesce_resume({'id': marker.stem})
            except Exception: pass
    for job in ledger.site_backups(active=True):
        if job['state'] == 'queued': continue
        try:
            partial = artifact_path(job['id'], partial=True)
            if partial.exists(): shutil.rmtree(partial)
            manifest = completed(job)
        except Exception: manifest = None
        ledger.finish_site_backup(job['id'], 'succeeded' if manifest else 'failed',
                                  '' if manifest else 'Worker interrupted the site backup; no completed artifact was published.', manifest)
    for job in ledger.site_restores(active=True):
        if job['state'] == 'running':
            ledger.finish_site_restore(job['id'], 'recovery-needed', job['step'], 'Worker interrupted the restore; retry continues it.')


def status(ledger, row):
    jobs = ledger.site_backups(row['id'])
    latest = jobs[0] if jobs else None
    success = next((j for j in jobs if j['state'] == 'succeeded'), None)
    manifest = json.loads(success['manifest']) if success and success['manifest'] else None
    available = bool(manifest and (artifact_path(success['id']) / manifest['files']['file']).is_file())
    with ledger.db() as db:
        schedule = db.execute('SELECT * FROM site_backup_schedules WHERE site_id=?', (row['id'],)).fetchone()
    snapshots = []
    for j in jobs:
        if j['state'] != 'succeeded' or not j['manifest'] or not artifact_path(j['id']).is_dir(): continue
        m = json.loads(j['manifest'])
        snapshots.append({'id': j['id'], 'kind': j['kind'], 'completed_at': m.get('completed_at'),
                          'bytes': m['files']['bytes'] + sum(d['bytes'] for d in m['dumps'].values())})
    return {'supported': supported(row), 'latest': latest, 'last_success': success,
            'manifest': {k: manifest.get(k) for k in ('coverage', 'completed_at', 'consistency', 'application_consistent', 'restore_verified', 'site_kind')} | {
                'bytes': manifest['files']['bytes'] + sum(v.get('bytes', 0) for v in manifest['volumes'].values()) + sum(d['bytes'] for d in manifest['dumps'].values()),
                'dumps': sorted(manifest['dumps']), 'volumes': {k: v['method'] for k, v in manifest['volumes'].items()}} if manifest else None,
            'available': available, 'snapshots': snapshots, 'options': options(ledger, row),
            'schedule': dict(schedule) if schedule else None, 'policy': policy(), 'retention': retention_policy(), 'retention_text': describe(retention_policy())}


def next_run_after(now, hour):
    moment = datetime.fromtimestamp(now)
    candidate = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= moment: candidate += timedelta(days=1)
    return candidate.timestamp()


def tick(ledger, now=None):
    """Nightly per-site backups on the server hour; a missed night runs once when the worker returns."""
    now = time.time() if now is None else now
    settings = policy()
    with ledger.db() as db:
        for row in ledger.list():
            if supported(row):
                db.execute('INSERT OR IGNORE INTO site_backup_schedules VALUES (?,?)', (row['id'], next_run_after(now, settings['hour'])))
        due = [dict(r) for r in db.execute('SELECT * FROM site_backup_schedules WHERE next_run<=? ORDER BY next_run', (now,))]
    for item in due:
        try: ledger.submit_site_backup(str(uuid.uuid4()), item['site_id'], kind='scheduled')
        except ValueError: continue  # Another operation holds the site; leave it due.
        with ledger.db() as db:
            db.execute('UPDATE site_backup_schedules SET next_run=? WHERE site_id=? AND next_run=?',
                       (next_run_after(now, settings['hour']), item['site_id'], item['next_run']))


def prune(ledger, now=None):
    """Tiered retention per live site; final and imported backups are never pruned."""
    from .retention import policy as retention_policy, keep_site_backups
    rule = retention_policy()['site']; now = time.time() if now is None else now
    for row in ledger.list():
        succeeded = [j for j in ledger.site_backups(row['id'], limit=None) if j['state'] == 'succeeded']
        entries = [{'id': j['id'], 'kind': j['kind'], 'completed_at': (json.loads(j['manifest']).get('completed_at') if j['manifest'] else None)} for j in succeeded]
        keep = keep_site_backups(entries, now, rule)
        for job in [j for j in succeeded if j['id'] not in keep and j['kind'] in PRUNABLE]:
            root = artifact_path(job['id'])
            if root.is_dir() and not root.is_symlink():
                trusted(root, directory=True); shutil.rmtree(root)
            ledger.finish_site_backup(job['id'], 'pruned', '', json.loads(job['manifest']) if job['manifest'] else None)


def package_archive(root, manifest, package):
    with tarfile.open(root / manifest['files']['file']) as source, tarfile.open(package, 'w:gz') as target:
        for member in source:
            clean = member.name[2:] if member.name.startswith('./') else member.name
            if not clean or clean == '.': continue
            if clean == 'dumps' or clean.startswith('dumps/'): continue
            member.name = clean
            target.addfile(member, source.extractfile(member) if member.isfile() else None)
        if manifest['dumps']:
            info = tarfile.TarInfo('dumps'); info.type = tarfile.DIRTYPE; info.mode = 0o755; target.addfile(info)
        for service, dump in manifest['dumps'].items():
            path = root / dump['file']
            info = tarfile.TarInfo('dumps/' + service + DUMP_EXTENSION[Path(dump['file']).name]); info.size = path.stat().st_size; info.mode = 0o644
            with path.open('rb') as stream: target.addfile(info, stream)
    package.chmod(0o600)


def snapshot(ident):
    request_id(ident)
    root = artifact_path(ident)
    if not root.is_dir() or root.is_symlink(): raise ValueError('Unknown site backup.')
    trusted(root, directory=True)
    manifest = json.loads(ci.regular(root / 'manifest.json'))
    if manifest.get('schema') != 1 or manifest.get('kind') != 'site-backup': raise ValueError('Not a site backup.')
    files = root / manifest['files']['file']; trusted(files)
    if checksum(files) != manifest['files']['sha256']: raise ValueError('Site backup files archive failed its checksum; restore refused.')
    for service, dump in manifest['dumps'].items():
        if checksum(root / dump['file']) != dump['sha256']: raise ValueError('Site backup dump failed its checksum; restore refused.')
    return root, manifest


def restore(ledger, host, ident, name, domain):
    """Always a new site. Packages redeploy from the capture; managed sites Create then refill."""
    root, manifest = snapshot(ident)
    validate_create({'name': name, 'domain': domain})
    if manifest.get('site_kind', 'package') == 'package':
        new = str(uuid.uuid4())
        data = {'name': name, 'domain': domain, 'service': manifest['route']['web_service'], 'port': manifest['route']['internal_port']}
        pd.STORE.mkdir(mode=0o700, exist_ok=True); trusted(pd.STORE, directory=True)
        private = pd.state_path(new); private.mkdir(mode=0o700); trusted(private, directory=True)
        package_archive(root, manifest, private / 'package')
        try: row = pd.submit(ledger, host, new, data, checksum(private / 'package'))
        except ValueError:
            shutil.rmtree(private); raise
        state = pd.load(row)
        state['restore_from'] = {'snapshot': ident, 'site_id': manifest['site_id'], 'site_name': manifest['site_name'],
                                 'volumes': {k: v['file'] for k, v in manifest['volumes'].items() if v.get('file')}}
        pd.save(row, state)
        return row
    managed = manifest['managed']; payload = managed['payload']
    data = {k: v for k, v in payload.items() if k in ('runtime', 'php_version', 'data_mb', 'layer_mb', 'memory_mb', 'cpus', 'pids_limit')}
    data.update(name=name, domain=domain)
    if managed.get('php_branch'): data['php_version'] = managed['php_branch']
    if managed.get('database'): data['database'] = managed['database']['spec']
    if data.get('runtime') == 'php':
        from .versions import require
        require(data.get('php_version'))
    new = str(uuid.uuid4())
    recorded = root / 'config/database.json'
    if managed.get('database') and recorded.exists():
        from .database_site import STATE as DB_STATE
        info = json.loads(ci.regular(recorded))
        if info.get('spec') != data['database']: raise ValueError('Captured database record does not match its settings; restore refused.')
        if not command(['docker', 'image', 'ls', '-q', info['image']]).strip():
            command(['docker', 'pull', info['image']], timeout=900)
        info.update(operation_id=new, stage='resolved')
        DB_STATE.mkdir(mode=0o700, exist_ok=True); trusted(DB_STATE, directory=True)
        atomic(DB_STATE / (new + '.json'), json.dumps(info, indent=2))
    row = ledger.submit(new, data)
    with ledger.db() as db:
        now = time.time()
        db.execute("INSERT INTO site_restores (id, site_id, snapshot, state, step, error, created, updated, scope) VALUES (?,?,?,'queued','waiting for site creation','',?,?,'full')", (str(uuid.uuid4()), new, ident, now, now))
    return row


def restore_volumes(row, plan, state):
    """Repopulate captured non-database volumes once, before any service starts."""
    source = state.get('restore_from')
    if not source or not source.get('volumes'): return
    root = artifact_path(source['snapshot']); trusted(root, directory=True)
    done = state.setdefault('volumes_restored', [])
    for key, file in source['volumes'].items():
        if key in done or key not in plan['volumes']: continue
        record = ca.volume_record(plan['volumes'][key]['name'])
        if not record: raise ValueError('Volume ' + key + ' was not created; retry the deployment.')
        mountpoint = Path(record['Mountpoint'])
        if any(mountpoint.iterdir()): raise ValueError('Volume ' + key + ' is not empty; it will not be overwritten by the backup.')
        archive = root / file; trusted(archive)
        run_tar(['--numeric-owner', '--xattrs', '-xf', str(archive), '-C', str(mountpoint)], row['id'], 1800)
        done.append(key); pd.save(row, state)


def extract_managed(root, files, uid, gid, folders):
    """Content trees under the site's own identity; control files stay root's. Python's data filter refuses escapes."""
    with tarfile.open(files) as archive:
        wanted = [m for m in archive if any(m.name == './' + f or m.name.startswith('./' + f + '/') for f in folders)]
        archive.extractall(root, members=wanted, filter='data')
    for folder in folders:
        target = root / folder
        if not target.exists(): continue
        os.chown(target, uid, gid, follow_symlinks=False)
        for current, dirs, names in os.walk(target, followlinks=False):
            for name in dirs + names:
                os.chown(Path(current) / name, uid, gid, follow_symlinks=False)


def restore_into(ledger, host, ident, site_id, scope):
    """Put a snapshot's files or database into an existing managed site; a safety backup is taken first."""
    if scope not in ('files', 'database'): raise ValueError('Choose files or database for an in-place restore.')
    row = site_for(ledger, site_id)
    if kind_of(row) != 'managed': raise ValueError('In-place restore is for managed sites; restore a Compose package to a new site instead.')
    root, manifest = snapshot(ident)
    if manifest.get('site_kind') != 'managed': raise ValueError('This backup is a Compose package; restore it to a new site.')
    if scope == 'database':
        from .database_site import state as db_state
        info = db_state(row); dump = manifest['dumps'].get('database')
        if not dump: raise ValueError('This backup holds no database dump.')
        if not info or info.get('stage') != 'ready': raise ValueError('This site has no ready managed database.')
        if info['engine'] != dump['engine']: raise ValueError('The backup dump is ' + dump['engine'] + '; this site runs ' + info['engine'] + '.')
    return ledger.submit_site_restore(str(uuid.uuid4()), row['id'], ident, scope)


def restore_site_files(site, archive_path):
    """Bring back .env and the site's nginx rules; both are bind-mounted files replaced by rename."""
    restored = []
    with tarfile.open(archive_path) as archive:
        for name in ('./.env', './conf/site.nginx.conf'):
            try: member = archive.getmember(name)
            except KeyError: continue
            with archive.extractfile(member) as stream: data = stream.read()
            atomic(site / name[2:], data.decode('utf-8', 'replace'), 0o600 if name == './.env' else 0o644)
            restored.append(name[2:])
    return restored


def refresh_web(row, site):
    """Recreate the nginx container so it mounts the replaced rules file (a reload would keep the old inode)."""
    if not (site / 'compose.yml').exists(): return
    command(['docker', 'compose', '-f', str(site / 'compose.yml'), 'up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '60', 'web'], timeout=120)
    if json.loads(row['payload']).get('pids_limit') is None: command(['docker', 'update', '--pids-limit', '-1', 'hosting-site-' + row['name']])


def clear_tree(target):
    for child in list(target.iterdir()):
        if child.is_dir() and not child.is_symlink(): shutil.rmtree(child)
        else: child.unlink()


def safety_backup(ledger, host, job, row):
    """Before anything in a live site changes: a complete backup of it as it is, recorded on the restore."""
    if job.get('safety_backup'): return
    ledger.finish_site_restore(job['id'], 'running', 'safety backup of the current site')
    safety_id = str(uuid.uuid4()); now = time.time()
    with ledger.db() as db:
        db.execute("INSERT INTO site_backups VALUES (?,?,'pre-restore','queued','','','',?,?)", (safety_id, row['id'], now, now))
        db.execute('UPDATE site_restores SET safety_backup=? WHERE id=?', (safety_id, job['id']))
    if not perform(ledger, host, {'id': safety_id, 'site_id': row['id'], 'kind': 'pre-restore'}):
        failed = next(b for b in ledger.site_backups(row['id']) if b['id'] == safety_id)
        raise ValueError('The safety backup failed, so nothing was changed: ' + failed['error'])


def perform_restore(ledger, host, job):
    """Second phase of a managed restore: content, dump, schedules, notes, routing profile."""
    row = ledger.get(job['site_id'])
    scope = job.get('scope', 'full')
    if row['state'] in ('failed', 'recovery-needed') and scope == 'full':
        ledger.finish_site_restore(job['id'], 'failed', job['step'], 'Site creation failed: ' + row['error']); return
    if row['state'] != 'succeeded': return
    ledger.finish_site_restore(job['id'], 'running', job['step'])
    try:
        if scope == 'dump':
            # The database alone, from a scheduled dump artifact (its own manifest and file), after a safety backup.
            from .database_backup import artifact_path as dump_path, restore_managed
            folder = dump_path(job['snapshot']); trusted(folder, directory=True)
            dump = json.loads(ci.regular(folder / 'manifest.json'))
            if dump.get('kind') != 'local-database-dump' or dump.get('operation') != job['snapshot']: raise ValueError('Not a database dump artifact')
            if checksum(folder / dump['file']) != dump['sha256']: raise ValueError('Dump checksum mismatch; restore refused.')
            safety_backup(ledger, host, job, row)
            ledger.finish_site_restore(job['id'], 'running', 'restoring database from the dump')
            restore_managed(host, row, folder / dump['file'], dump, job['id'])
            ledger.finish_site_restore(job['id'], 'running', 'verifying'); host.verify_domains(ledger.domains(row))
            ledger.finish_site_restore(job['id'], 'succeeded', 'restored database from the dump of ' + time.strftime('%Y-%m-%d %H:%M', time.gmtime(dump.get('completed_at') or 0)))
            return
        root, manifest = snapshot(job['snapshot'])
        managed = manifest['managed']; site = SITES / row['name']; trusted(site, directory=True)
        payload = json.loads(row['payload'])
        if scope != 'full':
            safety_backup(ledger, host, job, row)
            if scope == 'database':
                ledger.finish_site_restore(job['id'], 'running', 'restoring database')
                from .database_backup import restore_managed
                dump = manifest['dumps']['database']
                restore_managed(host, row, root / dump['file'], dump, job['id'])
            else:
                ledger.finish_site_restore(job['id'], 'running', 'replacing files')
                for folder in ('html', 'volumes'):
                    if (site / folder).is_dir(): clear_tree(site / folder)
                extract_managed(site, root / manifest['files']['file'], row['uid'], row['uid'], ('html', 'volumes'))
                if 'conf/site.nginx.conf' in restore_site_files(site, root / manifest['files']['file']):
                    ledger.finish_site_restore(job['id'], 'running', 'applying site rules')
                    refresh_web(row, site)
            ledger.finish_site_restore(job['id'], 'running', 'verifying')
            host.verify_domains(ledger.domains(row))
            ledger.finish_site_restore(job['id'], 'succeeded', 'restored ' + scope)
            return
        ledger.finish_site_restore(job['id'], 'running', 'restoring files')
        # The site was just Created, so html/ holds only the placeholder index page; a static site whose
        # content has no index.html (a site that serves index.htm) would otherwise keep serving it.
        for folder in ('html', 'volumes'):
            if (site / folder).is_dir(): clear_tree(site / folder)
        extract_managed(site, root / manifest['files']['file'], row['uid'], row['uid'], ('html', 'volumes'))
        restored_files = restore_site_files(site, root / manifest['files']['file'])
        if manifest['dumps']:
            ledger.finish_site_restore(job['id'], 'running', 'restoring database')
            from .database_backup import restore_managed
            dump = manifest['dumps']['database']
            restore_managed(host, row, root / dump['file'], dump, job['id'])
        if (root / 'config/schedules.json').exists():
            ledger.finish_site_restore(job['id'], 'running', 'restoring schedules')
            from .schedules import save as save_schedule
            for record in json.loads(ci.regular(root / 'config/schedules.json')):
                save_schedule(ledger, row['id'], {'name': record['name'], 'interval': record['interval'], 'enabled': bool(record['enabled']), **record['settings']})
        if (root / 'config/notes.json').exists():
            from .recovery_context import STORE as NOTES
            notes = json.loads(ci.regular(root / 'config/notes.json'))
            notes.update(site_id=row['id'], revision=str(uuid.uuid4()))
            NOTES.mkdir(mode=0o700, exist_ok=True); trusted(NOTES, directory=True)
            atomic(NOTES / (row['id'] + '.json'), json.dumps(notes, sort_keys=True))
        if managed.get('web_settings') and payload.get('runtime') == 'php':
            ledger.finish_site_restore(job['id'], 'running', 'applying routing profile')
            from .requests_site import apply
            apply(host, row, managed['web_settings']['profile'])
        elif 'conf/site.nginx.conf' in restored_files:
            ledger.finish_site_restore(job['id'], 'running', 'applying site rules')
            refresh_web(row, site)
        if managed.get('php_settings') and payload.get('runtime') == 'php':
            from .php_settings import apply as apply_php
            ledger.finish_site_restore(job['id'], 'running', 'applying PHP limits')
            apply_php(host, row, managed['php_settings'])
        if managed.get('mail_senders') and payload.get('runtime') == 'php':
            ledger.finish_site_restore(job['id'], 'running', 'restoring allowed mail senders')
            from .mail import apply_senders
            apply_senders(host, row, {'senders': managed['mail_senders']})
        if (managed.get('sftp_access') or {}).get('secondary'):
            # The keys come back; access itself is turned on when someone needs it, never by a restore.
            from .sftp import save_secondary
            save_secondary(row, managed['sftp_access']['secondary'])
        ledger.finish_site_restore(job['id'], 'running', 'verifying')
        host.verify_domains(ledger.domains(row))
        ledger.finish_site_restore(job['id'], 'succeeded', 'restored')
    except Exception as exc:
        current = next(j for j in ledger.site_restores() if j['id'] == job['id'])
        ledger.finish_site_restore(job['id'], 'recovery-needed', current['step'], str(exc))


def perform_delete(ledger, host, job):
    """Nothing is removed until a successful final backup exists; each step is durable and retryable."""
    ledger.finish_site_delete(job['id'], 'running', job['step'])
    try:
        row = ledger.get(job['site_id'])
        kind = kind_of(row)
        if kind is None: raise ValueError('Delete covers managed sites and Compose packages only.')
        backup = None
        if job['backup_id']:
            backup = next((b for b in ledger.site_backups(row['id']) if b['id'] == job['backup_id']), None)
        if not backup or backup['state'] != 'succeeded':
            ledger.finish_site_delete(job['id'], 'running', 'final backup')
            if not backup or backup['state'] == 'failed':
                backup_id = str(uuid.uuid4()); now = time.time()
                with ledger.db() as db:
                    db.execute("INSERT INTO site_backups VALUES (?,?,'final','queued','','','',?,?)", (backup_id, row['id'], now, now))
                    db.execute('UPDATE site_deletes SET backup_id=? WHERE id=?', (backup_id, job['id']))
                backup = {'id': backup_id, 'site_id': row['id'], 'kind': 'final'}
            manifest = perform(ledger, host, backup)
            if not manifest:
                failed = next(b for b in ledger.site_backups(row['id']) if b['id'] == backup['id'])
                raise ValueError('Final backup failed; the site and its data are retained. ' + failed['error'])
        root = SITES / row['name']
        ledger.finish_site_delete(job['id'], 'running', 'removing routing')
        host.unpublish(row)
        ingress = 'hosting-ingress-' + row['name']
        edge = host.inspect('hosting-edge')
        if edge and ingress in edge['NetworkSettings']['Networks']:
            command(['docker', 'network', 'disconnect', ingress, 'hosting-edge'])
        ledger.finish_site_delete(job['id'], 'running', 'stopping services')
        if kind == 'package':
            plan = ca.read(row)
            args = ['compose', '--project-directory', str(root), '--env-file', '/dev/null', '--project-name', plan['project_name'],
                    '--file', str(ca.plan_path(row['id']) / 'resolved.compose.json'), '--file', str(ca.plan_path(row['id']) / 'compose.hosting.yaml')]
            if (ca.plan_path(row['id']) / 'resolved.compose.json').exists() and root.exists():
                ca.docker([*args, 'down', '--remove-orphans', '--timeout', '30'], raw=True, timeout=120)
            for container in ca.project_containers(plan):
                command(['docker', 'rm', '--force', container['Id']])
            volumes = [v['name'] for v in plan['volumes'].values()]
        else:
            for compose in (root / 'database/compose.yml', root / 'compose.yml'):
                if compose.exists(): command(['docker', 'compose', '-f', compose, 'down', '--remove-orphans', '--timeout', '30'], timeout=120)
            for container in command(['docker', 'ps', '-aq', '--filter', 'label=hosting.operation=' + row['id']]).split():
                command(['docker', 'rm', '--force', container])
            volumes = []
        if kind == 'managed':
            from .sftp import remove as remove_sftp
            remove_sftp(host, row)
            from .mail import detach as detach_mail
            detach_mail(host, row)
        ledger.finish_site_delete(job['id'], 'running', 'removing storage')
        for name in volumes:
            if ca.volume_record(name): command(['docker', 'volume', 'rm', name])
        networks = {ingress, 'hosting-backend-' + row['name'], 'hosting-egress-' + row['name']}
        networks |= set(command(['docker', 'network', 'ls', '--filter', 'label=hosting.operation=' + row['id'], '--format', '{{.Name}}']).split())
        for network in sorted(networks):
            if command(['docker', 'network', 'ls', '--filter', 'name=^' + network + '$', '-q']).strip():
                command(['docker', 'network', 'rm', network])
        if kind == 'managed':
            image = 'hosting-php-site:' + row['id']
            if command(['docker', 'image', 'ls', '-q', image]).strip(): command(['docker', 'image', 'rm', image])
        if root.exists() and not root.is_symlink():
            trusted(root, directory=True); shutil.rmtree(root)
        command(['xfs_quota', '-x', '-c', f"limit -p bsoft=0 bhard=0 {row['project']}", '/srv'])
        ledger.finish_site_delete(job['id'], 'running', 'releasing identity')
        ledger.release_site(row)
        ledger.finish_site_delete(job['id'], 'succeeded', 'deleted')
    except Exception as exc:
        current = next(j for j in ledger.site_deletes() if j['id'] == job['id'])
        ledger.finish_site_delete(job['id'], 'recovery-needed' if current['step'] != 'final backup' else 'failed', current['step'], str(exc))


def listing(ledger, row):
    """Every backup of a site, with what it holds and where it is, for the backups page."""
    items = []
    restores = ledger.site_restores()
    for job in ledger.site_backups(row['id'], limit=None):
        manifest = json.loads(job['manifest']) if job['manifest'] else {}
        root = artifact_path(job['id'])
        available = job['state'] == 'succeeded' and root.is_dir()
        files = manifest.get('files') or {}
        item = {'id': job['id'], 'kind': job['kind'], 'state': job['state'], 'step': job['step'], 'error': job['error'],
                'created': job['created'], 'completed_at': manifest.get('completed_at'), 'path': str(root) if available else None,
                'site_kind': manifest.get('site_kind'), 'source_site': manifest.get('site_name'), 'imported': manifest.get('imported'),
                'coverage': manifest.get('coverage'), 'consistency': manifest.get('consistency'),
                'files': {'bytes': files.get('bytes', 0), 'entries': files.get('entries', 0), 'sha256': files.get('sha256'), 'excluded': files.get('excluded', [])} if files else None,
                'dumps': [{'service': s, 'engine': d['engine'], 'database': d['database'], 'bytes': d['bytes'], 'sha256': d['sha256'], 'file': d['file']} for s, d in (manifest.get('dumps') or {}).items()],
                'volumes': [{'key': k, 'method': v['method'], 'bytes': v.get('bytes', 0)} for k, v in (manifest.get('volumes') or {}).items()],
                'bytes': files.get('bytes', 0) + sum(v.get('bytes', 0) for v in (manifest.get('volumes') or {}).values()) + sum(d['bytes'] for d in (manifest.get('dumps') or {}).values()),
                'available': available,
                'used_by': [{'id': r['id'], 'site_id': r['site_id'], 'scope': r.get('scope', 'full'), 'state': r['state']} for r in restores if r['snapshot'] == job['id']]}
        items.append(item)
    return items


def web_identity():
    import pwd
    account = pwd.getpwnam('hosting-web')
    return account.pw_uid, account.pw_gid


def export(ident):
    """Copy a snapshot's files into a web-readable folder so the operator can download them."""
    root, manifest = snapshot(ident)
    uid, gid = web_identity()
    EXPORTS.mkdir(mode=0o700, exist_ok=True); os.chown(EXPORTS, uid, gid)
    now = time.time()
    for old in EXPORTS.iterdir():
        try:
            request_id(old.name)
            if now - old.lstat().st_mtime > EXPORT_TTL and old.is_dir() and not old.is_symlink(): shutil.rmtree(old)
        except ValueError: continue
    token = str(uuid.uuid4()); folder = EXPORTS / token; folder.mkdir(mode=0o700); os.chown(folder, uid, gid)
    names = [manifest['files']['file'], 'manifest.json'] + [d['file'] for d in manifest['dumps'].values()] + [v['file'] for v in manifest['volumes'].values() if v.get('file')]
    listed = []
    for name in names:
        flat = name.replace('/', '-')
        shutil.copyfile(root / name, folder / flat); os.chmod(folder / flat, 0o600); os.chown(folder / flat, uid, gid)
        listed.append({'name': flat, 'bytes': (folder / flat).stat().st_size})
    with tarfile.open(folder / 'snapshot.tar', 'w') as whole:
        for name in names: whole.add(root / name, arcname=name)
    os.chmod(folder / 'snapshot.tar', 0o600); os.chown(folder / 'snapshot.tar', uid, gid)
    listed.append({'name': 'snapshot.tar', 'bytes': (folder / 'snapshot.tar').stat().st_size})
    return {'token': token, 'files': listed, 'expires_at': now + EXPORT_TTL}


def claim_upload(name, target):
    """Copy a web-owned staged upload through no-follow descriptors; never trust its path afterwards."""
    import pwd
    import stat as statmod
    rootfd = os.open(UPLOADS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=rootfd)
    except FileNotFoundError: raise ValueError('The uploaded file is missing; upload it again.') from None
    finally: os.close(rootfd)
    try:
        info = os.fstat(fd)
        if not statmod.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != pwd.getpwnam('hosting-web').pw_uid:
            raise ValueError('Invalid staged upload.')
        with os.fdopen(os.dup(fd), 'rb') as source, target.open('xb') as output:
            os.fchmod(output.fileno(), 0o600)
            shutil.copyfileobj(source, output, 1048576); output.flush(); os.fsync(output.fileno())
    finally: os.close(fd)


def content_archive(upload, files_tar):
    """Turn an uploaded archive of site content into files.tar under ./html/ using the intake's safe reader."""
    from .application_package import Archive
    archive = Archive(upload); entries = 0
    try:
        with tarfile.open(files_tar, 'w') as out:
            base = tarfile.TarInfo('./html'); base.type = tarfile.DIRTYPE; base.mode = 0o755; out.addfile(base); entries += 1
            for name, entry in archive.entries.items():
                info = tarfile.TarInfo('./html/' + name); info.mode = 0o755 if entry['kind'] == 'directory' else 0o644
                if entry['kind'] == 'directory':
                    info.type = tarfile.DIRTYPE; out.addfile(info)
                elif entry['kind'] == 'link':
                    info.type = tarfile.SYMTYPE; info.linkname = os.path.relpath(entry['target'], os.path.dirname(name) or '.'); out.addfile(info)
                else:
                    member = entry['member']; info.size = entry['size']
                    import zipfile
                    stream = archive.archive.open(member) if isinstance(archive.archive, zipfile.ZipFile) else archive.archive.extractfile(member)
                    with stream: out.addfile(info, stream)
                entries += 1
    finally: archive.close()
    return entries


def import_backup(ledger, host, site_id, token, mode, names):
    """Register an uploaded backup as a snapshot of this site: a whole snapshot.tar, or site content plus a dump."""
    row = site_for(ledger, site_id)
    request_id(token)
    if mode not in ('snapshot', 'content'): raise ValueError('Choose a snapshot archive or site content.')
    staging()
    ident = str(uuid.uuid4()); partial = artifact_path(ident, partial=True); partial.mkdir(mode=0o700)
    try:
        if mode == 'snapshot':
            claim_upload(token + '.files', partial / 'upload.tar')
            with tarfile.open(partial / 'upload.tar') as archive:
                members = archive.getmembers()
                if not any(m.name in ('manifest.json', './manifest.json') for m in members): raise ValueError('The archive is not a panel snapshot (no manifest.json).')
                archive.extractall(partial, filter='data')
            (partial / 'upload.tar').unlink()
            manifest = json.loads((partial / 'manifest.json').read_text())
            if manifest.get('schema') != 1 or manifest.get('kind') != 'site-backup': raise ValueError('The archive is not a panel snapshot.')
            files = partial / manifest['files']['file']
            if checksum(files) != manifest['files']['sha256']: raise ValueError('Snapshot files archive failed its checksum.')
            for dump in manifest['dumps'].values():
                if checksum(partial / dump['file']) != dump['sha256']: raise ValueError('Snapshot dump failed its checksum.')
            for volume in manifest['volumes'].values():
                if volume.get('file') and checksum(partial / volume['file']) != volume['sha256']: raise ValueError('Snapshot volume archive failed its checksum.')
            if manifest.get('site_kind', 'package') != kind_of(row): raise ValueError('This snapshot is a ' + manifest.get('site_kind', 'package') + ' backup; this site is ' + kind_of(row) + '.')
            manifest['imported'] = {'from': 'snapshot archive', 'original_operation': manifest.get('operation'), 'original_site_id': manifest.get('site_id'),
                                    'original_site_name': manifest.get('site_name'), 'uploaded_at': time.time(), 'names': names}
        else:
            if kind_of(row) != 'managed': raise ValueError('Site content import is for managed sites; Compose packages take a snapshot archive.')
            from .database_site import state as db_state
            payload = json.loads(row['payload']); info = db_state(row)
            manifest = {'schema': 1, 'kind': 'site-backup', 'site_kind': 'managed', 'backup_kind': 'imported', 'site_id': row['id'], 'site_name': row['name'],
                        'domains': ledger.domains(row), 'quota_mb': payload['data_mb'], 'volumes': {}, 'dumps': {}, 'started_at': time.time(),
                        'application_consistent': False, 'restore_verified': False, 'coverage': 'files and dump as uploaded',
                        'consistency': 'uploaded by the operator; the panel did not observe how they were taken',
                        'managed': {'runtime': payload.get('runtime', 'static'), 'payload': payload,
                                    'php_branch': ledger.runtime_branch(row) if payload.get('runtime') == 'php' else None,
                                    'web_settings': None, 'database': {k: info[k] for k in ('engine', 'version', 'series', 'image', 'image_id', 'spec') if k in info} if info else None,
                                    'recorded': {'database': False, 'schedules': False, 'notes': False}},
                        'imported': {'from': 'site content upload', 'uploaded_at': time.time(), 'names': names}}
            if names.get('files'):
                claim_upload(token + '.files', partial / 'upload.archive')
                entries = content_archive(partial / 'upload.archive', partial / 'files.tar'); (partial / 'upload.archive').unlink()
            else:
                with tarfile.open(partial / 'files.tar', 'w') as out:
                    base = tarfile.TarInfo('./html'); base.type = tarfile.DIRTYPE; base.mode = 0o755; out.addfile(base)
                entries = 1
            if names.get('dump'):
                if not info or info.get('stage') != 'ready': raise ValueError('This site has no managed database for the uploaded dump.')
                lower = names['dump'].lower()
                if info['engine'] == 'postgres':
                    filename = 'database.dump' if lower.endswith('.dump') else 'database.sql'
                else:
                    if lower.endswith('.dump'): raise ValueError('A custom-format PostgreSQL dump cannot restore into ' + info['engine'] + '.')
                    filename = 'database.sql'
                folder = partial / 'dumps/database'; folder.mkdir(parents=True, mode=0o700)
                claim_upload(token + '.dump', folder / 'upload')
                if lower.endswith('.gz'):
                    import gzip
                    with gzip.open(folder / 'upload', 'rb') as source, (folder / filename).open('wb') as target: shutil.copyfileobj(source, target, 1048576)
                    (folder / 'upload').unlink()
                else: (folder / 'upload').rename(folder / filename)
                os.chmod(folder / filename, 0o600)
                if info['engine'] != 'postgres':
                    from .database_backup import foreign_databases
                    foreign = foreign_databases(folder / filename)
                    if foreign: raise ValueError('The dump selects database ' + ', '.join(foreign) + '; this site restores into its own database. Export the one database without --databases and import again.')
                manifest['dumps']['database'] = {'job_id': None, 'file': 'dumps/database/' + filename, 'bytes': (folder / filename).stat().st_size,
                                                 'sha256': checksum(folder / filename), 'engine': info['engine'], 'database': 'site', 'consistency': 'uploaded'}
            (partial / 'files.tar').chmod(0o600)
            manifest['files'] = {'file': 'files.tar', 'bytes': (partial / 'files.tar').stat().st_size, 'sha256': checksum(partial / 'files.tar'), 'entries': entries, 'source': 'upload', 'excluded': []}
        manifest.update(operation=ident, site_id=row['id'], site_name=row['name'], backup_kind='imported', completed_at=time.time())
        atomic(partial / 'manifest.json', json.dumps(manifest, sort_keys=True))
        os.rename(partial, artifact_path(ident))
    except BaseException:
        if partial.exists(): shutil.rmtree(partial)
        raise
    finally:
        for suffix in ('.files', '.dump'):
            (UPLOADS / (token + suffix)).unlink(missing_ok=True)
    now = time.time()
    with ledger.db() as db:
        db.execute("INSERT INTO site_backups VALUES (?,?,'imported','succeeded','imported','',?,?,?)", (ident, row['id'], json.dumps(manifest, sort_keys=True), now, now))
    return next(j for j in ledger.site_backups(row['id']) if j['id'] == ident)


def deleted_sites(ledger):
    """Deleted rows with their final backups, so a deletion can be undone into a new site."""
    result = []
    with ledger.db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM jobs WHERE state='deleted' ORDER BY updated DESC")]
    for row in rows:
        backups = [j for j in ledger.site_backups(row['id']) if j['state'] == 'succeeded' and artifact_path(j['id']).is_dir()]
        final = next((j for j in backups if j['kind'] == 'final'), None) or (backups[0] if backups else None)
        manifest = json.loads(final['manifest']) if final and final['manifest'] else None
        result.append({'id': row['id'], 'name': row['name'].split('~deleted-')[0], 'domain': row['domain'].split('~deleted-')[0],
                       'deleted_at': row['updated'], 'runtime': json.loads(row['payload']).get('runtime'),
                       'final_backup': {'id': final['id'], 'kind': final['kind'], 'completed_at': manifest.get('completed_at'),
                                        'bytes': manifest['files']['bytes'] + sum(d['bytes'] for d in manifest['dumps'].values()),
                                        'path': str(artifact_path(final['id']))} if final and manifest else None,
                       'backups': len(backups)})
    return result
