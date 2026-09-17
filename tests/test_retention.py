import datetime
import json
import time
import uuid

import pytest

from reeve import retention, remote_backup as remote, database_backup as local


def at(day, hour=3):
    return datetime.datetime(2026, 1, 1, hour).timestamp() + day * 86400


def test_site_keep_set_thins_by_age_and_protects_final_and_imported():
    now = at(400)
    entries = [{'id': 'd%d' % d, 'kind': 'scheduled', 'completed_at': at(d)} for d in range(0, 400)]
    entries += [{'id': 'final', 'kind': 'final', 'completed_at': at(10)}, {'id': 'imported', 'kind': 'imported', 'completed_at': at(20)},
                {'id': 'safety', 'kind': 'pre-restore', 'completed_at': at(399) + 60}]
    rule = {'within_days': 2, 'daily_days': 7, 'weekly_days': 31, 'monthly_days': 365}
    keep = retention.keep_site_backups(entries, now, rule)
    assert {'final', 'imported', 'safety'} <= keep
    days = sorted(int(i[1:]) for i in keep if i.startswith('d'))
    assert days[-7:] == list(range(393, 400))                       # one per day for the last week (the newest of day 399 is the safety backup, same bucket)
    assert all(d >= 400 - 365 for d in days)                          # nothing older than a year
    weekly = [d for d in days if 400 - 31 <= d < 393]; monthly = [d for d in days if d < 400 - 31]
    assert 3 <= len(weekly) <= 5 and 10 <= len(monthly) <= 12         # one per ISO week for a month, one per month for a year
    assert retention.keep_site_backups(entries, now, rule) == keep
    assert 'd50' not in keep and 'd398' in keep


def test_dump_keep_set_keeps_window_and_newest_per_site():
    now = at(10)
    entries = [{'id': 'a-old', 'site_id': 'a', 'created': at(1)}, {'id': 'a-new', 'site_id': 'a', 'created': at(9, 20)},
               {'id': 'b-only', 'site_id': 'b', 'created': at(0)}, {'id': 'a-mid', 'site_id': 'a', 'created': at(5)}]
    assert retention.keep_dumps(entries, now, 2) == {'a-new', 'b-only'}


def test_policy_reads_defaults_and_rejects_nonsense(tmp_path, monkeypatch):
    monkeypatch.setattr(retention, 'OPS', tmp_path)
    assert retention.policy() == retention.DEFAULT
    (tmp_path / 'server.yaml').write_text('retention: {database_days: 3, site: {daily_days: 14}}\n')
    monkeypatch.setattr(retention, 'regular', lambda p: p.read_bytes())
    rule = retention.policy(); assert rule['database_days'] == 3 and rule['site']['daily_days'] == 14 and rule['site']['monthly_days'] == 365
    (tmp_path / 'server.yaml').write_text('retention: {database_days: 0}\n')
    with pytest.raises(ValueError): retention.policy()
    assert '3 days' in retention.describe(rule)


def test_local_dump_retention_respects_window_newest_and_pending_uploads(tmp_path, monkeypatch):
    from reeve.core import Ledger
    ledger = Ledger(tmp_path / 'jobs.db', sites=tmp_path / 'sites')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'dbsite', 'domain': 'dbsite.example.com'}); ledger.update(row['id'], 'succeeded', 'published')
    monkeypatch.setattr('reeve.database_site.state', lambda r: {'stage': 'ready', 'engine': 'postgres'})
    monkeypatch.setattr(local, 'STAGING', tmp_path / 'dumps'); local.STAGING.mkdir()
    monkeypatch.setattr(local, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr('reeve.retention.policy', lambda: {'database_days': 2, 'site': retention.DEFAULT['site']})
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
            db.execute("INSERT INTO site_backups VALUES (?,?,?,'succeeded','','',?,?,?)", (ident, row['id'], kind, json.dumps(manifest, sort_keys=True), time.time() - age, time.time() - age))
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
    monkeypatch.setattr('reeve.retention.policy', lambda: retention.DEFAULT)
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
