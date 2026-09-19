"""Secure mode: WireGuard for administration and an nftables firewall that leaves only the sites, customer
SFTP and the tunnel reachable from the internet. Three ordered steps from the Settings page: enable the
tunnel (nothing blocked yet, except the panel port from outside), lock down (only from over the tunnel, only
after a live handshake), confirm within fifteen minutes or the lockdown reverts. Break-glass: a single-use
token in a URL the edge logs and the worker acts on, opening port 22 to that address for thirty minutes.

State lives root-only under /srv/ops/panel/worker/secure/: state.json, the server and client keys, the
unlock token. The rules file is /etc/reeve/firewall.nft, loaded by reeve-firewall.service before Docker.
sshd is untouched: 22 keeps listening; the firewall hides it from the internet.
"""
import ipaddress
import json
import random
import re
import secrets
import subprocess
import time
from pathlib import Path

from .host import OPS, PROXY, command

ROOT = OPS / 'panel/worker/secure'
STATE = ROOT / 'state.json'
RULES = Path('/etc/reeve/firewall.nft')
UNIT = Path('/etc/systemd/system/reeve-firewall.service')
WG_CONF = Path('/etc/wireguard/wg0.conf')
WEB_DROPIN = Path('/etc/systemd/system/reeve-web.service.d/secure.conf')
UNMATCHED_LOG = PROXY / 'data/logs/_unmatched.log'
PUBLIC_TCP = (80, 443, 2222)
PANEL_PORT = 8088
PENDING_SECONDS = 900
HANDSHAKE_SECONDS = 180
UNLOCK_MINUTES = 30
STAGES = ('off', 'wireguard', 'locked-pending', 'locked')


# ---- pure pieces

def choose_network(rng=random):
    """A random 10.x.y.0/24 away from the common home and office ranges, and a random high UDP port."""
    return f'10.{rng.randint(100, 250)}.{rng.randint(1, 254)}.0/24', rng.randint(40000, 60000)


def client_address(subnet, index):
    return str(ipaddress.ip_network(subnet)[index])


def server_conf(state):
    lines = ['[Interface]', f"Address = {client_address(state['subnet'], 1)}/24", f"ListenPort = {state['port']}",
             f"PrivateKey = {state['server_private']}", '']
    for client in state['clients']:
        lines += ['[Peer]', f"# {client['name']}", f"PublicKey = {client['public']}", f"AllowedIPs = {client['address']}/32", '']
    return '\n'.join(lines)


def client_conf(state, client):
    return '\n'.join(['[Interface]', f"PrivateKey = {client['private']}", f"Address = {client['address']}/24", '',
                      '[Peer]', f"PublicKey = {state['server_public']}", f"Endpoint = {state['endpoint']}:{state['port']}",
                      f"AllowedIPs = {state['subnet']}", 'PersistentKeepalive = 25', ''])


def rules(stage, port):
    """The nftables table for a stage. `wireguard`: only the panel port is hidden from outside (the tunnel and
    loopback reach it). `locked`: the internet reaches 80, 443, 2222 and the tunnel port; the tunnel reaches
    everything; the break-glass sets open 22 to an address for a while. Both families, one table."""
    head = ['destroy table inet reeve', 'table inet reeve {',
            '  set public_tcp { type inet_service; elements = { ' + ', '.join(str(p) for p in PUBLIC_TCP) + ' } }',
            '  set unlocked4 { type ipv4_addr; flags timeout; }', '  set unlocked6 { type ipv6_addr; flags timeout; }']
    if stage in ('locked', 'locked-pending'):
        body = ['  chain input {', '    type filter hook input priority -10; policy accept;',
                '    iif "lo" accept', '    iifname "wg0" accept', '    ct state established,related accept', '    ct state invalid drop',
                '    meta l4proto icmp accept', '    meta l4proto ipv6-icmp accept', '    udp dport 68 accept',
                f'    udp dport {port} accept', '    ip saddr @unlocked4 tcp dport 22 accept', '    ip6 saddr @unlocked6 tcp dport 22 accept',
                '    counter drop', '  }',
                '  chain forward {', '    type filter hook forward priority -10; policy accept;',
                '    iifname "wg0" accept', '    oifname "wg0" accept', '    ct state established,related accept', '    ct state invalid drop',
                '    iifname "docker0" accept', '    iifname "br-*" accept', '    tcp dport @public_tcp accept', '    counter drop', '  }']
    else:
        body = ['  chain input {', '    type filter hook input priority -10; policy accept;',
                '    iif "lo" accept', '    iifname "wg0" accept', f'    tcp dport {PANEL_PORT} drop', '  }']
    return '\n'.join(head + body + ['}', ''])


UNIT_TEXT = '''[Unit]
Description=Reeve firewall (nftables)
Before=docker.service wg-quick@wg0.service
After=network-pre.target
DefaultDependencies=no

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/reeve/firewall.nft
ExecReload=/usr/sbin/nft -f /etc/reeve/firewall.nft
ExecStop=/usr/sbin/nft destroy table inet reeve

[Install]
WantedBy=multi-user.target
'''

WEB_DROPIN_TEXT = '''[Service]
ExecStart=
ExecStart=/opt/reeve/current/.venv/bin/python -m uvicorn reeve.web:app --factory --host 0.0.0.0 --port 8088 --no-proxy-headers --no-access-log
'''


def unlock_requests(lines, token):
    """Pure: the addresses that asked for the unlock URL with the current token, from the edge's log lines."""
    found = []
    for line in lines:
        try: event = json.loads(line)
        except ValueError: continue
        request = event.get('request') or {}
        uri = str(request.get('uri', ''))
        if not uri.startswith('/.reeve/unlock/'): continue
        if secrets.compare_digest(uri[len('/.reeve/unlock/'):].split('?')[0], token):
            found.append((float(event.get('ts', 0)), str(request.get('remote_ip', ''))))
    return found


def in_subnet(address, subnet):
    try: return ipaddress.ip_address(address.split('%')[0]) in ipaddress.ip_network(subnet)
    except ValueError: return False


# ---- state

def load():
    if not STATE.exists(): return {'stage': 'off'}
    try: return json.loads(STATE.read_text())
    except (OSError, ValueError): return {'stage': 'off'}


def save(state):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = STATE.with_name('.state.new'); tmp.write_text(json.dumps(state, indent=2)); tmp.chmod(0o600); tmp.replace(STATE)


def write_private(path, text):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.new'); tmp.write_text(text); tmp.chmod(0o600); tmp.replace(path)


def apply_rules(stage, port):
    write_private(RULES, rules(stage, port)); RULES.chmod(0o600)
    if not UNIT.exists():
        UNIT.write_text(UNIT_TEXT); command(['systemctl', 'daemon-reload']); command(['systemctl', 'enable', '--now', '--quiet', 'reeve-firewall.service'], timeout=30)
    command(['nft', '-f', str(RULES)])


def restart_web_soon():
    """The panel is restarted a few seconds after the request that asked for it has been answered: the request
    itself runs in that panel, and a restart inside it would cut the reply off."""
    command(['systemd-run', '--quiet', '--on-active=3', '--unit=reeve-web-restart', 'systemctl', 'restart', 'reeve-web.service'], timeout=30)


def keypair():
    private = command(['wg', 'genkey']).strip()
    public = subprocess.run(['wg', 'pubkey'], input=private + '\n', capture_output=True, text=True, check=True).stdout.strip()
    return private, public


def endpoint():
    try: return json.loads(command(['ip', '-j', 'route', 'get', '1.1.1.1']))[0].get('prefsrc') or ''
    except Exception: return ''


def write_wireguard(state):
    write_private(WG_CONF, server_conf(state))
    command(['systemctl', 'enable', '--quiet', 'wg-quick@wg0.service'])
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'wg-quick@wg0.service']).returncode == 0:
        # Peers change without the tunnel going down: the stripped configuration is synced into the live interface.
        stripped = command(['wg-quick', 'strip', 'wg0'], timeout=30)
        subprocess.run(['wg', 'syncconf', 'wg0', '/dev/stdin'], input=stripped, text=True, check=True, timeout=30)
    else:
        command(['systemctl', 'start', 'wg-quick@wg0.service'], timeout=60)


def handshakes():
    """Public key → seconds since the last handshake, for every peer that ever completed one."""
    try: text = command(['wg', 'show', 'wg0', 'latest-handshakes'], timeout=10)
    except RuntimeError: return {}
    now = time.time(); result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) > 0: result[parts[0]] = now - int(parts[1])
    return result


# ---- the steps

def enable(name='first'):
    """Step 1: keys, one client, the tunnel up, the panel reachable on the tunnel address, its port hidden from outside."""
    state = load()
    if state['stage'] != 'off': raise ValueError('WireGuard is already enabled')
    subnet, port = choose_network()
    server_private, server_public = keypair()
    client_private, client_public = keypair()
    state = {'stage': 'wireguard', 'subnet': subnet, 'port': port, 'endpoint': endpoint(), 'server_private': server_private, 'server_public': server_public,
             'clients': [{'name': name, 'address': client_address(subnet, 2), 'private': client_private, 'public': client_public, 'created': time.time()}],
             'token': secrets.token_urlsafe(24), 'pending_until': None, 'enabled_at': time.time(), 'events': []}
    try:
        apply_rules('wireguard', port)       # the panel port is hidden from outside before the panel listens beyond loopback
        write_wireguard(state)
        WEB_DROPIN.parent.mkdir(mode=0o755, parents=True, exist_ok=True); WEB_DROPIN.write_text(WEB_DROPIN_TEXT)
        command(['systemctl', 'daemon-reload']); restart_web_soon()
    except Exception:
        # Nothing half-done stays: the table, the tunnel and the panel binding go back to how they were.
        subprocess.run(['nft', 'destroy', 'table', 'inet', 'reeve'], capture_output=True)
        subprocess.run(['systemctl', 'disable', '--now', '--quiet', 'wg-quick@wg0.service'], capture_output=True)
        if WEB_DROPIN.exists(): WEB_DROPIN.unlink(); subprocess.run(['systemctl', 'daemon-reload'], capture_output=True); subprocess.run(['systemctl', 'restart', 'reeve-web.service'], capture_output=True)
        raise
    save(state); note(state, 'WireGuard enabled')
    return status()


def add_client(name):
    state = load()
    if state['stage'] == 'off': raise ValueError('Enable WireGuard first')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,30}', name): raise ValueError('A client name is lowercase letters, digits and hyphens')
    if any(c['name'] == name for c in state['clients']): raise ValueError('A client with that name exists')
    if len(state['clients']) >= 200: raise ValueError('Too many clients')
    used = {c['address'] for c in state['clients']}
    index = next(i for i in range(2, 254) if client_address(state['subnet'], i) not in used)
    private, public = keypair()
    state['clients'].append({'name': name, 'address': client_address(state['subnet'], index), 'private': private, 'public': public, 'created': time.time()})
    save(state); write_wireguard(state); note(state, 'client ' + name + ' added')
    return status()


def client_config(name):
    state = load()
    client = next((c for c in state.get('clients', []) if c['name'] == name), None)
    if not client: raise ValueError('Unknown client')
    return {'name': name, 'text': client_conf(state, client)}


def client_qr(name):
    text = client_config(name)['text']
    svg = subprocess.run(['qrencode', '-t', 'SVG', '-o', '-', '-m', '1'], input=text, capture_output=True, text=True, timeout=10)
    return svg.stdout if svg.returncode == 0 else ''


def lockdown(requester):
    """Step 2: only from over the tunnel, only after a live handshake. Reverts unless confirmed in time."""
    state = load()
    if state['stage'] == 'off': raise ValueError('Enable WireGuard first')
    if state['stage'] == 'locked': raise ValueError('Already locked down')
    if not in_subnet(requester, state['subnet']): raise ValueError('Lock down from over the WireGuard tunnel, so the path is proven first')
    live = handshakes()
    if not any(seconds < HANDSHAKE_SECONDS for seconds in live.values()): raise ValueError('No live WireGuard handshake in the last three minutes')
    state['stage'] = 'locked-pending'; state['pending_until'] = time.time() + PENDING_SECONDS
    save(state); apply_rules('locked-pending', state['port']); note(state, 'locked down, awaiting confirmation')
    return status()


def confirm(requester):
    """Step 3: from over the tunnel, before the timer runs out."""
    state = load()
    if state['stage'] != 'locked-pending': raise ValueError('Nothing awaits confirmation')
    if not in_subnet(requester, state['subnet']): raise ValueError('Confirm from over the WireGuard tunnel')
    state['stage'] = 'locked'; state['pending_until'] = None
    save(state); apply_rules('locked', state['port']); note(state, 'lockdown confirmed')
    return status()


def revert(reason='reverted by the operator'):
    """Back to the tunnel-only stage: the internet reaches everything again except the panel port."""
    state = load()
    if state['stage'] in ('off', 'wireguard'): return status()
    state['stage'] = 'wireguard'; state['pending_until'] = None
    save(state); apply_rules('wireguard', state['port']); note(state, reason)
    return status()


def disable():
    """Everything off: the table gone, the tunnel down, the panel back on loopback only. Keys are kept."""
    state = load()
    if state['stage'] == 'off': return status()
    subprocess.run(['nft', 'destroy', 'table', 'inet', 'reeve'], capture_output=True)
    subprocess.run(['systemctl', 'disable', '--now', '--quiet', 'reeve-firewall.service', 'wg-quick@wg0.service'], capture_output=True)
    if WEB_DROPIN.exists(): WEB_DROPIN.unlink()
    command(['systemctl', 'daemon-reload']); restart_web_soon()
    state['stage'] = 'off'; state['pending_until'] = None; save(state); note(state, 'secure mode disabled')
    return status()


def close_unlock():
    state = load()
    for name in ('unlocked4', 'unlocked6'): subprocess.run(['nft', 'flush', 'set', 'inet', 'reeve', name], capture_output=True)
    state['unlock'] = None; save(state); note(state, 'port 22 closed again')
    return status()


def note(state, text):
    state.setdefault('events', []).append({'at': time.time(), 'text': text}); state['events'] = state['events'][-50:]; save(state)


# ---- the worker's minute pass

def tick():
    state = load()
    if state['stage'] == 'off': return
    now = time.time()
    if state['stage'] == 'locked-pending' and state.get('pending_until') and now > state['pending_until']:
        revert('lockdown reverted: not confirmed within fifteen minutes'); state = load()
    if state.get('unlock') and now > state['unlock']['until']:
        state['unlock'] = None; save(state)
    # Break-glass: the edge only logs the request; the worker opens 22 to that address alone, for a while.
    try: from .site_logs import tail_lines
    except ImportError: return
    since = state.get('unlock_seen', 0.0)
    asked = [(ts, ip) for ts, ip in unlock_requests(tail_lines(UNMATCHED_LOG, 512 * 1024), state['token']) if ts > since and ip]
    if asked:
        ts, address = asked[-1]
        family = 'unlocked6' if ':' in address else 'unlocked4'
        try:
            command(['nft', 'add', 'element', 'inet', 'reeve', family, '{ ' + address + f' timeout {UNLOCK_MINUTES}m ' + '}'])
            state['unlock'] = {'address': address, 'until': now + UNLOCK_MINUTES * 60}
            state['token'] = secrets.token_urlsafe(24)   # single use
            note(state, f'port 22 opened to {address} by the unlock token for {UNLOCK_MINUTES} minutes; a new token was issued')
        except RuntimeError as exc:
            note(state, 'unlock request seen but the firewall refused: ' + str(exc)[:200])
        state['unlock_seen'] = ts; save(state)


def status(requester=None):
    state = load()
    live = handshakes() if state['stage'] != 'off' else {}
    clients = [{'name': c['name'], 'address': c['address'], 'created': c['created'], 'handshake_seconds': live.get(c['public'])} for c in state.get('clients', [])]
    return {'stage': state['stage'], 'subnet': state.get('subnet'), 'port': state.get('port'), 'endpoint': state.get('endpoint'), 'clients': clients,
            'pending_until': state.get('pending_until'), 'unlock': state.get('unlock'), 'events': state.get('events', [])[-10:],
            'live_handshake': any(s < HANDSHAKE_SECONDS for s in live.values()),
            'from_tunnel': bool(requester and state.get('subnet') and in_subnet(requester, state['subnet'])),
            'public_tcp': list(PUBLIC_TCP), 'panel_port': PANEL_PORT, 'unlock_url': f"http://{state.get('endpoint') or '<server>'}/.reeve/unlock/<token>"}


def token():
    state = load()
    if state['stage'] == 'off': raise ValueError('Secure mode is off')
    return {'token': state['token'], 'url': f"http://{state.get('endpoint') or '<server>'}/.reeve/unlock/{state['token']}"}
