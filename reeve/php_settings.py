"""PHP limits: the per-site PHP tunables an operator adjusts, kept consistent with nginx.

Five values cover what customers ask for: execution time, upload size, POST size, input
variables and memory per request. They are the site's settings, stored in hosting.yaml, and
render two files together: conf/php.ini (FPM and CLI) and the nginx body size and FastCGI
read timeout that must agree with them. Applying is a durable content job: the candidate
nginx.conf is checked with `nginx -t`, the previous files are saved for rollback, PHP and web
are recreated (both files are bind-mounted, so a reload would keep the old inodes) and the
site's HTTPS names are verified; any failure restores the previous configuration. Backups
carry the settings and a restore to a new site applies them.
"""
import json

import yaml

from .host import NGINX, OPS, SITES, atomic, command, trusted

SAVED = OPS / 'panel/worker/php-settings'
FIELDS = ('max_execution_time', 'upload_max_filesize_mb', 'post_max_size_mb', 'max_input_vars', 'memory_limit_mb')
LABELS = {'max_execution_time': 'Execution time (seconds)', 'upload_max_filesize_mb': 'Largest upload (MB)',
          'post_max_size_mb': 'Largest POST (MB)', 'max_input_vars': 'Input variables', 'memory_limit_mb': 'Memory per request (MB)'}
BOUNDS = {'max_execution_time': (1, 3600), 'upload_max_filesize_mb': (1, 4096), 'post_max_size_mb': (1, 4096),
          'max_input_vars': (100, 1000000), 'memory_limit_mb': (32, 4096)}


class PhpRecoveryFailed(RuntimeError):
    pass


def budget(data):
    """Workers and per-request memory from the site's memory limit, as the pool has always been sized.

    Uncapped sites get the server profile's workers and memory a request (standard: eight and
    512 MB, the WordPress numbers, no per-site carrying of old pool values; small: three and
    256 MB); a memory-capped site keeps its budget arithmetic within the profile's ceiling."""
    from .profile import settings
    profile = settings()
    if data.get('memory_mb') is not None:
        php_memory = data['memory_mb'] - 32
        workers = max(1, min(profile['php_workers'], php_memory // 160))
        return workers, max(32, min(profile['php_memory_limit_mb'], (php_memory - 48) // workers))
    return profile['php_workers'], profile['php_memory_limit_mb']


def defaults(data):
    return {'max_execution_time': 120, 'upload_max_filesize_mb': 128, 'post_max_size_mb': 136, 'max_input_vars': 3000,
            'memory_limit_mb': budget(data)[1]}


def validate(data, payload=None):
    """Whole numbers within bounds; the POST size holds the upload; memory stays inside the container's limit."""
    if not isinstance(data, dict) or set(data) != set(FIELDS): raise ValueError('PHP limits need all five values')
    result = {}
    for field in FIELDS:
        value = data[field]
        if isinstance(value, str):
            if not value.strip().isdigit(): raise ValueError(LABELS[field] + ' must be a whole number')
            value = int(value.strip())
        if type(value) is not int: raise ValueError(LABELS[field] + ' must be a whole number')
        low, high = BOUNDS[field]
        if not low <= value <= high: raise ValueError(f'{LABELS[field]} must be between {low} and {high}')
        result[field] = value
    if result['post_max_size_mb'] < result['upload_max_filesize_mb']:
        raise ValueError('Largest POST must be at least the largest upload')
    if payload and payload.get('memory_mb') is not None and result['memory_limit_mb'] > payload['memory_mb'] - 32:
        raise ValueError(f"Memory per request cannot exceed the site's PHP memory of {payload['memory_mb'] - 32} MB")
    return result


def effective(meta, payload):
    """The settings in force: the site's own, or the defaults for its memory budget."""
    stored = (meta or {}).get('php_settings') or {}
    return {**defaults(payload), **{k: stored[k] for k in FIELDS if k in stored}}


def render_ini(settings):
    return f'''expose_php=Off
display_errors=Off
log_errors=On
error_log=/proc/self/fd/2
memory_limit={settings['memory_limit_mb']}M
upload_max_filesize={settings['upload_max_filesize_mb']}M
post_max_size={settings['post_max_size_mb']}M
max_execution_time={settings['max_execution_time']}
max_input_vars={settings['max_input_vars']}
session.save_path=/tmp
session.cookie_httponly=1
session.cookie_secure=1
cgi.fix_pathinfo=0
opcache.memory_consumption=16
opcache.interned_strings_buffer=4
sendmail_path=/usr/local/bin/hosting-sendmail
'''


def nginx_directives(settings):
    """What nginx must agree on: the request body it accepts and how long it waits for PHP."""
    return {'body': f"client_max_body_size {settings['post_max_size_mb']}m;",
            'timeout': f"fastcgi_read_timeout {settings['max_execution_time'] + 10}s;"}


def stored(row):
    """Only what an operator set; None for a site on the defaults, so a restore takes the defaults of its day."""
    payload = json.loads(row['payload'])
    if payload.get('runtime') != 'php': return None
    path = SITES / row['name'] / 'hosting.yaml'
    if not path.exists(): return None
    trusted(path)
    kept = (yaml.safe_load(path.read_text()) or {}).get('php_settings')
    return {k: kept[k] for k in FIELDS} if kept and all(k in kept for k in FIELDS) else None


def public(row):
    """The settings for the site page and the backup manifest; None for sites that are not managed PHP."""
    payload = json.loads(row['payload'])
    if payload.get('runtime') != 'php': return None
    path = SITES / row['name'] / 'hosting.yaml'
    if not path.exists(): return defaults(payload)
    trusted(path)
    return effective(yaml.safe_load(path.read_text()), payload)


def context(row):
    root = SITES / row['name']
    for p in (root, root / 'conf'): trusted(p, directory=True)
    for p in ('hosting.yaml', 'compose.yml', 'conf/nginx.conf', 'conf/php.ini', 'conf/site.nginx.conf'): trusted(root / p)
    meta = yaml.safe_load((root / 'hosting.yaml').read_text())
    compose = yaml.safe_load((root / 'compose.yml').read_text())
    if meta.get('runtime') != 'php' or meta.get('operation_id') != row['id']:
        raise ValueError('PHP limits apply to managed PHP sites')
    return root, meta, compose


def render_nginx(meta, settings):
    from .php_site import nginx
    if meta.get('web_settings'):
        from .requests_site import render
        return render(meta['web_settings']['trusted_ingress'], meta['web_settings']['profile'], settings)
    return nginx(NGINX, settings)


def check(row, root, compose, candidate):
    web = compose['services']['web']
    command(['docker', 'run', '--rm', '--read-only', '--network', 'hosting-backend-' + row['name'], '--user', web['user'],
             '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--tmpfs', '/tmp',
             '--volume', f"{candidate}:/etc/nginx/nginx.conf:ro", '--volume', f"{root}/conf/site.nginx.conf:/etc/hosting/site.nginx.conf:ro",
             '--entrypoint', 'nginx', web['image'], '-t'])


def recreate(row, root, compose):
    """PHP and web both mount files replaced by rename, so both are recreated, as a routing change does."""
    command(['docker', 'compose', '-f', str(root / 'compose.yml'), 'up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '60', 'php', 'web'], timeout=120)
    for name in ('php', 'web'):
        service = compose['services'][name]
        if service.get('pids_limit') == -1: command(['docker', 'update', '--pids-limit', '-1', service['container_name']])


def write(root, meta, settings, ini, nginx):
    meta = dict(meta, php_settings=settings) if settings is not None else {k: v for k, v in meta.items() if k != 'php_settings'}
    atomic(root / 'hosting.yaml', yaml.safe_dump(meta))
    atomic(root / 'conf/php.ini', ini, 0o644)
    atomic(root / 'conf/nginx.conf', nginx, 0o644)


def apply(host, row, data, ident=None):
    root, meta, compose = context(row)
    payload = json.loads(row['payload'])
    settings = validate(data, payload)
    ini = render_ini(settings); nginx = render_nginx(meta, settings)
    candidate = root / 'conf/nginx.candidate.conf'
    atomic(candidate, nginx, 0o644)
    try: check(row, root, compose, candidate)
    finally: candidate.unlink(missing_ok=True)
    prior = {'site_id': row['id'], 'settings': meta.get('php_settings'), 'ini': (root / 'conf/php.ini').read_text(),
             'nginx': (root / 'conf/nginx.conf').read_text()}
    if ident:
        from .core import request_id
        request_id(ident); SAVED.mkdir(mode=0o700, exist_ok=True)
        atomic(SAVED / (ident + '.json'), json.dumps(prior))
    names = [meta['domain'], *meta.get('aliases', [])]
    write(root, meta, settings, ini, nginx)
    try:
        recreate(row, root, compose)
        host.verify_domains(names)
    except Exception:
        write(root, meta, prior['settings'], prior['ini'], prior['nginx'])
        try:
            recreate(row, root, compose)
            host.verify_domains(names)
        except Exception as recovery:
            raise PhpRecoveryFailed('PHP limits rollback needs review: ' + str(recovery)) from None
        raise
    return settings


def rollback(host, row, ident):
    path = SAVED / (ident + '.json')
    if not path.exists(): return
    trusted(path); saved = json.loads(path.read_text())
    if saved['site_id'] != row['id']: raise ValueError('PHP limits recovery belongs to another site')
    root, meta, compose = context(row)
    write(root, meta, saved['settings'], saved['ini'], saved['nginx'])
    recreate(row, root, compose)


def perform(host, row, job, step):
    from .content_site import OUTPUT, ContentFailed
    step('validating and applying PHP limits')
    try:
        settings = apply(host, row, json.loads(job['payload']), job['id'])
    except PhpRecoveryFailed:
        raise
    except Exception as exc:
        OUTPUT.mkdir(mode=0o700, exist_ok=True); atomic(OUTPUT / (job['id'] + '.txt'), str(exc)[:4000])
        raise ContentFailed('PHP limits were not applied; the previous values stay in force. Inspect output.') from None
    OUTPUT.mkdir(mode=0o700, exist_ok=True)
    atomic(OUTPUT / (job['id'] + '.txt'), 'PHP limits applied: ' + ', '.join(f'{LABELS[k]} {settings[k]}' for k in FIELDS)
           + '. PHP and web were recreated and the site answered over HTTPS.\n')


def recover(ledger, host):
    for job in ledger.content_jobs():
        if job['kind'] == 'php-settings' and job['state'] == 'recovery-needed': rollback(host, ledger.get(job['site_id']), job['id'])
