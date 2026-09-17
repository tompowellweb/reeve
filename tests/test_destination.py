import json
import os
import stat
from pathlib import Path

import pytest

from reeve import destination as dst, remote_backup as remote, host as hm


def test_validation_of_both_destination_kinds():
    ok = dst.validate({'type': 'sftp', 'host': 'nas.example.net', 'port': '2223', 'username': 'hostingbackup', 'path': '/volume1/backups/hosting', 'auth': 'key'})
    assert ok == {'type': 'sftp', 'host': 'nas.example.net', 'port': 2223, 'username': 'hostingbackup', 'path': '/volume1/backups/hosting', 'auth': 'key', 'password': '', 'existing_password': ''}
    assert dst.validate({'type': 'sftp', 'host': '10.0.0.5', 'username': 'u', 'path': '/x', 'auth': 'password', 'password': 'pw'})['password'] == 'pw'
    s3 = dst.validate({'type': 's3', 'bucket': 'rackback', 'region': 'eu-west-2', 'prefix': '/hosting/', 'access_key_id': 'AKIAEXAMPLEEXAMPLE', 'secret_access_key': 'x' * 40, 'existing_password': 'old'})
    assert s3['prefix'] == 'hosting' and s3['existing_password'] == 'old'
    for bad in ({'type': 'ftp'}, {'type': 'sftp', 'host': 'bad host', 'username': 'u', 'path': '/x'}, {'type': 'sftp', 'host': 'h', 'username': 'u', 'path': 'relative'},
                {'type': 'sftp', 'host': 'h', 'username': 'u', 'path': '/x', 'auth': 'password'}, {'type': 'sftp', 'host': 'h', 'port': '70000', 'username': 'u', 'path': '/x'},
                {'type': 's3', 'bucket': 'b', 'region': 'nowhere', 'access_key_id': 'AKIAEXAMPLEEXAMPLE', 'secret_access_key': 'x' * 40}):
        with pytest.raises(ValueError): dst.validate(bad)


@pytest.fixture
def world(tmp_path, monkeypatch):
    secrets = tmp_path / 'secrets'
    for module in (dst, remote, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(dst, 'SECRETS', secrets); monkeypatch.setattr(dst, 'CONFIG', tmp_path / 'remote-backup.json'); monkeypatch.setattr(remote, 'CONFIG', tmp_path / 'remote-backup.json')
    monkeypatch.setattr(remote, 'regular', lambda p: p.read_bytes()); monkeypatch.setattr(remote, 'CACHE', tmp_path / 'cache')
    monkeypatch.setattr(remote, 'private', lambda p: Path(p))
    calls = []
    repo = {'exists': False, 'password': None, 'no_keyscan': False}
    def fake_command(args, timeout=120):
        calls.append([str(a) for a in args])
        if args[0] == 'ssh-keyscan':
            if args[4] != 'ed25519' or repo.get('no_keyscan'): raise RuntimeError('ssh-keyscan failed (1): closed')
            return '# comment\nnas.example.net ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGxOaNNRJKLd9yPcoRvRsHMDaKdSyqRchiLoYW1hGY4v\n'
        if args[0] == 'ssh-keygen' and args[1] == '-lf': return '256 SHA256:nas (ED25519)\n'
        if args[0] == 'ssh-keygen': Path(args[-1]).write_text('private'); Path(str(args[-1]) + '.pub').write_text('ssh-ed25519 AAAAserver reeve uploader\n'); return ''
        return ''
    monkeypatch.setattr(dst, 'command', fake_command)
    def fake_execute(config, args, maximum=None, digest=False):
        calls.append(['restic', *args, 'pw=' + Path(config['password_file']).read_text().strip()[:6]])
        if args[:2] == ['cat', 'config']:
            if repo['exists'] and Path(config['password_file']).read_text().strip() == repo['password']: return json.dumps({'id': 'f' * 64})
            raise remote.RemoteFailed('no repository')
        if args[0] == 'init': repo.update(exists=True, password=Path(config['password_file']).read_text().strip()); return ''
        return ''
    monkeypatch.setattr(dst, 'execute', fake_execute)
    return tmp_path, secrets, calls, repo


def test_connecting_sftp_with_the_server_key_pins_the_host_makes_a_password_and_initialises(world):
    tmp_path, secrets, calls, repo = world
    result = dst.connect({'type': 'sftp', 'host': 'nas.example.net', 'port': '22', 'username': 'hostingbackup', 'path': '/volume1/hosting', 'auth': 'key'})
    assert result['type'] == 'sftp' and result['repository'] == 'sftp://hostingbackup@nas.example.net:22//volume1/hosting' and result['repository_id'] == 'f' * 64
    assert [c for c in calls if c[0] == 'ssh-keyscan'] == [['ssh-keyscan', '-T', '10', '-t', 'ed25519', '-p', '22', 'nas.example.net']]  # one probe
    assert len(result['generated_password']) == 64 and result['fingerprints'] == ['256 SHA256:nas (ED25519)']
    assert any(c[0] == 'restic' and c[1] == 'init' for c in calls) and repo['exists']
    config = json.loads((tmp_path / 'remote-backup.json').read_text())
    assert config['type'] == 'sftp' and config['ssh_key_file'] == str(secrets / 'id_ed25519') and config['known_hosts_file'] == str(secrets / 'known_hosts') and config['enabled'] and config['repository_id'] == 'f' * 64
    assert (secrets / 'restic-password').read_text().strip() == result['generated_password'] and stat.S_IMODE((secrets / 'restic-password').stat().st_mode) == 0o600
    assert (secrets / 'known_hosts').read_text().startswith('nas.example.net ssh-ed25519') and not (secrets / 'candidate').exists()
    assert remote.settings()['repository_id'] == 'f' * 64
    view = dst.overview()
    assert view['configured'] and view['host'] == 'nas.example.net' and view['auth'] == 'key' and view['server_public_key'].startswith('ssh-ed25519 AAAAserver')  # made by the key-authenticated connect and 'password' not in json.dumps(view)
    assert dst.reveal()['password'] == result['generated_password']
    assert dst.set_enabled(False)['enabled'] is False and remote.settings()['enabled'] is False and dst.overview()['enabled'] is False
    # A failed second connection leaves the first in place.
    with pytest.raises(ValueError, match='could not be opened'):
        dst.connect({'type': 'sftp', 'host': 'other.example.net', 'username': 'u', 'path': '/x', 'auth': 'key', 'existing_password': 'wrong'})
    assert remote.settings(require_id=True)['repository'] == 'sftp://hostingbackup@nas.example.net:22//volume1/hosting' and not (secrets / 'candidate').exists()
    # Adopting the same repository with its password: no init, same identity.
    calls.clear()
    again = dst.connect({'type': 'sftp', 'host': 'nas.example.net', 'username': 'hostingbackup', 'path': '/volume1/hosting', 'auth': 'key', 'existing_password': result['generated_password']})
    assert again['generated_password'] is None and again['repository_id'] == 'f' * 64 and not any(c[1] == 'init' for c in calls if c[0] == 'restic')
    dst.disconnect()
    assert not (tmp_path / 'remote-backup.json').exists() and not (secrets / 'restic-password').exists() and (secrets / 'id_ed25519').exists()


def test_password_sftp_and_s3_write_their_credentials_and_the_uploader_uses_them(world):
    tmp_path, secrets, calls, repo = world
    dst.connect({'type': 'sftp', 'host': 'nas.example.net', 'username': 'u', 'path': '/x', 'auth': 'password', 'password': 'secret-pw'})
    config = remote.settings()
    assert config['ssh_password_file'] == str(secrets / 'ssh-password') and (secrets / 'ssh-password').read_text() == 'secret-pw\n' and stat.S_IMODE((secrets / 'ssh-askpass').stat().st_mode) == 0o700
    import subprocess
    assert subprocess.run([str(secrets / 'ssh-askpass')], capture_output=True, text=True).stdout == 'secret-pw\n'  # reads beside itself
    args, env = remote.command(config)
    joined = ' '.join(args)
    assert 'BatchMode=no' in joined and 'PreferredAuthentications=password' in joined and '-i ' not in joined and env['SSH_ASKPASS'] == str(secrets / 'ssh-askpass') and env['SSH_ASKPASS_REQUIRE'] == 'force'
    assert dst.overview()['auth'] == 'password'
    dst.connect({'type': 's3', 'bucket': 'rackback', 'region': 'eu-west-2', 'prefix': 'hosting', 'access_key_id': 'AKIAEXAMPLEEXAMPLE', 'secret_access_key': 'x' * 40})
    config = remote.settings()
    assert config['repository'] == 's3:https://s3.eu-west-2.amazonaws.com/rackback/hosting' and json.loads((secrets / 'aws.json').read_text())['AWS_DEFAULT_REGION'] == 'eu-west-2'
    assert not (secrets / 'ssh-password').exists() and not (secrets / 'known_hosts').exists()  # the SFTP credentials are gone with the SFTP destination
    args, env = remote.command(config)
    assert env['AWS_ACCESS_KEY_ID'] == 'AKIAEXAMPLEEXAMPLE' and 'sftp.args' not in ' '.join(args)
    assert dst.overview()['bucket'] == 'rackback' and dst.overview()['prefix'] == 'hosting'


def test_overview_before_any_connection_creates_nothing(world):
    tmp_path, secrets, calls, repo = world
    view = dst.overview()
    assert view == {'configured': False, 'server_public_key': None, 'fingerprints': []} and not secrets.exists()


def test_a_destination_that_refuses_the_probe_leaves_no_candidate_and_the_current_destination(world):
    tmp_path, secrets, calls, repo = world
    dst.connect({'type': 'sftp', 'host': 'nas.example.net', 'username': 'u', 'path': '/x', 'auth': 'key'})
    repo['no_keyscan'] = True
    with pytest.raises(ValueError, match='did not answer the host key probe'):
        dst.connect({'type': 'sftp', 'host': 'other.example.net', 'username': 'u', 'path': '/y', 'auth': 'key'})
    assert not (secrets / 'candidate').exists() and remote.settings()['repository'].endswith('nas.example.net:22//x')
