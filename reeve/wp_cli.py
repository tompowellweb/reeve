"""Pinned official WP-CLI PHAR, mounted read-only and executed only as the site UID."""
import hashlib
import os
from urllib.request import urlopen

from .host import OPS, trusted

VERSION = '2.12.0'
SHA256 = 'ce34ddd838f7351d6759068d09793f26755463b4a4610a5a5c0a97b68220d85c'
URL = f'https://github.com/wp-cli/wp-cli/releases/download/v{VERSION}/wp-cli-{VERSION}.phar'
CACHE = OPS / 'panel/worker/wp-cli'


def phar():
    CACHE.mkdir(mode=0o700, exist_ok=True); trusted(CACHE, directory=True)
    target = CACHE / (SHA256 + '.phar')
    if target.exists():
        trusted(target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != SHA256:
            raise ValueError('Cached WP-CLI checksum mismatch')
        return target
    with urlopen(URL, timeout=30) as response: raw = response.read(16 * 1048576 + 1)
    if len(raw) > 16 * 1048576 or hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError('WP-CLI download checksum mismatch')
    # Atomic publication also permits retry after a worker interruption during download/write.
    temporary = CACHE / 'download.tmp'
    with temporary.open('wb') as stream:
        os.fchmod(stream.fileno(), 0o444); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, target)
    return target
