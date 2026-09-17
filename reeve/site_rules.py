"""Site rules: the operator-editable nginx directives of a managed static or PHP site.

The text lives in the root-owned conf/site.nginx.conf, included inside the server block before
the template's own locations, so a site can add redirects, extra locations, error pages and
headers without a new routing profile. Applying is a durable content job: the candidate is
checked with `nginx -t` in a throwaway container, the previous text is saved for rollback, the
web container is recreated (the file is bind-mounted, so a reload would keep the old inode) and
the site's HTTPS names are verified; any failure restores the previous rules.
"""
import json
from pathlib import Path

import yaml

from .host import OPS, SITES, atomic, command, trusted, static_nginx

SAVED = OPS / 'panel/worker/site-rules'
LIMIT = 65536
DEFAULT = '# Site-specific nginx locations and rewrite rules. Preserved on retry.\n'


class RulesRecoveryFailed(RuntimeError):
    pass


def validate(data):
    if not isinstance(data, dict) or set(data) != {'text'} or not isinstance(data['text'], str):
        raise ValueError('Site rules need a text')
    text = data['text'].replace('\r\n', '\n')
    if '\x00' in text or len(text.encode()) > LIMIT:
        raise ValueError('Site rules must be text of at most 64 KiB')
    if text and not text.endswith('\n'): text += '\n'
    return {'text': text}


def read(row):
    """The current rules for the site page; None for sites without the file (Compose packages)."""
    path = SITES / row['name'] / 'conf/site.nginx.conf'
    if not path.exists(): return None
    trusted(path)
    return path.read_text()


def context(row):
    root = SITES / row['name']
    for p in (root, root / 'conf'): trusted(p, directory=True)
    for p in ('hosting.yaml', 'compose.yml', 'conf/nginx.conf'): trusted(root / p)
    meta = yaml.safe_load((root / 'hosting.yaml').read_text())
    compose = yaml.safe_load((root / 'compose.yml').read_text())
    if meta.get('runtime', 'static') not in ('static', 'php') or meta.get('operation_id') != row['id']:
        raise ValueError('Site rules apply to managed static and PHP sites')
    return root, meta, compose


def check(row, root, meta, compose, candidate):
    """`nginx -t` on the site's real nginx.conf with the candidate rules, as the site's own identity."""
    web = compose['services']['web']
    nginx_conf = root / 'conf/nginx.conf'
    if meta.get('runtime', 'static') == 'static' and 'site.nginx.conf' not in nginx_conf.read_text():
        # A static site created before rules existed: give it the include and the mount.
        atomic(nginx_conf, static_nginx(), 0o644)
        mount = f"{root}/conf/site.nginx.conf:/etc/hosting/site.nginx.conf:ro"
        if mount not in web['volumes']:
            web['volumes'].append(mount); atomic(root / 'compose.yml', yaml.safe_dump(compose))
    # A PHP template resolves its FPM upstream by name, so the check joins the private backend.
    network = ['--network', 'hosting-backend-' + row['name']] if meta.get('runtime') == 'php' else ['--network', 'none']
    command(['docker', 'run', '--rm', '--read-only', *network, '--user', web['user'], '--cap-drop', 'ALL',
             '--security-opt', 'no-new-privileges:true', '--tmpfs', '/tmp',
             '--volume', f"{nginx_conf}:/etc/nginx/nginx.conf:ro", '--volume', f"{candidate}:/etc/hosting/site.nginx.conf:ro",
             '--entrypoint', 'nginx', web['image'], '-t'])


def apply(host, row, text, ident=None):
    from .site_backup import refresh_web
    root, meta, compose = context(row)
    conf = root / 'conf'
    candidate = conf / 'site.nginx.candidate.conf'
    atomic(candidate, text, 0o644)
    try:
        check(row, root, meta, compose, candidate)
    finally:
        candidate.unlink(missing_ok=True)
    previous = (conf / 'site.nginx.conf').read_text() if (conf / 'site.nginx.conf').exists() else DEFAULT
    if ident:
        from .core import request_id
        request_id(ident); SAVED.mkdir(mode=0o700, exist_ok=True)
        atomic(SAVED / (ident + '.json'), json.dumps({'site_id': row['id'], 'text': previous}))
    names = [meta['domain'], *meta.get('aliases', [])]
    atomic(conf / 'site.nginx.conf', text, 0o644)
    try:
        refresh_web(row, root)
        host.verify_domains(names)
    except Exception:
        atomic(conf / 'site.nginx.conf', previous, 0o644)
        try:
            refresh_web(row, root)
            host.verify_domains(names)
        except Exception as recovery:
            raise RulesRecoveryFailed('Site rules rollback needs review: ' + str(recovery)) from None
        raise


def rollback(host, row, ident):
    from .site_backup import refresh_web
    path = SAVED / (ident + '.json')
    if not path.exists(): return
    trusted(path); saved = json.loads(path.read_text())
    if saved['site_id'] != row['id']: raise ValueError('Site rules recovery belongs to another site')
    root = SITES / row['name']
    atomic(root / 'conf/site.nginx.conf', saved['text'], 0o644)
    refresh_web(row, root)


def perform(host, row, job, step):
    from .content_site import OUTPUT, ContentFailed
    step('validating and applying site rules')
    try:
        apply(host, row, validate(json.loads(job['payload']))['text'], job['id'])
    except RulesRecoveryFailed:
        raise
    except Exception as exc:
        OUTPUT.mkdir(mode=0o700, exist_ok=True); atomic(OUTPUT / (job['id'] + '.txt'), str(exc)[:4000])
        raise ContentFailed('Site rules were not applied; the previous rules stay in force. Inspect output.') from None
    OUTPUT.mkdir(mode=0o700, exist_ok=True)
    atomic(OUTPUT / (job['id'] + '.txt'), 'Site rules validated and applied; the web container was recreated and the site answered over HTTPS.\n')


def recover(ledger, host):
    for job in ledger.content_jobs():
        if job['kind'] == 'site-rules' and job['state'] == 'recovery-needed': rollback(host, ledger.get(job['site_id']), job['id'])
