"""Daily housekeeping: the panel's own output folders stay bounded by age.

Everything else on the box is bounded where it is written: container logs by the local driver's
size caps, the journal by journald's limits, the edge's access logs by Caddy's rolling, the mail
relay's log by rotation in the spool, backups by the retention policy, site content by quotas.
The panel's own job outputs, backup logs, rebuild reports, download exports and fetch scratch
are the folders that only grow, so they are pruned here once a day and on request.
"""
import json
import shutil
import sys
import time

from .host import BACKUPS, OPS, atomic, trusted

STATE = OPS / 'panel/worker/housekeeping.json'
RULES = [  # folder, keep for (seconds), keep these names whatever their age, owned by the web user
    (OPS / 'panel/worker/content-output', 90 * 86400, (), False),
    (OPS / 'panel/worker/site-backup-logs', 90 * 86400, (), False),
    (OPS / 'panel/worker/php-rebuilds', 365 * 86400, ('latest.json', 'schedule.json'), False),
    (OPS / 'panel/web/downloads', 6 * 3600, (), True),
]
SCRATCH = [BACKUPS / 'staging/restore-fetch']


def prune(now=None, log=None):
    """Remove aged entries under each rule's folder; a folder that does not exist is nothing to do."""
    now = now or time.time()
    log = log or (lambda text: print(text, file=sys.stderr, flush=True))
    report = {}
    from .core import request_id
    for folder, keep, protected, web_owned in RULES:
        if not folder.is_dir() or folder.is_symlink(): continue
        if not web_owned: trusted(folder, directory=True)
        removed = 0
        for entry in folder.iterdir():
            if entry.name in protected or entry.is_symlink(): continue
            if web_owned:
                # The web process owns this folder; only export tokens are removed, as export() itself does.
                try: request_id(entry.name)
                except ValueError: continue
                if not entry.is_dir(): continue
            if now - entry.lstat().st_mtime <= keep: continue
            if entry.is_dir(): shutil.rmtree(entry)
            else: entry.unlink()
            removed += 1
        report[str(folder)] = removed
    for folder in SCRATCH:
        if folder.is_dir() and not folder.is_symlink() and now - folder.lstat().st_mtime > 86400:
            trusted(folder, directory=True); shutil.rmtree(folder); report[str(folder)] = 1
    STATE.parent.mkdir(mode=0o700, exist_ok=True); trusted(STATE.parent, directory=True)
    atomic(STATE, json.dumps({'last_run': now, 'removed': report}))
    if any(report.values()): log('housekeeping removed ' + ', '.join(f'{v} from {k}' for k, v in report.items() if v))
    return report


def due(now=None):
    now = now or time.time()
    if not STATE.exists(): return True
    trusted(STATE)
    return now - json.loads(STATE.read_text()).get('last_run', 0) >= 86400
