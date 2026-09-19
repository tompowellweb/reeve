"""Outbound mail: a send-only Postfix relay that every managed PHP site reaches as `mail`.

Design notes in docs/design.md. The relay (`hosting-mail`) publishes no port. It has its own
bridge network for delivery and joins each PHP site's private backend network under the alias
`mail`; the site's `hosting-sendmail` shim (mounted into the PHP container) submits `mail()`
there, and an application's own SMTP settings can point at `mail`, port 25, no authentication.
Postfix accepts a message from a site's network only if the envelope sender's domain is one of
that site's hostnames, and limits each site (one client address per backend network) to a
number of messages an hour. Delivery is direct from the server by default; the server setting
`mail.mode` can point everything at an authenticated relay or, on the VM, at a Mailpit sink.

The mail page reads the queue with `postqueue -j` and the per-delivery outcomes from the
relay's bounded log, grouped by site, so a site sending far above its habit stands out.
"""
import ipaddress
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .host import OPS, SITES, atomic, command, trusted

ROOT = OPS / 'panel/worker/mail'
STATE = OPS / 'mail'
GENERATED = ROOT / 'generated'
CONTAINER = 'hosting-mail'
SINK = 'hosting-mailpit'
NETWORK = 'hosting-mail-net'
SHIM = OPS / 'panel/mail/hosting-sendmail'
TEMPLATES = Path(__file__).resolve().parent.parent / 'templates/mail'
DEBIAN = 'debian:trixie-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132'
IMAGE_VERSION = 'mail-v1'
DOCKERFILE = f'''FROM {DEBIAN}
RUN apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends postfix ca-certificates \\
 && rm -rf /var/lib/apt/lists/* \\
 && postconf -F '*/*/chroot=n'
'''
DEFAULTS = {'mode': 'direct', 'relayhost': '', 'hostname': '', 'public_ip': '', 'rate_per_hour': 100, 'sink_image': 'axllent/mailpit@sha256:fb84cfb1c3a33007cfdee8ba5a77b8c6c894de5b1a368372ddc5c965cf5c0b6d'}
CAPS = ['CHOWN', 'DAC_OVERRIDE', 'DAC_READ_SEARCH', 'FOWNER', 'FSETID', 'SETGID', 'SETUID', 'KILL', 'NET_BIND_SERVICE']


def settings(document=None):
    """The server's mail settings from server.yaml (or the given document), validated."""
    config = OPS / 'server.yaml'
    values = (document if document is not None else ((yaml.safe_load(config.read_text()) or {}) if config.exists() else {})).get('mail', {})
    if not isinstance(values, dict) or values.keys() - DEFAULTS.keys(): raise ValueError('Invalid mail settings in server.yaml')
    result = {**DEFAULTS, **values}
    if result['mode'] is False: result['mode'] = 'off'  # YAML reads a bare `off` as a boolean
    if result['mode'] not in ('off', 'direct', 'relay', 'sink'): raise ValueError('mail.mode must be off, direct, relay or sink')
    if result['mode'] == 'relay' and not re.fullmatch(r'\[?[A-Za-z0-9.-]+\]?(:\d{1,5})?', str(result['relayhost'])): raise ValueError('mail.relayhost must be host or [host]:port')
    if type(result['rate_per_hour']) is not int or not 1 <= result['rate_per_hour'] <= 100000: raise ValueError('mail.rate_per_hour must be a whole number')
    for key in ('hostname', 'public_ip', 'sink_image'):
        if not isinstance(result[key], str) or '\n' in result[key]: raise ValueError('mail.' + key + ' must be text')
    if result['hostname'] and not re.fullmatch(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+', result['hostname']): raise ValueError('mail.hostname must be a hostname')
    if result['public_ip']: ipaddress.ip_address(result['public_ip'])
    return result


def registry():
    path = ROOT / 'sites.json'
    if not path.exists(): return {}
    trusted(path); return json.loads(path.read_text())


def save_registry(entries):
    ROOT.mkdir(mode=0o700, exist_ok=True); trusted(ROOT, directory=True)
    atomic(ROOT / 'sites.json', json.dumps(entries, indent=2, sort_keys=True))


MAX_SENDERS = 20


def validate_senders(data):
    """Extra addresses or domains a site may send as, one per line, beyond its own hostnames."""
    if not isinstance(data, dict) or set(data) != {'senders'} or not isinstance(data['senders'], (str, list)):
        raise ValueError('Allowed senders need the addresses text')
    text = data['senders'] if isinstance(data['senders'], str) else '\n'.join(s for s in data['senders'] if isinstance(s, str))
    if len(text) > 8192: raise ValueError('The allowed senders text is too long')
    senders = []
    for line in text.replace('\r\n', '\n').split('\n'):
        line = line.strip().lower()
        if not line or line.startswith('#'): continue
        if not re.fullmatch(r'([a-z0-9._%+-]+@)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+', line):
            raise ValueError('Each allowed sender is an address or a domain: ' + line[:60])
        if line not in senders: senders.append(line)
    if len(senders) > MAX_SENDERS: raise ValueError(f'At most {MAX_SENDERS} allowed senders')
    return {'senders': senders}


def extra_senders(row):
    path = SITES / row['name'] / 'hosting.yaml'
    if not path.exists(): return []
    trusted(path)
    return list((yaml.safe_load(path.read_text()) or {}).get('mail_senders') or [])


def php_sites(ledger):
    """Managed PHP sites that have a backend network: the relay's clients."""
    result = {}
    for row in ledger.list():
        if json.loads(row['payload']).get('runtime') != 'php' or row['state'] != 'succeeded': continue
        result[row['name']] = {'id': row['id'], 'domains': ledger.domains(row), 'senders': extra_senders(row)}
    return result


def subnet(name):
    info = json.loads(command(['docker', 'network', 'inspect', 'hosting-backend-' + name]))[0]
    return info['IPAM']['Config'][0]['Subnet']


def hostname(config):
    if config['hostname']: return config['hostname']
    return Path('/etc/hostname').read_text().strip() if Path('/etc/hostname').exists() else 'hosting'


def render(config, entries):
    """main.cf, master.cf and the access maps for these sites (name -> subnet, domains)."""
    names = sorted(entries)
    classes = ' '.join('site_' + n.replace('-', '_') for n in names)
    lines = ''.join(f"site_{n.replace('-', '_')} = check_sender_access texthash:/etc/postfix/hosting/senders-{n}, check_sender_access static:{{REJECT the sender address must be at one of this site's domains}}\n" for n in names)
    relayhost = {'off': '', 'direct': '', 'relay': config['relayhost'], 'sink': f'[{SINK}]:1025'}[config['mode']]
    tls = 'may' if config['mode'] != 'sink' else 'none'
    main = f'''# Generated by the hosting panel. Send-only relay; see docs/design.md.
compatibility_level = 3.6
maillog_file = /var/spool/postfix/hosting-log/mail.log
maillog_file_rotate_suffix = %Y%m%d
queue_directory = /var/spool/postfix
myhostname = {hostname(config)}
myorigin = $myhostname
mydestination =
inet_interfaces = all
inet_protocols = ipv4
mynetworks = 127.0.0.0/8
local_transport = error:local delivery is not offered
alias_maps =
relayhost = {relayhost}
smtp_helo_name = $myhostname
smtp_tls_security_level = {tls}
smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt
smtp_tls_loglevel = 1
smtpd_banner = $myhostname ESMTP
smtpd_helo_required = yes
disable_vrfy_command = yes
smtpd_tls_security_level = none
message_size_limit = 26214400
bounce_queue_lifetime = 1d
maximal_queue_lifetime = 2d
anvil_rate_time_unit = 3600s
smtpd_client_message_rate_limit = {config['rate_per_hour']}
smtpd_client_recipient_rate_limit = {config['rate_per_hour'] * 4}
smtpd_client_restrictions = check_client_access cidr:/etc/postfix/hosting/clients.cidr, reject
smtpd_helo_restrictions = permit
smtpd_sender_restrictions = check_client_access cidr:/etc/postfix/hosting/classes.cidr, reject
smtpd_recipient_restrictions = permit
smtpd_relay_restrictions = check_client_access cidr:/etc/postfix/hosting/clients.cidr, reject
smtpd_restriction_classes = {classes}
{lines}'''
    master = '''# Generated by the hosting panel: one unprivileged submission listener, no chroot, no local delivery.
smtp      inet  n       -       n       -       -       smtpd
pickup    unix  n       -       n       60      1       pickup
cleanup   unix  n       -       n       -       0       cleanup
qmgr      unix  n       -       n       300     1       qmgr
tlsmgr    unix  -       -       n       1000?   1       tlsmgr
rewrite   unix  -       -       n       -       -       trivial-rewrite
bounce    unix  -       -       n       -       0       bounce
defer     unix  -       -       n       -       0       bounce
trace     unix  -       -       n       -       0       bounce
verify    unix  -       -       n       -       1       verify
flush     unix  n       -       n       1000?   0       flush
proxymap  unix  -       -       n       -       -       proxymap
proxywrite unix -       -       n       -       1       proxymap
smtp      unix  -       -       n       -       -       smtp
relay     unix  -       -       n       -       -       smtp
showq     unix  n       -       n       -       -       showq
error     unix  -       -       n       -       -       error
retry     unix  -       -       n       -       -       error
discard   unix  -       -       n       -       -       discard
local     unix  -       n       n       -       -       local
virtual   unix  -       n       n       -       -       virtual
lmtp      unix  -       -       n       -       -       lmtp
anvil     unix  -       -       n       -       1       anvil
scache    unix  -       -       n       -       1       scache
postlog   unix-dgram n  -       n       -       1       postlogd
'''
    maps = {'clients.cidr': ''.join(f"{entries[n]['subnet']} OK\n" for n in names) + '0.0.0.0/0 REJECT not a hosted site\n',
            'classes.cidr': ''.join(f"{entries[n]['subnet']} site_{n.replace('-', '_')}\n" for n in names) + '0.0.0.0/0 REJECT not a hosted site\n'}
    for n in names:
        maps['senders-' + n] = ''.join(f'{d} OK\n' for d in [*entries[n]['domains'], *entries[n].get('senders', [])])
    return {'main.cf': main, 'master.cf': master, 'maps': maps}


def write_generated(files):
    """The generated tree is bind-mounted whole into the relay, so directories are never removed and
    files are replaced inside them (a rename within a mounted directory is visible; a replaced
    directory or a replaced single mounted file is not)."""
    GENERATED.mkdir(mode=0o755, exist_ok=True, parents=True); trusted(GENERATED, directory=True)
    os.chmod(GENERATED, 0o755)
    for name in ('main.cf', 'master.cf'): atomic(GENERATED / name, files[name], 0o444)
    maps = GENERATED / 'maps'
    maps.mkdir(mode=0o755, exist_ok=True); trusted(maps, directory=True); os.chmod(maps, 0o755)
    for name, text in files['maps'].items(): atomic(maps / name, text, 0o444)
    for stale in maps.iterdir():
        if stale.name not in files['maps']: stale.unlink()
    atomic(GENERATED / 'entrypoint.sh', (TEMPLATES / 'entrypoint.sh').read_text(), 0o555)


def image(step=lambda value: None):
    ROOT.mkdir(mode=0o700, exist_ok=True); trusted(ROOT, directory=True)
    saved = ROOT / 'image.json'
    if saved.exists():
        trusted(saved); result = json.loads(saved.read_text())
        if result.get('version') == IMAGE_VERSION and command(['docker', 'image', 'inspect', result['image_id'], '--format', '{{.Id}}']).strip() == result['image_id']:
            return result
    context = ROOT / 'build'; context.mkdir(mode=0o700, exist_ok=True); trusted(context, directory=True)
    atomic(context / 'Dockerfile', DOCKERFILE); atomic(context / '.dockerignore', '*\n!Dockerfile\n')
    step('building the mail relay image')
    tag = 'hosting-mail:' + IMAGE_VERSION
    command(['docker', 'build', '--tag', tag, '--file', context / 'Dockerfile', context], timeout=900)
    image_id = command(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']).strip()
    result = {'version': IMAGE_VERSION, 'image_id': image_id}
    atomic(saved, json.dumps(result, indent=2))
    return result


def install_shim():
    """The sendmail shim the PHP containers mount, from this release's template, at a stable path.

    Written in place: the file is bind-mounted into running containers, and a replacement by rename
    would leave them on the old inode until recreated."""
    SHIM.parent.mkdir(mode=0o755, exist_ok=True); trusted(SHIM.parent, directory=True)
    text = (TEMPLATES / 'hosting-sendmail').read_text()
    if SHIM.exists():
        trusted(SHIM)
        if SHIM.read_text() != text:
            with open(SHIM, 'r+') as stream:
                stream.seek(0); stream.truncate(); stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.chmod(SHIM, 0o755)
    else: atomic(SHIM, text, 0o755)
    return SHIM


def shim_mount():
    """What a PHP container mounts, once the relay has been set up on this server."""
    return f'{SHIM}:/usr/local/bin/hosting-sendmail:ro' if SHIM.exists() else None


def ensure_network():
    if not command(['docker', 'network', 'ls', '-q', '--filter', 'name=^' + NETWORK + '$']).strip():
        command(['docker', 'network', 'create', '--label', 'hosting.mail=network', NETWORK])


def deploy_sink(host, config):
    """On the VM: a Mailpit sink on the relay's network, its page on loopback only."""
    if host.inspect(SINK): command(['docker', 'rm', '--force', SINK])
    if config['mode'] != 'sink': return
    store = STATE / 'sink'; store.mkdir(mode=0o777, exist_ok=True); os.chmod(store, 0o777)  # the sink's own user writes here; rehearsal data only
    from .content_site import mount
    args = ['docker', 'create', '--name', SINK, '--label', 'hosting.mail=sink', '--network', NETWORK, '--restart', 'unless-stopped',
            '--publish', '127.0.0.1:8025:8025', '--read-only', '--tmpfs', '/tmp', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
            '--log-driver', 'local', '--log-opt', 'max-size=2m', '--log-opt', 'max-file=2', '--env', 'MP_SMTP_BIND_ADDR=0.0.0.0:1025',
            '--env', 'MP_MAX_MESSAGES=5000', '--env', 'MP_DATABASE=/data/mailpit.db']
    mount(args, store, '/data', False)
    command([*args, config['sink_image']])
    command(['docker', 'start', SINK])


def generate(config, sites):
    """The registry and generated files for these sites (name -> id, domains); subnets read from Docker."""
    entries = {name: {'id': site['id'], 'domains': site['domains'], 'senders': site.get('senders', []), 'subnet': subnet(name)} for name, site in sites.items()}
    save_registry(entries); write_generated(render(config, entries))
    return entries


def switch_off(host):
    """`mail.mode: off`: no relay and no sink run; the shim in every site reports mail as off."""
    for name in (CONTAINER, SINK):
        if host.inspect(name): command(['docker', 'rm', '--force', name])
    return {'running': False, 'mode': 'off', 'sites': []}


def deploy(host, sites, step=lambda value: None):
    """(Re)create the relay for these PHP sites and attach it to each site's backend network as `mail`."""
    config = settings()
    if config['mode'] == 'off': install_shim(); return switch_off(host)
    built = image(step); install_shim(); ensure_network()
    entries = generate(config, sites)
    STATE.mkdir(mode=0o700, exist_ok=True); trusted(STATE, directory=True)
    (STATE / 'queue').mkdir(mode=0o755, exist_ok=True); os.chmod(STATE / 'queue', 0o755)  # Postfix's unprivileged daemons traverse the spool root
    deploy_sink(host, config)
    if host.inspect(CONTAINER): command(['docker', 'rm', '--force', CONTAINER])
    step('starting the mail relay')
    from .content_site import mount
    args = ['docker', 'create', '--name', CONTAINER, '--label', 'hosting.mail=relay', '--network', NETWORK, '--restart', 'unless-stopped',
            '--cap-drop', 'ALL', *sum((['--cap-add', c] for c in CAPS), []), '--security-opt', 'no-new-privileges:true', '--pids-limit', '512',
            '--tmpfs', '/tmp', '--log-driver', 'local', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=6']
    mount(args, GENERATED, '/run/mail')
    mount(args, STATE / 'queue', '/var/spool/postfix', False)
    args.extend(['--entrypoint', '/run/mail/entrypoint.sh', built['image_id']])
    command(args)
    for name in sorted(entries):
        command(['docker', 'network', 'connect', '--alias', 'mail', 'hosting-backend-' + name, CONTAINER])
    command(['docker', 'start', CONTAINER])
    until = time.monotonic() + 30
    while time.monotonic() < until:
        live = host.inspect(CONTAINER)
        if not live['State']['Running']: raise ValueError('The mail relay exited; inspect its log')
        try:
            command(['docker', 'exec', CONTAINER, 'postfix', 'status']); break  # exit 0 once master runs
        except RuntimeError: pass
        time.sleep(1)
    else: raise ValueError('The mail relay did not start')
    return {'running': True, 'mode': config['mode'], 'sites': sorted(entries)}


def refresh(host, sites):
    """A running relay learns a changed site set without restarting: networks joined or left, maps reloaded."""
    live = host.inspect(CONTAINER)
    if settings()['mode'] == 'off': return switch_off(host)
    if not live or not live['State']['Running']: return deploy(host, sites)
    entries = generate(settings(), sites)
    attached = {n for n in live['NetworkSettings']['Networks'] if n.startswith('hosting-backend-')}
    wanted = {'hosting-backend-' + n for n in entries}
    for network in sorted(wanted - attached): command(['docker', 'network', 'connect', '--alias', 'mail', network, CONTAINER])
    for network in sorted(attached - wanted): command(['docker', 'network', 'disconnect', '--force', network, CONTAINER])
    command(['docker', 'exec', CONTAINER, 'sh', '-c', 'cp /run/mail/main.cf /etc/postfix/main.cf && rm -rf /etc/postfix/hosting && mkdir /etc/postfix/hosting && cp /run/mail/maps/* /etc/postfix/hosting/ && chmod 644 /etc/postfix/main.cf /etc/postfix/hosting/* && postfix reload'])
    return {'running': True, 'sites': sorted(entries)}


def attach(host, row, domains):
    """A PHP site created or renamed its hostnames: the relay (if set up) joins its network and learns its domains."""
    if not host.inspect(CONTAINER): return False
    entries = registry()
    sites = {n: {'id': e['id'], 'domains': e['domains'], 'senders': e.get('senders', [])} for n, e in entries.items()}
    sites[row['name']] = {'id': row['id'], 'domains': list(domains), 'senders': extra_senders(row)}
    refresh(host, sites)
    return True


def detach(host, row):
    """Before a site's networks go: leave its backend network and forget its domains."""
    live = host.inspect(CONTAINER)
    entries = registry()
    if row['name'] not in entries and not (live and 'hosting-backend-' + row['name'] in live['NetworkSettings']['Networks']): return
    sites = {n: {'id': e['id'], 'domains': e['domains'], 'senders': e.get('senders', [])} for n, e in entries.items() if n != row['name']}
    if live and live['State']['Running']: refresh(host, sites)
    else:
        entries.pop(row['name'], None); save_registry(entries)


def site_line(row):
    """What the site overview says: how to reach the relay, whether it is attached, and the SPF record to offer."""
    if json.loads(row['payload']).get('runtime') != 'php': return None
    config = settings()
    entry = registry().get(row['name'])
    ip = config['public_ip'] or '<server address>'
    return {'server': 'mail', 'port': 25, 'attached': bool(entry), 'set_up': SHIM.exists(), 'mode': config['mode'],
            'spf': f'v=spf1 ip4:{ip} ~all', 'domains': entry['domains'] if entry else [], 'senders': extra_senders(row)}


def apply_senders(host, row, data, ident=None):
    """Store a site's extra allowed senders in hosting.yaml and teach the relay; the previous list is kept for rollback."""
    senders = validate_senders(data)['senders']
    path = SITES / row['name'] / 'hosting.yaml'; trusted(path)
    meta = yaml.safe_load(path.read_text()) or {}
    if meta.get('runtime') != 'php' or meta.get('operation_id') != row['id']: raise ValueError('Allowed senders apply to managed PHP sites')
    previous = list(meta.get('mail_senders') or [])
    if ident:
        from .core import request_id
        request_id(ident); ROOT.mkdir(mode=0o700, exist_ok=True); (ROOT / 'saved').mkdir(mode=0o700, exist_ok=True)
        atomic(ROOT / 'saved' / (ident + '.json'), json.dumps({'site_id': row['id'], 'senders': previous}))
    meta['mail_senders'] = senders
    atomic(path, yaml.safe_dump(meta))
    try:
        attach(host, row, [meta['domain'], *meta.get('aliases', [])])
    except Exception:
        meta['mail_senders'] = previous; atomic(path, yaml.safe_dump(meta))
        try: attach(host, row, [meta['domain'], *meta.get('aliases', [])])
        except Exception as recovery: raise RuntimeError('Allowed senders rollback needs review: ' + str(recovery)) from None
        raise
    return senders


def rollback_senders(host, row, ident):
    path = ROOT / 'saved' / (ident + '.json')
    if not path.exists(): return
    trusted(path); saved = json.loads(path.read_text())
    if saved['site_id'] != row['id']: raise ValueError('Allowed senders recovery belongs to another site')
    apply_senders(host, row, {'senders': saved['senders']})


def perform_senders(host, row, job, step):
    from .content_site import OUTPUT, ContentFailed
    step('applying allowed senders')
    try: senders = apply_senders(host, row, json.loads(job['payload']), job['id'])
    except RuntimeError: raise
    except Exception as exc:
        OUTPUT.mkdir(mode=0o700, exist_ok=True); atomic(OUTPUT / (job['id'] + '.txt'), str(exc)[:4000])
        raise ContentFailed('Allowed senders were not changed; the previous list stays in force. Inspect output.') from None
    OUTPUT.mkdir(mode=0o700, exist_ok=True)
    atomic(OUTPUT / (job['id'] + '.txt'), f"{row['name']} may send as its own domains" + (' and ' + ', '.join(senders) if senders else '') + '.\n')


def recover(ledger, host):
    for job in ledger.content_jobs():
        if job['kind'] == 'mail-senders' and job['state'] == 'recovery-needed': rollback_senders(host, ledger.get(job['site_id']), job['id'])


LINE = re.compile(r'^(?:(\d{4}-\d\d-\d\dT\S+) )?([A-Z][a-z]{2} {1,2}\d{1,2} \d\d:\d\d:\d\d) \S+ postfix/(\w+)\[\d+\]: (?:([0-9A-F]{6,}): )?(.*)$')
MONTHS = {m: i for i, m in enumerate(('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'), 1)}


def stamp_to_time(rfc3339, syslog, now):
    """A docker `-t` prefix is exact; a plain syslog stamp has no year, so the latest year that is not in the future."""
    if rfc3339:
        try: return datetime.fromisoformat(rfc3339.replace('Z', '+00:00')).timestamp()
        except ValueError: return None
    month, day, clock = syslog.split()
    hour, minute, second = (int(x) for x in clock.split(':'))
    year = datetime.fromtimestamp(now, tz=timezone.utc).year
    when = datetime(year, MONTHS[month], int(day), hour, minute, second, tzinfo=timezone.utc).timestamp()
    if when > now + 86400: when = datetime(year - 1, MONTHS[month], int(day), hour, minute, second, tzinfo=timezone.utc).timestamp()
    return when


def site_for(address, entries):
    try: ip = ipaddress.ip_address(address)
    except ValueError: return None
    for name, entry in entries.items():
        if ip in ipaddress.ip_network(entry['subnet']): return name
    return None


def parse_log(text, entries, now=None):
    """Per-site counts from the relay's log: sent, deferred, bounced and rate-limit hits, in three windows."""
    now = now or time.time()
    windows = {'hour': 3600, 'day': 86400, 'month': 30 * 86400}
    sites = {n: {w: {'sent': 0, 'deferred': 0, 'bounced': 0, 'limited': 0} for w in windows} | {'recent': [], 'last': None, 'domains': {}} for n in entries}
    queue_site = {}
    for line in text.splitlines():
        m = LINE.match(line)
        if not m: continue
        rfc3339, syslog, program, qid, rest = m.groups()
        when = stamp_to_time(rfc3339, syslog, now)
        if when is None: continue
        age = now - when
        if program == 'smtpd' and qid and rest.startswith('client='):
            client = re.search(r'\[([0-9.]+)\]', rest)
            site = site_for(client.group(1), entries) if client else None
            if site: queue_site[qid] = site
        elif program == 'smtpd' and 'rate limit exceeded' in rest:
            client = re.search(r'from [^\[\s]*\[([0-9.]+)\]', rest)  # the client is named by reverse DNS or 'unknown'
            site = site_for(client.group(1), entries) if client else None
            if site:
                for w, span in windows.items():
                    if age <= span: sites[site][w]['limited'] += 1
        elif program in ('smtp', 'error', 'bounce', 'local') and qid and rest.startswith('to='):
            site = queue_site.get(qid)
            status = re.search(r'status=(\w+)', rest); to = re.search(r'to=<([^>]*)>', rest)
            if not site or not status: continue
            outcome = {'sent': 'sent', 'deferred': 'deferred', 'bounced': 'bounced'}.get(status.group(1))
            if not outcome: continue
            for w, span in windows.items():
                if age <= span: sites[site][w][outcome] += 1
            domain = to.group(1).rsplit('@', 1)[-1].lower() if to else ''
            if age <= windows['month'] and domain: sites[site]['domains'][domain] = sites[site]['domains'].get(domain, 0) + 1
            sites[site]['last'] = max(sites[site]['last'] or 0, when)
            if len(sites[site]['recent']) < 20 or when > sites[site]['recent'][-1]['when']:
                sites[site]['recent'].append({'when': when, 'to': to.group(1) if to else '', 'status': outcome, 'detail': rest[rest.find('status='):][:200]})
                sites[site]['recent'] = sorted(sites[site]['recent'], key=lambda r: -r['when'])[:20]
    for site in sites.values():
        site['domains'] = sorted(site['domains'].items(), key=lambda kv: -kv[1])[:8]
    return sites


def parse_queue(text, entries):
    """`postqueue -j` lines, each a message, matched to a site by its sender domain."""
    domain_site = {d.lower(): n for n, e in entries.items() for d in e['domains']}
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line: continue
        try: item = json.loads(line)
        except ValueError: continue
        sender = item.get('sender') or ''
        items.append({'id': item.get('queue_id'), 'queue': item.get('queue_name'), 'arrived': item.get('arrival_time'), 'size': item.get('message_size'),
                      'sender': sender, 'site': domain_site.get(sender.rsplit('@', 1)[-1].lower()),
                      'recipients': [r.get('address') for r in item.get('recipients', [])],
                      'reason': next((r.get('delay_reason') for r in item.get('recipients', []) if r.get('delay_reason')), '')})
    return sorted(items, key=lambda i: -(i['arrived'] or 0))


LOG = STATE / 'queue/hosting-log'
LOG_ROTATE_BYTES = 20 * 1024 * 1024
LOG_KEEP_DAYS = 35


def read_log(now=None):
    """The relay's persisted log, rotated by size and pruned by age here, newest file last."""
    now = now or time.time()
    if not LOG.is_dir(): return ''
    current = LOG / 'mail.log'
    if current.exists() and current.stat().st_size > LOG_ROTATE_BYTES:
        try: command(['docker', 'exec', CONTAINER, 'postfix', 'logrotate'])
        except RuntimeError: pass
    parts = []
    for path in sorted(LOG.glob('mail.log-*')):
        if now - path.stat().st_mtime > LOG_KEEP_DAYS * 86400: path.unlink(); continue
        parts.append(path.read_text(errors='replace'))
    if current.exists(): parts.append(current.read_text(errors='replace'))
    return ''.join(parts)


def status(host):
    config = settings()
    entries = registry()
    live = host.inspect(CONTAINER)
    running = bool(live and live['State']['Running'])
    result = {'set_up': SHIM.exists() and bool(entries) or running, 'running': running, 'mode': config['mode'], 'relayhost': config['relayhost'],
              'hostname': hostname(config), 'rate_per_hour': config['rate_per_hour'], 'public_ip': config['public_ip'],
              'sink': bool(host.inspect(SINK)) if config['mode'] == 'sink' else None, 'queue': [], 'sites': [], 'log': '', 'error': ''}
    if config['mode'] == 'off': result['set_up'] = True; return result
    if not running: return result
    try:
        queue = command(['docker', 'exec', CONTAINER, 'postqueue', '-j'])
        result['queue'] = parse_queue(queue, entries)
        log = read_log()
        counts = parse_log(log, entries)
        result['sites'] = [{'name': n, **counts[n]} for n in sorted(entries)]
        result['log'] = '\n'.join(log.splitlines()[-40:])
        attached = set(live['NetworkSettings']['Networks'])
        for site in result['sites']: site['attached'] = ('hosting-backend-' + site['name']) in attached
    except RuntimeError as exc:
        result['error'] = str(exc)[:500]
    return result


def flush(host):
    if not host.inspect(CONTAINER): raise ValueError('The mail relay is not running')
    command(['docker', 'exec', CONTAINER, 'postqueue', '-f'])
    return {'flushed': True}


def delete(host, queue_id):
    if not re.fullmatch(r'[0-9A-F]{6,20}', queue_id or ''): raise ValueError('Choose a queued message')
    if not host.inspect(CONTAINER): raise ValueError('The mail relay is not running')
    command(['docker', 'exec', CONTAINER, 'postsuper', '-d', queue_id])
    return {'deleted': queue_id}


def setup(host, ledger):
    """`reeve mail-setup`: build, generate, start, attach every PHP site; mount the shim into their PHP containers."""
    result = deploy(host, php_sites(ledger))
    from .php_site import add_shim_mount
    remounted = []
    for name, site in php_sites(ledger).items():
        row = ledger.get(site['id'])
        if add_shim_mount(host, row): remounted.append(name)
    return {**result, 'php_containers_recreated': remounted}
