import hashlib
import io
import pytest
from reeve import wp_cli
from reeve.content_jobs import validate_content


def test_download_and_cached_bytes_are_verified(monkeypatch, tmp_path):
    raw = b'checked upstream PHAR'
    monkeypatch.setattr(wp_cli, 'CACHE', tmp_path)
    monkeypatch.setattr(wp_cli, 'SHA256', hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(wp_cli, 'trusted', lambda *a, **kw: None)
    monkeypatch.setattr(wp_cli, 'urlopen', lambda *a, **kw: io.BytesIO(b'wrong download'))
    with pytest.raises(ValueError, match='download checksum'): wp_cli.phar()
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(wp_cli, 'urlopen', lambda *a, **kw: io.BytesIO(raw))
    path = wp_cli.phar()
    assert path.read_bytes() == raw and path.stat().st_mode & 0o777 == 0o444
    monkeypatch.setattr(wp_cli, 'urlopen', lambda *a, **kw: pytest.fail('Cached file should not download again'))
    assert wp_cli.phar() == path
    path.chmod(0o644); path.write_bytes(b'corrupt cache')
    with pytest.raises(ValueError, match='Cached WP-CLI checksum'): wp_cli.phar()


def test_wordpress_command_supports_nonstandard_directory_and_all_flag():
    data = {'tool':'wp', 'arguments':'plugin update --all', 'path':'custom/web', 'internet':True}
    assert validate_content('tool', data) == data


def test_old_ledger_upgrades_to_wp_cli_capability_without_losing_site_identity(tmp_path):
    import sqlite3
    import uuid
    from reeve.core import Ledger
    path = tmp_path / 'jobs.db'
    ledger = Ledger(path, sites=tmp_path / 'sites')
    ident = str(uuid.uuid4())
    ledger.submit(ident, {'name':'alpha', 'domain':'alpha.example.com','runtime':'php','php_version':'8.4'})
    ledger.update(ident, 'succeeded', 'ready')
    job = str(uuid.uuid4())
    ledger.submit_content(job, ident, 'tool', {'tool':'wp','arguments':'core version','path':'.','internet':False})
    with sqlite3.connect(path) as db: db.execute('PRAGMA user_version=9')
    upgraded = Ledger(path, sites=tmp_path / 'sites')
    assert upgraded.get(ident)['name'] == 'alpha'
    assert upgraded.content_jobs()[0]['id'] == job and upgraded.content_jobs()[0]['state'] == 'queued'
    with upgraded.db() as db: assert db.execute('PRAGMA user_version').fetchone()[0] == 17
