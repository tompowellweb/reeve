import io
import json
import re
import tarfile
import uuid

import pytest
from fastapi.testclient import TestClient

from reeve.application_package import review, metadata
from reeve.auth import Auth
from reeve.web import create_app

COMPOSE = b'''services:
  web:
    image: registry.example/application:1
    volumes: [./code:/app, ./settings.ini:/etc/app/settings.ini:ro, state:/state]
    ports: ['8889:8000']
volumes:
  state: {}
'''
DATA = dict(name='example', domain='example.hosting.test', service='web', port=8000)


def archive(files=None, links=None):
    files = files if files is not None else {'compose.yaml': COMPOSE, 'code/app.py': b'print("app")', 'settings.ini': b'PASSWORD=private-secret'}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as t:
        for name, value in files.items():
            m = tarfile.TarInfo(name); m.size = len(value); t.addfile(m, io.BytesIO(value))
        for name, value in (links or {}).items():
            m = tarfile.TarInfo(name); m.type = tarfile.SYMTYPE; m.linkname = value; t.addfile(m)
    return output.getvalue()


def test_custom_layout_and_all_mounts_are_discovered_without_manifest(tmp_path):
    p = tmp_path / 'upload'; p.write_bytes(archive(links={'code/current.py': 'app.py'}))
    result = review(p, DATA)
    assert result['state'] == 'reviewed' and not result['deployed']
    assert len(result['services'][0]['mounts']) == 3
    assert 'private-secret' not in json.dumps(result)
    assert result['remaining_checks'] and 'runtime_compatibility' in result['remaining_checks']
    assert metadata({**DATA, 'port': '8000'}) == DATA
    with pytest.raises(ValueError): metadata({**DATA, 'manifest': {}})


@pytest.mark.parametrize('files,links', [
    ({'../escape': b'x'}, {}),
    ({'/absolute': b'x'}, {}),
    ({'code/link/file': b'x', 'compose.yaml': COMPOSE}, {'code/link': '../inside'}),
    ({'compose.yaml': COMPOSE}, {'link': '../../outside'}),
    ({'compose.yaml': b'services: &x {web: *x}'}, {}),
])
def test_unsafe_archives_and_recursive_yaml_are_refused(tmp_path, files, links):
    p = tmp_path / 'upload'; p.write_bytes(archive(files, links))
    with pytest.raises(ValueError): review(p, DATA)
    assert not (tmp_path / 'escape').exists()


def test_preparation_errors_include_fields_and_preserve_native_dump(tmp_path):
    p = tmp_path / 'upload'
    p.write_bytes(archive({'project/docker-compose.yml': b'''services:
  web:
    build: .
    volumes: [./missing:/app]
  db:
    image: postgres:16
    volumes: [db:/var/lib/postgresql/data]
volumes: {db: {}}
''', 'project/backup/database.dump': b'PGDMP\x00original'}))
    result = review(p, DATA)
    assert result['state'] == 'needs_preparation'
    assert {i['code'] for i in result['issues']} == {'missing_file'}
    assert result['compose'] == 'project/docker-compose.yml'
    assert result['entries'] == 2


def test_intake_authentication_idempotency_private_results_and_revocation(tmp_path):
    auth = Auth(tmp_path / 'auth.db')
    token = auth.new_api_token('Preparation')
    other = auth.new_api_token('Other agent')
    app = create_app(tmp_path / 'auth.db', call=lambda msg: [])
    client = TestClient(app)
    assert client.get('/api/v1/preparation').status_code == 401
    headers = {'Authorization': 'Bearer ' + token}
    capabilities = client.get('/api/v1/preparation', headers=headers).json()
    assert capabilities['capabilities']['receive'] and not capabilities['capabilities']['deploy']
    assert 'manifest' not in capabilities['fields']
    assert 'docker compose up' in client.get('/api/v1/preparation', headers={**headers, 'Accept': 'text/plain'}).text
    ident = str(uuid.uuid4())
    upload_headers = {**headers, 'Content-Type': 'application/octet-stream', 'Idempotency-Key': ident}
    body = archive()
    response = client.post('/api/v1/imports', params=DATA, content=body, headers=upload_headers)
    assert response.status_code == 201, response.text
    value = response.json()
    assert value['state'] == 'reviewed' and not value['deployed']
    assert 'private-secret' not in response.text and 'owner' not in value
    assert client.post('/api/v1/imports', params=DATA, content=body, headers=upload_headers).json() == value
    assert len(list((tmp_path / 'imports').glob('*/package'))) == 1
    assert client.post('/api/v1/imports', params={**DATA, 'name': 'different'}, content=body, headers=upload_headers).status_code == 409
    assert client.post('/api/v1/imports', params=DATA, content=archive({'compose.yaml': b'changed'}), headers=upload_headers).status_code == 409
    assert client.get(value['status_url'], headers=headers).json() == value
    assert client.get(value['status_url'], headers={'Authorization': 'Bearer ' + other}).status_code == 404
    assert client.get('/api/sites', headers=headers).status_code == 401
    assert client.post('/create', data={'name': 'example'}, headers=headers).status_code == 401
    auth.revoke_api_token(auth.api_token(token)['id'])
    assert client.get('/api/v1/preparation', headers=headers).status_code == 401
    with auth.db() as db:
        assert all(token not in str(tuple(r)) for r in db.execute('SELECT * FROM intake_tokens'))
    assert (tmp_path / 'imports' / ident / 'package').stat().st_mode & 0o777 == 0o600


def test_browser_same_intake_csrf_and_rejected_package_retained(tmp_path):
    auth = Auth(tmp_path / 'auth.db'); auth.set_password('test-password-unique-123')
    client = TestClient(create_app(tmp_path / 'auth.db', call=lambda msg: []))
    login = client.get('/login'); csrf = re.search(r'name="csrf" value="([^"]+)"', login.text)[1]
    client.post('/login', data={'csrf': csrf, 'password': 'test-password-unique-123'})
    page = client.get('/imports')
    csrf = re.search(r'data-csrf="([^"]+)"', page.text)[1]
    assert 'No separate manifest' in page.text
    headers = {'Content-Type': 'application/octet-stream', 'Idempotency-Key': str(uuid.uuid4())}
    assert client.post('/api/v1/imports', params=DATA, content=b'bad archive', headers=headers).status_code == 403
    response = client.post('/api/v1/imports', params=DATA, content=b'bad archive', headers={**headers, 'X-CSRF-Token': csrf})
    assert response.status_code == 201 and response.json()['state'] == 'needs_preparation'
    page = client.get(response.json()['url'])
    assert 'not been deployed' in page.text
    assert 'The package or Compose could not be read' in page.text
    assert client.post('/agent-access', data={'name': 'test'}).status_code == 403
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    page = client.post('/agent-access', data={'csrf': csrf, 'name': 'test'})
    assert 'New agent token' in page.text
    assert client.get('/agent-access').text.count('New agent token') == 0


def test_busy_intake_does_not_remove_another_live_upload(tmp_path):
    import fcntl
    import os
    auth = Auth(tmp_path / 'auth.db'); token = auth.new_api_token('Uploader')
    client = TestClient(create_app(tmp_path / 'auth.db', call=lambda msg: []))
    root = tmp_path / 'imports'; root.mkdir()
    ident = str(uuid.uuid4()); partial = root / (ident + '.partial'); partial.write_bytes(b'live-upload')
    fd = os.open(root / 'lock', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        response = client.post('/api/v1/imports', params=DATA, content=archive(), headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/octet-stream', 'Idempotency-Key': ident})
        assert response.status_code == 409
        assert partial.read_bytes() == b'live-upload'
    finally: os.close(fd)



def test_custom_dockerfile_build_requires_no_registry_publication(tmp_path):
    p = tmp_path / 'upload'
    p.write_bytes(archive({'docker-compose.yml': b"services: {web: {build: {context: ./config}}}",
                          'config/Dockerfile': b'FROM python:3.12-slim\nCOPY app.py /app.py\n',
                          'config/app.py': b'print("application")'}))
    result = review(p, DATA)
    assert result['state'] == 'reviewed'
    assert result['services'][0]['image'] is None
    assert result['services'][0]['build']['dockerfile'] == './config/Dockerfile'
    assert not result['deployed']
    p.write_bytes(archive({'compose.yaml': b'services: {web: {build: /etc}}'}))
    result = review(p, DATA)
    assert result['state'] == 'needs_preparation'
    assert any(i['code'] == 'outside_project' for i in result['issues'])


def test_a_tar_carrying_a_zip_among_its_last_members_is_read_as_the_tar(tmp_path):
    # A real docroot once held css.zip, an old copy of the whole site, near the end of its tar. is_zipfile()
    # finds that zip's end record at the tail of the tar and the import used to unpack the zip instead.
    import zipfile
    from reeve.application_package import Archive
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, 'w') as z: z.writestr('index.php', 'old static copy'); z.writestr('css/old.css', 'old')
    p = tmp_path / 'upload'
    with tarfile.open(p, 'w') as t:
        for name, value in (('css/style.css', b'new'), ('css.zip', inner.getvalue()), ('index.php', b'<?php echo $_GET["path"]; ?>')):
            m = tarfile.TarInfo(name); m.size = len(value); t.addfile(m, io.BytesIO(value))
    assert zipfile.is_zipfile(p)
    a = Archive(p)
    try: assert set(a.entries) == {'css/style.css', 'css.zip', 'index.php'} and a.entries['index.php']['size'] == 28
    finally: a.close()
    plain = tmp_path / 'plain.zip'
    with zipfile.ZipFile(plain, 'w') as z: z.writestr('index.html', 'zip content')
    a = Archive(plain)
    try: assert set(a.entries) == {'index.html'}
    finally: a.close()
