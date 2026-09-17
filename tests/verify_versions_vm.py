"""Version acceptance; mutations are confined to disposable m2-versions."""
import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path

from reeve.core import Ledger
from reeve.host import Host, SITES, atomic, command
from reeve.php_runtime import catalog
from reeve.php_switch import snapshot_path
from reeve.versions import read, choices
from reeve.worker import rpc
from tests.verify_php_vm import request

RESULT = Path('/srv/ops/panel/worker/versions-acceptance.json')
FILES = ['conf/Containerfile', 'conf/php.ini', 'conf/php-fpm.conf', 'conf/pool.conf', 'conf/site.nginx.conf', '.env', 'html/version-proof.php']


def record(key, value):
    data = json.loads(RESULT.read_text()) if RESULT.exists() else {}
    data[key] = value
    atomic(RESULT, json.dumps(data, indent=2))
    print(key + ': ' + json.dumps(value), flush=True)


def row():
    return next(r for r in rpc({'op': 'list'}) if r['name'] == 'm2-versions')


def hashes():
    return {name: hashlib.sha256((SITES / 'm2-versions' / name).read_bytes()).hexdigest() for name in FILES}


def wait(ident, state):
    ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    for _ in range(1800):
        job = next(j for j in ledger.runtime_jobs() if j['id'] == ident)
        if job['state'] == state:
            return job
        if state == 'succeeded' and job['state'] == 'recovery-needed':
            raise AssertionError(job)
        time.sleep(0.5)
    raise AssertionError(job)


def assert_runtime(branch):
    current = row()
    assert current['php_branch'] == branch and current['health']['php_version'].startswith(branch + '.'), current
    for domain in current['domains']:
        code, body = request(dict(current, domain=domain), '/version-proof.php')
        info = json.loads(body)
        assert code == 200 and info['version'].startswith(branch + '.') and info['uid'] == current['uid'], info
    return current


def prepare():
    current = row()
    assert current['php_branch'] == '8.2'
    root = SITES / current['name']
    for name, text in {'conf/php.ini': '\n; retained version acceptance\nprecision=13\n',
                       'conf/pool.conf': '\n; retained version acceptance\n',
                       '.env': '\nVERSION_ACCEPTANCE=retained\n'}.items():
        with (root / name).open('a') as stream:
            stream.write(text)
    # Same numeric-owner mechanism as uploads; never root-owned content.
    command(['setpriv', f"--reuid={current['uid']}", f"--regid={current['uid']}", '--clear-groups', 'python3', '-c',
        'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])', root / 'html/version-proof.php',
        '<?php header("Content-Type: application/json"); echo json_encode(array("version"=>PHP_VERSION,"uid"=>posix_geteuid()));'])
    record('before', {'site': current, 'hashes': hashes(), 'image': Host().inspect('hosting-php-m2-versions')['Image'],
        'runtimes': {k: v['image_id'] for k, v in catalog().items()}, 'catalogue': read()})


def verify():
    old = json.loads(RESULT.read_text())['before']
    current = assert_runtime('8.2')
    assert hashes() == old['hashes']
    assert current['uid'] == old['site']['uid'] and current['domains'] == old['site']['domains']
    assert Host().inspect('hosting-php-m2-versions')['Image'] == old['image']
    assert command(['docker', 'exec', 'hosting-php-m2-versions', 'php', '-r', 'echo ini_get("precision").":".getenv("VERSION_ACCEPTANCE");']) == '13:retained'
    old_catalog = old['catalogue']
    assert read()['checked_at'] > old_catalog['checked_at']
    assert all(catalog()[branch]['image_id'] == image for branch, image in old['runtimes'].items())
    record('browser_switch_and_rollback', {'site': current, 'hashes_preserved': True, 'image_restored': old['image'],
        'manual_refresh': 'success; existing images unchanged', 'configuration': '13:retained'})


def failure():
    root = SITES / 'm2-versions'
    containerfile = root / 'conf/Containerfile'
    original = containerfile.read_text()
    if 'build_failure_retry' not in json.loads(RESULT.read_text()):
        before_id = Host().inspect('hosting-php-m2-versions')['Id']
        pending = row().get('runtime_job')
        if pending and pending['state'] == 'recovery-needed' and pending['step'] == 'building replacement site image':
            job, failed = pending, pending
            assert_runtime('8.2')
        else:
            try:
                atomic(containerfile, original + '\nRUN false\n', 0o644)
                job = rpc({'op': 'php-switch', 'id': str(uuid.uuid4()), 'site_id': row()['id'], 'branch': '8.3'})
                failed = wait(job['id'], 'recovery-needed')
                assert not snapshot_path(job['id']).exists()
                assert Host().inspect('hosting-php-m2-versions')['Id'] == before_id
                assert_runtime('8.2')
            finally:
                atomic(containerfile, original, 0o644)
        record('build_failure_before_restart', {'job': job['id'], 'step': failed['step'], 'old_container_unchanged': before_id})
        command(['systemctl', 'restart', 'reeve-worker'])
        ready()
        wait(job['id'], 'recovery-needed')
        rpc({'op': 'retry-runtime', 'id': job['id']})
        wait(job['id'], 'succeeded')
        assert_runtime('8.3')
        record('build_failure_retry', {'job': job['id'], 'failure_step': failed['step'], 'old_container_unchanged': before_id,
            'worker_restart': 'saved request retained', 'retry': 'succeeded'})
    # Candidate validation succeeds; its FPM entrypoint then deliberately fails at startup.
    wrapper = '#!/bin/sh\nif [ "$1" = "--test" ]; then exec /usr/local/sbin/php-fpm-real "$@"; fi\nexit 42\n'
    import shlex
    # Dockerfile RUN uses a single logical line; shell printf expands the explicit escapes.
    failing = original + '\nUSER root\nRUN mv /usr/local/sbin/php-fpm /usr/local/sbin/php-fpm-real && printf %b ' + shlex.quote(wrapper.replace('\n', '\\n')) + ' > /usr/local/sbin/php-fpm && chmod 755 /usr/local/sbin/php-fpm\nUSER 65534:65534\n'
    try:
        atomic(containerfile, failing, 0o644)
        pending = row().get('runtime_job')
        if pending and pending['state'] == 'recovery-needed':
            job = rpc({'op': 'retry-runtime', 'id': pending['id']})
        else:
            job = rpc({'op': 'php-switch', 'id': str(uuid.uuid4()), 'site_id': row()['id'], 'branch': '8.2'})
        failed = wait(job['id'], 'recovery-needed')
        assert 'previous runtime restored' in failed['error'], failed
        assert_runtime('8.3')
        assert json.loads(snapshot_path(job['id']).read_text())['rolled_back']
    finally:
        atomic(containerfile, original, 0o644)
    rpc({'op': 'retry-runtime', 'id': job['id']})
    wait(job['id'], 'succeeded')
    final = assert_runtime('8.2')
    assert hashes() == json.loads(RESULT.read_text())['before']['hashes']
    record('deployment_failure_retry', {'job': job['id'], 'failure_step': failed['step'], 'automatic_rollback': '8.3 restored',
        'retry_rebuild': '8.2 succeeded after fixing Containerfile', 'site': final})
    rows = rpc({'op': 'list'})
    assert all(r['state'] == 'succeeded' and r['health']['application'] == 'healthy' for r in rows)
    record('final', {'sites': len(rows), 'all_healthy': True, 'catalogue': choices(),
        'jobs': Ledger('/srv/ops/panel/worker/jobs.sqlite3').runtime_jobs(),
        'release': json.loads(Path('/srv/ops/panel/release.json').read_text())})


def ready():
    for _ in range(100):
        try:
            rpc({'op': 'defaults'})
            return
        except (OSError, ValueError):
            time.sleep(0.1)
    raise AssertionError('Worker socket did not become ready')


def interruption():
    current = assert_runtime('8.2')
    job = rpc({'op': 'php-switch', 'id': str(uuid.uuid4()), 'site_id': current['id'], 'branch': '8.3'})
    ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    for _ in range(12000):
        observed = next(j for j in ledger.runtime_jobs() if j['id'] == job['id'])
        if observed['step'] == 'deploying replacement':
            assert snapshot_path(job['id']).exists()
            command(['systemctl', 'kill', '--signal=KILL', 'reeve-worker'])
            break
        assert observed['state'] not in ('succeeded', 'recovery-needed'), observed
        time.sleep(0.01)
    else:
        raise AssertionError('Did not reach durable cutover intent')
    wait(job['id'], 'recovery-needed')
    ready()
    rpc({'op': 'retry-runtime', 'id': job['id']})
    wait(job['id'], 'succeeded')
    current = assert_runtime('8.3')
    assert hashes() == json.loads(RESULT.read_text())['before']['hashes']
    record('worker_interruption', {'job': job['id'], 'interrupted_step': observed['step'], 'saved_intent': str(snapshot_path(job['id'])),
        'retry': 'succeeded', 'site': current})


def cache_expiry():
    from reeve.versions import CACHE
    images = {k: v['image_id'] for k, v in catalog().items()}
    previous = read()
    old_jobs = {j['id'] for j in Ledger('/srv/ops/panel/worker/jobs.sqlite3').runtime_jobs()}
    aged = dict(previous, checked_at=time.time() - 31 * 86400, attempted_at=time.time() - 31 * 86400)
    command(['systemctl', 'stop', 'reeve-worker'])
    try:
        atomic(CACHE, json.dumps(aged))
    finally:
        command(['systemctl', 'start', 'reeve-worker'])
    ready()
    ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    for _ in range(120):
        new = [j for j in ledger.runtime_jobs() if j['id'] not in old_jobs and j['kind'] == 'refresh']
        if new:
            break
        time.sleep(0.5)
    assert new
    wait(new[0]['id'], 'succeeded')
    assert read()['checked_at'] > previous['checked_at'] and not read()['stale']
    assert images == {k: v['image_id'] for k, v in catalog().items()}
    record('monthly_refresh', {'injected_cache_age_days': 31, 'automatic_job': new[0]['id'], 'result': 'fresh signed catalogue',
        'running_images': 'unchanged', 'catalogue': read()})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['prepare', 'verify', 'failure', 'interruption', 'cache_expiry'])
    args = parser.parse_args()
    globals()[args.mode]()
