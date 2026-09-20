import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from reeve.auth import Auth
from reeve.core import DEFAULTS, Ledger
from reeve.web import create_app
from reeve.worker import dispatch
from types import SimpleNamespace


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text).group(1)


@pytest.fixture
def setup(tmp_path):
    auth = Auth(tmp_path / "auth.db")
    auth.set_password("test-password-unique-123")
    ledger = Ledger(tmp_path / "jobs.db", sites=tmp_path / "sites")
    host = SimpleNamespace(defaults=DEFAULTS, health=lambda row: {"application": "unknown", "container": "absent", "quota": None})
    def call(message):
        return dispatch(message, ledger, host)
    client = TestClient(create_app(tmp_path / "auth.db", call))
    return client, auth, ledger


def login(client):
    page = client.get("/login")
    return client.post("/login", data={"csrf": csrf(page), "password": "test-password-unique-123"})


def test_login_csrf_rotation_and_logout(setup):
    client, auth, _ = setup
    assert client.get("/api/sites").status_code == 401
    page = client.get("/login")
    old_cookie = client.cookies.get("hosting_session")
    assert "HttpOnly" in page.headers["set-cookie"]
    assert "SameSite=strict" in page.headers["set-cookie"]
    assert page.headers["referrer-policy"] == "same-origin"
    assert client.post("/login", data={"csrf": csrf(page), "password": "test-password-unique-123"}, headers={"Origin": "null"}).status_code == 403
    assert client.post("/login", data={"password": "test-password-unique-123"}).status_code == 403
    page = client.post("/login", data={"csrf": csrf(page), "password": "test-password-unique-123"})
    assert "Your first site" in page.text
    assert auth.session(old_cookie) is None
    assert client.post("/logout", data={"csrf": csrf(page)}, headers={"Origin": "https://evil.test"}).status_code == 403
    client.post("/logout", data={"csrf": csrf(page)})
    assert client.get("/api/sites").status_code == 401


def test_browser_create_and_duplicate_request(setup):
    client, _, ledger = setup
    login(client)
    page = client.get("/create")
    ident = str(uuid.uuid4())
    data = {"csrf": csrf(page), "id": ident, "name": "alpha", "domain": "alpha.hosting.test", "data_mb": "16"}
    response = client.post("/create", data=data)
    assert response.status_code == 200
    assert "Setup: queued" in response.text
    assert "Not configured" in response.text
    assert "Delete" not in response.text
    assert client.post("/create", data=data).status_code == 200
    assert len(ledger.list()) == 1
    data.update(id=str(uuid.uuid4()))
    assert "already reserved" in client.post("/create", data=data).text


def test_throttle_body_limit_and_host_header(setup):
    client, auth, _ = setup
    for _ in range(5):
        assert auth.login("wrong") is not None
    assert "Too many" in auth.login("test-password-unique-123")
    assert client.post("/login", content="x" * 8193).status_code == 413
    assert client.get("/login", headers={"Host": "evil.test"}).status_code == 400


def test_no_default_password(tmp_path):
    auth = Auth(tmp_path / "empty.db")
    assert auth.login("admin") is not None


def test_recovery_review_and_inspection_require_session_and_csrf(setup, monkeypatch):
    from reeve import recovery_inventory
    client, _, ledger = setup
    row = ledger.submit(str(uuid.uuid4()), {'name': 'recovery', 'domain': 'recovery.example.com'})
    calls = []
    monkeypatch.setattr(recovery_inventory, 'scan', lambda ledger, row: calls.append(row['id']))
    assert client.post('/sites/recovery/recovery/inspect').status_code == 401
    login(client)
    page = client.get('/sites/recovery')
    assert 'data-concern-toggle="coverage-detail"' in page.text and 'Storage has not been inspected yet' in page.text
    assert client.post('/sites/recovery/recovery/inspect').status_code == 403
    assert not calls
    reply = client.post('/sites/recovery/recovery/inspect', data={'csrf': csrf(page)})
    assert reply.status_code == 200 and calls == [row['id']]


def test_database_dump_and_schedule_are_authenticated_and_show_scope(setup, monkeypatch, tmp_path):
    from reeve import database_site
    client, _, ledger = setup
    row = ledger.submit(str(uuid.uuid4()), {'name': 'dump', 'domain': 'dump.example.com'})
    ledger.update(row['id'], 'succeeded', 'published')
    monkeypatch.setattr(database_site, 'state', lambda row: {'engine': 'postgres', 'stage': 'ready'})
    monkeypatch.setattr(database_site, 'public', lambda *a: None)
    assert client.post('/sites/dump/backup/database').status_code == 401
    assert client.post('/sites/dump/backup/remote').status_code == 401
    login(client); page = client.get('/sites/dump')
    assert 'Manage copies' in page.text and 'other databases, server roles' in page.text
    assert client.post('/sites/dump/backup/database').status_code == 403
    assert client.post('/sites/dump/backup/remote').status_code == 403
    from reeve import remote_backup
    s3 = {'id': str(uuid.UUID(int=77)), 'name': 'Rackback', 'destination': 'test', 'enabled': True, 'type': 's3', 'prune_local_after_days': 0, 'repository': 's3:https://s3.eu-west-2.amazonaws.com/rackback/hosting', 'repository_id': 'a' * 64}
    monkeypatch.setattr(remote_backup, 'settings', lambda **k: s3); monkeypatch.setattr(remote_backup, 'destinations', lambda **k: [s3])
    page = client.get('/sites/dump')
    assert 'Destination settings' in page.text
    destination = client.get('/backups')
    assert 'Rackback' in destination.text and 'Copy now' in destination.text and 'name="id" value="' + s3['id'] + '"' in destination.text
    assert '<code>rackback</code> in eu-west-2' in destination.text and '<h2>Local copies</h2><p><strong>/srv/backups</strong> · the default</p>' in destination.text and 'backups.local_path' in destination.text
    assert 'The folder does not exist' in destination.text  # not on this machine
    from reeve import host as hm
    (tmp_path / 'local-backups').mkdir(); (tmp_path / 'local-backups/dump.bin').write_bytes(b'x' * 4096)
    monkeypatch.setattr(hm, 'BACKUPS', tmp_path / 'local-backups')
    monkeypatch.setattr(hm, 'command', lambda args, timeout=120: '4096\t' + args[2] + '\n' if args[0] == 'du' else '')
    destination = client.get('/backups')
    assert f'<strong>{tmp_path}/local-backups</strong></p>' in destination.text and '<dt>Used by backups</dt><dd>4 KiB</dd>' in destination.text and 'Free on that filesystem' in destination.text
    assert client.post('/sites/dump/backup/remote', data={'csrf': csrf(page)}).status_code == 200
    with ledger.db() as db: assert db.execute('SELECT next_run FROM remote_cycles').fetchone()[0] == 0
    data = {'csrf': csrf(page), 'id': str(uuid.uuid4())}
    assert client.post('/sites/dump/backup/database', data=data).status_code == 200
    assert len(ledger.backup_jobs()) == 1
    assert client.post('/sites/dump/backup/database', data=data).status_code == 200
    assert len(ledger.backup_jobs()) == 1
    reply = client.post('/sites/dump/backup/schedule', data={'csrf': csrf(page), 'interval': '60'})
    assert reply.status_code == 200
    with ledger.db() as db:
        schedule = db.execute('SELECT * FROM backup_schedules').fetchone()
        assert schedule['interval'] == 60 and not schedule['enabled']


def test_domain_form_requires_csrf_and_queues_a_durable_change(setup):
    client, _, ledger = setup
    login(client)
    row = ledger.submit(str(uuid.uuid4()), {"name": "domains", "domain": "preview.example.com"})
    ledger.update(row["id"], "succeeded", "published")
    page = client.get("/sites/domains")
    data = {"id": str(uuid.uuid4()), "domain": "live.example.com", "aliases": "preview.example.com\nwww.live.example.com"}
    assert client.post("/sites/domains/domains", data=data).status_code == 403
    assert not ledger.domain_jobs()
    data["csrf"] = csrf(page)
    response = client.post("/sites/domains/domains", data=data)
    assert response.status_code == 200 and "Latest domain change: queued" in response.text
    assert len(ledger.domain_jobs()) == 1
    assert ledger.domains(row) == ["preview.example.com"]
    ledger.finish_domains(ledger.domain_jobs()[0])
    response = client.get("/sites/domains")
    assert 'href="https://live.example.com"' in response.text
    assert "www.live.example.com" in response.text


def test_dynamic_php_selector_and_switch_are_authenticated(setup, monkeypatch):
    from reeve import versions
    monkeypatch.setattr(versions, "choices", lambda: [{"branch": branch, "available": True, "installed": False} for branch in ("8.2", "8.3")])
    client, _, ledger = setup
    assert client.get("/versions").status_code == 401
    login(client)
    page = client.get("/create")
    assert 'value="8.2"' in page.text and 'value="8.3"' in page.text
    data = {"csrf": csrf(page), "id": str(uuid.uuid4()), "name": "dynamic", "domain": "dynamic.example.com", "runtime": "php", "php_version": "8.2"}
    assert client.post("/create", data=data).status_code == 200
    row = ledger.list()[0]
    ledger.update(row["id"], "succeeded", "published")
    page = client.get("/sites/dynamic")
    change = {"id": str(uuid.uuid4()), "branch": "8.3"}
    assert client.post("/sites/dynamic/php", data=change).status_code == 403
    assert not ledger.runtime_jobs()
    change["csrf"] = csrf(page)
    assert client.post("/sites/dynamic/php", data=change).status_code == 200
    assert ledger.runtime_jobs()[0]["kind"] == "switch"
    assert ledger.runtime_branch(row) == "8.2"
    ledger.finish_runtime(ledger.runtime_jobs()[0], "8.3")
    page = client.get("/sites/dynamic")
    assert "Current branch: 8.3" in page.text
    assert "Restore previous PHP runtime" in page.text


def test_database_create_and_credentials_require_auth_csrf(setup, monkeypatch):
    from reeve import database_site
    client, _, ledger = setup
    assert client.post('/sites/alpha/database/credentials').status_code == 401
    login(client)
    page=client.get('/create')
    response=client.post('/create',data={'csrf':csrf(page),'id':str(uuid.uuid4()),'name':'alpha','domain':'alpha.hosting.test',
        'db_engine':'postgres','db_series_postgres':'18'})
    row=ledger.list()[0]
    assert json.loads(row['payload'])['database']=={'engine':'postgres','series':'18','usage':'standard'}
    monkeypatch.setattr(database_site,'credentials',lambda row:{'password':'test-private-db-password'})
    assert client.post('/sites/alpha/database/credentials').status_code==403
    assert 'test-private-db-password' not in client.get('/api/sites').text
    reply=client.post('/sites/alpha/database/credentials',data={'csrf':csrf(response)})
    assert reply.status_code==200 and 'test-private-db-password' in reply.text
    assert reply.headers['cache-control']=='no-store'


def test_static_overview_exposes_schedules_without_php_and_returns_after_save(setup, monkeypatch):
    from reeve import schedules
    monkeypatch.setattr(schedules, 'export', lambda *args: None)
    client, _, ledger = setup
    row = ledger.submit(str(uuid.uuid4()), {'name': 'static', 'domain': 'static.example.com'})
    ledger.update(row['id'], 'succeeded', 'published')
    assert client.get('/sites/static').status_code == 401
    login(client)
    page = client.get('/sites/static')
    assert 'No scheduled tasks.' in page.text and 'Files &amp; access' in page.text
    assert 'id="php-dialog"' not in page.text and 'id="routing-dialog"' not in page.text
    form = page.text.split('id="schedule-new"')[1]
    assert 'value="shell"' in form and 'value="php"' not in form and 'value="wp"' not in form
    data = {'name': 'maintenance', 'interval': '15', 'tool': 'shell', 'arguments': 'printf "<check>"',
            'path': '.', 'return_to': 'overview'}
    assert client.post('/sites/static/schedules', data=data).status_code == 403
    response = client.post('/sites/static/schedules', data=dict(data, csrf=csrf(page)), follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/sites/static'
    page = client.get('/sites/static')
    summary = page.text.split('id="scheduled-tasks"')[1].split('</section>')[0]
    assert 'maintenance' in summary and 'Paused' in summary and 'Every 15 minutes' in summary
    assert '&lt;check&gt;' in summary and '<check>' not in summary
    assert len(schedules.list_schedules(ledger, row['id'])) == 1
    # Editing a name is deliberately fixed in the UI; saving changes this schedule.
    client.post('/sites/static/schedules', data=dict(data, csrf=csrf(page), interval='30', enabled='yes'))
    assert len(schedules.list_schedules(ledger, row['id'])) == 1
    assert schedules.list_schedules(ledger, row['id'])[0]['interval'] == 30
    # An arbitrary target is never accepted as a redirect URL.
    response = client.post('/sites/static/schedules', data=dict(data, csrf=csrf(page), return_to='https://evil.test'), follow_redirects=False)
    assert response.headers['location'] == '/sites/static/schedules'


def test_overview_shows_schedule_failure_and_toolbox_state(setup, monkeypatch):
    from reeve import schedules, toolbox
    monkeypatch.setattr(schedules, 'export', lambda *args: None)
    client, _, ledger = setup
    row = ledger.submit(str(uuid.uuid4()), {'name': 'php', 'domain': 'php.example.com', 'runtime': 'php', 'php_version': '8.4'})
    ledger.update(row['id'], 'succeeded', 'published')
    schedules.save(ledger, row['id'], {'name': 'wordpress', 'interval': 1, 'enabled': True, 'tool': 'wp',
        'arguments': 'cron event run --due-now', 'path': '.', 'internet': False})
    due = schedules.list_schedules(ledger, row['id'])[0]['next_run']
    schedules.tick(ledger, due+1)
    job = ledger.content_jobs()[0]
    ledger.update_content(job['id'], 'failed', 'failed', 'Synthetic command failure')
    schedules.tick(ledger, due+62)
    monkeypatch.setattr(toolbox, 'status', lambda *args: {'state': 'active', 'details': {'recipe': 'workbench'}})
    login(client)
    page = client.get('/sites/php')
    assert page.status_code == 200 and 'id="php-dialog"' in page.text
    summary = page.text.split('id="scheduled-tasks"')[1].split('</section>')[0]
    assert 'wp cron event run --due-now' in summary and 'Failed' in summary and 'Paused' in summary
    assert '/content/output/'+job['id'] in summary
    assert 'Active · workbench' in page.text and 'Manage toolbox' in page.text
    assert 'An operation needs to finish' in page.text or 'SSH toolbox is active' in page.text
    # Interrupted jobs stay visible for review without a repeated completion refresh.
    ledger.update_content(job['id'], 'recovery-needed', 'interrupted')
    page = client.get('/sites/php')
    assert 'Review the interrupted operation' in page.text
    assert 'data-pending="no"' in page.text


def test_site_rules_form_queues_a_durable_job_for_managed_sites_only(setup, monkeypatch):
    from reeve import site_rules
    client, _, ledger = setup
    monkeypatch.setattr(site_rules, 'SITES', ledger.sites); monkeypatch.setattr(site_rules, 'trusted', lambda *a, **k: None)
    row = ledger.submit(str(uuid.uuid4()), {'name': 'pages', 'domain': 'pages.example.com'})
    ledger.update(row['id'], 'succeeded', 'published')
    (ledger.sites / 'pages/conf').mkdir(parents=True); (ledger.sites / 'pages/conf/site.nginx.conf').write_text('# none yet\n')
    login(client)
    page = client.get('/sites/pages')
    assert 'data-dialog="rules-dialog"' in page.text and 'name="text"' in page.text
    reply = client.post('/sites/pages/rules', data={'csrf': csrf(page), 'id': str(uuid.uuid4()), 'text': 'location = /a { return 301 /b; }', 'return_to': 'overview'}, follow_redirects=False)
    assert reply.status_code == 303 and reply.headers['location'] == '/sites/pages'
    job = ledger.content_jobs(row['id'])[0]
    assert job['kind'] == 'site-rules' and job['state'] == 'queued' and json.loads(job['payload']) == {'text': 'location = /a { return 301 /b; }\n'}
    assert client.post('/sites/pages/rules', data={'csrf': csrf(page), 'id': str(uuid.uuid4()), 'text': 'x' * 66000}).status_code == 400  # over the 64 KiB rules limit, within the form limit


def test_php_limits_form_queues_a_durable_job_for_php_sites_only(setup, monkeypatch):
    from reeve import php_settings
    client, _, ledger = setup
    monkeypatch.setattr(php_settings, 'SITES', ledger.sites); monkeypatch.setattr(php_settings, 'trusted', lambda *a, **k: None)
    row = ledger.submit(str(uuid.uuid4()), {'name': 'shop', 'domain': 'shop.example.com', 'runtime': 'php', 'php_version': '8.3'})
    ledger.update(row['id'], 'succeeded', 'published')
    (ledger.sites / 'shop/conf').mkdir(parents=True)
    login(client)
    page = client.get('/sites/shop')
    assert 'data-dialog="php-limits-dialog"' in page.text and 'id="php-limits-dialog"' in page.text and page.text.count('id="php-dialog"') == 1 and 'name="max_input_vars"' in page.text and '120 s execution · 128 MB upload' in page.text
    values = {'max_execution_time': '120', 'upload_max_filesize_mb': '64', 'post_max_size_mb': '72', 'max_input_vars': '3000', 'memory_limit_mb': '512'}
    reply = client.post('/sites/shop/php-settings', data={'csrf': csrf(page), 'id': str(uuid.uuid4()), 'return_to': 'overview', **values}, follow_redirects=False)
    assert reply.status_code == 303 and reply.headers['location'] == '/sites/shop'
    job = ledger.content_jobs(row['id'])[0]
    assert job['kind'] == 'php-settings' and job['state'] == 'queued' and json.loads(job['payload']) == {k: int(v) for k, v in values.items()}
    assert client.post('/sites/shop/php-settings', data={'csrf': csrf(page), 'id': str(uuid.uuid4()), **values, 'post_max_size_mb': '8'}).status_code == 400
    static = ledger.submit(str(uuid.uuid4()), {'name': 'pages', 'domain': 'pages.example.com'})
    ledger.update(static['id'], 'succeeded', 'published'); (ledger.sites / 'pages/conf').mkdir(parents=True)
    assert 'php-dialog' not in client.get('/sites/pages').text


def test_customer_sftp_and_ownership_forms_queue_durable_jobs(setup, monkeypatch):
    from reeve import sftp
    client, _, ledger = setup
    monkeypatch.setattr(sftp, 'ROOT', ledger.sites.parent / 'sftp'); monkeypatch.setattr(sftp, 'trusted', lambda *a, **k: None)
    row = ledger.submit(str(uuid.uuid4()), {'name': 'pages', 'domain': 'pages.example.com'})
    ledger.update(row['id'], 'succeeded', 'published'); (ledger.sites / 'pages/conf').mkdir(parents=True)
    login(client)
    page = client.get('/sites/pages')
    assert 'data-dialog="sftp-dialog"' in page.text and 'name="secondary"' in page.text and 'Customer SFTP</strong><small>Off' in page.text and 'Turn on' in page.text and 'name="duration"' in page.text
    key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGxOaNNRJKLd9yPcoRvRsHMDaKdSyqRchiLoYW1hGY4v dev'
    reply = client.post('/sites/pages/sftp', data={'csrf': csrf(page), 'id': str(uuid.uuid4()), 'action': 'on', 'secondary': key + '\n', 'duration': '4h', 'return_to': 'overview'}, follow_redirects=False)
    assert reply.status_code == 303, reply.text
    job = ledger.content_jobs(row['id'])[0]
    assert job['kind'] == 'sftp-access' and json.loads(job['payload']) == {'action': 'on', 'secondary': [key.rsplit(' ', 1)[0]], 'duration': '4h'}
    assert client.post('/sites/pages/sftp', data={'csrf': csrf(page), 'id': str(uuid.uuid4()), 'action': 'on', 'secondary': 'ssh-rsa junk'}).status_code == 400
    assert client.get('/sites/pages/sftp/key', follow_redirects=False).status_code == 400  # off: no key to hand out
    ledger.update_content(job['id'], 'succeeded', 'completed')  # one content job at a time
    reply = client.post('/sites/pages/fix-ownership', data={'csrf': csrf(page), 'id': str(uuid.uuid4())}, follow_redirects=False)
    assert reply.status_code == 303 and reply.headers['location'] == '/sites/pages/files'
    assert ledger.content_jobs(row['id'])[0]['kind'] == 'fix-ownership'
    assert 'Fix ownership' in client.get('/sites/pages/files').text


def test_mail_page_shows_relay_queue_and_sites(setup, monkeypatch):
    client, _, ledger = setup
    login(client)
    from reeve import mail
    state = {'set_up': True, 'running': True, 'mode': 'sink', 'relayhost': '', 'hostname': 'hosting', 'rate_per_hour': 100, 'public_ip': '', 'sink': True, 'error': '',
             'queue': [{'id': '3AAA02', 'queue': 'deferred', 'arrived': 1789718000, 'size': 900, 'sender': 'me@blog.example.com', 'site': 'blog', 'recipients': ['b@example.org'], 'reason': 'connect timed out'}],
             'sites': [{'name': 'blog', 'attached': True, 'hour': {'sent': 2, 'deferred': 1, 'bounced': 0, 'limited': 0}, 'day': {'sent': 5, 'deferred': 1, 'bounced': 1, 'limited': 0}, 'month': {'sent': 40, 'deferred': 1, 'bounced': 1, 'limited': 3}, 'last': 1789718000, 'domains': [('example.org', 3)], 'recent': []}],
             'log': 'postfix/master: daemon started'}
    monkeypatch.setattr(mail, 'status', lambda host: state)
    page = client.get('/mail')
    assert page.status_code == 200 and 'Mailpit sink' in page.text and 'connect timed out' in page.text and '3 times in 30 days' in page.text and 'example.org (3)' in page.text
    dropped = []
    monkeypatch.setattr(mail, 'delete', lambda host, ident: dropped.append(ident) or {'deleted': ident})
    reply = client.post('/mail/delete', data={'csrf': csrf(page), 'id': '3AAA02'}, follow_redirects=False)
    assert reply.status_code == 303 and dropped == ['3AAA02']
    assert '<a href="/mail" aria-current="page">Mail</a>' in page.text  # the header names the section


def test_backups_page_offers_a_connection_and_takes_one(setup, monkeypatch):
    client, _, ledger = setup
    login(client)
    from reeve import destination
    monkeypatch.setattr(destination, 'overview', lambda: {'configured': False, 'server_public_key': None, 'fingerprints': []})
    page = client.get('/backups')
    assert page.status_code == 200 and 'Connect a destination' in page.text and 'Make this server\'s key' in page.text and 'name="bucket"' in page.text
    seen = {}
    monkeypatch.setattr(destination, 'connect', lambda data: seen.update(data) or {'type': 'sftp', 'repository': 'sftp://u@h:22//x', 'repository_id': 'f' * 64, 'generated_password': 'p' * 64, 'fingerprints': ['256 SHA256:nas (ED25519)']})
    reply = client.post('/backups/connect', data={'csrf': csrf(page), 'type': 'sftp', 'host': 'h', 'port': '22', 'username': 'u', 'path': '/x', 'auth': 'key'})
    assert reply.status_code == 200 and seen['host'] == 'h' and 'csrf' not in seen and 'shown once' in reply.text and 'p' * 64 in reply.text and 'SHA256:nas' in reply.text
    monkeypatch.setattr(destination, 'connect', lambda data: (_ for _ in ()).throw(ValueError('The destination did not answer with a host key')))
    reply = client.post('/backups/connect', data={'csrf': csrf(page), 'type': 'sftp', 'host': 'h', 'username': 'u', 'path': '/x'})
    assert reply.status_code == 200 and 'did not answer' in reply.text
    assert client.post('/backups/disconnect', data={'csrf': csrf(page), 'confirm': 'no'}).status_code == 200


def test_home_shows_the_server_summary_and_survives_a_busy_worker(tmp_path):
    auth = Auth(tmp_path / "auth.db"); auth.set_password("test-password-unique-123")
    ledger = Ledger(tmp_path / "jobs.db", sites=tmp_path / "sites")
    row = ledger.submit(str(uuid.uuid4()), {"name": "shop", "domain": "shop.hosting.test", "data_mb": 16})
    ledger.submit(str(uuid.uuid4()), {"name": "alpha", "domain": "alpha.hosting.test", "data_mb": 16})  # newer, but listed first by name
    host = SimpleNamespace(defaults=DEFAULTS, health=lambda row: {"application": "unknown", "container": "absent", "quota": None})
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"at": 1_700_000_000.0, "errors": {}, "took": 0.3,
        "cpu": {"cores": 4, "load": [1.78, 1.45, 1.4], "pressure": 8.77},
        "memory": {"total": 16 * 1024 ** 3, "available": 10 * 1024 ** 3, "used": 6 * 1024 ** 3, "swap_used": 0, "pressure": 0.0},
        "disk": {"srv": {"total": 200 * 1024 ** 3, "free": 138 * 1024 ** 3}, "root": {"total": 37 * 1024 ** 3, "free": 24 * 1024 ** 3}, "sites_used": 5 * 1024 ** 3, "backups_used": 39 * 1024 ** 3,
                 "docker": {"images": {"size": 8_000_000_000, "reclaimable": 1_000_000_000}, "build_cache": {"size": 9_000_000_000, "reclaimable": 6_000_000_000}}},
        "backups": {"state": "configured", "type": "sftp", "pending": 2, "last_copy": 1_700_000_000.0, "error": ""},
        "mail": {"set_up": True, "running": True, "mode": "sink", "queued": 1, "hour": {"sent": 3, "deferred": 0, "bounced": 0, "limited": 0}, "day": {"sent": 9, "deferred": 1, "bounced": 0, "limited": 0}, "error": ""},
        "sftp": {"on": ["shop"], "running": True},
        "sites": [{"id": row["id"], "name": "shop", "domain": "shop.hosting.test", "state": "succeeded", "runtime": "static", "health": "healthy",
                   "disk_used": 200 * 1024 ** 2, "disk_hard": 1024 ** 3, "memory": 20_000_000, "cpu": 0.5, "containers": 1}]}))
    working = {"on": True}
    def call(message):
        if not working["on"]: raise OSError("worker socket busy")
        return dispatch(message, ledger, host)
    client = TestClient(create_app(tmp_path / "auth.db", call, status_path=status))
    page = login(client)
    for text in ("Figures as of 22:13 UTC", "1.78", "load, 4 cores", "6.0 GiB", "used of 16.0 GiB", "138 GiB", "6.5 GiB reclaimable", "SFTP destination", "2 pending",
                 "queued · sink mode", "last hour 3 sent", "site on", "200 MiB", "of 1.0 GiB", "19.1 MiB", "50%"):
        assert text in page.text, text
    assert "queued" in page.text  # the live state from the worker
    with ledger.db() as db:
        import time
        db.execute('INSERT INTO traffic VALUES (?,?,?,?,?,?,?,?)', (row['id'], int(time.time() // 3600) * 3600, 1234, 5 * 1024 ** 2, 1200, 30, 4, 2))
    detail = client.get("/sites/shop")
    assert "Traffic" in detail.text and "Last 24 hours" in detail.text and "Last 30 days" in detail.text and "1,234" in detail.text and "4 server errors" in detail.text and detail.text.count(" bad\"") == 2 and "class=\"h20 bad\"" in detail.text
    assert page.text.index("alpha.hosting.test") < page.text.index("shop.hosting.test")
    working["on"] = False
    page = client.get("/")
    assert "Worker busy or unavailable" in page.text and "shop.hosting.test" in page.text and "healthy" in page.text and "138 GiB" in page.text
    empty = TestClient(create_app(tmp_path / "auth.db", call, status_path=tmp_path / "none.json"))
    login(empty)
    working["on"] = True
    page = empty.get("/")
    assert "No server summary yet" in page.text and "shop.hosting.test" in page.text


def test_header_navigation_marks_the_section_and_the_footer_names_the_release(tmp_path):
    auth = Auth(tmp_path / "auth.db"); auth.set_password("test-password-unique-123")
    ledger = Ledger(tmp_path / "jobs.db", sites=tmp_path / "sites")
    ledger.submit(str(uuid.uuid4()), {"name": "shop", "domain": "shop.hosting.test", "data_mb": 16})
    host = SimpleNamespace(defaults=DEFAULTS, health=lambda row: {"application": "unknown", "container": "absent", "quota": None}, inspect=lambda name: None)
    (tmp_path / "release.json").write_text(json.dumps({"schema": 1, "current": "7e4a3c7025dc6e8347bab8648d5e427acdfb2e42", "previous": "f9fde5d4d61c5770f53756c721f94976a9bd67ef"}))
    (tmp_path / "status.json").write_text(json.dumps({"at": 1.0, "errors": {}, "hostname": "hosting", "sites": []}))
    client = TestClient(create_app(tmp_path / "auth.db", lambda m: dispatch(m, ledger, host), status_path=tmp_path / "status.json", release_path=tmp_path / "release.json"))
    page = client.get("/login")
    assert "<title>Sign in · Reeve</title>" in page.text and 'id="navbar-menu"' not in page.text  # no sections before signing in
    page = login(client)
    assert 'class="navbar-brand navbar-brand-autodark pe-0 pe-md-3"><a href="/">Reeve</a>' in page.text and 'href="/" aria-current="page"' in page.text and 'href="/mail">Mail' in page.text
    assert "Reeve Panel · 7e4a3c7, previous f9fde5d · on hosting" in page.text and "Backup destination</a> · " not in page.text
    assert '<div class="page-pretitle">hosting</div>' in page.text
    # One top-level entry is active per page: the section itself, or the group that holds the page.
    for path, label, old_label in (("/sites/shop", "Sites", "Sites"), ("/mail", "Server", "Mail"), ("/backups", "Backups", "Backups"), ("/versions", "Server", "PHP"), ("/databases/versions", "Server", "Databases"), ("/history", "Backups", "History"), ("/settings", "Settings", "Settings")):
        text = client.get(path).text
        if 'id="navbar-menu"' in text:
            nav = text[text.index('id="navbar-menu"'):text.index('</ul></div>')]
            active = re.findall(r'<li class="nav-item(?: dropdown)? active">.*?<span class="nav-link-title">([^<]+)</span>', nav)
            assert active == [label], (path, active)
        else:   # a page still on the legacy frame, until the UI refresh reaches it
            nav = text[text.index('class="top-nav"'):text.index('</nav>')]
            assert nav.count('aria-current="page"') == 1 and f'aria-current="page">{old_label}<' in nav, path
    bare = TestClient(create_app(tmp_path / "auth.db", lambda m: dispatch(m, ledger, host), status_path=tmp_path / "none.json", release_path=tmp_path / "no-release.json"))
    login(bare)
    assert 'small">Reeve Panel</div>' in bare.get("/").text


def test_create_form_offers_mariadb_first_with_the_profile_usage_and_the_site_page_changes_usage(setup, tmp_path, monkeypatch):
    client, _, ledger = setup
    login(client)
    page = client.get('/create')
    assert page.text.index('value="mariadb"') < page.text.index('value="mysql"') and '<option value="standard" selected>' in page.text
    ident = str(uuid.uuid4())
    response = client.post('/create', data={'csrf': csrf(page), 'id': ident, 'name': 'dbsite', 'domain': 'dbsite.hosting.test', 'db_engine': 'mariadb', 'db_series_mariadb': '11.8', 'db_usage': 'light'})
    row = ledger.get(ident)
    assert json.loads(row['payload'])['database']['usage'] == 'light'
    # A ready database offers the usage change, which queues the durable job.
    from reeve import database_site as ds
    monkeypatch.setattr(ds, 'public', lambda row, host: {'engine': 'mariadb', 'version': '11.8.2', 'stage': 'ready', 'health': 'healthy', 'container': 'running', 'usage': 'light', 'usages': ['light', 'standard', 'high'], 'limits': {}, 'host': 'db', 'port': 3306, 'name': 'site', 'user': 'site'})
    ledger.update(ident, 'succeeded', 'published')
    page = client.get('/sites/dbsite')
    assert 'usage light' in page.text and 'id="database-usage-dialog"' in page.text and '<option value="light" selected>' in page.text
    reply = client.post('/sites/dbsite/database-usage', data={'csrf': csrf(page), 'id': str(uuid.uuid4()), 'usage': 'standard'}, follow_redirects=False)
    assert reply.status_code == 303
    job = ledger.content_jobs(ident)[0]
    assert job['kind'] == 'database-usage' and json.loads(job['payload']) == {'usage': 'standard'}
    assert client.post('/sites/dbsite/database-usage', data={'csrf': csrf(page), 'id': str(uuid.uuid4()), 'usage': 'huge'}).status_code == 400


def test_recover_page_offers_one_source_and_a_contextual_restore(setup, monkeypatch, tmp_path):
    from reeve import restoration
    client, _, ledger = setup
    live = ledger.submit(str(uuid.uuid4()), {'name': 'shop', 'domain': 'shop.example.com', 'runtime': 'php', 'php_version': '8.4'}); ledger.update(live['id'], 'succeeded', 'published')
    monkeypatch.setattr(restoration, 'SCAN', tmp_path / 'scan.json')
    backup = str(uuid.UUID(int=5)); dump = str(uuid.UUID(int=6)); other = str(uuid.UUID(int=7))
    restoration.write_scan({'state': 'succeeded', 'source': 'local', 'finished_at': 1.0, 'unread': 0, 'record': None, 'sites': [
        {'name': 'shop', 'site_kind': 'managed', 'runtime': 'php', 'domains': ['shop.example.com'], 'live': {'name': 'shop', 'managed': True, 'id': live['id']},
         'backups': [{'id': backup, 'source': 'local', 'snapshot': '', 'completed_at': 2.0, 'bytes': 10, 'has_dump': True, 'kind': 'site', 'site_name': 'shop'}],
         'dumps': [{'id': dump, 'source': 'local', 'snapshot': '', 'completed_at': 3.0, 'engine': 'mariadb', 'kind': 'dump', 'site_name': 'shop'}]},
        {'name': 'blog', 'site_kind': 'managed', 'runtime': 'static', 'domains': ['blog.example'], 'live': None,
         'backups': [{'id': other, 'source': 'local', 'snapshot': '', 'completed_at': 2.0, 'bytes': 10, 'has_dump': False, 'kind': 'site', 'site_name': 'blog'}], 'dumps': []}]})
    login(client)
    page = client.get('/recover').text
    assert 'value="site:' + backup + '"' in page and 'value="dump:' + dump + '"' in page   # one From choice per site
    assert 'name="as-0"' in page and 'Into shop: files and database' in page and 'name="target-0" value="shop"' in page   # live site: contextual
    assert 'name="as-1" value="new"' in page and 'name="name-1" value="blog"' in page and 'name="domains-1" value="blog.example"' in page  # not live: a new site
    reply = client.post('/recover/submit', data={'csrf': csrf(client.get('/recover')), 'include': ['0', '1'], 'from-0': 'dump:' + dump, 'as-0': 'both', 'target-0': 'shop', 'name-0': '', 'domains-0': '',
                                                  'from-1': 'site:' + other, 'as-1': 'new', 'name-1': 'blog', 'domains-1': 'blog.example www.blog.example'})
    assert reply.status_code == 200 and 'Recoveries' in reply.text
    rows = restoration.recoveries(ledger)
    assert sorted((r['mode'], r['backup_id']) for r in rows) == sorted([('dump', dump), ('new', other)])
    # Manage mode lists every backup with a download and a deletion; a whole site's deletion needs its name typed.
    page = client.get('/recover?mode=manage').text
    assert 'Backups found' in page and page.count('name="backup" value="site:' + backup + '"') >= 2 and 'name="backup" value="dump:' + dump + '"' in page
    assert 'Delete every backup of blog' in page and 'Downloads and deletions' in page
    token = csrf(client.get('/recover?mode=manage'))
    reply = client.post('/recover/manage', data={'csrf': token, 'action': 'delete', 'site': 'blog', 'confirm': 'wrong', 'backup': ['site:' + other]})
    assert 'Type the site name exactly' in reply.text and not restoration.actions(ledger)
    reply = client.post('/recover/manage', data={'csrf': token, 'action': 'download', 'backup': ['site:' + backup]})
    assert reply.status_code == 200 and 'download · queued' in reply.text
    reply = client.post('/recover/manage', data={'csrf': token, 'action': 'delete', 'site': 'blog', 'confirm': 'blog', 'backup': ['site:' + other]})
    assert 'recovery or another action is using that backup' in reply.text   # the restore queued above still holds it
    for r in rows: restoration.update(ledger, r['id'], 'succeeded', 'done')
    reply = client.post('/recover/manage', data={'csrf': token, 'action': 'delete', 'site': 'blog', 'confirm': 'blog', 'backup': ['site:' + other]})
    assert reply.status_code == 200 and 'delete · queued' in reply.text
    assert sorted((a['action'], a['backup_id']) for a in restoration.actions(ledger)) == sorted([('download', backup), ('delete', other)])


def test_a_tunnel_address_passes_the_host_check_but_other_names_do_not(setup):
    client, _, _ = setup
    assert client.get('/login', headers={'host': '10.181.134.1:8088'}).status_code == 200
    assert client.get('/login', headers={'host': '10.7.7.7'}).status_code == 200
    assert client.get('/login', headers={'host': 'evil.example'}).status_code == 400
    assert client.get('/login', headers={'host': '192.168.1.5:8088'}).status_code == 400


def test_settings_page_shows_both_retention_policies_and_asks_before_removing_backups(setup, monkeypatch):
    from reeve import settings as st
    client, _, ledger = setup
    login(client)
    page = client.get('/settings').text
    assert 'name="local_daily"' in page and 'name="remote_monthly"' in page and 'Here, on this server' in page
    monkeypatch.setattr(st, 'surplus', lambda ledger, values: {'local': {'count': 3, 'bytes': 5 * 1048576, 'ids': ['a', 'b', 'c']}, 'remote': {'count': 0, 'bytes': 0, 'ids': []}})
    saved = []
    monkeypatch.setattr(st, 'save', lambda host, ledger, group, values: saved.append(values) or {'group': group, 'saved': {}, 'note': 'ok'})
    values = {'csrf': csrf(client.get('/settings')), 'hour': '3', 'local_path': '/srv/backups', 'database_days': '2', 'local_daily': '1', 'local_weekly': '0', 'local_monthly': '0', 'remote_daily': '7', 'remote_weekly': '4', 'remote_monthly': '12'}
    reply = client.post('/settings/backups', data=values)
    assert 'Before these counts apply' in reply.text and '<strong>3</strong> here (5.0 MiB)' in reply.text and not saved
    assert 'name="existing" value="keep"' in reply.text and 'name="local_daily" value="1"' in reply.text
    reply = client.post('/settings/backups', data={**values, 'existing': 'keep'})
    assert reply.status_code == 200 and saved and saved[0]['existing'] == 'keep'
