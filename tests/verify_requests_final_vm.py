"""Final M2.4 retention and cron evidence; all site HTTP requests enter the Caddy edge."""
import json,time
from pathlib import Path
from reeve.core import Ledger
from reeve.host import Host,atomic,command,quota_record
from reeve.worker import rpc
from reeve.schedules import list_schedules
from tests.verify_php_vm import request
from tests.verify_databases_vm import info

ledger=Ledger('/srv/ops/panel/worker/jobs.sqlite3'); host=Host()
with ledger.db() as db: assert db.execute('PRAGMA user_version').fetchone()[0]==10
rows=rpc({'op':'list'}); assert len(rows)==20
assert all(r['health']['application']=='healthy' for r in rows),[(r['name'],r['health']) for r in rows]
https={}
for r in rows:
    status,_=request(r)
    assert status in (200,301,302,308),(r['name'],status)
    https[r['name']]=status
original={r['name']:info(r['name']) for r in rows if r['database'] and r['name']!='m24-wordpress'}
assert len(original)==5
wp=next(r for r in rows if r['name']=='m24-wordpress')
report=json.loads(Path('/var/lib/hosting-browser/results/m24-wordpress-acceptance.json').read_text())
status,body=request(wp,'/wp-content/m24-cron-proof.json'); assert status==200
cron=json.loads(body)
assert cron['uid']==wp['uid'] and cron['time']>report['schedule_details']['page_closed_at'],cron
schedules=list_schedules(ledger,wp['id']); schedule=next(s for s in schedules if s['name']=='wordpress')
assert schedule['last_state']=='succeeded' and schedule['settings']['tool']=='wp',schedule
with ledger.db() as db:
    cron_jobs=[r[0] for r in db.execute("select job_id from schedule_runs where site_id=? and name='wordpress'",(wp['id'],))]
cron_job=next(ident for ident in cron_jobs if 'hosting_acceptance_cron' in (Path('/srv/ops/panel/worker/content-output')/(ident+'.txt')).read_text())
assert not any(j['state'] in ('queued','running','recovery-needed') for j in ledger.content_jobs())
assert not command(['docker','ps','-aq','--filter','label=hosting.content']).strip()
assert not command(['docker','network','ls','-q','--filter','name=hosting-tool-net-']).strip()
assert not host.inspect('hosting-toolbox-m24-wordpress')
for name in ('hosting-site-m24-wordpress','hosting-php-m24-wordpress','hosting-db-m24-wordpress'):
    live=host.inspect(name)
    assert live['HostConfig']['Memory']==0 and live['HostConfig']['NanoCpus']==0
    assert command(['docker','exec',name,'cat','/sys/fs/cgroup/pids.max']).strip()=='max'
result={'checked_at':time.time(),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'worker_schema':10,'installed':str(Path('/opt/reeve/current').resolve()),'sites':len(rows),'databases':sum(bool(r['database']) for r in rows),
        'all_healthy':True,'caddy_https_status':https,'original_databases':original,
        'cron':cron,'cron_job':cron_job,'page_closed_at':report['schedule_details']['page_closed_at'],'schedule':schedule,
        'site_quota':quota_record(wp['project']),'wordpress_caps_unlimited':True,'temporary_tools_cleaned':True}
atomic(Path('/srv/ops/panel/worker/requests-final.json'),json.dumps(result,indent=2))
print(json.dumps({'sites':len(rows),'databases':result['databases'],'cron':'passed without page requests','all_caddy_https':'passed'}))
