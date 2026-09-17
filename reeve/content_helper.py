"""Fixed file helper, executed ONLY as the site UID inside a container.

Every traversal uses directory descriptors and O_NOFOLLOW. No archive-supplied
permissions, owners, links, devices, sparse records or executable hooks are used.
"""
import contextlib
import hashlib
import json
import os
import stat
import tarfile
import tempfile
import zipfile
from pathlib import Path

MAX_FILES = 20000
EDIT_LIMIT = 64 * 1024


def path_parts(value):
    if value in ('', '.'): return []
    if value.startswith('/') or any(ord(c) < 32 or c in '\\:' for c in value): raise ValueError('Unsafe content path')
    parts = value.split('/')
    if len(value.encode()) > 1024 or any(p in ('', '.', '..') or len(p.encode()) > 255 for p in parts): raise ValueError('Unsafe content path')
    return parts


@contextlib.contextmanager
def directory(root, parts, create=False):
    fd = os.dup(root)
    try:
        for part in parts:
            if create:
                try: os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError: pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        yield fd
    finally:
        os.close(fd)


def regular(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise ValueError('Only regular files with one link are supported')
    return info


def digest(fd):
    h = hashlib.sha256()
    while chunk := os.read(fd, 1048576): h.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return h.hexdigest()


def check_target(root, parts, is_dir, replace):
    try:
        with directory(root, parts[:-1]) as parent:
            try: info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError: return
    except FileNotFoundError: return
    if is_dir and stat.S_ISDIR(info.st_mode): return
    if not is_dir and replace and stat.S_ISREG(info.st_mode) and info.st_nlink == 1: return
    raise ValueError('Destination exists or is unsafe: ' + '/'.join(parts))


def write_file(root, parts, source, size, replace, expected=''):
    if not parts: raise ValueError('Choose a file path')
    with directory(root, parts[:-1], create=True) as parent:
        check_target(root, parts, False, replace)
        if expected:
            prior = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                regular(prior)
                if digest(prior) != expected: raise ValueError('File changed since it was opened; reload before saving')
            finally: os.close(prior)
        name = '.hosting-upload-' + next(tempfile._get_candidate_names())
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                remaining = size
                while remaining:
                    chunk = source.read(min(1048576, remaining))
                    if not chunk: raise ValueError('Truncated input')
                    output.write(chunk); remaining -= len(chunk)
                if source.read(1): raise ValueError('Input exceeds declared size')
                output.flush(); os.fsync(output.fileno())
            if replace:
                # Replacement changes the directory entry, never follows an existing link.
                check_target(root, parts, False, True)
                os.replace(name, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            else:
                os.link(name, parts[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        finally:
            try: os.unlink(name, dir_fd=parent)
            except FileNotFoundError: pass


def archive_entries(path, limit):
    entries = []
    total = 0
    if zipfile.is_zipfile(path):
        archive = zipfile.ZipFile(path)
        def records():
            for member in archive.infolist():
                mode = member.external_attr >> 16
                if member.flag_bits & 1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR): raise ValueError('Encrypted archives, links and special files are unsupported')
                yield member.filename, member.is_dir(), member.file_size, member
        opener = archive.open
    else:
        archive = tarfile.open(path, mode='r:*')
        def records():
            for member in archive:
                if not (member.isfile() or member.isdir()) or member.issparse(): raise ValueError('Archive links, sparse records and special files are unsupported')
                yield member.name, member.isdir(), member.size, member
        opener = archive.extractfile
    try:
        seen = {}
        for name, is_dir, size, member in records():
            # Ordinary tar archives use ./ prefixes and one trailing directory slash.
            while name.startswith('./'): name = name[2:]
            if is_dir: name = name.rstrip('/')
            if name in ('', '.') and is_dir: continue
            parts = path_parts(name)
            if not parts: raise ValueError('Archive file has no name')
            if name in seen: raise ValueError('Archive contains duplicate paths')
            seen[name] = is_dir
            if size < 0: raise ValueError('Invalid archive size')
            total += size
            if len(entries) >= MAX_FILES or total > limit: raise ValueError('Archive exceeds file count or expanded size bound')
            entries.append((parts, is_dir, size, member))
        for parts, is_dir, _, _ in entries:
            for i in range(1, len(parts)):
                if seen.get('/'.join(parts[:i])) is False: raise ValueError('Archive file is also a parent directory')
        return archive, opener, entries, total
    except BaseException:
        archive.close(); raise


def perform(root, action, data, input_path='/input'):
    parts = path_parts(data.get('path', '.'))
    if action == 'list':
        with directory(root, parts) as fd:
            entries = []
            # Iteration is bounded, including directories with millions of entries.
            with os.scandir(fd) as it:
                for entry in it:
                    if len(entries) >= 500: raise ValueError('Directory has over 500 entries; open a subdirectory by path')
                    info = entry.stat(follow_symlinks=False)
                    entries.append({'name': entry.name, 'directory': stat.S_ISDIR(info.st_mode),
                                    'regular': stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'size': info.st_size})
        return {'entries': sorted(entries, key=lambda e: (not e['directory'], e['name']))}
    if action == 'read':
        if not parts: raise ValueError('Choose a file')
        with directory(root, parts[:-1]) as parent:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                if regular(fd).st_size > EDIT_LIMIT: raise ValueError('Editor supports UTF-8 files up to 64 KiB')
                content = os.read(fd, EDIT_LIMIT + 1)
                if len(content) > EDIT_LIMIT or b'\x00' in content: raise ValueError('File is too large or binary')
                return {'text': content.decode('utf-8'), 'sha256': hashlib.sha256(content).hexdigest()}
            finally: os.close(fd)
    if action in ('upload', 'edit'):
        with open(input_path, 'rb') as source: write_file(root, parts, source, data['size'], data['replace'], data.get('expected', ''))
        return {'files': 1, 'bytes': data['size']}
    if action == 'extract':
        archive, opener, entries, total = archive_entries(input_path, data['expanded_limit'])
        with archive:
            # Validate the COMPLETE archive and all existing paths before the first write.
            for subparts, is_dir, _, _ in entries: check_target(root, parts + subparts, is_dir, data['replace'])
            for subparts, is_dir, size, member in entries:
                target = parts + subparts
                if is_dir:
                    with directory(root, target, create=True): pass
                else:
                    with opener(member) as source: write_file(root, target, source, size, data['replace'])
        return {'files': len(entries), 'bytes': total}
    raise ValueError('Unsupported file operation')


if __name__ == '__main__':
    import sys
    os.umask(0o077)
    try:
        if os.geteuid() == 0: raise ValueError('Refusing to run content helper as root')
        import resource
        # Bound parser memory/CPU independently of the site's optional container caps.
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1048576, 512 * 1048576))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        data = json.loads(Path('/request.json').read_text())
        root = os.open('/site', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: result = perform(root, data.pop('action'), data)
        finally: os.close(root)
        print(json.dumps(result))
    except Exception as exc:
        print(str(exc)[:1000]); sys.exit(1)
