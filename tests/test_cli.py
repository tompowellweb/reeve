import json
import sys

import pytest

from reeve import cli


@pytest.fixture
def worker(monkeypatch):
    """A fake worker: every message is recorded and answered from a small script."""
    rows = [{'id': 'site-1', 'name': 'shop', 'domain': 'shop.example.com', 'payload': json.dumps({'runtime': 'php'}), 'state': 'succeeded', 'health': {'application': 'healthy'},
             'domain_job': {'id': 'dom-1', 'state': 'failed'}, 'runtime_job': {'id': 'run-1', 'state': 'succeeded'}, 'database_job': None},
            {'id': 'site-2', 'name': 'blog', 'domain': 'blog.example.com', 'payload': json.dumps({}), 'state': 'recovery-needed', 'health': {'application': 'unknown'}, 'domain_job': None, 'runtime_job': None, 'database_job': None}]
    sent = []
    def fake(message):
        sent.append(message)
        if message['op'] == 'list': return rows
        if message['op'] in ('site-restores', 'site-deletes'): return []
        return {'ok': message['op']}
    monkeypatch.setattr(cli, 'rpc', fake)
    return sent


def run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, 'argv', ['reeve', *argv])
    cli.main()
    return capsys.readouterr().out


def test_sites_are_addressed_by_name_and_listed_as_a_table(worker, monkeypatch, capsys):
    text = run(monkeypatch, capsys, 'site', 'list')
    assert text.splitlines()[0].startswith('NAME') and text.index('blog') < text.index('shop')
    assert json.loads(run(monkeypatch, capsys, 'site', 'list', '--json'))[0]['name'] == 'shop'
    run(monkeypatch, capsys, 'site', 'domains', 'shop', 'shop.example.com', 'www.shop.example.com')
    assert worker[-1]['op'] == 'domains' and worker[-1]['site_id'] == 'site-1' and worker[-1]['domains'] == ['shop.example.com', 'www.shop.example.com']
    run(monkeypatch, capsys, 'site', 'create', 'new', 'new.example.com', '--alias', 'www.new.example.com', '--runtime', 'php', '--php-version', '8.4', '--database', 'mariadb', '--database-usage', 'light', '--data-mb', '2048')
    data = worker[-1]['data']
    assert worker[-1]['op'] == 'create' and data['aliases'] == ['www.new.example.com'] and data['database'] == {'engine': 'mariadb', 'series': None, 'exact': None, 'usage': 'light'} and data['data_mb'] == 2048
    with pytest.raises(SystemExit, match='No site named'): run(monkeypatch, capsys, 'site', 'backup', 'missing')
    run(monkeypatch, capsys, 'site', 'restore', 'backup-9', '--name', 'copy', '--domain', 'copy.example.com')
    assert worker[-1] == {'op': 'site-restore', 'snapshot': 'backup-9', 'name': 'copy', 'domain': 'copy.example.com'}


def test_retry_finds_the_failed_operation_whatever_its_kind(worker, monkeypatch, capsys):
    run(monkeypatch, capsys, 'site', 'retry', 'blog')
    assert worker[-1] == {'op': 'retry', 'id': 'site-2'}  # the site's own create
    run(monkeypatch, capsys, 'site', 'retry', 'shop')
    assert worker[-1] == {'op': 'retry-domains', 'id': 'dom-1'}  # the failed domain change of a healthy site


def test_the_other_nouns_map_to_the_worker(worker, monkeypatch, capsys):
    run(monkeypatch, capsys, 'php', 'switch', 'shop', '8.3'); assert worker[-1]['op'] == 'php-switch' and worker[-1]['branch'] == '8.3'
    run(monkeypatch, capsys, 'php', 'rollback', 'shop'); assert worker[-1]['op'] == 'php-rollback' and worker[-1]['previous'] == 'run-1'
    run(monkeypatch, capsys, 'db', 'usage', 'shop', 'high'); assert worker[-1]['op'] == 'content-submit' and worker[-1]['kind'] == 'database-usage' and worker[-1]['data'] == {'usage': 'high'}
    run(monkeypatch, capsys, 'db', 'add', 'shop', 'postgres', '--series', '18'); assert worker[-1]['op'] == 'add-database' and worker[-1]['data']['engine'] == 'postgres'
    run(monkeypatch, capsys, 'backup', 'copy'); assert worker[-1]['op'] == 'backup-remote'
    run(monkeypatch, capsys, 'backup', 'schedule', 'shop', '--interval', '60', '--paused'); assert worker[-1] == {'op': 'backup-schedule', 'site_id': 'site-1', 'interval': 60, 'enabled': False}
    run(monkeypatch, capsys, 'mail', 'status'); assert worker[-1]['op'] == 'mail-status'
    with pytest.raises(SystemExit): run(monkeypatch, capsys, 'site', 'nonsense')
