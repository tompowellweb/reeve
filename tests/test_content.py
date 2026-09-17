import hashlib
import io
import json
import os
import tarfile
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from reeve.content_helper import perform
from reeve.content_jobs import validate_content
from reeve.core import Ledger


def run(root, action, data, source=None):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try: return perform(fd, action, data, str(source) if source else '/input')
    finally: os.close(fd)


def archive(path, entries, kind='zip'):
    if kind == 'zip':
        with zipfile.ZipFile(path, 'w') as z:
            for name, body in entries: z.writestr(name, body)
    else:
        with tarfile.open(path, 'w:gz') as tar:
            for name, body in entries:
                member = tarfile.TarInfo(name); member.size = len(body)
                tar.addfile(member, io.BytesIO(body))


@pytest.mark.parametrize('kind', ['zip', 'tar'])
@pytest.mark.parametrize('bad', ['../escape', '/escape', 'nested/../../escape', 'c:\\escape', 'nested/./file'])
def test_bad_archive_validated_before_any_file_is_written(tmp_path, kind, bad):
    root = tmp_path / 'site'; root.mkdir()
    source = tmp_path / 'archive'; archive(source, [('valid', b'good'), (bad, b'bad')], kind)
    with pytest.raises(ValueError): run(root, 'extract', {'path': '.', 'replace': False, 'expanded_limit': 1024}, source)
    assert not list(root.iterdir())


def test_links_duplicates_conflicts_and_expansion_limit(tmp_path):
    root = tmp_path / 'site'; root.mkdir()
    source = tmp_path / 'archive'
    with tarfile.open(source, 'w') as tar:
        member = tarfile.TarInfo('escape'); member.type = tarfile.SYMTYPE; member.linkname = '../outside'
        tar.addfile(member)
    with pytest.raises(ValueError, match='links'): run(root, 'extract', {'path': '.', 'replace': True, 'expanded_limit': 1024}, source)
    for entries, match in [([('file', b'a'), ('file', b'b')], 'duplicate'), ([('file', b'a'), ('file/child', b'b')], 'parent'), ([('big', b'x' * 100)], 'bound')]:
        archive(source, entries)
        with pytest.raises(ValueError, match=match): run(root, 'extract', {'path': '.', 'replace': True, 'expanded_limit': 64}, source)
    assert not list(root.iterdir())


@pytest.mark.parametrize('kind', ['zip', 'tar'])
def test_archive_roundtrip_read_edit_and_revision_conflict(tmp_path, kind):
    root = tmp_path / 'site'; root.mkdir()
    source = tmp_path / 'archive'; archive(source, [('app/config.php', b'<?php return 1;'), ('app/readme.txt', b'hello')], kind)
    result = run(root, 'extract', {'path': '.', 'replace': False, 'expanded_limit': 1024}, source)
    assert result['files'] == 2
    assert (root / 'app/config.php').stat().st_uid == os.getuid()
    document = run(root, 'read', {'path': 'app/config.php'})
    source.write_bytes(b'<?php return 2;')
    data = {'path': 'app/config.php', 'size': source.stat().st_size, 'replace': True, 'expected': document['sha256']}
    run(root, 'edit', data, source)
    with pytest.raises(ValueError, match='changed'): run(root, 'edit', data, source)
    assert (root / 'app/config.php').read_text() == '<?php return 2;'
    assert not list(root.rglob('.hosting-upload-*'))


def test_existing_symlinks_and_hardlinks_are_not_followed(tmp_path):
    root = tmp_path / 'site'; root.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'secret').write_text('private')
    (root / 'link').symlink_to(outside, target_is_directory=True)
    source = tmp_path / 'input'; source.write_bytes(b'changed')
    with pytest.raises(OSError): run(root, 'upload', {'path': 'link/secret', 'size': 7, 'replace': True}, source)
    os.link(outside / 'secret', root / 'hard')
    with pytest.raises(ValueError): run(root, 'upload', {'path': 'hard', 'size': 7, 'replace': True}, source)
    with pytest.raises(ValueError): run(root, 'read', {'path': 'hard'})
    assert (outside / 'secret').read_text() == 'private'


def test_content_is_serialized_and_interruption_is_not_replayed(tmp_path):
    ledger = Ledger(tmp_path / 'db', tmp_path / 'sites')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'alpha', 'domain': 'alpha.example.com', 'runtime': 'php', 'php_version': '8.3'})
    ledger.update(row['id'], 'succeeded', 'ready')
    data = {'tool': 'php', 'arguments': '-v', 'path': '.', 'internet': False}
    ident = str(uuid.uuid4())
    ledger.submit_content(ident, row['id'], 'tool', data)
    assert ledger.submit_content(ident, row['id'], 'tool', data)['id'] == ident
    for action in (lambda: ledger.submit_content(str(uuid.uuid4()), row['id'], 'tool', data),
                   lambda: ledger.submit_domains(str(uuid.uuid4()), row['id'], ['other.example.com']),
                   lambda: ledger.submit_runtime(str(uuid.uuid4()), 'switch', row['id'], {'branch': '8.4'}),
                   lambda: ledger.submit_database(str(uuid.uuid4()), 'add', row['id'], {'engine': 'mysql'})):
        with pytest.raises(ValueError, match='pending'): action()
    ledger.update_content(ident, 'running', 'executing')
    ledger.interrupted()
    assert ledger.content_jobs()[0]['state'] == 'recovery-needed'
    ledger.update_content(ident, 'failed', 'reviewed')
    assert ledger.submit_content(str(uuid.uuid4()), row['id'], 'tool', data)['state'] == 'queued'


def test_streaming_upload_auth_size_cleanup_and_claim(tmp_path):
    from fastapi.testclient import TestClient
    from reeve.auth import Auth
    from reeve.web import create_app
    from tests.test_web import csrf
    auth = Auth(tmp_path / 'auth.db'); auth.set_password('test-password-unique-123')
    captured = []
    row = {'name': 'alpha', 'id': str(uuid.uuid4()), 'state': 'succeeded'}
    def call(message):
        if message['op'] == 'content-site': return row
        if message['op'] == 'content-status': return []
        if message['op'] == 'content-submit':
            captured.append(message)
            raw = (tmp_path / 'uploads' / message['id']).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == message['data']['sha256']
            assert len(raw) == message['data']['size']
            return {'id': message['id']}
        raise AssertionError(message)
    client = TestClient(create_app(tmp_path / 'auth.db', call))
    url = '/sites/alpha/content/upload?kind=upload&path=test.txt&id=' + str(uuid.uuid4())
    assert client.post(url, content=b'a').status_code == 401
    token, found = auth.new_session(authenticated=True); client.cookies.set('hosting_session', token)
    assert client.post(url, content=b'a').status_code == 403
    headers = {'x-csrf-token': found['csrf'], 'content-type': 'application/octet-stream'}
    assert client.post(url, content=b'a' * 10000, headers=headers).status_code == 200
    assert len(captured) == 1 and list((tmp_path / 'uploads').iterdir()) == [tmp_path / 'uploads/lock']
    assert client.post(url, content=b'a', headers={**headers, 'content-length': str(513 * 1048576)}).status_code == 413
    assert client.post(url, content=b'a', headers={**headers, 'origin': 'https://evil.example'}).status_code == 403
    assert client.post('/sites/alpha/content/tool', content=b'a' * 8193).status_code == 413


def test_sql_client_has_no_administrator_mounts(tmp_path, monkeypatch):
    from reeve import content_site, database_site
    row = {'name': 'alpha', 'id': str(uuid.uuid4()), 'uid': 30000, 'payload': '{"data_mb":1024}'}
    state = {'stage': 'ready', 'engine': 'mysql', 'mount': '/var/lib/mysql', 'app_password': 'test-password', 'image_id': 'sha256:test'}
    monkeypatch.setattr(database_site, 'state', lambda row: state)
    monkeypatch.setattr(content_site, 'atomic', lambda path, text, mode=0o600: path.write_text(text))
    (tmp_path / 'tmp').mkdir()
    args = content_site.sql_args(row, str(uuid.uuid4()), tmp_path)
    joined = ' '.join(map(str, args))
    assert '--user 30000:30000' in joined and '--network hosting-backend-alpha' in joined
    assert '/admin' not in joined and 'test-password' not in joined and 'database/data' not in joined
    assert '--read-only' in args and '--local-infile=0' in joined


def test_ordinary_tar_root_directory_is_accepted(tmp_path):
    root = tmp_path / 'site'; root.mkdir()
    source = tmp_path / 'site.tar.gz'
    with tarfile.open(source, 'w:gz') as tar:
        directory = tarfile.TarInfo('.'); directory.type = tarfile.DIRTYPE; tar.addfile(directory)
        member = tarfile.TarInfo('./welcome.txt'); member.size = 5
        tar.addfile(member, io.BytesIO(b'hello'))
    run(root, 'extract', {'path': '.', 'replace': False, 'expanded_limit': 1024}, source)
    assert (root / 'welcome.txt').read_text() == 'hello'


def test_full_quota_recovery_cannot_write_to_unmetered_temporary_space(tmp_path, monkeypatch):
    from reeve import content_site
    monkeypatch.setattr(content_site, 'RESCUE', tmp_path.parent)
    (tmp_path / 'tmp').mkdir()
    row = {'uid': 30000, 'payload': '{"data_mb":16}'}
    args = content_site.base_args(row, str(uuid.uuid4()), tmp_path)
    temporary_mount = next(str(arg) for arg in args if 'dst=/tmp' in str(arg))
    assert temporary_mount.endswith(',readonly')
    assert '--read-only' in args and '--cap-drop' in args and 'ALL' in args
