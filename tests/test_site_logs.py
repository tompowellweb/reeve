"""The site Logs page's reader: sources, container output, the edge's access log, bounds and filters."""
import json
import uuid

import pytest

from reeve import site_logs as sl
from reeve.core import Ledger


def test_edge_lines_are_readable_and_other_lines_are_skipped():
    line = json.dumps({'ts': 1789836000.5, 'status': 500, 'size': 1234, 'duration': 0.0421, 'request': {'method': 'GET', 'host': 'p1.throw.test', 'uri': '/index.php?x=1', 'remote_ip': '203.0.113.9'}})
    assert sl.edge_entry(line) == '2026-09-19T16:40:00Z 500 GET p1.throw.test/index.php?x=1 1234B 42ms from 203.0.113.9'
    assert sl.edge_entry('{"level":"info","msg":"server running"}') is None and sl.edge_entry('not json') is None


def test_tail_reads_whole_lines_from_the_end_within_the_limit(tmp_path):
    path = tmp_path / 'a.log'; path.write_text(''.join(f'line {i}\n' for i in range(1000)))
    assert sl.tail_lines(path)[-1] == 'line 999' and len(sl.tail_lines(path)) == 1000
    tail = sl.tail_lines(path, limit=100)
    assert tail[0].startswith('line ') and len(tail[0]) == len('line 999') and tail[-1] == 'line 999'  # never a partial first line
    assert sl.tail_lines(tmp_path / 'missing.log') == []


def test_read_lists_sources_and_bounds_lines_window_and_filter(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / 'jobs.db', tmp_path / 'sites')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'p1', 'domain': 'p1.throw.test', 'runtime': 'php', 'php_version': '8.4'})
    monkeypatch.setattr('reeve.database_site.state', lambda row: {'stage': 'ready'})
    calls = []
    def fake_command(args, timeout=120):
        calls.append(args)
        if 'hosting-db-p1' in args: raise RuntimeError('docker failed (1): Error response from daemon: No such container: hosting-db-p1')
        return '2026-09-19T18:10:00.000000000Z [19-Sep-2026 18:10:00] WARNING: [pool www] child 7 said into stderr: "PHP Fatal error:  Uncaught Error: Call to undefined function phpinf() in /var/www/html/index.php:1"\n2026-09-19T18:10:01.000000000Z 10.240.7.2 - GET /index.php 500\n'
    monkeypatch.setattr(sl, 'command', fake_command)
    logs = tmp_path / 'proxy/data/logs'; logs.mkdir(parents=True); monkeypatch.setattr(sl, 'PROXY', tmp_path / 'proxy')
    (logs / 'p1.throw.test.log').write_text(json.dumps({'ts': 1789836000.0, 'status': 200, 'size': 1, 'duration': 0.001, 'request': {'method': 'GET', 'host': 'p1.throw.test', 'uri': '/', 'remote_ip': '1.2.3.4'}}) + '\n'
                                          + json.dumps({'ts': 1789836001.0, 'status': 500, 'size': 1, 'duration': 0.002, 'request': {'method': 'GET', 'host': 'p1.throw.test', 'uri': '/index.php', 'remote_ip': '1.2.3.4'}}) + '\n')
    assert [s[0] for s in sl.sources(ledger, row)] == ['web', 'php', 'database', 'edge']
    result = sl.read(ledger, row, 'php', '100', '1h', 'fatal')
    assert result['container'] == 'hosting-php-p1' and result['count'] == 1 and 'PHP Fatal error' in result['lines'][0]
    assert calls[-1][:5] == ['docker', 'logs', '--tail', '100', '--timestamps'] and '--since' in calls[-1] and calls[-1][-1] == 'hosting-php-p1'
    assert sl.read(ledger, row, 'database', '100', '', '')['lines'][0].startswith('(no such container')
    edge = sl.read(ledger, row, 'edge', '100', '', '500')
    assert edge['count'] == 1 and edge['lines'][0].endswith('/index.php 1B 2ms from 1.2.3.4') and [s['key'] for s in edge['sources']] == ['web', 'php', 'database', 'edge']
    for bad in (('php', '7', '', ''), ('php', '100', 'yesterday', ''), ('mail', '100', '', '')):
        with pytest.raises(ValueError): sl.read(ledger, row, *bad)
