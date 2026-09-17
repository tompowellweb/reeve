"""Durable catalogue refresh and PHP switch jobs, with immutable Create inputs."""
import json
import time


class RuntimeLedger:
    def initialize_runtime_jobs(self, db):
        db.execute("CREATE TABLE IF NOT EXISTS runtime_jobs (id TEXT PRIMARY KEY, site_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, step TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS site_runtime (site_id TEXT PRIMARY KEY, branch TEXT NOT NULL)")
        db.execute('PRAGMA user_version=5')

    def runtime_pending(self, db, site_id):
        return db.execute("SELECT 1 FROM runtime_jobs WHERE site_id=? AND state!='succeeded' AND kind NOT IN ('refresh','rebuild')", (site_id,)).fetchone()

    def runtime_jobs(self, site_id=None):
        with self.db() as db:
            query = 'SELECT * FROM runtime_jobs'
            args = ()
            if site_id:
                query += ' WHERE site_id=?'
                args = (site_id,)
            return [dict(row) for row in db.execute(query + ' ORDER BY created DESC', args)]

    def runtime_branch(self, row):
        with self.db() as db:
            override = db.execute('SELECT branch FROM site_runtime WHERE site_id=?', (row['id'],)).fetchone()
        return override[0] if override else json.loads(row['payload']).get('php_version')

    def submit_runtime(self, ident, kind, site_id=None, payload=None):
        from .core import request_id, php_branch
        ident = request_id(ident)
        payload = payload or {}
        if kind in ('refresh', 'rebuild'):
            if site_id is not None or payload:
                raise ValueError('Refresh has no site inputs')
        elif kind == 'switch':
            request_id(site_id)
            if set(payload) != {'branch'}:
                raise ValueError('Switch requires a branch')
            php_branch(payload['branch'])
        elif kind == 'rollback':
            request_id(site_id)
            if set(payload) != {'previous'}:
                raise ValueError('Rollback requires the previous successful change')
            request_id(payload['previous'])
        else:
            raise ValueError('Unknown runtime operation')
        encoded = json.dumps(payload, sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM runtime_jobs WHERE id=?', (ident,)).fetchone()
            if prior:
                if (prior['kind'], prior['site_id'], prior['payload']) != (kind, site_id, encoded):
                    raise ValueError('Request identifier already belongs to different inputs')
                return dict(prior)
            if kind in ('refresh', 'rebuild'):
                prior = db.execute("SELECT * FROM runtime_jobs WHERE kind=? AND state IN ('queued','running')", (kind,)).fetchone()
                if prior:
                    return dict(prior)
            else:
                row = db.execute('SELECT * FROM jobs WHERE id=?', (site_id,)).fetchone()
                if not row or row['state'] != 'succeeded' or json.loads(row['payload']).get('runtime') != 'php':
                    raise ValueError('Choose a successfully created PHP site')
                if self.content_pending(db, site_id) or self.runtime_pending(db, site_id) or self.database_pending(db, site_id) or db.execute("SELECT 1 FROM domain_jobs WHERE site_id=? AND state!='succeeded'", (site_id,)).fetchone():
                    raise ValueError('Finish or retry the pending site change first')
                if kind == 'rollback':
                    previous = db.execute("SELECT * FROM runtime_jobs WHERE site_id=? AND kind IN ('switch','rollback') AND state='succeeded' ORDER BY created DESC LIMIT 1", (site_id,)).fetchone()
                    if not previous or previous['id'] != payload['previous']:
                        raise ValueError('Only the most recent successful PHP change can be rolled back')
            now = time.time()
            db.execute("INSERT INTO runtime_jobs VALUES (?,?,?,?,'queued','reserved','',?,?)", (ident, site_id, kind, encoded, now, now))
        return next(row for row in self.runtime_jobs() if row['id'] == ident)

    def update_runtime(self, ident, state, step, error=''):
        from .core import STATES
        if state not in STATES:
            raise ValueError('Unknown runtime state')
        with self.db() as db:
            db.execute('UPDATE runtime_jobs SET state=?,step=?,error=?,updated=? WHERE id=?', (state, step, error[:2000], time.time(), ident))

    def finish_runtime(self, job, branch=None):
        with self.db() as db:
            if branch:
                db.execute('INSERT OR REPLACE INTO site_runtime VALUES (?,?)', (job['site_id'], branch))
            db.execute("UPDATE runtime_jobs SET state='succeeded',step='verified',error='',updated=? WHERE id=?", (time.time(), job['id']))

    def retry_runtime(self, ident):
        from .core import request_id
        request_id(ident)
        with self.db() as db:
            row = db.execute('SELECT * FROM runtime_jobs WHERE id=?', (ident,)).fetchone()
            if not row:
                raise ValueError('Unknown runtime operation')
            db.execute("UPDATE runtime_jobs SET state='queued',error='',updated=? WHERE id=? AND state IN ('failed','recovery-needed')", (time.time(), ident))
        return dict(row)
