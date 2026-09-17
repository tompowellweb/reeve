import json
import os
import stat
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from reeve import sftp, host as hm
from reeve.content_jobs import validate_content

KEY1 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGxOaNNRJKLd9yPcoRvRsHMDaKdSyqRchiLoYW1hGY4v dev@laptop'
KEY2 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPNtwwSfqM0cU4pFoDD9SqQi0oUOsA6wXnB5gvSkwMfx other'
K1, K2 = KEY1.rsplit(' ', 1)[0], KEY2.rsplit(' ', 1)[0]  # what validation keeps: type and blob, no comment


def test_validation_takes_an_action_and_optional_secondary_keys():
    assert validate_content('sftp-access', {'action': 'on', 'secondary': KEY1 + '\r\n# comment\n\n' + KEY1 + '\n' + KEY2, 'duration': '7d'}) == {'action': 'on', 'secondary': [K1, K2], 'duration': '7d'}
    assert sftp.validate({'action': 'off'}) == {'action': 'off', 'secondary': [], 'duration': 'manual'} and sftp.validate({'action': 'rotate', 'secondary': [K1]})['duration'] == 'manual'  # always on unless a time is chosen
    with pytest.raises(ValueError, match='how long'): sftp.validate({'action': 'on', 'duration': '2y'})
    for bad in ({'action': 'maybe'}, {'secondary': KEY1}, {'action': 'on', 'secondary': 'ssh-ed25519 notbase64'}, {'action': 'on', 'secondary': 'command="x" ' + KEY1}, {'action': 'on', 'secondary': 5}):
        with pytest.raises(ValueError): sftp.validate(bad)
    many = '\n'.join(KEY1.rsplit(' ', 1)[0][:-1] + c for c in 'ABCDEFGHIJKLMNOPQRSTU')
    with pytest.raises(ValueError, match='At most 20'): sftp.validate({'action': 'on', 'secondary': many})
    assert validate_content('fix-ownership', {}) == {}
    with pytest.raises(ValueError): validate_content('fix-ownership', {'path': '.'})


def test_render_produces_one_confined_key_only_user_per_site():
    entries = {'a': {'name': 'shop', 'uid': 30042, 'keys': [K1]}, 'b': {'name': 'blog', 'uid': 30007, 'keys': [K1, K2]}}
    files = sftp.render(entries)
    config = json.loads(files['sftpgo.json'])
    assert config['sftpd']['bindings'] == [{'port': 2222, 'address': ''}] and config['sftpd']['host_keys'] == ['/run/sftp/host_key']
    assert config['sftpd']['password_authentication'] is False and config['sftpd']['enabled_ssh_commands'] == [] and config['common']['defender']['enabled']
    assert all(config[s]['bindings'][0]['port'] == 0 for s in ('ftpd', 'webdavd', 'httpd', 'telemetry'))
    assert config['data_provider'] == {'driver': 'memory', 'name': '/run/sftp/users.json', 'create_default_admin': False, 'users_base_dir': '/sites', 'backups_path': '/tmp/backups'}
    users = json.loads(files['users.json'])['users']
    assert [u['username'] for u in users] == ['blog', 'shop'] and users[1] == {'id': 2, 'username': 'shop', 'status': 1, 'home_dir': '/sites/shop', 'uid': 30042, 'gid': 30042, 'public_keys': [K1], 'permissions': {'/': ['*']}, 'filters': {'denied_protocols': ['FTP', 'DAV', 'HTTP']}}
    assert users[0]['public_keys'] == [K1, K2]


@pytest.fixture
def world(tmp_path, monkeypatch):
    sites = tmp_path / 'sites'; ops = tmp_path / 'ops'
    for module in (sftp, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(sftp, 'SITES', sites); monkeypatch.setattr(sftp, 'ROOT', ops / 'sftp'); monkeypatch.setattr(sftp, 'SAVED', ops / 'sftp/saved'); monkeypatch.setattr(sftp, 'GENERATED', ops / 'sftp/generated'); monkeypatch.setattr(sftp, 'PRIVATE', ops / 'sftp/private')
    monkeypatch.setattr('reeve.site_backup.EXPORTS', ops / 'downloads'); monkeypatch.setattr('reeve.site_backup.web_identity', lambda: (os.getuid(), os.getgid()))
    monkeypatch.setattr('reeve.content_site.OUTPUT', tmp_path / 'output')
    # ROOT itself does not exist yet, as on a fresh server; image() and host_key() are answered by the fakes below.
    monkeypatch.setattr(sftp, 'image', lambda step=None: (ops / 'sftp').mkdir(parents=True, exist_ok=True) or {'version': sftp.IMAGE_VERSION, 'image_id': 'sha256:sftpgo'})
    def fake_host_key():
        (ops / 'sftp').mkdir(parents=True, exist_ok=True)
        if not (ops / 'sftp/host_key').exists(): subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(ops / 'sftp/host_key')], check=True)
        return ops / 'sftp/host_key'
    monkeypatch.setattr(sftp, 'host_key', fake_host_key)
    ops.mkdir()  # the worker folder exists on a server; the sftp folder beneath it does not yet
    rows = {}
    for name, uid in (('shop', 30042), ('blog', 30007)):
        row = {'id': str(uuid.uuid4()), 'name': name, 'uid': uid, 'payload': json.dumps({'runtime': 'php' if name == 'shop' else 'static'})}
        (sites / name / 'html').mkdir(parents=True)
        (sites / name / 'hosting.yaml').write_text(yaml.safe_dump({'runtime': 'php' if name == 'shop' else 'static', 'operation_id': row['id']}))
        rows[name] = row
    calls = []
    def fake_command(args, timeout=120):
        calls.append(args)
        if args[:3] == ['docker', 'image', 'inspect']: return 'sha256:sftpgo'
        if args[0] == 'ssh-keygen': return subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True).stdout  # real keys and fingerprints
        return ''
    monkeypatch.setattr(sftp, 'command', fake_command)
    monkeypatch.setattr(sftp, 'wait_for_banner', lambda host: calls.append(['banner']))
    running = {'live': None}
    host = SimpleNamespace(inspect=lambda name: running['live'])
    return sites, rows, host, calls, running


def test_turning_on_makes_the_site_key_installs_secondary_keys_and_hands_out_the_private_half(world):
    sites, rows, host, calls, running = world
    ident = str(uuid.uuid4())
    before = time.time()
    result = sftp.apply(host, rows['shop'], {'action': 'on', 'secondary': KEY1, 'duration': '4h'}, ident)
    assert result['running'] and result['sites'] == ['shop'] and result['action'] == 'on' and result['secondary'] == 1
    assert before + 4 * 3600 - 5 <= result['expires_at'] <= time.time() + 4 * 3600
    entry = sftp.registry()[rows['shop']['id']]
    assert entry['generated'].startswith('ssh-ed25519 ') and entry['secondary'] == [K1] and entry['keys'] == [entry['generated'], K1] and entry['expires_at'] == result['expires_at']
    private = sftp.PRIVATE / rows['shop']['id']
    assert private.exists() and stat.S_IMODE(private.stat().st_mode) == 0o600 and 'PRIVATE KEY' in private.read_text()
    assert json.loads((sftp.GENERATED / 'users.json').read_text())['users'][0]['public_keys'] == [entry['generated'], K1] and (sftp.GENERATED / 'host_key').exists()
    assert yaml.safe_load((sites / 'shop/hosting.yaml').read_text())['sftp_secondary'] == [K1]  # kept with the site, not only the server
    create = ' '.join(next(c for c in calls if c[:2] == ['docker', 'create']))
    assert '--publish 2222:2222' in create and '--user 0:0' in create and '--cap-add SYS_CHROOT' not in create and '--cap-add CHOWN' in create and f'src={sites}/shop/html,dst=/sites/shop/html' in create and 'blog' not in create
    assert f'src={sftp.GENERATED},dst=/run/sftp,readonly' in create and create.endswith('sha256:sftpgo sftpgo serve') and not any(c[0] == 'docker' and c[1] == 'run' for c in calls)
    assert json.loads((sftp.SAVED / (ident + '.json')).read_text()) == {'site_id': rows['shop']['id'], 'entries': {}, 'secondary': []}
    view = sftp.public(rows['shop'])
    assert view['enabled'] and view['secondary'] == [K1] and view['key_fingerprint'].startswith('256 SHA256:') and view['user'] == 'shop' and view['expires_at'] == result['expires_at'] and view['has_key']
    export = sftp.export_key(rows['shop'])
    assert (sftp.PRIVATE.parent.parent / 'downloads' / export['token'] / 'shop-sftp-key').read_text() == private.read_text() and export['filename'] == 'shop-sftp-key'
    # Rotating makes a new key; the secondary stays; "until turned off" has no expiry.
    old_key = private.read_text(); running['live'] = {'State': {'Running': True}}
    rotated = sftp.apply(host, rows['shop'], {'action': 'rotate', 'secondary': [K1], 'duration': 'manual'})
    after = sftp.registry()[rows['shop']['id']]
    assert after['generated'] != entry['generated'] and after['secondary'] == [K1] and private.read_text() != old_key and rotated['expires_at'] is None and after['expires_at'] is None
    # Off keeps the site's key and its secondary keys for next time; the private key is still downloadable.
    calls.clear(); sftp.apply(host, rows['shop'], {'action': 'off'})
    assert rows['shop']['id'] not in sftp.registry() and private.exists() and sftp.public(rows['shop']) == {**sftp.public(rows['shop']), 'enabled': False, 'has_key': True, 'secondary': [K1]}
    assert sftp.export_key(rows['shop'])['filename'] == 'shop-sftp-key' and not any(c[:2] == ['docker', 'create'] for c in calls)
    again = sftp.apply(host, rows['shop'], {'action': 'on', 'secondary': [K1]})
    assert sftp.registry()[rows['shop']['id']]['generated'] == after['generated']  # the same key comes back on


def test_access_turns_itself_off_when_its_time_is_up(world):
    sites, rows, host, calls, running = world
    sftp.apply(host, rows['shop'], {'action': 'on', 'duration': '1h'}); running['live'] = {'State': {'Running': True}}
    sftp.apply(host, rows['blog'], {'action': 'on', 'duration': 'manual'})
    logged = []
    assert sftp.expire(host, now=time.time() + 1800, log=logged.append) == []  # not yet
    calls.clear()
    assert sftp.expire(host, now=time.time() + 3601, log=logged.append) == ['shop'] and logged[-1].endswith('shop')
    assert rows['shop']['id'] not in sftp.registry() and rows['blog']['id'] in sftp.registry() and (sftp.PRIVATE / rows['shop']['id']).exists()
    create = ' '.join(next(c for c in calls if c[:2] == ['docker', 'create']))
    assert '/sites/blog/html' in create and '/sites/shop/html' not in create
    sftp.apply(host, rows['blog'], {'action': 'off'}); calls.clear()
    assert sftp.expire(host, now=time.time() + 10 ** 6) == [] and not any(c[:2] == ['docker', 'create'] for c in calls)


def test_failed_regeneration_restores_the_previous_registry_and_delete_removes_an_entry(world, monkeypatch):
    sites, rows, host, calls, running = world
    sftp.apply(host, rows['shop'], {'action': 'on'})
    before = sftp.registry()
    attempts = []
    def banner(h):
        attempts.append(1)
        if len(attempts) == 1: raise ValueError('The SFTP server did not answer on its port')
    monkeypatch.setattr(sftp, 'wait_for_banner', banner)
    job = {'id': str(uuid.uuid4()), 'payload': json.dumps({'action': 'on', 'secondary': [K2]})}
    from reeve.content_site import ContentFailed
    with pytest.raises(ContentFailed, match='previous access stays'): sftp.perform(host, rows['blog'], job, lambda s: None)
    assert sftp.registry() == before and len(attempts) == 2
    assert 'did not answer' in (Path(sftp.SAVED).parent.parent.parent / 'output' / (job['id'] + '.txt')).read_text()
    # Delete drops the entry and regenerates without it.
    calls.clear(); sftp.remove(host, rows['shop'])
    assert sftp.registry() == {} and not (sftp.PRIVATE / rows['shop']['id']).exists() and not any(c[:2] == ['docker', 'create'] for c in calls)  # Delete removes the key
    # Rollback belongs to one site.
    ident = str(uuid.uuid4()); sftp.SAVED.mkdir(exist_ok=True)
    (sftp.SAVED / (ident + '.json')).write_text(json.dumps({'site_id': 'other', 'entries': {}}))
    with pytest.raises(ValueError, match='another site'): sftp.rollback(host, rows['shop'], ident)


def test_fix_ownership_gives_the_site_user_every_file_with_owner_access_and_nothing_more(world, monkeypatch):
    sites, rows, host, calls, running = world
    html = sites / 'shop/html'
    (html / 'wp-content/uploads').mkdir(parents=True)
    (html / 'index.php').write_text('x'); os.chmod(html / 'index.php', 0o444)
    (html / 'wp-content/uploads/a.jpg').write_bytes(b'j'); os.chmod(html / 'wp-content/uploads/a.jpg', 0o666)
    (html / 'wp-content/x.sh').write_text('#!/bin/sh'); os.chmod(html / 'wp-content/x.sh', 0o4755)
    os.chmod(html / 'wp-content', 0o770); os.chmod(html / 'wp-content/uploads', 0o755)
    os.symlink('index.php', html / 'link.php')
    chowned = []
    monkeypatch.setattr(sftp.os, 'chown', lambda path, uid, gid, follow_symlinks=True: chowned.append((str(path.relative_to(html) if path != html else '.'), uid, gid, follow_symlinks)))
    counts = sftp.fix_ownership(rows['shop'])
    assert counts == {'files': 3, 'directories': 3, 'links': 1, 'owner_changed': 7, 'mode_changed': 4}
    assert all(uid == 30042 and gid == 30042 and not follow for _, uid, gid, follow in chowned) and ('link.php', 30042, 30042, False) in chowned
    mode = lambda p: stat.S_IMODE((html / p).lstat().st_mode)
    assert mode('index.php') == 0o644 and mode('wp-content/uploads/a.jpg') == 0o644 and mode('wp-content/x.sh') == 0o755
    assert mode('wp-content') == 0o750 and mode('wp-content/uploads') == 0o755
    job = {'id': str(uuid.uuid4()), 'payload': '{}'}
    sftp.perform_fix(host, rows['shop'], job, lambda s: None)
    assert '3 files, 3 directories, 1 links seen' in (Path(sftp.SAVED).parent.parent.parent / 'output' / (job['id'] + '.txt')).read_text()
