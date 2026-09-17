"""Synthetic SQL/ownership/quota/recovery acceptance; never export credentials."""
import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path

from reeve.core import Ledger
from reeve.host import Host, SITES, command, atomic, project_id, quota_record
from reeve.worker import rpc
from reeve.database_site import state, paths
from tests.verify_php_vm import request

RESULT=Path('/srv/ops/panel/worker/database-acceptance.json')
PHP='''<?php
header('Content-Type: application/json');
$engine=getenv('DATABASE_ENGINE');
$dsn=($engine==='postgres'?'pgsql':'mysql').':host='.getenv('DATABASE_HOST').';port='.getenv('DATABASE_PORT').';dbname='.getenv('DATABASE_NAME');
$db=new PDO($dsn,getenv('DATABASE_USER'),getenv('DATABASE_PASSWORD'),array(PDO::ATTR_ERRMODE=>PDO::ERRMODE_EXCEPTION));
if(PHP_SAPI==='cli' && isset($argv[1]) && $argv[1]==='seed') {
 $db->exec('CREATE TABLE IF NOT EXISTS acceptance_records (id INTEGER PRIMARY KEY, payload VARCHAR(64) NOT NULL)');
 if((int)$db->query('SELECT count(*) FROM acceptance_records')->fetchColumn()===0) {
  $db->beginTransaction();$stmt=$db->prepare('INSERT INTO acceptance_records(id,payload) VALUES (?,?)');
  for($i=1;$i<=200;$i++)$stmt->execute(array($i,'record-'.$i));$db->commit();
 }
}
$rows=$db->query('SELECT id,payload FROM acceptance_records ORDER BY id')->fetchAll(PDO::FETCH_NUM);
foreach($rows as &$row)$row[0]=(int)$row[0];unset($row);
echo json_encode(array('version'=>$db->query('SELECT VERSION()')->fetchColumn(),'user'=>$db->query('SELECT CURRENT_USER')->fetchColumn(),
 'uid'=>posix_geteuid(),'count'=>count($rows),'export_sha256'=>hash('sha256',json_encode($rows))));
'''


def record(key,value):
    data=json.loads(RESULT.read_text()) if RESULT.exists() else {}
    data[key]=value;atomic(RESULT,json.dumps(data,indent=2));print(key+': '+json.dumps(value),flush=True)


def site(name): return next(r for r in rpc({'op':'list'}) if r['name']==name)


def info(name):
    row=site(name);code,body=request(row,'/database-proof.php');assert code==200,(code,body)
    result=json.loads(body)
    expected=hashlib.sha256(json.dumps([[i,'record-'+str(i)] for i in range(1,201)],separators=(',',':')).encode()).hexdigest()
    assert result['count']==200 and result['export_sha256']==expected and result['uid']==row['uid'],result
    assert result['user'].startswith('site'),result
    assert state(row)['version'] in result['version'],result
    return result


def prepare(name):
    row=site(name)
    command(['setpriv',f"--reuid={row['uid']}",f"--regid={row['uid']}",'--clear-groups','python3','-c',
             'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])',SITES/name/'html/database-proof.php',PHP])
    command(['docker','exec','hosting-php-'+name,'php','/site/database-proof.php','seed'])
    result=info(name)
    db=state(row);live=Host().inspect('hosting-db-'+name)
    data=paths(row)[0]/'data';project,_=project_id(data)
    assert project==row['project'] and data.stat().st_uid==db['uid'] and db['uid']!=row['uid']
    assert not live['HostConfig']['PortBindings'] and all(m['Type']=='bind' for m in live['Mounts'])
    raw=json.dumps(rpc({'op':'list'}))
    assert db['admin_password'] not in raw and db['app_password'] not in raw
    record('prepared-'+name,{'site_id':row['id'],'db':row['database'],'sql':result,'data_uid':data.stat().st_uid,
        'site_uid':row['uid'],'quota_project':project,'image_id':live['Image'],'ports':'unpublished','secrets':'absent from routine API'})


def recreate(name):
    row=site(name);before=info(name);db=state(row)
    command(['docker','compose','-f',paths(row)[0]/'compose.yml','up','-d','--force-recreate','--wait','--wait-timeout','90'],timeout=150)
    command(['docker','update','--pids-limit','-1','hosting-db-'+name])
    assert info(name)==before
    assert Host().inspect('hosting-db-'+name)['Image']==db['image_id']
    record('recreated-'+name,{'200_records_preserved':True,'exact_image_preserved':db['image_id'],'credentials_preserved':True})


def quota(name):
    import subprocess
    row=site(name);db=state(row);data=paths(row)[0]/'data';file=data/'.quota-acceptance'
    before=info(name)
    command(['docker','stop','hosting-db-'+name])
    try:
        result=subprocess.run(['docker','run','--rm','--network','none','--user',f"{db['uid']}:{db['gid']}",'--cap-drop','ALL','--memory','64m','--volume',f"{data}:{db['mount']}",'--entrypoint','dd',db['image'],'if=/dev/zero',f"of={db['mount']}/.quota-acceptance",'bs=1M','count=1100','conv=fsync'],capture_output=True,text=True)
        assert result.returncode and any(error in result.stderr for error in ('Disk quota exceeded','No space left on device')),result.stderr
        import os
        free=os.statvfs('/srv')
        assert free.f_bavail*free.f_frsize > 1024**3
        assert quota_record(row['project'])['used_bytes'] >= quota_record(row['project'])['hard_bytes'] - 2*1024**2
        assert info('m2-db-postgres')['count']==200
        detail={'refused_write':result.stderr.splitlines()[0],'bytes_written':file.stat().st_size,'quota':quota_record(row['project']),'neighbour':'PostgreSQL still queried 200 records'}
    finally:
        if file.exists():file.unlink()
        command(['docker','start','hosting-db-'+name])
    for _ in range(120):
        try:
            assert info(name)==before;break
        except Exception:time.sleep(0.5)
    else:raise AssertionError('Database did not recover after quota fixture')
    record('quota-'+name,detail)


def ready():
    for _ in range(100):
        try:rpc({'op':'defaults'});return
        except (OSError,ValueError):time.sleep(0.1)
    raise AssertionError('Worker unavailable')


def wait(ident,wanted):
    ledger=Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    for _ in range(1800):
        job=next(j for j in ledger.database_jobs() if j['id']==ident)
        if job['state']==wanted:return job
        if wanted=='succeeded' and job['state']=='recovery-needed':raise AssertionError(job)
        time.sleep(0.5)
    raise AssertionError(job)


def interruption(name):
    row=site(name);job=rpc({'op':'add-database','id':str(uuid.uuid4()),'site_id':row['id'],'data':{'engine':'mariadb','series':'10.11'}})
    ledger=Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    for _ in range(90000):
        observed=next(j for j in ledger.database_jobs() if j['id']==job['id'])
        if observed['step']=='connecting site to database':
            saved=state(row);assert saved['stage']=='initializing'
            command(['systemctl','kill','--signal=KILL','reeve-worker']);break
        assert observed['state'] not in ('succeeded','recovery-needed'),observed
        time.sleep(0.01)
    else:raise AssertionError('Did not reach interruption point')
    wait(job['id'],'recovery-needed');ready();rpc({'op':'retry-database','id':job['id']});wait(job['id'],'succeeded')
    after=state(row)
    assert all(after[k]==saved[k] for k in ('image_id','admin_password','app_password','uid','gid'))
    record('interruption-'+name,{'job':job['id'],'step':observed['step'],'retry':'succeeded; same image, identity and passwords'})
    prepare(name)


def integration(name=None):
    from reeve.database_versions import read as catalogue
    from tests.verify_versions_vm import wait as wait_runtime
    row=site('m2-db-postgres');before=info(row['name']);previous_branch=row['php_branch']
    change=rpc({'op':'php-switch','id':str(uuid.uuid4()),'site_id':row['id'],'branch':'8.3'})
    wait_runtime(change['id'],'succeeded');assert info(row['name'])==before
    rollback=rpc({'op':'php-rollback','id':str(uuid.uuid4()),'site_id':row['id'],'previous':change['id']})
    wait_runtime(rollback['id'],'succeeded');assert info(row['name'])==before and site(row['name'])['php_branch']==previous_branch
    record('php_switch_with_database',{'switch':change['id'],'rollback':rollback['id'],'result':'DB credentials and 200 records preserved across 8.5 -> 8.3 -> 8.5'})
    mysql=Host().inspect('hosting-db-m2-db-mysql')
    network=mysql['NetworkSettings']['Networks']['hosting-backend-m2-db-mysql']
    probe='$c=@fsockopen("'+network['IPAddress']+'",3306,$e,$s,2); echo $c ? "connected" : "blocked";'
    assert command(['docker','exec','hosting-php-m2-db-postgres','php','-r',probe]).strip()=='blocked'
    record('network_isolation',{'cross_site_mysql_connection':'blocked','DB_networks':'internal, no published ports'})
    images={r['id']:state(r)['image_id'] for r in rpc({'op':'list'}) if r['database']}
    checked=catalogue()['checked_at']
    job=rpc({'op':'refresh-databases','id':str(uuid.uuid4())});wait(job['id'],'succeeded')
    assert catalogue()['checked_at']>checked
    assert images=={r['id']:state(r)['image_id'] for r in rpc({'op':'list'}) if r['database']}
    record('catalogue_refresh',{'job':job['id'],'running_images':'unchanged','catalogue':catalogue()})


def final(name=None):
    rows=rpc({'op':'list'});ledger=Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    assert all(r['state']=='succeeded' and r['health']['application']=='healthy' for r in rows)
    assert all(r['database']['health']=='healthy' for r in rows if r['database'])
    assert all(j['state']=='succeeded' for j in ledger.database_jobs())
    record('final',{'sites':rows,'database_jobs':ledger.database_jobs(),
        'release':json.loads(Path('/srv/ops/panel/release.json').read_text()),
        'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'failed_units':command(['systemctl','--failed','--no-legend']),
        'services':command(['systemctl','is-active','reeve-web','reeve-worker'])})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['prepare','recreate','quota','interruption','integration','final']);p.add_argument('name',nargs='?')
    a=p.parse_args();globals()[a.mode](a.name)
