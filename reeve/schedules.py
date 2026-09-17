"""Interval schedules enqueue normal site-UID tools, with no overlapping runs or replay backlog."""
import json
import re
import time
import uuid
from .content_jobs import validate_content


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS schedules (site_id TEXT NOT NULL, name TEXT NOT NULL, interval INTEGER NOT NULL, payload TEXT NOT NULL, enabled INTEGER NOT NULL, next_run REAL NOT NULL, last_job TEXT, error TEXT NOT NULL, PRIMARY KEY(site_id,name))')
    db.execute('CREATE TABLE IF NOT EXISTS schedule_runs (site_id TEXT NOT NULL, name TEXT NOT NULL, job_id TEXT PRIMARY KEY)')
    # Version 10 also marks persisted WP-CLI payloads: older tools treat unknown kinds as shell.
    db.execute('PRAGMA user_version=10')


def validate(data):
    if not isinstance(data,dict) or set(data)!={'name','interval','enabled','tool','arguments','path','internet'}:
        raise ValueError('Invalid schedule settings')
    if not isinstance(data['name'],str) or not re.fullmatch('[a-z][a-z0-9-]{0,47}',data['name']): raise ValueError('Use a lowercase schedule name')
    if type(data['interval']) is not int or not 1<=data['interval']<=1440: raise ValueError('Interval must be 1–1440 minutes')
    if type(data['enabled']) is not bool: raise ValueError('Invalid schedule state')
    payload=validate_content('tool',{k:data[k] for k in ('tool','arguments','path','internet')})
    if not payload['arguments'].strip(): raise ValueError('Supply a command or PHP script')
    return payload


def list_schedules(ledger,site_id):
    ledger.get(site_id)
    with ledger.db() as db:
        rows=[dict(r) for r in db.execute('SELECT s.*,j.state AS last_state,j.updated AS last_updated FROM schedules s LEFT JOIN content_jobs j ON j.id=s.last_job WHERE s.site_id=? ORDER BY s.name',(site_id,))]
    for row in rows: row['settings']=json.loads(row.pop('payload'))
    return rows


def export(ledger,site_id):
    from .host import OPS,atomic,trusted
    root=OPS/'panel/worker/schedules'; root.mkdir(mode=0o700,exist_ok=True); trusted(root,directory=True)
    records=[{k:r[k] for k in ('name','interval','enabled','settings')} for r in list_schedules(ledger,site_id)]
    atomic(root/(site_id+'.json'),json.dumps(records,indent=2))


def save(ledger,site_id,data):
    payload=validate(data); row=ledger.get(site_id)
    if row['state']!='succeeded': raise ValueError('Finish site setup first')
    if payload['tool']!='shell' and json.loads(row['payload']).get('runtime')!='php': raise ValueError('This command needs a PHP site')
    with ledger.db() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT * FROM schedules WHERE site_id=? AND name=?',(site_id,data['name'])).fetchone()
        if not old and db.execute('SELECT count(*) FROM schedules WHERE site_id=?',(site_id,)).fetchone()[0]>=16: raise ValueError('At most 16 schedules per site')
        last=old['last_job'] if old else None
        previous=db.execute('SELECT state FROM content_jobs WHERE id=?',(last,)).fetchone()
        if previous and previous['state']=='failed': last=None
        db.execute('INSERT OR REPLACE INTO schedules VALUES (?,?,?,?,?,?,?,?)',
            (site_id,data['name'],data['interval'],json.dumps(payload,sort_keys=True),int(data['enabled']),time.time()+data['interval']*60,last,''))
    export(ledger,site_id)
    return list_schedules(ledger,site_id)


def tick(ledger,now=None):
    now=time.time() if now is None else now
    with ledger.db() as db:
        db.execute('BEGIN IMMEDIATE')
        for item in db.execute('SELECT * FROM schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run',(now,)).fetchall():
            # A failed run pauses this schedule until the operator explicitly saves it again.
            # Editing a schedule clears its error but does not discard a running invocation.
            previous=db.execute('SELECT state FROM content_jobs WHERE id=?',(item['last_job'],)).fetchone()
            if previous and previous['state'] in ('queued','running','recovery-needed'): continue
            if previous and previous['state']=='failed' and not item['error']:
                db.execute("UPDATE schedules SET enabled=0,error='Previous run failed; review its output before resuming' WHERE site_id=? AND name=?",(item['site_id'],item['name']))
                continue
            try: ledger.content_ready(db,item['site_id'])
            except ValueError: continue
            ident=str(uuid.uuid4())
            db.execute("INSERT INTO content_jobs VALUES (?,?,?,?,'queued',?,'',?,?)",(ident,item['site_id'],'tool',item['payload'],'scheduled: '+item['name'],now,now))
            db.execute('INSERT INTO schedule_runs VALUES (?,?,?)',(item['site_id'],item['name'],ident))
            db.execute('UPDATE schedules SET next_run=?,last_job=?,error=? WHERE site_id=? AND name=?',(now+item['interval']*60,ident,'',item['site_id'],item['name']))


def prune(ledger):
    from .content_site import OUTPUT
    removed=[]
    with ledger.db() as db:
        for item in db.execute('SELECT site_id,name FROM schedules').fetchall():
            rows=db.execute("SELECT r.job_id FROM schedule_runs r JOIN content_jobs j ON j.id=r.job_id WHERE r.site_id=? AND r.name=? AND j.state IN ('succeeded','failed') ORDER BY j.created DESC LIMIT -1 OFFSET 20",(item['site_id'],item['name'])).fetchall()
            for row in rows:
                db.execute('DELETE FROM schedule_runs WHERE job_id=?',(row['job_id'],)); db.execute('DELETE FROM content_jobs WHERE id=?',(row['job_id'],)); removed.append(row['job_id'])
    for ident in removed: (OUTPUT/(ident+'.txt')).unlink(missing_ok=True)
