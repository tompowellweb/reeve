"""Independent, bounded restic uploader for completed backups, to every configured destination.

A destination is a restic repository: on an SFTP server, in an Amazon S3 bucket, or in a folder this
server can reach (a disk here, a mounted NAS). Each lives in its own root-private folder under
DESTINATIONS with its configuration and credentials; receipts and cycles are keyed by the
destination's identity (repository address and id). Root-owned configuration only. No destination
credentials or backend diagnostics reach RPC. The minute timer checks each destination's durable
hourly due time; the worker never waits on the network.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import selectors
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import database_backup as local
from . import site_backup as sites
from .compose_inspect import regular
from .host import trusted

CONFIG = Path('/srv/ops/panel/worker/remote-backup.json')          # before 1.5.0: the one destination; migrated on first read
DESTINATIONS = Path('/srv/ops/panel/worker/destinations')
CACHE = Path('/srv/ops/panel/worker/restic-cache')
LOCK = Path('/srv/ops/panel/worker/remote-backup.lock')
HEX = re.compile(r'[0-9a-f]{64}')


class RemoteFailed(Exception): pass


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS remote_copies (
        destination TEXT NOT NULL, job_id TEXT NOT NULL, snapshot TEXT NOT NULL DEFAULT '',
        verified REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', pruned REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(destination,job_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS remote_cycles (
        destination TEXT PRIMARY KEY, next_run REAL NOT NULL DEFAULT 0,
        started REAL NOT NULL DEFAULT 0, finished REAL NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '')''')
    if 'forgotten' not in {r[1] for r in db.execute('PRAGMA table_info(remote_copies)')}:
        db.execute('ALTER TABLE remote_copies ADD COLUMN forgotten REAL NOT NULL DEFAULT 0')
    if 'pruned_at' not in {r[1] for r in db.execute('PRAGMA table_info(remote_cycles)')}:
        db.execute('ALTER TABLE remote_cycles ADD COLUMN pruned_at REAL NOT NULL DEFAULT 0')


def private(path):
    path = Path(path)
    trusted(path)
    if not path.is_absolute() or path.stat().st_mode & 0o077:
        raise RemoteFailed('Remote backup configuration and credentials must be private root-owned files.')
    return path


TYPES = ('sftp', 's3', 'local')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _.-]{0,39}')
LOCAL_PATH = re.compile(r'/[A-Za-z0-9_][A-Za-z0-9_./-]*')
LOCAL_ROOTS = ('/srv/backups/repositories', '/mnt', '/media')   # where the worker and the hourly copy may write; the units say the same


def validate_config(value, require_id=True):
    """The one reading of a destination's configuration: what the file may say, checked strictly."""
    allowed = {'id', 'name', 'created', 'type', 'repository', 'repository_id', 'password_file', 'ssh_key_file', 'ssh_password_file', 'ssh_askpass_file',
               'known_hosts_file', 'aws_credentials_file', 'enabled', 'timeout_seconds', 'prune_local_after_days'}
    if not isinstance(value, dict) or value.keys() - allowed: raise ValueError()
    if type(value.get('enabled', True)) is not bool: raise ValueError()
    if value.get('type') not in TYPES: raise ValueError()
    repository = value['repository']
    if not isinstance(repository, str) or len(repository) > 2048 or any(c.isspace() for c in repository): raise ValueError()
    if value['type'] == 'sftp':
        # URL syntax supports an explicit port and an absolute remote path.
        if not re.fullmatch(r'sftp://[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+(?::[0-9]{1,5})?//[A-Za-z0-9_./-]+', repository): raise ValueError()
        private(value['known_hosts_file'])
        if 'ssh_key_file' in value: private(value['ssh_key_file'])
        elif 'ssh_password_file' in value and 'ssh_askpass_file' in value: private(value['ssh_password_file']); private(value['ssh_askpass_file'])
        else: raise ValueError()
    elif value['type'] == 's3':
        # Amazon endpoints only; TLS verification is always enabled.
        if not re.fullmatch(r's3:https://s3[.-][a-z0-9-]+\.amazonaws\.com/[a-z0-9.-]+(?:/[A-Za-z0-9_./-]+)?', repository): raise ValueError()
        credentials = json.loads(regular(private(value['aws_credentials_file'])))
        if not isinstance(credentials, dict) or credentials.keys() - {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_DEFAULT_REGION'}: raise ValueError()
        if not {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'} <= credentials.keys(): raise ValueError()
        if any(not isinstance(v, str) or not v or '\x00' in v for v in credentials.values()): raise ValueError()
    else:
        # A folder this server can reach: a repository on a disk here or on a mounted share, under one of the
        # roots the service units let the worker and the hourly copy write, and never a root itself.
        if not LOCAL_PATH.fullmatch(repository) or '/../' in repository + '/' or not local_allowed(repository): raise ValueError()
    private(value['password_file'])
    if require_id and not HEX.fullmatch(value.get('repository_id', '')): raise ValueError()
    if 'name' in value and (not isinstance(value['name'], str) or not NAME.fullmatch(value['name'])): raise ValueError()
    value.setdefault('timeout_seconds', 600)
    value.setdefault('prune_local_after_days', 0)  # Superseded by the retention policy; accepted and ignored.
    if type(value['timeout_seconds']) is not int or not 10 <= value['timeout_seconds'] <= 900: raise ValueError()
    if type(value['prune_local_after_days']) is not int or value['prune_local_after_days'] not in (0, 7): raise ValueError()
    value['destination'] = hashlib.sha256((repository + '\n' + value.get('repository_id', '')).encode()).hexdigest()
    value.setdefault('name', default_name(value))
    return value


def local_allowed(path):
    return any(path.startswith(root + '/') and len(path) > len(root) + 1 for root in LOCAL_ROOTS)


def default_name(value):
    if value['type'] == 'sftp': return re.sub(r'^sftp://[^@]+@([^:/]+).*$', r'\1', value['repository'])[:40]
    if value['type'] == 's3': return re.sub(r'^s3:https://[^/]+/([^/]+).*$', r'\1', value['repository'])[:40]
    return value['repository'][:40]


def load(path, require_id=True):
    """A destination's configuration file, validated; the folder it lives in is the destination's home."""
    path = Path(path)
    try:
        value = json.loads(regular(private(path)))
        value = validate_config(value, require_id)
    except Exception:
        raise RemoteFailed('Remote backup configuration is invalid; check the private setup file and credential permissions.') from None
    value['path'] = str(path); value['dir'] = str(path.parent)
    value.setdefault('id', path.parent.name)
    return value


def migrate():
    """The single destination of releases before 1.5.0, moved into its own folder under DESTINATIONS. Root only;
    a no-op once done or when there was none."""
    if not CONFIG.exists(): return
    import shutil, uuid
    try: trusted(CONFIG); config = json.loads(CONFIG.read_text())
    except (OSError, ValueError): return
    ident = str(uuid.uuid4()); home = DESTINATIONS / ident
    DESTINATIONS.mkdir(mode=0o700, parents=True, exist_ok=True); home.mkdir(mode=0o700)
    for key in ('password_file', 'known_hosts_file', 'ssh_password_file', 'ssh_askpass_file', 'aws_credentials_file'):
        if key in config and Path(config[key]).exists():
            target = home / Path(config[key]).name
            shutil.move(config[key], target); config[key] = str(target)
    config.update(id=ident, created=time.time())
    tmp = home / '.config.new'; tmp.write_text(json.dumps(config, indent=2)); tmp.chmod(0o600); tmp.replace(home / 'config.json')
    CONFIG.unlink()


def destinations(require_id=True, include_invalid=False):
    """Every configured destination, oldest first. An invalid one is skipped, or, with include_invalid, listed
    with its error so the page can say so."""
    if os.getuid() == 0: migrate()
    found = []
    if not DESTINATIONS.is_dir(): return found
    for home in sorted(DESTINATIONS.iterdir(), key=lambda p: p.stat().st_mtime):
        if home.name.startswith('.') or not home.is_dir() or home.is_symlink() or not (home / 'config.json').is_file(): continue
        try: found.append(load(home / 'config.json', require_id))
        except RemoteFailed as exc:
            if include_invalid: found.append({'id': home.name, 'name': home.name[:8], 'error': str(exc), 'type': None, 'enabled': False, 'invalid': True})
    found.sort(key=lambda d: d.get('created', 0))
    return found


def destination(ident, require_id=True):
    from .core import request_id
    request_id(ident)
    path = DESTINATIONS / ident / 'config.json'
    if not path.is_file(): raise ValueError('Unknown destination')
    return load(path, require_id)


def enabled_destinations(require_id=True):
    return [d for d in destinations(require_id=require_id) if d.get('enabled', True)]


def settings(require_id=True):
    """Before 1.5.0 there was one destination; readers that still think so get the first off-machine one, or the
    first of any kind. None without any; RemoteFailed when the only ones there are invalid."""
    every = destinations(require_id=require_id, include_invalid=True)
    valid = [d for d in every if not d.get('invalid')]
    if not valid:
        if every: raise RemoteFailed(every[0]['error'])
        return None
    return next((d for d in valid if d['type'] != 'local'), valid[0])


def verified_everywhere(ledger, job_ids=None):
    """The artifacts every enabled destination has verified, or None when no destination is enabled: the
    guard local pruning uses, so a backup is never removed here before it has been copied everywhere it goes."""
    active = enabled_destinations()
    if not active: return None
    with ledger.db() as db:
        held = None
        for config in active:
            rows = {r[0] for r in db.execute('SELECT job_id FROM remote_copies WHERE destination=? AND verified>0', (config['destination'],))}
            held = rows if held is None else held & rows
    return held or set()


def command(config):
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
           'RESTIC_REPOSITORY': config['repository'], 'RESTIC_PASSWORD_FILE': config['password_file'],
           'AWS_EC2_METADATA_DISABLED': 'true'}
    # A root-private index cache keeps each upload from re-reading the whole repository over the network.
    CACHE.mkdir(mode=0o700, exist_ok=True)
    args = ['/usr/bin/restic', '--cache-dir', str(CACHE)]
    if config['type'] == 'sftp':
        options = ['-F', '/dev/null', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UserKnownHostsFile=' + config['known_hosts_file'],
                   '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2']
        if config.get('ssh_key_file'):
            options += ['-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-i', config['ssh_key_file']]
        else:
            # A password, read by ssh from a root-private askpass program; never on a command line.
            options += ['-o', 'BatchMode=no', '-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password', '-o', 'NumberOfPasswordPrompts=1']
            env.update({'SSH_ASKPASS': config['ssh_askpass_file'], 'SSH_ASKPASS_REQUIRE': 'force', 'DISPLAY': 'none:0'})
        args += ['-o', 'sftp.args=' + shlex.join(options)]
    elif config['type'] == 's3':
        env.update(json.loads(regular(private(config['aws_credentials_file']))))
    return args, env


def execute(config, args, maximum=4 * 1024**2, digest=False):
    """Bound output, elapsed time and the whole subprocess group; discard private stderr."""
    command_args, env = command(config)
    if digest:
        # Hash the download as a stream: no second SQL file or unbounded memory buffer.
        proc = subprocess.Popen([*command_args, *args], env=env, cwd='/', stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        size, result = 0, hashlib.sha256()
        deadline = time.monotonic() + config['timeout_seconds']
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() >= deadline: raise RemoteFailed('Remote verification timed out; local dump retained.')
                    if not selector.select(0.1): continue
                    block = os.read(proc.stdout.fileno(), 65536)
                    if not block: break
                    size += len(block)
                    if size >= maximum: raise RemoteFailed('Remote dump exceeded its recorded size; local dump retained.')
                    result.update(block)
            if proc.wait(timeout=max(0.01, deadline - time.monotonic())):
                raise RemoteFailed('Remote download failed; local dump retained.')
            return size, result.hexdigest()
        finally:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait(); proc.stdout.close()
    # Bound what we read, not what restic may write: pack files and the index cache are larger
    # than any command output, and a process file-size limit would kill uploads of big dumps.
    proc = subprocess.Popen([*command_args, *args], env=env, cwd='/', stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    chunks, size = [], 0
    deadline = time.monotonic() + config['timeout_seconds']
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                if time.monotonic() >= deadline:
                    raise RemoteFailed('Remote backup timed out. Local dumps retained; retry after checking the destination.')
                if not selector.select(0.1): continue
                block = os.read(proc.stdout.fileno(), 65536)
                if not block: break
                size += len(block)
                if size >= maximum: raise RemoteFailed('Remote command output exceeded its bound; local artifacts retained.')
                chunks.append(block)
        if proc.wait(timeout=max(0.01, deadline - time.monotonic())):
            raise RemoteFailed('Remote transfer or verification failed. Local dumps retained; check destination access, space and repository locks.')
        return b''.join(chunks)
    except subprocess.TimeoutExpired:
        raise RemoteFailed('Remote backup timed out. Local dumps retained; retry after checking the destination.') from None
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL); proc.wait()
        proc.stdout.close()


def snapshot_from_backup(output):
    """restic's JSON summary line carries the new snapshot id; fall back to listing when absent."""
    for line in output.decode('utf-8', 'replace').splitlines():
        try: item = json.loads(line)
        except ValueError: continue
        if isinstance(item, dict) and item.get('message_type') == 'summary' and HEX.fullmatch(str(item.get('snapshot_id', ''))):
            return item['snapshot_id']
    return None


def existing_snapshot(config, tag):
    snapshots = json.loads(execute(config, ['snapshots', '--json', '--tag', tag, '--latest', '1']))
    if len(snapshots) > 1: raise RemoteFailed('Remote snapshot could not be identified; local artifact retained.')
    return snapshots[0]['id'] if snapshots else None


def repository(config):
    info = json.loads(execute(config, ['cat', 'config']))
    if info.get('id') != config['repository_id']:
        raise RemoteFailed('Remote repository identity changed. Copies and local retention stopped for review.')
    # A crash or power cut mid-upload leaves a lock behind; restic removes only locks whose
    # process is gone or that have aged out, so a live uploader's lock is kept.
    execute(config, ['unlock'])


def verify(config, job, manifest, snapshot):
    if not HEX.fullmatch(snapshot): raise RemoteFailed('Invalid remote snapshot identity.')
    root = local.artifact_path(job['id'])
    remote_manifest = json.loads(execute(config, ['dump', snapshot, str(root / 'manifest.json')]))
    if remote_manifest != manifest: raise RemoteFailed('Remote manifest differs from the completed dump; local files retained.')
    size, checksum = execute(config, ['dump', snapshot, str(root / manifest['file'])],
                             maximum=manifest['bytes'] + 1, digest=True)
    if (size, checksum) != (manifest['bytes'], manifest['sha256']):
        raise RemoteFailed('Remote dump checksum differs; local files retained.')


def copy(config, job):
    manifest = local.completed(job)
    if not manifest: raise RemoteFailed('A completed local dump is missing; review previous destinations and retention receipts.')
    if manifest != json.loads(job['artifact']): raise RemoteFailed('Local manifest differs from its durable record; files retained.')
    root = local.artifact_path(job['id'])
    # Select exactly the two immutable files; never include credentials or a partial dump.
    tag = 'hosting-db:' + job['id']
    # A lost receipt after publication is recovered by tag; a fresh copy needs no listing first.
    snapshot = existing_snapshot(config, tag) if job.get('recover') else None
    if not snapshot:
        described = ['--tag', 'site-name:' + str(manifest.get('site_name', '')), '--tag', 'engine:' + str(manifest.get('engine', ''))]
        output = execute(config, ['backup', '--json', '--quiet', '--host', 'reeve', '--tag', tag, *described,
                                  str(root / 'manifest.json'), str(root / manifest['file'])])
        snapshot = snapshot_from_backup(output) or existing_snapshot(config, tag)
    if not snapshot: raise RemoteFailed('Remote snapshot could not be identified; local dump retained.')
    verify(config, job, manifest, snapshot)
    return snapshot


def copy_site(config, job):
    """Upload a complete site backup folder as one tagged snapshot and read its manifest and files back."""
    root, manifest = sites.snapshot(job['id'])
    tag = 'hosting-site:' + job['id']
    snapshot = existing_snapshot(config, tag) if job.get('recover') else None
    if not snapshot:
        # Descriptive tags beside the identity, so a repository can be listed by site without reading manifests.
        described = ['--tag', 'site-name:' + str(manifest.get('site_name', '')), '--tag', 'site-kind:' + str(manifest.get('site_kind', '')),
                     '--tag', 'backup-kind:' + str(manifest.get('backup_kind', '')), '--tag', 'domain:' + str((manifest.get('domains') or [''])[0])]
        output = execute(config, ['backup', '--json', '--quiet', '--host', 'reeve', '--tag', tag, *described, str(root)], maximum=64 * 1024**2)
        snapshot = snapshot_from_backup(output) or existing_snapshot(config, tag)
    if not snapshot: raise RemoteFailed('Remote site snapshot could not be identified; local backup retained.')
    if not HEX.fullmatch(snapshot): raise RemoteFailed('Invalid remote snapshot identity.')
    remote_manifest = json.loads(execute(config, ['dump', snapshot, str(root / 'manifest.json')]))
    if remote_manifest != manifest: raise RemoteFailed('Remote site manifest differs from the local backup; local files retained.')
    size, checksum = execute(config, ['dump', snapshot, str(root / manifest['files']['file'])], maximum=manifest['files']['bytes'] + 1, digest=True)
    if (size, checksum) != (manifest['files']['bytes'], manifest['files']['sha256']):
        raise RemoteFailed('Remote site files archive differs; local files retained.')
    return snapshot


def pending_sites(ledger, destination, site_id=None):
    with ledger.db() as db:
        rows = [dict(r) for r in db.execute('''SELECT s.* FROM site_backups s
            LEFT JOIN remote_copies r ON r.job_id=s.id AND r.destination=?
            WHERE s.state='succeeded' AND COALESCE(r.verified,0)=0'''
            + (' AND s.site_id=?' if site_id else '') + ' ORDER BY s.created',
            (destination, site_id) if site_id else (destination,))]
    return [r for r in rows if sites.artifact_path(r['id']).is_dir()]


def remote_surplus(ledger, config, counts):
    """The verified repository copies of complete backups that the given remote counts would forget."""
    from .retention import keep_site_backups
    destination = config['destination']
    with ledger.db() as db:
        site_rows = [dict(r) for r in db.execute('''SELECT s.id, s.site_id, s.kind, s.kept, s.manifest, r.snapshot FROM site_backups s
            JOIN remote_copies r ON r.job_id=s.id AND r.destination=? WHERE r.verified>0 AND r.forgotten=0''', (destination,))]
    by_site = {}
    for s in site_rows: by_site.setdefault(s['site_id'], []).append(s)
    surplus = []
    for entries in by_site.values():
        described = []
        for s in entries:
            manifest = json.loads(s['manifest']) if s['manifest'] else {}
            described.append({'id': s['id'], 'kind': s['kind'], 'kept': bool(s.get('kept')), 'completed_at': manifest.get('completed_at')})
            s['bytes'] = (manifest.get('files') or {}).get('bytes', 0) + sum(d.get('bytes', 0) for d in (manifest.get('dumps') or {}).values())
        keep = keep_site_backups(described, time.time(), counts)
        surplus += [s for s in entries if s['id'] not in keep]
    return surplus


def remote_retention(ledger, config, now=None):
    """Forget remote snapshots outside the remote policy, then prune the repository at most daily."""
    from .retention import policy as retention_policy, keep_dumps
    rule = retention_policy(); now = time.time() if now is None else now; destination = config['destination']
    with ledger.db() as db:
        dumps = [dict(r) for r in db.execute('''SELECT b.id, b.site_id, b.created, r.snapshot FROM backup_jobs b
            JOIN remote_copies r ON r.job_id=b.id AND r.destination=? WHERE r.verified>0 AND r.forgotten=0''', (destination,))]
        cycle_row = db.execute('SELECT pruned_at FROM remote_cycles WHERE destination=?', (destination,)).fetchone()
    forget = [d for d in dumps if d['id'] not in keep_dumps(dumps, now, rule['database_days'])]
    forget += remote_surplus(ledger, config, rule['remote'])
    if forget:
        execute(config, ['forget', *[f['snapshot'] for f in forget]])
        with ledger.db() as db:
            for item in forget:
                db.execute('UPDATE remote_copies SET forgotten=? WHERE destination=? AND job_id=?', (now, destination, item['id']))
    if forget and (not cycle_row or now - cycle_row['pruned_at'] > 86400):
        execute(config, ['prune'], maximum=64 * 1024**2)
        with ledger.db() as db: db.execute('UPDATE remote_cycles SET pruned_at=? WHERE destination=?', (now, destination))
    return [f['id'] for f in forget]


def pending(ledger, destination, site_id=None):
    with ledger.db() as db:
        return [dict(r) for r in db.execute('''SELECT b.* FROM backup_jobs b
            LEFT JOIN remote_copies r ON r.job_id=b.id AND r.destination=?
            WHERE b.state='succeeded' AND b.cleanup_error='' AND COALESCE(r.verified,0)=0'''
            + (' AND b.site_id=?' if site_id else '') + ' ORDER BY b.created',
            (destination, site_id) if site_id else (destination,))]


def destination_status(ledger, config, site_id):
    destination = config['destination']
    with ledger.db() as db:
        cycle = db.execute('SELECT * FROM remote_cycles WHERE destination=?', (destination,)).fetchone()
        receipt = db.execute('''SELECT r.job_id,r.snapshot,r.verified,b.created FROM remote_copies r
            JOIN backup_jobs b ON b.id=r.job_id WHERE r.destination=? AND (? IS NULL OR b.site_id=?) AND r.verified>0
            ORDER BY b.created DESC LIMIT 1''', (destination, site_id, site_id)).fetchone()
        site_receipt = db.execute('''SELECT r.job_id,r.snapshot,r.verified,s.created FROM remote_copies r
            JOIN site_backups s ON s.id=r.job_id WHERE r.destination=? AND (? IS NULL OR s.site_id=?) AND r.verified>0
            ORDER BY s.created DESC LIMIT 1''', (destination, site_id, site_id)).fetchone()
    return {'id': config['id'], 'name': config['name'], 'type': config['type'], 'repository': config['repository'], 'repository_id': config.get('repository_id', ''),
            'enabled': config.get('enabled', True), 'state': 'configured' if config.get('enabled', True) else 'paused',
            'pending': len(pending(ledger, destination, site_id)), 'pending_sites': len(pending_sites(ledger, destination, site_id)),
            'cycle': dict(cycle) if cycle else None, 'last_copy': dict(receipt) if receipt else None, 'last_site_copy': dict(site_receipt) if site_receipt else None}


def status(ledger, site_id):
    """Every destination's state, and the summary older pages read: the state of the whole, the newest copies
    anywhere, the copies still waiting for any destination."""
    from .retention import policy as retention_policy, describe
    rule = retention_policy()
    every = destinations(include_invalid=True)
    items = []; errors = []
    for config in every:
        if config.get('invalid'): errors.append(config); items.append({**config, 'state': 'configuration error', 'pending': 0, 'pending_sites': 0, 'cycle': None, 'last_copy': None, 'last_site_copy': None}); continue
        items.append(destination_status(ledger, config, site_id))
    valid = [i for i in items if not i.get('invalid')]
    newest = lambda key: max((i[key] for i in valid if i.get(key)), key=lambda r: r['created'], default=None)
    state = ('not configured' if not every else 'configuration error' if errors and not valid else
             'configured' if any(i['enabled'] for i in valid) else 'paused')
    first = next((i for i in valid if i['type'] != 'local'), valid[0] if valid else None)
    return {'state': state, 'type': first['type'] if first else None, 'destinations': items,
            'error': errors[0]['error'] if errors else '',
            'pending': sum(i['pending'] for i in valid), 'pending_sites': sum(i['pending_sites'] for i in valid),
            'cycle': first['cycle'] if first else None, 'last_copy': newest('last_copy'), 'last_site_copy': newest('last_site_copy'),
            'retention': rule, 'retention_text': describe(rule), 'retention_days': rule['database_days']}


def request(ledger, ident=None):
    """Run the next cycle now, for one destination or every enabled one."""
    chosen = [destination(ident)] if ident else enabled_destinations()
    chosen = [c for c in chosen if c.get('enabled', True)]
    if not chosen: raise ValueError('Connect and enable a destination first.')
    with ledger.db() as db:
        for config in chosen:
            db.execute('INSERT OR IGNORE INTO remote_cycles(destination) VALUES (?)', (config['destination'],))
            db.execute('UPDATE remote_cycles SET next_run=0 WHERE destination=?', (config['destination'],))


def prune(ledger, config):
    """Superseded by database_backup.retention_tick; kept for the old opt-in only."""
    if not config['prune_local_after_days']: return
    cutoff = time.time() - config['prune_local_after_days'] * 86400
    with ledger.db() as db:
        jobs = [dict(r) for r in db.execute('''SELECT b.*,r.snapshot,r.pruned FROM backup_jobs b
            JOIN remote_copies r ON r.job_id=b.id AND r.destination=?
            WHERE r.verified>0 AND b.created<? AND b.cleanup_error=''
            AND EXISTS (SELECT 1 FROM backup_jobs newer WHERE newer.site_id=b.site_id
                AND newer.state='succeeded' AND newer.created>b.created)''', (config['destination'], cutoff))]
    for job in jobs:
        root = local.artifact_path(job['id'])
        if job['pruned'] and not root.exists(): continue
        manifest = json.loads(job['artifact'])
        verify(config, job, manifest, job['snapshot'])  # Re-read before releasing local protection.
        if root.exists():
            trusted(root, directory=True)
            if set(p.name for p in root.iterdir()) - {'manifest.json', manifest['file']}:
                raise RemoteFailed('Unexpected files in an old dump; retention stopped for review.')
        # Durable intent before unlink; repeat safely after interruption between either file.
        with ledger.db() as db:
            db.execute('UPDATE remote_copies SET pruned=? WHERE destination=? AND job_id=?',
                       (time.time(), config['destination'], job['id']))
        if root.exists():
            for name in (manifest['file'], 'manifest.json'): (root / name).unlink(missing_ok=True)
            root.rmdir()
            fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)


RECORD_STATE = CONFIG.with_name('server-record-upload.json')


def copy_record(ledger, config):
    """The server record as its own snapshot, when its content changed since the last copy or a day passed.
    Small and independent of the site copies; a failure here is not a cycle failure."""
    from . import server_record
    try:
        server_record.write(ledger)
        record = server_record.current()
        if not record: return
        identity = server_record.digest(record)
        try: state = json.loads(RECORD_STATE.read_text())
        except (OSError, ValueError): state = {}
        if 'destination' in state: state = {state['destination']: state}   # the single-destination shape of releases before 1.5.0
        mine = state.get(config['destination']) or {}
        fresh = mine.get('digest') == identity and time.time() - mine.get('uploaded_at', 0) < 86400
        if fresh: return
        output = execute(config, ['backup', '--json', '--quiet', '--host', 'reeve', '--tag', server_record.TAG, str(server_record.RECORD)])
        snapshot = snapshot_from_backup(output) or existing_snapshot(config, server_record.TAG)
        if not snapshot: return
        # Keep only the newest record snapshot: the old ones say nothing the new one does not.
        execute(config, ['forget', '--quiet', '--tag', server_record.TAG, '--keep-last', '3'])
        state[config['destination']] = {'digest': identity, 'uploaded_at': time.time(), 'snapshot': snapshot}
        tmp = RECORD_STATE.with_name('.server-record-upload.new')
        tmp.write_text(json.dumps(state)); tmp.chmod(0o600); tmp.replace(RECORD_STATE)
    except Exception:
        return


def cycle(ledger, config, force=False):
    if not config or not config.get('enabled', True): return
    destination = config['destination']; now = time.time()
    with ledger.db() as db:
        db.execute('INSERT OR IGNORE INTO remote_cycles(destination) VALUES (?)', (destination,))
        old = db.execute('SELECT * FROM remote_cycles WHERE destination=?', (destination,)).fetchone()
        if not force and old['state'] != 'running' and old['next_run'] > now: return
        db.execute("UPDATE remote_cycles SET state='running',started=?,next_run=?,error='' WHERE destination=?", (now, now + 3600, destination))
    error = ''
    try:
        repository(config)
        # Complete site backups first: they are the recovery unit. Dumps follow from the full ledger,
        # independent of the 50-row UI history.
        for job in pending_sites(ledger, destination):
            if time.time() - now > 1500: break
            with ledger.db() as db:
                job['recover'] = db.execute('SELECT 1 FROM remote_copies WHERE destination=? AND job_id=?', (destination, job['id'])).fetchone() is not None
                db.execute('INSERT OR IGNORE INTO remote_copies(destination,job_id) VALUES (?,?)', (destination, job['id']))
            try:
                snapshot = copy_site(config, job)
            except Exception:
                with ledger.db() as db:
                    db.execute("UPDATE remote_copies SET error='Transfer or verification failed; local backup retained' WHERE destination=? AND job_id=?", (destination, job['id']))
                raise
            with ledger.db() as db:
                db.execute("UPDATE remote_copies SET snapshot=?,verified=?,error='' WHERE destination=? AND job_id=?", (snapshot, time.time(), destination, job['id']))
        for job in pending(ledger, destination):
            if time.time() - now > 1500: break  # Continue remaining backlog next cycle.
            with ledger.db() as db:
                job['recover'] = db.execute('SELECT 1 FROM remote_copies WHERE destination=? AND job_id=?', (destination, job['id'])).fetchone() is not None
                db.execute('INSERT OR IGNORE INTO remote_copies(destination,job_id) VALUES (?,?)', (destination, job['id']))
            try:
                snapshot = copy(config, job)
            except Exception:
                with ledger.db() as db:
                    db.execute("UPDATE remote_copies SET error='Transfer or verification failed; local dump retained' WHERE destination=? AND job_id=?", (destination, job['id']))
                raise
            with ledger.db() as db:
                db.execute("UPDATE remote_copies SET snapshot=?,verified=?,error='' WHERE destination=? AND job_id=?", (snapshot, time.time(), destination, job['id']))
        copy_record(ledger, config)
        if not pending(ledger, destination) and not pending_sites(ledger, destination): remote_retention(ledger, config)
    except RemoteFailed as exc: error = str(exc)
    except Exception: error = 'Remote copy failed; check the private configuration, repository and local artifacts. Local protection retained.'
    with ledger.db() as db:
        db.execute('UPDATE remote_cycles SET state=?,finished=?,error=? WHERE destination=?',
                   ('failed' if error else ('backlog' if pending(ledger, destination) or pending_sites(ledger, destination) else 'succeeded'), time.time(), error, destination))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true', help='Run the pending queue now, for every enabled destination')
    args = parser.parse_args()
    if os.getuid() != 0: raise SystemExit('Run as root')
    os.umask(0o077)
    try:
        with LOCK.open('a') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return
            from .core import Ledger
            ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3')
            for config in enabled_destinations():
                cycle(ledger, config, force=args.force)
    except Exception:
        raise SystemExit('Remote backup setup failed; check private configuration, credentials and repository access.') from None


if __name__ == '__main__': main()
