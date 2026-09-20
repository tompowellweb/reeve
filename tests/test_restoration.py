"""Restoration as a server feature: discovery, the picks, and the recovery queue's steps."""
import json
import uuid
from types import SimpleNamespace

import pytest

from reeve import restoration as rs
from reeve.core import Ledger


def ident(): return str(uuid.uuid4())


SITE_MANIFEST = {'schema': 1, 'kind': 'site-backup', 'site_kind': 'managed', 'operation': None, 'backup_kind': 'scheduled', 'site_id': 'S1', 'site_name': 'shop',
                 'domains': ['shop.example.com', 'www.shop.example.com'], 'completed_at': 2000.0, 'coverage': 'complete', 'files': {'bytes': 1000},
                 'dumps': {'database': {'bytes': 50, 'engine': 'mariadb'}}, 'managed': {'runtime': 'php'}}
DUMP_MANIFEST = {'schema': 1, 'kind': 'local-database-dump', 'operation': None, 'site_id': 'S1', 'site_name': 'shop', 'engine': 'mariadb', 'completed_at': 2500.0, 'bytes': 60, 'consistency': 'x'}


def test_entries_come_from_manifests_or_from_snapshot_tags():
    entry = rs.entry_from_manifest({**SITE_MANIFEST, 'operation': 'B1'}, 'repository', 'abc')
    assert entry['kind'] == 'site' and entry['id'] == 'B1' and entry['bytes'] == 1050 and entry['has_dump'] and entry['engine'] == 'mariadb' and entry['runtime'] == 'php'
    dump = rs.entry_from_manifest({**DUMP_MANIFEST, 'operation': 'D1'}, 'local')
    assert dump['kind'] == 'dump' and dump['engine'] == 'mariadb' and dump['completed_at'] == 2500.0
    assert rs.entry_from_manifest({'kind': 'other'}, 'local') is None
    tagged = rs.entry_from_tags({'id': 'snap', 'time': '2026-09-19T03:00:05.123456Z', 'tags': ['hosting-site:B2', 'site-name:shop', 'site-kind:managed', 'backup-kind:scheduled', 'domain:shop.example.com']})
    assert tagged['id'] == 'B2' and tagged['site_name'] == 'shop' and tagged['domains'] == ['shop.example.com'] and tagged['from_tags'] and tagged['completed_at'] == 1789786805.0
    assert rs.entry_from_tags({'id': 'x', 'tags': ['hosting-site:B3']}) is None  # an older copy without descriptive tags needs its manifest


def test_grouping_puts_backups_newest_first_and_finds_the_live_site(tmp_path):
    ledger = Ledger(tmp_path / 'jobs.db', tmp_path / 'sites')
    live = ledger.submit(ident(), {'name': 'shop', 'domain': 'shop.example.com', 'runtime': 'php', 'php_version': '8.4'}); ledger.update(live['id'], 'succeeded', 'published')
    entries = [rs.entry_from_manifest({**SITE_MANIFEST, 'operation': 'B1', 'completed_at': 1000.0}, 'local'),
               rs.entry_from_manifest({**SITE_MANIFEST, 'operation': 'B2'}, 'repository', 'snap2'),
               rs.entry_from_manifest({**DUMP_MANIFEST, 'operation': 'D1'}, 'repository', 'snap3'),
               rs.entry_from_manifest({**SITE_MANIFEST, 'operation': 'B9', 'site_name': 'blog', 'site_id': 'S2', 'domains': ['blog.example']}, 'local')]
    sites = rs.group(entries, ledger)
    assert [s['name'] for s in sites] == ['blog', 'shop']
    shop = sites[1]
    assert [b['id'] for b in shop['backups']] == ['B2', 'B1'] and [d['id'] for d in shop['dumps']] == ['D1']
    assert shop['domains'] == ['shop.example.com', 'www.shop.example.com'] and shop['live']['name'] == 'shop' and shop['live']['managed'] and sites[0]['live'] is None


def test_folder_and_local_scans_read_artifacts_by_their_manifests(tmp_path, monkeypatch):
    folder = tmp_path / 'old'; (folder / 'B1').mkdir(parents=True); (folder / 'junk').mkdir()
    (folder / 'B1/manifest.json').write_text(json.dumps({**SITE_MANIFEST, 'operation': 'B1'}))
    (folder / 'server-record.json').write_text(json.dumps({'kind': 'server-record', 'schema': 1, 'sites': [], 'settings': {'profile': 'small'}, 'hostname': 'old'}))
    found, record = rs.scan_folder(str(folder))
    assert [e['id'] for e in found] == ['B1'] and found[0]['source'] == 'folder:' + str(folder / 'B1') and record['hostname'] == 'old'
    with pytest.raises(ValueError, match='does not exist'): rs.scan_folder(str(tmp_path / 'nowhere'))
    staging = tmp_path / 'staging/site'; b = ident(); (staging / b).mkdir(parents=True); (staging / b / 'manifest.json').write_text(json.dumps({**SITE_MANIFEST, 'operation': b}))
    d = ident(); (tmp_path / 'staging/db' / d).mkdir(parents=True); (tmp_path / 'staging/db' / d / 'manifest.json').write_text(json.dumps({**DUMP_MANIFEST, 'operation': d}))
    (staging / 'wrong').mkdir(); (staging / 'wrong/manifest.json').write_text(json.dumps({**SITE_MANIFEST, 'operation': 'other'}))  # folder name and operation differ: skipped
    monkeypatch.setattr(rs.sites, 'STAGING', staging); monkeypatch.setattr(rs.dumps, 'STAGING', tmp_path / 'staging/db')
    found, _ = rs.scan_local()
    assert sorted((e['kind'], e['id']) for e in found) == sorted([('site', b), ('dump', d)])


@pytest.fixture
def world(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / 'jobs.db', tmp_path / 'sites')
    monkeypatch.setattr(rs, 'SCAN', tmp_path / 'scan.json'); monkeypatch.setattr(rs, 'REQUEST', tmp_path / 'request.json'); monkeypatch.setattr(rs, 'MANIFESTS', tmp_path / 'manifests')
    live = ledger.submit(ident(), {'name': 'shop', 'domain': 'shop.example.com', 'runtime': 'php', 'php_version': '8.4'}); ledger.update(live['id'], 'succeeded', 'published')
    scan = {'state': 'succeeded', 'source': 'repository', 'sites': [
        {'name': 'shop', 'backups': [rs.entry_from_manifest({**SITE_MANIFEST, 'operation': 'a' * 8 + '-' + 'b' * 4 + '-' + 'c' * 4 + '-' + 'd' * 4 + '-' + 'e' * 12}, 'repository', 'snap1')],
         'dumps': [rs.entry_from_manifest({**DUMP_MANIFEST, 'operation': str(uuid.UUID(int=7))}, 'repository', 'snap2')], 'live': {'name': 'shop'}},
        {'name': 'blog', 'backups': [rs.entry_from_manifest({**SITE_MANIFEST, 'operation': str(uuid.UUID(int=9)), 'site_name': 'blog', 'domains': ['blog.example'], 'dumps': {}}, 'local')], 'dumps': [], 'live': None}]}
    rs.write_scan(scan)
    return ledger, live, scan


def test_picks_are_checked_before_anything_is_queued(world):
    ledger, live, scan = world
    shop_backup = scan['sites'][0]['backups'][0]['id']; blog_backup = scan['sites'][1]['backups'][0]['id']; dump = scan['sites'][0]['dumps'][0]['id']
    with pytest.raises(ValueError, match='at least one'): rs.submit(ledger, [])
    with pytest.raises(ValueError, match='not in the last scan'): rs.submit(ledger, [{'backup': ident(), 'mode': 'new', 'name': 'x', 'domains': 'x.example'}])
    with pytest.raises(ValueError, match='already exists'): rs.submit(ledger, [{'backup': blog_backup, 'mode': 'new', 'name': 'shop', 'domains': 'blog.example'}])
    with pytest.raises(ValueError, match='at least one hostname'): rs.submit(ledger, [{'backup': blog_backup, 'mode': 'new', 'name': 'blog', 'domains': ''}])
    with pytest.raises(ValueError, match='Choose a live site'): rs.submit(ledger, [{'backup': shop_backup, 'mode': 'files', 'target': 'nope'}])
    with pytest.raises(ValueError, match='no database dump'): rs.submit(ledger, [{'backup': blog_backup, 'mode': 'database', 'target': 'shop'}])
    assert rs.recoveries(ledger) == []
    queued = rs.submit(ledger, [{'backup': blog_backup, 'mode': 'new', 'name': 'blog', 'domains': 'blog.example, www.blog.example'},
                                {'backup': shop_backup, 'mode': 'both', 'target': 'shop'}, {'backup': dump, 'mode': 'dump', 'target': 'shop'}])
    rows = rs.recoveries(ledger, active=True)
    assert [r['id'] for r in rows] == queued and [r['mode'] for r in rows] == ['new', 'both', 'dump']
    assert json.loads(rows[0]['domains']) == ['blog.example', 'www.blog.example'] and rows[1]['target'] == live['id'] and rows[2]['backup_id'] == dump


def test_a_recovery_steps_through_fetch_restore_wait_and_hostnames(world, monkeypatch):
    ledger, live, scan = world
    blog_backup = scan['sites'][1]['backups'][0]['id']
    [rid] = rs.submit(ledger, [{'backup': blog_backup, 'mode': 'new', 'name': 'blog', 'domains': 'blog.example www.blog.example'}])
    monkeypatch.setattr(rs, 'fetch', lambda row: 'already here')
    created = {}
    def fake_restore(ledger_, host, backup, name, domain):
        row = ledger_.submit(ident(), {'name': name, 'domain': domain}); created['id'] = row['id']
        with ledger_.db() as db: db.execute("INSERT INTO site_restores (id, site_id, snapshot, state, step, error, created, updated, scope) VALUES (?,?,?,'queued','w','',1,1,'full')", (ident(), row['id'], backup))
        return row
    monkeypatch.setattr(rs.sites, 'restore', fake_restore)
    host = SimpleNamespace()
    def step(): rs.perform(ledger, host, rs.recoveries(ledger, active=True)[0]); return rs.recoveries(ledger)[0]
    assert step()['state'] == 'fetched'
    assert step()['state'] == 'waiting' and rs.recoveries(ledger)[0]['child'] == created['id']
    assert step()['state'] == 'waiting'  # the site is still being created
    ledger.update(created['id'], 'succeeded', 'published')
    assert step()['state'] == 'waiting'  # the restore phase has not run
    with ledger.db() as db: db.execute("UPDATE site_restores SET state='succeeded' WHERE site_id=?", (created['id'],))
    row = step(); assert row['state'] == 'hostnames' and row['phase'] == 'domains'
    job = next(j for j in ledger.domain_jobs() if j['id'] == row['child']); assert json.loads(job['payload']) == ['blog.example', 'www.blog.example']
    assert step()['state'] == 'hostnames'
    ledger.finish_domains(job)
    row = step(); assert row['state'] == 'succeeded' and 'with its hostnames' in row['step']
    # A failure is recorded with its reason and can be retried from the start.
    [rid2] = rs.submit(ledger, [{'backup': scan['sites'][0]['backups'][0]['id'], 'mode': 'files', 'target': 'shop'}])
    monkeypatch.setattr(rs, 'fetch', lambda row: (_ for _ in ()).throw(ValueError('the snapshot is gone')))
    row = step(); assert row['state'] == 'failed' and 'snapshot is gone' in row['error']
    assert rs.retry(ledger, rid2)['id'] == rid2 and rs.recoveries(ledger, active=True)[0]['state'] == 'queued'
    with ledger.db() as db: db.execute("UPDATE recoveries SET state='fetching' WHERE id=?", (rid2,))
    rs.recover(ledger); assert rs.recoveries(ledger)[0]['state'] == 'recovery-needed'


def test_scan_requests_are_recorded_and_performed(world, monkeypatch):
    ledger, live, scan = world
    with pytest.raises(ValueError, match='absolute folder'): rs.request_scan('folder', 'relative')
    with pytest.raises(ValueError): rs.request_scan('folder', '/x/../y')
    assert rs.request_scan('local')['state'] == 'requested'
    monkeypatch.setattr(rs, 'scan_local', lambda: ([rs.entry_from_manifest({**SITE_MANIFEST, 'operation': str(uuid.UUID(int=3))}, 'local')], None))
    assert rs.perform_scan(ledger) is True and rs.perform_scan(ledger) is False
    result = rs.read_scan()
    assert result['state'] == 'succeeded' and result['sites'][0]['name'] == 'shop' and result['sites'][0]['live']['name'] == 'shop' and result['record'] is None
    monkeypatch.setattr('reeve.remote_backup.settings', lambda require_id=True: None)
    with pytest.raises(ValueError, match='No backup destination'): rs.request_scan('repository')
    monkeypatch.setattr('reeve.remote_backup.destination', lambda ident, **k: {'id': ident, 'name': 'Gone', 'type': 'sftp'})
    rs.request_scan('repository:' + str(uuid.UUID(int=4)))
    monkeypatch.setattr('reeve.remote_backup.destination', lambda ident, **k: (_ for _ in ()).throw(ValueError('Unknown destination')))
    rs.perform_scan(ledger)
    assert rs.read_scan()['state'] == 'failed' and 'Unknown destination' in rs.read_scan()['error']


def test_the_pages_source_choice_names_a_backup_or_a_dump(world):
    ledger, live, scan = world
    shop_backup = scan['sites'][0]['backups'][0]['id']; dump = scan['sites'][0]['dumps'][0]['id']
    with pytest.raises(ValueError, match='only be restored into a live site'): rs.submit(ledger, [{'backup': 'dump:' + dump, 'mode': 'new', 'name': 'x', 'domains': 'x.example'}])
    queued = rs.submit(ledger, [{'backup': 'dump:' + dump, 'mode': 'both', 'target': 'shop'}, {'backup': 'site:' + shop_backup, 'mode': 'files', 'target': 'shop'}])
    rows = {r['id']: r for r in rs.recoveries(ledger)}
    assert rows[queued[0]]['mode'] == 'dump' and rows[queued[0]]['backup_id'] == dump  # a dump means the database, whatever the page's other choice
    assert rows[queued[1]]['mode'] == 'files' and rows[queued[1]]['backup_id'] == shop_backup


def test_tagged_dump_snapshots_need_no_manifest_and_newest_are_read_first(monkeypatch, tmp_path):
    monkeypatch.setattr(rs, 'MANIFESTS', tmp_path / 'manifests')
    listing = [
        {'id': 'old', 'time': '2026-09-18T03:00:00Z', 'tags': ['hosting-site:' + str(uuid.UUID(int=1))], 'paths': ['/srv/backups/staging/site/' + str(uuid.UUID(int=1))]},
        {'id': 'new', 'time': '2026-09-19T03:00:00Z', 'tags': ['hosting-db:' + str(uuid.UUID(int=2)), 'site-name:shop', 'engine:mariadb'], 'paths': ['/x/manifest.json', '/x/database.sql']},
    ]
    calls = []
    def execute(config, args, maximum=0):
        calls.append(args)
        if args[0] == 'snapshots' and '--tag' in args: return '[]'
        if args[0] == 'snapshots': return json.dumps(listing)
        return json.dumps({**SITE_MANIFEST, 'operation': str(uuid.UUID(int=1))})
    monkeypatch.setattr('reeve.remote_backup.execute', execute)
    progress = []
    found, record, unread = rs.scan_repository({'id': 'dest-id', 'name': 'NAS'}, progress=lambda d, t: progress.append((d, t)))
    assert [e['id'] for e in found] == [str(uuid.UUID(int=2)), str(uuid.UUID(int=1))]  # newest first; the dump came from its tags
    assert found[0]['kind'] == 'dump' and found[0]['engine'] == 'mariadb' and found[0]['from_tags']
    assert sum(1 for c in calls if c[0] == 'dump') == 1 and unread == 0 and record is None  # one manifest read, for the untagged copy


def _artifact(module, ident, manifest, filename, body=b'x'):
    root = module.artifact_path(ident); root.mkdir(parents=True)
    (root / filename).write_bytes(body); (root / 'manifest.json').write_text(json.dumps({**manifest, 'operation': ident}))
    return root


def test_manage_picks_are_checked_and_deletions_remove_one_copy_only(world, monkeypatch, tmp_path):
    ledger, live, scan = world
    monkeypatch.setattr(rs.sites, 'STAGING', tmp_path / 'staging/site'); monkeypatch.setattr(rs.dumps, 'STAGING', tmp_path / 'staging/db')
    monkeypatch.setattr(rs, 'trusted', lambda *a, **k: None)
    shop_backup = scan['sites'][0]['backups'][0]['id']; blog_backup = scan['sites'][1]['backups'][0]['id']; shop_dump = scan['sites'][0]['dumps'][0]['id']
    with pytest.raises(ValueError, match='Unknown backup action'): rs.submit_actions(ledger, [{'action': 'shred', 'backup': 'site:' + blog_backup}])
    with pytest.raises(ValueError, match='not in the last scan'): rs.submit_actions(ledger, [{'action': 'delete', 'backup': 'site:' + str(uuid.UUID(int=99))}])
    # A backup a recovery is using stays until the recovery finishes.
    [rid] = rs.submit(ledger, [{'backup': blog_backup, 'mode': 'new', 'name': 'blog', 'domains': 'blog.example'}])
    with pytest.raises(ValueError, match='recovery or another action'): rs.submit_actions(ledger, [{'action': 'delete', 'backup': 'site:' + blog_backup}])
    rs.update(ledger, rid, 'succeeded', 'done')
    # Local: the artifact folder goes, the ledger row says the operator removed it, the scan no longer lists it.
    with ledger.db() as db:
        db.execute("INSERT INTO site_backups (id, site_id, kind, state, step, error, manifest, created, updated) VALUES (?,?,?,?,?,?,?,1,1)",
                   (blog_backup, live['id'], 'final', 'succeeded', 'done', '', json.dumps({'completed_at': 5})))
    root = _artifact(rs.sites, blog_backup, SITE_MANIFEST, 'files.tar')
    [aid] = rs.submit_actions(ledger, [{'action': 'delete', 'backup': 'site:' + blog_backup}])
    assert rs.actions(ledger, active=True)[0]['id'] == aid
    rs.perform_action(ledger, rs.actions(ledger, active=True)[0])
    done = rs.actions(ledger)[0]
    assert done['state'] == 'succeeded' and 'removed from this server' in done['step'] and not root.exists()
    assert ledger.site_backups(live['id'], limit=None)[0]['state'] == 'pruned'
    assert rs.find_entry(rs.read_scan(), 'site', blog_backup) is None and [s['name'] for s in rs.read_scan()['sites']] == ['shop']
    # Repository: forget the snapshot, prune, record it; the local copy is not touched.
    calls = []
    nas = {'id': str(uuid.UUID(int=8)), 'name': 'NAS', 'type': 'sftp', 'destination': 'sftp:x', 'repository': 'r', 'password_file': 'p', 'timeout_seconds': 5, 'enabled': True}
    monkeypatch.setattr('reeve.remote_backup.destinations', lambda **k: [nas]); monkeypatch.setattr('reeve.remote_backup.destination', lambda ident, **k: nas)
    current = rs.read_scan()
    for site in current['sites']:
        for e in site['backups'] + site['dumps']:
            if e['source'] == 'repository': e['source'] = 'repository:' + nas['id']
    rs.write_scan(current)
    monkeypatch.setattr('reeve.remote_backup.execute', lambda config, args, **k: calls.append(args))
    with ledger.db() as db: db.execute("INSERT INTO remote_copies (destination, job_id, snapshot, verified) VALUES ('sftp:x', ?, 'snap1', 9)", (shop_backup,))
    local_copy = _artifact(rs.sites, shop_backup, SITE_MANIFEST, 'files.tar')
    [aid] = rs.submit_actions(ledger, [{'action': 'delete', 'backup': 'site:' + shop_backup}])
    rs.perform_action(ledger, rs.actions(ledger, active=True)[0])
    assert calls == [['forget', 'snap1'], ['prune']] and local_copy.exists() and rs.actions(ledger)[0]['state'] == 'succeeded'
    with ledger.db() as db: assert db.execute('SELECT forgotten FROM remote_copies WHERE job_id=?', (shop_backup,)).fetchone()[0] > 0
    # A dump in the repository is deleted the same way; a failure is recorded with its reason.
    monkeypatch.setattr('reeve.remote_backup.execute', lambda config, args, **k: (_ for _ in ()).throw(RuntimeError('repository is already locked')))
    [aid] = rs.submit_actions(ledger, [{'action': 'delete', 'backup': 'dump:' + shop_dump}])
    rs.perform_action(ledger, rs.actions(ledger, active=True)[0])
    assert rs.actions(ledger)[0]['state'] == 'failed' and 'already locked' in rs.actions(ledger)[0]['error']
    assert rs.find_entry(rs.read_scan(), 'dump', shop_dump) is not None


def test_manage_downloads_fetch_then_export_and_folder_copies_can_go(world, monkeypatch, tmp_path):
    ledger, live, scan = world
    monkeypatch.setattr(rs.sites, 'STAGING', tmp_path / 'staging/site'); monkeypatch.setattr(rs.dumps, 'STAGING', tmp_path / 'staging/db')
    monkeypatch.setattr(rs.sites, 'EXPORTS', tmp_path / 'downloads')
    import pwd, os as _os
    me = pwd.getpwuid(_os.getuid()); monkeypatch.setattr(rs.sites, 'web_identity', lambda: (me.pw_uid, me.pw_gid))
    shop_backup = scan['sites'][0]['backups'][0]['id']; shop_dump = scan['sites'][0]['dumps'][0]['id']
    fetched = []
    monkeypatch.setattr(rs, 'fetch', lambda row: fetched.append(row['backup_id']) or 'fetched from the repository')
    monkeypatch.setattr(rs.sites, 'export', lambda ident: {'token': 'tok', 'files': [{'name': 'snapshot.tar', 'bytes': 3}], 'expires_at': 9})
    [aid] = rs.submit_actions(ledger, [{'action': 'download', 'backup': 'site:' + shop_backup}])
    rs.perform_action(ledger, rs.actions(ledger, active=True)[0])
    done = rs.actions(ledger)[0]
    assert fetched == [shop_backup] and done['state'] == 'succeeded' and done['result']['token'] == 'tok' and 'six hours' in done['step']
    # A dump download copies its file and manifest into the web folder.
    _artifact(rs.dumps, shop_dump, {**DUMP_MANIFEST, 'file': 'database.sql'}, 'database.sql', b'CREATE TABLE t (x int);')
    [aid] = rs.submit_actions(ledger, [{'action': 'download', 'backup': 'dump:' + shop_dump}])
    rs.perform_action(ledger, rs.actions(ledger, active=True)[0])
    done = rs.actions(ledger)[0]
    assert done['state'] == 'succeeded' and {f['name'] for f in done['result']['files']} == {'database.sql', 'manifest.json'}
    assert (rs.sites.EXPORTS / done['result']['token'] / 'database.sql').read_bytes() == b'CREATE TABLE t (x int);'
    # A folder scan: an artifact subfolder can be deleted; the scanned folder itself cannot.
    folder = tmp_path / 'old'; sub = folder / 'one'; sub.mkdir(parents=True)
    (sub / 'manifest.json').write_text('{}'); (sub / 'files.tar').write_bytes(b'x')
    other = str(uuid.UUID(int=21))
    rs.write_scan({'state': 'succeeded', 'source': 'folder', 'folder': str(folder), 'sites': [
        {'name': 'old', 'backups': [rs.entry_from_manifest({**SITE_MANIFEST, 'operation': other, 'site_name': 'old'}, 'folder:' + str(sub)),
                                     rs.entry_from_manifest({**SITE_MANIFEST, 'operation': str(uuid.UUID(int=22)), 'site_name': 'old'}, 'folder:' + str(folder))], 'dumps': [], 'live': None}]})
    with pytest.raises(ValueError, match='remove the folder by hand'): rs.submit_actions(ledger, [{'action': 'delete', 'backup': 'site:' + str(uuid.UUID(int=22))}])
    [aid] = rs.submit_actions(ledger, [{'action': 'delete', 'backup': 'site:' + other}])
    rs.perform_action(ledger, rs.actions(ledger, active=True)[0])
    assert rs.actions(ledger)[0]['state'] == 'succeeded' and not sub.exists() and folder.exists()
    # The worker stopping mid-action fails it with a reason; the page's status carries the list.
    [aid] = rs.submit_actions(ledger, [{'action': 'download', 'backup': 'site:' + str(uuid.UUID(int=22))}])
    with ledger.db() as db: db.execute("UPDATE backup_actions SET state='running' WHERE id=?", (aid,))
    rs.recover(ledger)
    assert rs.status(ledger)['actions'][0]['state'] == 'failed' and 'queue it again' in rs.status(ledger)['actions'][0]['error']


def test_a_compose_site_finishes_its_recovery_and_does_not_hold_up_the_queue(world, monkeypatch):
    """Restoring a Compose application redeploys its captured package: files, volumes and dumps come
    back as part of that, so there is no separate restore job. Waiting for one stalled the recovery in
    `waiting` for ever, and the queue runs one at a time, so every site behind it never started."""
    ledger, live, scan = world
    blog_backup = scan['sites'][1]['backups'][0]['id']; shop_backup = scan['sites'][0]['backups'][0]['id']
    first, second = rs.submit(ledger, [{'backup': blog_backup, 'mode': 'new', 'name': 'blog', 'domains': 'blog.example'},
                                       {'backup': shop_backup, 'mode': 'new', 'name': 'later', 'domains': 'later.example'}])
    monkeypatch.setattr(rs, 'fetch', lambda row: 'already here')
    created = {}
    def fake_restore(ledger_, host, backup, name, domain):
        row = ledger_.submit(ident(), {'name': name, 'domain': domain}); created[name] = row['id']
        with ledger_.db() as db:   # a package: the deployment writes no site_restores row
            db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps({'name': name, 'domain': domain, 'runtime': 'compose'}), row['id']))
        return ledger_.get(row['id'])
    monkeypatch.setattr(rs.sites, 'restore', fake_restore)
    host = SimpleNamespace()
    def step(): rs.perform(ledger, host, rs.recoveries(ledger, active=True)[0]); return {r['id']: r for r in rs.recoveries(ledger)}
    step(); step()
    assert step()[first]['state'] == 'waiting'          # the deployment is still running
    ledger.update(created['blog'], 'succeeded', 'published')
    assert step()[first]['state'] == 'succeeded'        # and now it finishes, with no restore job in sight
    # The next site in the queue is reached rather than stranded behind it.
    assert rs.recoveries(ledger, active=True)[0]['id'] == second
    step(); assert {r['id']: r for r in rs.recoveries(ledger)}[second]['state'] in ('fetched', 'waiting')


def test_downloads_run_beside_the_restores_and_keep_the_operators_order(world, monkeypatch):
    """The repository link is the bottleneck, so downloads stay serial; but they no longer wait for the
    restore in front of them, which used to leave the link idle for the whole length of every restore."""
    ledger, live, scan = world
    shop_backup = scan['sites'][0]['backups'][0]['id']; blog_backup = scan['sites'][1]['backups'][0]['id']
    first, second = rs.submit(ledger, [{'backup': blog_backup, 'mode': 'new', 'name': 'blog', 'domains': 'blog.example'},
                                       {'backup': shop_backup, 'mode': 'files', 'target': 'shop'}])
    fetched = []
    monkeypatch.setattr(rs, 'fetch', lambda row: fetched.append(row['id']) or 'fetched from the repository')
    monkeypatch.setattr(rs, 'perform_scan', lambda ledger_: False)
    monkeypatch.setattr(rs.sites, 'restore', lambda *a: (_ for _ in ()).throw(AssertionError('not reached')))
    lanes = {}; host = SimpleNamespace()
    def tick(): rs.tick(ledger, host, lanes, spawn=lambda work: work())
    # `perform` is stubbed to do nothing, so the first recovery never finishes restoring.
    inflight = []
    monkeypatch.setattr(rs, 'perform', lambda ledger_, host_, row: inflight.append(row['id']))
    tick(); assert fetched == [first] and inflight == [first]      # the operator's first pick downloads first
    tick(); assert fetched == [first, second]                      # and the second follows without waiting for that restore
    tick(); assert fetched == [first, second]                      # one download at a time: there is no third
    assert [r['state'] for r in rs.recoveries(ledger, active=True)] == ['fetched', 'fetched']
    assert inflight == [first, first, first]                       # while the restores stay serial and in order
    assert 'waiting its turn' in {r['id']: r for r in rs.recoveries(ledger)}[second]['step']


def test_a_download_failure_belongs_to_its_own_recovery(world, monkeypatch):
    ledger, live, scan = world
    [rid] = rs.submit(ledger, [{'backup': scan['sites'][1]['backups'][0]['id'], 'mode': 'new', 'name': 'blog', 'domains': 'blog.example'}])
    monkeypatch.setattr(rs, 'fetch', lambda row: (_ for _ in ()).throw(ValueError('the snapshot is gone')))
    assert rs.download_lane(ledger, rs.recoveries(ledger, active=True), {}, spawn=lambda work: work()) == rid
    row = rs.recoveries(ledger)[0]
    assert row['state'] == 'failed' and 'snapshot is gone' in row['error']


def test_the_runtimes_the_queue_needs_are_built_beside_it(world, monkeypatch):
    """A restore used to build its PHP runtime inline, one branch at a time in the middle of the queue: a
    minute a branch on the critical path. The branches are named in the manifests the scan already read, so
    they are built beside the downloads instead. They are the same images, so no more disk is spent."""
    ledger, live, scan = world
    blog_backup = scan['sites'][1]['backups'][0]['id']; shop_backup = scan['sites'][0]['backups'][0]['id']
    rs.remember_manifest(blog_backup, {**SITE_MANIFEST, 'operation': blog_backup,
                                       'managed': {'runtime': 'php', 'php_branch': '7.0', 'payload': {'runtime': 'php', 'php_version': '8.4'}}})
    rs.remember_manifest(shop_backup, {**SITE_MANIFEST, 'operation': shop_backup,
                                       'managed': {'runtime': 'php', 'payload': {'runtime': 'php', 'php_version': '8.3'}}})
    rs.submit(ledger, [{'backup': blog_backup, 'mode': 'new', 'name': 'blog', 'domains': 'blog.example'},
                       {'backup': shop_backup, 'mode': 'new', 'name': 'later', 'domains': 'later.example'}])
    pending = rs.recoveries(ledger, active=True)
    # The pinned branch wins over the payload's version, exactly as the restore itself chooses it.
    assert rs.runtime_branches(pending) == ['7.0', '8.3']
    built, asked = {'8.3': {}}, []
    import reeve.php_runtime as php_runtime
    monkeypatch.setattr(php_runtime, 'catalog', lambda: built)
    monkeypatch.setattr(php_runtime, 'build', lambda branches: asked.extend(branches))
    # Only the branch that is missing, and a branch already in the catalogue is left alone.
    assert rs.prebuild_runtimes(pending, {}, spawn=lambda work: work()) == ['7.0'] and asked == ['7.0']
    # A branch already in flight is not started twice, and no more than BUILDING run at once.
    alive = SimpleNamespace(is_alive=lambda: True)
    assert rs.prebuild_runtimes(pending, {'runtimes': {'7.0': alive}}, spawn=lambda work: work()) == []
    assert rs.prebuild_runtimes(pending, {'runtimes': {b: alive for b in ('a', 'b', 'c')}}, spawn=lambda work: work()) == []
    # A backup the scan described by its tags alone has no manifest: that site builds its runtime the old way.
    rs.manifest_cache(blog_backup).unlink(); rs.manifest_cache(shop_backup).unlink()
    assert rs.runtime_branches(rs.recoveries(ledger, active=True)) == []
    # Preparing a runtime ahead of time never fails a recovery: the restore's own ensure() reports it instead.
    monkeypatch.setattr(php_runtime, 'build', lambda branches: (_ for _ in ()).throw(RuntimeError('Surý is unreachable')))
    rs.remember_manifest(blog_backup, {**SITE_MANIFEST, 'operation': blog_backup, 'managed': {'runtime': 'php', 'payload': {'runtime': 'php', 'php_version': '7.0'}}})
    assert rs.prebuild_runtimes(rs.recoveries(ledger, active=True), {}, spawn=lambda work: work()) == ['7.0']
    assert [r['state'] for r in rs.recoveries(ledger, active=True)] == ['queued', 'queued']
