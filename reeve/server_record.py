"""The server's own record: what a replacement server needs beyond the site backups. Settings from
server.yaml, the release, the hostname and the inventory of sites with their hostnames and latest complete
backup, plus the deleted sites whose final backups still exist. Written to the worker's folder whenever it
changes and copied to the backup repository as its own snapshot (tag `hosting-server`), so a fresh install
that connects the repository can read what was hosted and restore it without notes kept elsewhere.

What stays outside on purpose: the destination and its repository password (they unlock the repository)
and the operator password (a fresh install prints its own).
"""
import hashlib
import json
import socket
import time

from .host import OPS

RECORD = OPS / 'panel/worker/server-record.json'
TAG = 'hosting-server'
EXCLUDED_KEYS = ('schema',)


def build(ledger):
    """The record as a mapping. Settings are the whole document minus the schema line, so nothing an operator
    set is lost on a rebuild; sites carry what restoration asks for."""
    from . import settings as server_settings, site_backup, updates
    document = {k: v for k, v in server_settings.document().items() if k not in EXCLUDED_KEYS}
    sites = []
    for row in ledger.list():
        if row['state'] == 'deleted': continue
        backups = [b for b in ledger.site_backups(row['id']) if b['state'] == 'succeeded' and b['manifest']]
        latest = None
        for backup in backups:
            manifest = json.loads(backup['manifest'])
            if manifest.get('coverage') != 'complete' and manifest.get('coverage') is not None: continue
            candidate = {'id': backup['id'], 'kind': backup['kind'], 'completed_at': manifest.get('completed_at')}
            if latest is None or (candidate['completed_at'] or 0) > (latest['completed_at'] or 0): latest = candidate
        payload = json.loads(row['payload'])
        sites.append({'id': row['id'], 'name': row['name'], 'runtime': payload.get('runtime'), 'domains': ledger.domains(row),
                      'quiesce': site_backup.options(ledger, row)['quiesce'], 'state': row['state'], 'latest_backup': latest})
    deleted = [{'name': d['name'], 'domain': d['domain'], 'deleted_at': d['deleted_at'], 'runtime': d['runtime'],
                'final_backup': {k: d['final_backup'][k] for k in ('id', 'kind', 'completed_at')} if d['final_backup'] else None}
               for d in site_backup.deleted_sites(ledger)]
    return {'schema': 1, 'kind': 'server-record', 'version': updates.installed().get('version'), 'hostname': socket.gethostname(),
            'settings': document, 'sites': sorted(sites, key=lambda s: s['name']), 'deleted': deleted}


def digest(record):
    """Pure: the identity of a record's content, ignoring when it was written."""
    body = {k: v for k, v in record.items() if k != 'written_at'}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def current():
    if not RECORD.exists(): return None
    try: return json.loads(RECORD.read_text())
    except (OSError, ValueError): return None


def write(ledger):
    """Write the record if its content changed. Returns True when it did."""
    record = build(ledger)
    previous = current()
    if previous and digest(previous) == digest(record): return False
    record['written_at'] = time.time()
    RECORD.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = RECORD.with_name('.server-record.new')
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True)); tmp.chmod(0o600); tmp.replace(RECORD)
    return True


def parse(text):
    """Pure: a record read back from a repository or a folder, checked to be one."""
    record = json.loads(text)
    if not isinstance(record, dict) or record.get('kind') != 'server-record' or record.get('schema') != 1: raise ValueError('Not a server record')
    if not isinstance(record.get('sites'), list) or not isinstance(record.get('settings'), dict): raise ValueError('Damaged server record')
    return record
