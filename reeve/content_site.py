"""Root orchestration; application code and archive parsing run inside site-UID containers."""
import errno
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

import yaml

from .content_jobs import UPLOAD_LIMIT, relative, validate_content
from .host import ENV, OPS, SITES, atomic, command, trusted, verify_quota

SPOOL = OPS / 'panel/web/uploads'
OUTPUT = OPS / 'panel/worker/content-output'
COMPOSER = OPS / 'panel/worker/composer'
RESCUE = OPS / 'panel/worker/content-rescue'


class ContentFailed(ValueError):
    pass


def workspace(row, ident):
    from .core import request_id
    request_id(ident)
    root = SITES / row['name']
    trusted(root, directory=True)
    marker = root / '.hosting-operation'; trusted(marker)
    if marker.read_text().strip() != row['id']: raise ValueError('Content directory belongs to another operation')
    if (RESCUE / ident).exists():
        work = RESCUE / ident; trusted(work, directory=True); trusted(work / 'site.json')
        if json.loads((work / 'site.json').read_text())['site_id'] != row['id']: raise ValueError('Recovery workspace belongs to another site')
        return work
    existing = root / '.tools' / ident
    if existing.exists():
        trusted(root / '.tools', directory=True); trusted(existing, directory=True)
        verify_quota(existing, row['project'], json.loads(row['payload'])['data_mb'])
        return existing
    try:
        quota = verify_quota(root, row['project'], json.loads(row['payload'])['data_mb'])
        if quota['hard_bytes'] - quota['used_bytes'] < 65536:
            raise OSError(errno.ENOSPC, 'Site quota is full')
        tools = root / '.tools'; tools.mkdir(mode=0o700, exist_ok=True); trusted(tools, directory=True)
        work = tools / ident; work.mkdir(mode=0o700, exist_ok=True); trusted(work, directory=True)
        verify_quota(work, row['project'], json.loads(row['payload'])['data_mb'])
        return work
    except OSError as exc:
        if exc.errno not in (errno.ENOSPC, errno.EDQUOT): raise
        # Tiny control metadata can live outside a full site quota. Recovery gets
        # a READ-ONLY temporary directory, so it cannot bypass the site's data cap.
        RESCUE.mkdir(mode=0o700, exist_ok=True); trusted(RESCUE, directory=True)
        work = RESCUE / ident; work.mkdir(mode=0o700, exist_ok=True); trusted(work, directory=True)
        atomic(work / 'site.json', json.dumps({'site_id': row['id']}))
        return work



def prepare(row, ident, kind, data):
    if kind in ('web-settings','site-rules','php-settings','sftp-access','fix-ownership','mail-senders','database-usage'): return
    if kind == 'sql':
        from .database_site import credentials
        credentials(row)
    work = workspace(row, ident)
    if work.parent == RESCUE and (kind != 'tool' or data['tool'] != 'shell' or data['internet']):
        cleanup(row, ident)
        raise ValueError('Site quota is full; use Shell without outgoing network access to free content space first')
    if kind == 'tool': return
    # The web process may control its spool. Copy a bounded, open regular file;
    # verify its digest before committing the job. Never rename web-owned data
    # into a trusted root directory or trust its path after opening it.
    spoolfd = os.open(SPOOL, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: fd = os.open(ident, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=spoolfd)
    finally: os.close(spoolfd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != pwd.getpwnam('hosting-web').pw_uid or info.st_size != data['size']:
            raise ValueError('Invalid staged upload')
        input_file = work / 'input'
        if input_file.exists(): trusted(input_file); input_file.unlink()
        h = hashlib.sha256(); total = 0
        with os.fdopen(os.dup(fd), 'rb') as source, input_file.open('xb') as target:
            os.fchmod(target.fileno(), 0o444)
            while chunk := source.read(min(1048576, data['size'] - total + 1)):
                total += len(chunk)
                if total > data['size'] or total > UPLOAD_LIMIT: raise ValueError('Upload grew while being claimed')
                h.update(chunk); target.write(chunk)
            target.flush(); os.fsync(target.fileno())
        if total != data['size'] or h.hexdigest() != data['sha256']: raise ValueError('Upload changed or was truncated; upload again')
    except BaseException:
        cleanup(row, ident)
        raise
    finally: os.close(fd)


def tools_image():
    context = Path(__file__).resolve().parent.parent / 'templates/content-tools'
    recipe = hashlib.sha256((context / 'Containerfile').read_bytes()).hexdigest()[:16]
    tag = 'hosting-content-tools:' + recipe
    if not command(['docker', 'image', 'ls', '-q', tag]).strip():
        command(['docker', 'build', '-f', context / 'Containerfile', '-t', tag, context], timeout=600)
    return command(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']).strip()


def mount(args, source, target, readonly=True):
    # Source is always a root-selected path, never browser text.
    args.extend(['--mount', f'type=bind,src={source},dst={target}' + (',readonly' if readonly else '')])


def base_args(row, ident, work, network='none'):
    data = json.loads(row['payload'])
    args = ['docker', 'run', '--name', 'hosting-tool-' + ident, '--label', 'hosting.content=' + ident,
            '--user', f"{row['uid']}:{row['uid']}", '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--read-only', '--network', network, '--log-driver', 'none', '--pids-limit', str(data.get('pids_limit') or -1),
            '--storage-opt', 'size=' + (str(data['layer_mb']) + 'm' if data.get('layer_mb') else '0'),
            '--env', 'HOME=/tmp', '--env', 'LANG=C.UTF-8', '--workdir', '/site']
    if data.get('memory_mb'): args.extend(['--memory', str(data['memory_mb']) + 'm'])
    if data.get('cpus'): args.extend(['--cpus', str(data['cpus'])])
    tmp = work / 'tmp'
    if not tmp.exists(): tmp.mkdir(mode=0o700); os.chown(tmp, row['uid'], row['uid'])
    mount(args, tmp, '/tmp', work.parent == RESCUE)
    return args


def execute(args, timeout=900, output_limit=65536):
    # Docker 29 drops --pids-limit=-1 during create, as it also does with
    # Compose. Apply the explicit setting before any uploaded command starts.
    name = args[args.index('--name') + 1]
    command(['docker', 'create', *args[2:]])
    command(['docker', 'update', '--pids-limit', args[args.index('--pids-limit') + 1], name])
    attach = ['docker', 'start', '--attach', name]
    # Bound attachment output; no Docker log file. Always remove the container in
    # the caller, including a disconnected/killed Docker CLI or timeout.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(['/usr/bin/prlimit', '--fsize=1048576:1048576', '--', *map(str, attach)],
                                stdout=output, stderr=subprocess.STDOUT, env=ENV, cwd='/', start_new_session=True)
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL); proc.wait(); code = 124
        output.seek(0); raw = output.read(output_limit + 1)
    text = raw[:output_limit].decode(errors='replace')
    if len(raw) > output_limit: text += '\n[Output truncated at 64 KiB]'
    if code == 124: text += '\n[Operation timed out; inspect partial changes before another run]'
    else:
        state = json.loads(command(['docker', 'inspect', name, '--format', '{{json .State}}']))
        if state['Running']: raise RuntimeError('Tool attachment ended before its container stopped; inspect recovery')
        code = code or state['ExitCode']
    return code, text


def cleanup(row, ident):
    name = 'hosting-tool-' + ident
    found = command(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']).strip()
    if found:
        owner = command(['docker', 'inspect', name, '--format', '{{index .Config.Labels "hosting.content"}}']).strip()
        if owner != ident: raise ValueError('Unmanaged tools container collision')
        command(['docker', 'rm', '-f', name])
    network = 'hosting-tool-net-' + ident
    if command(['docker', 'network', 'ls', '-q', '--filter', 'name=^' + network + '$']).strip():
        owner = command(['docker', 'network', 'inspect', network, '--format', '{{index .Labels "hosting.content"}}']).strip()
        if owner != ident: raise ValueError('Unmanaged tools network collision')
        command(['docker', 'network', 'rm', network])
    for work in (SITES / row['name'] / '.tools' / ident, RESCUE / ident):
        if work.exists():
            trusted(work, directory=True)
            # shutil's fd-based deletion does not follow application-created symlinks in tmp.
            if not shutil.rmtree.avoids_symlink_attacks: raise RuntimeError('Safe descriptor-based cleanup required')
            shutil.rmtree(work)



def file_call(row, action, data, ident, work):
    request = dict(data, action=action)
    request['expanded_limit'] = min(json.loads(row['payload'])['data_mb'] * 1048576, 4 * 1024**3)
    atomic(work / 'request.json', json.dumps(request), 0o444)
    args = base_args(row, ident, work)
    mount(args, SITES / row['name'] / 'html', '/site', action in ('list', 'read'))
    mount(args, Path(__file__).with_name('content_helper.py'), '/helper.py')
    mount(args, work / 'request.json', '/request.json')
    if action not in ('list', 'read'): mount(args, work / 'input', '/input')
    args.extend(['--entrypoint', 'python3', tools_image(), '/helper.py'])
    return execute(args, timeout=300, output_limit=800000 if action in ('list', 'read') else 65536)


def inspect_files(row, action, path):
    if row['state'] != 'succeeded' or action not in ('list', 'read'): raise ValueError('Unsupported file inspection')
    relative(path, root=action == 'list')
    ident = str(uuid.uuid4()); work = workspace(row, ident)
    try:
        code, output = file_call(row, action, {'path': path}, ident, work)
        if code: raise ValueError(output[:1000])
        return json.loads(output)
    finally: cleanup(row, ident)


def composer(branch):
    """Cache an immutable, official SHA-256-checked PHAR; never execute its installer as root."""
    from urllib.request import urlopen
    channel = '2.2' if tuple(map(int, branch.split('.'))) < (7, 2) else 'stable'
    COMPOSER.mkdir(mode=0o700, exist_ok=True); trusted(COMPOSER, directory=True)
    record = COMPOSER / (channel + '.json')
    if record.exists():
        trusted(record); saved = json.loads(record.read_text())
        file = COMPOSER / (saved['sha256'] + '.phar'); trusted(file)
        if hashlib.sha256(file.read_bytes()).hexdigest() != saved['sha256']: raise ValueError('Cached Composer checksum mismatch')
        return file
    url = 'https://getcomposer.org/download/latest-' + ('2.2.x' if channel == '2.2' else 'stable') + '/composer.phar'
    with urlopen(url + '.sha256sum', timeout=30) as response: checksum = response.read(1024).decode().split()[0]
    if not re.fullmatch('[0-9a-f]{64}', checksum): raise ValueError('Invalid official Composer checksum')
    with urlopen(url, timeout=30) as response: raw = response.read(8 * 1048576 + 1)
    if len(raw) > 8 * 1048576 or hashlib.sha256(raw).hexdigest() != checksum: raise ValueError('Composer download checksum mismatch')
    file = COMPOSER / (checksum + '.phar')
    with file.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o444); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    atomic(record, json.dumps({'channel': channel, 'sha256': checksum, 'source': url}))
    return file


def application_args(row, ident, work, data):
    root = SITES / row['name']; metadata = yaml.safe_load((root / 'hosting.yaml').read_text())
    is_php = json.loads(row['payload']).get('runtime') == 'php'
    from .database_site import state
    db = state(row)
    backend = 'hosting-backend-' + row['name'] if is_php or db else 'none'
    network = backend
    if data['internet']:
        network = 'hosting-tool-net-' + ident
        command(['docker', 'network', 'create', '--label', 'hosting.content=' + ident, network])
    args = base_args(row, ident, work, network)
    if data['internet'] and backend != 'none': args.extend(['--network', backend])
    mount(args, root / 'html', '/site', False)
    args.extend(['--workdir', '/site' + ('/' + data['path'] if data['path'] != '.' else '')])
    image = metadata['php_image']['id'] if is_php else tools_image()
    if is_php:
        from .requests_site import cli_access
        cli_access(args,root)
        branch = metadata['php_runtime']['branch'] if 'branch' in metadata['php_runtime'] else metadata['php_version']
        mount(args, root / 'conf/php.ini', '/etc/php/' + branch + '/cli/conf.d/99-hosting.ini')
        from .mail import SHIM
        if SHIM.exists(): mount(args, SHIM, '/usr/local/bin/hosting-sendmail')
        args.extend(['--env-file', str(root / '.env')])
        if db: args.extend(['--env-file', str(root / '.database-app.env')])
    tool = data['tool']; words = shlex.split(data['arguments']) if tool != 'shell' else []
    if tool == 'composer':
        mount(args, composer(branch), '/composer.phar')
        args.extend(['--env', 'COMPOSER_HOME=/tmp/composer', '--env', 'COMPOSER_CACHE_DIR=/tmp/composer-cache'])
        argv = ['php', '/composer.phar', '--no-interaction', *words]
    elif tool == 'wp':
        from .wp_cli import phar
        mount(args, phar(), '/wp-cli.phar')
        args.extend(['--env', 'WP_CLI_CACHE_DIR=/tmp/wp-cli-cache'])
        argv = ['php', '/wp-cli.phar', *words]
    elif tool == 'console': argv = ['php', 'bin/console', *words]
    elif tool == 'php': argv = ['php', *words]
    else: argv = ['sh', '-c', data['arguments']]
    args.extend(['--entrypoint', argv[0], image, *argv[1:]])
    return args


def sql_args(row, ident, work):
    from .database_site import state
    db = state(row)
    if not db or db['stage'] != 'ready': raise ValueError('Database is not ready')
    args = base_args(row, ident, work, 'hosting-backend-' + row['name'])
    # Use the exact server image for its matching client, but mount no server data,
    # administrator credentials or content. Override declared volumes explicitly.
    empty = work / 'empty'; empty.mkdir(mode=0o755); os.chmod(empty, 0o755)
    mount(args, empty, db['mount'])
    mount(args, work / 'input', '/input.sql')
    if db['engine'] == 'postgres':
        atomic(work / 'pgpass', 'db:5432:site:site:' + db['app_password'] + '\n', 0o600)
        os.chown(work / 'pgpass', row['uid'], row['uid'])
        mount(args, work / 'pgpass', '/pgpass')
        args.extend(['--env', 'PGPASSFILE=/pgpass'])
        argv = ['psql', '-X', '-h', 'db', '-U', 'site', '-d', 'site', '-v', 'ON_ERROR_STOP=1', '-f', '/input.sql']
    else:
        atomic(work / 'client.cnf', '[client]\nhost=db\nuser=site\ndatabase=site\npassword=' + db['app_password'] + '\n', 0o444)
        mount(args, work / 'client.cnf', '/client.cnf')
        # Fixed shell text, no browser/SQL interpolation. Application account only.
        client = 'mariadb' if db['engine'] == 'mariadb' else 'mysql'
        argv = ['sh', '-c', client + ' --defaults-extra-file=/client.cnf --binary-mode --local-infile=0 < /input.sql']
    args.extend(['--entrypoint', argv[0], db['image_id'], *argv[1:]])
    return args


def output(ident):
    from .core import request_id
    request_id(ident)
    path = OUTPUT / (ident + '.txt')
    if not path.exists(): return 'No output recorded yet.'
    trusted(path)
    return path.read_text()[:66000]


def perform(row, job, step):
    ident = job['id']; data = validate_content(job['kind'], json.loads(job['payload']))
    work = workspace(row, ident)
    code, text = 1, 'Operation could not start.'
    try:
        step('running as site identity')
        if job['kind'] == 'tool': code, text = execute(application_args(row, ident, work, data))
        elif job['kind'] == 'sql': code, text = execute(sql_args(row, ident, work), timeout=1800)
        else: code, text = file_call(row, job['kind'], data, ident, work)
    except Exception as exc:
        text = str(exc)[:2000]
    finally:
        # Keep output private and out of ordinary site/API diagnostics.
        from .database_site import state
        db = state(row)
        if db:
            for key in ('app_password', 'admin_password'): text = text.replace(db[key], '[redacted]')
        OUTPUT.mkdir(mode=0o700, exist_ok=True); trusted(OUTPUT, directory=True)
        atomic(OUTPUT / (ident + '.txt'), text)
        cleanup(row, ident)
    if code: raise ContentFailed('Operation failed; open its output and inspect any partial changes before submitting again')


def recover(ledger):
    from .core import request_id
    # Stop interrupted helpers before allowing any new mutation; never replay them.
    jobs = {j['id']: j for j in ledger.content_jobs()}
    for row in ledger.list():
        folder = SITES / row['name'] / '.tools'
        if not folder.exists(): continue
        trusted(folder, directory=True)
        for child in folder.iterdir():
            from .core import request_id
            request_id(child.name)
            job = jobs.get(child.name)
            if not job or job['state'] != 'queued': cleanup(row, child.name)

    if RESCUE.exists():
        trusted(RESCUE, directory=True)
        for child in RESCUE.iterdir():
            request_id(child.name); trusted(child, directory=True); trusted(child / 'site.json')
            job = jobs.get(child.name)
            if not job or job['state'] != 'queued':
                cleanup(ledger.get(json.loads((child / 'site.json').read_text())['site_id']), child.name)
