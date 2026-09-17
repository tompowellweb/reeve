"""Read-only package review. Never extract archives or execute application commands."""
import posixpath
import re
import stat
import tarfile
import zipfile

import yaml

from .compose_inspect import BASE_FILES
from .core import validate_domains

UPLOAD_LIMIT = 512 * 1048576
EXPANDED_LIMIT = 4 * 1024**3
ENTRY_LIMIT = 100000
CONTROL_LIMIT = 1048576


def path_name(value):
    if not isinstance(value, str) or '\\' in value or '\x00' in value or value.startswith('/'):
        raise ValueError('Package paths must stay inside the project folder.')
    while value.startswith('./'): value = value[2:]
    value = value.rstrip('/')
    if any(p in ('', '..') for p in value.split('/')) and value:
        raise ValueError('Package paths must stay inside the project folder.')
    return '' if value == '.' else value


def metadata(data):
    if set(data) != {'name', 'domain', 'service', 'port'}:
        raise ValueError('Supply only name, domain, service and port.')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', str(data['name'])):
        raise ValueError('Use a site name of up to 32 lower-case letters, numbers and hyphens.')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', str(data['service'])):
        raise ValueError('Choose the Compose HTTP service.')
    try: port = int(data['port'])
    except (ValueError, TypeError): raise ValueError('Use an internal HTTP port from 1 to 65535.') from None
    if isinstance(data['port'], bool) or not 1 <= port <= 65535:
        raise ValueError('Use an internal HTTP port from 1 to 65535.')
    return dict(name=data['name'], domain=validate_domains([data['domain']])[0], service=data['service'], port=port)


class Archive:
    def __init__(self, path):
        self.archive = None
        self.entries = {}
        self.expanded = 0
        try:
            # A tar is recognised by its first header. is_zipfile() alone is not enough: it looks for a
            # zip end record near the end of the file, which a tar carrying a .zip among its last
            # members also has, and the zip's contents would then replace the tar's.
            if not tarfile.is_tarfile(path) and zipfile.is_zipfile(path):
                self.archive = zipfile.ZipFile(path)
                for m in self.archive.infolist():
                    mode = m.external_attr >> 16
                    kind = 'link' if stat.S_ISLNK(mode) else 'directory' if m.is_dir() else 'file'
                    if m.flag_bits & 1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK):
                        raise ValueError('Encrypted archives and special files are unsupported.')
                    self.add(m.filename, kind, m.file_size, m)
            else:
                self.archive = tarfile.open(path, 'r:*')
                for m in self.archive:
                    if m.issparse() or not (m.isfile() or m.isdir() or m.issym()):
                        raise ValueError('Hard links, sparse files and special files are unsupported.')
                    self.add(m.name, 'directory' if m.isdir() else 'link' if m.issym() else 'file', m.size, m)
            for name, entry in self.entries.items():
                parents = name.split('/')[:-1]
                for i in range(1, len(parents) + 1):
                    parent = self.entries.get('/'.join(parents[:i]))
                    if parent and parent['kind'] != 'directory':
                        raise ValueError('An archive member is nested beneath a file or link.')
                if entry['kind'] == 'link':
                    m = entry['member']
                    target = self.read_member(m, 4096).decode('utf-8') if isinstance(m, zipfile.ZipInfo) else m.linkname
                    if target.startswith('/') or '\\' in target or '\x00' in target:
                        raise ValueError('An archive link points outside the project.')
                    combined = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
                    if combined == '..' or combined.startswith('../'):
                        raise ValueError('An archive link points outside the project.')
                    entry['target'] = combined
        except Exception:
            if self.archive: self.archive.close()
            raise

    def add(self, raw, kind, size, member):
        name = path_name(raw)
        if not name and kind == 'directory': return
        if not name or name in self.entries: raise ValueError('Empty or duplicate archive path.')
        self.expanded += size
        if size < 0 or self.expanded > EXPANDED_LIMIT or len(self.entries) >= ENTRY_LIMIT:
            raise ValueError('Package exceeds the expanded size or file-count limit.')
        self.entries[name] = {'kind': kind, 'size': size, 'member': member}

    def read_member(self, member, limit):
        stream = self.archive.open(member) if isinstance(self.archive, zipfile.ZipFile) else self.archive.extractfile(member)
        if stream is None: raise ValueError('Expected a regular file.')
        with stream: data = stream.read(limit + 1)
        if len(data) > limit: raise ValueError('A configuration input exceeds its size limit.')
        return data

    def read(self, name):
        entry = self.entries.get(name)
        if not entry or entry['kind'] != 'file': raise ValueError('Required configuration file is missing or is a link.')
        return self.read_member(entry['member'], CONTROL_LIMIT)

    def close(self): self.archive.close()


def review(path, data):
    archive = None
    try:
        archive = Archive(path)
        entries = archive.entries
        # Accept either files at archive root or one enclosing project directory.
        candidates = [n for n, e in entries.items() if posixpath.basename(n) in BASE_FILES and e['kind'] == 'file']
        roots = [n for n in candidates if '/' not in n]
        if not roots:
            top = {n.split('/')[0] for n in entries}
            if len(top) == 1:
                roots = [n for n in candidates if n.count('/') == 1]
        if len(roots) != 1: raise ValueError('Supply one Compose file at the project root.')
        compose = roots[0]; prefix = posixpath.dirname(compose)
        raw = archive.read(compose)
        # Bound aliases before construction; source diagnostics are never returned.
        tokens = list(yaml.scan(raw))
        if sum(isinstance(t, yaml.tokens.AliasToken) for t in tokens) > 50:
            raise ValueError('Compose contains too many YAML aliases.')
        model = yaml.safe_load(raw)
        if not isinstance(model, dict): raise ValueError('Compose must contain a services mapping.')
        seen = set(); visited = 0
        def walk(value, depth=0):
            nonlocal visited
            visited += 1
            if visited > 20000 or depth > 40 or id(value) in seen: raise ValueError('Compose is too large or recursive.')
            if isinstance(value, (list, dict)):
                seen.add(id(value))
                if isinstance(value, dict) and any(not isinstance(k, str) for k in value):
                    raise ValueError('Compose keys must be text.')
                for child in value.values() if isinstance(value, dict) else value: walk(child, depth + 1)
                seen.remove(id(value))
        walk(model)
        services = model.get('services')
        if not isinstance(services, dict) or not 1 <= len(services) <= 32:
            raise ValueError('Compose must contain between one and 32 services.')
        issues = []
        def issue(code, field, message): issues.append({'code': code, 'path': field, 'message': message})
        def local(value, field, must_exist=True):
            if not isinstance(value, str): issue('invalid_path', field, 'Use a project-relative file path.'); return
            if '$' in value: issue('unresolved_path', field, 'Resolve variables in mounted/input paths during preparation.'); return
            try: name = path_name(value)
            except ValueError: issue('outside_project', field, 'This path is outside the project folder.'); return
            full = posixpath.join(prefix, name) if prefix else name
            pieces = full.split('/')
            if any(entries.get('/'.join(pieces[:i]), {}).get('kind') == 'link' for i in range(1, len(pieces) + 1)):
                issue('linked_input', field, 'A mounted/configuration path crosses a link; use its real path.'); return
            exists = not name or full in entries or any(n.startswith(full + '/') for n in entries)
            if must_exist and not exists: issue('missing_file', field, 'A referenced file or directory is missing from the package.')
        for key in ('include', 'extends'):
            if key in model: issue('compose_merge_required', key, 'Merge Compose inputs into one file during preparation.')
        inventory = []
        for name, spec in services.items():
            if not isinstance(spec, dict): raise ValueError('Every Compose service must be a mapping.')
            base = 'services.' + name
            build = spec.get('build')
            build_info = None
            if build is not None:
                if isinstance(build, str): build = {'context': build}
                if not isinstance(build, dict): raise ValueError('Compose build must be a path or mapping.')
                context = build.get('context', '.')
                local(context, base + '.build.context')
                dockerfile = build.get('dockerfile', 'Dockerfile')
                if 'dockerfile_inline' in build:
                    if not isinstance(build['dockerfile_inline'], str): raise ValueError('Inline Dockerfile must be text.')
                    build_info = {'context': context, 'dockerfile': '(inline)'}
                elif isinstance(context, str) and isinstance(dockerfile, str):
                    if dockerfile.startswith('/') or '..' in dockerfile.split('/'):
                        issue('outside_project', base + '.build.dockerfile', 'Keep the Dockerfile inside the supplied build context.')
                    else:
                        location = posixpath.join(context, dockerfile)
                        local(location, base + '.build.dockerfile')
                        try: full = posixpath.join(prefix, path_name(location)) if prefix else path_name(location)
                        except ValueError: full = None
                        if full in entries and entries[full]['kind'] == 'file': archive.read(full)
                        build_info = {'context': context, 'dockerfile': location}
                else: raise ValueError('Build context and Dockerfile must be paths.')
                if build.get('additional_contexts'):
                    issue('additional_build_contexts', base + '.build.additional_contexts', 'Additional build inputs need a confined runtime build review.')
            image = spec.get('image')
            if image is None and build is None:
                issue('image_or_build_required', base, 'Supply a registry image or a Dockerfile build context.')
            elif image is not None:
                if not isinstance(image, str) or not image:
                    issue('invalid_image', base + '.image', 'Use an image reference or a Dockerfile build.')
                    image = None
                elif not re.fullmatch(r'[A-Za-z0-9._/:-]+(?:@sha256:[0-9a-f]{64})?', image) and '$' not in image:
                    issue('invalid_image', base + '.image', 'Supply an ordinary image reference without embedded credentials.')
                    image = None
                elif '$' in image:
                    issue('unresolved_image', base + '.image', 'Resolve the image reference during preparation.')
            for field in ('include', 'extends', 'label_file'):
                if spec.get(field): issue('compose_merge_required', base + '.' + field, 'Merge this external Compose input during preparation.')
            envs = spec.get('env_file', [])
            if isinstance(envs, (str, dict)): envs = [envs]
            if not isinstance(envs, list): raise ValueError('Invalid environment file list.')
            for value in envs:
                local(value.get('path') if isinstance(value, dict) else value, base + '.env_file')
            mounts = []
            for value in spec.get('volumes', []):
                if isinstance(value, str):
                    parts = value.split(':')
                    if len(parts) < 2:
                        issue('anonymous_volume', base + '.volumes', 'Give every persistent volume a name.'); continue
                    source, target = parts[:2]
                    kind = 'bind' if source.startswith(('.', '/', '~')) else 'volume'
                elif isinstance(value, dict):
                    kind, source, target = value.get('type'), value.get('source'), value.get('target')
                else: raise ValueError('Invalid Compose mount.')
                if kind == 'bind': local(source, base + '.volumes')
                elif kind == 'volume':
                    if not source: issue('anonymous_volume', base + '.volumes', 'Give every persistent volume a name.')
                else: issue('mount_type', base + '.volumes', 'This storage type needs a supported capture method.')
                mounts.append({'type': kind, 'source': source, 'target': target})
            inventory.append({'service': name, 'build': build_info, 'image': image if isinstance(image, str) and '$' not in image else None, 'mounts': mounts})
        if data['service'] not in services:
            issue('http_service_missing', 'service', 'The selected HTTP service is absent from Compose.')
        for name, definition in (model.get('volumes') or {}).items():
            if definition and (definition.get('external') or definition.get('driver_opts')):
                issue('external_volume', 'volumes.' + name, 'An incoming project cannot depend on an existing host volume or driver path.')
        for kind in ('configs', 'secrets'):
            for name, spec in (model.get(kind) or {}).items():
                if isinstance(spec, dict) and spec.get('file'): local(spec['file'], kind + '.' + name)
                if isinstance(spec, dict) and spec.get('external'):
                    issue('external_input', kind + '.' + name, 'Include the required configuration as a private project file.')
        return {'state': 'needs_preparation' if issues else 'reviewed', 'issues': issues,
                'compose': compose, 'entries': len(entries), 'expanded_bytes': archive.expanded,
                'services': inventory, 'deployed': False,
                'checks': ['archive_paths', 'compose_structure', 'declared_file_paths', 'http_service'],
                'remaining_checks': ['upstream_image_access_and_build', 'runtime_compatibility', 'database_restore', 'application_start', 'backup_and_restore']}
    except (yaml.YAMLError, UnicodeError, RecursionError, tarfile.TarError, zipfile.BadZipFile, EOFError, TypeError, AttributeError):
        raise ValueError('The package or Compose could not be read. Check its archive and configuration types.') from None
    finally:
        if archive: archive.close()
