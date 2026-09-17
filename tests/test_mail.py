import json
import uuid
from types import SimpleNamespace

import pytest
import yaml

from reeve import mail, host as hm

ENTRIES = {'shop': {'id': 'a', 'domains': ['shop.example.com', 'www.shop.example.com'], 'subnet': '10.240.35.0/24'},
           'my-blog': {'id': 'b', 'domains': ['blog.example.com'], 'subnet': '10.240.7.0/24'}}
CONFIG = {**mail.DEFAULTS, 'hostname': 'server.example.com', 'rate_per_hour': 50}


def test_settings_are_validated(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, 'OPS', tmp_path)
    assert mail.settings() == mail.DEFAULTS
    (tmp_path / 'server.yaml').write_text(yaml.safe_dump({'schema': 1, 'mail': {'mode': 'sink', 'public_ip': '203.0.113.10', 'rate_per_hour': 20}}))
    assert mail.settings()['mode'] == 'sink' and mail.settings()['public_ip'] == '203.0.113.10'
    for bad in ({'mode': 'imap'}, {'relayhost': 'x', 'mode': 'relay', 'rate_per_hour': 'ten'}, {'rate_per_hour': 0}, {'public_ip': 'nope'}, {'hostname': 'Bad Host'}, {'other': 1}):
        (tmp_path / 'server.yaml').write_text(yaml.safe_dump({'mail': bad}))
        with pytest.raises(ValueError): mail.settings()


def test_render_restricts_each_site_to_its_network_and_domains():
    files = mail.render(CONFIG, ENTRIES)
    main = files['main.cf']
    assert 'myhostname = server.example.com' in main and 'relayhost = \n' in main and 'smtp_tls_security_level = may' in main
    assert 'smtpd_client_message_rate_limit = 50' in main and 'inet_interfaces = all' in main and 'mydestination =\n' in main
    assert 'smtpd_restriction_classes = site_my_blog site_shop' in main
    assert "site_shop = check_sender_access texthash:/etc/postfix/hosting/senders-shop, check_sender_access static:{REJECT the sender address must be at one of this site's domains}" in main
    assert files['maps']['clients.cidr'] == '10.240.7.0/24 OK\n10.240.35.0/24 OK\n0.0.0.0/0 REJECT not a hosted site\n'
    assert files['maps']['classes.cidr'].startswith('10.240.7.0/24 site_my_blog\n10.240.35.0/24 site_shop\n')
    assert files['maps']['senders-shop'] == 'shop.example.com OK\nwww.shop.example.com OK\n'
    assert 'chroot' not in files['master.cf'].split('\n', 1)[1].replace('no chroot', '') and 'smtp      inet  n       -       n' in files['master.cf']
    sink = mail.render({**CONFIG, 'mode': 'sink'}, ENTRIES)['main.cf']
    assert 'relayhost = [hosting-mailpit]:1025' in sink and 'smtp_tls_security_level = none' in sink
    relay = mail.render({**CONFIG, 'mode': 'relay', 'relayhost': '[email-smtp.eu-west-2.amazonaws.com]:587'}, ENTRIES)['main.cf']
    assert 'relayhost = [email-smtp.eu-west-2.amazonaws.com]:587' in relay


LOG = '''2026-09-17T09:00:00.000000000Z Sep 17 09:00:00 hosting-mail postfix/smtpd[40]: 3AAA01: client=unknown[10.240.35.3]
2026-09-17T09:00:00.100000000Z Sep 17 09:00:00 hosting-mail postfix/qmgr[30]: 3AAA01: from=<orders@shop.example.com>, size=1200, nrcpt=1 (queue active)
2026-09-17T09:00:01.000000000Z Sep 17 09:00:01 hosting-mail postfix/smtp[50]: 3AAA01: to=<a@gmail.com>, relay=gmail-smtp-in.l.google.com[1.2.3.4]:25, delay=1, status=sent (250 ok)
2026-09-17T09:10:00.000000000Z Sep 17 09:10:00 hosting-mail postfix/smtpd[41]: 3AAA02: client=unknown[10.240.7.9]
2026-09-17T09:10:01.000000000Z Sep 17 09:10:01 hosting-mail postfix/smtp[51]: 3AAA02: to=<b@example.org>, relay=none, delay=1, status=deferred (connect timed out)
2026-09-17T09:20:00.000000000Z Sep 17 09:20:00 hosting-mail postfix/smtpd[42]: warning: Message delivery request rate limit exceeded: 51 from hosting-php-shop.hosting-backend-shop[10.240.35.3] for service smtp
2026-08-01T09:00:00.000000000Z Aug  1 09:00:00 hosting-mail postfix/smtpd[43]: 3AAA03: client=unknown[10.240.35.3]
2026-08-01T09:00:01.000000000Z Aug  1 09:00:01 hosting-mail postfix/smtp[53]: 3AAA03: to=<old@example.net>, relay=x, delay=1, status=bounced (550 no such user)
2026-09-17T09:30:00.000000000Z Sep 17 09:30:00 hosting-mail postfix/smtpd[44]: 3AAA04: client=unknown[10.240.99.9]
2026-09-17T09:30:01.000000000Z Sep 17 09:30:01 hosting-mail postfix/smtp[54]: 3AAA04: to=<x@y>, relay=x, delay=1, status=sent (250 ok)
'''


def test_log_parsing_counts_per_site_by_client_network_and_window():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 17, 10, 20, tzinfo=timezone.utc).timestamp()
    sites = mail.parse_log(LOG, ENTRIES, now)
    shop, blog = sites['shop'], sites['my-blog']
    assert shop['hour']['sent'] == 0 and shop['day']['sent'] == 1 and shop['month']['sent'] == 1  # 09:00 is 80 minutes ago
    assert shop['hour']['limited'] == 1 and shop['month']['bounced'] == 0  # August is outside 30 days
    assert blog['day']['deferred'] == 1 and blog['day']['sent'] == 0
    assert shop['domains'] == [('gmail.com', 1)] and shop['recent'][0]['to'] == 'a@gmail.com' and shop['recent'][0]['status'] == 'sent'
    assert shop['last'] == datetime(2026, 9, 17, 9, 0, 1, tzinfo=timezone.utc).timestamp()
    # A client outside every site's network is nobody's.
    assert sum(s['month']['sent'] for s in sites.values()) == 1
    # The persisted log has plain syslog stamps without a year: the latest year not in the future.
    plain = '\n'.join(l.split(' ', 1)[1] for l in LOG.splitlines())
    again = mail.parse_log(plain, ENTRIES, now)
    assert again['shop']['day']['sent'] == 1 and again['my-blog']['day']['deferred'] == 1 and again['shop']['hour']['limited'] == 1
    assert mail.stamp_to_time(None, 'Dec 31 23:00:00', datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()) == datetime(2026, 12, 31, 23, tzinfo=timezone.utc).timestamp()


def test_queue_parsing_matches_messages_to_sites_by_sender_domain():
    text = json.dumps({'queue_name': 'deferred', 'queue_id': '3AAA02', 'arrival_time': 1789718000, 'message_size': 900, 'sender': 'me@blog.example.com',
                       'recipients': [{'address': 'b@example.org', 'delay_reason': 'connect timed out'}]}) + '\n' + json.dumps({'queue_name': 'active', 'queue_id': '3AAA05', 'arrival_time': 1789719000, 'message_size': 90, 'sender': 'x@nowhere.test', 'recipients': [{'address': 'c@d'}]}) + '\n'
    items = mail.parse_queue(text, ENTRIES)
    assert [i['id'] for i in items] == ['3AAA05', '3AAA02'] and items[1]['site'] == 'my-blog' and items[1]['reason'] == 'connect timed out' and items[0]['site'] is None


@pytest.fixture
def world(tmp_path, monkeypatch):
    ops = tmp_path / 'ops'; (ops / 'w').mkdir(parents=True)  # the worker folder exists on a server
    for module in (mail, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(mail, 'OPS', ops); monkeypatch.setattr(mail, 'ROOT', ops / 'w/mail'); monkeypatch.setattr(mail, 'GENERATED', ops / 'w/mail/generated')
    monkeypatch.setattr(mail, 'STATE', ops / 'mail'); monkeypatch.setattr(mail, 'LOG', ops / 'mail/queue/hosting-log'); monkeypatch.setattr(mail, 'SHIM', ops / 'shim/hosting-sendmail'); monkeypatch.setattr(mail, 'SITES', tmp_path / 'sites')
    monkeypatch.setattr(mail, 'image', lambda step=None: {'version': mail.IMAGE_VERSION, 'image_id': 'sha256:mailimage'})
    monkeypatch.setattr(mail, 'hostname', lambda config: 'server.example.com')
    calls = []
    def fake_command(args, timeout=120):
        calls.append(args)
        if args[:3] == ['docker', 'network', 'inspect']: return json.dumps([{'IPAM': {'Config': [{'Subnet': '10.240.35.0/24' if args[3].endswith('shop') else '10.240.7.0/24'}]}}])
        if args[:3] == ['docker', 'network', 'ls']: return 'net\n'
        if args[:2] == ['docker', 'exec'] and args[-2:] == ['postfix', 'status']: return 'the Postfix mail system is running'
        return ''
    monkeypatch.setattr(mail, 'command', fake_command)
    live = {'value': None}
    host = SimpleNamespace(inspect=lambda name: live['value'] if name == mail.CONTAINER else None)
    return ops, host, calls, live


def test_deploy_creates_the_relay_on_every_site_network_and_refresh_reloads_without_restart(world):
    ops, host, calls, live = world
    sites = {'shop': {'id': 'a', 'domains': ['shop.example.com']}, 'my-blog': {'id': 'b', 'domains': ['blog.example.com']}}
    running = {'State': {'Running': True}, 'NetworkSettings': {'Networks': {mail.NETWORK: {}, 'hosting-backend-shop': {}, 'hosting-backend-my-blog': {}}}}
    started = []
    def inspect(name):
        if name != mail.CONTAINER: return None
        if any(c[:2] == ['docker', 'start'] for c in calls): started.append(1)
        return running if started else None
    host.inspect = inspect
    result = mail.deploy(host, sites)
    assert result == {'running': True, 'mode': 'direct', 'sites': ['my-blog', 'shop']}
    create = ' '.join(next(c for c in calls if c[:2] == ['docker', 'create']))
    assert '--publish' not in create and '--cap-drop ALL' in create and '--cap-add SYS_CHROOT' not in create and f'--network {mail.NETWORK}' in create
    assert f'src={ops}/mail/queue,dst=/var/spool/postfix' in create and f'src={mail.GENERATED},dst=/run/mail,readonly' in create and create.endswith('--entrypoint /run/mail/entrypoint.sh sha256:mailimage')
    assert ['docker', 'network', 'connect', '--alias', 'mail', 'hosting-backend-my-blog', 'hosting-mail'] in calls and ['docker', 'network', 'connect', '--alias', 'mail', 'hosting-backend-shop', 'hosting-mail'] in calls
    assert (mail.SHIM).exists() and (mail.GENERATED / 'maps/senders-shop').read_text() == 'shop.example.com OK\n' and (mail.GENERATED / 'entrypoint.sh').exists()
    assert mail.registry()['shop']['subnet'] == '10.240.35.0/24'
    import stat; assert stat.S_IMODE((ops / 'mail/queue').stat().st_mode) == 0o755
    # A third site attaches without a restart: one network connect and a reload.
    calls.clear()
    row = {'id': 'c', 'name': 'forum', 'payload': json.dumps({'runtime': 'php'})}
    assert mail.attach(host, row, ['forum.example.com']) is True
    assert not any(c[:2] == ['docker', 'create'] for c in calls)
    assert ['docker', 'network', 'connect', '--alias', 'mail', 'hosting-backend-forum', 'hosting-mail'] in calls
    assert any(c[:3] == ['docker', 'exec', 'hosting-mail'] and 'postfix reload' in c[-1] for c in calls)
    assert 'forum' in mail.registry() and 'site_forum' in (mail.GENERATED / 'main.cf').read_text()
    maps_inode = (mail.GENERATED / 'maps').stat().st_ino
    # Delete detaches: the network is left and the site forgotten.
    calls.clear(); mail.detach(host, {'id': 'a', 'name': 'shop', 'payload': '{}'})
    assert ['docker', 'network', 'disconnect', '--force', 'hosting-backend-shop', 'hosting-mail'] in calls and 'shop' not in mail.registry()
    assert (mail.GENERATED / 'maps').stat().st_ino == maps_inode and not (mail.GENERATED / 'maps/senders-shop').exists()  # same mounted directory, stale map gone
    line = mail.site_line({'id': 'c', 'name': 'forum', 'payload': json.dumps({'runtime': 'php'})})
    assert line['attached'] and line['server'] == 'mail' and line['port'] == 25 and line['spf'] == 'v=spf1 ip4:<server address> ~all' and line['domains'] == ['forum.example.com']
    assert mail.site_line({'id': 'x', 'name': 'pages', 'payload': json.dumps({'runtime': 'static'})}) is None
    # Without a relay, attach does nothing and says so.
    host.inspect = lambda name: None
    assert mail.attach(host, row, ['forum.example.com']) is False


def test_shim_is_mounted_into_new_and_existing_php_containers(world, tmp_path, monkeypatch):
    ops, host, calls, live = world
    from reeve import php_site
    monkeypatch.setattr(php_site, 'trusted', lambda *a, **k: None); monkeypatch.setattr(php_site, 'SITES', tmp_path / 'sites')
    monkeypatch.setattr(php_site, 'command', lambda args, timeout=120: calls.append(args) or (mail.SHIM.read_text() if args[:2] == ['docker', 'exec'] else ''))
    web = {"volumes": [], "networks": {"ingress": {}}, "user": "30000:30000", "restart": "unless-stopped", "cap_drop": ["ALL"], "security_opt": [], "pids_limit": -1, "storage_opt": {"size": "0"}, "logging": {"driver": "local"}, "labels": {}}
    compose = {"services": {"web": dict(web)}, "networks": {"ingress": {}}}
    row = {'id': 'a', 'name': 'shop', 'uid': 30042}
    php_site.compose_services(compose, tmp_path / 'sites/shop', {'php_version': '8.3'}, row, 'img', 'hosting-backend-shop')
    assert not any('hosting-sendmail' in v for v in compose['services']['php']['volumes'])  # no relay set up yet
    mail.install_shim()
    before = mail.SHIM.stat().st_ino
    monkeypatch.setattr(mail, 'TEMPLATES', tmp_path / 'tpl'); (tmp_path / 'tpl').mkdir(); (tmp_path / 'tpl/hosting-sendmail').write_text('#!/usr/bin/env php\nnew\n')
    mail.install_shim()
    assert mail.SHIM.stat().st_ino == before and mail.SHIM.read_text() == '#!/usr/bin/env php\nnew\n'  # same inode: running containers see it
    compose = {"services": {"web": dict(web)}, "networks": {"ingress": {}}}
    php_site.compose_services(compose, tmp_path / 'sites/shop', {'php_version': '8.3'}, row, 'img', 'hosting-backend-shop')
    assert f'{mail.SHIM}:/usr/local/bin/hosting-sendmail:ro' in compose['services']['php']['volumes']
    # An existing site's compose gains the mount and its PHP container is recreated once.
    site = tmp_path / 'sites/shop'; site.mkdir(parents=True)
    (site / 'compose.yml').write_text(yaml.safe_dump({'services': {'php': {'container_name': 'hosting-php-shop', 'volumes': ['a:/b'], 'pids_limit': -1}}}))
    calls.clear()
    assert php_site.add_shim_mount(host, row) is True and php_site.add_shim_mount(host, row) is False
    assert sum(1 for c in calls if c[:2] == ['docker', 'compose']) == 1
    # A container still holding an older copy is recreated once the shim changes.
    monkeypatch.setattr(php_site, 'command', lambda args, timeout=120: calls.append(args) or ('#!/usr/bin/env php\nold\n' if args[:2] == ['docker', 'exec'] else ''))
    assert php_site.add_shim_mount(host, row) is True
    assert sum(1 for c in calls if c[:2] == ['docker', 'compose']) == 2 and f'{mail.SHIM}:/usr/local/bin/hosting-sendmail:ro' in yaml.safe_load((site / 'compose.yml').read_text())['services']['php']['volumes']


def test_allowed_senders_extend_a_site_and_travel_with_its_backup(world, tmp_path, monkeypatch):
    ops, host, calls, live = world
    from reeve.content_jobs import validate_content
    assert validate_content('mail-senders', {'senders': 'Owner@example.net\n\n# note\nexample.org\n'}) == {'senders': ['owner@example.net', 'example.org']}
    assert mail.validate_senders({'senders': ['a@b.c']}) == {'senders': ['a@b.c']}
    for bad in ({'senders': 'not an address'}, {'senders': 'a@b'}, {'x': ''}, {'senders': '\n'.join(f'u{i}@example.org' for i in range(21))}):
        with pytest.raises(ValueError): mail.validate_senders(bad)
    files = mail.render(CONFIG, {'shop': {**ENTRIES['shop'], 'senders': ['owner@example.net', 'example.org']}})
    assert files['maps']['senders-shop'] == 'shop.example.com OK\nwww.shop.example.com OK\nowner@example.net OK\nexample.org OK\n'
    site = tmp_path / 'sites/shop'; site.mkdir(parents=True)
    row = {'id': 'a', 'name': 'shop', 'payload': json.dumps({'runtime': 'php'})}
    (site / 'hosting.yaml').write_text(yaml.safe_dump({'runtime': 'php', 'operation_id': 'a', 'domain': 'shop.example.com', 'aliases': []}))
    running = {'State': {'Running': True}, 'NetworkSettings': {'Networks': {mail.NETWORK: {}, 'hosting-backend-shop': {}}}}
    host.inspect = lambda name: running if name == mail.CONTAINER else None
    mail.save_registry({'shop': {'id': 'a', 'domains': ['shop.example.com'], 'senders': [], 'subnet': '10.240.35.0/24'}})
    ident = str(uuid.uuid4())
    assert mail.apply_senders(host, row, {'senders': 'owner@example.net'}, ident) == ['owner@example.net']
    assert yaml.safe_load((site / 'hosting.yaml').read_text())['mail_senders'] == ['owner@example.net']
    assert (mail.GENERATED / 'maps/senders-shop').read_text() == 'shop.example.com OK\nowner@example.net OK\n' and mail.extra_senders(row) == ['owner@example.net']
    assert json.loads((mail.ROOT / 'saved' / (ident + '.json')).read_text()) == {'site_id': 'a', 'senders': []}
    assert mail.site_line(row)['senders'] == ['owner@example.net']
    # A failed refresh puts the previous list back.
    monkeypatch.setattr(mail, 'refresh', lambda host, sites: (_ for _ in ()).throw(RuntimeError('reload failed')) if 'x@y.z' in sites['shop']['senders'] else None)
    with pytest.raises(RuntimeError, match='reload failed'): mail.apply_senders(host, row, {'senders': 'x@y.z'})
    assert yaml.safe_load((site / 'hosting.yaml').read_text())['mail_senders'] == ['owner@example.net']


def test_a_failing_startup_recovery_is_reported_and_never_stops_the_worker(monkeypatch):
    from reeve import worker, mail, sftp
    monkeypatch.setattr(mail, 'recover', lambda ledger, host: (_ for _ in ()).throw(RuntimeError('rollback needs review')))
    seen = []
    monkeypatch.setattr(sftp, 'recover', lambda ledger, host: seen.append('sftp ran'))
    monkeypatch.setattr(__import__('reeve.database_site', fromlist=['recover_usage']), 'recover_usage', lambda *a, **k: None)
    for module in ('database_backup', 'content_site', 'toolbox', 'requests_site', 'site_rules', 'php_settings', 'site_backup'):
        monkeypatch.setattr(__import__('reeve.' + module, fromlist=['recover']), 'recover', lambda *a, **k: None)
    logged = []
    assert worker.startup_recovery(None, None, log=logged.append) == ['mail']
    assert seen == ['sftp ran'] and logged == ['startup recovery of mail failed and is left for review: rollback needs review']


def test_persisted_log_is_read_oldest_first_and_pruned(world):
    ops, host, calls, live = world
    import os, time as t
    mail.LOG.mkdir(parents=True)
    (mail.LOG / 'mail.log-20260801').write_text('old\n'); os.utime(mail.LOG / 'mail.log-20260801', (t.time() - 40 * 86400,) * 2)
    (mail.LOG / 'mail.log-20260910').write_text('rotated\n'); (mail.LOG / 'mail.log').write_text('current\n')
    assert mail.read_log() == 'rotated\ncurrent\n' and not (mail.LOG / 'mail.log-20260801').exists()
    assert not any(c[-1] == 'logrotate' for c in calls)
    (mail.LOG / 'mail.log').write_bytes(b'x' * (mail.LOG_ROTATE_BYTES + 1)); calls.clear()
    mail.read_log()
    assert ['docker', 'exec', 'hosting-mail', 'postfix', 'logrotate'] in calls


def test_the_scheduled_backup_work_calls_every_step(monkeypatch):
    from reeve import worker, database_backup, backup_jobs, site_backup
    seen = []
    monkeypatch.setattr(database_backup, 'recover', lambda ledger, host: seen.append('recover'))
    monkeypatch.setattr(database_backup, 'retention_tick', lambda ledger: seen.append('retention'))
    monkeypatch.setattr(backup_jobs, 'tick', lambda ledger: seen.append('dumps'))
    monkeypatch.setattr(site_backup, 'tick', lambda ledger: seen.append('sites'))
    monkeypatch.setattr(site_backup, 'prune', lambda ledger: seen.append('prune'))
    worker.scheduled_backups(None, None)
    assert seen == ['recover', 'dumps', 'sites', 'prune', 'retention']


def test_mail_off_removes_the_relay_and_reports_off(world, monkeypatch):
    ops, host, calls, live = world
    (ops / 'server.yaml').write_text('mail:\n  mode: off\n')
    monkeypatch.setattr(mail, 'install_shim', lambda: None)
    live['value'] = {'State': {'Running': True}, 'NetworkSettings': {'Networks': {}}}
    assert mail.deploy(host, {'shop': {'id': 'x', 'domains': ['shop.example.com'], 'senders': []}}) == {'running': False, 'mode': 'off', 'sites': []}
    assert ['docker', 'rm', '--force', mail.CONTAINER] in calls and not any(c[:2] == ['docker', 'create'] for c in calls)
    calls.clear(); live['value'] = None
    assert mail.refresh(host, {})['mode'] == 'off' and calls == []
    status = mail.status(host)
    assert status['mode'] == 'off' and status['set_up'] is True and status['running'] is False and status['sites'] == []
    (ops / 'server.yaml').write_text('mail:\n  mode: sideways\n')
    with pytest.raises(ValueError): mail.settings()
