import json, subprocess, time
from pathlib import Path
from reeve.core import Ledger
from reeve.host import Host, atomic, command
from reeve.toolbox import status
from reeve.worker import rpc
ledger=Ledger('/srv/ops/panel/worker/jobs.sqlite3'); host=Host()
path=Path('/srv/ops/panel/worker/toolbox-acceptance.json'); report=json.loads(path.read_text())
row=next(r for r in ledger.list() if r['name']=='m2-db-postgres')
saved=status(ledger,host,row); previous=report['active']['state']['details']
assert saved['state']=='active'
for field in ('image_id','host_public_key','recipe_sha256'):
    assert saved['details'][field]==previous[field]
assert saved['details']['path']=='wp-content/themes/odd-layout'
container=host.inspect('hosting-toolbox-'+row['name'])['Id']
command(['systemctl','restart','reeve-worker'])
for attempt in range(40):
    try:
        rpc({'op':'list'}); break
    except (OSError,RuntimeError): time.sleep(.5)
else: raise AssertionError('Worker did not restart')
assert status(ledger,host,row)['state']=='active'
assert host.inspect('hosting-toolbox-'+row['name'])['Id']==container
report['restart']={'same_image':True,'same_host_key':True,'worker_restart_preserves_container':True,'details':saved['details']}
atomic(path,json.dumps(report,indent=2))
print(json.dumps(saved['details']))
