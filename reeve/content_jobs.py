"""Durable content operations. Never automatically replay imports or application commands."""
import json
import re
import shlex
import time

UPLOAD_LIMIT = 512 * 1048576
EDIT_LIMIT = 64 * 1024


def relative(value, root=False):
    if root and value in ('', '.'):
        return '.'
    if not isinstance(value, str) or len(value.encode()) > 1024 or value.startswith('/'):
        raise ValueError('Use a path relative to the site content directory')
    if any(p in ('', '.', '..') or len(p.encode()) > 255 for p in value.split('/')) or any(ord(c) < 32 or c in '\\:' for c in value):
        raise ValueError('Unsafe content path')
    return value


def validate_content(kind, data):
    if kind=='web-settings':
        from .requests_site import validate
        return validate(data)
    if kind=='site-rules':
        from .site_rules import validate
        return validate(data)
    if kind=='php-settings':
        from .php_settings import validate
        return validate(data)
    if kind=='database-usage':
        from .database_site import validate_usage
        return validate_usage(data)
    if kind=='sftp-access':
        from .sftp import validate
        return validate(data)
    if kind=='mail-senders':
        from .mail import validate_senders
        return validate_senders(data)
    if kind=='fix-ownership':
        if data not in ({}, None): raise ValueError('Ownership fix takes no settings')
        return {}
    if kind in ('toolbox-start','toolbox-stop'):
        from .toolbox import validate
        return validate(kind,data)
    if not isinstance(data, dict): raise ValueError('Expected content settings')
    if kind in ('upload', 'extract', 'sql', 'edit'):
        if set(data) != {'sha256', 'size', 'path', 'replace', 'expected'}: raise ValueError('Invalid upload settings')
        if not isinstance(data['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', data['sha256']): raise ValueError('Invalid upload digest')
        if type(data['size']) is not int or not 0 <= data['size'] <= UPLOAD_LIMIT: raise ValueError('Upload exceeds 512 MiB')
        if type(data['replace']) is not bool: raise ValueError('Invalid replacement choice')
        relative(data['path'], root=kind in ('extract', 'sql'))
        if kind == 'sql' and data['path'] != '.': raise ValueError('SQL import has no content destination')
        if kind == 'edit':
            if data['size'] > EDIT_LIMIT or not re.fullmatch('[0-9a-f]{64}', data['expected']): raise ValueError('Invalid editor size or revision')
        elif data['expected']: raise ValueError('Unexpected file revision')
    elif kind == 'tool':
        if set(data) != {'tool', 'arguments', 'path', 'internet'}: raise ValueError('Invalid tool settings')
        if data['tool'] not in ('php', 'composer', 'wp', 'console', 'shell'): raise ValueError('Choose a supported site tool')
        relative(data['path'], root=True)
        if type(data['internet']) is not bool: raise ValueError('Invalid network choice')
        if not isinstance(data['arguments'], str) or len(data['arguments']) > 4096 or '\x00' in data['arguments']: raise ValueError('Tool arguments are too long or invalid')
        if data['tool'] != 'shell': shlex.split(data['arguments'])
    else: raise ValueError('Unknown content operation')
    return dict(data)


class ContentLedger:
    def initialize_content_jobs(self, db):
        db.execute("CREATE TABLE IF NOT EXISTS content_jobs (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, step TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS toolbox_sessions (site_id TEXT PRIMARY KEY, start_id TEXT NOT NULL, state TEXT NOT NULL, details TEXT NOT NULL, updated REAL NOT NULL, name TEXT NOT NULL)")
        db.execute('PRAGMA user_version=8')

    def content_pending(self, db, site_id, toolbox=True):
        if self.backup_pending(db, site_id): return True
        return db.execute("SELECT 1 FROM content_jobs WHERE site_id=? AND state IN ('queued','running','recovery-needed')", (site_id,)).fetchone() or (toolbox and db.execute("SELECT 1 FROM toolbox_sessions WHERE site_id=? AND state IN ('starting','active')",(site_id,)).fetchone())

    def content_jobs(self, site_id=None):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM content_jobs' + (' WHERE site_id=?' if site_id else '') + ' ORDER BY created DESC', (site_id,) if site_id else ())]

    def content_ready(self, db, site_id, stop=False):
        row = db.execute('SELECT * FROM jobs WHERE id=?', (site_id,)).fetchone()
        if not row or row['state'] != 'succeeded': raise ValueError('Finish site setup first')
        if self.content_pending(db, site_id, toolbox=not stop) or self.runtime_pending(db, site_id) or self.database_pending(db, site_id) or db.execute("SELECT 1 FROM domain_jobs WHERE site_id=? AND state!='succeeded'", (site_id,)).fetchone():
            raise ValueError('Finish or resolve the pending site operation first')
        return dict(row)

    def submit_content(self, ident, site_id, kind, data, prepare=lambda row: None):
        from .core import request_id
        request_id(ident); request_id(site_id)
        data = validate_content(kind, data)
        encoded = json.dumps(data, sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM content_jobs WHERE id=?', (ident,)).fetchone()
            if previous:
                if (previous['site_id'], previous['kind'], previous['payload']) != (site_id, kind, encoded): raise ValueError('Request identifier belongs to different inputs')
                return dict(previous)
            row = self.content_ready(db, site_id, stop=kind=='toolbox-stop')
            if kind == 'tool' and data['tool'] != 'shell' and json.loads(row['payload']).get('runtime') != 'php': raise ValueError('This tool needs a PHP site')
            # Claim immutable input before acknowledging the durable operation. A failed
            # transaction can leave only a private orphan, reconciled on worker startup.
            prepare(row)
            now = time.time()
            db.execute("INSERT INTO content_jobs VALUES (?,?,?,?,'queued','reserved','',?,?)", (ident, site_id, kind, encoded, now, now))
        return next(j for j in self.content_jobs(site_id) if j['id'] == ident)

    def update_content(self, ident, state, step, error=''):
        from .core import STATES
        if state not in STATES: raise ValueError('Invalid content state')
        with self.db() as db:
            db.execute('UPDATE content_jobs SET state=?,step=?,error=?,updated=? WHERE id=?', (state, step, error[:2000], time.time(), ident))
