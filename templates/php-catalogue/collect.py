"""Run in an isolated collector image; apt verifies Surý's signed index."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

SUFFIXES = ('fpm', 'cli', 'bcmath', 'curl', 'gd', 'imagick', 'intl', 'mbstring', 'mysql',
            'pgsql', 'sqlite3', 'soap', 'xml', 'zip', 'opcache', 'apcu', 'redis', 'memcached')


def parse(text):
    packages = {}
    for paragraph in text.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines() if ': ' in line and not line.startswith(' '))
        name, version = fields.get('Package', ''), fields.get('Version', '')
        if not re.fullmatch(r'php\d{1,2}\.\d{1,2}-[a-z0-9-]+', name) or not version:
            continue
        names = [name, *[item.strip().split(' ')[0] for item in fields.get('Provides', '').split(',') if item.strip()]]
        for provided in names:
            if not re.fullmatch(r'php\d{1,2}\.\d{1,2}-[a-z0-9-]+', provided):
                continue
            old = packages.get(provided)
            if old and subprocess.run(['dpkg', '--compare-versions', version, 'le', old['version']]).returncode == 0:
                continue
            packages[provided] = {'version': version, 'provider': name}
    branches = sorted({re.match(r'php(\d+\.\d+)-', name)[1] for name in packages if name.endswith('-fpm')}, key=lambda b: tuple(map(int, b.split('.'))))
    for branch in branches:
        if tuple(map(int, branch.split('.'))) >= (8, 5):
            packages.setdefault('php' + branch + '-opcache', {'version': packages['php' + branch + '-fpm']['version'], 'provider': 'built into PHP'})
    return [{'branch': branch, 'package_version': packages['php' + branch + '-fpm']['version'],
             'packages': {suffix: packages.get('php' + branch + '-' + suffix) for suffix in SUFFIXES},
             'missing': [suffix for suffix in SUFFIXES if 'php' + branch + '-' + suffix not in packages]} for branch in branches]


if __name__ == '__main__':
    options = ['-o', 'Dir::Etc::sourcelist=/etc/apt/sources.list.d/php.list', '-o', 'Dir::Etc::sourceparts=-',
               '-o', 'APT::Update::Error-Mode=any', '-o', 'Acquire::Retries=1', '-o', 'Acquire::https::Timeout=20']
    subprocess.run(['apt-get', *options, 'update'], check=True, stdout=subprocess.DEVNULL, timeout=180)
    branches = parse(subprocess.check_output(['apt-cache', *options, 'dumpavail'], text=True))
    signed = list(Path('/var/lib/apt/lists').glob('*packages.sury.org*InRelease'))
    assert len(signed) == 1 and branches, 'Missing verified release or PHP packages'
    print(json.dumps({'branches': branches, 'architecture': subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip(),
        'distribution': 'trixie', 'source': 'https://packages.sury.org/php/',
        'inrelease_sha256': hashlib.sha256(signed[0].read_bytes()).hexdigest()}))
