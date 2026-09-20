"""Backup destinations from the panel: connect any number of restic repositories and hold their state.

A destination is an SFTP server (host, port, username, absolute path; authenticated with the server's
own uploader key, shown on the page so the operator can authorise it, or a password), an Amazon S3
bucket (bucket, region, optional prefix, an access key pair), or a folder this server can reach (a
disk here, a mounted share). Each gets a name and its own root-private folder under
`remote_backup.DESTINATIONS` holding its configuration and credentials. The repository password is
made here and shown once, to be kept outside the server, or an existing repository's password is
supplied to adopt it. Connecting pins an SFTP host key (`ssh-keyscan`, fingerprints shown), writes
every secret as a private root file, opens or initialises the repository and records its identity;
only then does the folder become a destination, so a failed attempt leaves nothing behind. Pause,
resume and disconnect are per destination. The recovery card carries every destination.
"""
import json
import os
import re
import secrets
import shutil
import time
import uuid
from pathlib import Path

from .host import atomic, command, trusted
from . import remote_backup as remote
from .remote_backup import DESTINATIONS, RemoteFailed, execute, NAME, LOCAL_PATH

SECRETS = Path('/srv/ops/panel/worker/remote-secrets')   # the server's own uploader key, shared by every SFTP destination
HOST = re.compile(r'[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?')
USER = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}')
PATH_RE = re.compile(r'/[A-Za-z0-9_./-]*')
REGION = re.compile(r'[a-z]{2}-[a-z]+-[0-9]')
BUCKET = re.compile(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]')
PREFIX = re.compile(r'[A-Za-z0-9_./-]{0,200}')
SECRET_FILES = ('restic-password', 'known_hosts', 'ssh-password', 'ssh-askpass', 'aws.json')


def field(form, name, pattern=None, required=True, default=''):
    value = str(form.get(name, default) or default).strip()
    if not value:
        if required: raise ValueError(f'{name.replace("_", " ").capitalize()} is required')
        return ''
    if pattern and not pattern.fullmatch(value): raise ValueError(f'{name.replace("_", " ").capitalize()} is not valid')
    if '\n' in value or '\x00' in value: raise ValueError(f'{name.replace("_", " ").capitalize()} is not valid')
    return value


def validate(form):
    if not isinstance(form, dict): raise ValueError('Destination settings are needed')
    kind = field(form, 'type')
    existing = str(form.get('existing_password', '') or '')
    if '\n' in existing or len(existing) > 1024: raise ValueError('The repository password is not valid')
    name = field(form, 'name', NAME, required=False)
    if kind == 'sftp':
        port = field(form, 'port', re.compile(r'[0-9]{1,5}'), required=False, default='22')
        if not 1 <= int(port) <= 65535: raise ValueError('Port must be 1 to 65535')
        auth = field(form, 'auth', re.compile(r'key|password'), required=False, default='key')
        password = str(form.get('password', '') or '')
        if auth == 'password' and (not password or '\n' in password or len(password) > 1024): raise ValueError('A password is required for password authentication')
        return {'type': 'sftp', 'name': name, 'host': field(form, 'host', HOST), 'port': int(port), 'username': field(form, 'username', USER),
                'path': field(form, 'path', PATH_RE), 'auth': auth, 'password': password if auth == 'password' else '', 'existing_password': existing}
    if kind == 's3':
        return {'type': 's3', 'name': name, 'bucket': field(form, 'bucket', BUCKET), 'region': field(form, 'region', REGION), 'prefix': field(form, 'prefix', PREFIX, required=False).strip('/'),
                'access_key_id': field(form, 'access_key_id', re.compile(r'[A-Z0-9]{16,128}')), 'secret_access_key': field(form, 'secret_access_key', re.compile(r'[A-Za-z0-9/+=]{16,256}')), 'existing_password': existing}
    if kind == 'local':
        path = field(form, 'path', LOCAL_PATH)
        if '/../' in path + '/': raise ValueError('Path is not valid')
        return {'type': 'local', 'name': name, 'path': path.rstrip('/') or '/', 'existing_password': existing}
    raise ValueError('Choose SFTP, Amazon S3 or a folder')


def server_key():
    """The server's own uploader key; its public half is what an SFTP destination account authorises."""
    SECRETS.mkdir(mode=0o700, exist_ok=True); trusted(SECRETS, directory=True)
    key = SECRETS / 'id_ed25519'
    if not key.exists():
        command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'reeve uploader', '-f', key])
        os.chmod(key, 0o600); os.chmod(key.with_suffix('.pub'), 0o600)
    trusted(key)
    return key.with_suffix('.pub').read_text().strip()


def private_file(path, text):
    atomic(path, text, 0o600)


def keyscan(host, port):
    """One probe for the modern key type, one more for RSA only if needed: a modern sshd penalises a
    source that keeps connecting without authenticating, and each key type is its own connection."""
    for kind in ('ed25519', 'rsa'):
        try: output = command(['ssh-keyscan', '-T', '10', '-t', kind, '-p', str(port), host])
        except RuntimeError as exc: raise ValueError('The destination did not answer the host key probe; check the host and port, then wait a minute (a modern sshd holds off a source that probed it too often): ' + str(exc)[:200]) from None
        lines = [l for l in output.splitlines() if l.strip() and not l.startswith('#')]
        if lines: return '\n'.join(sorted(lines)) + '\n'
    raise ValueError('The destination did not answer with a host key; check the host and port')


def fingerprints(known_hosts):
    known_hosts = Path(known_hosts)
    if not known_hosts.exists(): return []
    trusted(known_hosts)
    try: return [line.strip() for line in command(['ssh-keygen', '-lf', known_hosts]).splitlines() if line.strip()]
    except RuntimeError: return []


def repository_id(config):
    return json.loads(execute(config, ['cat', 'config']))['id']


def candidate_folder():
    """A new destination is built in a hidden folder and renamed into place only once its repository opened."""
    DESTINATIONS.mkdir(mode=0o700, parents=True, exist_ok=True); trusted(DESTINATIONS, directory=True)
    ident = str(uuid.uuid4()); home = DESTINATIONS / ('.candidate-' + ident)
    home.mkdir(mode=0o700)
    return ident, home


def finish(ident, home, config):
    """The candidate becomes the destination: its files are already in place, only its folder name changes."""
    config.update(id=ident, created=time.time())
    for key in ('password_file', 'known_hosts_file', 'ssh_password_file', 'ssh_askpass_file', 'aws_credentials_file'):
        if key in config: config[key] = str(DESTINATIONS / ident / Path(config[key]).name)
    atomic(home / 'config.json', json.dumps(config, indent=2), 0o600)
    home.rename(DESTINATIONS / ident)


def connect(form):
    """Write a candidate set of secrets and configuration, open or initialise the repository, then make it a destination."""
    data = validate(form)
    ident, home = candidate_folder()
    try: return _connect(data, ident, home)
    except Exception:
        if home.exists(): shutil.rmtree(home)
        raise


def _connect(data, ident, home):
    generated = not data['existing_password']
    password = data['existing_password'] or secrets.token_hex(32)
    private_file(home / 'restic-password', password + '\n')
    config = {'enabled': True, 'timeout_seconds': 600, 'password_file': str(home / 'restic-password'), 'type': data['type']}
    if data['name']: config['name'] = data['name']
    result = {'type': data['type'], 'generated_password': password if generated else None}
    if data['type'] == 'sftp':
        private_file(home / 'known_hosts', keyscan(data['host'], data['port']))
        config.update(repository=f"sftp://{data['username']}@{data['host']}:{data['port']}//{data['path'].lstrip('/')}", known_hosts_file=str(home / 'known_hosts'))
        if data['auth'] == 'password':
            private_file(home / 'ssh-password', data['password'] + '\n')
            # The helper reads the password beside itself, so it works from the candidate folder and the final one.
            atomic(home / 'ssh-askpass', '#!/bin/sh\nexec cat "$(dirname "$0")/ssh-password"\n', 0o700)
            config.update(ssh_password_file=str(home / 'ssh-password'), ssh_askpass_file=str(home / 'ssh-askpass'))
        else:
            server_key(); config['ssh_key_file'] = str(SECRETS / 'id_ed25519')
        result['fingerprints'] = fingerprints(home / 'known_hosts')
    elif data['type'] == 's3':
        private_file(home / 'aws.json', json.dumps({'AWS_ACCESS_KEY_ID': data['access_key_id'], 'AWS_SECRET_ACCESS_KEY': data['secret_access_key'], 'AWS_DEFAULT_REGION': data['region']}))
        config.update(repository=f"s3:https://s3.{data['region']}.amazonaws.com/{data['bucket']}" + (f"/{data['prefix']}" if data['prefix'] else ''), aws_credentials_file=str(home / 'aws.json'))
    else:
        config['repository'] = data['path']
        folder = Path(data['path'])
        if folder.exists() and not folder.is_dir(): raise ValueError('That path is not a folder')
        if not folder.exists():
            if not folder.parent.is_dir(): raise ValueError('The folder\'s parent does not exist; mount or create it first')
            folder.mkdir(mode=0o700)
    atomic(home / 'config.json', json.dumps(config, indent=2), 0o600)
    checked = remote.load(home / 'config.json', require_id=False)
    try: repo = repository_id(checked)
    except RemoteFailed:
        if not generated: raise ValueError('The repository could not be opened with that password; check the password, the address and the credentials') from None
        execute(checked, ['init', '--json'])
        repo = repository_id(checked)
    config['repository_id'] = repo
    for other in remote.destinations(require_id=False):
        if other.get('repository_id') == repo: raise ValueError('That repository is already connected as ' + other['name'])
    finish(ident, home, config)
    result.update(id=ident, name=config.get('name') or remote.load(DESTINATIONS / ident / 'config.json')['name'], repository=config['repository'], repository_id=repo)
    return result


def set_enabled(ident, enabled):
    config = remote.destination(ident, require_id=False)
    raw = json.loads(Path(config['path']).read_text()); raw['enabled'] = bool(enabled)
    atomic(Path(config['path']), json.dumps(raw, indent=2), 0o600)
    return {'id': ident, 'enabled': raw['enabled']}


def disconnect(ident, remove_repository=False):
    """Forget a destination: its configuration and credentials go; the copies stay where they are unless a folder
    repository is to be removed too. The server's own key stays authorised where it was."""
    config = remote.destination(ident, require_id=False)
    home = Path(config['dir']); trusted(home, directory=True)
    shutil.rmtree(home)
    if remove_repository and config['type'] == 'local' and Path(config['repository']).is_dir() and (Path(config['repository']) / 'config').is_file():
        shutil.rmtree(config['repository'])
    return {'disconnected': ident, 'repository_removed': bool(remove_repository and config['type'] == 'local')}


def reveal(ident):
    """A repository's password, for the operator's password manager; the runbook needs it for recovery."""
    config = remote.destination(ident, require_id=False)
    return {'id': ident, 'name': config['name'], 'password': Path(config['password_file']).read_text().strip()}


def describe(config):
    """What the page shows about one destination, never a secret."""
    info = {'id': config['id'], 'name': config['name'], 'type': config['type'], 'enabled': config.get('enabled', True), 'repository': config['repository'],
            'repository_id': config.get('repository_id', '')}
    if config['type'] == 'sftp':
        m = re.fullmatch(r'sftp://([^@]+)@([^:/]+)(?::(\d+))?//(.*)', config['repository'])
        info.update(username=m.group(1), host=m.group(2), port=int(m.group(3) or 22), path='/' + m.group(4), auth='password' if config.get('ssh_password_file') else 'key',
                    fingerprints=fingerprints(config['known_hosts_file']))
    elif config['type'] == 's3':
        m = re.fullmatch(r's3:https://s3\.([a-z0-9-]+)\.amazonaws\.com/([^/]+)(?:/(.*))?', config['repository'])
        info.update(region=m.group(1), bucket=m.group(2), prefix=m.group(3) or '')
    else:
        folder = Path(config['repository'])
        info.update(path=config['repository'], present=folder.is_dir() and (folder / 'config').is_file())
    return info


def overview():
    """The connections as the page shows them, never a secret: every destination, valid or not, and the server key."""
    key = SECRETS / 'id_ed25519.pub'
    items = []
    for config in remote.destinations(require_id=False, include_invalid=True):
        items.append(config if config.get('invalid') else describe(config))
    return {'configured': bool(items), 'destinations': items, 'server_public_key': key.read_text().strip() if key.exists() else None}


# ---- the recovery card: the way into every repository, kept outside the server

def card():
    """Everything a fresh server needs to open every destination: for each, the address, the pinned host keys, the
    credentials (this server's uploader key, so the destination need not authorise a new one) and the repository
    password. Keep it in a password manager: it is the whole of the backups. A folder on this server's own disk is
    listed too; a fresh server connects it only if the folder is there (a mounted share is; a dead disk is not)."""
    import socket
    every = remote.destinations(require_id=False)
    if not every: raise ValueError('No destination is connected')
    out = {'kind': 'reeve-recovery-card', 'schema': 2, 'made_at': time.time(), 'hostname': socket.gethostname(), 'destinations': []}
    for config in every:
        item = {'name': config['name'], 'type': config['type'], 'repository': config['repository'], 'repository_id': config.get('repository_id', ''),
                'repository_password': Path(config['password_file']).read_text().strip()}
        if config['type'] == 'sftp':
            item['known_hosts'] = Path(config['known_hosts_file']).read_text()
            if config.get('ssh_password_file'): item['ssh_password'] = Path(config['ssh_password_file']).read_text().rstrip('\n')
            else:
                item['ssh_private_key'] = Path(config['ssh_key_file']).read_text()
                item['ssh_public_key'] = Path(config['ssh_key_file'] + '.pub').read_text().strip()
        elif config['type'] == 's3':
            item['aws'] = json.loads(Path(config['aws_credentials_file']).read_text())
        out['destinations'].append(item)
    return out


def card_items(text):
    """The destinations a card names: one on a schema 1 card, any number on schema 2."""
    try: data = json.loads(text)
    except ValueError: raise ValueError('Not a recovery card') from None
    if not isinstance(data, dict) or data.get('kind') != 'reeve-recovery-card' or data.get('schema') not in (1, 2): raise ValueError('Not a recovery card')
    items = [data] if data['schema'] == 1 else data.get('destinations')
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items): raise ValueError('The card names no destination')
    return items


def connect_card(text):
    """Connect from a recovery card: every destination it names, with the pinned host keys and credentials as recorded,
    each repository opened with the card's password and required to be the very repository the card names. Uploads start
    paused, since the server the card came from may still be writing. A destination already connected here, or a folder
    that is not there, is skipped and said so."""
    results = []
    known = {d.get('repository_id') for d in remote.destinations(require_id=False)}
    for data in card_items(text):
        for key in ('type', 'repository', 'repository_id', 'repository_password'):
            if not isinstance(data.get(key), str) or not data[key]: raise ValueError('The card is missing its ' + key.replace('_', ' '))
        if data['type'] not in ('sftp', 's3', 'local'): raise ValueError('Unknown destination type on the card')
        name = data.get('name') if isinstance(data.get('name'), str) and NAME.fullmatch(data.get('name') or '') else ''
        if data['repository_id'] in known:
            results.append({'name': name or data['repository'], 'type': data['type'], 'repository': data['repository'], 'skipped': 'already connected'}); continue
        if data['type'] == 'local' and not (Path(data['repository']) / 'config').is_file():
            results.append({'name': name or data['repository'], 'type': data['type'], 'repository': data['repository'], 'skipped': 'the folder is not on this server'}); continue
        ident, home = candidate_folder()
        try:
            private_file(home / 'restic-password', data['repository_password'] + '\n')
            config = {'enabled': False, 'timeout_seconds': 600, 'password_file': str(home / 'restic-password'), 'type': data['type'],
                      'repository': data['repository'], 'repository_id': data['repository_id']}
            if name: config['name'] = name
            if data['type'] == 'sftp':
                if not data.get('known_hosts'): raise ValueError('The card has no host keys')
                private_file(home / 'known_hosts', data['known_hosts']); config['known_hosts_file'] = str(home / 'known_hosts')
                if data.get('ssh_password'):
                    private_file(home / 'ssh-password', data['ssh_password'] + '\n')
                    atomic(home / 'ssh-askpass', '#!/bin/sh\nexec cat "$(dirname "$0")/ssh-password"\n', 0o700)
                    config.update(ssh_password_file=str(home / 'ssh-password'), ssh_askpass_file=str(home / 'ssh-askpass'))
                elif data.get('ssh_private_key'):
                    SECRETS.mkdir(mode=0o700, exist_ok=True)
                    if not (SECRETS / 'id_ed25519').exists():
                        private_file(SECRETS / 'id_ed25519', data['ssh_private_key'])
                        if data.get('ssh_public_key'): atomic(SECRETS / 'id_ed25519.pub', data['ssh_public_key'] + '\n', 0o600)
                    config['ssh_key_file'] = str(SECRETS / 'id_ed25519')
                else: raise ValueError('The card has no SFTP credentials')
            elif data['type'] == 's3':
                if not isinstance(data.get('aws'), dict): raise ValueError('The card has no S3 credentials')
                private_file(home / 'aws.json', json.dumps(data['aws'])); config['aws_credentials_file'] = str(home / 'aws.json')
            atomic(home / 'config.json', json.dumps(config, indent=2), 0o600)
            checked = remote.load(home / 'config.json', require_id=False)
            try: ident_found = repository_id(checked)
            except RemoteFailed: raise ValueError('The repository ' + (name or data['repository']) + ' could not be opened with the card; check the destination is reachable from here') from None
            if ident_found != data['repository_id']: raise ValueError('That is a different repository from the one the card names for ' + (name or data['repository']))
            finish(ident, home, config)
            known.add(data['repository_id'])
            results.append({'id': ident, 'name': config.get('name') or remote.load(DESTINATIONS / ident / 'config.json')['name'], 'type': config['type'], 'repository': config['repository'],
                            'repository_id': config['repository_id'], 'enabled': False,
                            'fingerprints': fingerprints(home.parent / ident / 'known_hosts') if config['type'] == 'sftp' else []})
        except Exception:
            if home.exists(): shutil.rmtree(home)
            raise
    return {'connected': results, 'type': results[0]['type'] if results else None, 'repository': results[0]['repository'] if results else None,
            'repository_id': results[0].get('repository_id') if results else None, 'enabled': False, 'fingerprints': results[0].get('fingerprints', []) if results else []}
