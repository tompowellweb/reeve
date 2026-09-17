"""Traffic per site from the edge's access logs: hourly buckets, one mechanism, bounded.

Caddy writes one JSON line per request into a file per hostname under the edge's data folder
(rolled at 20 MiB, kept 30 days). Once a minute the worker reads each file from where it stopped
last time, remembering the inode and the byte offset per file: a changed inode or a shorter file
means Caddy rolled or recreated it, so the rest of the rolled file is read first and the new
file starts from the top. Only new lines are ever parsed, so a busy site costs the same per
minute whatever its log holds. Each request lands in an hourly bucket for its site, found from
the hostname through the site's domains: requests, bytes sent, 2xx/3xx, 4xx, 5xx and requests
slower than a second. Nothing per request is kept and no client address is stored, so the table
grows with time only (one row per site per hour, pruned after 30 days), and history survives an
edge recreate because it lives in the ledger, not in the log files. The panel's own health
probes are not counted. The home page sums the last day per site; the site page shows the day
by hour and the month by day.
"""
import json
import os
import re
import time

from .host import OPS, PROXY, atomic, trusted

LOGS = PROXY / 'data/logs'
OFFSETS = OPS / 'panel/worker/traffic-offsets.json'
KEEP = 30 * 86400
SLOW = 1.0
HEALTH = '/__hosting_health'
CHUNK = 16 * 1024 * 1024  # at most this much of one file per minute; the rest waits for the next
ROLLED = re.compile(r'-\d{4}-\d{2}-\d{2}T')
EMPTY = {'requests': 0, 'bytes': 0, 'ok': 0, 'client_errors': 0, 'server_errors': 0, 'slow': 0}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS traffic (
        site_id TEXT NOT NULL, hour INTEGER NOT NULL, requests INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0,
        ok INTEGER NOT NULL DEFAULT 0, client_errors INTEGER NOT NULL DEFAULT 0, server_errors INTEGER NOT NULL DEFAULT 0,
        slow INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(site_id, hour))''')


def hostnames(ledger):
    """Every hostname the edge routes, to its site."""
    with ledger.db() as db:
        result = {r[0]: r[1] for r in db.execute("SELECT domain, id FROM jobs WHERE state!='deleted'")}
        result.update({r[0]: r[1] for r in db.execute('SELECT domain, site_id FROM site_domains WHERE active=1')})
    return result


def parse(line):
    """One access log line as (timestamp, status, bytes, duration, uri); None for anything else."""
    try:
        entry = json.loads(line)
        if entry.get('msg') != 'handled request': return None
        return float(entry['ts']), int(entry['status']), int(entry.get('size', 0)), float(entry.get('duration', 0)), str(entry['request'].get('uri', ''))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def count(bucket, status, size, duration):
    bucket['requests'] += 1; bucket['bytes'] += size
    if status >= 500: bucket['server_errors'] += 1
    elif status >= 400: bucket['client_errors'] += 1
    else: bucket['ok'] += 1
    if duration >= SLOW: bucket['slow'] += 1


def saved_offsets():
    if not OFFSETS.exists(): return {}
    try:
        trusted(OFFSETS); return json.loads(OFFSETS.read_text())
    except (ValueError, OSError):
        return {}


def read_from(path, offset):
    """Whole lines from the offset, at most a chunk; returns the text and the offset after the last full line."""
    with open(path, 'rb') as stream:
        stream.seek(offset)
        data = stream.read(CHUNK)
    cut = data.rfind(b'\n')
    if cut < 0: return '', offset
    return data[:cut + 1].decode('utf-8', 'replace'), offset + cut + 1


def collect(hostnames, offsets, log):
    """New lines from every current log, with the tail of a rolled file read first; buckets and the new offsets."""
    buckets = {}
    report = {'files': 0, 'lines': 0, 'unknown': 0, 'skipped': 0}
    if not LOGS.is_dir(): return buckets, offsets, report
    by_inode = {}
    for path in LOGS.iterdir():
        if path.is_file() and not path.is_symlink(): by_inode[path.stat().st_ino] = path
    new_offsets = {}
    for path in sorted(LOGS.iterdir()):
        if not path.name.endswith('.log') or ROLLED.search(path.name) or path.is_symlink() or not path.is_file(): continue
        hostname = path.name[:-4]
        site = hostnames.get(hostname)
        info = path.stat()
        saved = offsets.get(path.name, {})
        texts = []
        if saved and saved.get('inode') != info.st_ino:
            rolled = by_inode.get(saved.get('inode'))
            if rolled is not None and rolled != path and rolled.stat().st_size >= saved.get('offset', 0):
                texts.append(read_from(rolled, saved['offset'])[0])  # what arrived before the roll
            start = 0
        else:
            start = saved.get('offset', 0) if saved.get('offset', 0) <= info.st_size else 0
        text, offset = read_from(path, start)
        texts.append(text)
        new_offsets[path.name] = {'inode': info.st_ino, 'offset': offset}
        report['files'] += 1
        for line in ''.join(texts).splitlines():
            if not line.strip(): continue
            parsed = parse(line)
            if parsed is None: report['skipped'] += 1; continue
            ts, status, size, duration, uri = parsed
            if uri.startswith(HEALTH): continue
            report['lines'] += 1
            if site is None: report['unknown'] += 1; continue
            bucket = buckets.setdefault((site, int(ts // 3600) * 3600), dict(EMPTY))
            count(bucket, status, size, duration)
    return buckets, new_offsets, report


def store(ledger, buckets):
    with ledger.db() as db:
        for (site, hour), b in buckets.items():
            db.execute('''INSERT INTO traffic (site_id, hour, requests, bytes, ok, client_errors, server_errors, slow) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(site_id, hour) DO UPDATE SET requests=requests+excluded.requests, bytes=bytes+excluded.bytes, ok=ok+excluded.ok,
                client_errors=client_errors+excluded.client_errors, server_errors=server_errors+excluded.server_errors, slow=slow+excluded.slow''',
                       (site, hour, b['requests'], b['bytes'], b['ok'], b['client_errors'], b['server_errors'], b['slow']))


def prune(ledger, now):
    with ledger.db() as db:
        db.execute('DELETE FROM traffic WHERE hour < ?', (int(now) - KEEP,))


def tick(ledger, now=None, log=None):
    """The minute's work: read what is new, add it to the buckets, remember where each file stands."""
    now = now or time.time()
    buckets, offsets, report = collect(hostnames(ledger), saved_offsets(), log)
    store(ledger, buckets)
    OFFSETS.parent.mkdir(mode=0o700, exist_ok=True)
    atomic(OFFSETS, json.dumps(offsets, sort_keys=True))
    if int(now) % 3600 < 60: prune(ledger, now)
    return report


def recent(ledger, now=None, hours=24):
    """Per site, the last day: requests, bytes and server errors; sites without traffic are absent."""
    now = now or time.time()
    since = int(now // 3600) * 3600 - (hours - 1) * 3600
    with ledger.db() as db:
        rows = db.execute('SELECT site_id, SUM(requests), SUM(bytes), SUM(server_errors), SUM(client_errors) FROM traffic WHERE hour>=? GROUP BY site_id', (since,))
        return {r[0]: {'requests': r[1], 'bytes': r[2], 'server_errors': r[3], 'client_errors': r[4]} for r in rows}


def site_traffic(ledger, site_id, now=None):
    """What the site page shows: the day and the month as totals, the day by hour and the month by day."""
    now = now or time.time()
    this_hour = int(now // 3600) * 3600
    today = int(now // 86400) * 86400
    with ledger.db() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM traffic WHERE site_id=? AND hour>=? ORDER BY hour', (site_id, today - 29 * 86400))]
    def total(selected):
        result = dict(EMPTY)
        for r in selected:
            for k in EMPTY: result[k] += r[k]
        return result
    hours = [{'hour': h, **total([r for r in rows if r['hour'] == h])} for h in range(this_hour - 23 * 3600, this_hour + 1, 3600)]
    days = [{'day': d, **total([r for r in rows if d <= r['hour'] < d + 86400])} for d in range(today - 29 * 86400, today + 1, 86400)]
    return {'day': total([r for r in rows if r['hour'] >= this_hour - 23 * 3600]), 'month': total(rows), 'hours': hours, 'days': days,
            'peak_hour': max((h['requests'] for h in hours), default=0), 'peak_day': max((d['requests'] for d in days), default=0)}
