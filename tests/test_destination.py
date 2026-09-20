import json
import os
import stat
from pathlib import Path

import pytest

from reeve import destination as dst, remote_backup as remote, host as hm


def test_validation_of_every_destination_kind():
    ok = dst.validate({'type': 'sftp', 'name': 'NAS', 'host': 'nas.example.net', 'port': '2223', 'username': 'hostingbackup', 'path': '/volume1/backups/hosting', 'auth': 'key'})
    assert ok == {'type': 'sftp', 'name': 'NAS', 'host': 'nas.example.net', 'port': 2223, 'username': 'hostingbackup', 'path': '/volume1/backups/hosting', 'auth': 'key', 'password': '', 'existing_password': ''}
    assert dst.validate({'type': 'sftp', 'host': '10.0.0.5', 'username': 'u', 'path': '/x', 'auth': 'password', 'password': 'pw'})['password'] == 'pw'
    s3 = dst.validate({'type': 's3', 'bucket': 'rackback', 'region': 'eu-west-2', 'prefix': '/hosting/', 'access_key_id': 'AKIAEXAMPLEEXAMPLE', 'secret_access_key': 'x' * 40, 'existing_password': 'old'})
    assert s3['prefix'] == 'hosting' and s3['existing_password'] == 'old' and s3['name'] == ''
    assert dst.validate({'type': 'local', 'name': 'Second disk', 'path': '/mnt/backups/reeve/'}) == {'type': 'local', 'name': 'Second disk', 'path': '/mnt/backups/reeve', 'existing_password': ''}
    with pytest.raises(ValueError, match='goes under /srv/backups/repositories, /mnt or /media'): dst.validate({'type': 'local', 'path': '/srv/localrepo'})
    with pytest.raises(ValueError, match='goes under'): dst.validate({'type': 'local', 'path': '/mnt'})
    for bad in ({'type': 'ftp'}, {'type': 'sftp', 'host': 'bad host', 'username': 'u', 'path': '/x'}, {'type': 'sftp', 'host': 'h', 'username': 'u', 'path': 'relative'},
                {'type': 'sftp', 'host': 'h', 'username': 'u', 'path': '/x', 'auth': 'password'}, {'type': 'sftp', 'host': 'h', 'port': '70000', 'username': 'u', 'path': '/x'},
                {'type': 's3', 'bucket': 'b', 'region': 'nowhere', 'access_key_id': 'AKIAEXAMPLEEXAMPLE', 'secret_access_key': 'x' * 40},
                {'type': 'local', 'path': 'relative'}, {'type': 'local', 'path': '/mnt/../etc'}, {'type': 'local', 'path': '/srv/sites/x'}, {'type': 'sftp', 'name': 'bad\nname', 'host': 'h', 'username': 'u', 'path': '/x'}):
        with pytest.raises(ValueError): dst.validate(bad)


@pytest.fixture
def world(tmp_path, monkeypatch):
    secrets = tmp_path / 'secrets'; homes = tmp_path / 'destinations'
    for module in (dst, remote, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(dst, 'SECRETS', secrets); monkeypatch.setattr(remote, 'DESTINATIONS', homes); monkeypatch.setattr(dst, 'DESTINATIONS', homes)
    monkeypatch.setattr(remote, 'CONFIG', tmp_path / 'legacy.json'); monkeypatch.setattr(remote, 'LOCAL_ROOTS', ('/srv/backups/repositories', str(tmp_path / 'mnt')))
    monkeypatch.setattr(remote, 'regular', lambda p: p.read_bytes()); monkeypatch.setattr(remote, 'CACHE', tmp_path / 'cache')
    monkeypatch.setattr(remote, 'private', lambda p: Path(p))
    calls = []
    repo = {'exists': False, 'password': None, 'no_keyscan': False, 'ids': {}}
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
        # One simulated repository per address; each remembers the password it was made with and its identity.
        repository = repo['ids'].setdefault(config['repository'], {'exists': False, 'password': None, 'id': ('%02x' % (len(repo['ids']) % 256)) * 32})
        if args[:2] == ['cat', 'config']:
            if repository['exists'] and Path(config['password_file']).read_text().strip() == repository['password']: return json.dumps({'id': repository['id']})
            raise remote.RemoteFailed('no repository')
        if args[0] == 'init': repository.update(exists=True, password=Path(config['password_file']).read_text().strip()); repo['exists'] = True; return ''
        return ''
    monkeypatch.setattr(dst, 'execute', fake_execute)
    return tmp_path, secrets, calls, repo


def test_connecting_sftp_with_the_server_key_pins_the_host_makes_a_password_and_initialises(world):
    tmp_path, secrets, calls, repo = world
    result = dst.connect({'type': 'sftp', 'name': 'NAS', 'host': 'nas.example.net', 'port': '22', 'username': 'hostingbackup', 'path': '/volume1/hosting', 'auth': 'key'})
    assert result['type'] == 'sftp' and result['name'] == 'NAS' and result['repository'] == 'sftp://hostingbackup@nas.example.net:22//volume1/hosting' and result['repository_id'] == '00' * 32
    assert [c for c in calls if c[0] == 'ssh-keyscan'] == [['ssh-keyscan', '-T', '10', '-t', 'ed25519', '-p', '22', 'nas.example.net']]  # one probe
    assert len(result['generated_password']) == 64 and result['fingerprints'] == ['256 SHA256:nas (ED25519)']
    assert any(c[0] == 'restic' and c[1] == 'init' for c in calls) and repo['exists']
    home = remote.DESTINATIONS / result['id']
    config = json.loads((home / 'config.json').read_text())
    assert config['type'] == 'sftp' and config['ssh_key_file'] == str(secrets / 'id_ed25519') and config['known_hosts_file'] == str(home / 'known_hosts') and config['enabled'] and config['repository_id'] == '00' * 32
    assert (home / 'restic-password').read_text().strip() == result['generated_password'] and stat.S_IMODE((home / 'restic-password').stat().st_mode) == 0o600
    assert (home / 'known_hosts').read_text().startswith('nas.example.net ssh-ed25519') and not any(p.name.startswith('.candidate') for p in remote.DESTINATIONS.iterdir())
    assert remote.settings()['repository_id'] == '00' * 32 and [d['name'] for d in remote.destinations()] == ['NAS']
    view = dst.overview()
    assert view['configured'] and view['destinations'][0]['host'] == 'nas.example.net' and view['destinations'][0]['auth'] == 'key' and view['server_public_key'].startswith('ssh-ed25519 AAAAserver') and 'password' not in json.dumps(view)
    assert dst.reveal(result['id'])['password'] == result['generated_password']
    assert dst.set_enabled(result['id'], False)['enabled'] is False and remote.settings()['enabled'] is False and dst.overview()['destinations'][0]['enabled'] is False
    # A failed second connection leaves nothing behind and the first as it was.
    with pytest.raises(ValueError, match='could not be opened'):
        dst.connect({'type': 'sftp', 'host': 'other.example.net', 'username': 'u', 'path': '/x', 'auth': 'key', 'existing_password': 'wrong'})
    assert [d['repository'] for d in remote.destinations()] == ['sftp://hostingbackup@nas.example.net:22//volume1/hosting'] and len(list(remote.DESTINATIONS.iterdir())) == 1
    # The same repository cannot be connected twice; another one can, and both are listed oldest first.
    with pytest.raises(ValueError, match='already connected as NAS'):
        dst.connect({'type': 'sftp', 'host': 'nas.example.net', 'username': 'hostingbackup', 'path': '/volume1/hosting', 'auth': 'key', 'existing_password': result['generated_password']})
    second = dst.connect({'type': 'sftp', 'name': 'Other', 'host': 'nas.example.net', 'username': 'u', 'path': '/other', 'auth': 'key'})
    assert [d['name'] for d in remote.destinations()] == ['NAS', 'Other'] and second['repository_id'] != result['repository_id']
    dst.disconnect(result['id'])
    assert [d['name'] for d in remote.destinations()] == ['Other'] and (secrets / 'id_ed25519').exists()


def test_password_sftp_s3_and_a_folder_write_their_credentials_and_the_uploader_uses_them(world, tmp_path):
    _, secrets, calls, repo = world
    first = dst.connect({'type': 'sftp', 'host': 'nas.example.net', 'username': 'u', 'path': '/x', 'auth': 'password', 'password': 'secret-pw'})
    config = remote.destination(first['id']); home = Path(config['dir'])
    assert config['ssh_password_file'] == str(home / 'ssh-password') and (home / 'ssh-password').read_text() == 'secret-pw\n' and stat.S_IMODE((home / 'ssh-askpass').stat().st_mode) == 0o700
    import subprocess
    assert subprocess.run([str(home / 'ssh-askpass')], capture_output=True, text=True).stdout == 'secret-pw\n'  # reads beside itself
    args, env = remote.command(config)
    joined = ' '.join(args)
    assert 'BatchMode=no' in joined and 'PreferredAuthentications=password' in joined and '-i ' not in joined and env['SSH_ASKPASS'] == str(home / 'ssh-askpass') and env['SSH_ASKPASS_REQUIRE'] == 'force'
    assert dst.overview()['destinations'][0]['auth'] == 'password' and config['name'] == 'nas.example.net'
    s3 = dst.connect({'type': 's3', 'name': 'Rackback', 'bucket': 'rackback', 'region': 'eu-west-2', 'prefix': 'hosting', 'access_key_id': 'AKIAEXAMPLEEXAMPLE', 'secret_access_key': 'x' * 40})
    config = remote.destination(s3['id'])
    assert config['repository'] == 's3:https://s3.eu-west-2.amazonaws.com/rackback/hosting' and json.loads((Path(config['dir']) / 'aws.json').read_text())['AWS_DEFAULT_REGION'] == 'eu-west-2'
    args, env = remote.command(config)
    assert env['AWS_ACCESS_KEY_ID'] == 'AKIAEXAMPLEEXAMPLE' and 'sftp.args' not in ' '.join(args)
    view = dst.overview()['destinations']
    assert view[1]['bucket'] == 'rackback' and view[1]['prefix'] == 'hosting' and view[1]['name'] == 'Rackback'
    # A folder: made if its parent exists, initialised, and described as present.
    folder = tmp_path / 'mnt' / 'reeve'; folder.parent.mkdir()
    with pytest.raises(ValueError, match='parent does not exist'): dst.connect({'type': 'local', 'path': str(tmp_path / 'mnt' / 'nowhere' / 'x')})
    with pytest.raises(ValueError, match='goes under'): dst.connect({'type': 'local', 'path': str(tmp_path / 'elsewhere')})
    disk = dst.connect({'type': 'local', 'name': 'Second disk', 'path': str(folder)})
    config = remote.destination(disk['id'])
    assert config['type'] == 'local' and config['repository'] == str(folder) and folder.is_dir()
    args, env = remote.command(config)
    assert env['RESTIC_REPOSITORY'] == str(folder) and 'sftp.args' not in ' '.join(args) and 'AWS_ACCESS_KEY_ID' not in env
    (folder / 'config').write_text('restic')
    assert dst.overview()['destinations'][2] == {**dst.overview()['destinations'][2], 'type': 'local', 'path': str(folder), 'present': True}
    assert remote.settings()['type'] == 'sftp'   # the compatibility reader prefers an off-machine destination
    assert dst.disconnect(disk['id'], remove_repository=True)['repository_removed'] and not folder.exists()
    assert [d['type'] for d in remote.destinations()] == ['sftp', 's3']


def test_overview_before_any_connection_creates_nothing(world):
    tmp_path, secrets, calls, repo = world
    view = dst.overview()
    assert view == {'configured': False, 'destinations': [], 'server_public_key': None} and not secrets.exists()
    assert remote.settings() is None and remote.destinations() == []


def test_a_destination_that_refuses_the_probe_leaves_no_candidate_and_the_others(world):
    tmp_path, secrets, calls, repo = world
    dst.connect({'type': 'sftp', 'host': 'nas.example.net', 'username': 'u', 'path': '/x', 'auth': 'key'})
    repo['no_keyscan'] = True
    with pytest.raises(ValueError, match='did not answer the host key probe'):
        dst.connect({'type': 'sftp', 'host': 'other.example.net', 'username': 'u', 'path': '/y', 'auth': 'key'})
    assert not any(p.name.startswith('.candidate') for p in remote.DESTINATIONS.iterdir()) and remote.settings()['repository'].endswith('nas.example.net:22//x')


def test_the_single_destination_of_older_releases_is_migrated_into_its_own_folder(world, tmp_path):
    _, secrets, calls, repo = world
    legacy = tmp_path / 'legacy.json'; secrets.mkdir()
    (secrets / 'restic-password').write_text('pw\n'); (secrets / 'known_hosts').write_text('nas ssh-ed25519 AAAA\n'); (secrets / 'id_ed25519').write_text('k')
    legacy.write_text(json.dumps({'enabled': True, 'type': 'sftp', 'repository': 'sftp://u@nas:22//x', 'repository_id': 'a' * 64, 'timeout_seconds': 600,
                                  'password_file': str(secrets / 'restic-password'), 'known_hosts_file': str(secrets / 'known_hosts'), 'ssh_key_file': str(secrets / 'id_ed25519')}))
    import os
    if os.getuid() != 0: remote.migrate()   # destinations() migrates only as root; the test calls it directly
    found = remote.destinations()
    assert not legacy.exists() and len(found) == 1 and found[0]['name'] == 'nas' and found[0]['repository_id'] == 'a' * 64
    home = Path(found[0]['dir'])
    assert found[0]['password_file'] == str(home / 'restic-password') and (home / 'known_hosts').exists() and not (secrets / 'restic-password').exists() and (secrets / 'id_ed25519').exists()
    assert found[0]['destination'] == remote.load(home / 'config.json')['destination']
