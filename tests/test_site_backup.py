import io
import json
import subprocess
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from reeve import site_backup as sb, package_deploy as pd, compose_adopt as ca, compose_inspect as ci, host as hm
from reeve.core import DEFAULTS, Ledger
from test_application_intake import DATA


@pytest.fixture
def context(tmp_path, monkeypatch):
    sites = tmp_path / 'sites'; sites.mkdir()
    for module in (pd, ca, sb):
        monkeypatch.setattr(module, 'SITES', sites)
        monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(pd, 'STORE', tmp_path / 'packages'); monkeypatch.setattr(ca, 'STORE', tmp_path / 'adoptions')
    monkeypatch.setattr(pd, 'PROXY', tmp_path / 'proxy'); monkeypatch.setattr(ca, 'PROXY', tmp_path / 'proxy')
    monkeypatch.setattr(sb, 'STAGING', tmp_path / 'backups/staging/site'); sb.STAGING.mkdir(parents=True)
    monkeypatch.setattr(sb, 'RESERVE_BYTES', 0)
    for module in (ci, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(ci, 'regular', lambda path: Path(path).read_bytes())
    # Plain tar locally; the worker wraps it in prlimit/env on the VM.
    monkeypatch.setattr(sb, 'run_tar', lambda args, ident, timeout: subprocess.run(['tar', *args], check=True, capture_output=True, text=True).stdout)
    ledger = Ledger(tmp_path / 'jobs.db', sites)
    host = SimpleNamespace(defaults=DEFAULTS, inspect=lambda name: None, unpublish=lambda row: None)
    # A published package site with a bind, a database volume, a data volume and a restored dump.
    ident = str(uuid.uuid4())
    row = pd.submit(ledger, host, ident, DATA, 'a' * 64)
    ledger.update(ident, 'succeeded', 'published'); row = ledger.get(ident)
    root = sites / DATA['name']; (root / 'code').mkdir(parents=True); (root / 'dumps').mkdir()
    (root / 'compose.yaml').write_text('services: {web: {image: x}}\n'); (root / 'code/app.py').write_text('print(1)')
    (root / 'dumps/db.sql').write_text('-- original input dump')
    state = pd.load(row); state.update(stage='published', images={'web': 'sha256:web', 'db': 'sha256:db', 'store': 'sha256:store'},
                                     databases={'db': {'engine': 'mysql', 'dump': 'dumps/db.sql', 'restored': True}})
    pd.save(row, state)
    volumes = tmp_path / 'volumes'; (volumes / 'dbdata').mkdir(parents=True); (volumes / 'files').mkdir()
    (volumes / 'dbdata/ibdata1').write_bytes(b'raw'); (volumes / 'files/upload.txt').write_text('uploaded')
    plan = {'name': DATA['name'], 'project_name': 'package-' + ident, 'route': {'domain': DATA['domain'], 'aliases': [], 'web_service': 'web', 'internal_port': 8000},
            'summary': {'services': [{'name': 'web', 'image': 'x'}, {'name': 'db', 'image': 'mysql:5.7'}, {'name': 'store', 'image': 'busybox'}]},
            'model': {'services': {'web': {'image': 'sha256:web', 'volumes': [{'type': 'bind', 'source': str(root / 'code'), 'target': '/app'}]},
                                   'db': {'image': 'sha256:db', 'environment': {'MYSQL_ROOT_PASSWORD': 'pw', 'MYSQL_DATABASE': 'app'}, 'volumes': [{'type': 'volume', 'source': 'db_data', 'target': '/var/lib/mysql'}]},
                                   'store': {'image': 'sha256:store', 'volumes': [{'type': 'volume', 'source': 'files', 'target': '/files'}]}}},
            'images': state['images'], 'volumes': {'db_data': {'name': 'p_db_data', 'external': False}, 'files': {'name': 'p_files', 'external': False}},
            'stage': 'published', 'sources': [], 'original_containers': [], 'package_id': ident}
    ca.plan_path(ident).mkdir(parents=True); ca.save(row, plan)
    for name in ('resolved.compose.json', 'compose.hosting.yaml'): (ca.plan_path(ident) / name).write_text('{}')
    records = {'p_db_data': {'Name': 'p_db_data', 'Mountpoint': str(volumes / 'dbdata')}, 'p_files': {'Name': 'p_files', 'Mountpoint': str(volumes / 'files')}}
    monkeypatch.setattr(ca, 'volume_record', lambda name: records.get(name))
    dumps = []
    def dump_package(host, row, job):
        from reeve import database_backup as db
        folder = db.artifact_path(job['id']); folder.mkdir(parents=True)
        (folder / 'database.sql').write_text('-- fresh dump'); (folder / 'manifest.json').write_text('{}')
        dumps.append(job['id'])
        return {'file': 'database.sql', 'bytes': 13, 'sha256': sb.checksum(folder / 'database.sql'), 'engine': 'mysql', 'database': 'app', 'consistency': 'global read lock during dump'}
    monkeypatch.setattr('reeve.database_backup.dump_package', dump_package)
    monkeypatch.setattr('reeve.database_backup.cleanup', lambda host, job: None)
    monkeypatch.setattr('reeve.database_backup.STAGING', tmp_path / 'backups/staging/db'); (tmp_path / 'backups/staging/db').mkdir()
    return ledger, host, row, volumes, dumps


def test_capture_takes_fresh_dump_files_and_non_database_volumes(context):
    ledger, host, row, volumes, dumps = context
    job = ledger.submit_site_backup(str(uuid.uuid4()), row['id'])
    with pytest.raises(ValueError): ledger.submit_domains(str(uuid.uuid4()), row['id'], ['other.example.com'])
    manifest = sb.perform(ledger, host, job)
    assert manifest and manifest['coverage'] == 'complete' and not manifest['application_consistent']
    assert manifest['volumes']['db_data']['method'] == 'logical dump' and 'file' not in manifest['volumes']['db_data']
    assert manifest['volumes']['files']['file'] == 'volumes/files.tar'
    assert manifest['dumps']['db']['job_id'] == dumps[0] and ledger.backup_jobs(row['id'])[0]['state'] == 'succeeded'
    root = sb.artifact_path(job['id'])
    with tarfile.open(root / 'files.tar') as files: names = files.getnames()
    assert './code/app.py' in names and './dumps/db.sql' in names
    with tarfile.open(root / 'volumes/files.tar') as tar: assert './upload.txt' in tar.getnames()
    assert (root / 'dumps/db/database.sql').read_text() == '-- fresh dump'
    assert (root / 'config/plan.json').exists() and (root / 'config/package-state.json').exists()
    status = sb.status(ledger, row)
    assert status['available'] and status['manifest']['coverage'] == 'complete' and status['manifest']['volumes'] == {'db_data': 'logical dump', 'files': 'raw copy (not quiesced)'}
    from reeve.recovery_context import summary
    assert summary({'site': status, 'remote': {}})['title'] == 'Backed up locally'
    assert summary({'site': status, 'remote': {'last_site_copy': {'verified': 5.0}}})['title'] == 'Backed up and copied off'
    assert sb.completed(job)['operation'] == job['id']


def test_failed_backup_publishes_nothing_and_a_partial_is_cleaned(context, monkeypatch):
    ledger, host, row, volumes, dumps = context
    monkeypatch.setattr(sb, 'archive_tree', lambda *a, **k: (_ for _ in ()).throw(sb.SiteBackupFailed('synthetic archive failure')))
    job = ledger.submit_site_backup(str(uuid.uuid4()), row['id'])
    assert sb.perform(ledger, host, job) is None
    latest = ledger.site_backups(row['id'])[0]
    assert latest['state'] == 'failed' and 'synthetic' in latest['error']
    assert not sb.artifact_path(job['id']).exists() and not sb.artifact_path(job['id'], partial=True).exists()
    assert not sb.status(ledger, row)['available']


def test_restore_submits_captured_site_as_new_package_with_fresh_dump_and_volumes(context, monkeypatch):
    ledger, host, row, volumes, dumps = context
    job = ledger.submit_site_backup(str(uuid.uuid4()), row['id']); manifest = sb.perform(ledger, host, job)
    with pytest.raises(ValueError): sb.restore(ledger, host, job['id'], DATA['name'], 'copy.example.com')  # name in use
    new = sb.restore(ledger, host, job['id'], 'example-copy', 'copy.example.com')
    assert new['name'] == 'example-copy' and json.loads(new['payload'])['package_input']['service'] == 'web'
    package = pd.state_path(new['id']) / 'package'
    with tarfile.open(package) as tar:
        names = tar.getnames()
        assert 'dumps/db.sql' in names and 'code/app.py' in names and not any(n.startswith('./') for n in names)
        assert tar.extractfile('dumps/db.sql').read() == b'-- fresh dump'
    from reeve.application_package import review
    report = review(package, json.loads(new['payload'])['package_input'])
    assert report['state'] == 'reviewed'
    state = pd.load(new)
    assert state['restore_from']['snapshot'] == job['id'] and state['restore_from']['volumes'] == {'files': 'volumes/files.tar'}
    # Volumes are repopulated once, only when empty, before the first start.
    fresh = volumes / 'fresh'; fresh.mkdir()
    monkeypatch.setattr(ca, 'volume_record', lambda name: {'Name': name, 'Mountpoint': str(fresh)} if name == 'new_files' else None)
    plan = {'volumes': {'files': {'name': 'new_files'}}}
    sb.restore_volumes(new, plan, state)
    assert (fresh / 'upload.txt').read_text() == 'uploaded' and pd.load(new)['volumes_restored'] == ['files']
    (fresh / 'upload.txt').write_text('changed at runtime')
    sb.restore_volumes(new, plan, pd.load(new))
    assert (fresh / 'upload.txt').read_text() == 'changed at runtime'
    with pytest.raises(ValueError, match='Unknown site backup'): sb.restore(ledger, host, str(uuid.uuid4()), 'x', 'x.example.com')


def test_delete_requires_a_successful_final_backup_before_removing_anything(context, monkeypatch):
    ledger, host, row, volumes, dumps = context
    removed = []
    monkeypatch.setattr(sb, 'command', lambda args, timeout=120: removed.append(args) or '')
    monkeypatch.setattr(ca, 'docker', lambda args, **k: removed.append(['docker', *args]))
    monkeypatch.setattr(ca, 'project_containers', lambda plan: [])
    host.unpublish = lambda row: removed.append(['unpublish'])
    monkeypatch.setattr(sb, 'archive_tree', lambda *a, **k: (_ for _ in ()).throw(sb.SiteBackupFailed('disk full')))
    job = ledger.submit_site_delete(str(uuid.uuid4()), row['id'])
    sb.perform_delete(ledger, host, job)
    current = ledger.site_deletes(row['id'])[0]
    assert current['state'] == 'failed' and 'disk full' in current['error'] and not removed
    assert (sb.SITES / DATA['name'] / 'code/app.py').exists() and ledger.get(row['id'])['state'] == 'succeeded'
    # After the cause is fixed, retry takes a new final backup and then removes everything.
    monkeypatch.setattr(sb, 'archive_tree', lambda source, target, **kwargs: (Path(target).write_bytes(b'tar'), 1)[1])
    ledger.retry_site_delete(job['id'])
    sb.perform_delete(ledger, host, ledger.site_deletes(row['id'])[0])
    current = ledger.site_deletes(row['id'])[0]
    assert current['state'] == 'succeeded', current['error']
    final = next(b for b in ledger.site_backups(row['id']) if b['id'] == current['backup_id'])
    assert final['kind'] == 'final' and final['state'] == 'succeeded'
    assert ['unpublish'] in removed and any('down' in r for r in removed) and any('volume' in r and 'rm' in r for r in removed)
    assert not (sb.SITES / DATA['name']).exists()
    assert row['id'] not in [r['id'] for r in ledger.list()]
    with ledger.db() as db:
        gone = db.execute('SELECT name, domain, state FROM jobs WHERE id=?', (row['id'],)).fetchone()
        assert gone['state'] == 'deleted' and gone['name'].startswith(DATA['name'] + '~deleted-')
        assert not db.execute('SELECT 1 FROM site_domains WHERE site_id=?', (row['id'],)).fetchone()
    # The name and hostname are free again.
    again = pd.submit(ledger, host, str(uuid.uuid4()), DATA, 'b' * 64)
    assert again['name'] == DATA['name']


@pytest.fixture
def managed(tmp_path, monkeypatch, context):
    """A published managed PHP site with a ready MariaDB, a schedule export and owner notes."""
    ledger, host, _, volumes, dumps = context
    from reeve import database_site, recovery_context, schedules
    monkeypatch.setattr(database_site, 'STATE', tmp_path / 'dbstate'); database_site.STATE.mkdir()
    monkeypatch.setattr(database_site, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(recovery_context, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(recovery_context, 'regular', lambda path: Path(path).read_bytes())
    monkeypatch.setattr('reeve.requests_site.SITES', sb.SITES); monkeypatch.setattr('reeve.php_settings.SITES', sb.SITES); monkeypatch.setattr('reeve.php_settings.trusted', lambda *a, **k: None)
    monkeypatch.setattr('reeve.sftp.ROOT', sb.SITES.parent / 'sftp'); monkeypatch.setattr('reeve.sftp.trusted', lambda *a, **k: None)
    monkeypatch.setattr('reeve.mail.SITES', sb.SITES); monkeypatch.setattr('reeve.mail.trusted', lambda *a, **k: None)
    monkeypatch.setattr(sb.os, 'chown', lambda *a, **k: None)  # Site identities are not real users locally.
    monkeypatch.setattr(recovery_context, 'STORE', tmp_path / 'notes'); recovery_context.STORE.mkdir()
    monkeypatch.setattr(sb, 'OPS', tmp_path / 'ops'); (tmp_path / 'ops/panel/worker/schedules').mkdir(parents=True)
    monkeypatch.setattr('reeve.versions.require', lambda branch: branch)
    row = ledger.submit(str(uuid.uuid4()), {'name': 'shop', 'domain': 'shop.example.com', 'runtime': 'php', 'php_version': '8.4',
                                             'database': {'engine': 'mariadb', 'series': '12.3'}})
    ledger.update(row['id'], 'succeeded', 'published'); row = ledger.get(row['id'])
    root = sb.SITES / 'shop'
    for folder in ('html/wp-content', 'database/data', 'database/conf', 'conf', 'volumes', '.tools', 'logs'): (root / folder).mkdir(parents=True)
    (root / 'html/index.php').write_text('<?php echo 1;'); (root / 'html/wp-content/upload.jpg').write_bytes(b'jpeg')
    (root / 'database/data/ibdata1').write_bytes(b'live database pages'); (root / '.tools/tmp').write_text('scratch')
    (root / 'conf/site.nginx.conf').write_text('# custom rules\n'); (root / '.env').write_text('APP=1\n')
    (root / 'hosting.yaml').write_text('web_settings: {profile: wordpress}\nruntime: php\nphp_settings: {max_execution_time: 120, upload_max_filesize_mb: 64, post_max_size_mb: 72, max_input_vars: 3000, memory_limit_mb: 256}\nmail_senders: [owner@example.net]\n')
    info = {'schema': 1, 'operation_id': row['id'], 'spec': {'engine': 'mariadb', 'series': '12.3'}, 'engine': 'mariadb', 'version': '12.3.3',
            'series': '12.3', 'image': 'mariadb@sha256:abc', 'image_id': 'sha256:db', 'admin_password': 'adminpw', 'app_password': 'apppw',
            'stage': 'ready', 'uid': 999, 'gid': 999, 'mount': '/var/lib/mysql'}
    (database_site.STATE / (row['id'] + '.json')).write_text(json.dumps(info))
    (tmp_path / 'ops/panel/worker/schedules' / (row['id'] + '.json')).write_text(json.dumps([{'name': 'wordpress', 'interval': 1, 'enabled': 1,
        'settings': {'tool': 'wp', 'arguments': 'cron event run --due-now', 'path': '.', 'internet': True}}]))
    (recovery_context.STORE / (row['id'] + '.json')).write_text(json.dumps({'schema': 1, 'site_id': row['id'], 'revision': 'r1', 'external': 'no', 'notes': '', 'checks': 'open the shop', 'updated': 1.0}))
    with ledger.db() as db: db.execute('INSERT INTO site_runtime VALUES (?,?)', (row['id'], '8.4'))
    def dump_managed(host, row, job):
        from reeve import database_backup as db
        folder = db.artifact_path(job['id']); folder.mkdir(parents=True)
        (folder / 'database.sql').write_text('-- managed dump'); (folder / 'manifest.json').write_text('{}')
        return {'file': 'database.sql', 'bytes': 15, 'sha256': sb.checksum(folder / 'database.sql'), 'engine': 'mariadb', 'database': 'site', 'consistency': 'global read lock during dump'}
    monkeypatch.setattr('reeve.database_backup.dump', dump_managed)
    return ledger, host, row


def test_managed_capture_excludes_live_database_pages_and_records_private_state(managed):
    ledger, host, row = managed
    job = ledger.submit_site_backup(str(uuid.uuid4()), row['id'])
    manifest = sb.perform(ledger, host, job)
    assert manifest and manifest['site_kind'] == 'managed' and manifest['coverage'] == 'complete'
    assert manifest['managed']['php_branch'] == '8.4' and manifest['managed']['web_settings'] == {'profile': 'wordpress'}
    assert manifest['managed']['database']['image_id'] == 'sha256:db' and manifest['managed']['recorded'] == {'database': True, 'schedules': True, 'notes': True}
    assert manifest['dumps']['database']['engine'] == 'mariadb'
    root = sb.artifact_path(job['id'])
    with tarfile.open(root / 'files.tar') as files: names = files.getnames()
    assert './html/wp-content/upload.jpg' in names and './conf/site.nginx.conf' in names and './database/conf' in names
    assert not any(n.startswith('./database/data') or n.startswith('./.tools') for n in names)
    assert json.loads((root / 'config/database.json').read_text())['app_password'] == 'apppw'
    assert (root / 'config/database.json').stat().st_mode & 0o777 == 0o600
    assert sb.status(ledger, row)['manifest']['site_kind'] == 'managed'


def test_managed_restore_creates_then_refills_a_new_site(managed, monkeypatch):
    ledger, host, row = managed
    from reeve import database_site, recovery_context
    from reeve import sftp
    sftp.save_registry({row['id']: {'name': 'shop', 'uid': row['uid'], 'generated': 'ssh-ed25519 AAAAgen', 'secondary': ['ssh-ed25519 AAAAtest'], 'keys': ['ssh-ed25519 AAAAgen', 'ssh-ed25519 AAAAtest']}})
    monkeypatch.setattr(sftp, 'secondary_keys', lambda row: ['ssh-ed25519 AAAAtest'])
    monkeypatch.setattr(sftp, 'key_fingerprint', lambda public: '256 SHA256:x (ED25519)')
    job = ledger.submit_site_backup(str(uuid.uuid4()), row['id']); sb.perform(ledger, host, job)
    monkeypatch.setattr(sb, 'command', lambda args, timeout=120: 'present' if args[:3] == ['docker', 'image', 'ls'] else '')
    new = sb.restore(ledger, host, job['id'], 'shop-copy', 'copy.example.com')
    payload = json.loads(new['payload'])
    assert payload['runtime'] == 'php' and payload['php_version'] == '8.4' and payload['database'] == {'engine': 'mariadb', 'series': '12.3', 'usage': 'standard'} and 'aliases' not in payload
    pre = json.loads((database_site.STATE / (new['id'] + '.json')).read_text())
    assert pre['app_password'] == 'apppw' and pre['operation_id'] == new['id'] and pre['stage'] == 'resolved' and pre['image_id'] == 'sha256:db'
    pending = ledger.site_restores(new['id'])[0]
    assert pending['state'] == 'queued' and pending['snapshot'] == job['id']
    # Nothing happens until Create has succeeded; a failed Create fails the restore.
    sb.perform_restore(ledger, host, pending); assert ledger.site_restores(new['id'])[0]['state'] == 'queued'
    # Simulate the completed Create: the folder, identity and an empty database.
    site = sb.SITES / 'shop-copy'; (site / 'html').mkdir(parents=True); (site / 'conf').mkdir(); (site / 'html/index.php').write_text('placeholder'); (site / 'html/index.html').write_text('placeholder')
    ledger.update(new['id'], 'succeeded', 'published')
    restored = []
    monkeypatch.setattr('reeve.database_backup.restore_managed', lambda host, row, path, dump, ident: restored.append((row['name'], Path(path).name, dump['engine'])))
    applied = []
    monkeypatch.setattr('reeve.requests_site.apply', lambda host, row, profile, **k: applied.append(profile))
    monkeypatch.setattr('reeve.schedules.export', lambda ledger, site_id: None)
    monkeypatch.setattr('reeve.php_settings.apply', lambda host, row, data, ident=None: applied.append(('php', row['name'], data)))
    monkeypatch.setattr('reeve.sftp.save_secondary', lambda row, keys: applied.append(('sftp', row['name'], list(keys))))
    monkeypatch.setattr('reeve.mail.apply_senders', lambda host, row, data, ident=None: applied.append(('mail', row['name'], data)))
    host.verify_domains = lambda domains: applied.append(('verified', domains))
    sb.perform_restore(ledger, host, ledger.site_restores(new['id'])[0])
    result = ledger.site_restores(new['id'])[0]
    assert result['state'] == 'succeeded', result['error']
    # The backup carried the site's PHP limits and the new site received them after its routing profile.
    limits = {'max_execution_time': 120, 'upload_max_filesize_mb': 64, 'post_max_size_mb': 72, 'max_input_vars': 3000, 'memory_limit_mb': 256}
    assert json.loads(next(b for b in ledger.site_backups(row['id']) if b['id'] == job['id'])['manifest'])['managed']['php_settings'] == limits
    assert applied.index(('php', 'shop-copy', limits)) > applied.index('wordpress')
    assert json.loads(next(b for b in ledger.site_backups(row['id']) if b['id'] == job['id'])['manifest'])['managed']['sftp_access'] == {'secondary': ['ssh-ed25519 AAAAtest']}
    assert ('sftp', 'shop-copy', ['ssh-ed25519 AAAAtest']) in applied  # keys back, access not turned on
    assert json.loads(next(b for b in ledger.site_backups(row['id']) if b['id'] == job['id'])['manifest'])['managed']['mail_senders'] == ['owner@example.net']
    assert ('mail', 'shop-copy', {'senders': ['owner@example.net']}) in applied
    assert (site / 'html/wp-content/upload.jpg').read_bytes() == b'jpeg' and (site / 'conf/site.nginx.conf').read_text() == '# custom rules\n'
    # Create's placeholder pages are gone: the new site's html/ is exactly the backup's (a real site serves index.htm).
    assert not (site / 'html/index.html').exists() and (site / 'html/index.php').read_text() != 'placeholder'
    assert (site / '.env').read_text() == 'APP=1\n' and not (site / 'database/data').exists()
    assert restored == [('shop-copy', 'database.sql', 'mariadb')] and 'wordpress' in applied and ('verified', ['copy.example.com']) in applied
    from reeve.schedules import list_schedules
    assert [s['name'] for s in list_schedules(ledger, new['id'])] == ['wordpress']
    assert recovery_context.read(ledger.get(new['id']))['checks'] == 'open the shop'


def test_nightly_schedule_and_retention(managed, monkeypatch):
    ledger, host, row = managed
    monkeypatch.setattr(sb, 'policy', lambda: {'hour': 3})
    monkeypatch.setattr('reeve.retention.policy', lambda document=None: {'database_days': 2, 'local': {'daily': 1, 'weekly': 0, 'monthly': 0}, 'remote': {'daily': 1, 'weekly': 0, 'monthly': 0}})
    monkeypatch.setattr('reeve.remote_backup.settings', lambda **k: None)
    import datetime
    noon = datetime.datetime(2026, 9, 16, 12, 0).timestamp()
    sb.tick(ledger, now=noon)
    schedule = sb.status(ledger, row)['schedule']
    assert datetime.datetime.fromtimestamp(schedule['next_run']) == datetime.datetime(2026, 9, 17, 3, 0)
    assert not ledger.site_backups(row['id'])
    sb.tick(ledger, now=schedule['next_run'] + 60)
    queued = ledger.site_backups(row['id'])
    assert len(queued) == 1 and queued[0]['kind'] == 'scheduled' and queued[0]['state'] == 'queued'
    assert sb.status(ledger, row)['schedule']['next_run'] > schedule['next_run']
    sb.perform(ledger, host, queued[0])
    for _ in range(3):
        sb.perform(ledger, host, ledger.submit_site_backup(str(uuid.uuid4()), row['id']))
    with ledger.db() as db:
        db.execute("INSERT INTO site_backups (id, site_id, kind, state, step, error, manifest, created, updated) VALUES (?,?,'final','succeeded','','',?,?,?)", (str(uuid.uuid4()), row['id'], json.dumps({'files': {'bytes': 1}, 'dumps': {}, 'volumes': {}, 'completed_at': 1.0}), 0, 0))
    sb.prune(ledger, now=noon + 86400 * 30)
    states = [(j['kind'], j['state']) for j in ledger.site_backups(row['id'])]
    # All four same-day backups fall in one daily bucket: one survivor, the final is untouched.
    assert states.count(('manual', 'succeeded')) + states.count(('scheduled', 'succeeded')) == 1
    assert ('final', 'succeeded') in states and sum(1 for k, s in states if s == 'pruned') == 3
    pruned = next(j for j in ledger.site_backups(row['id']) if j['state'] == 'pruned')
    assert not sb.artifact_path(pruned['id']).exists() and len(sb.status(ledger, row)['snapshots']) == 1
    assert 'complete site backups' in sb.status(ledger, row)['retention_text']


def test_managed_delete_removes_containers_networks_image_and_folder_after_final_backup(managed, monkeypatch):
    ledger, host, row = managed
    removed = []
    def fake_command(args, timeout=120):
        removed.append(args)
        if args[:3] == ['docker', 'ps', '-aq']: return 'c1\n'
        if args[:3] == ['docker', 'network', 'ls']: return 'net\n'
        if args[:3] == ['docker', 'image', 'ls']: return 'img\n'
        return ''
    monkeypatch.setattr(sb, 'command', fake_command)
    host.unpublish = lambda row: removed.append(['unpublish'])
    (sb.SITES / 'shop/compose.yml').write_text('x'); (sb.SITES / 'shop/database/compose.yml').write_text('x')
    job = ledger.submit_site_delete(str(uuid.uuid4()), row['id'])
    sb.perform_delete(ledger, host, job)
    current = ledger.site_deletes(row['id'])[0]
    assert current['state'] == 'succeeded', current['error']
    flat = [' '.join(map(str, r)) for r in removed]
    assert any('compose -f' in f and 'database/compose.yml down' in f for f in flat) and any('docker rm --force c1' in f for f in flat)
    assert any('network rm hosting-ingress-shop' in f for f in flat) and any('network rm hosting-backend-shop' in f for f in flat)
    assert any('image rm hosting-php-site:' + row['id'] in f for f in flat) and any('xfs_quota' in f for f in flat)
    assert not (sb.SITES / 'shop').exists() and row['id'] not in [r['id'] for r in ledger.list()]
    with ledger.db() as db:
        assert not db.execute('SELECT 1 FROM site_runtime WHERE site_id=?', (row['id'],)).fetchone()
        assert not db.execute('SELECT 1 FROM schedules WHERE site_id=?', (row['id'],)).fetchone()


def test_listing_export_import_and_history(context, managed, monkeypatch, tmp_path):
    ledger, host, row = managed
    _, _, package_row, volumes, dumps = context
    monkeypatch.setattr(sb, 'EXPORTS', tmp_path / 'web/downloads'); monkeypatch.setattr(sb, 'UPLOADS', tmp_path / 'web/backup-uploads')
    sb.UPLOADS.mkdir(parents=True)
    import pwd, os as _os
    me = pwd.getpwuid(_os.getuid())
    monkeypatch.setattr(sb, 'web_identity', lambda: (me.pw_uid, me.pw_gid))
    monkeypatch.setattr('pwd.getpwnam', lambda name: me)
    job = ledger.submit_site_backup(str(uuid.uuid4()), row['id']); assert sb.perform(ledger, host, job)
    items = sb.listing(ledger, row)
    assert items[0]['id'] == job['id'] and items[0]['available'] and items[0]['path'] == str(sb.artifact_path(job['id']))
    assert items[0]['dumps'][0]['engine'] == 'mariadb' and items[0]['files']['excluded'] == ['./database/data', './.tools']
    result = sb.export(job['id'])
    names = {f['name'] for f in result['files']}
    assert {'files.tar', 'manifest.json', 'snapshot.tar', 'dumps-database-database.sql'} <= names
    folder = sb.EXPORTS / result['token']
    assert (folder / 'snapshot.tar').stat().st_mode & 0o777 == 0o600
    # Import the exported snapshot into another managed site: same kind, becomes its own backup entry.
    other = ledger.submit(str(uuid.uuid4()), {'name': 'shop-two', 'domain': 'two.example.com', 'runtime': 'php', 'php_version': '8.4'})
    ledger.update(other['id'], 'succeeded', 'published'); other = ledger.get(other['id'])
    token = str(uuid.uuid4())
    Path(sb.UPLOADS / (token + '.files')).write_bytes((folder / 'snapshot.tar').read_bytes())
    imported = sb.import_backup(ledger, host, other['id'], token, 'snapshot', {'files': 'snapshot.tar'})
    assert imported['kind'] == 'imported' and imported['state'] == 'succeeded'
    manifest = json.loads(imported['manifest'])
    assert manifest['site_id'] == other['id'] and manifest['imported']['original_site_name'] == 'shop' and manifest['imported']['original_operation'] == job['id']
    assert not (sb.UPLOADS / (token + '.files')).exists()
    entry = next(i for i in sb.listing(ledger, other) if i['id'] == imported['id'])
    assert entry['source_site'] == 'shop-two' and entry['imported']['original_site_name'] == 'shop'
    with pytest.raises(ValueError, match='Compose package'): sb.import_backup(ledger, host, package_row['id'], token, 'content', {'files': 'x.zip'})
    # Site content plus a gzipped SQL dump from elsewhere becomes a usable backup of a managed site.
    import gzip, io, zipfile
    from reeve import database_site
    (database_site.STATE / (other['id'] + '.json')).write_text(json.dumps({'stage': 'ready', 'engine': 'mariadb', 'version': '12.3.3', 'series': '12.3', 'image': 'i', 'image_id': 'sha256:db', 'spec': {'engine': 'mariadb', 'series': '12.3'}}))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z: z.writestr('index.php', '<?php echo "old";'); z.writestr('wp-content/plugin.php', 'x')
    token = str(uuid.uuid4())
    (sb.UPLOADS / (token + '.files')).write_bytes(buffer.getvalue()); (sb.UPLOADS / (token + '.dump')).write_bytes(gzip.compress(b'-- old dump'))
    old = sb.import_backup(ledger, host, other['id'], token, 'content', {'files': 'site.zip', 'dump': 'site.sql.gz'})
    manifest = json.loads(old['manifest'])
    assert manifest['dumps']['database']['file'] == 'dumps/database/database.sql' and (sb.artifact_path(old['id']) / 'dumps/database/database.sql').read_bytes() == b'-- old dump'
    with tarfile.open(sb.artifact_path(old['id']) / 'files.tar') as files: assert './html/wp-content/plugin.php' in files.getnames()
    with pytest.raises(ValueError, match='cannot restore'): sb.import_backup(ledger, host, other['id'], token, 'content', {'dump': 'x.dump'})
    # A dump taken with --databases from another server selects its own database name: refused before it becomes a backup.
    before = len(ledger.site_backups(other['id'])); foreign = str(uuid.uuid4()); (sb.UPLOADS / (foreign + '.dump')).write_bytes(gzip.compress(b'CREATE DATABASE /*!32312 IF NOT EXISTS*/ `alice_drupal`;\nUSE `alice_drupal`;\n'))
    with pytest.raises(ValueError, match='selects database alice_drupal'): sb.import_backup(ledger, host, other['id'], foreign, 'content', {'dump': 'alice.sql.gz'})
    assert not (sb.UPLOADS / (foreign + '.dump')).exists() and len(ledger.site_backups(other['id'])) == before
    # In-place restores: files from the imported content, then the database from the fresh backup; each takes a safety backup first.
    (sb.SITES / 'shop-two/html').mkdir(parents=True); (sb.SITES / 'shop-two/html/index.php').write_text('hacked')
    (sb.SITES / 'shop-two/conf').mkdir(); (sb.SITES / 'shop-two/database/data').mkdir(parents=True)
    restored = []
    monkeypatch.setattr('reeve.database_backup.restore_managed', lambda host, row, path, dump, ident: restored.append((row['name'], Path(path).read_bytes())))
    host.verify_domains = lambda domains: None
    files_job = sb.restore_into(ledger, host, old['id'], other['id'], 'files')
    with pytest.raises(ValueError): ledger.submit_site_backup(str(uuid.uuid4()), other['id'])  # the site is busy
    sb.perform_restore(ledger, host, files_job)
    done = ledger.site_restores(other['id'])[0]
    assert done['state'] == 'succeeded' and done['scope'] == 'files' and done['safety_backup'], done['error']
    assert (sb.SITES / 'shop-two/html/index.php').read_text() == '<?php echo "old";' and (sb.SITES / 'shop-two/html/wp-content/plugin.php').exists()
    safety = next(b for b in ledger.site_backups(other['id']) if b['id'] == done['safety_backup'])
    assert safety['kind'] == 'pre-restore' and safety['state'] == 'succeeded'
    db_job = sb.restore_into(ledger, host, imported['id'], other['id'], 'database')
    sb.perform_restore(ledger, host, db_job)
    assert ledger.site_restores(other['id'])[0]['state'] == 'succeeded' and restored == [('shop-two', b'-- managed dump')]
    with pytest.raises(ValueError, match='Compose package'): sb.restore_into(ledger, host, old['id'], package_row['id'], 'files')
    with pytest.raises(ValueError, match='files or database'): sb.restore_into(ledger, host, job['id'], other['id'], 'volumes')
    # Deleted sites keep their final backup and appear in the history with a restore source.
    removed = []
    monkeypatch.setattr(sb, 'command', lambda args, timeout=120: removed.append(args) or '')
    host.unpublish = lambda row: None
    delete = ledger.submit_site_delete(str(uuid.uuid4()), row['id']); sb.perform_delete(ledger, host, delete)
    assert ledger.site_deletes(row['id'])[0]['state'] == 'succeeded', ledger.site_deletes(row['id'])[0]['error']
    history = sb.deleted_sites(ledger)
    assert history[0]['name'] == 'shop' and history[0]['domain'] == 'shop.example.com' and history[0]['final_backup']['kind'] == 'final'
    assert history[0]['final_backup']['path'] == str(sb.artifact_path(history[0]['final_backup']['id']))


def test_backup_pages_render_and_downloads_stay_private(tmp_path):
    from reeve.auth import Auth
    from reeve.web import create_app
    from fastapi.testclient import TestClient
    import re
    auth = Auth(tmp_path / 'auth.db'); auth.set_password('strong-test-password-123')
    row = {'id': str(uuid.uuid4()), 'name': 'shop', 'domain': 'shop.example.com', 'state': 'succeeded', 'step': 'published', 'error': '', 'uid': 30001, 'project': 100001,
           'payload': json.dumps({'runtime': 'php', 'php_version': '8.4', 'data_mb': 1024}), 'created': 1.0, 'updated': 2.0, 'health': {'application': 'healthy'}, 'domains': ['shop.example.com']}
    snapshot = str(uuid.uuid4())
    listing = [{'id': snapshot, 'kind': 'manual', 'state': 'succeeded', 'step': '', 'error': '', 'created': 1.0, 'completed_at': 2.0, 'path': '/srv/backups/staging/site/' + snapshot,
                'site_kind': 'managed', 'source_site': 'shop', 'imported': None, 'coverage': 'complete', 'consistency': 'live copy', 'files': {'bytes': 1048576, 'entries': 10, 'sha256': 'x', 'excluded': ['./database/data']},
                'dumps': [{'service': 'database', 'engine': 'mariadb', 'database': 'site', 'bytes': 2048, 'sha256': 'y', 'file': 'dumps/database/database.sql'}], 'volumes': [], 'bytes': 1050624, 'available': True, 'used_by': []}]
    calls = []
    def call(message):
        calls.append(message)
        if message['op'] == 'list': return [row]
        if message['op'] == 'site-backup-list': return listing
        if message['op'] == 'site-backups': return {'supported': True, 'latest': None, 'policy': {'hour': 3, 'keep': 7}, 'snapshots': [], 'available': True}
        if message['op'] == 'site-restores': return []
        if message['op'] == 'site-backup-export': return {'token': message['snapshot'], 'files': []}
        if message['op'] == 'deleted-sites': return [{'id': 'x', 'name': 'gone', 'domain': 'gone.example.com', 'deleted_at': 3.0, 'runtime': 'static', 'backups': 1,
                                                       'final_backup': {'id': snapshot, 'kind': 'final', 'completed_at': 2.0, 'bytes': 4096, 'path': '/srv/backups/staging/site/' + snapshot}}]
        if message['op'] == 'site-restore-into': return {'id': 'r', 'state': 'queued'}
        return []
    client = TestClient(create_app(tmp_path / 'auth.db', call=call))
    assert client.get('/sites/shop/backups', follow_redirects=False).status_code in (303, 401)
    login = client.get('/login'); csrf = re.search('name="csrf" value="([^"]+)"', login.text)[1]
    client.post('/login', data={'csrf': csrf, 'password': 'strong-test-password-123'})
    page = client.get('/sites/shop/backups'); assert page.status_code == 200
    assert 'Local path' in page.text and snapshot in page.text and 'Files into this site' in page.text and 'Database into this site' in page.text and 'Import a backup' in page.text
    csrf = re.search('name="csrf" value="([^"]+)"', page.text)[1]
    assert client.post('/sites/shop/backups/restore-into', data={'csrf': csrf, 'snapshot': snapshot, 'scope': 'files', 'confirm': 'wrong'}).status_code == 200
    assert not any(c['op'] == 'site-restore-into' for c in calls)
    response = client.post('/sites/shop/backups/restore-into', data={'csrf': csrf, 'snapshot': snapshot, 'scope': 'files', 'confirm': 'shop'}, follow_redirects=False)
    assert response.status_code == 303 and any(c['op'] == 'site-restore-into' and c['scope'] == 'files' for c in calls)
    folder = tmp_path / 'downloads' / snapshot; folder.mkdir(parents=True); (folder / 'snapshot.tar').write_bytes(b'tar bytes')
    exported = client.post('/sites/shop/backups/export', data={'csrf': csrf, 'snapshot': snapshot}, follow_redirects=False)
    assert exported.headers['location'].endswith('?export=' + snapshot)
    page = client.get('/sites/shop/backups?export=' + snapshot); assert '/downloads/' + snapshot + '/snapshot.tar' in page.text
    assert client.get('/downloads/' + snapshot + '/snapshot.tar').content == b'tar bytes'
    assert client.get('/downloads/' + snapshot + '/../auth.db').status_code in (404, 400)
    assert client.get('/downloads/not-a-uuid/snapshot.tar').status_code == 404
    history = client.get('/history'); assert history.status_code == 200 and 'gone.example.com' in history.text and 'Restore' in history.text


def test_quiesced_backup_stops_writers_durably_and_resumes_even_on_failure(managed, monkeypatch, tmp_path):
    ledger, host, row = managed
    monkeypatch.setattr(sb, 'STOPS', tmp_path / 'stops')
    calls = []
    monkeypatch.setattr(sb, 'command', lambda args, timeout=120: calls.append(args) or '')
    containers = {'hosting-site-shop': {'Id': 'web1', 'State': {'Running': True}}, 'hosting-php-shop': {'Id': 'php1', 'State': {'Running': False}}}
    host.inspect = lambda name: containers.get(name)
    assert not sb.options(ledger, row)['quiesce']
    assert sb.set_options(ledger, row, True) == {'quiesce': True}
    job = ledger.submit_site_backup(str(uuid.uuid4()), row['id'])
    manifest = sb.perform(ledger, host, job)
    assert manifest['application_consistent'] and '1 stopped' in manifest['consistency']
    stops = [c for c in calls if c[:2] == ['docker', 'stop']]; starts = [c for c in calls if c[:2] == ['docker', 'start']]
    assert stops == [['docker', 'stop', '--timeout', '30', 'web1']] and starts == [['docker', 'start', 'web1']]  # only what was running
    assert calls.index(stops[0]) < calls.index(starts[0]) and not list((tmp_path / 'stops').glob('*.json'))
    # A failure during the copy still resumes the site.
    monkeypatch.setattr(sb, 'archive_tree', lambda *a, **k: (_ for _ in ()).throw(sb.SiteBackupFailed('disk full')))
    calls.clear()
    assert sb.perform(ledger, host, ledger.submit_site_backup(str(uuid.uuid4()), row['id'])) is None
    assert [c for c in calls if c[:2] == ['docker', 'start']] == [['docker', 'start', 'web1']]
    # A worker that died between stop and start leaves a marker; recovery starts the writers first.
    (tmp_path / 'stops').mkdir(exist_ok=True)
    (tmp_path / 'stops' / (str(uuid.uuid4()) + '.json')).write_text(json.dumps({'site_id': row['id'], 'containers': ['web1'], 'started_at': 1.0}))
    calls.clear(); sb.recover(ledger)
    assert ['docker', 'start', 'web1'] in calls and not list((tmp_path / 'stops').glob('*.json'))
    assert sb.status(ledger, row)['options'] == {'quiesce': True}


def test_static_template_carries_site_rules_and_restore_recreates_web(tmp_path, monkeypatch):
    from reeve import host as hm
    config = hm.static_nginx()
    assert 'include /etc/hosting/site.nginx.conf;' in config and config.index('site.nginx.conf') < config.index('location / {')
    assert 'absolute_redirect off;' in config  # a static site's own redirects stay on the public HTTPS name
    # A restored rules file is bind-mounted by inode: the web container must be recreated, not reloaded.
    monkeypatch.setattr(hm, 'trusted', lambda *a, **k: None)
    site = tmp_path / 'site'; (site / 'conf').mkdir(parents=True); (site / 'html').mkdir()
    with tarfile.open(tmp_path / 'files.tar', 'w') as out:
        info = tarfile.TarInfo('./conf/site.nginx.conf'); data = b'location = /old { return 301 /new; }\n'; info.size = len(data)
        out.addfile(info, io.BytesIO(data))
    assert sb.restore_site_files(site, tmp_path / 'files.tar') == ['conf/site.nginx.conf']
    assert (site / 'conf/site.nginx.conf').read_text().startswith('location = /old')
    calls = []
    monkeypatch.setattr(sb, 'command', lambda args, timeout=120: calls.append(args))
    row = {'name': 'pages', 'payload': json.dumps({'runtime': 'static'})}
    sb.refresh_web(row, site); assert calls == []  # no compose file, nothing to recreate
    (site / 'compose.yml').write_text('services: {}')
    sb.refresh_web(row, site)
    assert calls[0][:3] == ['docker', 'compose', '-f'] and '--force-recreate' in calls[0] and calls[0][-1] == 'web'
    assert calls[1] == ['docker', 'update', '--pids-limit', '-1', 'hosting-site-pages']
