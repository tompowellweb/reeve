import json
from pathlib import Path
from reeve.core import Ledger
from reeve.host import Host, atomic, command
from reeve.toolbox import status
ledger=Ledger('/srv/ops/panel/worker/jobs.sqlite3'); host=Host()
row=next(r for r in ledger.list() if r['name']=='m2-versions')
saved=status(ledger,host,row); assert saved['state']=='active'
settings=json.loads(row['payload']); live=host.inspect('hosting-toolbox-'+row['name'])
assert live['HostConfig']['Memory']==settings['memory_mb']*1048576
assert live['HostConfig']['NanoCpus']==int((settings.get('cpus') or 0)*1000000000)
assert live['HostConfig']['StorageOpt']['size']==str(settings['layer_mb'])+'m'
assert command(['docker','exec',live['Id'],'cat','/sys/fs/cgroup/memory.max']).strip()==str(settings['memory_mb']*1048576)
assert command(['docker','exec',live['Id'],'cat','/sys/fs/cgroup/pids.max']).strip()==str(settings.get('pids_limit') or 'max')
assert live['Config']['User']==f"{row['uid']}:{row['uid']}"
path=Path('/srv/ops/panel/worker/toolbox-acceptance.json'); report=json.loads(path.read_text())
report['explicit_limits']={'site':row['name'],'uid':row['uid'],'memory_mb':settings['memory_mb'],'cpus':settings.get('cpus'),
                          'layer_mb':settings['layer_mb'],'pids_limit':settings.get('pids_limit'),'image_id':live['Image']}
atomic(path,json.dumps(report,indent=2)); print(json.dumps(report['explicit_limits']))
