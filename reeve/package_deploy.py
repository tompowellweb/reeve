"""Durable deployment from a private intake archive, using the existing Compose runtime.

Mode 2 doctrine (2026-09-16): the Compose project is accepted as supplied. Published ports
and container names are stripped; only host hazards are refused; images run as they ship.
Recognised database images are restored once from dumps/ before the application starts.
Input archives stay immutable; only worker-owned copies reach Compose or the builder.
"""
import copy
import hashlib
import json
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import subprocess
import signal
import time
import zipfile
from pathlib import Path

import yaml

from . import compose_adopt as ca, compose_inspect as ci
from .application_package import Archive, UPLOAD_LIMIT, metadata, review
from .core import request_id, validate_create
from .host import OPS, SITES, PROXY, ENV, atomic, trusted, preflight, apply_quota

STORE = OPS / 'panel/worker/packages'
INTAKE = OPS / 'panel/web/imports'
BUILD_FIELDS = {'context', 'dockerfile', 'dockerfile_inline', 'args', 'target'}
# Host hazards: anything that reaches outside the container's own namespaces or the site folder.
HAZARDS = {'privileged', 'cap_add', 'devices', 'device_cgroup_rules', 'network_mode', 'pid', 'ipc', 'uts',
           'userns_mode', 'cgroup_parent', 'cgroup', 'volumes_from', 'sysctls', 'gpus', 'use_api_socket',
           'provider', 'develop', 'post_start', 'pre_stop', 'extends', 'include', 'label_file'}
DATABASES = {'mysql': 'MySQL', 'mariadb': 'MariaDB', 'postgres': 'PostgreSQL'}
DUMP_SUFFIXES = ('.sql', '.sql.gz', '.dump')


def state_path(ident):
    return STORE / request_id(ident)


def load(row):
    path = state_path(row['id']) / 'state.json'
    trusted(path)
    return json.loads(path.read_text())


def save(row, state):
    atomic(state_path(row['id']) / 'state.json', json.dumps(state, sort_keys=True))


def eligibility(report):
    if report.get('state') != 'reviewed': return 'Resolve the package preparation issues first.'
    return ''


def family(image):
    return str(image).split('@')[0].split('/')[-1].split(':')[0]


def submit(ledger, host, ident, data, checksum):
    request_id(ident); data = metadata(data)
    if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum):
        raise ValueError('Invalid package checksum.')
    settings = validate_create({'name': data['name'], 'domain': data['domain'], **host.defaults})
    payload = dict(settings, runtime='compose', package_id=ident, package_sha256=checksum,
                   package_input=data, project_name='package-' + ident)
    serialized = json.dumps(payload, sort_keys=True)
    STORE.mkdir(mode=0o700, exist_ok=True); trusted(STORE, directory=True)
    with ledger.db() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()
        if prior:
            if json.loads(prior['payload']).get('package_input') != data or json.loads(prior['payload']).get('package_sha256') != checksum:
                raise ValueError('This operation already belongs to different package inputs.')
            return dict(prior)
        root = SITES / data['name']
        if root.exists() or root.is_symlink(): raise ValueError('This site folder already exists; it has been kept.')
        if (PROXY / 'routes.json').exists():
            trusted(PROXY / 'routes.json')
            if data['domain'] in json.loads((PROXY / 'routes.json').read_text()):
                raise ValueError('This hostname already belongs to an edge route.')
        ordinal = db.execute('SELECT coalesce(max(uid),29999)+1 FROM jobs').fetchone()[0]
        if ordinal >= 60000: raise ValueError('Site identity range exhausted.')
        now = time.time()
        try:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,'queued','receiving worker copy','',?,?)",
                       (ident, data['name'], data['domain'], serialized, ordinal, ordinal + 70000, now, now))
            db.execute('INSERT INTO site_domains VALUES (?,?,1)', (data['domain'], ident))
            db.execute('INSERT INTO adopted_projects VALUES (?,?)', (payload['project_name'], ident))
        except sqlite3.IntegrityError:
            raise ValueError('Site name, hostname or Compose project is already reserved.') from None
        path = state_path(ident); path.mkdir(mode=0o700, exist_ok=True); trusted(path, directory=True)
        if (path / 'state.json').exists():
            old = json.loads((path / 'state.json').read_text())
            if old['input'] != data or old['sha256'] != checksum or old['stage'] != 'reserved':
                raise ValueError('Saved operation differs; keep it for recovery.')
        else:
            atomic(path / 'state.json', json.dumps({'input': data, 'sha256': checksum, 'stage': 'reserved'}))
    return ledger.get(ident)


def worker_copy(row, state):
    """Anchor both user-controlled path components with no-follow directory descriptors."""
    target = state_path(row['id']) / 'package'
    if target.exists():
        trusted(target)
        with target.open('rb') as stream: checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        if checksum != state['sha256']: raise ValueError('Worker package checksum changed; preserve it for review.')
        return target
    flags = os.O_RDONLY | os.O_NOFOLLOW
    rootfd = os.open(INTAKE, flags | os.O_DIRECTORY)
    try:
        dirfd = os.open(row['id'], flags | os.O_DIRECTORY, dir_fd=rootfd)
        try: fd = os.open('package', flags | os.O_NONBLOCK, dir_fd=dirfd)
        finally: os.close(dirfd)
    finally: os.close(rootfd)
    partial = target.with_name('package.partial')
    try:
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= UPLOAD_LIMIT:
                raise ValueError('Expected a bounded regular package file.')
            with partial.open('wb') as dest:
                digest = hashlib.sha256(); size = 0
                while chunk := source.read(1048576):
                    size += len(chunk)
                    if size > UPLOAD_LIMIT: raise ValueError('Package grew beyond the upload limit.')
                    dest.write(chunk); digest.update(chunk)
                dest.flush(); os.fsync(dest.fileno())
            if digest.hexdigest() != state['sha256']:
                raise ValueError('Uploaded package changed; submit a fresh review.')
        partial.replace(target)
    finally: partial.unlink(missing_ok=True)
    return target


def extract(package, destination, report):
    """Private, new directory only. Never use archive extraction or restore archive ownership."""
    archive = Archive(package)
    prefix = posixpath.dirname(report['compose'])
    try:
        for name, entry in archive.entries.items():
            relative = posixpath.relpath(name, prefix) if prefix else name
            if relative == '.': continue
            if relative.startswith('../'): raise ValueError('File escapes the enclosed project.')
            target = destination / relative
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if entry['kind'] == 'directory': target.mkdir(mode=0o755, exist_ok=True)
            elif entry['kind'] == 'file':
                member = entry['member']
                source = archive.archive.open(member) if isinstance(archive.archive, zipfile.ZipFile) else archive.archive.extractfile(member)
                with source, target.open('xb') as output:
                    shutil.copyfileobj(source, output, 1048576)
                    if output.tell() != entry['size']: raise ValueError('Archive file was truncated.')
                mode = member.external_attr >> 16 if isinstance(member, zipfile.ZipInfo) else member.mode
                # Keep executability, never setuid, ownership or world-writable permissions.
                target.chmod(0o755 if mode & 0o111 else 0o644)
        # Install links last: none can become an extraction traversal path.
        for name, entry in archive.entries.items():
            if entry['kind'] != 'link': continue
            relative = posixpath.relpath(name, prefix) if prefix else name
            linked = posixpath.relpath(entry['target'], prefix) if prefix else entry['target']
            if linked == '..' or linked.startswith('../'): raise ValueError('Link escapes the enclosed project.')
            target = destination / relative
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.symlink_to(posixpath.relpath(linked, posixpath.dirname(relative) or '.'))
    finally: archive.close()
    # mkdir's mode is filtered by the worker's 0077 umask; containers need traversal.
    for folder, dirs, files in os.walk(destination, followlinks=False):
        Path(folder).chmod(0o755)
    destination.chmod(0o700)


def prepare_model(root, report, data, project):
    """Accept the Compose project as supplied; refuse host hazards; strip published identities."""
    filename = Path(report['compose']).name
    model = ci.safe_parse(ci.regular(root / filename))
    extra = set(model) - {'name', 'version', 'services', 'networks', 'volumes'}
    if extra:
        raise ValueError('Top-level ' + ', '.join(sorted(extra)) + ' is not supported yet; keep configuration as project files and bind mounts.')
    if not isinstance(model.get('services'), dict) or data['service'] not in model['services']:
        raise ValueError('The selected HTTP service is absent from Compose.')
    model = copy.deepcopy(model); model['name'] = project; model.pop('version', None)
    for service, spec in model['services'].items():
        if not isinstance(spec, dict): raise ValueError(service + ': every service must be a mapping.')
        hazards = HAZARDS & set(spec)
        if hazards:
            raise ValueError(service + ': ' + ', '.join(sorted(hazards)) + ' reaches outside the container and is refused. Remove it or host this project manually.')
        if spec.get('security_opt') not in (None, [], ['no-new-privileges:true'], ['no-new-privileges']):
            raise ValueError(service + ': custom security options are refused.')
        if spec.get('storage_opt') and set(spec['storage_opt']) != {'size'}:
            raise ValueError(service + ': only a writable-layer size option is supported.')
        for key in ('ports', 'container_name', 'pull_policy'): spec.pop(key, None)
        build = spec.get('build')
        if build is not None:
            if isinstance(build, str): build = {'context': build}
            if not isinstance(build, dict) or set(build) - BUILD_FIELDS:
                raise ValueError(service + ': build supports a local context, Dockerfile, arguments and target only.')
            context = ci.inside(root, build.get('context', '.'))
            if not context.is_dir(): raise ValueError(service + ': build context must be a supplied directory.')
            if 'dockerfile_inline' not in build:
                ci.regular(ci.inside(context, build.get('dockerfile', 'Dockerfile')))
            build['context'] = str(context); spec['build'] = build
            spec['image'] = 'hosting-package-' + project.removeprefix('package-') + ':' + service.lower()
        elif not spec.get('image'):
            raise ValueError(service + ': supply a registry image or a Dockerfile build context.')
    for key, definition in (model.get('networks') or {}).items():
        definition = definition or {}; model.setdefault('networks', {})[key] = definition
        if key == 'hosting_ingress': raise ValueError('hosting_ingress is reserved for Caddy.')
        if definition.get('external') or definition.get('ipam') or definition.get('driver_opts') or definition.get('driver', 'bridge') != 'bridge':
            raise ValueError('Network ' + key + ': shared, external or custom-driver networks reach outside the project and are refused.')
        definition['name'] = project + '_' + key
    for key, definition in (model.get('volumes') or {}).items():
        definition = definition or {}; model['volumes'][key] = definition
        if definition.get('external') or definition.get('driver_opts') or definition.get('driver', 'local') != 'local':
            raise ValueError('Volume ' + key + ': external volumes and host driver options are refused.')
        definition['name'] = project + '_' + key
    # review() already confines all mounted/env input paths and rejects link crossings.
    # Root .env is implicit input and needs the same protection.
    env = root / '.env'
    if env.exists() or env.is_symlink():
        raw = ci.regular(env)
        if re.search(rb'^\s*(?:export\s+)?(?:COMPOSE_|DOCKER_)[A-Za-z0-9_]*\s*=', raw, re.M):
            raise ValueError('Remove Compose/Docker control variables from the application .env.')
    return model


def databases(model, root):
    """Recognised database services and their optional dumps/<service>.* input."""
    found = {}
    for service, spec in model['services'].items():
        engine = family(spec.get('image', ''))
        if engine in DATABASES and not spec.get('build'):
            found[service] = {'engine': engine, 'dump': None, 'restored': False}
    folder = root / 'dumps'
    if not folder.is_dir() or folder.is_symlink(): return found
    for path in sorted(folder.iterdir()):
        name = path.name
        suffix = next((s for s in DUMP_SUFFIXES if name.endswith(s)), None)
        service = name[:-len(suffix)] if suffix else None
        if service not in found:
            raise ValueError('dumps/' + name + ' does not name a MySQL, MariaDB or PostgreSQL service as dumps/<service>.sql, .sql.gz or .dump.')
        if path.is_symlink() or not path.is_file():
            raise ValueError('dumps/' + name + ' must be a regular file.')
        if found[service]['dump']: raise ValueError(service + ': supply one dump file only.')
        found[service]['dump'] = 'dumps/' + name
    return found


def credentials(engine, spec):
    """Administrative connection values from the resolved service environment; never logged."""
    env = spec.get('environment') or {}
    if isinstance(env, list): env = dict(item.split('=', 1) if '=' in item else (item, '') for item in env)
    env = {k: ('' if v is None else str(v)) for k, v in env.items()}
    if engine == 'postgres':
        user = env.get('POSTGRES_USER') or 'postgres'
        password = env.get('POSTGRES_PASSWORD', '')
        if not password:
            raise ValueError('The PostgreSQL service needs POSTGRES_PASSWORD in its environment or env_file for dumps and restore.')
        return {'user': user, 'password': password, 'database': env.get('POSTGRES_DB') or user}
    prefix = 'MARIADB' if engine == 'mariadb' else 'MYSQL'
    password = env.get(prefix + '_ROOT_PASSWORD') or env.get('MYSQL_ROOT_PASSWORD', '')
    database = env.get(prefix + '_DATABASE') or env.get('MYSQL_DATABASE', '')
    if not password or not database:
        raise ValueError('The ' + DATABASES[engine] + ' service needs a root password and a database name in its environment for dumps and restore.')
    return {'user': 'root', 'password': password, 'database': database}


def bounded(args, log, timeout, failure):
    """Bounded helper execution with root-private diagnostics; application values are never echoed."""
    with log.open('ab') as output:
        os.fchmod(output.fileno(), 0o600)
        proc = subprocess.Popen(['/usr/bin/prlimit', '--fsize=8388608:8388608', '--', *map(str, args)],
                                stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env={**ENV, 'COMPOSE_DISABLE_ENV_FILE': 'true'}, cwd='/', start_new_session=True)
        try: code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL); proc.wait()
            raise ValueError(failure + ' It timed out; its private diagnostic is retained. Retry the saved deployment.') from None
    if code: raise ValueError(failure + ' Its private diagnostic is retained; retry the saved deployment after review.')


def image_command(args, private, timeout):
    bounded(args, private / 'image.log', timeout, 'Image preparation failed. Check upstream access and the supplied Dockerfile.')


def prepare_images(args, model, state, private, row, step):
    """Build or pull every service image once; identities are recorded for retries."""
    images = state.setdefault('images', {})
    for service, spec in model['services'].items():
        if images.get(service):
            try:
                ca.docker(['image', 'inspect', images[service]]); continue
            except ValueError: pass  # Rebuild a missing image; the recorded identity is stale.
        if spec.get('build'):
            step('building ' + service + ' from its supplied Dockerfile')
            image_command([*args, 'build', '--pull', service], private, timeout=1800)
        else:
            step('pulling image for ' + service)
            image_command(['docker', 'pull', spec['image']], private, timeout=600)
        image = ca.docker(['image', 'inspect', spec['image']])[0]
        declared = {m['target'] for m in spec.get('volumes', [])}
        if set(image.get('Config', {}).get('Volumes') or {}) - declared:
            raise ValueError(service + ': its image declares storage absent from Compose. Declare a volume or bind for it so the data can be backed up.')
        images[service] = image['Id']; save(row, state)
    return images


def compile_runtime(row, root, report, state, step):
    project = json.loads(row['payload'])['project_name']; data = state['input']
    model = prepare_model(root, report, data, project)
    private = state_path(row['id']); definition = private / 'build.compose.json'
    atomic(definition, json.dumps(model))
    args = ['docker', 'compose', '--project-directory', str(root), '--project-name', project,
            '--env-file', str(root / '.env') if (root / '.env').exists() else '/dev/null', '--file', str(definition)]
    resolved = ci.run([*args, 'config', '--format', 'json'])
    if set(resolved['services']) != set(model['services']):
        raise ValueError('Compose resolution changed the service set.')
    state['databases'] = databases(resolved, root)
    for service, info in state['databases'].items():
        if info['dump']: credentials(info['engine'], resolved['services'][service])
    save(row, state)  # Images may already be prepared by the build lane; the database inventory must persist regardless.
    images = prepare_images(args, resolved, state, private, row, step)
    plan_volumes = {}
    for service, spec in resolved['services'].items():
        for mount in spec.get('volumes', []):
            if mount['type'] == 'bind':
                ci.inside(root, mount['source'])
                if mount.get('bind') and set(mount['bind']) - {'create_host_path'}:
                    raise ValueError(service + ': host bind propagation and labelling are refused.')
                mount.setdefault('bind', {})['create_host_path'] = False
            elif mount['type'] == 'volume':
                key = mount.get('source')
                definition = (resolved.get('volumes') or {}).get(key or '', {})
                if not key or not definition.get('name'):
                    raise ValueError(service + ': give every persistent volume a name so its data can be backed up.')
                plan_volumes[key] = {'name': definition['name'], 'external': False}
            elif mount['type'] != 'tmpfs':
                raise ValueError(service + ': this mount type reaches outside the project and is refused.')
        spec.pop('build', None); spec['image'] = images[service]
    sources = []
    paths = {root / Path(report['compose']).name}
    if (root / '.env').exists(): paths.add(root / '.env')
    for service, spec in model['services'].items():
        envfiles = spec.get('env_file', [])
        if isinstance(envfiles, (str, dict)): envfiles = [envfiles]
        for envfile in envfiles:
            paths.add(ci.inside(root, envfile['path'] if isinstance(envfile, dict) else envfile))
        build = spec.get('build')
        if build and 'dockerfile_inline' not in build:
            paths.add(Path(build['context']) / build.get('dockerfile', 'Dockerfile'))
    for path in sorted(paths):
        raw = ci.regular(path)
        sources.append(dict(ci.fingerprint(path, raw), file=str(path.relative_to(root))))
    for service, spec in resolved['services'].items():
        user = str(spec.get('user') or '')
        if not re.fullmatch('[0-9]+(?::[0-9]+)?', user): continue
        uid, _, gid = user.partition(':')
        for mount in spec.get('volumes', []):
            if mount['type'] != 'bind' or mount.get('read_only'): continue
            source = ci.inside(root, mount['source'])
            # An explicit numeric user owns its writable bind state. Never follow application symlinks.
            os.chown(source, int(uid), int(gid or uid), follow_symlinks=False)
            if source.is_dir():
                for folder, dirs, files in os.walk(source, followlinks=False):
                    for name in dirs + files:
                        os.chown(Path(folder) / name, int(uid), int(gid or uid), follow_symlinks=False)
    route = dict(domain=data['domain'], aliases=[], web_service=data['service'], internal_port=data['port'])
    services = [{'name': service, 'image': model['services'][service].get('image', ''),
                 'user': str(spec.get('user') or 'Image default'),
                 'database': DATABASES.get(state['databases'].get(service, {}).get('engine')),
                 'dump': state['databases'].get(service, {}).get('dump'),
                 'networks': list(spec.get('networks', {'default': {}}))} for service, spec in resolved['services'].items()]
    persistence = []
    for service, spec in resolved['services'].items():
        for m in spec.get('volumes', []):
            if m['type'] == 'tmpfs': continue
            persistence.append({'kind': 'Bind mount' if m['type'] == 'bind' else 'Named volume',
                                'source': m['source'] if m['type'] == 'bind' else plan_volumes[m['source']]['name'],
                                'target': m['target'], 'service': service, 'read_only': m.get('read_only', False)})
    summary = {'name': row['name'], 'project_name': project, 'route': route, 'sources': sources,
               'services': services, 'persistence': persistence}
    plan = {'name': row['name'], 'project_name': project, 'route': route, 'sources': sources,
            'summary': summary, 'model': resolved, 'stage': 'reviewed', 'original_containers': [],
            'volumes': plan_volumes, 'images': images, 'package_id': row['id']}
    ca.STORE.mkdir(mode=0o700, exist_ok=True)
    ca.plan_path(row['id']).mkdir(mode=0o700, exist_ok=True)
    ca.save(row, plan)


def readiness(engine, creds):
    """TCP loopback only: the images' initialization servers listen on their Unix socket alone."""
    if engine == 'postgres':
        return 'pg_isready', ['--host=127.0.0.1', '--username=' + creds['user']]
    admin = '"$(command -v mariadb-admin || command -v mysqladmin)"' if engine == 'mariadb' else 'mysqladmin'
    return 'sh', ['-c', admin + ' --host=127.0.0.1 --user=root ping']


def restore_command(engine, creds, filename):
    path = '/restore/' + filename
    if engine == 'postgres':
        target = '--host=127.0.0.1 --username=' + creds['user'] + ' --dbname=' + creds['database']
        if filename.endswith('.dump'):
            return 'pg_restore ' + target + ' --no-owner --no-privileges --single-transaction --exit-on-error ' + path
        reader = 'gzip -t ' + path + ' && gzip -dc ' + path if filename.endswith('.gz') else 'cat ' + path
        return reader + ' | psql ' + target + ' --set ON_ERROR_STOP=1 --single-transaction --quiet'
    tool = '"$(command -v mariadb || command -v mysql)"' if engine == 'mariadb' else 'mysql'
    reader = 'gzip -t ' + path + ' && gzip -dc ' + path if filename.endswith('.gz') else 'cat ' + path
    return reader + ' | ' + tool + ' --host=127.0.0.1 --user=root --database=' + creds['database']


def wait_ready(container, image, engine, creds, envfile, log, timeout=240):
    deadline = time.monotonic() + timeout
    entrypoint, command = readiness(engine, creds)
    args = ['docker', 'run', '--rm', '--network', 'container:' + container, '--env-file', str(envfile),
            '--log-driver', 'none', '--read-only', '--tmpfs', '/tmp', '--entrypoint', entrypoint, image, *command]
    while True:
        with log.open('ab') as output, open(os.devnull, 'rb') as stdin:
            os.fchmod(output.fileno(), 0o600)
            code = subprocess.run(['/usr/bin/prlimit', '--fsize=1048576:1048576', '--', *args], stdout=output,
                                  stderr=subprocess.STDOUT, stdin=stdin, env=ENV, cwd='/', timeout=60).returncode
        if code == 0: return
        if time.monotonic() >= deadline:
            raise ValueError('The database service did not accept connections in time; its private diagnostic is retained.')
        time.sleep(3)


def restore_databases(host, row, plan, args, step):
    """Called by the adoption runtime before the full project start; runs each restore once."""
    state = load(row)
    if state.get('restore_from'):
        from .site_backup import restore_volumes
        step('restoring captured volumes')
        restore_volumes(row, plan, state)
        state = load(row)
    pending = {s: d for s, d in state.get('databases', {}).items() if d.get('dump') and not d.get('restored')}
    if not pending: return
    root = SITES / row['name']; private = state_path(row['id']); log = private / 'restore.log'
    step('starting database services for restore')
    ca.docker([*args, 'up', '--detach', '--wait', '--wait-timeout', '240', '--pull', 'never', '--no-build', *sorted(pending)],
              raw=True, timeout=300)
    for service in sorted(pending):
        engine = pending[service]['engine']; spec = plan['model']['services'][service]
        containers = [c for c in ca.project_containers(plan)
                      if c['Config']['Labels'].get('com.docker.compose.service') == service and c['State']['Running']]
        if len(containers) != 1: raise ValueError(service + ': expected one running database container for restore.')
        creds = credentials(engine, spec)
        dump = ci.inside(root, pending[service]['dump'])
        if dump.is_symlink() or not dump.is_file(): raise ValueError(service + ': dump file changed; keep it for review.')
        envfile = private / ('restore-' + service + '.env')
        atomic(envfile, ('PGPASSWORD=' if engine == 'postgres' else 'MYSQL_PWD=') + creds['password'] + '\n')
        try:
            step('waiting for ' + service + ' to accept connections')
            wait_ready(containers[0]['Id'], plan['images'][service], engine, creds, envfile, log)
            step('restoring ' + service + ' from ' + pending[service]['dump'])
            name = 'hosting-restore-' + row['id'] + '-' + service.lower()
            if host.inspect(name): ca.docker(['rm', '--force', name], raw=True)
            bounded(['docker', 'run', '--rm', '--name', name, '--label', 'hosting.package=' + row['id'],
                     '--network', 'container:' + containers[0]['Id'], '--env-file', str(envfile), '--log-driver', 'none',
                     '--read-only', '--tmpfs', '/tmp', '--mount', 'type=bind,src=' + str(dump) + ',dst=/restore/' + dump.name + ',readonly',
                     '--entrypoint', 'sh', plan['images'][service], '-c', 'set -o pipefail 2>/dev/null; ' + restore_command(engine, creds, dump.name)],
                    log, 900, service + ': the database restore failed.')
        finally: envfile.unlink(missing_ok=True)
        state['databases'][service].update(restored=True, restored_at=time.time()); save(row, state)


def dump_target(row):
    """The one recognised database service of a package site, for scheduled native dumps."""
    try: state = load(row)
    except (OSError, ValueError): return None
    found = [(s, d) for s, d in state.get('databases', {}).items() if d.get('engine') in DATABASES]
    if len(found) != 1 or not (ca.plan_path(row['id']) / 'plan.json').exists(): return None
    service, info = found[0]; plan = ca.read(row)
    spec = plan['model']['services'].get(service, {})
    try: creds = credentials(info['engine'], spec)
    except ValueError: return None
    return {'service': service, 'engine': info['engine'], 'image_id': plan['images'].get(service),
            'image': next((s['image'] for s in plan['summary']['services'] if s['name'] == service), ''),
            'project': plan['project_name'], 'user': str(spec.get('user') or ''), **creds}


def inputs(row):
    """Immutable worker copy and its structural review; refused packages never reach extraction."""
    state = load(row); package = worker_copy(row, state)
    report = review(package, state['input'])
    reason = eligibility(report)
    if reason: raise ValueError(reason)
    return state, package, report


def project_root(row, state):
    return SITES / ('.package-' + row['id']) if state['stage'] in ('reserved', 'extracted') else SITES / row['name']


def build_images(host, row, step):
    """Image preparation only: runs outside the serial site operation, touches no site state."""
    state, package, report = inputs(row)
    if state['stage'] == 'reserved': raise ValueError('Extract the package before preparing its images.')
    root = project_root(row, state); private = state_path(row['id'])
    project = json.loads(row['payload'])['project_name']
    model = prepare_model(root, report, state['input'], project)
    definition = private / 'build.compose.json'; atomic(definition, json.dumps(model))
    args = ['docker', 'compose', '--project-directory', str(root), '--project-name', project,
            '--env-file', str(root / '.env') if (root / '.env').exists() else '/dev/null', '--file', str(definition)]
    resolved = ci.run([*args, 'config', '--format', 'json'])
    prepare_images(args, resolved, state, private, row, step)
    state['images_ready'] = True; save(row, state)


def extract_stage(row, state, package, report, step):
    if state['stage'] != 'reserved': return
    root = SITES / row['name']
    stage = SITES / ('.package-' + row['id'])
    if not stage.exists():
        if state.get('folder_identity'): raise ValueError('Staging folder disappeared; keep the operation for recovery.')
        stage.mkdir(mode=0o700)
    elif not state.get('folder_identity'):
        raise ValueError('An unowned staging folder exists; it has been kept for review.')
    trusted(stage, directory=True)
    identity = [stage.stat().st_dev, stage.stat().st_ino]
    if state.get('folder_identity') and state['folder_identity'] != identity:
        raise ValueError('Staging folder changed; preserve it for review.')
    state['folder_identity'] = identity; save(row, state)
    if root.exists() or root.is_symlink(): raise ValueError('Site folder appeared before extraction; it has been kept.')
    # This directory has never been exposed to containers; retry only replays extraction.
    for path in stage.iterdir():
        if path.is_dir() and not path.is_symlink(): shutil.rmtree(path)
        else: path.unlink()
    step('extracting supplied project files under the site quota')
    apply_quota(stage, row['project'], json.loads(row['payload'])['data_mb'])
    extract(package, stage, report)
    prepare_model(stage, report, state['input'], json.loads(row['payload'])['project_name'])
    state['stage'] = 'extracted'; save(row, state)


def perform(host, row, step):
    private = state_path(row['id']); root = SITES / row['name']
    preflight(); state, package, report = inputs(row)
    extract_stage(row, state, package, report, step)
    if state['stage'] == 'extracted':
        stage = SITES / ('.package-' + row['id'])
        if not root.exists():
            if state.get('site_identity'): raise ValueError('Reserved destination disappeared; preserve the operation.')
            root.mkdir(mode=0o700)
            state['site_identity'] = [root.stat().st_dev, root.stat().st_ino]; save(row, state)
        trusted(root, directory=True)
        if [root.stat().st_dev, root.stat().st_ino] != state.get('site_identity'):
            raise ValueError('Destination folder changed; preserve it for review.')
        # XFS refuses moving a project-inheriting directory into a parent with a different
        # project. Create the destination first, assign the same quota and move its contents.
        apply_quota(root, row['project'], json.loads(row['payload'])['data_mb'])
        if stage.exists():
            trusted(stage, directory=True)
            if [stage.stat().st_dev, stage.stat().st_ino] != state['folder_identity']:
                raise ValueError('Prepared staging folder changed; preserve it for review.')
            for path in stage.iterdir():
                target = root / path.name
                if target.exists() or target.is_symlink():
                    raise ValueError('Destination contains conflicting files; both copies have been kept.')
                path.rename(target)
            stage.rmdir()
        verify_extracted(package, root, report)
        state['stage'] = 'files ready'; save(row, state)
    trusted(root, directory=True)
    if [root.stat().st_dev, root.stat().st_ino] != state['site_identity']:
        raise ValueError('Site folder was replaced; do not overwrite its data.')
    if not (ca.plan_path(row['id']) / 'plan.json').exists():
        compile_runtime(row, root, report, state, step)
    state = load(row); state['stage'] = 'runtime prepared'; save(row, state)
    ca.perform(host, row, step, before_up=lambda plan, args: restore_databases(host, row, plan, args, step))
    state = load(row); state['stage'] = 'published'; save(row, state)


def verify_extracted(package, root, report):
    archive = Archive(package); prefix = posixpath.dirname(report['compose'])
    try:
        for name, entry in archive.entries.items():
            relative = posixpath.relpath(name, prefix) if prefix else name
            if relative == '.': continue
            target = root / relative
            if entry['kind'] == 'directory':
                if not target.is_dir() or target.is_symlink(): raise ValueError('A supplied directory is missing after extraction.')
            elif entry['kind'] == 'link':
                linked = posixpath.relpath(entry['target'], prefix) if prefix else entry['target']
                if not target.is_symlink() or os.readlink(target) != posixpath.relpath(linked, posixpath.dirname(relative) or '.'):
                    raise ValueError('A supplied link changed during extraction.')
            else:
                member = entry['member']
                source = archive.archive.open(member) if isinstance(archive.archive, zipfile.ZipFile) else archive.archive.extractfile(member)
                with source: expected = hashlib.file_digest(source, 'sha256').hexdigest()
                if not target.is_file() or target.is_symlink(): raise ValueError('A supplied file is missing after extraction.')
                with target.open('rb') as current: observed = hashlib.file_digest(current, 'sha256').hexdigest()
                if expected != observed: raise ValueError('A supplied file changed during extraction.')
    finally: archive.close()


def public(row):
    if (ca.plan_path(row['id']) / 'plan.json').exists():
        result = ca.public(row)
        try: result['databases'] = load(row).get('databases', {})
        except (OSError, ValueError): result['databases'] = {}
        return result
    state = load(row); data = state['input']
    return {'project_name': json.loads(row['payload'])['project_name'], 'services': [], 'persistence': [], 'databases': {},
            'stage': row['step'], 'route': {'web_service': data['service'], 'internal_port': data['port']}}
