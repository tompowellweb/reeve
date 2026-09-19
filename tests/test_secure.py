"""Secure mode: the rules, the tunnel configuration, the ordered steps and the break-glass, with the host faked."""
import json
import random
import time

import pytest

from reeve import secure


def test_network_and_port_stay_in_their_ranges_and_addresses_follow():
    for seed in range(50):
        subnet, port = secure.choose_network(random.Random(seed))
        a, b, c, d = subnet.split('/')[0].split('.')
        assert a == '10' and 100 <= int(b) <= 250 and 1 <= int(c) <= 254 and d == '0' and subnet.endswith('/24') and 40000 <= port <= 60000
    assert secure.client_address('10.123.45.0/24', 1) == '10.123.45.1' and secure.client_address('10.123.45.0/24', 2) == '10.123.45.2'
    assert secure.in_subnet('10.123.45.7', '10.123.45.0/24') and not secure.in_subnet('10.123.46.7', '10.123.45.0/24') and not secure.in_subnet('garbage', '10.123.45.0/24')


def test_rules_hide_only_the_panel_before_lockdown_and_everything_but_the_sites_after():
    before = secure.rules('wireguard', 51111)
    assert 'tcp dport 8088 drop' in before and 'chain forward' not in before and before.startswith('destroy table inet reeve\ntable inet reeve {')
    after = secure.rules('locked', 51111)
    for needed in ('udp dport 51111 accept', 'elements = { 80, 443, 2222 }', 'iifname "wg0" accept', 'ip saddr @unlocked4 tcp dport 22 accept',
                   'ip6 saddr @unlocked6 tcp dport 22 accept', 'meta l4proto ipv6-icmp accept', 'iifname "br-*" accept', 'tcp dport @public_tcp accept', 'counter drop'):
        assert needed in after, needed
    assert after.count('counter drop') == 2 and 'tcp dport 22 accept\n' not in after.replace('@unlocked4 tcp dport 22 accept\n', '').replace('@unlocked6 tcp dport 22 accept\n', '')
    assert secure.rules('locked-pending', 51111) == after


def test_wireguard_configurations_carry_the_tunnel_and_every_peer():
    state = {'subnet': '10.200.7.0/24', 'port': 45678, 'endpoint': '203.0.113.5', 'server_private': 'SPRIV', 'server_public': 'SPUB',
             'clients': [{'name': 'first', 'address': '10.200.7.2', 'private': 'CPRIV', 'public': 'CPUB'}, {'name': 'laptop', 'address': '10.200.7.3', 'private': 'LPRIV', 'public': 'LPUB'}]}
    server = secure.server_conf(state)
    assert 'Address = 10.200.7.1/24' in server and 'ListenPort = 45678' in server and server.count('[Peer]') == 2 and 'AllowedIPs = 10.200.7.3/32' in server
    client = secure.client_conf(state, state['clients'][1])
    assert 'PrivateKey = LPRIV' in client and 'Address = 10.200.7.3/24' in client and 'Endpoint = 203.0.113.5:45678' in client and 'AllowedIPs = 10.200.7.0/24' in client and 'PublicKey = SPUB' in client


def test_unlock_requests_match_the_current_token_only():
    good = json.dumps({'ts': 10.0, 'request': {'uri': '/.reeve/unlock/abc123?x=1', 'remote_ip': '203.0.113.9'}})
    wrong = json.dumps({'ts': 11.0, 'request': {'uri': '/.reeve/unlock/nope', 'remote_ip': '203.0.113.10'}})
    other = json.dumps({'ts': 12.0, 'request': {'uri': '/', 'remote_ip': '203.0.113.11'}})
    assert secure.unlock_requests([good, wrong, other, 'junk'], 'abc123') == [(10.0, '203.0.113.9')]


@pytest.fixture
def box(tmp_path, monkeypatch):
    root = tmp_path / 'secure'
    monkeypatch.setattr(secure, 'ROOT', root); monkeypatch.setattr(secure, 'STATE', root / 'state.json')
    monkeypatch.setattr(secure, 'RULES', tmp_path / 'reeve/firewall.nft'); monkeypatch.setattr(secure, 'UNIT', tmp_path / 'reeve-firewall.service')
    monkeypatch.setattr(secure, 'WG_CONF', tmp_path / 'wg0.conf'); monkeypatch.setattr(secure, 'WEB_DROPIN', tmp_path / 'dropin/secure.conf')
    monkeypatch.setattr(secure, 'UNMATCHED_LOG', tmp_path / '_unmatched.log')
    calls = []
    keys = iter(['SPRIV', 'CPRIV', 'LPRIV'])
    monkeypatch.setattr(secure, 'keypair', lambda: (lambda k: (k, k.replace('PRIV', 'PUB')))(next(keys)))
    monkeypatch.setattr(secure, 'endpoint', lambda: '203.0.113.5')
    monkeypatch.setattr(secure, 'command', lambda args, timeout=120: calls.append(args) or '')
    monkeypatch.setattr(secure.subprocess, 'run', lambda *a, **k: type('R', (), {'returncode': 1, 'stdout': ''})())
    live = {}
    monkeypatch.setattr(secure, 'handshakes', lambda: dict(live))
    return calls, live


def test_the_three_steps_are_ordered_and_guarded(box):
    calls, live = box
    assert secure.status()['stage'] == 'off'
    with pytest.raises(ValueError, match='Enable WireGuard first'): secure.lockdown('10.1.1.1')
    status = secure.enable()
    assert status['stage'] == 'wireguard' and status['clients'][0]['address'].endswith('.2') and status['endpoint'] == '203.0.113.5'
    assert secure.RULES.read_text().count('tcp dport 8088 drop') == 1 and secure.UNIT.exists() and secure.WEB_DROPIN.exists()
    assert ['nft', '-f', str(secure.RULES)] in calls and any(c[0] == 'systemd-run' and 'reeve-web.service' in c for c in calls)
    order = [i for i, c in enumerate(calls) if c[:2] == ['nft', '-f'] or c[0] == 'systemd-run']
    assert calls[order[0]][:2] == ['nft', '-f']   # the panel port is hidden before the panel listens beyond loopback
    with pytest.raises(ValueError, match='already enabled'): secure.enable()
    subnet = status['subnet']; inside = secure.client_address(subnet, 2)
    with pytest.raises(ValueError, match='over the WireGuard tunnel'): secure.lockdown('203.0.113.77')
    with pytest.raises(ValueError, match='No live WireGuard handshake'): secure.lockdown(inside)
    live['CPUB'] = 30.0
    status = secure.lockdown(inside)
    assert status['stage'] == 'locked-pending' and status['pending_until'] > time.time() + 800 and 'udp dport' in secure.RULES.read_text()
    with pytest.raises(ValueError, match='Confirm from over'): secure.confirm('203.0.113.77')
    # Not confirmed in time: the pass reverts to the tunnel-only stage.
    state = secure.load(); state['pending_until'] = time.time() - 1; secure.save(state)
    secure.tick()
    assert secure.status()['stage'] == 'wireguard' and 'reverted' in secure.status()['events'][-1]['text'] and 'tcp dport 8088 drop' in secure.RULES.read_text()
    secure.lockdown(inside); assert secure.confirm(inside)['stage'] == 'locked' and secure.status()['pending_until'] is None
    # Another client keeps the tunnel up while its peers are synced.
    status = secure.add_client('laptop')
    assert [c['name'] for c in status['clients']] == ['first', 'laptop'] and status['clients'][1]['address'].endswith('.3')
    with pytest.raises(ValueError, match='exists'): secure.add_client('laptop')
    with pytest.raises(ValueError, match='lowercase'): secure.add_client('Bad Name')
    assert 'PrivateKey = LPRIV' in secure.client_config('laptop')['text']
    assert secure.revert()['stage'] == 'wireguard'


def test_break_glass_opens_22_to_the_asker_once_and_rotates_the_token(box):
    calls, live = box
    secure.enable()
    token = secure.token()['token']; assert secure.token()['url'].endswith('/.reeve/unlock/' + token)
    secure.UNMATCHED_LOG.write_text(json.dumps({'ts': time.time(), 'request': {'uri': '/.reeve/unlock/' + token, 'remote_ip': '203.0.113.9'}}) + '\n')
    secure.tick()
    status = secure.status()
    assert status['unlock']['address'] == '203.0.113.9' and status['unlock']['until'] > time.time() + 1700
    assert any(c[:4] == ['nft', 'add', 'element', 'inet'] and 'unlocked4' in c and '203.0.113.9' in c[-1] for c in calls)
    assert secure.token()['token'] != token   # single use
    secure.tick()   # the same log line is not acted on twice
    assert sum(1 for c in calls if c[:3] == ['nft', 'add', 'element']) == 1
    secure.UNMATCHED_LOG.write_text(json.dumps({'ts': time.time() + 1, 'request': {'uri': '/.reeve/unlock/' + secure.token()['token'], 'remote_ip': '2001:db8::9'}}) + '\n')
    secure.tick()
    assert any('unlocked6' in c for c in calls if c[:3] == ['nft', 'add', 'element'])
    assert secure.close_unlock()['unlock'] is None
