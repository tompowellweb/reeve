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
