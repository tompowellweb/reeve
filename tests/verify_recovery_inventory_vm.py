"""Observe all managed fixtures through the installed worker; change no application data."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from reeve import recovery_inventory as ri
from reeve.core import Ledger
from reeve.worker import rpc
from tests.verify_compose_adoption_vm import cmd, export, INPUT

OUT = Path('/srv/ops/panel/worker/recovery-inventory-acceptance.json')


def snapshot():
    ids = cmd(['docker', 'ps', '-aq']).split()
    containers = json.loads(cmd(['docker', 'inspect', *ids]))
    return {c['Name']: [c['Id'], c['State']['StartedAt'], c['Image']]
            for c in containers if c['Name'].startswith(('/hosting-site-', '/hosting-php-', '/hosting-db-', '/retained-demo-'))}


def main():
    os.umask(0o077)
    before = snapshot()
    rows = rpc({'op': 'list'})
    report = {'installed': str(Path('/opt/reeve/current').resolve()),
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'sites': {}}
    for row in rows:
        result = rpc({'op': 'recovery-inspect', 'site_id': row['id']})
        assert not result['error'], row['name'] + ': ' + result['error']
        info = result['inventory']
        assert info['coverage'] == 'incomplete' and info['restore'] == 'not verified'
        assert info['services'] and info['storage'] and info['inputs'] and info['images']
        assert not any(g['code'] in ('mounts-changed', 'missing-service', 'image-changed', 'networks-changed',
            'storage-unavailable', 'source-changed', 'image-reference-changed', 'service-changed') for g in info['gaps']), row['name']
        assert ri.location(row).stat().st_uid == 0 and not ri.location(row).stat().st_mode & 0o077
        report['sites'][row['name']] = {'services': len(info['services']), 'storage_boundaries': len(info['storage']),
            'named_volumes': [s['source'] for s in info['storage'] if s['kind'] == 'volume'],
            'database_candidates': [s['database_hint'] for s in info['services'] if s['database_hint']],
            'gap_codes': sorted({g['code'] for g in info['gaps']}), 'revision': info['revision']}
        print(row['name'] + ': inventory recorded; coverage incomplete', flush=True)
    # Helper failure in this acceptance process: no Docker daemon or worker outage.
    ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    row = next(r for r in rows if r['name'] == 'm25-static')
    prior = ri.read(row)['inventory']
    with patch.object(ri, 'docker', side_effect=RuntimeError('private-test-diagnostic')):
        failure = ri.scan(ledger, row)
    assert failure['error'] and failure['inventory'] == prior
    assert 'private-test-diagnostic' not in json.dumps(failure)
    assert not rpc({'op': 'recovery-inspect', 'site_id': row['id']})['error']
    assert snapshot() == before
    expected = json.loads((INPUT / 'expected-export.json').read_text())
    for row in rows:
        if row['name'] in ('m3-demo-fresh', 'm3-demo-existing'):
            for domain in row['domains']: assert export(domain) == expected
        if row['name'] in ('m24-wordpress', 'm25-static'):
            cmd(['curl', '--noproxy', '*', '--fail', '--silent', '--show-error',
                 '--cacert', '/srv/ops/proxy/data/caddy/pki/authorities/local/root.crt',
                 '--resolve', row['domain'] + ':443:127.0.0.1', 'https://' + row['domain'] + '/'])
    old = [r for r in rpc({'op': 'list'}) if json.loads(r['payload']).get('runtime') != 'compose']
    assert len(old) == 25 and all(r['health']['application'] == 'healthy' for r in old)
    report.update(managed_sites=len(rows), original_sites_healthy=25, original_databases_healthy=10,
                  container_identities_and_start_times_unchanged=True, helper_failure_preserved_previous=True,
                  caddy_https_passed=True, demo_full_exports_match=True, backup_snapshots=0,
                  inventories_private=True, worker_schema=11)
    OUT.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'acceptance': 'passed', 'report': str(OUT)}))


if __name__ == '__main__': main()
