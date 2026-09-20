import json
import uuid

import pytest

from reeve import recovery_context as context
from reeve.core import Ledger


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(context, 'STORE', tmp_path / 'context')
    monkeypatch.setattr(context, 'trusted', lambda *a, **k: None)
    monkeypatch.setattr('reeve.host.trusted', lambda *a, **k: None)
    monkeypatch.setattr(context, 'regular', lambda p: p.read_bytes())
    ledger = Ledger(tmp_path / 'jobs.db')
    row = ledger.submit(str(uuid.uuid4()), {'name': 'context', 'domain': 'context.example.com'})
    ledger.update(row['id'], 'succeeded', 'published')
    return ledger, row


def test_owner_answers_do_not_require_inventory_or_block_backups(saved):
    ledger, row = saved
    assert context.read(row)['external'] == 'unknown'
    first = context.save(ledger, row, {'revision': '', 'external': 'no', 'notes': 'Only files here.'})
    assert first['external'] == 'no'
    with ledger.db() as db: assert not ledger.backup_pending(db, row['id'])
    second = context.save(ledger, row, {'revision': first['revision'], 'checks': 'Open a recent uploaded photo.'})
    assert second['notes'] == first['notes'] and second['external'] == 'no'
    assert second['checks'] == 'Open a recent uploaded photo.'
    assert context.summary({})['title'] == 'Not yet protected'
    assert 'restore_verified' not in second and 'backup_complete' not in second
    same = context.save(ledger, row, {'revision': second['revision'], 'checks': second['checks']})
    assert same == second


def test_unknown_and_stale_forms_preserve_notes(saved):
    ledger, row = saved
    first = context.save(ledger, row, {'revision': '', 'external': 'unknown', 'notes': 'Ask the developer.'})
    with pytest.raises(ValueError, match='another window'):
        context.save(ledger, row, {'revision': '', 'external': 'no', 'notes': ''})
    assert context.read(row) == first
    for bad in ({'revision': first['revision'], 'external': 'maybe', 'notes': ''},
                {'revision': first['revision'], 'checks': 'x' * 1201},
                {'revision': first['revision'], 'checks': '', 'command': 'do something'}):
        with pytest.raises(ValueError): context.save(ledger, row, bad)
    assert context.read(row) == first


def test_protection_uses_available_copies_and_never_a_plan_claim():
    assert context.summary({'available': False, 'last_success': {'id': 'missing'}})['title'] == 'Not yet protected'
    assert context.summary({'available': True, 'last_success': {'id': 'local'}})['title'] == 'Partially protected'
    assert context.summary({'remote': {'last_copy': {'verified': 1}}})['title'] == 'Partially protected'
    assert context.summary({'error': 'unavailable', 'available': True})['title'] == 'Backup status unavailable'


def test_notes_and_server_destination_require_authentication_csrf_and_escape_output(saved, tmp_path):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from reeve.auth import Auth
    from reeve.core import DEFAULTS
    from reeve.web import create_app
    from reeve.worker import dispatch
    from tests.test_web import csrf, login
    ledger, row = saved
    auth_path = tmp_path / 'auth.db'; Auth(auth_path).set_password('test-password-unique-123')
    host = SimpleNamespace(defaults=DEFAULTS, health=lambda row: {'application': 'unknown', 'container': 'absent', 'quota': None})
    client = TestClient(create_app(auth_path, lambda message: dispatch(message, ledger, host)))
    url = '/sites/context/recovery/context'
    assert client.post(url).status_code == 401
    assert client.get('/backups').status_code == 401
    assert client.post('/backups/copy').status_code == 401
    login(client); page = client.get('/sites/context')
    assert 'concerns-table' in page.text and 'Edit recovery plan' not in page.text
    assert 'storage_class_' not in page.text and 'recovery-plan-dialog' not in page.text
    assert client.post(url).status_code == 403
    assert client.post('/backups/copy').status_code == 403
    data = {'csrf': csrf(page), 'section': 'external', 'revision': '', 'external': 'unknown', 'notes': '<script>not_executed()</script>'}
    reply = client.post(url, data=data)
    assert reply.status_code == 200 and 'Not yet protected' in reply.text
    assert '&lt;script&gt;not_executed' in reply.text and '<script>not_executed' not in reply.text
    reply = client.post(url, data=data)
    assert 'These notes changed' in reply.text
    assert 'data-concern-panel ><td' in reply.text  # Rejected editor stays open inline.
    assert '&lt;script&gt;not_executed' in reply.text
    assert client.post('/sites/context/recovery/plan', data={'csrf': csrf(page)}).status_code == 404
    destination = client.get('/backups')
    assert destination.status_code == 200 and 'Any number of restic repositories' in destination.text and 'No destination is connected yet' in destination.text
    assert 'Copy now' not in destination.text
    assert client.post('/backups/copy', data={'csrf': csrf(page)}).status_code == 200
    assert 'Connect and enable a destination first' in client.post('/backups/copy', data={'csrf': csrf(page)}).text
