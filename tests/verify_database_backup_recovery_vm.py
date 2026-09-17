"""Synthetic stranded helper across a real worker restart; no application service stops."""
import json
import time
import uuid
from pathlib import Path

from reeve.core import Ledger
from reeve.database_site import state
from reeve.database_backup import artifact_path, helper_name, completed
from reeve.host import Host, atomic, command
from reeve.worker import rpc


def main():
    ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    row = next(r for r in ledger.list() if r['name'] == 'm25-postgres')
    info = state(row); ident = str(uuid.uuid4())
    old = rpc({'op': 'backup-status', 'site_id': row['id']})['last_success']
    manifest = completed(old)
    deadline = time.monotonic() + 90
    while ledger.backup_jobs(active=True) or any(j['state'] in ('queued', 'running') for j in ledger.content_jobs()):
        assert time.monotonic() < deadline, 'Worker did not become idle'
        time.sleep(0.25)
    command(['systemctl', 'stop', 'reeve-worker'])
    try:
        with ledger.db() as db:
            now = time.time()
            db.execute("INSERT INTO backup_jobs VALUES (?,?,'running','','','',?,?)", (ident, row['id'], now, now))
        partial = artifact_path(ident, partial=True); partial.mkdir(mode=0o700)
        atomic(partial / 'database.dump', 'incomplete synthetic dump')
        command(['docker', 'run', '-d', '--name', helper_name(ident), '--label', 'hosting.backup=' + ident,
                 '--network', 'none', '--user', f"{info['uid']}:{info['gid']}", '--read-only', '--cap-drop', 'ALL',
                 '--security-opt', 'no-new-privileges:true', '--log-driver', 'none',
                 '--entrypoint', 'sleep', info['image_id'], '120'])
        assert Host().inspect(helper_name(ident))['State']['Running']
    finally: command(['systemctl', 'start', 'reeve-worker'])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = next(j for j in ledger.backup_jobs() if j['id'] == ident)
        if result['state'] == 'failed': break
        time.sleep(0.25)
    assert result['state'] == 'failed' and not result['artifact'] and not result['cleanup_error']
    assert Host().inspect(helper_name(ident)) is None and not partial.exists()
    assert completed(old) == manifest
    report = {'stranded_job': ident, 'previous_dump': old['id'], 'helper_removed': True,
              'partial_discarded': True, 'previous_dump_retained': True, 'worker_restarted': True,
              'application_services_stopped': False, 'fault': 'Synthetic sleeping helper and partial dump; not an interrupted real SQL stream'}
    atomic(Path('/srv/ops/panel/worker/db-dump-acceptance/recovery.json'), json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__': main()
