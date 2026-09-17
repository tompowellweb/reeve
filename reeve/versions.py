"""Persistent PHP availability catalogue. Reading never performs network I/O."""
import hashlib
import json
import re
import time
from pathlib import Path

from .host import OPS, atomic, command, trusted

CACHE = OPS / 'panel/worker/php-available.json'
INTERVAL = 30 * 86400


def branch(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]?\.[0-9]{1,2}', value):
        raise ValueError('Choose a PHP branch such as 8.2')
    return value


def read():
    if not CACHE.exists():
        return {'branches': [], 'checked_at': None, 'attempted_at': None, 'error': '', 'stale': True}
    trusted(CACHE)
    data = json.loads(CACHE.read_text())
    data['stale'] = not data.get('checked_at') or time.time() - data['checked_at'] >= INTERVAL
    return data


def choices():
    from .php_runtime import catalog
    installed = catalog()
    rows = {item['branch']: dict(item, installed=item['branch'] in installed, available=not item['missing']) for item in read()['branches']}
    for item in rows.values():
        item['prerelease'] = bool(re.search(r'alpha|beta|rc[0-9]|dev', item['package_version'], re.I))
        if item['prerelease']:
            item['available'] = False
    for version, item in installed.items():
        if version not in rows:
            rows[version] = {'branch': version, 'installed': True, 'available': False, 'missing': [], 'package_version': item['php_version']}
    return sorted(rows.values(), key=lambda item: tuple(map(int, item['branch'].split('.'))), reverse=True)


def require(value):
    branch(value)
    if not any(item['branch'] == value and (item['available'] or item['installed']) for item in choices()):
        raise ValueError('PHP branch is not available with the required packages; check the version catalogue')
    return value


def refresh():
    old = read()
    attempted = time.time()
    try:
        context = Path(__file__).resolve().parent.parent / 'templates/php-catalogue'
        recipe = hashlib.sha256(b''.join(p.name.encode() + p.read_bytes() for p in (context / "Containerfile", context / "collect.py"))).hexdigest()
        tag = 'hosting-php-catalogue:' + recipe[:16]
        command(['docker', 'build', '--tag', tag, '--file', context / 'Containerfile', context], timeout=600)
        result = json.loads(command(['docker', 'run', '--rm', '--memory', '256m', '--pids-limit', '128',
            '--security-opt', 'no-new-privileges:true', '--storage-opt', 'size=512m', tag], timeout=240))
        if result['distribution'] != 'trixie' or not result['branches']:
            raise RuntimeError('Repository returned no usable metadata')
        for item in result['branches']:
            branch(item['branch'])
        result.update(checked_at=attempted, attempted_at=attempted, error='', schema=1)
        atomic(CACHE, json.dumps(result, indent=2))
        return read()
    except Exception as exc:
        old.update(attempted_at=attempted, error=str(exc)[:1500])
        old.pop('stale', None)
        atomic(CACHE, json.dumps(old, indent=2))
        raise


def due():
    data = read()
    # Failed refresh retries daily; it never discards a last-known-good catalogue.
    return data['stale'] and time.time() - (data.get('attempted_at') or 0) >= 86400
