import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from reeve import database_backup as backup, backup_jobs
from reeve.core import Ledger


@pytest.fixture
def context(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / 'jobs.db', sites=tmp_path / 'sites')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'dbsite', 'domain': 'dbsite.example.com'})
    ledger.update(row['id'], 'succeeded', 'published'); row = ledger.get(row['id'])
    monkeypatch.setattr('reeve.database_site.state', lambda r: {'stage': 'ready', 'engine': 'postgres'})
    monkeypatch.setattr(backup, 'STAGING', tmp_path / 'staging'); backup.STAGING.mkdir()
    monkeypatch.setattr(backup, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr('reeve.host.trusted', lambda *a, **k: None)
    monkeypatch.setattr(backup.ci, 'regular', lambda path: path.read_bytes())
    monkeypatch.setattr(backup, 'policy', lambda: backup.DEFAULT_POLICY.copy())
    monkeypatch.setattr(backup, 'free_bytes', lambda: 100 * 1024**3)
    return ledger, row


def test_dump_reservation_is_idempotent_and_excludes_site_mutations(context):
    ledger, row = context; ident = str(uuid.uuid4())
    first = ledger.submit_backup(ident, row['id'])
    assert ledger.submit_backup(ident, row['id']) == first
    with pytest.raises(ValueError, match='pending'):
        ledger.submit_content(str(uuid.uuid4()), row['id'], 'tool', {'tool': 'shell', 'arguments': 'true', 'path': '.', 'internet': False})
    with pytest.raises(ValueError): ledger.submit_domains(str(uuid.uuid4()), row['id'], ['new.example.com'])
    with pytest.raises(ValueError): ledger.submit_backup(str(uuid.uuid4()), row['id'])
    assert len(ledger.backup_jobs()) == 1


def test_failed_attempt_does_not_erase_last_success_and_cleanup_holds_site(context):
    ledger, row = context
    first = ledger.submit_backup(str(uuid.uuid4()), row['id'])
    root = backup.artifact_path(first['id']); root.mkdir(); (root / 'database.sql').write_text('retained')
    artifact = {'file': 'database.sql'}
    ledger.finish_backup(first['id'], 'succeeded', artifact=artifact)
    second = ledger.submit_backup(str(uuid.uuid4()), row['id'])
    ledger.finish_backup(second['id'], 'failed', 'cap reached', cleanup_error='helper needs cleanup')
    result = backup_jobs.status(ledger, row)
    assert result['latest']['id'] == second['id'] and result['last_success']['id'] == first['id']
    assert result['available']
    with pytest.raises(ValueError): ledger.submit_backup(str(uuid.uuid4()), row['id'])


def test_schedule_enqueues_missed_period_once_and_keeps_all_completed_records(context):
    ledger, row = context
    backup_jobs.tick(ledger, now=1000)
    backup_jobs.tick(ledger, now=1900)
    assert len(ledger.backup_jobs()) == 1
    backup_jobs.tick(ledger, now=10000)
    assert len(ledger.backup_jobs()) == 1  # Already pending; cannot reconstruct old recovery points.
    first = ledger.backup_jobs()[0]; ledger.finish_backup(first['id'], 'succeeded', artifact={'file': 'database.sql'})
    backup_jobs.tick(ledger, now=10000)
    assert len(ledger.backup_jobs()) == 2
    assert any(j['id'] == first['id'] for j in ledger.backup_jobs())
    with ledger.db() as db: assert db.execute('SELECT next_run FROM backup_schedules').fetchone()[0] == 10900
    ledger.backup_schedule(row['id'], 60, False)
    backup_jobs.tick(ledger, now=10**12)
    assert len(ledger.backup_jobs()) == 2


def test_capacity_refusal_preserves_every_pending_artifact(context):
    _, _ = context
    root = backup.STAGING / 'retained'; root.mkdir(); file = root / 'database.sql'; file.write_bytes(b'x' * 100)
    settings = {**backup.DEFAULT_POLICY, 'artifact_limit_mb': 1, 'staging_limit_mb': 1}
    with pytest.raises(backup.DumpFailed, match='cap reached'): backup.capacity(settings)
    assert file.read_bytes() == b'x' * 100


def test_interrupted_dump_removes_only_its_owned_helper_and_marks_failure(context, monkeypatch):
    ledger, row = context
    job = ledger.submit_backup(str(uuid.uuid4()), row['id']); ledger.finish_backup(job['id'], 'running')
    partial = backup.artifact_path(job['id'], partial=True); partial.mkdir(); (partial / 'database.sql').write_text('incomplete')
    commands = []
    host = SimpleNamespace(inspect=lambda name: {'Config': {'Labels': {'hosting.backup': job['id']}}})
    monkeypatch.setattr(backup.ci, 'run', lambda args, **kwargs: commands.append(args))
    ledger.interrupted(); backup.recover(ledger, host)
    result = ledger.backup_jobs()[0]
    assert result['state'] == 'failed' and not result['artifact'] and not partial.exists()
    assert commands == [['docker', 'rm', '--force', backup.helper_name(job['id'])]]


def test_helper_identity_conflict_preserves_partial_and_blocks_followup(context, monkeypatch):
    ledger, row = context
    job = ledger.submit_backup(str(uuid.uuid4()), row['id']); ledger.finish_backup(job['id'], 'running')
    partial = backup.artifact_path(job['id'], partial=True); partial.mkdir()
    host = SimpleNamespace(inspect=lambda name: {'Config': {'Labels': {'hosting.backup': 'someone-else'}}})
    monkeypatch.setattr(backup.ci, 'run', lambda *a, **k: pytest.fail('Must not remove an unrelated container'))
    ledger.interrupted(); backup.recover(ledger, host)
    assert ledger.backup_jobs()[0]['cleanup_error'] and partial.exists()
    with pytest.raises(ValueError): ledger.submit_backup(str(uuid.uuid4()), row['id'])


def test_recovery_adopts_atomically_published_dump_after_ledger_interruption(context):
    ledger, row = context
    job = ledger.submit_backup(str(uuid.uuid4()), row['id']); ledger.finish_backup(job['id'], 'running')
    root = backup.artifact_path(job['id']); root.mkdir(); output = root / 'database.sql'; output.write_text('complete')
    manifest = {'schema': 1, 'operation': job['id'], 'site_id': row['id'], 'file': output.name,
                'bytes': output.stat().st_size, 'sha256': backup.checksum(output)}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    ledger.interrupted(); backup.recover(ledger, SimpleNamespace(inspect=lambda name: None))
    assert ledger.backup_jobs()[0]['state'] == 'succeeded'
    output.write_text('corrupt')
    with pytest.raises(backup.DumpFailed, match='checksum'): backup.completed(job)


def test_failure_diagnostics_are_redacted_and_cleanup_does_not_overwrite_primary(context, monkeypatch):
    ledger, row = context
    job = ledger.submit_backup(str(uuid.uuid4()), row['id'])
    def fail(*args): raise RuntimeError('password=secret')
    monkeypatch.setattr(backup, 'dump', fail); monkeypatch.setattr(backup, 'cleanup', fail)
    backup.perform(ledger, None, job)
    result = ledger.backup_jobs()[0]
    assert 'secret' not in json.dumps(result) and result['error'] and result['cleanup_error']
    monkeypatch.setattr(backup, 'dump', lambda *a: {'file': 'database.sql'})
    backup.perform(ledger, None, job)
    result = ledger.backup_jobs()[0]
    assert result['state'] == 'succeeded' and result['artifact'] and result['cleanup_error']


def test_native_arguments_retain_routines_events_triggers_and_database_scope():
    for engine in ('mysql', 'mariadb'):
        args = backup.client_args({'engine': engine})
        assert {'--routines', '--events', '--triggers', '--lock-all-tables'} <= set(args)
        assert args[-2:] == ['--databases', 'site'] and '--single-transaction' not in args
    assert '--format=custom' in backup.client_args({'engine': 'postgres'})
    with pytest.raises(backup.DumpFailed): backup.client_args({'engine': 'unknown'})


def test_streaming_enforces_real_byte_and_time_limits(context, monkeypatch):
    import subprocess
    import sys
    popen = subprocess.Popen
    def noisy(args, **kwargs):
        return popen([*args[:3], sys.executable, '-c', 'import os; os.write(1,b"x"*1048576)'], **kwargs)
    monkeypatch.setattr(backup.subprocess, 'Popen', noisy)
    path = backup.STAGING / 'oversized.sql'
    with pytest.raises(backup.DumpFailed): backup.stream_dump('test', path, 65536, backup.DEFAULT_POLICY)
    assert path.stat().st_size <= 65536
    def slow(args, **kwargs):
        return popen([*args[:3], sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
    monkeypatch.setattr(backup.subprocess, 'Popen', slow)
    with pytest.raises(backup.DumpFailed, match='timed out'):
        backup.stream_dump('test', backup.STAGING / 'slow.sql', 65536, {**backup.DEFAULT_POLICY, 'timeout_seconds': 0.1})


def test_restore_selects_the_site_database_and_foreign_dumps_are_detected(tmp_path):
    # A plain single-database mysqldump carries no USE statement; the client must default to `site`.
    assert backup.client_restore('mysql', 'database.sql').startswith('mysql ') and ' site < /restore/database.sql' in backup.client_restore('mysql', 'database.sql')
    assert backup.client_restore('mariadb', 'x.sql').startswith('mariadb ')
    plain = tmp_path / 'plain.sql'; plain.write_bytes(b'-- MySQL dump\n/*!40101 SET NAMES utf8mb4 */;\nCREATE TABLE `drupal_users` (id int);\nINSERT INTO `drupal_users` VALUES (1);\n')
    assert backup.foreign_databases(plain) == []
    own = tmp_path / 'own.sql'; own.write_bytes(b'CREATE DATABASE /*!32312 IF NOT EXISTS*/ `site` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;\nUSE `site`;\n')
    assert backup.foreign_databases(own) == []
    foreign = tmp_path / 'foreign.sql'; foreign.write_bytes(b'CREATE DATABASE /*!32312 IF NOT EXISTS*/ `alice_drupal`;\nUSE `alice_drupal`;\n-- USE not a statement\nuse other;\n')
    assert backup.foreign_databases(foreign) == ['alice_drupal', 'other']
