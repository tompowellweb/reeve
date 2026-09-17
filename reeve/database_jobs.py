"""One optional database per site, with durable initialization and retry."""
import json
import time


def validate_database(data):
    from .database_versions import ENGINES, numeric
    from .core import BOUNDS
    from .database_site import USAGES
    if not isinstance(data, dict) or set(data) - {'engine', 'series', 'exact', 'memory_mb', 'cpus', 'layer_mb', 'pids_limit', 'usage'}:
        raise ValueError('Unknown database fields')
    if data.get('engine') not in ENGINES: raise ValueError('Choose MySQL, MariaDB or PostgreSQL')
    if data.get('usage') not in (None, '', *USAGES): raise ValueError('Database usage must be light, standard or high')
    result = {'engine': data['engine']}
    if data.get('usage'): result['usage'] = data['usage']
    else:
        from .profile import settings
        result['usage'] = settings()['database_usage']
    if data.get('exact'):
        if not numeric(data['exact'], data['engine']): raise ValueError('Use an exact numeric database release, without image names or suffixes')
        result['exact'] = data['exact']
    elif data.get('series'):
        import re
        if not isinstance(data['series'], str) or not re.fullmatch(r'\d+(?:\.\d+)?', data['series']): raise ValueError('Invalid database series')
        result['series'] = data['series']
    for key in ('memory_mb', 'cpus', 'layer_mb', 'pids_limit'):
        value = data.get(key)
        if value is None: continue
        low, high = BOUNDS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or (key != 'cpus' and int(value) != value):
            raise ValueError('Invalid database ' + key)
        result[key] = value
    return result


class DatabaseLedger:
    def initialize_database_jobs(self, db):
        db.execute("CREATE TABLE IF NOT EXISTS database_jobs (id TEXT PRIMARY KEY, site_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, step TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)")
        db.execute('PRAGMA user_version=6')

    def database_pending(self, db, site_id):
        return db.execute("SELECT 1 FROM database_jobs WHERE site_id=? AND state!='succeeded'", (site_id,)).fetchone()

    def database_jobs(self, site_id=None):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM database_jobs' + (' WHERE site_id=?' if site_id else '') + ' ORDER BY created DESC', (site_id,) if site_id else ())]

    def submit_database(self, ident, kind, site_id=None, payload=None):
        from .core import request_id
        ident = request_id(ident)
        if kind == 'add':
            request_id(site_id); payload = validate_database(payload)
        elif kind != 'refresh' or site_id is not None or payload:
            raise ValueError('Invalid database operation')
        encoded = json.dumps(payload or {}, sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT * FROM database_jobs WHERE id=?', (ident,)).fetchone()
            if prior:
                if (prior['kind'], prior['site_id'], prior['payload']) != (kind, site_id, encoded): raise ValueError('Request identifier belongs to different inputs')
                return dict(prior)
            if kind == 'refresh':
                prior = db.execute("SELECT * FROM database_jobs WHERE kind='refresh' AND state IN ('queued','running')").fetchone()
                if prior: return dict(prior)
            else:
                row = db.execute('SELECT * FROM jobs WHERE id=?', (site_id,)).fetchone()
                if not row or row['state'] != 'succeeded': raise ValueError('Finish site setup before adding a database')
                if json.loads(row['payload']).get('database') or db.execute("SELECT 1 FROM database_jobs WHERE site_id=? AND kind='add'", (site_id,)).fetchone():
                    raise ValueError('This site already has a database request; retry it instead')
                if self.content_pending(db, site_id) or self.runtime_pending(db, site_id) or db.execute("SELECT 1 FROM domain_jobs WHERE site_id=? AND state!='succeeded'", (site_id,)).fetchone():
                    raise ValueError('Finish or retry the pending site change first')
            now = time.time()
            db.execute("INSERT INTO database_jobs VALUES (?,?,?,?,'queued','reserved','',?,?)", (ident, site_id, kind, encoded, now, now))
        return next(j for j in self.database_jobs() if j['id'] == ident)

    def update_database(self, ident, state, step, error=''):
        from .core import STATES
        if state not in STATES: raise ValueError('Invalid database job state')
        with self.db() as db:
            db.execute('UPDATE database_jobs SET state=?,step=?,error=?,updated=? WHERE id=?', (state, step, error[:2000], time.time(), ident))

    def retry_database(self, ident):
        from .core import request_id
        request_id(ident)
        with self.db() as db:
            row = db.execute('SELECT * FROM database_jobs WHERE id=?', (ident,)).fetchone()
            if not row: raise ValueError('Unknown database operation')
            db.execute("UPDATE database_jobs SET state='queued',error='',updated=? WHERE id=? AND state IN ('failed','recovery-needed')", (time.time(), ident))
        return dict(row)
