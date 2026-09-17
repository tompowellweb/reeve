import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from reeve import site_rules as sr, host as hm
from reeve.content_jobs import validate_content


def test_validation_normalises_text_and_bounds_it():
    assert validate_content('site-rules', {'text': 'location = /a { return 301 /b; }'}) == {'text': 'location = /a { return 301 /b; }\n'}
    assert sr.validate({'text': 'a\r\nb\r\n'}) == {'text': 'a\nb\n'} and sr.validate({'text': ''}) == {'text': ''}
    for bad in ({'text': 'x\x00'}, {'text': 'x' * 70000}, {'rules': 'x'}, {'text': 1}):
        with pytest.raises(ValueError): sr.validate(bad)


@pytest.fixture
def site(tmp_path, monkeypatch):
    sites = tmp_path / 'sites'; root = sites / 'pages'; (root / 'conf').mkdir(parents=True); (root / 'html').mkdir()
    for module in (sr, hm): monkeypatch.setattr(module, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr(sr, 'SITES', sites); monkeypatch.setattr(sr, 'SAVED', tmp_path / 'saved')
    monkeypatch.setattr('reeve.content_site.OUTPUT', tmp_path / 'output')
    row = {'id': str(uuid.uuid4()), 'name': 'pages', 'payload': json.dumps({'runtime': 'static'})}
    (root / 'hosting.yaml').write_text(yaml.safe_dump({'runtime': 'static', 'operation_id': row['id'], 'domain': 'pages.example.com', 'aliases': ['www.pages.example.com']}))
    (root / 'compose.yml').write_text(yaml.safe_dump({'services': {'web': {'image': 'nginx@sha256:abc', 'user': '30001:30001', 'volumes': [f'{root}/html:/site:ro']}}}))
    (root / 'conf/nginx.conf').write_text(hm.NGINX)  # created before rules existed: no include yet
    (root / 'conf/site.nginx.conf').write_text(sr.DEFAULT)
    calls = []
    monkeypatch.setattr(sr, 'command', lambda args, timeout=120: calls.append(args))
    monkeypatch.setattr('reeve.site_backup.refresh_web', lambda row, site: calls.append(['refresh', row['name']]))
    verified = []
    host = SimpleNamespace(verify_domains=lambda names: verified.append(names))
    return root, row, host, calls, verified


def test_apply_checks_with_nginx_then_recreates_web_and_verifies(site):
    root, row, host, calls, verified = site
    sr.apply(host, row, 'location = /a { return 301 /b; }\n', str(uuid.uuid4()))
    check = calls[0]
    assert check[:3] == ['docker', 'run', '--rm'] and '--network' in check and check[check.index('--network') + 1] == 'none'
    assert check[check.index('--user') + 1] == '30001:30001' and check[-1] == '-t' and 'site.nginx.candidate.conf:/etc/hosting/site.nginx.conf:ro' in ' '.join(check)
    assert calls[1] == ['refresh', 'pages'] and verified == [['pages.example.com', 'www.pages.example.com']]
    assert (root / 'conf/site.nginx.conf').read_text() == 'location = /a { return 301 /b; }\n' and not (root / 'conf/site.nginx.candidate.conf').exists()
    # An older static site gains the include and the mount on first use.
    assert 'include /etc/hosting/site.nginx.conf' in (root / 'conf/nginx.conf').read_text()
    assert f'{root}/conf/site.nginx.conf:/etc/hosting/site.nginx.conf:ro' in yaml.safe_load((root / 'compose.yml').read_text())['services']['web']['volumes']
    saved = json.loads(next((sr.SAVED).glob('*.json')).read_text()); assert saved == {'site_id': row['id'], 'text': sr.DEFAULT}


def test_invalid_rules_change_nothing_and_a_failed_apply_rolls_back(site, monkeypatch):
    root, row, host, calls, verified = site
    def failing(args, timeout=120):
        if args[-1] == '-t': raise RuntimeError('docker failed (1): nginx: [emerg] unknown directive "bogus"')
    monkeypatch.setattr(sr, 'command', failing)
    job = {'id': str(uuid.uuid4()), 'payload': json.dumps({'text': 'bogus;'})}
    from reeve.content_site import ContentFailed
    with pytest.raises(ContentFailed, match='previous rules stay'): sr.perform(host, row, job, lambda s: None)
    assert (root / 'conf/site.nginx.conf').read_text() == sr.DEFAULT and not verified
    assert 'unknown directive' in (Path(sr.SAVED).parent / 'output' / (job['id'] + '.txt')).read_text()
    # Valid rules but the site stops answering: the previous text comes back and the web container is recreated again.
    monkeypatch.setattr(sr, 'command', lambda args, timeout=120: None)
    attempts = []
    def verify(names):
        attempts.append(names)
        if len(attempts) == 1: raise RuntimeError('curl failed (22)')
    host.verify_domains = verify
    with pytest.raises(RuntimeError, match='curl failed'): sr.apply(host, row, 'location = /x { return 500; }\n')
    assert (root / 'conf/site.nginx.conf').read_text() == sr.DEFAULT and len(attempts) == 2


def test_rollback_restores_saved_rules_for_the_same_site_only(site):
    root, row, host, calls, verified = site
    ident = str(uuid.uuid4()); sr.SAVED.mkdir()
    (sr.SAVED / (ident + '.json')).write_text(json.dumps({'site_id': 'other', 'text': 'x'}))
    with pytest.raises(ValueError, match='another site'): sr.rollback(host, row, ident)
    (sr.SAVED / (ident + '.json')).write_text(json.dumps({'site_id': row['id'], 'text': '# restored\n'}))
    sr.rollback(host, row, ident)
    assert (root / 'conf/site.nginx.conf').read_text() == '# restored\n' and calls[-1] == ['refresh', 'pages']
