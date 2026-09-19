import pytest
"""The certificate each name is served with, as the Domains section states it."""
from reeve import certificates as cs


def test_the_served_certificate_is_described_for_the_operator():
    public = cs.describe("issuer=C=US, O=Let's Encrypt, CN=R11\nnotAfter=Dec 18 16:09:28 2026 GMT\n")
    assert public == {'state': 'public', 'text': "Let's Encrypt certificate, expires 18 Dec 2026"}
    own = cs.describe('issuer=CN=Caddy Local Authority - ECC Intermediate\nnotAfter=Sep 20 04:09:28 2026 GMT\n')
    assert own['state'] == 'internal' and own['text'].startswith("This server's own certificate") and 'points at this server' in own['text']
    none = cs.describe(None)
    assert none['state'] == 'none' and 'still obtaining' in none['text']
    assert cs.describe('issuer=CN=Something\n') == {'state': 'public', 'text': 'CN=Something certificate'}


def test_the_status_is_read_from_the_edge_and_the_resolver(monkeypatch):
    monkeypatch.setattr(cs, 'served', lambda domain: "issuer=O=Let's Encrypt\nnotAfter=Dec 18 16:09:28 2026 GMT\n" if domain == 'a.example' else None)
    monkeypatch.setattr(cs, 'resolves', lambda domain: ['203.0.113.5'] if domain == 'a.example' else [])
    assert cs.status('a.example') == {'state': 'public', 'text': "Let's Encrypt certificate, expires 18 Dec 2026", 'addresses': ['203.0.113.5']}
    assert cs.status('b.example')['state'] == 'none' and cs.status('b.example')['addresses'] == []
    monkeypatch.undo()
    assert cs.resolves('localhost') and cs.resolves('name.invalid', wait=2) == []
    assert cs.served('name.invalid', address='127.0.0.1', port=9, timeout=2) is None  # nothing listening: no certificate, no exception


def test_the_edges_log_yields_the_latest_error_and_success_per_name():
    lines = [
        '{"level":"info","ts":1789835170.0,"logger":"tls.obtain","msg":"obtaining certificate","identifier":"a.example"}',
        '{"level":"error","ts":1789835179.1,"logger":"http.acme_client","msg":"validating authorization","identifier":"a.example","problem":{"type":"urn:ietf:params:acme:error:unauthorized","detail":"104.21.63.218: Invalid response from http://a.example/.well-known/acme-challenge/x: 404"}}',
        '{"level":"error","ts":1789835180.5,"logger":"http.acme_client","msg":"validating authorization","identifier":"a.example","problem":{"type":"urn:ietf:params:acme:error:unauthorized","detail":"Cannot negotiate ALPN protocol"}}',
        '{"level":"error","ts":1789835181.0,"logger":"tls.obtain","msg":"could not get certificate from issuer","identifier":"a.example","issuer":"acme-v02.api.letsencrypt.org-directory","error":"HTTP 403 urn:ietf:params:acme:error:unauthorized - Cannot negotiate ALPN protocol"}',
        '{"level":"info","ts":1789835181.02,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"a.example","issuer":"local"}',
        'not json at all',
        '{"level":"info","ts":1789836000.0,"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"b.example","issuer":"acme-v02.api.letsencrypt.org-directory"}',
    ]
    log = cs.issuance_log(lines)
    assert log['a.example']['last_error'] == '104.21.63.218: Invalid response from http://a.example/.well-known/acme-challenge/x: 404' and log['a.example']['error_at'] == 1789835181.0  # the first challenge's verdict, not the fallback's
    assert log['a.example']['obtained'] == 'local' and log['b.example'] == {'last_error': '', 'error_at': None, 'obtained': 'acme-v02.api.letsencrypt.org-directory', 'obtained_at': 1789836000.0}
    assert 'c.example' not in log


def test_requesting_a_public_certificate_forgets_the_local_one_and_restarts_the_edge(tmp_path, monkeypatch):
    import reeve.host as hm
    proxy = tmp_path / 'proxy'; local = proxy / 'data/caddy/certificates/local/a.example'; local.mkdir(parents=True); (local / 'a.example.crt').write_text('x')
    monkeypatch.setattr(hm, 'PROXY', proxy)
    monkeypatch.setattr(hm, 'tls_settings', lambda: {'mode': 'public', 'email': ''})
    calls = []
    monkeypatch.setattr(hm, 'command', lambda args, timeout=120: calls.append(args) or '{"logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"a.example","issuer":"acme","ts":1}')
    monkeypatch.setattr(cs, 'served', lambda name: "issuer=O=Let's Encrypt\nnotAfter=Dec 18 16:09:28 2026 GMT\n")
    result = cs.request_public('a.example', ['a.example'], wait=5)
    assert not local.exists() and calls[0] == ['docker', 'restart', 'hosting-edge'] and result['state'] == 'public'
    assert result['issuance']['obtained'] == 'acme'
    with pytest.raises(ValueError, match='does not route'): cs.request_public('other.example', ['a.example'])
    monkeypatch.setattr(hm, 'tls_settings', lambda: {'mode': 'internal', 'email': ''})
    with pytest.raises(ValueError, match='off'): cs.request_public('a.example', ['a.example'])
