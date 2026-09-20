import datetime
import json
import time
import uuid

import pytest

from reeve import retention, remote_backup as remote, database_backup as local


def at(day, hour=3):
    return datetime.datetime(2026, 1, 1, hour).timestamp() + day * 86400


def test_site_keep_set_counts_days_weeks_and_months_and_protects_final_imported_and_kept():
    now = at(400)
    entries = [{'id': 'd%d' % d, 'kind': 'scheduled', 'completed_at': at(d)} for d in range(0, 400)]
    entries += [{'id': 'final', 'kind': 'final', 'completed_at': at(10)}, {'id': 'imported', 'kind': 'imported', 'completed_at': at(20)},
                {'id': 'chosen', 'kind': 'scheduled', 'kept': True, 'completed_at': at(30)},
                {'id': 'safety', 'kind': 'pre-restore', 'completed_at': at(399) + 60}]
    keep = retention.keep_site_backups(entries, now, {'daily': 7, 'weekly': 4, 'monthly': 12})
    assert {'final', 'imported', 'chosen'} <= keep
    days = sorted(int(i[1:]) for i in keep if i.startswith('d'))
    assert 'safety' in keep and 'd399' not in keep and days[-6:] == list(range(393, 399))   # the newest of each of the last 7 days; day 399's newest is the safety copy
    months = {datetime.date.fromtimestamp(at(d)).strftime('%Y-%m') for d in days}; weeks = {datetime.date.fromtimestamp(at(d)).isocalendar()[:2] for d in days}
    assert len(months) == 12 and len(weeks) >= 4 and len(days) <= 7 + 4 + 12 and min(days) >= 400 - 12 * 31 - 31   # the last 12 months and 4 weeks with a backup, the newest of each
    # Counts, not ages: two dailies keep the newest of the two most recent days that have a backup, however old.
    sparse = [{'id': 'old', 'kind': 'scheduled', 'completed_at': at(1)}, {'id': 'older', 'kind': 'scheduled', 'completed_at': at(0)},
              {'id': 'oldest', 'kind': 'scheduled', 'completed_at': at(0) - 3600}]
    assert retention.keep_site_backups(sparse, now, {'daily': 2, 'weekly': 0, 'monthly': 0}) == {'old', 'older'}
    assert retention.keep_site_backups(sparse, now, {'daily': 1, 'weekly': 0, 'monthly': 0}) == {'old'}
    assert retention.keep_site_backups(sparse, now, retention.DEFAULT['local']) == {'old', 'older'}


def test_dump_keep_set_keeps_window_and_newest_per_site():
    now = at(10)
    entries = [{'id': 'a-old', 'site_id': 'a', 'created': at(1)}, {'id': 'a-new', 'site_id': 'a', 'created': at(9, 20)},
               {'id': 'b-only', 'site_id': 'b', 'created': at(0)}, {'id': 'a-mid', 'site_id': 'a', 'created': at(5)}]
    assert retention.keep_dumps(entries, now, 2) == {'a-new', 'b-only'}


def test_policy_reads_defaults_maps_the_old_days_and_rejects_nonsense(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, 'OPS', tmp_path)
    assert retention.policy() == retention.DEFAULT and retention.DEFAULT['local'] == {'daily': 2, 'weekly': 0, 'monthly': 0}
    monkeypatch.setattr(retention, 'regular', lambda p: p.read_bytes())
    (tmp_path / 'server.yaml').write_text('retention: {database_days: 3, local: {daily: 1}, remote: {monthly: 24}}\n')
    rule = retention.policy()
    assert rule['database_days'] == 3 and rule['local'] == {'daily': 1, 'weekly': 0, 'monthly': 0} and rule['remote'] == {'daily': 7, 'weekly': 4, 'monthly': 24}
    # A server.yaml written before 1.4.9 keeps about what it kept, here and off-machine alike, until the operator chooses.
    (tmp_path / 'server.yaml').write_text('retention: {site: {within_days: 2, daily_days: 7, weekly_days: 31, monthly_days: 365}}\n')
    rule = retention.policy(); assert rule['local'] == rule['remote'] == {'daily': 7, 'weekly': 4, 'monthly': 12}
    for bad in ('retention: {database_days: 0}', 'retention: {local: {daily: 0}}', 'retention: {remote: {yearly: 1}}', 'retention: {local: {daily: 5000}}'):
        (tmp_path / 'server.yaml').write_text(bad + '\n')
        with pytest.raises(ValueError): retention.policy()
    (tmp_path / 'server.yaml').write_text('retention: {local: {daily: 2}}\n')
    assert retention.describe(retention.policy()) == 'database dumps 2 days; complete site backups: here the newest of the last 2 daily; off-machine the newest of the last 7 daily, 4 weekly, 12 monthly'


def test_local_dump_retention_respects_window_newest_and_pending_uploads(tmp_path, monkeypatch):
    from reeve.core import Ledger
    ledger = Ledger(tmp_path / 'jobs.db', sites=tmp_path / 'sites')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'dbsite', 'domain': 'dbsite.example.com'}); ledger.update(row['id'], 'succeeded', 'published')
    monkeypatch.setattr('reeve.database_site.state', lambda r: {'stage': 'ready', 'engine': 'postgres'})
    monkeypatch.setattr(local, 'STAGING', tmp_path / 'dumps'); local.STAGING.mkdir()
    monkeypatch.setattr(local, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr('reeve.retention.policy', lambda: {'database_days': 2, 'local': retention.DEFAULT['local'], 'remote': retention.DEFAULT['remote']})
    def artifact(age):
        job = ledger.submit_backup(str(uuid.uuid4()), row['id']); root = local.artifact_path(job['id']); root.mkdir()
        (root / 'database.sql').write_text('x'); (root / 'manifest.json').write_text('{}')
        ledger.finish_backup(job['id'], 'succeeded', artifact={'file': 'database.sql'})
        with ledger.db() as db: db.execute('UPDATE backup_jobs SET created=? WHERE id=?', (time.time() - age, job['id']))
        return job['id']
    old, older, fresh = artifact(5 * 86400), artifact(4 * 86400), artifact(3600)
    monkeypatch.setattr(remote, 'settings', lambda **k: None)
    local.retention_tick(ledger)
    states = {j['id']: j['state'] for j in ledger.backup_jobs(row['id'])}
    assert states[fresh] == 'succeeded' and states[old] == 'pruned' and states[older] == 'pruned'
    assert not local.artifact_path(old).exists() and local.artifact_path(fresh).exists()
    # With a destination configured, an old dump that was never copied off-machine stays.
    stale = artifact(6 * 86400)
    monkeypatch.setattr(remote, 'settings', lambda **k: {'destination': 'dest', 'enabled': True})
    local.retention_tick(ledger)
    assert {j['id']: j['state'] for j in ledger.backup_jobs(row['id'])}[stale] == 'succeeded'
    with ledger.db() as db: db.execute("INSERT INTO remote_copies(destination, job_id, snapshot, verified) VALUES ('dest', ?, 'abc', 1)", (stale,))
    local.retention_tick(ledger)
    assert {j['id']: j['state'] for j in ledger.backup_jobs(row['id'])}[stale] == 'pruned'


def test_remote_cycle_copies_site_snapshots_and_forgets_outside_policy(tmp_path, monkeypatch):
    from reeve.core import Ledger
    from reeve import site_backup as sb
    ledger = Ledger(tmp_path / 'jobs.db', sites=tmp_path / 'sites')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'remote', 'domain': 'remote.example.com'}); ledger.update(row['id'], 'succeeded', 'published')
    monkeypatch.setattr(sb, 'STAGING', tmp_path / 'site'); sb.STAGING.mkdir(); monkeypatch.setattr(sb, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(sb.ci, 'regular', lambda p: p.read_bytes())
    monkeypatch.setattr(remote, 'repository', lambda c: None)
    monkeypatch.setattr(remote, 'CACHE', tmp_path / 'cache')
    config = {'destination': 'dest', 'enabled': True, 'timeout_seconds': 10, 'repository_id': 'a' * 64, 'prune_local_after_days': 0, 'type': 'sftp'}
    monkeypatch.setattr(remote, 'settings', lambda **k: config)
    def snapshot(kind, age):
        ident = str(uuid.uuid4()); root = sb.artifact_path(ident); root.mkdir()
        (root / 'files.tar').write_bytes(b'tar ' + ident.encode())
        manifest = {'schema': 1, 'kind': 'site-backup', 'operation': ident, 'site_id': row['id'], 'site_name': 'remote', 'backup_kind': kind,
                    'files': {'file': 'files.tar', 'bytes': 40, 'sha256': sb.checksum(root / 'files.tar')}, 'dumps': {}, 'volumes': {}, 'completed_at': time.time() - age}
        (root / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True))
        with ledger.db() as db:
            db.execute("INSERT INTO site_backups (id, site_id, kind, state, step, error, manifest, created, updated) VALUES (?,?,?,'succeeded','','',?,?,?)", (ident, row['id'], kind, json.dumps(manifest, sort_keys=True), time.time() - age, time.time() - age))
        return ident, manifest
    recent, _ = snapshot('scheduled', 3600); ancient, ancient_manifest = snapshot('scheduled', 500 * 86400); final, _ = snapshot('final', 500 * 86400)
    calls = []
    def execute(config, args, maximum=0, digest=False):
        calls.append(args[0] if args[0] != 'dump' else 'dump')
        if args[0] == 'backup': return json.dumps({'message_type': 'summary', 'snapshot_id': 'f' * 64}).encode()
        if args[0] == 'snapshots': raise AssertionError('a fresh copy must not list the repository')
        if args[0] == 'dump' and args[2].endswith('manifest.json'):
            ident = args[2].split('/')[-2]; return (sb.artifact_path(ident) / 'manifest.json').read_bytes()
        if digest:
            ident = args[2].split('/')[-2]; data = (sb.artifact_path(ident) / 'files.tar').read_bytes(); import hashlib
            return len(data), hashlib.sha256(data).hexdigest()
        return b''
    monkeypatch.setattr(remote, 'execute', execute)
    monkeypatch.setattr('reeve.retention.policy', lambda: {**retention.DEFAULT, 'remote': {'daily': 1, 'weekly': 0, 'monthly': 0}})
    assert len(remote.pending_sites(ledger, 'dest')) == 3
    remote.cycle(ledger, config, force=True)
    with ledger.db() as db:
        receipts = {r['job_id']: dict(r) for r in db.execute('SELECT * FROM remote_copies')}
        state = db.execute('SELECT state, pruned_at FROM remote_cycles').fetchone()
    assert state[0] == 'succeeded' and all(receipts[i]['verified'] for i in (recent, ancient, final))
    assert receipts[ancient]['forgotten'] and not receipts[recent]['forgotten'] and not receipts[final]['forgotten']
    assert calls.count('forget') == 1 and calls.count('prune') == 1 and state[1] > 0, (calls, state)
    status = remote.status(ledger, row['id'])
    assert status['pending_sites'] == 0 and status['last_site_copy'] and 'complete site backups' in status['retention_text']


def test_local_site_prune_follows_the_local_counts_keeps_kept_and_waits_for_the_copy(tmp_path, monkeypatch):
    from reeve.core import Ledger
    from reeve import site_backup as sb
    ledger = Ledger(tmp_path / 'jobs.db', sites=tmp_path / 'sites')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'shop', 'domain': 'shop.example.com'}); ledger.update(row['id'], 'succeeded', 'published')
    monkeypatch.setattr(sb, 'STAGING', tmp_path / 'site'); sb.STAGING.mkdir(); monkeypatch.setattr(sb, 'trusted', lambda *a, **k: None)
    def backup(kind, age, size=100):
        ident = str(uuid.uuid4()); root = sb.artifact_path(ident); root.mkdir(); (root / 'files.tar').write_bytes(b'x' * size)
        manifest = {'completed_at': time.time() - age, 'files': {'file': 'files.tar', 'bytes': size}, 'dumps': {}}
        with ledger.db() as db:
            db.execute("INSERT INTO site_backups (id, site_id, kind, state, step, error, manifest, created, updated) VALUES (?,?,?,'succeeded','','',?,?,?)",
                       (ident, row['id'], kind, json.dumps(manifest), time.time() - age, time.time() - age))
        return ident
    newest, yesterday, older, oldest = backup('scheduled', 3600), backup('scheduled', 86400 + 3600), backup('manual', 3 * 86400, 5000), backup('scheduled', 9 * 86400, 7000)
    final = backup('final', 20 * 86400)
    import reeve.host as hm
    from reeve import settings as st
    ops = tmp_path / 'ops'; ops.mkdir(); (ops / 'server.yaml').write_text('schema: 1\nretention: {local: {daily: 2, weekly: 0, monthly: 0}}\n')
    for module in (st, hm, retention, sb, __import__('reeve.mail', fromlist=['OPS']), __import__('reeve.php_updates', fromlist=['OPS'])): monkeypatch.setattr(module, 'OPS', ops)
    monkeypatch.setattr(st, 'trusted', lambda *a, **k: None); monkeypatch.setattr(hm, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(retention, 'regular', lambda p: p.read_bytes())
    monkeypatch.setattr(remote, 'settings', lambda **k: None)
    surplus = sb.local_surplus(ledger, {'daily': 2, 'weekly': 0, 'monthly': 0})
    assert [j['id'] for j in surplus] == [oldest, older] and sum(j['bytes'] for j in surplus) == 12000
    # With a destination connected, a copy the repository has not verified yet stays whatever the counts say.
    monkeypatch.setattr(remote, 'settings', lambda **k: {'destination': 'dest', 'enabled': True})
    assert sb.local_surplus(ledger, {'daily': 2, 'weekly': 0, 'monthly': 0}) == []
    with ledger.db() as db:
        for ident in (oldest, older): db.execute("INSERT INTO remote_copies(destination, job_id, snapshot, verified) VALUES ('dest', ?, 's', 1)", (ident,))
    assert [j['id'] for j in sb.local_surplus(ledger, {'daily': 2, 'weekly': 0, 'monthly': 0})] == [oldest, older]
    # The operator keeps one; the prune removes the other and never the final.
    sb.mark_kept(ledger, [older])
    sb.prune(ledger)
    states = {j['id']: j['state'] for j in ledger.site_backups(row['id'], limit=None)}
    assert states == {newest: 'succeeded', yesterday: 'succeeded', older: 'succeeded', oldest: 'pruned', final: 'succeeded'}
    assert not sb.artifact_path(oldest).exists() and sb.artifact_path(older).exists()
    # The settings preview names what a candidate policy would remove; saving with "keep" marks them kept first.
    monkeypatch.setattr('reeve.remote_backup.settings', lambda **k: None)
    values = {'hour': '3', 'local_path': str(tmp_path / 'b'), 'database_days': '2', 'local_daily': '1', 'local_weekly': '0', 'local_monthly': '0', 'remote_daily': '7', 'remote_weekly': '4', 'remote_monthly': '12'}
    found = st.surplus(ledger, values)
    assert found['local'] == {'count': 1, 'bytes': 100, 'ids': [yesterday]} and found['remote']['count'] == 0
    monkeypatch.setattr(st, 'apply', lambda *a: 'applied')
    result = st.save(None, ledger, 'backups', {**values, 'existing': 'keep'})
    assert result['saved']['local_daily'] == 1 and result['note'].startswith('1 existing backup marked kept')
    with ledger.db() as db: assert db.execute('SELECT kept FROM site_backups WHERE id=?', (yesterday,)).fetchone()[0] == 1
    assert st.surplus(ledger, values)['local']['count'] == 0
