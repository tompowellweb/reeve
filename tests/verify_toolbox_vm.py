"""Read-only runtime/retention checks for the named toolbox fixture."""
import argparse
import json
from pathlib import Path

from reeve.core import Ledger
from reeve.host import Host, atomic, command, project_id, quota_record
from reeve.worker import rpc
from reeve.toolbox import status


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('mode',choices=('active','stopped','final'))
    args=parser.parse_args()
    ledger=Ledger('/srv/ops/panel/worker/jobs.sqlite3'); host=Host()
    row=next(r for r in ledger.list() if r['name']=='m2-db-postgres')
    saved=status(ledger,host,row)
    path=Path('/srv/ops/panel/worker/toolbox-acceptance.json')
    report=json.loads(path.read_text()) if path.exists() else {}
    if args.mode=='active':
        assert saved['state']=='active' and saved['container']=='running'
        live=host.inspect('hosting-toolbox-'+row['name'])
        assert live['Config']['User']==f"{row['uid']}:{row['uid']}"
        assert not live['HostConfig']['Privileged'] and live['HostConfig']['ReadonlyRootfs']
        assert live['HostConfig']['Memory']==0 and live['HostConfig']['NanoCpus']==0
        assert command(['docker','exec',live['Id'],'cat','/sys/fs/cgroup/pids.max']).strip()=='max'
        assert all(m['Type']=='bind' for m in live['Mounts'])
        assert not any('/database/' in m['Source'] or 'docker.sock' in m['Source'] for m in live['Mounts'])
        assert live['HostConfig']['PortBindings']['2222/tcp'][0]['HostIp']=='127.0.0.1'
        for name in ('home','tmp'):
            directory=Path('/srv/sites')/row['name']/'toolbox'/name
            assert directory.stat().st_uid==row['uid'] and project_id(directory)[0]==row['project']
        report['active']={'state':saved,'uid':row['uid'],'image_id':live['Image'],'memory':0,'cpu':0,'pids':'max',
                          'mounts':[{'source':m['Source'],'destination':m['Destination'],'rw':m['RW']} for m in live['Mounts']],
                          'networks':list(live['NetworkSettings']['Networks'])}
        print(json.dumps(saved['details']))
    else:
        if args.mode=='stopped':
            assert saved['state']=='stopped' and saved['container']=='absent'
            assert not command(['docker','network','ls','-q','--filter','name=^hosting-toolbox-net-'+row['name']+'$']).strip()
        theme=Path('/srv/sites')/row['name']/'html/wp-content/themes/odd-layout'
        assert (theme/'ssh-proof.txt').read_text()=='written through the toolbox SSH session\n'
        assert (theme/'vendor/autoload.php').exists()
        assert {p.lstat().st_uid for p in theme.rglob('*')}=={row['uid']}
        from tests.verify_databases_vm import info
        sites=rpc({'op':'list'})
        assert len(sites)==19 and all(s['health']['application']=='healthy' for s in sites)
        databases={s['name']:info(s['name']) for s in sites if s['database']}
        assert not any(j['state'] in ('queued','running','recovery-needed') for j in ledger.content_jobs())
        report[args.mode]={'sites':len(sites),'healthy':True,'databases':databases,'theme_ownership':row['uid'],
                          'state':saved['state'],'container':saved['container'],'quota':quota_record(row['project'])}
        print(json.dumps({args.mode:'passed'}))
    atomic(path,json.dumps(report,indent=2))


if __name__=='__main__': main()
