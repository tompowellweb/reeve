"""The recovery card: the way into every repository, exported whole and imported in one step."""
import json
from pathlib import Path

import pytest

from reeve import destination as dest, remote_backup as remote


@pytest.fixture
def box(tmp_path, monkeypatch):
    secrets = tmp_path / 'secrets'; secrets.mkdir(mode=0o700); homes = tmp_path / 'destinations'; homes.mkdir(mode=0o700)
    monkeypatch.setattr(dest, 'SECRETS', secrets); monkeypatch.setattr(dest, 'DESTINATIONS', homes); monkeypatch.setattr(remote, 'DESTINATIONS', homes)
    monkeypatch.setattr(remote, 'CONFIG', tmp_path / 'legacy.json')
    monkeypatch.setattr(dest, 'trusted', lambda *a, **k: None); monkeypatch.setattr('reeve.host.trusted', lambda *a, **k: None); monkeypatch.setattr(remote, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(remote, 'regular', lambda p: p.read_bytes()); monkeypatch.setattr(remote, 'private', lambda p: Path(p))
    (secrets / 'id_ed25519').write_text('PRIVATE\n'); (secrets / 'id_ed25519.pub').write_text('ssh-ed25519 PUB reeve uploader\n')
    nas = homes / '11111111-1111-1111-1111-111111111111'; nas.mkdir()
    (nas / 'restic-password').write_text('repo-pass\n'); (nas / 'known_hosts').write_text('[nas]:2222 ssh-ed25519 AAAA\n')
    (nas / 'config.json').write_text(json.dumps({'id': nas.name, 'name': 'NAS', 'created': 1, 'enabled': True, 'timeout_seconds': 600, 'type': 'sftp', 'repository': 'sftp://tom@nas:2222//config/x', 'repository_id': 'a' * 64,
                                                 'password_file': str(nas / 'restic-password'), 'known_hosts_file': str(nas / 'known_hosts'), 'ssh_key_file': str(secrets / 'id_ed25519')}))
    disk = homes / '22222222-2222-2222-2222-222222222222'; disk.mkdir(); repo = tmp_path / 'disk-repo'; repo.mkdir(); (repo / 'config').write_text('restic')
    (disk / 'restic-password').write_text('disk-pass\n')
    (disk / 'config.json').write_text(json.dumps({'id': disk.name, 'name': 'Second disk', 'created': 2, 'enabled': True, 'timeout_seconds': 600, 'type': 'local', 'repository': str(repo), 'repository_id': 'b' * 64,
                                                  'password_file': str(disk / 'restic-password')}))
    for home in (nas, disk):
        for f in home.iterdir(): f.chmod(0o600)
    return secrets, homes, repo


def test_the_card_holds_the_whole_way_in_to_every_destination(box):
    card = dest.card()
    assert card['kind'] == 'reeve-recovery-card' and card['schema'] == 2 and [d['name'] for d in card['destinations']] == ['NAS', 'Second disk']
    nas, disk = card['destinations']
    assert nas['repository_id'] == 'a' * 64 and nas['repository_password'] == 'repo-pass' and nas['known_hosts'].startswith('[nas]:2222')
    assert nas['ssh_private_key'] == 'PRIVATE\n' and nas['ssh_public_key'].endswith('reeve uploader')
    assert disk['type'] == 'local' and disk['repository_password'] == 'disk-pass' and 'known_hosts' not in disk


def test_a_fresh_server_connects_every_destination_from_the_card_paused_and_to_the_same_repositories(box, monkeypatch, tmp_path):
    secrets, homes, repo = box
    card = dest.card()
    fresh = tmp_path / 'fresh'; fresh.mkdir(mode=0o700); fresh_homes = tmp_path / 'fresh-destinations'
    monkeypatch.setattr(dest, 'SECRETS', fresh); monkeypatch.setattr(dest, 'DESTINATIONS', fresh_homes); monkeypatch.setattr(remote, 'DESTINATIONS', fresh_homes)
    monkeypatch.setattr(dest, 'repository_id', lambda checked: {'sftp://tom@nas:2222//config/x': 'a' * 64, str(repo): 'b' * 64}[checked['repository']])
    result = dest.connect_card(json.dumps(card))
    assert [c['name'] for c in result['connected']] == ['NAS', 'Second disk'] and all(c['enabled'] is False for c in result['connected']) and result['enabled'] is False
    found = remote.destinations()
    assert [d['name'] for d in found] == ['NAS', 'Second disk'] and all(d['enabled'] is False for d in found)
    nas = found[0]
    assert nas['ssh_key_file'] == str(fresh / 'id_ed25519') and (fresh / 'id_ed25519').read_text() == 'PRIVATE\n' and Path(nas['known_hosts_file']).read_text().startswith('[nas]')
    # Connecting again skips what is already here; a folder that is not on this server is skipped and said so.
    again = dest.connect_card(json.dumps(card))
    assert [c.get('skipped') for c in again['connected']] == ['already connected', 'already connected']
    import shutil; shutil.rmtree(repo)
    for d in found: dest.disconnect(d['id'])
    partial = dest.connect_card(json.dumps(card))
    assert [c.get('skipped') for c in partial['connected']] == [None, 'the folder is not on this server']
    # A schema 1 card from an older release still connects its one destination; a wrong repository is refused.
    old = {'kind': 'reeve-recovery-card', 'schema': 1, 'type': 'sftp', 'repository': 'sftp://tom@nas:2222//config/x', 'repository_id': 'a' * 64,
           'repository_password': 'repo-pass', 'known_hosts': '[nas]:2222 ssh-ed25519 AAAA\n', 'ssh_password': 'pw'}
    for d in remote.destinations(): dest.disconnect(d['id'])
    assert dest.connect_card(json.dumps(old))['connected'][0]['repository_id'] == 'a' * 64
    for d in remote.destinations(): dest.disconnect(d['id'])
    with pytest.raises(ValueError, match='different repository'): dest.connect_card(json.dumps({**old, 'repository_id': 'c' * 64}))
    assert remote.destinations() == [] and not any(p.name.startswith('.candidate') for p in fresh_homes.iterdir())
