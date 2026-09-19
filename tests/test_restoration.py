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
    assert step()['state'] == 'restoring'
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
    rs.request_scan('repository')
    monkeypatch.setattr('reeve.remote_backup.settings', lambda require_id=True: None)
    rs.perform_scan(ledger)
    assert rs.read_scan()['state'] == 'failed' and 'No backup destination' in rs.read_scan()['error']


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
    found, record, unread = rs.scan_repository({}, progress=lambda d, t: progress.append((d, t)))
    assert [e['id'] for e in found] == [str(uuid.UUID(int=2)), str(uuid.UUID(int=1))]  # newest first; the dump came from its tags
    assert found[0]['kind'] == 'dump' and found[0]['engine'] == 'mariadb' and found[0]['from_tags']
    assert sum(1 for c in calls if c[0] == 'dump') == 1 and unread == 0 and record is None  # one manifest read, for the untagged copy
