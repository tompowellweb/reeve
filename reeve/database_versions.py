"""Cached official image series; resolve to an architecture-specific immutable digest."""
import hashlib
import json
import platform
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.request import Request, urlopen

from .host import OPS, atomic, trusted

ENGINES = ('mysql', 'mariadb', 'postgres')
CACHE = OPS / 'panel/worker/database-available.json'
INTERVAL = 30 * 86400
SOURCE = 'https://raw.githubusercontent.com/docker-library/official-images/master/library/'


def fetch(url, headers=None):
    with urlopen(Request(url, headers={'User-Agent': 'reeve/1', **(headers or {})}), timeout=30) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError('Database metadata exceeds size limit')
    return data


def architecture():
    return {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine(), platform.machine())


def numeric(tag, engine):
    if not isinstance(tag, str) or len(tag) > 32 or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', tag): return False
    parts = tag.split('.')
    return len(parts) == (2 if engine == 'postgres' and int(parts[0]) >= 10 else 3)


def series(engine, version):
    parts = version.split('.')
    return parts[0] if engine == 'postgres' and int(parts[0]) >= 10 else '.'.join(parts[:2])


def published(text, engine, arch):
    result = {}
    for block in text.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in block.splitlines() if ': ' in line and not line.startswith('#'))
        tags = fields.get('Tags', '').split(', ')
        if {'arm64': 'arm64v8'}.get(arch, arch) not in fields.get('Architectures', '').split(', '):
            continue
        for tag in tags:
            if numeric(tag, engine):
                result[tag] = {'default': ('latest' if engine == 'postgres' else 'lts') in tags,
                               'track': 'LTS' if 'lts' in tags else ('Innovation' if 'innovation' in tags else 'Stable')}
    return result


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows = []; self.row = []; self.cell = None
    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.row = []
        if tag in ('td', 'th'): self.cell = []
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split())); self.cell = None
        if tag == 'tr': self.rows.append(self.row)


def maria_support(html):
    parser = Tables(); parser.feed(html)
    dates = {}
    for row in parser.rows:
        if len(row) >= 5 and re.fullmatch(r'\d+\.\d+', row[0]):
            try: dates[row[0]] = datetime.strptime(row[2], '%d %b %Y').replace(tzinfo=timezone.utc).timestamp()
            except ValueError: pass
    if not dates:
        raise ValueError('Could not read MariaDB community support dates; previous catalogue retained')
    return dates


def token(engine):
    return json.loads(fetch('https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/' + engine + ':pull'))['token']


def read():
    if not CACHE.exists(): return {'engines': {}, 'checked_at': None, 'attempted_at': None, 'error': '', 'stale': True}
    trusted(CACHE)
    result = json.loads(CACHE.read_text())
    result['stale'] = time.time() - (result.get('checked_at') or 0) >= INTERVAL
    return result


def refresh():
    previous = read(); now = time.time()
    try:
        arch = architecture(); engines = {}
        support = maria_support(fetch('https://mariadb.org/about/').decode())
        for engine in ENGINES:
            metadata = fetch(SOURCE + engine)
            active = published(metadata.decode(), engine, arch)
            all_tags = json.loads(fetch(f'https://registry-1.docker.io/v2/library/{engine}/tags/list?n=20000',
                                      {'Authorization': 'Bearer ' + token(engine)}))['tags']
            versions = [tag for tag in all_tags if numeric(tag, engine)]
            grouped = {}
            for version in sorted(versions, key=lambda v: tuple(map(int, v.split('.'))), reverse=True):
                branch = series(engine, version)
                grouped.setdefault(branch, {'series': branch, 'version': version, 'current': False, 'default': False, 'track': 'Compatibility'})
                if version in active:
                    item = grouped[branch]
                    item.update(version=version, current=True, **active[version])
                    if engine == 'mariadb' and (branch in support and support[branch] <= now):
                        item.update(current=False, track='Compatibility', default=False)
            rows = sorted(grouped.values(), key=lambda r: tuple(map(int, r['series'].split('.'))), reverse=True)
            if not any(r['default'] for r in rows):
                raise ValueError(f'No verified stable/LTS default for {engine}; previous catalogue retained')
            engines[engine] = {'series': rows, 'versions': versions, 'source_sha256': hashlib.sha256(metadata).hexdigest()}
        result = {'schema': 1, 'architecture': arch, 'engines': engines, 'checked_at': now, 'attempted_at': now,
                  'error': '', 'source': SOURCE, 'mariadb_support': support}
        atomic(CACHE, json.dumps(result, indent=2))
        return read()
    except Exception as exc:
        previous.update(attempted_at=now, error=str(exc)[:1000]); previous.pop('stale', None)
        atomic(CACHE, json.dumps(previous, indent=2)); raise


def due():
    data = read()
    return data['stale'] and time.time() - (data.get('attempted_at') or 0) >= 86400


def select(spec):
    engine = spec['engine']; data = read()['engines'].get(engine, {})
    if spec.get('exact'):
        return spec['exact']
    rows = data.get('series', [])
    matches = [r for r in rows if r['series'] == spec.get('series')] if spec.get('series') else [r for r in rows if r['default']]
    if not matches: raise ValueError('Database series is unavailable; check the version catalogue or use an exact version')
    return matches[0]['version']


def resolve(engine, version):
    if engine not in ENGINES or not numeric(version, engine): raise ValueError('Invalid database version')
    auth = {'Authorization': 'Bearer ' + token(engine), 'Accept': ', '.join([
        'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
        'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])}
    raw = fetch(f'https://registry-1.docker.io/v2/library/{engine}/manifests/{version}', auth)
    manifest = json.loads(raw)
    if 'manifests' in manifest:
        found = [m for m in manifest['manifests'] if m.get('platform', {}).get('os') == 'linux' and m['platform'].get('architecture') == architecture()]
        if len(found) != 1: raise ValueError('No unambiguous Linux image for the VM architecture')
        digest = found[0]['digest']
    else:
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest): raise ValueError('Invalid registry digest')
    return 'docker.io/library/' + engine + '@' + digest
