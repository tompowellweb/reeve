"""Restoration as a server feature: find every site ever backed up to the connected repository, a folder of
backups or this server's own staging, let the operator choose sites and points in time, and bring them back
as new sites or into live ones, files and database together or apart, one recovery after another.

A scan is asked for and runs in the worker (reading a repository takes time); its result is a file the page
reads. A recovery is a row in `recoveries`, worked as a small state machine by the worker loop: fetch the
artifact if it is not local, hand it to the existing restore code, wait for that, then apply the hostnames.
Nothing here restores by itself; every step is the ordinary site restore, which takes its own safety backup
before touching a live site.
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .host import OPS, BACKUPS, trusted
from . import site_backup as sites
from . import database_backup as dumps

SCAN = OPS / 'panel/worker/recovery-scan.json'
REQUEST = OPS / 'panel/worker/recovery-scan-request.json'
MANIFESTS = OPS / 'panel/worker/recovery-manifests'
FETCH = BACKUPS / 'staging/restore-fetch'
MODES = ('new', 'files', 'database', 'both', 'dump')
STATES = ('queued', 'fetching', 'restoring', 'waiting', 'hostnames', 'succeeded', 'failed', 'recovery-needed')
ACTIONS = ('download', 'delete')
ACTION_STATES = ('queued', 'running', 'succeeded', 'failed')


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS recoveries (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, backup_id TEXT NOT NULL, snapshot TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', domains TEXT NOT NULL DEFAULT '[]',
        site_name TEXT NOT NULL DEFAULT '', child TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL, step TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS backup_actions (
        id TEXT PRIMARY KEY, action TEXT NOT NULL, kind TEXT NOT NULL, backup_id TEXT NOT NULL, source TEXT NOT NULL,
        snapshot TEXT NOT NULL DEFAULT '', site_name TEXT NOT NULL DEFAULT '', bytes INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL, step TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, updated REAL NOT NULL)""")


# ---- discovery

def request_scan(source, folder=''):
    """Ask the worker for a scan. `source` is repository, folder or local."""
    if source not in ('repository', 'folder', 'local'): raise ValueError('Scan the repository, a folder or this server')
    if source == 'folder':
        if not re.fullmatch(r'/[A-Za-z0-9_][A-Za-z0-9_./-]*', folder or '') or '/../' in folder + '/': raise ValueError('Give an absolute folder path')
    REQUEST.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = REQUEST.with_name('.scan-request.new')
    tmp.write_text(json.dumps({'source': source, 'folder': folder, 'requested_at': time.time()})); tmp.chmod(0o600); tmp.replace(REQUEST)
    write_scan({'state': 'requested', 'source': source, 'folder': folder, 'requested_at': time.time()})
    return read_scan()


def write_scan(result):
    SCAN.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = SCAN.with_name('.recovery-scan.new'); tmp.write_text(json.dumps(result, sort_keys=True)); tmp.chmod(0o600); tmp.replace(SCAN)


def read_scan():
    if not SCAN.exists(): return None
    try: return json.loads(SCAN.read_text())
    except (OSError, ValueError): return None


def manifest_cache(ident):
    return MANIFESTS / (ident + '.json')


def cached_manifest(ident):
    path = manifest_cache(ident)
    if not path.exists(): return None
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return None


def remember_manifest(ident, manifest):
    MANIFESTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = MANIFESTS / ('.' + ident + '.new'); tmp.write_text(json.dumps(manifest)); tmp.chmod(0o600); tmp.replace(manifest_cache(ident))


def entry_from_manifest(manifest, source, snapshot=''):
    """Pure: what the page shows for one backup, from its manifest."""
    if manifest.get('kind') == 'site-backup':
        return {'kind': 'site', 'id': manifest['operation'], 'source': source, 'snapshot': snapshot, 'site_id': manifest.get('site_id'),
                'site_name': manifest.get('site_name'), 'site_kind': manifest.get('site_kind'), 'runtime': (manifest.get('managed') or {}).get('runtime') or 'compose',
                'domains': manifest.get('domains') or [], 'backup_kind': manifest.get('backup_kind'), 'completed_at': manifest.get('completed_at'),
                'coverage': manifest.get('coverage'), 'bytes': (manifest.get('files') or {}).get('bytes', 0) + sum(d.get('bytes', 0) for d in (manifest.get('dumps') or {}).values()),
                'has_dump': bool((manifest.get('dumps') or {}).get('database')), 'engine': ((manifest.get('dumps') or {}).get('database') or {}).get('engine')}
    if manifest.get('kind') == 'local-database-dump':
        return {'kind': 'dump', 'id': manifest['operation'], 'source': source, 'snapshot': snapshot, 'site_id': manifest.get('site_id'),
                'site_name': manifest.get('site_name'), 'engine': manifest.get('engine'), 'completed_at': manifest.get('completed_at'),
                'bytes': manifest.get('bytes', 0), 'consistency': manifest.get('consistency')}
    return None


def entry_from_tags(snapshot):
    """Pure: a site backup or a dump described by its snapshot's tags alone (the manifest is read only when needed)."""
    tags = dict(t.split(':', 1) for t in snapshot.get('tags', []) if ':' in t)
    if 'site-name' not in tags: return None
    if 'hosting-site' in tags:
        return {'kind': 'site', 'id': tags['hosting-site'], 'source': 'repository', 'snapshot': snapshot['id'], 'site_id': None,
                'site_name': tags['site-name'], 'site_kind': tags.get('site-kind'), 'runtime': None, 'domains': [tags['domain']] if tags.get('domain') else [],
                'backup_kind': tags.get('backup-kind'), 'completed_at': snapshot_time(snapshot.get('time')), 'coverage': None, 'bytes': None,
                'has_dump': None, 'engine': None, 'from_tags': True}
    if 'hosting-db' in tags:
        return {'kind': 'dump', 'id': tags['hosting-db'], 'source': 'repository', 'snapshot': snapshot['id'], 'site_id': None,
                'site_name': tags['site-name'], 'engine': tags.get('engine'), 'completed_at': snapshot_time(snapshot.get('time')),
                'bytes': None, 'consistency': None, 'from_tags': True}
    return None


def snapshot_time(text):
    """Pure: restic's RFC 3339 time to seconds."""
    if not text: return None
    from datetime import datetime
    try: return datetime.fromisoformat(re.sub(r'\.\d+', '', text).replace('Z', '+00:00')).timestamp()
    except ValueError: return None


def scan_local():
    """Every artifact in this server's staging: complete site backups (live and deleted sites) and dumps."""
    found = []
    for folder in (sites.STAGING, dumps.STAGING):
        if not folder.is_dir(): continue
        for child in folder.iterdir():
            if child.name.startswith('.') or not child.is_dir() or child.is_symlink(): continue
            try: manifest = json.loads((child / 'manifest.json').read_text())
            except (OSError, ValueError): continue
            entry = entry_from_manifest(manifest, 'local')
            if entry and entry['id'] == child.name: found.append(entry)
    return found, None


def scan_folder(folder):
    """A folder of backups: artifacts as subfolders (each with a manifest), or the folder itself as one artifact,
    plus a server record if one lies there."""
    root = Path(folder)
    if not root.is_dir(): raise ValueError('That folder does not exist on this server')
    found = []; record = None
    candidates = [root, *[c for c in root.iterdir() if c.is_dir() and not c.is_symlink()]]
    for child in candidates:
        manifest_path = child / 'manifest.json'
        if not manifest_path.is_file(): continue
        try: manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError): continue
        entry = entry_from_manifest(manifest, 'folder:' + str(child))
        if entry: found.append(entry)
    for name in ('server-record.json',):
        path = root / name
        if path.is_file():
            from .server_record import parse
            try: record = parse(path.read_text())
            except (OSError, ValueError): record = None
    return found, record


def scan_repository(config, budget=600, progress=None):
    """Every site backup and dump in the repository, by tags where the copy carried them and by the manifest
    otherwise (read once, then cached; newest first, so a slow repository loses old entries to the budget,
    not recent ones), plus the newest server record. `progress(done, total)` is told every few reads."""
    from . import remote_backup as remote
    from .server_record import parse, RECORD, TAG
    started = time.monotonic()
    listing = json.loads(remote.execute(config, ['snapshots', '--json'], maximum=32 * 1024 ** 2))
    listing.sort(key=lambda s: s.get('time') or '', reverse=True)
    found = []; record = None; unread = 0; done = 0
    for snapshot in listing:
        tags = snapshot.get('tags', [])
        identity = next((t.split(':', 1)[1] for t in tags if t.startswith(('hosting-site:', 'hosting-db:'))), None)
        if TAG in tags:
            continue
        if not identity or not re.fullmatch(r'[0-9a-f-]{36}', identity): continue
        done += 1
        if progress and done % 5 == 0: progress(done, len(listing))
        manifest = cached_manifest(identity)
        if manifest is None and entry_from_tags(snapshot):
            found.append(entry_from_tags(snapshot)); continue   # described by its tags: no read needed
        if manifest is None:
            paths = snapshot.get('paths') or []
            manifest_path = next((p for p in paths if p.endswith('manifest.json')), None) or (paths[0].rstrip('/') + '/manifest.json' if paths else None)
            if time.monotonic() - started > budget or not manifest_path:
                entry = entry_from_tags(snapshot)
                if entry: found.append(entry)
                else: unread += 1
                continue
            try:
                manifest = json.loads(remote.execute(config, ['dump', snapshot['id'], manifest_path], maximum=4 * 1024 ** 2))
                remember_manifest(identity, manifest)
            except Exception:
                entry = entry_from_tags(snapshot)
                if entry: found.append(entry)
                else: unread += 1
                continue
        entry = entry_from_manifest(manifest, 'repository', snapshot['id'])
        if entry: found.append(entry)
    newest = json.loads(remote.execute(config, ['snapshots', '--json', '--tag', TAG, '--latest', '1']))
    if newest:
        try: record = parse(remote.execute(config, ['dump', newest[0]['id'], str(RECORD)], maximum=4 * 1024 ** 2))
        except Exception: record = None
    return found, record, unread


def group(entries, ledger):
    """Pure given the entries: sites as the operator knows them, each with its backups newest first, its dumps,
    and the live site on this server with the same name, if any."""
    live = {row['name']: row for row in ledger.list()}
    copied = offsite_copies(ledger)
    with ledger.db() as db: kept = {r[0] for r in db.execute('SELECT id FROM site_backups WHERE kept=1')}
    by_name = {}
    for entry in entries:
        if entry['source'] == 'local' and copied is not None: entry['offsite'] = copied.get(entry['id'], 'waiting')
        if entry['id'] in kept: entry['kept'] = True
        site = by_name.setdefault(entry['site_name'] or '(unnamed)', {'name': entry['site_name'] or '(unnamed)', 'site_ids': [], 'site_kind': None, 'runtime': None,
                                                                          'domains': [], 'backups': [], 'dumps': [], 'live': None})
        if entry.get('site_id') and entry['site_id'] not in site['site_ids']: site['site_ids'].append(entry['site_id'])
        if entry['kind'] == 'site':
            site['backups'].append(entry)
            if entry.get('site_kind'): site['site_kind'] = entry['site_kind']
            if entry.get('runtime'): site['runtime'] = entry['runtime']
        else: site['dumps'].append(entry)
    for site in by_name.values():
        site['backups'].sort(key=lambda e: e.get('completed_at') or 0, reverse=True)
        site['dumps'].sort(key=lambda e: e.get('completed_at') or 0, reverse=True)
        seen = set()
        for backup in site['backups']:
            for domain in backup.get('domains') or []:
                if domain not in seen: seen.add(domain); site['domains'].append(domain)
        site['local_bytes'] = sum(e.get('bytes') or 0 for e in site['backups'] + site['dumps'] if e['source'] == 'local')
        row = live.get(site['name'])
        if row:
            payload = json.loads(row['payload'])
            site['live'] = {'id': row['id'], 'name': row['name'], 'state': row['state'], 'runtime': payload.get('runtime'),
                            'managed': sites.kind_of(row) == 'managed', 'domains': ledger.domains(row)}
    return sorted(by_name.values(), key=lambda s: s['name'])


def offsite_copies(ledger):
    """Which local artifacts the connected destination holds: job id to 'verified' or 'forgotten'; None without a destination."""
    from . import remote_backup as remote
    config = remote.settings(require_id=False)
    if not config: return None
    with ledger.db() as db:
        rows = db.execute('SELECT job_id, verified, forgotten FROM remote_copies WHERE destination=?', (config['destination'],)).fetchall()
    return {r['job_id']: ('forgotten' if r['forgotten'] else 'verified' if r['verified'] else 'waiting') for r in rows}


def perform_scan(ledger):
    """The worker's side of a requested scan."""
    if not REQUEST.exists(): return False
    try: request = json.loads(REQUEST.read_text())
    except (OSError, ValueError): REQUEST.unlink(missing_ok=True); return False
    REQUEST.unlink(missing_ok=True)
    result = {'state': 'running', 'source': request['source'], 'folder': request.get('folder', ''), 'started_at': time.time(), 'progress': ''}
    write_scan(result)
    def progress(done, total):
        write_scan({**result, 'progress': f'read {done} of {total} snapshots'})
    try:
        entries, record, unread = [], None, 0
        if request['source'] == 'repository':
            from . import remote_backup as remote
            config = remote.settings()
            if not config: raise ValueError('No backup destination is connected')
            entries, record, unread = scan_repository(config, progress=progress)
        elif request['source'] == 'folder':
            entries, record = scan_folder(request['folder'])
        local, _ = scan_local()
        known = {(e['kind'], e['id']) for e in entries}
        entries.extend(e for e in local if (e['kind'], e['id']) not in known)
        result.update(state='succeeded', finished_at=time.time(), sites=group(entries, ledger), record=record, unread=unread, error='')
    except Exception as exc:
        result.update(state='failed', finished_at=time.time(), error=str(exc)[:600], sites=[], record=None)
    write_scan(result)
    return True


# ---- the recovery queue

def recoveries(ledger, active=False):
    with ledger.db() as db:
        rows = db.execute("SELECT * FROM recoveries WHERE state NOT IN ('succeeded','failed') ORDER BY created" if active else 'SELECT * FROM recoveries ORDER BY created DESC LIMIT 200').fetchall()
    return [dict(r) for r in rows]


def find_entry(scan, kind, ident):
    for site in (scan or {}).get('sites') or []:
        for entry in site['backups'] if kind == 'site' else site['dumps']:
            if entry['id'] == ident: return entry
    return None


def submit(ledger, items):
    """Queue recoveries from the page's picks. Each item: {backup, mode, name?, domains?, target?, dump?}. A live
    target is checked here so a wrong pick fails before anything is queued; the artifact is checked when fetched."""
    from .core import validate_create, validate_domains
    scan = read_scan()
    if not scan or scan.get('state') != 'succeeded': raise ValueError('Scan first')
    if not isinstance(items, list) or not items: raise ValueError('Choose at least one site')
    live = {row['name']: row for row in ledger.list()}
    names_taken = set(live) | {r['name'] for r in ledger.list()}
    queued = []
    for item in items:
        mode = item.get('mode')
        # The page names its source as "site:<id>" or "dump:<id>"; a dump can only go into a live site's database.
        backup = str(item.get('backup', ''))
        kind, backup = backup.split(':', 1) if backup.startswith(('site:', 'dump:')) else (('dump' if mode == 'dump' else 'site'), backup)
        if kind == 'dump': mode = 'dump'
        if mode not in MODES: raise ValueError('Unknown recovery mode')
        if kind == 'dump' and not item.get('target'): raise ValueError('A database dump can only be restored into a live site')
        entry = find_entry(scan, kind, backup)
        if not entry: raise ValueError('That backup is not in the last scan')
        row = {'id': str(uuid.uuid4()), 'source': entry['source'], 'backup_id': entry['id'], 'snapshot': entry.get('snapshot') or '', 'mode': mode,
               'target': '', 'name': '', 'domains': '[]', 'site_name': entry.get('site_name') or '', 'phase': '', 'state': 'queued', 'step': 'queued', 'error': ''}
        if mode == 'new':
            name = str(item.get('name', '')).strip(); domains = [d.strip().lower() for d in str(item.get('domains', '')).replace(',', ' ').split() if d.strip()]
            if not domains: raise ValueError('Give at least one hostname for ' + (name or entry.get('site_name') or 'the site'))
            validate_create({'name': name, 'domain': domains[0], 'aliases': domains[1:]})
            if name in names_taken: raise ValueError('A site named ' + name + ' already exists on this server')
            names_taken.add(name)
            row.update(name=name, domains=json.dumps(domains))
        else:
            target = live.get(str(item.get('target', '')))
            if not target: raise ValueError('Choose a live site to restore into')
            if sites.kind_of(target) != 'managed': raise ValueError('In-place restore is for managed sites; restore a Compose package as a new site')
            if target['state'] != 'succeeded': raise ValueError('The site ' + target['name'] + ' is not ready')
            if mode in ('database', 'both') and entry.get('has_dump') is False: raise ValueError('That backup holds no database dump')
            row.update(target=target['id'])
        queued.append(row)
    now = time.time()
    with ledger.db() as db:
        for row in queued:
            db.execute('INSERT INTO recoveries (id, source, backup_id, snapshot, mode, target, name, domains, site_name, child, phase, state, step, error, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (row['id'], row['source'], row['backup_id'], row['snapshot'], row['mode'], row['target'], row['name'], row['domains'], row['site_name'], '', '', 'queued', 'queued', '', now, now))
    return [r['id'] for r in queued]


def update(ledger, ident, state, step, error='', child=None, phase=None):
    if state not in STATES: raise ValueError('Unknown recovery state')
    with ledger.db() as db:
        db.execute('UPDATE recoveries SET state=?, step=?, error=?, updated=?, child=COALESCE(?, child), phase=COALESCE(?, phase) WHERE id=?',
                   (state, step, error[:2000], time.time(), child, phase, ident))


def retry(ledger, ident):
    with ledger.db() as db:
        row = db.execute('SELECT * FROM recoveries WHERE id=?', (ident,)).fetchone()
        if not row: raise ValueError('Unknown recovery')
        db.execute("UPDATE recoveries SET state='queued', step='queued', error='', updated=? WHERE id=? AND state IN ('failed','recovery-needed')", (time.time(), ident))
    return dict(row)


def recover(ledger):
    """Startup: a recovery interrupted while fetching or handing over needs a look; one that was only waiting resumes."""
    with ledger.db() as db:
        db.execute("UPDATE recoveries SET state='recovery-needed', error='The worker stopped during this step', updated=? WHERE state IN ('fetching','restoring')", (time.time(),))
        db.execute("UPDATE backup_actions SET state='failed', error='The worker stopped during this action; queue it again', updated=? WHERE state='running'", (time.time(),))


def fetch(row):
    """Make the artifact local under its own id: from the repository, a folder or already here. Verified by the
    restore code's own checksum reading afterwards."""
    kind = 'dump' if row['mode'] == 'dump' else 'site'
    module = dumps if kind == 'dump' else sites
    target = module.artifact_path(row['backup_id'])
    if target.is_dir(): return 'already here'
    module.STAGING.mkdir(mode=0o700, parents=True, exist_ok=True)
    if row['source'].startswith('folder:'):
        origin = Path(row['source'][len('folder:'):])
        if not (origin / 'manifest.json').is_file(): raise ValueError('The folder no longer holds this backup')
        partial = module.artifact_path(row['backup_id'], partial=True)
        shutil.rmtree(partial, ignore_errors=True)
        shutil.copytree(origin, partial, symlinks=False)
        for path in [partial, *partial.rglob('*')]:
            shutil.chown(path, 'root', 'root'); path.chmod(0o700 if path.is_dir() else 0o600)
        partial.rename(target)
        return 'copied from the folder'
    if row['source'] == 'repository':
        from . import remote_backup as remote
        config = remote.settings()
        if not config: raise ValueError('No backup destination is connected')
        if not row['snapshot']: raise ValueError('The scan did not record a snapshot for this backup')
        shutil.rmtree(FETCH, ignore_errors=True); FETCH.mkdir(mode=0o700, parents=True)
        args, env = remote.command(config)
        subprocess.run([*args, 'restore', row['snapshot'], '--target', str(FETCH), '--verify'], env=env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
        fetched = FETCH / str(target).lstrip('/')
        if not fetched.is_dir():
            # The artifact may have lived under another backup root on the old server: find it by its manifest.
            fetched = next((p.parent for p in FETCH.rglob('manifest.json') if p.parent.name == row['backup_id']), None)
            if not fetched: raise ValueError('The snapshot does not contain the backup folder')
        fetched.rename(target)
        shutil.rmtree(FETCH, ignore_errors=True)
        return 'fetched from the repository'
    raise ValueError('This backup is not on this server any more; scan again')


def perform(ledger, host, row):
    """One step of one recovery. Called each loop pass for the oldest recovery that is not finished."""
    ident = row['id']; mode = row['mode']
    try:
        if row['state'] == 'queued':
            update(ledger, ident, 'fetching', 'fetching the backup')
            how = fetch(row)
            update(ledger, ident, 'restoring', how)
            return
        if row['state'] == 'restoring':
            if mode == 'new':
                domains = json.loads(row['domains'])
                site = sites.restore(ledger, host, row['backup_id'], row['name'], domains[0])
                update(ledger, ident, 'waiting', 'creating the site and filling it', child=site['id'])
            elif mode == 'dump':
                job = ledger.submit_site_restore(str(uuid.uuid4()), row['target'], row['backup_id'], 'dump')
                update(ledger, ident, 'waiting', 'restoring the database from the dump', child=job['id'])
            else:
                first = 'files' if mode in ('files', 'both') else 'database'
                job = sites.restore_into(ledger, host, row['backup_id'], row['target'], first)
                update(ledger, ident, 'waiting', 'restoring ' + first + ' into the live site', child=job['id'], phase=first)
            return
        if row['state'] == 'waiting':
            if mode == 'new':
                site = ledger.get(row['child'])
                if site['state'] in ('failed', 'recovery-needed'): raise ValueError('Site creation ' + site['state'] + ': ' + site['error'])
                restore_job = next((j for j in ledger.site_restores(site['id']) if j['scope'] == 'full'), None)
                if site['state'] != 'succeeded' or not restore_job: return
                if restore_job['state'] in ('failed', 'recovery-needed'): raise ValueError('Restore ' + restore_job['state'] + ': ' + restore_job['error'])
                if restore_job['state'] != 'succeeded': return
                domains = json.loads(row['domains'])
                if len(domains) > 1:
                    job = ledger.submit_domains(str(uuid.uuid4()), site['id'], domains)
                    update(ledger, ident, 'hostnames', 'applying the hostnames', child=job['id'], phase='domains')
                    return
                update(ledger, ident, 'succeeded', 'restored as ' + row['name'])
                return
            job = next((j for j in ledger.site_restores(row['target']) if j['id'] == row['child']), None)
            if not job: raise ValueError('The restore job disappeared')
            if job['state'] in ('failed', 'recovery-needed'): raise ValueError('Restore ' + job['state'] + ': ' + job['error'])
            if job['state'] != 'succeeded': return
            if mode == 'both' and row['phase'] == 'files':
                second = sites.restore_into(ledger, host, row['backup_id'], row['target'], 'database')
                update(ledger, ident, 'waiting', 'restoring database into the live site', child=second['id'], phase='database')
                return
            update(ledger, ident, 'succeeded', 'restored into the live site')
            return
        if row['state'] == 'hostnames':
            job = next((j for j in ledger.domain_jobs() if j['id'] == row['child']), None)
            if not job: raise ValueError('The domains job disappeared')
            if job['state'] in ('failed', 'recovery-needed'): raise ValueError('Hostnames ' + job['state'] + ': ' + job['error'])
            if job['state'] != 'succeeded': return
            update(ledger, ident, 'succeeded', 'restored as ' + row['name'] + ' with its hostnames')
    except Exception as exc:
        update(ledger, ident, 'failed', row['step'], str(exc))


def tick(ledger, host):
    """The worker's pass: a requested scan, one step of the oldest unfinished recovery, then one backup action."""
    perform_scan(ledger)
    pending = recoveries(ledger, active=True)
    if pending: perform(ledger, host, pending[0])
    waiting = actions(ledger, active=True)
    if waiting: perform_action(ledger, waiting[0])


def status(ledger):
    return {'scan': read_scan(), 'recoveries': recoveries(ledger), 'actions': actions(ledger)}


# ---- managing what the scan found: download a backup, or let one go

def actions(ledger, active=False):
    with ledger.db() as db:
        rows = db.execute("SELECT * FROM backup_actions WHERE state IN ('queued','running') ORDER BY created" if active
                          else 'SELECT * FROM backup_actions ORDER BY created DESC LIMIT 100').fetchall()
    result = []
    for r in rows:
        row = dict(r)
        try: row['result'] = json.loads(row['result']) if row['result'] else None
        except ValueError: row['result'] = None
        result.append(row)
    return result


def submit_actions(ledger, items):
    """Queue downloads and deletions from the page's picks: [{action, backup: 'site:<id>' | 'dump:<id>'}]. Every pick
    must be in the last scan; a backup that a recovery is using cannot be deleted. Deleting is final for that copy only:
    a local artifact's repository copy stays, and a repository snapshot's local copy stays."""
    scan = read_scan()
    if not scan or scan.get('state') != 'succeeded': raise ValueError('Scan first')
    if not isinstance(items, list) or not items: raise ValueError('Choose at least one backup')
    busy = {r['backup_id'] for r in recoveries(ledger, active=True)} | {a['backup_id'] for a in actions(ledger, active=True)}
    queued = []
    for item in items:
        action = str(item.get('action', ''))
        if action not in ACTIONS: raise ValueError('Unknown backup action')
        backup = str(item.get('backup', ''))
        if not backup.startswith(('site:', 'dump:')): raise ValueError('Name a backup or a dump')
        kind, ident = backup.split(':', 1)
        entry = find_entry(scan, kind, ident)
        if not entry: raise ValueError('That backup is not in the last scan; scan again')
        if entry['id'] in busy: raise ValueError('A recovery or another action is using that backup; wait for it to finish')
        if action == 'delete' and entry['source'].startswith('folder:') and Path(entry['source'][7:]) == Path(scan.get('folder') or '/nonexistent'):
            raise ValueError('That folder is one backup by itself; remove the folder by hand')
        queued.append({'id': str(uuid.uuid4()), 'action': action, 'kind': kind, 'backup_id': entry['id'], 'source': entry['source'],
                       'snapshot': entry.get('snapshot') or '', 'site_name': entry.get('site_name') or '', 'bytes': entry.get('bytes') or 0})
        busy.add(entry['id'])
    now = time.time()
    with ledger.db() as db:
        for row in queued:
            db.execute('INSERT INTO backup_actions (id, action, kind, backup_id, source, snapshot, site_name, bytes, state, step, error, result, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (row['id'], row['action'], row['kind'], row['backup_id'], row['source'], row['snapshot'], row['site_name'], int(row['bytes']), 'queued', 'queued', '', '', now, now))
    return [r['id'] for r in queued]


def update_action(ledger, ident, state, step, error='', result=None):
    if state not in ACTION_STATES: raise ValueError('Unknown action state')
    with ledger.db() as db:
        db.execute('UPDATE backup_actions SET state=?, step=?, error=?, updated=?, result=COALESCE(?, result) WHERE id=?',
                   (state, step, error[:2000], time.time(), json.dumps(result) if result is not None else None, ident))


def forget_entry(kind, ident):
    """The scan no longer lists a backup that is gone."""
    scan = read_scan()
    if not scan or not scan.get('sites'): return
    for site in scan['sites']:
        key = 'backups' if kind == 'site' else 'dumps'
        site[key] = [e for e in site[key] if e['id'] != ident]
        site['local_bytes'] = sum(e.get('bytes') or 0 for e in site['backups'] + site['dumps'] if e['source'] == 'local')
    scan['sites'] = [s for s in scan['sites'] if s['backups'] or s['dumps']]
    write_scan(scan)


def export_dump(ident):
    """A dump's file and manifest in the web-readable folder, as site_backup.export does for a complete backup."""
    root = dumps.artifact_path(ident)
    manifest = json.loads((root / 'manifest.json').read_text())
    if manifest.get('kind') != 'local-database-dump': raise ValueError('Not a database dump.')
    uid, gid = sites.web_identity()
    sites.EXPORTS.mkdir(mode=0o700, exist_ok=True); os.chown(sites.EXPORTS, uid, gid)
    token = str(uuid.uuid4()); folder = sites.EXPORTS / token; folder.mkdir(mode=0o700); os.chown(folder, uid, gid)
    listed = []
    for name in (manifest['file'], 'manifest.json'):
        shutil.copyfile(root / name, folder / name); os.chmod(folder / name, 0o600); os.chown(folder / name, uid, gid)
        listed.append({'name': name, 'bytes': (folder / name).stat().st_size})
    return {'token': token, 'files': listed, 'expires_at': time.time() + sites.EXPORT_TTL}


def delete_local(ledger, row):
    module = dumps if row['kind'] == 'dump' else sites
    target = module.artifact_path(row['backup_id'])
    if target.is_dir() and not target.is_symlink():
        trusted(target, directory=True); shutil.rmtree(target)
    manifest_cache(row['backup_id']).unlink(missing_ok=True)
    if row['kind'] == 'dump': ledger.finish_backup(row['backup_id'], 'pruned', 'removed by the operator')
    else:
        with ledger.db() as db: kept = db.execute('SELECT manifest FROM site_backups WHERE id=?', (row['backup_id'],)).fetchone()
        manifest = None
        if kept and kept['manifest']:
            try: manifest = json.loads(kept['manifest'])
            except ValueError: manifest = None
        ledger.finish_site_backup(row['backup_id'], 'pruned', 'removed by the operator', manifest)
    return 'removed from this server' + ('; the repository copy stays' if row.get('offsite') == 'verified' else '')


def delete_remote(ledger, row):
    from . import remote_backup as remote
    config = remote.settings()
    if not config: raise ValueError('No backup destination is connected')
    if not row['snapshot']: raise ValueError('The scan did not record a snapshot for this backup')
    remote.execute(config, ['forget', row['snapshot']])
    with ledger.db() as db:
        db.execute('UPDATE remote_copies SET forgotten=? WHERE destination=? AND job_id=?', (time.time(), config['destination'], row['backup_id']))
    remote.execute(config, ['prune'], maximum=64 * 1024**2)
    return 'forgotten and the repository pruned; a local copy, if any, stays'


def delete_folder(row):
    origin = Path(row['source'][len('folder:'):])
    if not (origin / 'manifest.json').is_file(): raise ValueError('The folder no longer holds this backup')
    shutil.rmtree(origin)
    return 'removed from the folder'


def perform_action(ledger, row):
    """One backup action, whole: a download makes the artifact local if it is not, then prepares the files for six hours;
    a deletion removes the copy the scan listed and nothing else."""
    ident = row['id']
    try:
        update_action(ledger, ident, 'running', 'working')
        if row['action'] == 'download':
            fetched = fetch({'mode': 'dump' if row['kind'] == 'dump' else 'site', 'backup_id': row['backup_id'], 'source': row['source'], 'snapshot': row['snapshot']})
            result = export_dump(row['backup_id']) if row['kind'] == 'dump' else sites.export(row['backup_id'])
            update_action(ledger, ident, 'succeeded', fetched + '; files ready for six hours', result=result)
            return
        if row['source'] == 'local': how = delete_local(ledger, row)
        elif row['source'] == 'repository': how = delete_remote(ledger, row)
        elif row['source'].startswith('folder:'): how = delete_folder(row)
        else: raise ValueError('Unknown backup source')
        forget_entry(row['kind'], row['backup_id'])
        update_action(ledger, ident, 'succeeded', how)
    except Exception as exc:
        update_action(ledger, ident, 'failed', row['step'] if row['state'] != 'queued' else 'working', str(exc))
