import hashlib
import json
import sys
import time
import uuid

import pytest

from reeve import remote_backup as remote, database_backup as local
from reeve.core import Ledger


@pytest.fixture
def setup(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / 'jobs.db', sites=tmp_path / 'sites')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'remote', 'domain': 'remote.example.com'})
    ledger.update(row['id'], 'succeeded', 'published')
    monkeypatch.setattr('reeve.database_site.state', lambda row: {'stage': 'ready'})
    monkeypatch.setattr(local, 'STAGING', tmp_path / 'dumps'); local.STAGING.mkdir()
    monkeypatch.setattr(local, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(remote, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(local.ci, 'regular', lambda p: p.read_bytes())
    monkeypatch.setattr(remote, 'regular', lambda p: p.read_bytes())
    config = {'id': 'test-id', 'name': 'Test', 'destination': 'test', 'repository_id': 'a' * 64, 'enabled': True, 'prune_local_after_days': 0,
              'timeout_seconds': 10, 'type': 'sftp', 'repository': 'sftp://backup@example.com//repo',
              'password_file': '/private/password', 'ssh_key_file': '/private/key', 'known_hosts_file': '/private/known_hosts'}
    monkeypatch.setattr(remote, 'settings', lambda **k: config)
    monkeypatch.setattr(remote, 'destinations', lambda **k: [config])
    monkeypatch.setattr(remote, 'destination', lambda ident, **k: config)
    monkeypatch.setattr(remote, 'CACHE', tmp_path / 'cache')
    def artifact(age=0):
        job = ledger.submit_backup(str(uuid.uuid4()), row['id'])
        root = local.artifact_path(job['id']); root.mkdir()
        (root / 'database.sql').write_bytes(b'runtime-only records ' + job['id'].encode())
        manifest = {'schema': 1, 'operation': job['id'], 'site_id': row['id'], 'file': 'database.sql',
                    'bytes': (root / 'database.sql').stat().st_size, 'sha256': local.checksum(root / 'database.sql')}
        (root / 'manifest.json').write_text(json.dumps(manifest))
        ledger.finish_backup(job['id'], 'succeeded', artifact=manifest)
        with ledger.db() as db: db.execute('UPDATE backup_jobs SET created=? WHERE id=?', (time.time() - age, job['id']))
        return next(j for j in ledger.backup_jobs() if j['id'] == job['id'])
    return ledger, row, config, artifact


def test_outage_retries_all_backlog_beyond_history_and_keeps_local_work_independent(setup, monkeypatch):
    ledger, row, config, artifact = setup
    jobs = [artifact() for _ in range(57)]
    assert len(ledger.backup_jobs()) == 50 and len(remote.pending(ledger, 'test')) == 57
    monkeypatch.setattr(remote, 'repository', lambda c: None)
    def fail(*a): raise RuntimeError('AWS_SECRET_ACCESS_KEY=do-not-print')
    monkeypatch.setattr(remote, 'copy', fail)
    remote.cycle(ledger, config)
    assert len(remote.pending(ledger, 'test')) == 57
    result = remote.status(ledger, row['id'])
    assert result['cycle']['state'] == 'failed' and 'do-not-print' not in json.dumps(result)
    # Network failure neither reserves the site nor prevents another local dump.
    jobs.append(artifact())
    copied = []
    monkeypatch.setattr(remote, 'copy', lambda c, j: copied.append(j['id']) or 'b' * 64)
    remote.cycle(ledger, config)  # Not yet hourly due.
    assert not copied
    remote.request(ledger); remote.cycle(ledger, config)
    assert len(copied) == 58 and not remote.pending(ledger, 'test')
    assert all(local.artifact_path(j['id']).exists() for j in jobs)
    assert remote.status(ledger, row['id'])['cycle']['state'] == 'succeeded'
    # New destination never inherits another destination's receipts.
    assert len(remote.pending(ledger, 'another-repository')) == 58


def test_remote_publish_before_receipt_is_adopted_only_after_download_checks(setup, monkeypatch):
    ledger, row, config, artifact = setup; job = artifact()
    manifest = json.loads(job['artifact']); calls = []
    def execute(c, args, **kwargs):
        calls.append(args[0])
        if args[0] == 'snapshots': return json.dumps([{'id': 'b' * 64}]).encode()
        if args[-1].endswith('manifest.json'): return json.dumps(manifest).encode()
        return manifest['bytes'], manifest['sha256']
    monkeypatch.setattr(remote, 'execute', execute)
    recovering = {**job, 'recover': True}
    assert remote.copy(config, recovering) == 'b' * 64
    assert calls == ['snapshots', 'dump', 'dump']  # No duplicate backup after interrupted receipt.
    manifest['sha256'] = 'c' * 64
    with pytest.raises(remote.RemoteFailed, match='manifest differs'): remote.copy(config, recovering)


def test_successful_backup_exit_is_insufficient_without_remote_bytes(setup, monkeypatch):
    ledger, row, config, artifact = setup; job = artifact(); manifest = json.loads(job['artifact'])
    seen = []
    def execute(c, args, **kwargs):
        seen.append(args[0])
        if args[0] == 'snapshots': return json.dumps([{'id': 'b' * 64}]).encode()
        if args[0] == 'backup': return b'{}'  # No summary line: the identity is then looked up by tag.
        if args[-1].endswith('manifest.json'): return json.dumps(manifest).encode()
        return manifest['bytes'], 'c' * 64
    monkeypatch.setattr(remote, 'execute', execute)
    with pytest.raises(remote.RemoteFailed, match='checksum'): remote.copy(config, job)
    assert 'backup' in seen and local.artifact_path(job['id']).exists()


def test_retention_requires_fresh_remote_check_and_keeps_latest_and_pending(setup, monkeypatch):
    ledger, row, config, artifact = setup
    old = artifact(age=10 * 86400); newest = artifact(age=9 * 86400)
    config['prune_local_after_days'] = 7
    with ledger.db() as db:
        for j in (old, newest):
            db.execute('INSERT INTO remote_copies(destination,job_id,snapshot,verified) VALUES (?,?,?,?)', ('test', j['id'], 'b' * 64, time.time()))
    def fail(*a): raise remote.RemoteFailed('unreachable')
    monkeypatch.setattr(remote, 'verify', fail)
    with pytest.raises(remote.RemoteFailed): remote.prune(ledger, config)
    assert local.artifact_path(old['id']).exists()
    verified = []
    monkeypatch.setattr(remote, 'verify', lambda c, j, m, s: verified.append(j['id']))
    remote.prune(ledger, config)
    assert verified == [old['id']] and not local.artifact_path(old['id']).exists()
    assert local.artifact_path(newest['id']).exists()
    pending = artifact(age=20 * 86400)
    remote.prune(ledger, config)
    assert local.artifact_path(pending['id']).exists()


def test_repository_identity_change_stops_upload_and_prune(setup, monkeypatch):
    ledger, row, config, artifact = setup; artifact()
    monkeypatch.setattr(remote, 'execute', lambda *a, **k: json.dumps({'id': 'c' * 64}).encode())
    monkeypatch.setattr(remote, 'copy', lambda *a: pytest.fail('Do not upload to replaced repository'))
    remote.cycle(ledger, config)
    assert 'identity changed' in remote.status(ledger, row['id'])['cycle']['error']


def test_retention_resumes_after_interruption_between_file_unlinks(setup, monkeypatch):
    from pathlib import Path
    ledger, row, config, artifact = setup
    old = artifact(age=10 * 86400); latest = artifact()
    config['prune_local_after_days'] = 7
    with ledger.db() as db:
        db.execute('INSERT INTO remote_copies(destination,job_id,snapshot,verified) VALUES (?,?,?,?)', ('test', old['id'], 'b' * 64, time.time()))
    checks = []
    monkeypatch.setattr(remote, 'verify', lambda *a: checks.append(True))
    unlink = Path.unlink
    def interrupted(path, **kwargs):
        if path.name == 'manifest.json': raise OSError('simulated interruption')
        return unlink(path, **kwargs)
    monkeypatch.setattr(Path, 'unlink', interrupted)
    with pytest.raises(OSError): remote.prune(ledger, config)
    assert not (local.artifact_path(old['id']) / 'database.sql').exists()
    with ledger.db() as db: assert db.execute('SELECT pruned FROM remote_copies').fetchone()[0] > 0
    monkeypatch.setattr(Path, 'unlink', unlink)
    remote.prune(ledger, config)
    assert len(checks) == 2 and not local.artifact_path(old['id']).exists()
    assert local.artifact_path(latest['id']).exists()


def test_interrupted_cycle_and_manual_request_during_upload_are_not_lost(setup, monkeypatch):
    ledger, row, config, artifact = setup; artifact()
    remote.request(ledger)
    with ledger.db() as db: db.execute("UPDATE remote_cycles SET state='running',next_run=?", (time.time() + 3600,))
    monkeypatch.setattr(remote, 'repository', lambda c: None)
    def copy(*a): remote.request(ledger); return 'b' * 64
    monkeypatch.setattr(remote, 'copy', copy)
    remote.cycle(ledger, config)
    assert not remote.pending(ledger, 'test')
    assert remote.status(ledger, row['id'])['cycle']['next_run'] == 0


def test_transports_have_strict_host_verification_and_isolated_credentials(setup, monkeypatch, tmp_path):
    _, _, config, _ = setup
    args, env = remote.command(config)
    assert 'StrictHostKeyChecking=yes' in args[-1] and 'BatchMode=yes' in args[-1]
    assert 'GlobalKnownHostsFile=/dev/null' in args[-1]
    credentials = tmp_path / 'aws.json'; credentials.write_text(json.dumps({'AWS_ACCESS_KEY_ID': 'id', 'AWS_SECRET_ACCESS_KEY': 'secret'})); credentials.chmod(0o600)
    args, env = remote.command({**config, 'type': 's3', 'aws_credentials_file': str(credentials)})
    assert 'secret' not in repr(args) and env['AWS_SECRET_ACCESS_KEY'] == 'secret'
    assert env['AWS_EC2_METADATA_DISABLED'] == 'true' and 'AWS_PROFILE' not in env


def test_real_download_stream_is_bounded_and_hashed_without_sql_tempfile(monkeypatch):
    config = {'timeout_seconds': 0.2}
    monkeypatch.setattr(remote, 'command', lambda c: ([sys.executable, '-c'], {'PATH': '/usr/bin'}))
    data = b'complete download'
    assert remote.execute(config, ["import os; os.write(1,b'complete download')"], maximum=100, digest=True) == (len(data), hashlib.sha256(data).hexdigest())
    with pytest.raises(remote.RemoteFailed, match='recorded size'):
        remote.execute(config, ['import os; os.write(1,b"x"*10000)'], maximum=100, digest=True)
    with pytest.raises(remote.RemoteFailed, match='timed out'):
        remote.execute(config, ['import time; time.sleep(20)'], maximum=100, digest=True)


def test_configuration_rejects_commands_plain_http_secrets_in_urls_and_wrong_modes(tmp_path, monkeypatch):
    homes = tmp_path / 'destinations'; home = homes / '33333333-3333-3333-3333-333333333333'; home.mkdir(parents=True)
    monkeypatch.setattr(remote, 'DESTINATIONS', homes); monkeypatch.setattr(remote, 'CONFIG', tmp_path / 'legacy.json'); monkeypatch.setattr(remote, 'CACHE', tmp_path / 'cache')
    monkeypatch.setattr(remote, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(remote, 'regular', lambda p: p.read_bytes())
    secret = tmp_path / 'secret'; secret.write_text('private'); secret.chmod(0o600)
    config = {'type': 'sftp', 'repository': 'sftp://backup@example.com:2222//repo', 'repository_id': 'a' * 64,
              'password_file': str(secret), 'ssh_key_file': str(secret), 'known_hosts_file': str(secret)}
    def write(c): (home / 'config.json').write_text(json.dumps(c)); (home / 'config.json').chmod(0o600)
    write(config); assert remote.settings()['type'] == 'sftp' and remote.settings()['id'] == home.name and remote.settings()['name'] == 'example.com'
    for invalid in ({**config, 'repository': 'sftp://root:password@example.com//repo'}, {**config, 'command': 'anything'},
                    {**config, 'repository_id': 'wrong'}, {**config, 'prune_local_after_days': 1}, {**config, 'name': 'x' * 41},
                    {**config, 'type': 'local', 'repository': '/srv/sites/shop'}, {**config, 'type': 'local', 'repository': '/etc/reeve'}, {**config, 'type': 'local', 'repository': 'relative'}):
        write(invalid)
        with pytest.raises(remote.RemoteFailed): remote.settings()
        assert remote.destinations() == [] and remote.destinations(include_invalid=True)[0]['invalid']
    write(config); secret.chmod(0o644)
    with pytest.raises(remote.RemoteFailed): remote.settings()
    secret.chmod(0o600)
    aws = tmp_path / 'aws'; aws.write_text(json.dumps({'AWS_ACCESS_KEY_ID': 'id', 'AWS_SECRET_ACCESS_KEY': 'secret'})); aws.chmod(0o600)
    s3 = {'type': 's3', 'repository': 's3:https://s3.eu-west-2.amazonaws.com/test-bucket/hosting',
          'repository_id': 'a' * 64, 'password_file': str(secret), 'aws_credentials_file': str(aws)}
    write(s3); assert remote.settings()['type'] == 's3' and remote.settings()['name'] == 'test-bucket'
    write({**s3, 'repository': s3['repository'].replace('https:', 'http:')})
    with pytest.raises(remote.RemoteFailed): remote.settings()
    folder = {'type': 'local', 'name': 'Disk', 'repository': str(tmp_path / 'repo'), 'repository_id': 'b' * 64, 'password_file': str(secret)}
    write(folder); found = remote.settings()
    assert found['type'] == 'local' and found['name'] == 'Disk' and remote.command(found)[1]['RESTIC_REPOSITORY'] == str(tmp_path / 'repo')
    # Two destinations: every enabled one is a copy target, the guard wants a receipt from each.
    other = homes / '44444444-4444-4444-4444-444444444444'; other.mkdir()
    (other / 'config.json').write_text(json.dumps({**s3, 'name': 'S3', 'enabled': False, 'created': 5})); (other / 'config.json').chmod(0o600)
    assert [d['name'] for d in remote.destinations()] == ['Disk', 'S3'] and [d['name'] for d in remote.enabled_destinations()] == ['Disk']
    assert remote.destination(other.name)['name'] == 'S3'
    with pytest.raises(ValueError): remote.destination('not-an-id')


def test_a_cycle_clears_stale_repository_locks_before_uploading(setup, monkeypatch):
    ledger, row, config, artifact = setup; artifact()
    seen = []
    def execute(config, args, maximum=None, digest=False):
        seen.append(args)
        if args[:2] == ['cat', 'config']: return '{"id": "%s"}' % config['repository_id']
        return ''
    monkeypatch.setattr(remote, 'execute', execute)
    remote.repository(config)
    assert seen == [['cat', 'config'], ['unlock']]
