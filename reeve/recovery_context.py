"""Optional owner knowledge, separate from discovered inventory and backup success."""
import json
import time

from .core import request_id
from .host import OPS, atomic, trusted
from .compose_inspect import regular
from .recovery_inventory import digest

STORE = OPS / 'panel/worker/recovery-context'


def read(row):
    request_id(row['id'])
    path = STORE / (row['id'] + '.json')
    if not path.exists() and not path.is_symlink():
        return {'revision': '', 'external': 'unknown', 'notes': '', 'checks': '', 'updated': None}
    trusted(STORE, directory=True)
    value = json.loads(regular(path))
    if value.get('schema') != 1 or value.get('site_id') != row['id']:
        raise ValueError('Saved site notes could not be read. Previous records have been retained.')
    return {k: value[k] for k in ('revision', 'external', 'notes', 'checks', 'updated')}


def save(ledger, row, data):
    if not isinstance(data, dict) or set(data) not in ({'revision', 'external', 'notes'}, {'revision', 'checks'}):
        raise ValueError('Invalid site notes fields.')
    if 'external' in data and data['external'] not in ('unknown', 'yes', 'no'):
        raise ValueError('Choose Yes, No or I don’t know.')
    for field in ('notes', 'checks'):
        if field in data and (not isinstance(data[field], str) or len(data[field]) > 1200 or
                any(ord(c) < 32 and c not in '\n\r\t' for c in data[field])):
            raise ValueError('Keep notes within 1,200 characters and use plain text.')
    # Serialize only the short metadata write. No Docker inspection or site mutation.
    with ledger.db() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = read(row)
        if data['revision'] != previous['revision']:
            raise ValueError('These notes changed in another window. Review the latest notes before saving.')
        value = {k: previous[k] for k in ('external', 'notes', 'checks')}
        value.update({k: v.strip() if isinstance(v, str) else v for k, v in data.items() if k != 'revision'})
        if all(value[k] == previous[k] for k in value): return previous
        record = {'schema': 1, 'site_id': row['id'], **value}
        record.update(revision=digest(record), updated=time.time())
        STORE.mkdir(mode=0o700, exist_ok=True); trusted(STORE, directory=True)
        atomic(STORE / (row['id'] + '.json'), json.dumps(record, sort_keys=True))
    return read(row)


def summary(backup):
    """Summarize evidence, never a declaration or image/engine-name assumption."""
    remote = backup.get('remote') or {}
    local = bool(backup.get('available') and backup.get('last_success'))
    copied = bool(remote.get('last_copy'))
    if backup.get('error'):
        return {'title': 'Backup status unavailable', 'detail': 'The last backup state could not be checked. Complete site recovery has not been verified.'}
    site = backup.get('site') or {}
    if site.get('available') and remote.get('last_site_copy'):
        return {'title': 'Backed up and copied off', 'detail': 'A complete site backup exists and its latest copy is verified on the destination. Its restore has not been tested.'}
    if site.get('available'):
        return {'title': 'Backed up locally', 'detail': 'A complete local site backup exists. It has not been copied off this machine and its restore has not been tested.'}
    if local or copied:
        return {'title': 'Partially protected', 'detail': 'Database copies are available. Files and configuration still need backup coverage.'}
    return {'title': 'Not yet protected', 'detail': 'No available backup is recorded for this site. Complete site backups are not available yet.'}
