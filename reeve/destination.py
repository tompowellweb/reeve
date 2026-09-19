"""The backup destination from the panel: connect an SFTP server or an Amazon S3 bucket, hold the state.

Until now the destination was root-only file setup. The backups page now takes the connection:
for SFTP the host, port, username and absolute path, authenticated with the server's own key
(shown on the page so the operator can authorise it on the destination) or a password; for S3
the bucket, region, optional prefix and an access key pair. The repository password is made here
and shown once, to be kept outside the server as the runbook requires, or an existing
repository's password is supplied to adopt it. Connecting pins the destination's host key
(`ssh-keyscan`, fingerprints shown), writes every secret as a private root file, opens or
initialises the repository, and records its identity; only then does the new connection replace
the old one, so a failed attempt leaves a working destination untouched. Pause, resume and
disconnect are the other actions; the files remain the record the uploader reads.
"""
import json
import os
import re
import secrets
import shutil
from pathlib import Path

from .host import atomic, command, trusted
from .remote_backup import CONFIG, RemoteFailed, execute, settings

SECRETS = Path('/srv/ops/panel/worker/remote-secrets')
HOST = re.compile(r'[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?')
USER = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}')
PATH_RE = re.compile(r'/[A-Za-z0-9_./-]*')
REGION = re.compile(r'[a-z]{2}-[a-z]+-[0-9]')
BUCKET = re.compile(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]')
PREFIX = re.compile(r'[A-Za-z0-9_./-]{0,200}')


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
    if kind == 'sftp':
        port = field(form, 'port', re.compile(r'[0-9]{1,5}'), required=False, default='22')
        if not 1 <= int(port) <= 65535: raise ValueError('Port must be 1 to 65535')
        auth = field(form, 'auth', re.compile(r'key|password'), required=False, default='key')
        password = str(form.get('password', '') or '')
        if auth == 'password' and (not password or '\n' in password or len(password) > 1024): raise ValueError('A password is required for password authentication')
        return {'type': 'sftp', 'host': field(form, 'host', HOST), 'port': int(port), 'username': field(form, 'username', USER),
                'path': field(form, 'path', PATH_RE), 'auth': auth, 'password': password if auth == 'password' else '', 'existing_password': existing}
    if kind == 's3':
        return {'type': 's3', 'bucket': field(form, 'bucket', BUCKET), 'region': field(form, 'region', REGION), 'prefix': field(form, 'prefix', PREFIX, required=False).strip('/'),
                'access_key_id': field(form, 'access_key_id', re.compile(r'[A-Z0-9]{16,128}')), 'secret_access_key': field(form, 'secret_access_key', re.compile(r'[A-Za-z0-9/+=]{16,256}')), 'existing_password': existing}
    raise ValueError('Choose SFTP or Amazon S3')


def server_key():
    """The server's own uploader key; its public half is what the destination account authorises."""
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
    if not known_hosts.exists(): return []
    trusted(known_hosts)
    try: return [line.strip() for line in command(['ssh-keygen', '-lf', known_hosts]).splitlines() if line.strip()]
    except RuntimeError: return []


def repository_id(config):
    return json.loads(execute(config, ['cat', 'config']))['id']


def connect(form):
    """Write a candidate set of secrets and configuration, open or initialise the repository, then make it current."""
    data = validate(form)
    SECRETS.mkdir(mode=0o700, exist_ok=True); trusted(SECRETS, directory=True)
    candidate = SECRETS / 'candidate'
    if candidate.exists(): trusted(candidate, directory=True); shutil.rmtree(candidate)
    candidate.mkdir(mode=0o700)
    try: return _connect(data, candidate)
    except Exception:
        if candidate.exists(): shutil.rmtree(candidate)
        raise


def _connect(data, candidate):
    generated = not data['existing_password']
    password = data['existing_password'] or secrets.token_hex(32)
    private_file(candidate / 'restic-password', password + '\n')
    config = {'enabled': True, 'timeout_seconds': 600, 'password_file': str(SECRETS / 'restic-password')}
    result = {'type': data['type'], 'generated_password': password if generated else None}
    if data['type'] == 'sftp':
        private_file(candidate / 'known_hosts', keyscan(data['host'], data['port']))
        config.update(type='sftp', repository=f"sftp://{data['username']}@{data['host']}:{data['port']}//{data['path'].lstrip('/')}",
                      known_hosts_file=str(SECRETS / 'known_hosts'))
        if data['auth'] == 'password':
            private_file(candidate / 'ssh-password', data['password'] + '\n')
            # The helper reads the password beside itself, so it works from the candidate folder and the final one.
            atomic(candidate / 'ssh-askpass', '#!/bin/sh\nexec cat "$(dirname "$0")/ssh-password"\n', 0o700)
            config.update(ssh_password_file=str(SECRETS / 'ssh-password'), ssh_askpass_file=str(SECRETS / 'ssh-askpass'))
        else:
            server_key(); config['ssh_key_file'] = str(SECRETS / 'id_ed25519')
        result['fingerprints'] = fingerprints(candidate / 'known_hosts')
    else:
        private_file(candidate / 'aws.json', json.dumps({'AWS_ACCESS_KEY_ID': data['access_key_id'], 'AWS_SECRET_ACCESS_KEY': data['secret_access_key'], 'AWS_DEFAULT_REGION': data['region']}))
        config.update(type='s3', repository=f"s3:https://s3.{data['region']}.amazonaws.com/{data['bucket']}" + (f"/{data['prefix']}" if data['prefix'] else ''),
                      aws_credentials_file=str(SECRETS / 'aws.json'))
    # The candidate is checked in place of the current files: point the candidate config at them.
    trial = dict(config, password_file=str(candidate / 'restic-password'))
    for key, name in (('known_hosts_file', 'known_hosts'), ('ssh_password_file', 'ssh-password'), ('ssh_askpass_file', 'ssh-askpass'), ('aws_credentials_file', 'aws.json')):
        if key in trial: trial[key] = str(candidate / name)
    trial_path = candidate / 'remote-backup.json'
    atomic(trial_path, json.dumps(trial, indent=2), 0o600)
    try:
        checked = settings_from(trial_path, require_id=False)
        try: ident = repository_id(checked)
        except RemoteFailed:
            if not generated: raise ValueError('The repository could not be opened with that password; check the password, the address and the credentials') from None
            execute(checked, ['init', '--json'])
            ident = repository_id(checked)
        config['repository_id'] = ident
        result['repository_id'] = ident
    except Exception:
        raise
    # Success: the candidate secrets become the current ones, then the configuration.
    for name in ('restic-password', 'known_hosts', 'ssh-password', 'ssh-askpass', 'aws.json'):
        if (candidate / name).exists(): os.replace(candidate / name, SECRETS / name)
    for name in ('known_hosts', 'ssh-password', 'ssh-askpass', 'aws.json'):
        if name not in {Path(v).name for k, v in config.items() if k.endswith('_file')} and (SECRETS / name).exists(): (SECRETS / name).unlink()
    atomic(CONFIG, json.dumps(config, indent=2), 0o600)
    shutil.rmtree(candidate)
    result['repository'] = config['repository']
    return result


def settings_from(path, require_id=True):
    """`settings()` against another configuration file."""
    from . import remote_backup
    original = remote_backup.CONFIG
    remote_backup.CONFIG = Path(path)
    try: return settings(require_id=require_id)
    finally: remote_backup.CONFIG = original


def set_enabled(enabled):
    if not CONFIG.exists(): raise ValueError('No destination is connected')
    trusted(CONFIG)
    config = json.loads(CONFIG.read_text()); config['enabled'] = bool(enabled)
    atomic(CONFIG, json.dumps(config, indent=2), 0o600)
    return {'enabled': config['enabled']}


def disconnect():
    """Forget the destination: configuration and credentials go; the server's own key stays authorised where it was."""
    if CONFIG.exists(): trusted(CONFIG); CONFIG.unlink()
    for name in ('restic-password', 'known_hosts', 'ssh-password', 'ssh-askpass', 'aws.json'):
        path = SECRETS / name
        if path.exists(): trusted(path); path.unlink()
    return {'disconnected': True}


def reveal():
    """The repository password, for the operator's password manager; the runbook needs it for recovery."""
    path = SECRETS / 'restic-password'
    if not path.exists(): raise ValueError('No destination is connected')
    trusted(path)
    return {'password': path.read_text().strip()}


def overview():
    """What the page shows about the connection, never a secret."""
    key = SECRETS / 'id_ed25519.pub'
    info = {'configured': CONFIG.exists(), 'server_public_key': key.read_text().strip() if key.exists() else None, 'fingerprints': fingerprints(SECRETS / 'known_hosts')}
    if not CONFIG.exists(): return info
    try: config = settings(require_id=False)
    except RemoteFailed as exc: return {**info, 'error': str(exc)}
    info.update(type=config['type'], enabled=config.get('enabled', True), repository=config['repository'], repository_id=config.get('repository_id', ''))
    if config['type'] == 'sftp':
        m = re.fullmatch(r'sftp://([^@]+)@([^:/]+)(?::(\d+))?//(.*)', config['repository'])
        info.update(username=m.group(1), host=m.group(2), port=int(m.group(3) or 22), path='/' + m.group(4), auth='password' if config.get('ssh_password_file') else 'key')
    else:
        m = re.fullmatch(r's3:https://s3\.([a-z0-9-]+)\.amazonaws\.com/([^/]+)(?:/(.*))?', config['repository'])
        info.update(region=m.group(1), bucket=m.group(2), prefix=m.group(3) or '')
    return info


# ---- the recovery card: the way into the repository, kept outside the server

def card():
    """Everything a fresh server needs to open this destination: the address, the pinned host keys, the
    credentials (this server's uploader key, so the destination need not authorise a new one) and the repository
    password. Keep it in a password manager: it is the whole of the backups."""
    import socket, time
    if not CONFIG.exists(): raise ValueError('No destination is connected')
    trusted(CONFIG); config = json.loads(CONFIG.read_text())
    out = {'kind': 'reeve-recovery-card', 'schema': 1, 'made_at': time.time(), 'hostname': socket.gethostname(),
           'type': config.get('type'), 'repository': config.get('repository'), 'repository_id': config.get('repository_id'),
           'repository_password': (SECRETS / 'restic-password').read_text().strip()}
    if config.get('type') == 'sftp':
        out['known_hosts'] = Path(config['known_hosts_file']).read_text()
        if config.get('ssh_password_file'): out['ssh_password'] = Path(config['ssh_password_file']).read_text().rstrip('\n')
        else:
            out['ssh_private_key'] = Path(config['ssh_key_file']).read_text()
            out['ssh_public_key'] = Path(config['ssh_key_file'] + '.pub').read_text().strip()
    elif config.get('type') == 's3':
        out['aws'] = json.loads(Path(config['aws_credentials_file']).read_text())
    return out


def connect_card(text):
    """Connect from a recovery card: the pinned host keys and credentials as recorded, the repository opened with the
    card's password and required to be the very repository the card names. Uploads start paused, since the server
    the card came from may still be writing."""
    try: data = json.loads(text)
    except ValueError: raise ValueError('Not a recovery card') from None
    if not isinstance(data, dict) or data.get('kind') != 'reeve-recovery-card' or data.get('schema') != 1: raise ValueError('Not a recovery card')
    for key in ('type', 'repository', 'repository_id', 'repository_password'):
        if not isinstance(data.get(key), str) or not data[key]: raise ValueError('The card is missing its ' + key.replace('_', ' '))
    if data['type'] not in ('sftp', 's3'): raise ValueError('Unknown destination type on the card')
    SECRETS.mkdir(mode=0o700, exist_ok=True); trusted(SECRETS, directory=True)
    candidate = SECRETS / 'candidate'
    if candidate.exists(): trusted(candidate, directory=True); shutil.rmtree(candidate)
    candidate.mkdir(mode=0o700)
    try:
        private_file(candidate / 'restic-password', data['repository_password'] + '\n')
        config = {'enabled': False, 'timeout_seconds': 600, 'password_file': str(SECRETS / 'restic-password'), 'type': data['type'],
                  'repository': data['repository'], 'repository_id': data['repository_id']}
        if data['type'] == 'sftp':
            if not data.get('known_hosts'): raise ValueError('The card has no host keys')
            private_file(candidate / 'known_hosts', data['known_hosts']); config['known_hosts_file'] = str(SECRETS / 'known_hosts')
            if data.get('ssh_password'):
                private_file(candidate / 'ssh-password', data['ssh_password'] + '\n')
                atomic(candidate / 'ssh-askpass', '#!/bin/sh\nexec cat "$(dirname "$0")/ssh-password"\n', 0o700)
                config.update(ssh_password_file=str(SECRETS / 'ssh-password'), ssh_askpass_file=str(SECRETS / 'ssh-askpass'))
            elif data.get('ssh_private_key'):
                private_file(candidate / 'id_ed25519', data['ssh_private_key'])
                if data.get('ssh_public_key'): atomic(candidate / 'id_ed25519.pub', data['ssh_public_key'] + '\n', 0o644)
                config['ssh_key_file'] = str(SECRETS / 'id_ed25519')
            else: raise ValueError('The card has no SFTP credentials')
        else:
            if not isinstance(data.get('aws'), dict): raise ValueError('The card has no S3 credentials')
            private_file(candidate / 'aws.json', json.dumps(data['aws'])); config['aws_credentials_file'] = str(SECRETS / 'aws.json')
        trial = dict(config, password_file=str(candidate / 'restic-password'))
        for key, name in (('known_hosts_file', 'known_hosts'), ('ssh_password_file', 'ssh-password'), ('ssh_askpass_file', 'ssh-askpass'), ('aws_credentials_file', 'aws.json'), ('ssh_key_file', 'id_ed25519')):
            if key in trial: trial[key] = str(candidate / name)
        atomic(candidate / 'remote-backup.json', json.dumps(trial, indent=2), 0o600)
        checked = settings_from(candidate / 'remote-backup.json', require_id=False)
        try: ident = repository_id(checked)
        except RemoteFailed: raise ValueError('The repository could not be opened with the card; check the destination is reachable from here') from None
        if ident != data['repository_id']: raise ValueError('That is a different repository from the one the card names')
        for name in ('restic-password', 'known_hosts', 'ssh-password', 'ssh-askpass', 'aws.json', 'id_ed25519', 'id_ed25519.pub'):
            if (candidate / name).exists(): os.replace(candidate / name, SECRETS / name)
        atomic(CONFIG, json.dumps(config, indent=2), 0o600)
    finally:
        if candidate.exists(): shutil.rmtree(candidate)
    return {'type': config['type'], 'repository': config['repository'], 'repository_id': config['repository_id'], 'enabled': False,
            'fingerprints': fingerprints(SECRETS / 'known_hosts') if config['type'] == 'sftp' else []}
