"""Durable local database dump attempts; successful artifacts are never pruned here."""
import json
import time
import uuid


class BackupLedger:
    def initialize_backups(self, db):
        db.execute("""CREATE TABLE IF NOT EXISTS backup_jobs (
            id TEXT PRIMARY KEY, site_id TEXT NOT NULL, state TEXT NOT NULL, error TEXT NOT NULL,
            artifact TEXT NOT NULL, cleanup_error TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS backup_schedules (
            site_id TEXT PRIMARY KEY, interval INTEGER NOT NULL, enabled INTEGER NOT NULL, next_run REAL NOT NULL)""")
        from .remote_backup import initialize
        initialize(db)
        from .site_backup import initialize as initialize_site_backups
        initialize_site_backups(db)
        db.execute('PRAGMA user_version=13')

    def backup_pending(self, db, site_id):
        return (db.execute("SELECT 1 FROM backup_jobs WHERE site_id=? AND (state IN ('queued','running','recovery-needed') OR cleanup_error!='')", (site_id,)).fetchone()
                or db.execute("SELECT 1 FROM site_backups WHERE site_id=? AND state IN ('queued','running','recovery-needed')", (site_id,)).fetchone()
                or db.execute("SELECT 1 FROM site_deletes WHERE site_id=? AND state IN ('queued','running','recovery-needed')", (site_id,)).fetchone()
                or db.execute("SELECT 1 FROM site_restores WHERE site_id=? AND state IN ('queued','running','recovery-needed')", (site_id,)).fetchone())

    def submit_site_backup(self, ident, site_id, kind='manual'):
        from .core import request_id
        request_id(ident); request_id(site_id)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM site_backups WHERE id=?', (ident,)).fetchone()
            if old:
                if old['site_id'] != site_id: raise ValueError('Request identifier belongs to another site')
                return dict(old)
            row = self.content_ready(db, site_id)
            from .site_backup import supported
            if not supported(row):
                raise ValueError('Complete site backups cover managed sites and Compose packages only.')
            now = time.time()
            db.execute("INSERT INTO site_backups (id, site_id, kind, state, step, error, manifest, created, updated) VALUES (?,?,?,'queued','','','',?,?)", (ident, site_id, kind, now, now))
        return next(j for j in self.site_backups(site_id) if j['id'] == ident)

    def site_backups(self, site_id=None, active=False, limit=50):
        where, args = [], []
        if site_id: where.append('site_id=?'); args.append(site_id)
        if active: where.append("state IN ('queued','running','recovery-needed')")
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM site_backups' + (' WHERE ' + ' AND '.join(where) if where else '') +
                                                ' ORDER BY created DESC' + ('' if active or limit is None else ' LIMIT ' + str(int(limit))), args)]

    def finish_site_backup(self, ident, state, error='', manifest=None, step=None):
        if state not in ('running', 'succeeded', 'failed', 'recovery-needed', 'pruned'): raise ValueError('Invalid site backup state')
        with self.db() as db:
            if step is not None:
                db.execute('UPDATE site_backups SET state=?,step=?,updated=? WHERE id=?', (state, step, time.time(), ident))
            else:
                db.execute('UPDATE site_backups SET state=?,error=?,manifest=?,updated=? WHERE id=?',
                           (state, error, json.dumps(manifest, sort_keys=True) if manifest else '', time.time(), ident))

    def submit_site_delete(self, ident, site_id):
        from .core import request_id
        request_id(ident); request_id(site_id)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM site_deletes WHERE id=?', (ident,)).fetchone()
            if old:
                if old['site_id'] != site_id: raise ValueError('Request identifier belongs to another site')
                return dict(old)
            row = self.content_ready(db, site_id)
            from .site_backup import supported
            if not supported(row):
                raise ValueError('Delete covers managed sites and Compose packages; a final complete backup is required.')
            now = time.time()
            db.execute("INSERT INTO site_deletes VALUES (?,?,'','queued','final backup','',?,?)", (ident, site_id, now, now))
        return next(j for j in self.site_deletes(site_id) if j['id'] == ident)

    def site_deletes(self, site_id=None, active=False):
        where, args = [], []
        if site_id: where.append('site_id=?'); args.append(site_id)
        if active: where.append("state IN ('queued','running','recovery-needed')")
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM site_deletes' + (' WHERE ' + ' AND '.join(where) if where else '') +
                                                ' ORDER BY created DESC', args)]

    def finish_site_delete(self, ident, state, step, error=''):
        if state not in ('queued', 'running', 'succeeded', 'failed', 'recovery-needed'): raise ValueError('Invalid delete state')
        with self.db() as db:
            db.execute('UPDATE site_deletes SET state=?,step=?,error=?,updated=? WHERE id=?', (state, step, error[:2000], time.time(), ident))

    def retry_site_delete(self, ident):
        from .core import request_id
        request_id(ident)
        with self.db() as db:
            db.execute("UPDATE site_deletes SET state='queued',error='',updated=? WHERE id=? AND state IN ('failed','recovery-needed')", (time.time(), ident))
        return next(j for j in self.site_deletes() if j['id'] == ident)

    def site_restores(self, site_id=None, active=False, source=None):
        where, args = [], []
        if site_id: where.append('site_id=?'); args.append(site_id)
        if active: where.append("state IN ('queued','running','recovery-needed')")
        if source:
            where.append('snapshot IN (SELECT id FROM site_backups WHERE site_id=?)'); args.append(source)
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM site_restores' + (' WHERE ' + ' AND '.join(where) if where else '') +
                                                ' ORDER BY created DESC', args)]

    def submit_site_restore(self, ident, site_id, snapshot, scope):
        from .core import request_id
        request_id(ident); request_id(site_id); request_id(snapshot)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM site_restores WHERE id=?', (ident,)).fetchone()
            if old: return dict(old)
            self.content_ready(db, site_id)
            now = time.time()
            db.execute("INSERT INTO site_restores (id, site_id, snapshot, state, step, error, created, updated, scope) VALUES (?,?,?,'queued','queued','',?,?,?)",
                       (ident, site_id, snapshot, now, now, scope))
        return next(j for j in self.site_restores(site_id) if j['id'] == ident)

    def finish_site_restore(self, ident, state, step, error=''):
        if state not in ('queued', 'running', 'succeeded', 'failed', 'recovery-needed'): raise ValueError('Invalid restore state')
        with self.db() as db:
            db.execute('UPDATE site_restores SET state=?,step=?,error=?,updated=? WHERE id=?', (state, step, error[:2000], time.time(), ident))

    def retry_site_restore(self, ident):
        from .core import request_id
        request_id(ident)
        with self.db() as db:
            db.execute("UPDATE site_restores SET state='queued',error='',updated=? WHERE id=? AND state IN ('failed','recovery-needed')", (time.time(), ident))
        return next(j for j in self.site_restores() if j['id'] == ident)

    def release_site(self, row):
        """Free the name, hostnames and project for reuse; keep the row and all backup records."""
        suffix = '~deleted-' + row['id'][:8]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM site_domains WHERE site_id=?', (row['id'],))
            db.execute('DELETE FROM adopted_projects WHERE site_id=?', (row['id'],))
            db.execute('DELETE FROM backup_schedules WHERE site_id=?', (row['id'],))
            db.execute('DELETE FROM site_backup_schedules WHERE site_id=?', (row['id'],))
            for table in ('schedules', 'schedule_runs', 'site_runtime', 'toolbox_sessions'):
                try: db.execute('DELETE FROM ' + table + ' WHERE site_id=?', (row['id'],))
                except Exception: pass
            db.execute("UPDATE jobs SET name=?, domain=?, state='deleted', step='deleted', error='', updated=? WHERE id=?",
                       (row['name'] + suffix, row['domain'] + suffix, time.time(), row['id']))

    def backup_jobs(self, site_id=None, active=False):
        where, args = [], []
        if site_id: where.append('site_id=?'); args.append(site_id)
        if active: where.append("(state IN ('queued','running','recovery-needed') OR cleanup_error!='')")
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM backup_jobs' +
                (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY created DESC' +
                ('' if active else ' LIMIT 50'), args)]

    def submit_backup(self, ident, site_id):
        from .core import request_id
        from .database_site import state
        request_id(ident); request_id(site_id)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM backup_jobs WHERE id=?', (ident,)).fetchone()
            if old:
                if old['site_id'] != site_id: raise ValueError('Request identifier belongs to another site')
                return dict(old)
            row = self.content_ready(db, site_id)
            if not supported(row):
                raise ValueError('Local dumps need a ready managed database or one recognised MySQL, MariaDB or PostgreSQL Compose service.')
            now = time.time()
            db.execute("INSERT INTO backup_jobs VALUES (?,?,'queued','','','',?,?)", (ident, site_id, now, now))
        return next(j for j in self.backup_jobs(site_id) if j['id'] == ident)

    def finish_backup(self, ident, state, error='', artifact=None, cleanup_error=''):
        if state not in ('running', 'succeeded', 'failed', 'recovery-needed', 'pruned'): raise ValueError('Invalid backup state')
        with self.db() as db:
            db.execute('UPDATE backup_jobs SET state=?,error=?,artifact=?,cleanup_error=?,updated=? WHERE id=?',
                (state, error, json.dumps(artifact, sort_keys=True) if artifact else '', cleanup_error, time.time(), ident))

    def backup_schedule(self, site_id, interval, enabled):
        from .database_site import state
        if type(interval) is not int or interval not in (15, 60) or type(enabled) is not bool:
            raise ValueError('Choose 15 or 60 minutes and an enabled state')
        row = self.get(site_id)
        if not supported(row):
            raise ValueError('This site has no supported database')
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO backup_schedules VALUES (?,?,?,?)',
                       (site_id, interval, int(enabled), time.time() + interval * 60))


def supported(row):
    """A ready managed database, or a package with exactly one recognised database service."""
    from .database_site import state
    payload = json.loads(row['payload'])
    if payload.get('runtime') != 'compose':
        info = state(row)
        return bool(info and info.get('stage') == 'ready')
    if not payload.get('package_id') or row['state'] != 'succeeded': return False
    from .package_deploy import dump_target
    return dump_target(row) is not None


def tick(ledger, now=None):
    now = time.time() if now is None else now
    for row in ledger.list():
        if row['state'] != 'succeeded': continue
        if supported(row):
            with ledger.db() as db:
                db.execute('INSERT OR IGNORE INTO backup_schedules VALUES (?,15,1,?)', (row['id'], now + 900))
    with ledger.db() as db:
        due = [dict(r) for r in db.execute('SELECT * FROM backup_schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run', (now,))]
    for item in due:
        try: ledger.submit_backup(str(uuid.uuid4()), item['site_id'])
        except ValueError: continue  # Another operation holds the site; leave this dump due.
        with ledger.db() as db:
            db.execute('UPDATE backup_schedules SET next_run=? WHERE site_id=? AND next_run=?',
                       (now + item['interval'] * 60, item['site_id'], item['next_run']))


def status(ledger, row):
    from .remote_backup import status as remote_status
    from .database_site import state
    from .database_backup import policy, usage, artifact_path
    jobs = ledger.backup_jobs(row['id'])
    scope = 'Managed application database "site" only; files, other databases and server roles are excluded.'
    if json.loads(row['payload']).get('runtime') == 'compose':
        from .package_deploy import dump_target
        target = dump_target(row) if json.loads(row['payload']).get('package_id') else None
        scope = ('Compose service "' + target['service'] + '" database "' + target['database'] + '" only; files, other databases and server roles are excluded.') if target else 'No recognised database service.'
    with ledger.db() as db:
        schedule = db.execute('SELECT * FROM backup_schedules WHERE site_id=?', (row['id'],)).fetchone()
        success = db.execute("SELECT * FROM backup_jobs WHERE site_id=? AND state='succeeded' ORDER BY created DESC LIMIT 1", (row['id'],)).fetchone()
    latest = jobs[0] if jobs else None
    artifact = json.loads(success['artifact']) if success and success['artifact'] else None
    available = bool(artifact and (artifact_path(success['id']) / artifact['file']).is_file())
    successful = dict(success) if success else None
    if successful and artifact:
        successful['updated'] = artifact.get('completed_at', successful['updated'])
    from .site_backup import status as site_status
    site = site_status(ledger, row)
    return {'supported': supported(row),
            'schedule': dict(schedule) if schedule else None, 'latest': latest,
            'last_success': successful, 'artifact': artifact,
            'available': available, 'policy': policy(), 'stored_bytes': usage(),
            'scope': scope, 'site': site,
            'remote': remote_status(ledger, row['id']), 'local_full': 'available' if site['available'] else 'not configured',
            'remote_full': 'copied' if (remote_status(ledger, row['id']).get('last_site_copy')) else 'not configured'}
