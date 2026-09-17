#!/bin/sh
# Restore selected complete site backups from the off-machine restic repository into this fresh
# panel. Run as root after bootstrap.sh. Repository access must already be in
# /srv/ops/panel/worker/remote-backup.json with its private files (restic password, SSH key,
# pinned known_hosts); this script never prints them.
#
#   recover-sites.sh "SNAPSHOT_ID=name=hostname[,alias,...]" ...
#
# Each argument names a site backup id from the original panel, the new site name and its
# hostnames: the primary first, then any aliases. A restore is always a new site with one
# hostname; the full list is applied afterwards as an ordinary domains job, because the backup's
# aliases belong to the old identity and are never restored by themselves.
set -eu
log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
PY=/opt/reeve/current/.venv/bin/python
cd /opt/reeve/current
for spec in "$@"; do
    backup=${spec%%=*}; rest=${spec#*=}; name=${rest%%=*}; hostnames=${rest#*=}; domain=${hostnames%%,*}
    log "fetching site backup $backup"
    $PY - "$backup" <<'PYEOF'
import json, sys
from reeve import remote_backup as remote, site_backup as sites
import shutil, subprocess
config = remote.settings()
backup = sys.argv[1]
target = sites.STAGING.parent / 'restore-fetch'
if sites.artifact_path(backup).is_dir():
    # Already fetched by an earlier run (or a retry): snapshot() below verifies its checksums.
    print(json.dumps({'backup': backup, 'fetched': 'already in staging'}))
else:
    snapshots = json.loads(remote.execute(config, ['snapshots', '--json', '--tag', 'hosting-site:' + backup, '--latest', '1']))
    assert len(snapshots) == 1, 'site backup not found in the repository'
    shutil.rmtree(target, ignore_errors=True); target.mkdir(mode=0o700)
    # A restore writes whole archives, so it runs outside the uploader's output-size guard.
    args, env = remote.command(config)
    subprocess.run([*args, 'restore', snapshots[0]['id'], '--target', str(target), '--verify'], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
    fetched = target / str(sites.artifact_path(backup)).lstrip('/')
    sites.STAGING.mkdir(mode=0o700, exist_ok=True)
    fetched.rename(sites.artifact_path(backup))
    shutil.rmtree(target, ignore_errors=True)
root, manifest = sites.snapshot(backup)
print(json.dumps({'backup': backup, 'site': manifest['site_name'], 'kind': manifest.get('site_kind'), 'bytes': manifest['files']['bytes']}))
PYEOF
    log "restoring $name ($domain)"
    reeve site-restore "$backup" --name "$name" --domain "$domain" >/dev/null
done
log "waiting for sites"
for spec in "$@"; do
    rest=${spec#*=}; name=${rest%%=*}
    for i in $(seq 1 400); do
        state=$(reeve list | $PY -c "import json,sys; r=[x for x in json.load(sys.stdin) if x['name']=='$name']; print(r[0]['state'] if r else 'missing')")
        case "$state" in succeeded|failed|recovery-needed) break;; esac
        sleep 3
    done
    phase=$(reeve site-restores "$(reeve list | $PY -c "import json,sys; print(next(x['id'] for x in json.load(sys.stdin) if x['name']=='$name'))")" 2>/dev/null | $PY -c "import json,sys; d=json.load(sys.stdin); print(d[0]['state'] if d else 'none')" || echo none)
    if [ "$phase" != none ]; then
        for i in $(seq 1 400); do
            phase=$(reeve site-restores "$(reeve list | $PY -c "import json,sys; print(next(x['id'] for x in json.load(sys.stdin) if x['name']=='$name'))")" | $PY -c "import json,sys; print(json.load(sys.stdin)[0]['state'])")
            case "$phase" in succeeded|failed|recovery-needed) break;; esac
            sleep 3
        done
    fi
    log "$name: site $state, restore phase $phase"
    hostnames=${rest#*=}
    if [ "$state" = succeeded ] && [ "$hostnames" != "${hostnames%%,*}" ]; then
        site=$(reeve list | $PY -c "import json,sys; print(next(x['id'] for x in json.load(sys.stdin) if x['name']=='$name'))")
        reeve domains "$site" $(printf '%s' "$hostnames" | tr ',' ' ') >/dev/null
        for i in $(seq 1 100); do
            job=$(reeve list | $PY -c "import json,sys; j=next(x for x in json.load(sys.stdin) if x['name']=='$name')['domain_job']; print(j['state'] if j else 'none')")
            case "$job" in succeeded|failed|none) break;; esac
            sleep 3
        done
        log "$name: hostnames $(printf '%s' "$hostnames" | tr ',' ' '), domains job $job"
    fi
done
