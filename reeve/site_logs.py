"""What a site's containers and the edge wrote, for the site's Logs page. A managed site has its web (nginx),
PHP-FPM and database containers, whose output Docker keeps under the `local` driver (10 MB × 3 each); a Compose
package has every container of its project. The edge keeps one JSON access log per hostname. Nothing here is
stored again: the page reads the last lines on demand, bounded, optionally filtered."""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .host import PROXY, command

LINES = (100, 500, 2000)
SINCE = {'': None, '1h': 3600, '24h': 86400, '7d': 7 * 86400}
TAIL_BYTES = 4 * 1024 ** 2


def sources(ledger, row):
    """The log sources a site has, in the order the page shows them: (key, label, container or None)."""
    payload = json.loads(row['payload'])
    if payload.get('runtime') == 'compose':
        from .compose_adopt import read, project_containers
        found = []
        try:
            for container in project_containers(read(row)):
                name = container['Name'].lstrip('/'); service = container['Config'].get('Labels', {}).get('com.docker.compose.service', name)
                found.append((service, service, name))
        except Exception:
            pass
        return [*sorted(found), ('edge', 'Edge access log', None)]
    result = [('web', 'Web server (nginx)', 'hosting-site-' + row['name'])]
    if payload.get('runtime') == 'php': result.append(('php', 'PHP', 'hosting-php-' + row['name']))
    from .database_site import state
    if state(row): result.append(('database', 'Database', 'hosting-db-' + row['name']))
    result.append(('edge', 'Edge access log', None))
    return result


def container_lines(name, lines, since):
    """The last `lines` of a container's output, timestamps first, newest last. Both streams, as Docker keeps them."""
    args = ['docker', 'logs', '--tail', str(lines), '--timestamps']
    if since: args += ['--since', str(int(time.time() - since))]
    try: text = command([*args, name], timeout=30)
    except RuntimeError as exc:
        if 'No such container' in str(exc): return ['(no such container: the site may be stopped or not created yet)']
        raise
    return text.splitlines()[-lines:]


def edge_entry(line):
    """Pure: one of Caddy's JSON access log lines as one readable line, or None for anything else."""
    try: event = json.loads(line)
    except ValueError: return None
    request = event.get('request') or {}
    if 'status' not in event or not request: return None
    when = datetime.fromtimestamp(float(event.get('ts', 0)), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    duration = event.get('duration')
    took = f" {float(duration) * 1000:.0f}ms" if isinstance(duration, (int, float)) else ''
    return f"{when} {event['status']} {request.get('method', '?')} {request.get('host', '')}{request.get('uri', '')} {event.get('size', 0)}B{took} from {request.get('remote_ip', '')}"


def tail_lines(path, limit=TAIL_BYTES):
    """The last `limit` bytes of a file as whole lines, oldest first; nothing when the file is missing."""
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, 2); size = handle.tell(); handle.seek(max(0, size - limit))
            data = handle.read()
    except OSError:
        return []
    text = data.decode(errors='replace')
    if len(data) == limit and '\n' in text: text = text.split('\n', 1)[1]
    return text.splitlines()


def edge_lines(domains, lines, since):
    """The edge's access log entries for the site's hostnames, merged and newest last."""
    cutoff = time.time() - since if since else None
    entries = []
    for domain in domains:
        if not re.fullmatch(r'[a-z0-9.-]+', domain): continue
        for line in tail_lines(PROXY / 'data/logs' / (domain + '.log')):
            try: stamp = float(json.loads(line).get('ts', 0))
            except ValueError: continue
            if cutoff and stamp < cutoff: continue
            formatted = edge_entry(line)
            if formatted: entries.append((stamp, formatted))
    entries.sort()
    return [text for _, text in entries[-lines:]]


def read(ledger, row, source, lines=100, since='', match=''):
    lines = int(lines) if str(lines).isdigit() else 100
    if lines not in LINES: raise ValueError('Choose 100, 500 or 2000 lines')
    if since not in SINCE: raise ValueError('Choose a time window')
    match = str(match or '')[:200]
    available = sources(ledger, row)
    chosen = next((s for s in available if s[0] == source), None)
    if not chosen: raise ValueError('Unknown log source for this site')
    key, label, container = chosen
    if container: found = container_lines(container, lines, SINCE[since])
    else: found = edge_lines(ledger.domains(row), lines, SINCE[since])
    if match:
        needle = match.lower(); found = [l for l in found if needle in l.lower()]
    return {'source': key, 'label': label, 'container': container, 'lines': found, 'count': len(found), 'limit': lines, 'since': since, 'match': match,
            'sources': [{'key': k, 'label': l} for k, l, _ in available]}
