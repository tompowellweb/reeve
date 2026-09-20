import json
import os
import stat
import uuid

import pytest

from reeve import host as hm, server_status as ss
from reeve.core import Ledger


def test_sizes_quotas_containers_and_health_are_read_from_the_commands_output():
    assert ss.parse_size('9.259GB') == 9_259_000_000 and ss.parse_size('1.225GB (15%)') == 1_225_000_000 and ss.parse_size('879.7kB') == 879_700
    with pytest.raises(ValueError): ss.parse_size('lots')
    assert ss.application_health([]) == 'absent'
    assert ss.application_health([{'state': 'running', 'health': 'healthy'}, {'state': 'running', 'health': None}]) == 'healthy'
    assert ss.application_health([{'state': 'running', 'health': 'healthy'}, {'state': 'exited', 'health': None}]) == 'unhealthy'
    assert ss.application_health([{'state': 'running', 'health': 'starting'}]) == 'starting'
    assert ss.application_health([{'state': 'running', 'health': None}]) == 'running'


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A fake box: kernel files, cgroups, three commands, one managed PHP site and one package site."""
    for module in (ss, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    ops = tmp_path / 'ops'; (ops / 'panel/worker').mkdir(parents=True); (tmp_path / 'backups').mkdir()
    monkeypatch.setattr(ss, 'OPS', ops); monkeypatch.setattr(ss, 'BACKUPS', tmp_path / 'backups'); monkeypatch.setattr(ss, 'STATUS', ops / 'panel/status.json'); monkeypatch.setattr(ss, 'SAMPLE', ops / 'panel/worker/server-status-sample.json')
    proc = tmp_path / 'proc'; (proc / 'pressure').mkdir(parents=True)
    (proc / 'pressure/cpu').write_text('some avg10=8.58 avg60=8.77 avg300=8.69 total=1609587464\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
    (proc / 'pressure/memory').write_text('some avg10=0.00 avg60=0.50 avg300=0.00 total=195445\n')
    (proc / 'meminfo').write_text('MemTotal:       16314408 kB\nMemFree:          225000 kB\nMemAvailable:   10588932 kB\nSwapTotal:       2169852 kB\nSwapFree:        2168816 kB\n')
    monkeypatch.setattr(ss, 'PROC', proc)
    cg = tmp_path / 'cgroups'; monkeypatch.setattr(ss, 'CGROUPS', cg)
    ledger = Ledger(tmp_path / 'jobs.db', sites=tmp_path / 'sites')
    php = ledger.submit(str(uuid.uuid4()), {'name': 'shop', 'domain': 'shop.hosting.test', 'runtime': 'php', 'php_version': '8.3', 'data_mb': 512})
    package = ledger.submit(str(uuid.uuid4()), {'name': 'app', 'domain': 'app.hosting.test'})
    with ledger.db() as db:  # a deployed package: the row's payload names its Compose project
        db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps({'name': 'app', 'domain': 'app.hosting.test', 'runtime': 'compose', 'package_id': package['id'], 'project_name': 'package-' + package['id']}), package['id']))
    usage = {'php': 40_000_000, 'web': 1_000_000, 'pkg': 5_000_000}
    def scope(ident, memory_now, key):
        (cg / f'docker-{ident}.scope').mkdir(parents=True, exist_ok=True)
        (cg / f'docker-{ident}.scope/memory.current').write_text(str(memory_now + 1_000_000) + '\n')  # a million bytes of inactive file cache, not counted
        (cg / f'docker-{ident}.scope/memory.stat').write_text('anon 1\nfile 2\ninactive_file 1000000\nactive_file 5\n')
        (cg / f'docker-{ident}.scope/cpu.stat').write_text(f'usage_usec {usage[key]}\nuser_usec 1\nsystem_usec 1\n')
    scope('a' * 64, 17_000_000, 'php'); scope('b' * 64, 3_000_000, 'web'); scope('c' * 64, 9_000_000, 'pkg')
    listing = (f"{'a' * 64}\thosting-php-shop\trunning\tUp 2 hours (healthy)\n{'b' * 64}\thosting-site-shop\trunning\tUp 2 hours (healthy)\n"
               f"{'c' * 64}\tpackage-{package['id']}-web-1\trunning\tUp 1 hour\n{'d' * 64}\thosting-sftp\trunning\tUp 4 minutes\n{'e' * 64}\thosting-site-other\texited\tExited (0)\n")
    calls = []
    def fake_command(args, timeout=120):
        calls.append(args)
        if args[:3] == ['docker', 'system', 'df']: return 'Images\t8.128GB\t1.225GB (15%)\nContainers\t879.7kB\t0B (0%)\nLocal Volumes\t723.9MB\t0B (0%)\nBuild Cache\t9.259GB\t6.089GB\n'
        if args[:2] == ['docker', 'ps']: return listing
        if args[0] == 'xfs_quota': return (f"Project quota on /srv (/dev/vdb)\n#0 40000000 0 0 00 [------]\n#{php['project']} 204800 0 524288 00 [------]\n"
                                           f"#{package['project']} 1024 0 1048576 00 [------]\n#999 512 0 1024 00 [------]\n")
        if args[0] == 'du': return '41000000000\t' + args[2] + '\n'
        if args[0] == 'findmnt': return '/dev/vdb\n'
        raise AssertionError(args)
    monkeypatch.setattr(ss, 'command', fake_command)
    monkeypatch.setattr(ss, 'filesystem', lambda path: {'total': 200 * 1024 ** 3, 'free': 138 * 1024 ** 3} if str(path) == str(ops.parent) else {'total': 37 * 1024 ** 3, 'free': 24 * 1024 ** 3})
    monkeypatch.setattr(ss, 'backups_summary', lambda ledger: {'state': 'configured', 'type': 'sftp', 'pending': 2, 'last_copy': 1_700_000_000.0, 'error': ''})
    monkeypatch.setattr(ss, 'mail_summary', lambda host: {'set_up': True, 'running': True, 'mode': 'sink', 'queued': 1, 'hour': {'sent': 3, 'deferred': 0, 'bounced': 0, 'limited': 0}, 'day': {'sent': 9, 'deferred': 1, 'bounced': 0, 'limited': 0}, 'error': ''})
    monkeypatch.setattr('reeve.sftp.registry', lambda: {php['id']: {'name': 'shop'}})
    return ledger, php, package, usage, scope, calls


def test_summary_gathers_the_box_and_every_site_and_the_second_sample_gives_cpu(box):
    ledger, php, package, usage, scope, calls = box
    first = ss.write(ledger, None, now=1000.0)
    assert first['errors'] == {} and first['took'] >= 0
    assert first['cpu'] == {'cores': os.cpu_count() or 1, 'load': first['cpu']['load'], 'pressure': 8.77} and len(first['cpu']['load']) == 3
    assert first['memory']['total'] == 16314408 * 1024 and first['memory']['used'] == (16314408 - 10588932) * 1024 and first['memory']['swap_used'] == (2169852 - 2168816) * 1024 and first['memory']['pressure'] == 0.5
    disk = first['disk']
    assert disk['srv']['free'] == 138 * 1024 ** 3 and disk['root']['free'] == 24 * 1024 ** 3 and disk['backups_used'] == 41_000_000_000 and disk['image'] is False
    assert disk['sites_used'] == (204800 + 1024) * 1024  # project 0 and the stray project 999 are not sites
    assert disk['docker']['images'] == {'size': 8_128_000_000, 'reclaimable': 1_225_000_000} and disk['docker']['build_cache']['reclaimable'] == 6_089_000_000
    sites = {s['name']: s for s in first['sites']}
    assert sites['shop']['health'] == 'healthy' and sites['shop']['containers'] == 2 and sites['shop']['memory'] == 20_000_000 and sites['shop']['cpu'] is None
    assert sites['shop']['disk_used'] == 204800 * 1024 and sites['shop']['disk_hard'] == 524288 * 1024 and sites['shop']['runtime'] == 'php'
    assert sites['app']['health'] == 'running' and sites['app']['memory'] == 9_000_000 and sites['app']['containers'] == 1 and sites['app']['runtime'] == 'compose'
    assert first['backups']['pending'] == 2 and first['mail']['queued'] == 1 and first['sftp'] == {'on': ['shop'], 'running': True}
    # A minute later the containers have used 30 s of CPU between them: half a core for shop, none for the package.
    usage['php'] += 20_000_000; usage['web'] += 10_000_000
    scope('a' * 64, 17_000_000, 'php'); scope('b' * 64, 3_000_000, 'web')
    second = ss.write(ledger, None, now=1060.0)
    sites = {s['name']: s for s in second['sites']}
    assert sites['shop']['cpu'] == 0.5 and sites['app']['cpu'] == 0.0
    written = ss.STATUS
    assert stat.S_IMODE(written.stat().st_mode) == 0o644 and ss.read(written)['at'] == 1060.0
    assert ss.read(written.with_name('missing.json')) is None
    written.write_text('{not json'); assert ss.read(written) is None


def test_a_failing_source_is_reported_and_the_rest_still_shows(box, monkeypatch):
    ledger, *_ = box
    monkeypatch.setattr(ss, 'docker_disk', lambda: (_ for _ in ()).throw(RuntimeError('docker away')))
    monkeypatch.setattr(ss, 'mail_summary', lambda host: (_ for _ in ()).throw(RuntimeError('relay away')))
    result = ss.write(ledger, None, now=1000.0)
    assert result['disk'] is None and result['mail'] is None
    assert result['errors'] == {'disk': 'docker away', 'mail': 'relay away'}
    assert result['cpu'] and result['sites'] and result['backups'] and result['sftp']


def test_backups_mail_and_sftp_summaries_reduce_the_modules_status(monkeypatch):
    monkeypatch.setattr('reeve.remote_backup.status', lambda ledger, site_id: {'state': 'paused', 'type': 's3', 'pending': 1, 'pending_sites': 3, 'last_copy': {'created': 5.0}, 'last_site_copy': {'created': 9.0}})
    assert ss.backups_summary(None) == {'state': 'paused', 'type': 's3', 'pending': 4, 'destinations': 0, 'last_copy': 9.0, 'error': ''}
    monkeypatch.setattr('reeve.remote_backup.status', lambda ledger, site_id: {'state': 'not configured', 'type': None, 'pending': 0, 'pending_sites': 0, 'last_copy': None, 'last_site_copy': None})
    assert ss.backups_summary(None)['last_copy'] is None
    counts = lambda s, d: {'hour': {'sent': s, 'deferred': d, 'bounced': 0, 'limited': 0}, 'day': {'sent': s * 2, 'deferred': d, 'bounced': 1, 'limited': 0}}
    monkeypatch.setattr('reeve.mail.status', lambda host: {'set_up': True, 'running': True, 'mode': 'direct', 'queue': [{}, {}], 'sites': [{'name': 'a', **counts(2, 1)}, {'name': 'b', **counts(3, 0)}], 'error': ''})
    assert ss.mail_summary(None) == {'set_up': True, 'running': True, 'mode': 'direct', 'queued': 2, 'hour': {'sent': 5, 'deferred': 1, 'bounced': 0, 'limited': 0}, 'day': {'sent': 10, 'deferred': 1, 'bounced': 2, 'limited': 0}, 'error': ''}
    monkeypatch.setattr('reeve.mail.status', lambda host: {'set_up': False, 'running': False, 'mode': 'direct', 'queue': [], 'sites': [], 'error': ''})
    assert ss.mail_summary(None)['hour'] is None
    monkeypatch.setattr('reeve.sftp.registry', lambda: {'x': {'name': 'beta'}, 'y': {'name': 'alpha'}})
    assert ss.sftp_summary([{'name': 'hosting-sftp', 'state': 'exited'}]) == {'on': ['alpha', 'beta'], 'running': False}
