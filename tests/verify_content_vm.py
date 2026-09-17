"""Root VM checks using disposable sites; no customer data or credential output."""
import argparse
import json
import time
import uuid
from pathlib import Path

from reeve.core import Ledger
from reeve.host import Host, atomic, command, preflight, quota_record
from reeve.worker import rpc

LEDGER = '/srv/ops/panel/worker/jobs.sqlite3'
REPORT = Path('/srv/ops/panel/worker/content-acceptance.json')


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('mode', choices=('quota', 'interrupt', 'final'))
    args = parser.parse_args()
    ledger = Ledger(LEDGER); host = Host()
    report = json.loads(REPORT.read_text()) if REPORT.exists() else {}
    def row(name): return next(r for r in ledger.list() if r['name'] == name)
    def submit(site, text):
        ident = str(uuid.uuid4())
        rpc({'op': 'content-submit', 'site_id': site['id'], 'id': ident, 'kind': 'tool',
             'data': {'tool': 'shell', 'arguments': text, 'path': '.', 'internet': False}})
        return ident
    def wait(ident, state=None):
        until = time.monotonic() + 120
        while time.monotonic() < until:
            job = next(j for j in ledger.content_jobs() if j['id'] == ident)
            if (state and job['state'] == state) or (not state and job['state'] not in ('queued','running')): return job
            time.sleep(1)
        raise AssertionError('Timed out waiting for ' + ident)
    if args.mode == 'quota':
        site = row('m2-versions')
        # Only this named synthetic filler is changed; the fixture has a 16 MiB site quota.
        ident = submit(site, 'dd if=/dev/zero of=m23-quota-proof.bin bs=1M count=32')
        job = wait(ident); assert job['state'] == 'failed'
        output = rpc({'op': 'content-output', 'id': ident})
        assert 'No space left on device' in output or 'Disk quota exceeded' in output, output
        filled = quota_record(site['project']); assert filled['hard_bytes'] == 16 * 1048576
        neighbour = host.health(row('m2-db-postgres')); assert neighbour['application'] == 'healthy'
        cleanup = submit(site, 'rm -- m23-quota-proof.bin')
        assert wait(cleanup)['state'] == 'succeeded'
        report['quota'] = {'operation': ident, 'result': job['state'], 'filled': filled,
                           'neighbour': neighbour['application'], 'cleanup': cleanup, 'after': quota_record(site['project'])}
    elif args.mode == 'interrupt':
        site = row('m2-unlimited-static')
        for prior in ledger.content_jobs(site['id']):
            if prior['state'] in ('queued', 'running'): wait(prior['id'])
        assert wait(submit(site, 'rm -f -- m23-interrupted.txt'))['state'] == 'succeeded'
        ident = submit(site, 'printf started > m23-interrupted.txt; sleep 120; printf replayed >> m23-interrupted.txt')
        wait(ident, 'running')
        until = time.monotonic() + 30
        while time.monotonic() < until:
            if (Path('/srv/sites') / site['name'] / 'html/m23-interrupted.txt').exists(): break
            time.sleep(1)
        else: raise AssertionError('Command never started')
        live = host.inspect('hosting-tool-' + ident)
        assert live and live['Config']['User'] == str(site['uid']) + ':' + str(site['uid'])
        assert live['HostConfig']['Privileged'] is False and live['HostConfig']['ReadonlyRootfs'] is True
        assert live['HostConfig']['Memory'] == 0 and live['HostConfig']['NanoCpus'] == 0 and live['HostConfig']['PidsLimit'] in (-1, 0)
        assert command(['docker', 'exec', 'hosting-tool-' + ident, 'cat', '/sys/fs/cgroup/pids.max']).strip() == 'max'
        command(['systemctl', 'kill', '-s', 'SIGKILL', '--kill-whom=main', 'reeve-worker'])
        time.sleep(5)
        job = wait(ident); assert job['state'] == 'recovery-needed'
        assert not host.inspect('hosting-tool-' + ident)
        content = (Path('/srv/sites') / site['name'] / 'html/m23-interrupted.txt').read_text()
        assert content == 'started'
        rpc({'op': 'content-resolve', 'id': ident})
        result = wait(ident); assert result['state'] == 'failed'
        report['interruption'] = {'operation': ident, 'interrupted_state': job['state'], 'resolved_state': result['state'],
            'replayed': False, 'container_removed': True, 'uid': live['Config']['User'], 'memory': 0, 'cpu': 0, 'pids': 'max',
            'mounts': [{'source': m['Source'], 'destination': m['Destination'], 'rw': m['RW']} for m in live['Mounts']]}
    else:
        sites = rpc({'op': 'list'})
        assert len(sites) == 19 and all(s['health']['application'] == 'healthy' for s in sites)
        jobs = ledger.content_jobs()
        assert not any(j['state'] in ('queued','running','recovery-needed') for j in jobs)
        rescue = Path('/srv/ops/panel/worker/content-rescue')
        assert not rescue.exists() or not list(rescue.iterdir())
        assert not command(['docker', 'ps', '-aq', '--filter', 'label=hosting.content']).strip()
        assert not command(['docker', 'network', 'ls', '-q', '--filter', 'label=hosting.content']).strip()
        spool = Path('/srv/ops/panel/web/uploads')
        assert not [p for p in spool.iterdir() if p.name != 'lock']
        for s in sites:
            work = Path('/srv/sites') / s['name'] / '.tools'
            assert not work.exists() or not list(work.iterdir())
        from tests.verify_databases_vm import info
        retained_databases = {s['name']: info(s['name']) for s in sites if s['database']}
        file_owners = {}
        for s in sites:
            content = Path('/srv/sites') / s['name'] / 'html/m23-demo'
            if content.exists():
                owners = {p.lstat().st_uid for p in content.rglob('*')}
                assert owners == {s['uid']}, (s['name'], owners)
                file_owners[s['name']] = s['uid']
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        report['final'] = {'boot_id': boot, 'installed': str(Path('/opt/reeve/current').resolve()),
            'preflight': preflight(), 'sites': [{'name': s['name'], 'health': s['health']['application']} for s in sites],
            'content_jobs': [{k:j[k] for k in ('id','site_id','kind','state','step','error')} for j in jobs],
            'retained_databases': retained_databases, 'content_file_owners': file_owners,
            'temporary_containers': 0, 'temporary_networks': 0, 'staged_uploads': 0}
    atomic(REPORT, json.dumps(report, indent=2))
    print(json.dumps({args.mode: 'passed', 'evidence': str(REPORT)}))


if __name__ == '__main__': main()
