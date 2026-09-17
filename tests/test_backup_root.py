import pytest

from reeve import host as hm


def test_backup_root_defaults_and_reads_a_plain_absolute_path(tmp_path, monkeypatch):
    ops = tmp_path / 'ops'; ops.mkdir(); monkeypatch.setattr(hm, 'OPS', ops)
    assert str(hm.backup_root()) == '/srv/backups'  # no settings file yet
    (ops / 'server.yaml').write_text('schema: 1\n')
    assert str(hm.backup_root()) == '/srv/backups'  # no backups block
    (ops / 'server.yaml').write_text('backups:\n  local_path: /mnt/backups/reeve\n')
    assert str(hm.backup_root()) == '/mnt/backups/reeve'
    for bad in ('backups:\n  local_path: relative/path\n', 'backups:\n  local_path: /srv/../etc\n', 'backups:\n  path: /x\n', 'backups: 3\n', 'backups:\n  local_path: "/srv/back ups"\n'):
        (ops / 'server.yaml').write_text(bad)
        with pytest.raises(ValueError): hm.backup_root()
    # The web process cannot read the private settings and never touches the folder: the default, quietly.
    (ops / 'server.yaml').write_text('backups:\n  local_path: /mnt/backups/reeve\n'); (ops / 'server.yaml').chmod(0)
    import os
    if os.getuid() != 0: assert str(hm.backup_root()) == '/srv/backups'
    (ops / 'server.yaml').chmod(0o600)
