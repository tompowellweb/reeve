import json
import os
import uuid

import pytest

from reeve import host as hm, traffic as tr
from reeve.core import Ledger


def line(ts, host, status=200, size=1000, duration=0.01, uri='/', msg='handled request'):
    return json.dumps({'level': 'info', 'ts': ts, 'logger': 'http.log.access.log0', 'msg': msg, 'request': {'host': host, 'uri': uri, 'client_ip': '10.0.0.1'},
                       'status': status, 'size': size, 'duration': duration}) + '\n'


@pytest.fixture
def edge(tmp_path, monkeypatch):
    for module in (tr, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    logs = tmp_path / 'proxy/data/logs'; logs.mkdir(parents=True)
    worker = tmp_path / 'ops/panel/worker'; worker.mkdir(parents=True)
    monkeypatch.setattr(tr, 'LOGS', logs); monkeypatch.setattr(tr, 'OFFSETS', worker / 'traffic-offsets.json')
    ledger = Ledger(tmp_path / 'jobs.db', sites=tmp_path / 'sites')
    shop = ledger.submit(str(uuid.uuid4()), {'name': 'shop', 'domain': 'shop.example.com', 'aliases': ['www.shop.example.com']})
    blog = ledger.submit(str(uuid.uuid4()), {'name': 'blog', 'domain': 'blog.example.com'})
    return ledger, logs, shop, blog


def test_parse_reads_a_request_line_and_rejects_the_rest():
    assert tr.parse(line(1700000000.5, 'a.example', 404, 327, 0.0679, '/x')) == (1700000000.5, 404, 327, 0.0679, '/x')
    assert tr.parse(line(1, 'a', msg='server is up')) is None
    assert tr.parse('not json') is None and tr.parse('{"msg": "handled request"}') is None


def test_tick_buckets_by_site_and_hour_and_reads_only_what_is_new(edge):
    ledger, logs, shop, blog = edge
    hour = 1700000000 - 1700000000 % 3600
    shop_log = logs / 'shop.example.com.log'
    shop_log.write_text(line(hour + 10, 'shop.example.com') + line(hour + 20, 'shop.example.com', 503, 10, 2.5) + line(hour + 30, 'shop.example.com', 404, 300)
                        + line(hour + 40, 'shop.example.com', uri='/__hosting_health') + line(hour + 3700, 'shop.example.com', size=5000) + 'garbage\n')
    (logs / 'www.shop.example.com.log').write_text(line(hour + 50, 'www.shop.example.com', 301, 0))
    (logs / 'blog.example.com.log').write_text(line(hour + 60, 'blog.example.com'))
    (logs / 'gone.example.com.log').write_text(line(hour + 70, 'gone.example.com'))
    (logs / 'shop.example.com-2026-09-17T13-17-00.000.log').write_text(line(hour, 'shop.example.com') * 5)  # an old rolled file is not re-read
    report = tr.tick(ledger, now=hour + 4000)
    assert report == {'files': 4, 'lines': 7, 'unknown': 1, 'skipped': 1}
    with ledger.db() as db:
        rows = {(r['site_id'], r['hour']): dict(r) for r in db.execute('SELECT * FROM traffic')}
    first = rows[(shop['id'], hour)]
    assert (first['requests'], first['bytes'], first['ok'], first['client_errors'], first['server_errors'], first['slow']) == (4, 1310, 2, 1, 1, 1)
    assert rows[(shop['id'], hour + 3600)]['requests'] == 1 and rows[(blog['id'], hour)]['requests'] == 1
    assert len(rows) == 3
    offsets = json.loads(tr.OFFSETS.read_text())
    assert offsets['shop.example.com.log'] == {'inode': shop_log.stat().st_ino, 'offset': shop_log.stat().st_size}
    # Nothing new: nothing counted twice. A partial line waits for its newline.
    assert tr.tick(ledger, now=hour + 4060)['lines'] == 0
    with open(shop_log, 'a') as f: f.write(line(hour + 4000, 'shop.example.com')[:-10])
    assert tr.tick(ledger, now=hour + 4120)['lines'] == 0
    with open(shop_log, 'a') as f: f.write(line(hour + 4000, 'shop.example.com')[-10:])
    assert tr.tick(ledger, now=hour + 4180)['lines'] == 1
    with ledger.db() as db:
        assert db.execute('SELECT requests FROM traffic WHERE site_id=? AND hour=?', (shop['id'], hour + 3600)).fetchone()[0] == 2


def test_a_rolled_file_gives_up_its_tail_and_a_recreated_one_starts_over(edge):
    ledger, logs, shop, blog = edge
    hour = 1700000000 - 1700000000 % 3600
    current = logs / 'shop.example.com.log'
    current.write_text(line(hour + 1, 'shop.example.com'))
    tr.tick(ledger, now=hour + 100)
    # Two more lines arrive, then Caddy rolls: the file is renamed and a fresh one starts.
    with open(current, 'a') as f: f.write(line(hour + 2, 'shop.example.com') + line(hour + 3, 'shop.example.com'))
    os.rename(current, logs / 'shop.example.com-2026-09-17T14-00-00.000.log')
    current.write_text(line(hour + 4, 'shop.example.com'))
    assert tr.tick(ledger, now=hour + 200)['lines'] == 3
    # The edge is recreated with an empty log folder: the offset is beyond the new file, so it starts from the top.
    current.write_text(line(hour + 5, 'shop.example.com'))
    (logs / 'shop.example.com-2026-09-17T14-00-00.000.log').unlink()
    saved = json.loads(tr.OFFSETS.read_text()); saved['shop.example.com.log']['offset'] = 10 ** 6; tr.OFFSETS.write_text(json.dumps(saved))
    assert tr.tick(ledger, now=hour + 300)['lines'] == 1
    with ledger.db() as db:
        assert db.execute('SELECT requests FROM traffic WHERE site_id=?', (shop['id'],)).fetchone()[0] == 5


def test_recent_and_site_traffic_windows_and_pruning(edge):
    ledger, logs, shop, blog = edge
    now = 1700000000.0
    this_hour = int(now // 3600) * 3600; today = int(now // 86400) * 86400
    with ledger.db() as db:
        for hour, requests, errors in ((this_hour, 10, 1), (this_hour - 23 * 3600, 5, 0), (this_hour - 24 * 3600, 100, 0), (today - 29 * 86400, 7, 2), (today - 30 * 86400, 1000, 0), (this_hour - 40 * 86400, 1, 0)):
            db.execute('INSERT INTO traffic VALUES (?,?,?,?,?,?,?,?)', (shop['id'], hour, requests, requests * 100, requests - errors, 0, errors, 0))
        db.execute('INSERT INTO traffic VALUES (?,?,?,?,?,?,?,?)', (blog['id'], this_hour, 3, 300, 3, 0, 0, 0))
    day = tr.recent(ledger, now)
    assert day[shop['id']] == {'requests': 15, 'bytes': 1500, 'server_errors': 1, 'client_errors': 0} and day[blog['id']]['requests'] == 3
    site = tr.site_traffic(ledger, shop['id'], now)
    assert site['day']['requests'] == 15 and site['day']['server_errors'] == 1
    assert site['month']['requests'] == 122 and site['month']['server_errors'] == 3  # the 30-day window starts 29 days before today
    assert len(site['hours']) == 24 and site['hours'][-1]['requests'] == 10 and site['hours'][0]['requests'] == 5 and site['peak_hour'] == 10
    assert len(site['days']) == 30 and site['days'][-1]['requests'] == 10 and site['days'][-2]['requests'] == 105 and site['days'][0]['requests'] == 7  # 22:00 UTC: the last two hours of the day are yesterday's
    assert site['peak_day'] == max(d['requests'] for d in site['days'])
    (logs / 'x.log').write_text('')
    tr.tick(ledger, now=this_hour + 30)  # the first minute of an hour prunes
    with ledger.db() as db:
        kept = [r[0] for r in db.execute('SELECT hour FROM traffic WHERE site_id=? ORDER BY hour', (shop['id'],))]
    assert this_hour - 40 * 86400 not in kept and today - 30 * 86400 not in kept and today - 29 * 86400 in kept and len(kept) == 4


def test_reconcile_edge_rewrites_and_reloads_only_when_the_rendered_text_moved_on(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from reeve import host as hm
    proxy = tmp_path / 'proxy'; (proxy / 'conf').mkdir(parents=True)
    monkeypatch.setattr(hm, 'PROXY', proxy); monkeypatch.setattr(hm, 'trusted', lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(hm, 'command', lambda args, timeout=120: calls.append(args) or '')
    routes = {'a.example': {'upstream': 'web-a:8080', 'operation': 'x', 'network': 'hosting-backend-a'}}
    (proxy / 'routes.json').write_text(json.dumps(routes))
    host = SimpleNamespace(inspect=lambda name: {'State': {'Running': True}})
    assert hm.reconcile_edge(host)['reconciled'] is True and 'log {' in (proxy / 'conf/Caddyfile').read_text()
    assert [c[4] for c in calls] == ['validate', 'reload'] and (proxy / 'conf/previous.Caddyfile').read_text() == ''
    calls.clear()
    assert hm.reconcile_edge(host) == {'reconciled': False, 'reason': 'current'} and calls == []
    (proxy / 'conf/Caddyfile').write_text('old text without logs')
    assert hm.reconcile_edge(SimpleNamespace(inspect=lambda name: None))['reason'] == 'edge not running'
    assert (proxy / 'conf/Caddyfile').read_text() == 'old text without logs'
    def failing(args, timeout=120):
        calls.append(args)
        if args[4] == 'reload' and len([c for c in calls if c[4] == 'reload']) == 1: raise RuntimeError('reload refused')
        return ''
    monkeypatch.setattr(hm, 'command', failing)
    with pytest.raises(RuntimeError): hm.reconcile_edge(host)
    assert (proxy / 'conf/Caddyfile').read_text() == 'old text without logs' and [c[4] for c in calls] == ['validate', 'reload', 'reload']
