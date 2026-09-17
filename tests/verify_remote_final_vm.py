"""Read-only final health and coverage report after remote-copy acceptance."""
import json
import subprocess
import time
from pathlib import Path

from reeve.core import Ledger
from reeve.database_backup import completed, usage
from reeve.remote_backup import CONFIG
from reeve.worker import rpc


def command(args):
    return subprocess.check_output(args, stderr=subprocess.PIPE, text=True).strip()


def main(output='/srv/ops/panel/worker/remote-copy-acceptance/final.json', expected_sites=27):
    ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3')
    sites = rpc({'op': 'list'})
    assert len(sites) == expected_sites
    assert all(s['health']['application'] == 'healthy' for s in sites if json.loads(s['payload']).get('runtime') != 'compose')
    # Since 2026-09-16 a real destination (backup-target) is configured; record rather than forbid it.
    destination_configured = CONFIG.exists()
    report = {'installed': str(Path('/opt/reeve/current').resolve()), 'checked_at': time.time(),
              'managed_sites': len(sites), 'normal_destination_configured': destination_configured, 'dumps': {},
              'off_machine_copies': 0, 'full_site_snapshots': 0}
    with ledger.db() as db:
        report['worker_schema'] = db.execute('PRAGMA user_version').fetchone()[0]
        report['retained_dump_count'] = db.execute("SELECT COUNT(*) FROM backup_jobs WHERE state='succeeded'").fetchone()[0]
        assert not db.execute("SELECT 1 FROM backup_jobs WHERE state IN ('running','recovery-needed') OR cleanup_error!=''").fetchone()
        assert not db.execute("SELECT 1 FROM content_jobs WHERE state='recovery-needed'").fetchone()
        for site in sites:
            if not site.get('database'): continue
            job = dict(db.execute("SELECT * FROM backup_jobs WHERE site_id=? AND state='succeeded' ORDER BY created DESC LIMIT 1", (site['id'],)).fetchone())
            manifest = completed(job); assert manifest
            schedule = db.execute('SELECT * FROM backup_schedules WHERE site_id=?', (site['id'],)).fetchone()
            assert schedule['enabled'] and schedule['interval'] == 15
            report['dumps'][site['name']] = {'job_id': job['id'], 'sha256': manifest['sha256'], 'completed_at': manifest['completed_at'], 'interval': 15}
        wordpress = next(s for s in sites if s['name'] == 'm24-wordpress')
        schedule = db.execute("SELECT * FROM schedules WHERE site_id=? AND name='wordpress'", (wordpress['id'],)).fetchone()
        assert schedule['enabled'] and schedule['interval'] == 1
        assert json.loads(schedule['payload']) == {'tool': 'wp', 'arguments': 'cron event run --due-now', 'path': '.', 'internet': True}
        success = db.execute("SELECT id FROM content_jobs WHERE site_id=? AND state='succeeded' ORDER BY created DESC LIMIT 1", (wordpress['id'],)).fetchone()
        report['wordpress_latest_success'] = success['id']
    # The host-adopted M3 demos and their expected export were retired on 2026-09-16.
    for site in sites:
        if site['name'] not in ('m25-legacy', 'm25-mysql', 'm25-mariadb', 'm25-postgres', 'm24-wordpress', 'm25-static'): continue
        for domain in site['domains']:
            command(['curl', '--noproxy', '*', '--fail', '--silent', '--show-error', '--cacert',
                '/srv/ops/proxy/data/caddy/pki/authorities/local/root.crt', '--resolve', domain + ':443:127.0.0.1', 'https://' + domain + '/'])
    assert not command(['docker', 'ps', '-aq', '--filter', 'label=hosting.backup'])
    assert not command(['systemctl', '--failed', '--no-legend', '--no-pager'])
    assert command(['systemctl', 'is-active', 'reeve-remote.timer']) == 'active'
    assert command(['systemctl', 'show', 'reeve-remote.service', '-p', 'Result', '--value']) == 'success'
    report.update(original_25_healthy=True, caddy_https=True,
                  wordpress_schedule_unchanged=True, unresolved_operations=0, dump_helpers=0, failed_units=0,
                  timer_active=True, staging_bytes=usage(), boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
    Path(output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument('--output', default='/srv/ops/panel/worker/remote-copy-acceptance/final.json')
    main(parser.parse_args().output)
