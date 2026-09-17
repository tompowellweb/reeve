import importlib.util
import json
import time
import uuid
from pathlib import Path

import pytest

from reeve import versions, php_runtime
from reeve.core import Ledger


def ident():
    return str(uuid.uuid4())


def test_signed_collector_discovers_branches_and_virtual_extensions():
    spec = importlib.util.spec_from_file_location('collector', Path(__file__).resolve().parents[1] / 'templates/php-catalogue/collect.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    text = '\n\n'.join(f'Package: php8.2-{suffix}\nVersion: 8.2.30-1\nArchitecture: amd64' for suffix in module.SUFFIXES)
    text += '\n\nPackage: php8.5-fpm\nVersion: 8.5.1-1\nProvides: php8.5-opcache (= 8.5.1)\n'
    rows = module.parse(text)
    assert rows[0]['branch'] == '8.2' and not rows[0]['missing']
    assert rows[1]['packages']['opcache']['provider'] == 'php8.5-fpm'
    assert 'cli' in rows[1]['missing']


def test_expired_cache_failure_preserves_known_versions_and_installed_images(tmp_path, monkeypatch):
    cache = tmp_path / 'cache.json'
    old = {'schema': 1, 'checked_at': time.time() - 31 * 86400, 'attempted_at': 0, 'error': '',
           'branches': [{'branch': '8.2', 'missing': [], 'package_version': '8.2.30'}]}
    cache.write_text(json.dumps(old))
    monkeypatch.setattr(versions, 'CACHE', cache)
    monkeypatch.setattr(versions, 'trusted', lambda *args, **kw: None)
    monkeypatch.setattr(versions, 'atomic', lambda path, contents: path.write_text(contents))
    monkeypatch.setattr(php_runtime, 'catalog', lambda: {'7.0': {'php_version': '7.0.33'}})
    def failed(*args, **kw):
        raise RuntimeError('Repository unavailable')
    monkeypatch.setattr(versions, 'command', failed)
    assert versions.due()
    with pytest.raises(RuntimeError, match='unavailable'):
        versions.refresh()
    current = versions.read()
    assert current['branches'] == old['branches'] and current['checked_at'] == old['checked_at']
    assert current['error'] == 'Repository unavailable' and current['stale']
    assert not versions.due()  # failed attempts retry daily, not in a tight loop
    assert versions.require('7.0') == '7.0'
    assert versions.require('8.2') == '8.2'
    with pytest.raises(ValueError):
        versions.require('8.6')
    with pytest.raises(ValueError):
        versions.require('8.2;id')


def test_runtime_jobs_serialize_with_domain_edits_and_preserve_create(tmp_path):
    ledger = Ledger(tmp_path / 'jobs.db', tmp_path / 'sites')
    data = {'name': 'php', 'domain': 'php.example.com', 'runtime': 'php', 'php_version': '8.2'}
    row = ledger.submit(ident(), data)
    ledger.update(row['id'], 'succeeded', 'published')
    job = ledger.submit_runtime(ident(), 'switch', row['id'], {'branch': '8.3'})
    assert ledger.submit_runtime(job['id'], 'switch', row['id'], {'branch': '8.3'})['id'] == job['id']
    with pytest.raises(ValueError, match='different inputs'):
        ledger.submit_runtime(job['id'], 'switch', row['id'], {'branch': '8.4'})
    with pytest.raises(ValueError, match='pending'):
        ledger.submit_domains(ident(), row['id'], ['new.example.com'])
    with pytest.raises(ValueError, match='pending'):
        ledger.submit_runtime(ident(), 'switch', row['id'], {'branch': '8.4'})
    ledger.update_runtime(job['id'], 'running', 'deploying replacement')
    reopened = Ledger(ledger.path, ledger.sites)
    reopened.interrupted()
    assert reopened.runtime_jobs()[0]['state'] == 'recovery-needed'
    reopened.retry_runtime(job['id'])
    reopened.finish_runtime(job, '8.3')
    assert reopened.runtime_branch(row) == '8.3'
    assert reopened.submit(row['id'], data)['payload'] == row['payload']
    rollback = reopened.submit_runtime(ident(), 'rollback', row['id'], {'previous': job['id']})
    reopened.finish_runtime(rollback, '8.2')
    assert reopened.runtime_branch(row) == '8.2'
    with pytest.raises(ValueError, match='most recent'):
        reopened.submit_runtime(ident(), 'rollback', row['id'], {'previous': job['id']})


def test_refresh_deduplicates_without_blocking_other_sites(tmp_path):
    ledger = Ledger(tmp_path / 'jobs.db', tmp_path / 'sites')
    job = ledger.submit_runtime(ident(), 'refresh')
    assert ledger.submit_runtime(ident(), 'refresh')['id'] == job['id']
    ledger.update_runtime(job['id'], 'recovery-needed', 'checking', 'offline')
    assert ledger.submit_runtime(ident(), 'refresh')['id'] != job['id']
