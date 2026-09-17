"""Panel updates: the newest release upstream is known and shown; only the operator applies it.

Once a day the worker lists the tags of the repository the panel was installed from and records
the newest release version beside the installed one. The home page and the PHP page say "Reeve
X available". `reeve update [--to X]` fetches that tag into `/opt/reeve/src` and runs the
installer from it, with the same schema check and rollback as any install; the panel never
updates itself unasked, for the same reason it never changes a site's PHP branch unasked.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from .host import OPS, atomic, command, trusted

RECORD = OPS / 'panel/release.json'
STATE = OPS / 'panel/worker/update-check.json'
SRC = Path('/opt/reeve/src')
DEFAULT_SOURCE = 'https://github.com/tompowellweb/reeve.git'
TAG = re.compile(r'refs/tags/v(\d+\.\d+\.\d+)$')


def parse(version):
    """A release version as a comparable tuple; None for anything that is not one."""
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', str(version or ''))
    return tuple(int(x) for x in match.groups()) if match else None


def installed():
    """What runs: version, commit, whether modified, from the installer's record."""
    if not RECORD.exists(): return {'version': None, 'current': None, 'modified': False, 'source': None}
    try: entry = json.loads(RECORD.read_text())
    except (OSError, ValueError): return {'version': None, 'current': None, 'modified': False, 'source': None}
    return {'version': entry.get('version'), 'current': entry.get('current'), 'modified': bool(entry.get('modified')), 'source': entry.get('source'),
            'previous_version': entry.get('previous_version')}


def source():
    return installed().get('source') or DEFAULT_SOURCE


def newest(listing):
    """The highest release tag in a `git ls-remote --tags` listing."""
    versions = [m.group(1) for line in listing.splitlines() for m in [TAG.search(line.strip())] if m]
    return max(versions, key=parse, default=None)


def check(now=None):
    """Ask the repository for its tags and record what is newest against what is installed."""
    now = now or time.time()
    here = installed()
    try:
        latest = newest(command(['git', 'ls-remote', '--tags', source()], timeout=60))
        error = ''
    except RuntimeError as exc:
        latest, error = None, str(exc)[:300]
    mine = parse(here['version'])
    state = {'checked_at': now, 'latest': latest, 'installed': here['version'], 'source': source(),
             'available': bool(latest and (mine is None or parse(latest) > mine)), 'error': error}
    STATE.parent.mkdir(mode=0o700, exist_ok=True)
    atomic(STATE, json.dumps(state))
    return state


def due(now=None):
    now = now or time.time()
    if not STATE.exists(): return True
    try:
        trusted(STATE); return now - json.loads(STATE.read_text()).get('checked_at', 0) >= 86400
    except (OSError, ValueError):
        return True


def state():
    """The last check, for the pages; None before the first."""
    if not STATE.exists(): return None
    try:
        trusted(STATE); return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None


def status():
    """What the pages show: the installed release and the last check, with availability judged against what runs now."""
    here = installed(); last = state() or {}
    latest = last.get('latest'); mine = parse(here.get('version'))
    return {**here, 'latest': latest, 'checked_at': last.get('checked_at'), 'error': last.get('error', ''),
            'available': bool(latest and (mine is None or parse(latest) > mine))}


def apply(version=None, log=None):
    """Fetch the release into the panel's own clone and run its installer. Root only; the installer restarts the services."""
    log = log or (lambda text: print(text, flush=True))
    url = source()
    if not SRC.exists():
        SRC.parent.mkdir(parents=True, exist_ok=True)
        command(['git', 'clone', '--quiet', url, str(SRC)], timeout=300)
    command(['git', '-C', str(SRC), 'fetch', '--quiet', '--tags', url], timeout=300)
    target = version or newest(command(['git', '-C', str(SRC), 'ls-remote', '--tags', url], timeout=60))
    if not target or not parse(target): raise ValueError('No release to update to')
    tag = 'v' + str(target).lstrip('v')
    command(['git', '-C', str(SRC), 'checkout', '--quiet', '--force', tag])
    log(f'Installing Reeve {tag[1:]} from {url}')
    result = subprocess.run([sys.executable, str(SRC / 'install.py')], cwd=str(SRC))
    if result.returncode != 0: raise RuntimeError('The installer failed; the running release is unchanged unless it said otherwise')
    return {'installed': tag[1:], 'from': url}
