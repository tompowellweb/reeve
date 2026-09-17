"""The server's state for the home page: one summary a minute, written where the web process reads it.

The home page shows the box, not only its sites: load and pressure, memory, disk left with what
is reclaimable, the backup destination, the mail relay and customer SFTP, and per site the disk
used against its quota and the memory and CPU its containers hold. The worker gathers it once a
minute from what the kernel and Docker already know (three commands, a few files, no agent, no
exporter) and writes one world-readable JSON file that the web process reads directly, so the
home page stays current while the worker is busy with a long job, and it says how old its
figures are. Every section is gathered under its own guard: a source that fails is reported in
the file and the rest of the page still shows.
"""
import json
import os
import re
import time
from pathlib import Path

from .host import BACKUPS, OPS, atomic, command, trusted

STATUS = OPS / 'panel/status.json'
SAMPLE = OPS / 'panel/worker/server-status-sample.json'
CGROUPS = Path('/sys/fs/cgroup/system.slice')
PROC = Path('/proc')
UNITS = {'B': 1, 'kB': 10 ** 3, 'MB': 10 ** 6, 'GB': 10 ** 9, 'TB': 10 ** 12, 'KiB': 1024, 'MiB': 1024 ** 2, 'GiB': 1024 ** 3, 'TiB': 1024 ** 4}


def pressure(kind):
    """The 'some' share of the last minute a task waited on this resource, from the kernel's PSI."""
    text = (PROC / 'pressure' / kind).read_text().splitlines()[0]
    return float(re.search(r'avg60=([0-9.]+)', text).group(1))


def cpu():
    load = os.getloadavg()
    return {'cores': os.cpu_count() or 1, 'load': [round(x, 2) for x in load], 'pressure': pressure('cpu')}


def memory():
    values = {}
    for line in (PROC / 'meminfo').read_text().splitlines():
        key, _, rest = line.partition(':')
        values[key] = int(rest.split()[0]) * 1024
    return {'total': values['MemTotal'], 'available': values['MemAvailable'], 'used': values['MemTotal'] - values['MemAvailable'],
            'swap_used': values.get('SwapTotal', 0) - values.get('SwapFree', 0), 'pressure': pressure('memory')}


def filesystem(path):
    info = os.statvfs(path)
    return {'total': info.f_blocks * info.f_frsize, 'free': info.f_bavail * info.f_frsize}


def parse_size(text):
    """Docker's human sizes ('9.259GB', '1.225GB (15%)') as bytes."""
    m = re.match(r'([0-9.]+)\s*([kMGT]?i?B)', text.strip())
    if not m: raise ValueError('Unreadable size: ' + text)
    return int(float(m.group(1)) * UNITS[m.group(2)])


def docker_disk():
    """What Docker holds under /srv and what a prune would return."""
    result = {}
    for line in command(['docker', 'system', 'df', '--format', '{{.Type}}\t{{.Size}}\t{{.Reclaimable}}']).splitlines():
        kind, size, reclaimable = line.split('\t')
        result[kind.strip().lower().replace(' ', '_')] = {'size': parse_size(size), 'reclaimable': parse_size(reclaimable)}
    return result


def quotas():
    """Every XFS project's used and hard bytes from one report."""
    result = {}
    for line in command(['xfs_quota', '-x', '-c', 'report -p -n -b', '/srv']).splitlines():
        fields = line.split()
        if fields and fields[0].startswith('#') and fields[0][1:].isdigit() and len(fields) >= 4:
            result[int(fields[0][1:])] = {'used': int(fields[1]) * 1024, 'hard': int(fields[3]) * 1024}
    return result


def containers():
    """Every container's state and health from one listing."""
    result = []
    for line in command(['docker', 'ps', '-a', '--no-trunc', '--format', '{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}']).splitlines():
        ident, name, state, status = (line.split('\t') + ['', '', '', ''])[:4]
        health = 'healthy' if '(healthy)' in status else 'unhealthy' if '(unhealthy)' in status else 'starting' if '(health: starting)' in status else None
        result.append({'id': ident, 'name': name, 'state': state, 'health': health})
    return result


def cgroup(ident):
    """Memory held and CPU time consumed by one container, from its cgroup; None once it is gone.

    Memory is the cgroup's current charge less its inactive file cache, the figure `docker stats`
    shows, so the page agrees with what an operator checks it against."""
    scope = CGROUPS / f'docker-{ident}.scope'
    try:
        memory_now = int((scope / 'memory.current').read_text())
        stats = dict(l.split() for l in (scope / 'memory.stat').read_text().splitlines() if len(l.split()) == 2)
        usage = next(int(l.split()[1]) for l in (scope / 'cpu.stat').read_text().splitlines() if l.startswith('usage_usec '))
    except (OSError, StopIteration, ValueError):
        return None
    return {'memory': max(0, memory_now - int(stats.get('inactive_file', 0))), 'cpu_usec': usage}


def belongs(row, name):
    payload = json.loads(row['payload'])
    if payload.get('runtime') == 'compose': return name.startswith(payload.get('project_name', 'package-' + row['id']) + '-')
    return name in ('hosting-site-' + row['name'], 'hosting-php-' + row['name'], 'hosting-db-' + row['name'])


def application_health(owned):
    """One word for the site from its containers, as the site page's health reads."""
    if not owned: return 'absent'
    if any(c['state'] != 'running' for c in owned): return 'unhealthy'
    checks = [c['health'] for c in owned if c['health']]
    if 'unhealthy' in checks: return 'unhealthy'
    if 'starting' in checks: return 'starting'
    return 'healthy' if checks else 'running'


def site_figures(rows, listing, projects, previous, now, traffic=None):
    """Per site: health, disk used of quota, memory now, CPU as a share of one core over the last sample."""
    sample = {'at': now, 'cpu_usec': {}}
    sites = []
    for row in rows:
        owned = [c for c in listing if belongs(row, c['name'])]
        usage = [u for u in (cgroup(c['id']) for c in owned if c['state'] == 'running') if u]
        cpu_usec = sum(u['cpu_usec'] for u in usage)
        sample['cpu_usec'][row['id']] = cpu_usec
        share = None
        if previous and row['id'] in previous.get('cpu_usec', {}) and now > previous['at']:
            delta = cpu_usec - previous['cpu_usec'][row['id']]
            share = round(max(0.0, delta / ((now - previous['at']) * 1_000_000)), 3) if delta >= 0 else None
        quota = projects.get(row['project'])
        day = (traffic or {}).get(row['id'], {})
        sites.append({'requests_24h': day.get('requests', 0), 'errors_24h': day.get('server_errors', 0), 'bytes_24h': day.get('bytes', 0),
                      'id': row['id'], 'name': row['name'], 'domain': row['domain'], 'state': row['state'],
                      'runtime': json.loads(row['payload']).get('runtime', 'static'), 'health': application_health(owned),
                      'disk_used': quota['used'] if quota else None, 'disk_hard': quota['hard'] if quota else None,
                      'memory': sum(u['memory'] for u in usage) if usage else None, 'cpu': share, 'containers': len(owned)})
    return sites, sample


def backups_summary(ledger):
    from .remote_backup import status
    info = status(ledger, None)
    last = max([(info.get('last_copy') or {}).get('created') or 0, (info.get('last_site_copy') or {}).get('created') or 0]) or None
    return {'state': info['state'], 'type': info.get('type'), 'pending': info.get('pending', 0) + info.get('pending_sites', 0),
            'last_copy': last, 'error': info.get('error', '')}


def mail_summary(host):
    from .mail import status
    info = status(host)
    totals = {w: {k: sum(site[w][k] for site in info['sites']) for k in ('sent', 'deferred', 'bounced', 'limited')} for w in ('hour', 'day')} if info['sites'] else None
    return {'set_up': info['set_up'], 'running': info['running'], 'mode': info['mode'], 'queued': len(info['queue']),
            'hour': totals['hour'] if totals else None, 'day': totals['day'] if totals else None, 'error': info.get('error', '')}


def sftp_summary(listing):
    from .sftp import registry
    entries = registry()
    return {'on': sorted(e['name'] for e in entries.values()), 'running': any(c['name'] == 'hosting-sftp' and c['state'] == 'running' for c in listing)}


def previous_sample():
    if not SAMPLE.exists(): return None
    try:
        trusted(SAMPLE); return json.loads(SAMPLE.read_text())
    except (ValueError, OSError):
        return None


def summary(ledger, host, now=None):
    """Everything the home page shows, each part under its own guard."""
    now = now or time.time()
    started = time.monotonic()
    import socket
    from .profile import settings as profile_settings
    result = {'at': now, 'errors': {}, 'hostname': socket.gethostname()}
    def part(name, make):
        try: result[name] = make()
        except Exception as exc:
            result[name] = None; result['errors'][name] = str(exc)[:300]
    part('profile', profile_settings)
    from .updates import installed, state as update_state
    part('update', lambda: {**installed(), **(update_state() or {})})
    part('cpu', cpu)
    part('memory', memory)
    def disk():
        projects = quotas()
        rows = ledger.list()
        site_projects = {row['project'] for row in rows}
        backups_used = int(command(['du', '-sb', str(BACKUPS)]).split()[0]) if BACKUPS.exists() else 0
        return {'srv': filesystem(OPS.parent), 'root': filesystem('/'), 'sites_used': sum(q['used'] for p, q in projects.items() if p in site_projects),
                'backups_used': backups_used, 'docker': docker_disk()}
    part('disk', disk)
    listing = []
    def sites():
        nonlocal listing
        listing = containers()
        from .traffic import recent
        figures, sample = site_figures(ledger.list(), listing, quotas(), previous_sample(), now, recent(ledger, now))
        SAMPLE.parent.mkdir(mode=0o700, exist_ok=True)
        atomic(SAMPLE, json.dumps(sample))
        return figures
    part('sites', sites)
    part('backups', lambda: backups_summary(ledger))
    part('mail', lambda: mail_summary(host))
    part('sftp', lambda: sftp_summary(listing))
    result['took'] = round(time.monotonic() - started, 2)
    return result


def write(ledger, host, now=None):
    """The summary as a world-readable file beside the panel's release record."""
    result = summary(ledger, host, now)
    atomic(STATUS, json.dumps(result), 0o644)
    return result


def read(path=STATUS):
    """What the web process shows; None when no summary has been written yet or it is unreadable."""
    path = Path(path)
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except (OSError, ValueError):
        return None
