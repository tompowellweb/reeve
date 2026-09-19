"""The recovery card: the way into the repository, exported whole and imported in one step."""
import json
from pathlib import Path

import pytest

from reeve import destination as dest


@pytest.fixture
def box(tmp_path, monkeypatch):
    secrets = tmp_path / 'secrets'; secrets.mkdir(mode=0o700); config = tmp_path / 'remote-backup.json'
    monkeypatch.setattr(dest, 'SECRETS', secrets); monkeypatch.setattr(dest, 'CONFIG', config)
    monkeypatch.setattr(dest, 'trusted', lambda *a, **k: None); monkeypatch.setattr('reeve.host.trusted', lambda *a, **k: None)
    (secrets / 'restic-password').write_text('repo-pass\n'); (secrets / 'known_hosts').write_text('[nas]:2222 ssh-ed25519 AAAA\n')
    (secrets / 'id_ed25519').write_text('PRIVATE\n'); (secrets / 'id_ed25519.pub').write_text('ssh-ed25519 PUB reeve uploader\n')
    config.write_text(json.dumps({'enabled': True, 'timeout_seconds': 600, 'type': 'sftp', 'repository': 'sftp://tom@nas:2222//config/x', 'repository_id': 'abc',
                                  'password_file': str(secrets / 'restic-password'), 'known_hosts_file': str(secrets / 'known_hosts'), 'ssh_key_file': str(secrets / 'id_ed25519')}))
    return secrets, config


def test_the_card_holds_the_whole_way_in(box):
    secrets, config = box
    card = dest.card()
    assert card['kind'] == 'reeve-recovery-card' and card['repository_id'] == 'abc' and card['repository_password'] == 'repo-pass'
    assert card['known_hosts'].startswith('[nas]:2222') and card['ssh_private_key'] == 'PRIVATE\n' and card['ssh_public_key'].endswith('reeve uploader')


def test_a_fresh_server_connects_from_the_card_paused_and_to_the_same_repository(box, monkeypatch, tmp_path):
    secrets, config = box
    card = dest.card()
    fresh = tmp_path / 'fresh'; fresh.mkdir(mode=0o700); fresh_config = tmp_path / 'fresh.json'
    monkeypatch.setattr(dest, 'SECRETS', fresh); monkeypatch.setattr(dest, 'CONFIG', fresh_config)
    seen = {}
    monkeypatch.setattr(dest, 'settings_from', lambda path, require_id=True: seen.setdefault('trial', json.loads(Path(path).read_text())))
    monkeypatch.setattr(dest, 'repository_id', lambda checked: 'abc')
    result = dest.connect_card(json.dumps(card))
    assert result['enabled'] is False and result['repository_id'] == 'abc' and 'fingerprints' in result
    written = json.loads(fresh_config.read_text())
    assert written['enabled'] is False and written['ssh_key_file'] == str(fresh / 'id_ed25519') and (fresh / 'id_ed25519').read_text() == 'PRIVATE\n'
    assert (fresh / 'known_hosts').read_text().startswith('[nas]:2222') and (fresh / 'restic-password').read_text() == 'repo-pass\n' and not (fresh / 'candidate').exists()
    assert seen['trial']['ssh_key_file'].endswith('candidate/id_ed25519')  # checked from the candidate before anything became current
    monkeypatch.setattr(dest, 'repository_id', lambda checked: 'other')
    with pytest.raises(ValueError, match='different repository'): dest.connect_card(json.dumps(card))
    for bad in ('not json', json.dumps({'kind': 'x'}), json.dumps({**card, 'repository_password': ''}), json.dumps({**card, 'known_hosts': ''})):
        with pytest.raises(ValueError): dest.connect_card(bad)
