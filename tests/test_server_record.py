"""The server record: what a replacement server reads about this one."""
import json
import uuid

import pytest

from reeve import server_record as sr
from reeve.core import Ledger


def ident(): return str(uuid.uuid4())


def test_the_record_carries_settings_sites_hostnames_and_latest_backups(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / 'jobs.db', tmp_path / 'sites')
    shop = ledger.submit(ident(), {'name': 'shop', 'domain': 'shop.example.com', 'aliases': ['www.shop.example.com'], 'runtime': 'php', 'php_version': '8.4'})
    ledger.update(shop['id'], 'succeeded', 'published')
    pages = ledger.submit(ident(), {'name': 'pages', 'domain': 'pages.example.com'})
    ledger.update(pages['id'], 'succeeded', 'published')
    older, newer = ident(), ident()
    with ledger.db() as db:
        for b, at in ((older, 1000.0), (newer, 2000.0)):
            db.execute("INSERT INTO site_backups (id, site_id, kind, state, step, error, manifest, created, updated) VALUES (?,?,'scheduled','succeeded','done','',?,?,?)", (b, shop['id'], json.dumps({'coverage': 'complete', 'completed_at': at}), at, at))
        db.execute("INSERT INTO site_backups (id, site_id, kind, state, step, error, manifest, created, updated) VALUES (?,?,'manual','failed','x','boom','',3000,3000)", (ident(), shop['id']))
    monkeypatch.setattr('reeve.settings.document', lambda: {'schema': 1, 'profile': 'small', 'tls': {'mode': 'public', 'email': 'a@b.example'}})
    monkeypatch.setattr('reeve.updates.installed', lambda: {'version': '1.3.0'})
    monkeypatch.setattr('reeve.site_backup.deleted_sites', lambda ledger: [{'name': 'old', 'domain': 'old.example', 'deleted_at': 5.0, 'runtime': 'static', 'final_backup': {'id': 'f', 'kind': 'final', 'completed_at': 4.0, 'bytes': 1, 'path': '/x'}}])
    record = sr.build(ledger)
    assert record['kind'] == 'server-record' and record['version'] == '1.3.0' and record['settings'] == {'profile': 'small', 'tls': {'mode': 'public', 'email': 'a@b.example'}}
    names = [s['name'] for s in record['sites']]
    assert names == ['pages', 'shop']
    site = record['sites'][1]
    assert site['domains'] == ['shop.example.com', 'www.shop.example.com'] and site['runtime'] == 'php' and site['latest_backup'] == {'id': newer, 'kind': 'scheduled', 'completed_at': 2000.0}
    assert record['sites'][0]['latest_backup'] is None and record['deleted'][0]['final_backup'] == {'id': 'f', 'kind': 'final', 'completed_at': 4.0}
    assert sr.digest(record) == sr.digest({**record, 'written_at': 123})  # the time of writing is not content
    monkeypatch.setattr(sr, 'RECORD', tmp_path / 'record.json')
    assert sr.write(ledger) is True and sr.write(ledger) is False  # unchanged content is not rewritten
    assert sr.parse((tmp_path / 'record.json').read_text())['sites'][1]['name'] == 'shop'
    with pytest.raises(ValueError, match='Not a server record'): sr.parse(json.dumps({'kind': 'site-backup'}))
    with pytest.raises(ValueError, match='Damaged'): sr.parse(json.dumps({'kind': 'server-record', 'schema': 1, 'sites': 'x', 'settings': {}}))
