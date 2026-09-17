"""Customer SFTP and the ownership fix for lazily uploaded content.

One SFTP-only server (`hosting-sftp`, port 2222) serves every site that has keys: each customer
logs in as the site name with a public key, lands in a chroot that holds only that site's `html/`,
and runs as the site's numeric identity, so uploads, FPM-written files and the panel's own tools
agree on ownership. No shell, no forwarding, no passwords. The server is SFTPGo, not OpenSSH:
its version banner is the version that runs, so the quarterly ASV scan is not failed by Debian's
backported-but-unchanged OpenSSH banner (Tom, 2026-09-17: the port stays open for customers who
update when they want). It is an infrastructure container like the edge: SFTPGo runs as root on a
read-only root with only the capabilities that writing files as each site's uid needs, and the
generated users and configuration are root-owned files mounted read-only.

Per-site keys are a durable content job (`sftp-access`): validate, save the previous registry,
regenerate the server and wait for its banner; any failure restores
the previous registry and regenerates again. Backups carry a site's keys; a restore to a new site
applies them; Delete removes the entry.

`fix-ownership` is the companion for uploads made as `tom` or `root`: everything under `html/`
becomes the site user's, directories get owner rwx, files owner rw, setuid/setgid and group or
world write are stripped, and nothing else changes.
"""
import json
import os
import shutil
import socket
import stat
import time
from pathlib import Path

import yaml

from .host import OPS, SITES, atomic, command, trusted

ROOT = OPS / 'panel/worker/sftp'
SAVED = ROOT / 'saved'
GENERATED = ROOT / 'generated'
CONTAINER = 'hosting-sftp'
PORT = 2222
MAX_KEYS = 20
DURATIONS = {'1h': 3600, '4h': 4 * 3600, '24h': 24 * 3600, '7d': 7 * 86400, 'manual': None}  # how long Turn on lasts; the port closes when no site is on
IMAGE = 'drakkan/sftpgo@sha256:9011fe608d336d3daf6ed6224b16fd40443aab8f3335e0b20853d6a539c58738'  # SFTPGo 2.7.5; the weekly rebuild moves this pin with panel releases
IMAGE_VERSION = 'sftpgo-2.7.5'
CAPS = ['CHOWN', 'DAC_OVERRIDE', 'DAC_READ_SEARCH', 'FOWNER', 'FSETID', 'SETUID', 'SETGID']


class SftpRecoveryFailed(RuntimeError):
    pass


def validate(data):
    """`on`, `rotate` or `off`, with optional secondary public keys (a developer's), one per line.

    The site's own key is made by the panel and handed out as the private key; the secondary keys
    are installed beside it. `off` drops everything."""
    from .toolbox import public_key
    if not isinstance(data, dict) or not {'action'} <= set(data) <= {'action', 'secondary', 'duration'}:
        raise ValueError('Customer SFTP needs an action')
    if data['action'] not in ('on', 'rotate', 'off'): raise ValueError('Customer SFTP action must be on, rotate or off')
    duration = data.get('duration', 'manual') or 'manual'
    if duration not in DURATIONS: raise ValueError('Choose how long access stays on')
    raw = data.get('secondary', '')
    if not isinstance(raw, (str, list)): raise ValueError('Secondary keys must be text')
    text = raw if isinstance(raw, str) else '\n'.join(k for k in raw if isinstance(k, str))
    if len(text) > 65536: raise ValueError('The keys text is too long')
    keys = []
    for line in text.replace('\r\n', '\n').split('\n'):
        line = line.strip()
        if not line or line.startswith('#'): continue
        key = public_key(line)
        if key not in keys: keys.append(key)
    if len(keys) > MAX_KEYS: raise ValueError(f'At most {MAX_KEYS} secondary keys per site')
    return {'action': data['action'], 'secondary': keys, 'duration': duration}


def registry():
    path = ROOT / 'sites.json'
    if not path.exists(): return {}
    trusted(path)
    return json.loads(path.read_text())


def save_registry(entries):
    ROOT.mkdir(mode=0o700, exist_ok=True); trusted(ROOT, directory=True)
    atomic(ROOT / 'sites.json', json.dumps(entries, indent=2, sort_keys=True))


def fingerprint():
    key = ROOT / 'host_key'
    if not (key.exists() and (ROOT / 'host_key.pub').exists()): return None
    return command(['ssh-keygen', '-lf', ROOT / 'host_key.pub']).strip()


PRIVATE = ROOT / 'private'


def key_fingerprint(public):
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.pub') as handle:
        handle.write(public + '\n'); handle.flush()
        try: return command(['ssh-keygen', '-lf', handle.name]).strip()
        except RuntimeError: return ''


def secondary_keys(row):
    """A site's secondary public keys, kept in hosting.yaml so they survive off and on."""
    path = SITES / row['name'] / 'hosting.yaml'
    if not path.exists(): return []
    trusted(path)
    return list((yaml.safe_load(path.read_text()) or {}).get('sftp_secondary') or [])


def save_secondary(row, keys):
    path = SITES / row['name'] / 'hosting.yaml'; trusted(path)
    meta = yaml.safe_load(path.read_text()) or {}
    if keys: meta['sftp_secondary'] = list(keys)
    else: meta.pop('sftp_secondary', None)
    atomic(path, yaml.safe_dump(meta))


def public(row):
    """What the site page and the backup manifest see; None for Compose packages."""
    if json.loads(row['payload']).get('runtime') == 'compose': return None
    entry = registry().get(row['id'])
    key = PRIVATE / row['id']
    return {'enabled': bool(entry), 'user': row['name'], 'port': PORT, 'fingerprint': fingerprint() if entry else None,
            'expires_at': entry.get('expires_at') if entry else None, 'has_key': key.exists(),
            'key_fingerprint': key_fingerprint(key.with_suffix('.pub').read_text().strip().rsplit(' ', 1)[0]) if key.with_suffix('.pub').exists() else None,
            'secondary': secondary_keys(row), 'durations': list(DURATIONS)}


def site_key(row, rotate=False):
    """The site's own key pair, made here; the public half is what the server installs."""
    ROOT.mkdir(mode=0o700, exist_ok=True); PRIVATE.mkdir(mode=0o700, exist_ok=True); trusted(PRIVATE, directory=True)
    path = PRIVATE / row['id']
    if rotate or not path.exists():
        for stale in (path, path.with_suffix('.pub')):
            if stale.exists(): stale.unlink()
        command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'sftp ' + row['name'], '-f', path])
        os.chmod(path, 0o600)
    trusted(path)
    return path.with_suffix('.pub').read_text().strip().rsplit(' ', 1)[0]


def export_key(row):
    """The private key into the web user's download folder under a token, as backup exports are."""
    from .site_backup import EXPORTS, web_identity
    import uuid
    if not (PRIVATE / row['id']).exists(): raise ValueError('This site has no SFTP key yet; turn access on once to make it')
    uid, gid = web_identity()
    EXPORTS.mkdir(mode=0o700, exist_ok=True); os.chown(EXPORTS, uid, gid)
    token = str(uuid.uuid4()); folder = EXPORTS / token; folder.mkdir(mode=0o700); os.chown(folder, uid, gid)
    name = row['name'] + '-sftp-key'
    shutil.copyfile(PRIVATE / row['id'], folder / name); os.chmod(folder / name, 0o600); os.chown(folder / name, uid, gid)
    return {'token': token, 'filename': name}


def host_key():
    ROOT.mkdir(mode=0o700, exist_ok=True); trusted(ROOT, directory=True)
    key = ROOT / 'host_key'
    if not key.exists():
        command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key])
        os.chmod(key, 0o600)
    trusted(key)
    return key


def image(step=lambda value: None):
    """The pinned SFTPGo image, pulled once."""
    try: image_id = command(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}']).strip()
    except RuntimeError:
        step('pulling the SFTP server image')
        command(['docker', 'pull', '--quiet', IMAGE], timeout=600)
        image_id = command(['docker', 'image', 'inspect', IMAGE, '--format', '{{.Id}}']).strip()
    return {'version': IMAGE_VERSION, 'image_id': image_id}


def render(entries, built=None):
    """SFTPGo's configuration and its users, one per site: home is the folder holding only that site's html/."""
    config = {
        'common': {'idle_timeout': 15, 'umask': '022', 'max_per_host_connections': 20,
                   'defender': {'enabled': True, 'driver': 'memory', 'ban_time': 30, 'ban_time_increment': 50, 'threshold': 15,
                                'score_invalid': 2, 'score_valid': 1, 'score_limit_exceeded': 3, 'score_no_auth': 0, 'observation_time': 30}},
        'sftpd': {'bindings': [{'port': PORT, 'address': ''}], 'host_keys': ['/run/sftp/host_key'], 'enabled_ssh_commands': [],
                  'password_authentication': False, 'keyboard_interactive_authentication': False, 'max_auth_tries': 4},
        'ftpd': {'bindings': [{'port': 0}]}, 'webdavd': {'bindings': [{'port': 0}]}, 'httpd': {'bindings': [{'port': 0}]}, 'telemetry': {'bindings': [{'port': 0}]},
        'data_provider': {'driver': 'memory', 'name': '/run/sftp/users.json', 'create_default_admin': False, 'users_base_dir': '/sites', 'backups_path': '/tmp/backups'}}
    users = [{'id': i, 'username': e['name'], 'status': 1, 'home_dir': '/sites/' + e['name'], 'uid': e['uid'], 'gid': e['uid'],
              'public_keys': list(e['keys']), 'permissions': {'/': ['*']}, 'filters': {'denied_protocols': ['FTP', 'DAV', 'HTTP']}}
             for i, e in enumerate(sorted(entries.values(), key=lambda e: e['name']), 1)]
    return {'sftpgo.json': json.dumps(config, indent=1), 'users.json': json.dumps({'users': users, 'version': 19}, indent=1)}


def write_generated(files):
    """The generated tree is mounted whole; files are replaced inside it, and the host key is copied in."""
    GENERATED.mkdir(mode=0o755, exist_ok=True); trusted(GENERATED, directory=True); os.chmod(GENERATED, 0o755)
    for name, text in files.items(): atomic(GENERATED / name, text, 0o444)
    atomic(GENERATED / 'host_key', host_key().read_text(), 0o600)


def wait_for_banner(host):
    until = time.monotonic() + 20
    while time.monotonic() < until:
        live = host.inspect(CONTAINER)
        if not live or not live['State']['Running']: raise ValueError('The SFTP server exited; inspect its log')
        try:
            with socket.create_connection(('127.0.0.1', PORT), timeout=1) as conn:
                if conn.recv(256).startswith(b'SSH-2.0-'): return
        except OSError: pass
        time.sleep(.5)
    raise ValueError('The SFTP server did not answer on its port')


def deploy(host, entries, step=lambda value: None):
    """Regenerate the server for these sites: no sites, no server."""
    from .content_site import mount
    if host.inspect(CONTAINER): command(['docker', 'rm', '--force', CONTAINER])
    if not entries: return {'running': False}
    built = image(step)
    write_generated(render(entries, built))
    for entry in entries.values():
        html = SITES / entry['name'] / 'html'
        if html.is_symlink() or not html.is_dir(): raise ValueError('Site content folder is missing: ' + entry['name'])
    step('starting the SFTP server')
    args = ['docker', 'create', '--name', CONTAINER, '--label', 'hosting.sftp=server', '--read-only', '--user', '0:0', '--cap-drop', 'ALL',
            *sum((['--cap-add', c] for c in CAPS), []), '--security-opt', 'no-new-privileges:true',
            '--restart', 'unless-stopped', '--publish', f'{PORT}:{PORT}', '--pids-limit', '256',
            '--tmpfs', '/tmp:mode=1777', '--tmpfs', '/sites:mode=755',
            '--env', 'SFTPGO_CONFIG_DIR=/run/sftp', '--env', 'SFTPGO_LOG_FILE_PATH=',
            '--log-driver', 'local', '--log-opt', 'max-size=5m', '--log-opt', 'max-file=4']
    mount(args, GENERATED, '/run/sftp')
    for entry in sorted(entries.values(), key=lambda e: e['name']):
        mount(args, SITES / entry['name'] / 'html', '/sites/' + entry['name'] + '/html', False)
    args.extend([built['image_id'], 'sftpgo', 'serve'])
    command(args)
    command(['docker', 'start', CONTAINER])
    wait_for_banner(host)
    return {'running': True, 'sites': sorted(e['name'] for e in entries.values())}


def context(row):
    root = SITES / row['name']
    trusted(root, directory=True); trusted(root / 'hosting.yaml')
    meta = yaml.safe_load((root / 'hosting.yaml').read_text())
    if meta.get('runtime', 'static') not in ('static', 'php') or meta.get('operation_id') != row['id']:
        raise ValueError('Customer SFTP applies to managed static and PHP sites')
    return root, meta


def apply(host, row, data, ident=None, step=lambda value: None):
    context(row)
    settings = validate(data)
    entries = registry(); prior = json.loads(json.dumps(entries)); prior_secondary = secondary_keys(row)
    if ident:
        from .core import request_id
        request_id(ident); ROOT.mkdir(mode=0o700, exist_ok=True); SAVED.mkdir(mode=0o700, exist_ok=True)
        atomic(SAVED / (ident + '.json'), json.dumps({'site_id': row['id'], 'entries': prior, 'secondary': prior_secondary}))
    if settings['action'] == 'off':
        entries.pop(row['id'], None)  # the site's key stays for the next time
    else:
        save_secondary(row, settings['secondary'])
        generated = site_key(row, rotate=settings['action'] == 'rotate')
        seconds = DURATIONS[settings['duration']]
        entries[row['id']] = {'name': row['name'], 'uid': row['uid'], 'generated': generated, 'secondary': settings['secondary'],
                              'keys': [generated, *[k for k in settings['secondary'] if k != generated]],
                              'expires_at': time.time() + seconds if seconds else None}
    save_registry(entries)
    try:
        result = deploy(host, entries, step)
    except Exception:
        save_registry(prior); save_secondary(row, prior_secondary)
        try: deploy(host, prior, step)
        except Exception as recovery:
            raise SftpRecoveryFailed('Customer SFTP rollback needs review: ' + str(recovery)) from None
        raise
    return {**result, 'action': settings['action'], 'secondary': len(settings['secondary']), 'expires_at': entries.get(row['id'], {}).get('expires_at')}


def expire(host, now=None, log=None):
    """Turn off every site whose time is up; with no site left the server goes and the port closes."""
    import sys
    now = now or time.time()
    log = log or (lambda text: print(text, file=sys.stderr, flush=True))
    entries = registry()
    due = [name for name, e in ((e['name'], e) for e in entries.values()) if e.get('expires_at') and e['expires_at'] <= now]
    if not due: return []
    remaining = {k: v for k, v in entries.items() if v['name'] not in due}
    save_registry(remaining)
    deploy(host, remaining)
    log('customer SFTP turned off after its time for: ' + ', '.join(sorted(due)))
    return sorted(due)


def remove(host, row):
    """Delete drops the site's entry; the server is regenerated without its mount."""
    entries = registry()
    for stale in (PRIVATE / row['id'], (PRIVATE / row['id']).with_suffix('.pub')):
        if stale.exists(): stale.unlink()
    if row['id'] not in entries: return
    del entries[row['id']]; save_registry(entries)
    deploy(host, entries)


def rollback(host, row, ident):
    path = SAVED / (ident + '.json')
    if not path.exists(): return
    trusted(path); saved = json.loads(path.read_text())
    if saved['site_id'] != row['id']: raise ValueError('Customer SFTP recovery belongs to another site')
    save_registry(saved['entries'])
    if 'secondary' in saved: save_secondary(row, saved['secondary'])
    deploy(host, saved['entries'])


def perform(host, row, job, step):
    from .content_site import OUTPUT, ContentFailed
    step('validating and applying customer SFTP access')
    try:
        result = apply(host, row, json.loads(job['payload']), job['id'], step)
    except SftpRecoveryFailed:
        raise
    except Exception as exc:
        OUTPUT.mkdir(mode=0o700, exist_ok=True); atomic(OUTPUT / (job['id'] + '.txt'), str(exc)[:4000])
        raise ContentFailed('Customer SFTP was not changed; the previous access stays in force. Inspect output.') from None
    OUTPUT.mkdir(mode=0o700, exist_ok=True)
    if result['action'] == 'off': text = f"Customer SFTP off for {row['name']}; its key is kept for next time.\n"
    else:
        until = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(result['expires_at'])) if result.get('expires_at') else 'turned off'
        text = (f"Customer SFTP on for {row['name']} until {until}: sftp -P {PORT} {row['name']}@<this server> with the site's key"
                + (" (a new one)" if result['action'] == 'rotate' else " (download it from the site page)")
                + (f" and {result['secondary']} secondary key(s)" if result['secondary'] else '') + f"; server host key {fingerprint()}.\n")
    atomic(OUTPUT / (job['id'] + '.txt'), text + ('The server runs for: ' + ', '.join(result['sites']) + '.\n' if result.get('running') else 'No site has customer SFTP; the server is not running.\n'))


def fix_ownership(row):
    """Everything under html/ becomes the site user's with owner access; nothing else changes."""
    root = SITES / row['name']; trusted(root, directory=True)
    html = root / 'html'
    if html.is_symlink() or not html.is_dir(): raise ValueError('Site content folder is missing')
    uid = row['uid']; counts = {'files': 0, 'directories': 0, 'links': 0, 'owner_changed': 0, 'mode_changed': 0}
    def fix(path):
        info = path.lstat()
        kind = 'links' if stat.S_ISLNK(info.st_mode) else 'directories' if stat.S_ISDIR(info.st_mode) else 'files'
        counts[kind] += 1
        if info.st_uid != uid or info.st_gid != uid:
            os.chown(path, uid, uid, follow_symlinks=False); counts['owner_changed'] += 1
        if kind == 'links': return
        mode = stat.S_IMODE(info.st_mode)
        wanted = (mode | (0o700 if kind == 'directories' else 0o600)) & ~0o7022
        if wanted != mode:
            os.chmod(path, wanted); counts['mode_changed'] += 1
    fix(html)
    for current, dirs, names in os.walk(html, followlinks=False):
        for name in dirs + names: fix(Path(current) / name)
    return counts


def perform_fix(host, row, job, step):
    from .content_site import OUTPUT, ContentFailed
    step('fixing ownership of site content')
    try: counts = fix_ownership(row)
    except Exception as exc:
        OUTPUT.mkdir(mode=0o700, exist_ok=True); atomic(OUTPUT / (job['id'] + '.txt'), str(exc)[:4000])
        raise ContentFailed('Ownership fix stopped; inspect output.') from None
    OUTPUT.mkdir(mode=0o700, exist_ok=True)
    atomic(OUTPUT / (job['id'] + '.txt'), (f"Site content is owned by the site user (uid {row['uid']}): {counts['files']} files, {counts['directories']} directories, "
                                          f"{counts['links']} links seen; ownership changed on {counts['owner_changed']}, modes on {counts['mode_changed']}.\n"))


def recover(ledger, host):
    for job in ledger.content_jobs():
        if job['kind'] == 'sftp-access' and job['state'] == 'recovery-needed': rollback(host, ledger.get(job['site_id']), job['id'])
