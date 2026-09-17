"""Real restic/SFTP protocol acceptance on an isolated loopback SSH server.

This deliberately does not claim an off-machine destination. No production keys or
destination configuration are used. Private fixture dumps/repository are retained.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from reeve import remote_backup as remote, database_backup as local
from reeve.core import Ledger


def run():
    assert os.getuid() == 0
    os.umask(0o077)
    base = Path('/srv/ops/panel/worker/remote-copy-acceptance')
    base.mkdir(exist_ok=True, mode=0o700)
    root = base / uuid.uuid4().hex; root.mkdir(mode=0o700)
    report = {'installed': str(Path('/opt/reeve/current').resolve()), 'fixture': str(root),
              'transport': 'real restic 0.18 SFTP through isolated loopback sshd', 'off_machine': False,
              'amazon_s3_live_test': False, 'artifacts': {}}
    for key in ('host_key', 'client_key'):
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(root / key)], check=True, stdout=subprocess.DEVNULL)
    shutil.copyfile(root / 'client_key.pub', root / 'authorized_keys')
    (root / 'known_hosts').write_text('[127.0.0.1]:22223 ' + (root / 'host_key.pub').read_text())
    (root / 'password').write_text(uuid.uuid4().hex + uuid.uuid4().hex)
    (root / 'sshd_config').write_text(f'''Port 22223
ListenAddress 127.0.0.1
HostKey {root}/host_key
PidFile {root}/sshd.pid
AuthorizedKeysFile {root}/authorized_keys
StrictModes yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin prohibit-password
AllowUsers root
ForceCommand internal-sftp
DisableForwarding yes
Subsystem sftp internal-sftp
''')
    config = {'type': 'sftp', 'repository': f'sftp://root@127.0.0.1:22223/{root}/repo',
              'password_file': str(root / 'password'), 'ssh_key_file': str(root / 'client_key'),
              'known_hosts_file': str(root / 'known_hosts'), 'timeout_seconds': 10,
              'repository_id': 'a' * 64, 'prune_local_after_days': 0, 'enabled': True}
    remote.CONFIG = root / 'remote.json'; remote.CONFIG.write_text(json.dumps(config))
    fixture = Ledger(root / 'jobs.sqlite3', sites=root / 'sites')
    local.STAGING = root / 'staging'; local.STAGING.mkdir(mode=0o700)
    with sqlite3.connect('file:/srv/ops/panel/worker/jobs.sqlite3?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        for name in ('m25-legacy', 'm25-mysql', 'm25-mariadb', 'm25-postgres'):
            source = dict(db.execute('''SELECT b.* FROM backup_jobs b JOIN jobs s ON s.id=b.site_id
                WHERE s.name=? AND b.state='succeeded' AND b.cleanup_error='' ORDER BY b.created DESC LIMIT 1''', (name,)).fetchone())
            manifest = json.loads(source['artifact']); source_root = Path('/srv/backups/staging/db') / source['id']
            target = local.artifact_path(source['id']); shutil.copytree(source_root, target)
            assert local.completed(source) == manifest
            with fixture.db() as fd:
                fd.execute('INSERT INTO backup_jobs VALUES (?,?,?,?,?,?,?,?)', tuple(source[k] for k in ('id','site_id','state','error','artifact','cleanup_error','created','updated')))
            report['artifacts'][name] = {'job_id': source['id'], 'bytes': manifest['bytes'], 'sha256': manifest['sha256']}
    # Connection failure is a recorded upload outcome; every local artifact remains.
    remote.cycle(fixture, remote.settings(), force=True)
    with fixture.db() as db: assert db.execute('SELECT state FROM remote_cycles').fetchone()[0] == 'failed'
    assert len(remote.pending(fixture, remote.settings()['destination'])) == 4
    report['outage_retained_all'] = True
    with (root / 'sshd.log').open('wb') as log:
        server = subprocess.Popen(['/usr/sbin/sshd', '-D', '-e', '-f', str(root / 'sshd_config')], stdout=log, stderr=log)
        try:
            time.sleep(0.3); assert server.poll() is None
            remote.execute(remote.settings(require_id=False), ['init'])
            config['repository_id'] = json.loads(remote.execute(remote.settings(require_id=False), ['cat', 'config']))['id']
            remote.CONFIG.write_text(json.dumps(config)); config = remote.settings()
            remote.cycle(fixture, config, force=True)
            assert not remote.pending(fixture, config['destination']), 'Inspect private fixture ledger for sanitized failure'
            with fixture.db() as db:
                receipts = [dict(r) for r in db.execute('SELECT * FROM remote_copies WHERE destination=?', (config['destination'],))]
                assert len(receipts) == 4 and all(r['verified'] for r in receipts)
            report['repository_id'] = config['repository_id']
            report['snapshots'] = {r['job_id']: r['snapshot'] for r in receipts}
            # Lost receipt after publication: discover exact tagged snapshot, verify, do not duplicate.
            with fixture.db() as db: db.execute('UPDATE remote_copies SET verified=0 WHERE destination=?', (config['destination'],))
            remote.cycle(fixture, config, force=True)
            snapshots = json.loads(remote.execute(config, ['snapshots', '--json']))
            assert len(snapshots) == 4 and not remote.pending(fixture, config['destination'])
            report['receipt_recovery_no_duplicate'] = True
            # Replace pinned SSH host key with a different generated public key; fail closed.
            known = (root / 'known_hosts').read_text()
            (root / 'known_hosts').write_text('[127.0.0.1]:22223 ' + (root / 'client_key.pub').read_text())
            try:
                remote.repository(config)
                raise AssertionError('Wrong SSH host key was accepted')
            except remote.RemoteFailed: report['wrong_host_key_rejected'] = True
            finally: (root / 'known_hosts').write_text(known)
            # Independent retrieval into a new empty directory, using the installed restic binary.
            restore = root / 'retrieved'; restore.mkdir(mode=0o700)
            for receipt in receipts:
                target = restore / receipt['job_id']; target.mkdir(mode=0o700)
                remote.execute(config, ['restore', receipt['snapshot'], '--target', str(target), '--verify'])
                original = local.artifact_path(receipt['job_id'])
                returned = target / str(original).lstrip('/')
                assert (returned / 'manifest.json').read_bytes() == (original / 'manifest.json').read_bytes()
                manifest = json.loads((returned / 'manifest.json').read_text())
                assert local.checksum(returned / manifest['file']) == manifest['sha256']
            report['four_retrieved_files_match'] = True
            # Complete site backups travel too, under the same retention as the local copies.
            from reeve import site_backup as sites
            sites.STAGING = root / 'site-staging'; sites.STAGING.mkdir(mode=0o700)
            with sqlite3.connect('file:/srv/ops/panel/worker/jobs.sqlite3?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                live = dict(db.execute('''SELECT s.* FROM site_backups s JOIN jobs j ON j.id=s.site_id
                    WHERE j.name='m25-mysql' AND s.state='succeeded' AND s.kind='manual' ORDER BY s.created DESC LIMIT 1''').fetchone())
            shutil.copytree(Path('/srv/backups/staging/site') / live['id'], sites.artifact_path(live['id']))
            with fixture.db() as fd:
                fd.execute('INSERT INTO site_backups (id, site_id, kind, state, step, error, manifest, created, updated) VALUES (?,?,?,?,?,?,?,?,?)',
                           tuple(live[k] for k in ('id', 'site_id', 'kind', 'state', 'step', 'error', 'manifest', 'created', 'updated')))
            remote.cycle(fixture, config, force=True)
            with fixture.db() as db:
                site_receipt = dict(db.execute('SELECT * FROM remote_copies WHERE job_id=?', (live['id'],)).fetchone())
            assert site_receipt['verified'] and not remote.pending_sites(fixture, config['destination'])
            report['site_snapshot'] = {'backup_id': live['id'], 'snapshot': site_receipt['snapshot'], 'bytes': json.loads(live['manifest'])['files']['bytes']}
            # Retrieve the whole site backup folder independently and check the files archive.
            target = root / 'retrieved-site'; target.mkdir(mode=0o700)
            remote.execute(config, ['restore', site_receipt['snapshot'], '--target', str(target), '--verify'], maximum=64 * 1024**2)
            returned = target / str(sites.artifact_path(live['id'])).lstrip('/')
            manifest = json.loads((returned / 'manifest.json').read_text())
            assert local.checksum(returned / manifest['files']['file']) == manifest['files']['sha256']
            report['site_snapshot']['retrieved_files_match'] = True
            # External retention: an older duplicate dump of one site is forgotten remotely; the newest and the site backup stay.
            old_job = next(iter(receipts))['job_id']
            aged = str(uuid.uuid4()); shutil.copytree(local.artifact_path(old_job), local.artifact_path(aged))
            aged_manifest = json.loads((local.artifact_path(aged) / 'manifest.json').read_text()); aged_manifest['operation'] = aged
            (local.artifact_path(aged) / 'manifest.json').write_text(json.dumps(aged_manifest))
            with fixture.db() as fd:
                site_of = fd.execute('SELECT site_id FROM backup_jobs WHERE id=?', (old_job,)).fetchone()[0]
                fd.execute("INSERT INTO backup_jobs VALUES (?,?,'succeeded','',?,'',?,?)", (aged, site_of, json.dumps(aged_manifest), time.time() - 5 * 86400, time.time() - 5 * 86400))
            remote.cycle(fixture, config, force=True)
            with fixture.db() as db:
                aged_receipt = dict(db.execute('SELECT * FROM remote_copies WHERE job_id=?', (aged,)).fetchone())
                pruned_at = db.execute('SELECT pruned_at FROM remote_cycles WHERE destination=?', (config['destination'],)).fetchone()[0]
                untouched = [dict(r) for r in db.execute('SELECT job_id, forgotten FROM remote_copies WHERE job_id!=?', (aged,))]
            assert aged_receipt['verified'] and aged_receipt['forgotten'] and pruned_at > 0
            assert all(not r['forgotten'] for r in untouched)
            remaining = json.loads(remote.execute(config, ['snapshots', '--json']))
            assert len(remaining) == 5 and all('hosting-db:' + aged not in s.get('tags', []) for s in remaining)
            report['remote_retention'] = {'forgotten_dump': aged, 'age_days': 5, 'remaining_snapshots': len(remaining), 'repository_pruned': True,
                                          'policy': json.loads(json.dumps(__import__('reeve.retention', fromlist=['policy']).policy()))}
        finally:
            server.terminate(); server.wait(timeout=10)
    report['sshd_stopped'] = True
    report['local_live_artifacts_untouched'] = True
    (base / 'latest.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__': run()
