"""PHP patch rebuilds: the policy of 2026-09-12 as a scheduled job.

A site pins its PHP branch; patch releases within the branch arrive by rebuilding the branch
image from the same recipe with fresh packages and rolling each site onto it. The rebuild is a
singleton runtime job (`rebuild`): every catalogued branch is built again with `--no-cache`,
checked exactly as a first build is, and its package list compared with the running image's.
A branch whose packages did not change keeps its image (the fresh one is discarded, so sites are
never churned for nothing). A branch that changed becomes the catalogue image, the image it
replaces is kept as `previous` for rollback and the one before that is dropped, and every PHP
site still on an older image gets a same-branch switch job: the existing per-site mechanism that
builds the site image, validates it, recreates PHP, verifies HTTPS and rolls back on failure.
The mail relay and SFTP server images are rebuilt the same way and redeployed when their
packages changed. A report per run backs the versions page. `server.yaml` `updates:` sets the
hour and the interval in days; the worker queues a run when one is due.
"""
import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from .host import OPS, SITES, atomic, command, trusted
from .php_runtime import CATALOG, REQUIRED_EXTENSIONS, catalog

REPORTS = OPS / 'panel/worker/php-rebuilds'
DEFAULTS = {'hour': 4, 'every_days': 7}


def policy():
    config = OPS / 'server.yaml'
    values = (yaml.safe_load(config.read_text()) or {}).get('updates', {}) if config.exists() else {}
    if not isinstance(values, dict) or values.keys() - DEFAULTS.keys(): raise ValueError('Invalid updates policy in server.yaml')
    result = {**DEFAULTS, **values}
    if type(result['hour']) is not int or not 0 <= result['hour'] <= 23: raise ValueError('updates.hour must be 0 to 23')
    if type(result['every_days']) is not int or not 1 <= result['every_days'] <= 90: raise ValueError('updates.every_days must be 1 to 90')
    return result


def schedule():
    path = REPORTS / 'schedule.json'
    if not path.exists(): return {'last_started': 0.0}
    trusted(path); return json.loads(path.read_text())


def mark_started(now=None):
    REPORTS.mkdir(mode=0o700, exist_ok=True); trusted(REPORTS, directory=True)
    atomic(REPORTS / 'schedule.json', json.dumps({'last_started': now or time.time()}))


def due(now=None):
    """Once the interval has passed, at the configured hour (any minute of it); a missed window waits for the next."""
    now = now or time.time()
    settings = policy()
    moment = datetime.fromtimestamp(now)
    if moment.hour != settings['hour']: return False
    return now - schedule()['last_started'] >= settings['every_days'] * 86400 - 3600


def packages_of(lines):
    result = {}
    for line in lines:
        if '\t' in line:
            name, version = line.split('\t', 1); result[name] = version
    return result


def compare(old, new):
    before, after = packages_of(old), packages_of(new)
    changed = [{'package': p, 'from': before[p], 'to': after[p]} for p in sorted(after) if p in before and before[p] != after[p]]
    added = sorted(set(after) - set(before)); removed = sorted(set(before) - set(after))
    return {'changed': changed, 'added': added, 'removed': removed}


def build_fresh(branch, recipe, context, stamp):
    """A branch image from the recipe with fresh packages, checked as a first build is."""
    tag = f'hosting-php:{branch}-{recipe[:16]}-{stamp}'
    command(['docker', 'build', '--no-cache', '--build-arg', f'PHP_VERSION={branch}', '--tag', tag, '--file', context / 'Containerfile', context], timeout=1800)
    image_id = command(['docker', 'image', 'inspect', tag, '--format', '{{.Id}} ']).strip()
    info = json.loads(command(['docker', 'run', '--rm', '--network', 'none', '--user', '30000:30000', '--cap-drop', 'ALL',
                               '--security-opt', 'no-new-privileges:true', '--entrypoint', 'php', tag, '-r',
                               'echo json_encode(array("php_version" => PHP_VERSION, "extensions" => get_loaded_extensions()));']))
    if not info['php_version'].startswith(branch + '.') or not REQUIRED_EXTENSIONS.issubset(info['extensions']):
        raise RuntimeError(f'PHP {branch} rebuild does not meet the template contract')
    packages = command(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'cat', tag, '/usr/share/hosting-runtime/packages.tsv']).splitlines()
    return {'image': tag, 'image_id': image_id, 'recipe': recipe, **info, 'packages': packages, 'built_at': time.time()}


def remove_image(reference):
    """Best effort: an image still layered under a site image stays."""
    if not reference: return
    try: command(['docker', 'image', 'rm', reference])
    except RuntimeError: pass


def sites_on_older_images(ledger, runtimes):
    """PHP sites whose pinned base image is not the catalogue's for their branch."""
    result = []
    for row in ledger.list():
        if json.loads(row['payload']).get('runtime') != 'php' or row['state'] != 'succeeded': continue
        path = SITES / row['name'] / 'hosting.yaml'
        if not path.exists(): continue
        trusted(path)
        meta = yaml.safe_load(path.read_text()) or {}
        branch = ledger.runtime_branch(row)
        current = runtimes.get(branch)
        if current and (meta.get('php_runtime') or {}).get('image_id') != current['image_id']:
            result.append({'id': row['id'], 'name': row['name'], 'branch': branch, 'image_id': (meta.get('php_runtime') or {}).get('image_id')})
    return result


def rebuild_infrastructure(host, ledger, step):
    """The mail relay and SFTP server images from their pinned Debian with fresh packages; redeployed when changed."""
    from . import mail, sftp
    outcomes = {}
    for name, module in (('mail', mail), ('sftp', sftp)):
        if not hasattr(module, 'DOCKERFILE'): outcomes[name] = {'skipped': 'pinned image; the pin moves with panel releases'}; continue
        saved = module.ROOT / 'image.json'
        if not saved.exists(): outcomes[name] = {'skipped': 'not set up'}; continue
        trusted(saved); current = json.loads(saved.read_text())
        step(f'rebuilding the {name} image')
        context = module.ROOT / 'build'; context.mkdir(mode=0o700, exist_ok=True); trusted(context, directory=True)
        atomic(context / 'Dockerfile', module.DOCKERFILE); atomic(context / '.dockerignore', '*\n!Dockerfile\n')
        tag = f'hosting-{name}:{module.IMAGE_VERSION}'
        command(['docker', 'build', '--no-cache', '--tag', tag, '--file', context / 'Dockerfile', context], timeout=900)
        image_id = command(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']).strip()
        listing = lambda image: command(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'dpkg-query', image, '-W', '-f', '${Package}\\t${Version}\\n']).splitlines()
        diff = compare(listing(current['image_id']), listing(image_id)) if image_id != current['image_id'] else {'changed': [], 'added': [], 'removed': []}
        if not (diff['changed'] or diff['added'] or diff['removed']):
            if image_id != current['image_id']: command(['docker', 'tag', current['image_id'], tag])
            outcomes[name] = {'changed': []}; continue
        atomic(saved, json.dumps({**current, 'image_id': image_id, 'previous_image_id': current['image_id'], 'built_at': time.time()}, indent=2))
        step(f'restarting {name} on the rebuilt image')
        if name == 'mail': mail.deploy(host, mail.php_sites(ledger))
        else:
            registry = sftp.registry()
            if registry: sftp.deploy(host, registry)
        outcomes[name] = diff
    return outcomes


def rebuild(ledger, host, job, step):
    """The rebuild job: branch images, sites onto them, infrastructure images; a report for the page."""
    REPORTS.mkdir(mode=0o700, exist_ok=True); trusted(REPORTS, directory=True)
    mark_started()
    context = Path(__file__).resolve().parent.parent / 'templates/php-image'
    recipe = hashlib.sha256(b''.join(path.name.encode() + path.read_bytes() for path in sorted(context.iterdir()))).hexdigest()
    stamp = datetime.now().strftime('%Y%m%d%H%M')
    runtimes = catalog()
    report = {'id': job['id'], 'started': time.time(), 'branches': {}, 'sites': [], 'infrastructure': {}, 'skipped_sites': []}
    for branch in sorted(runtimes):
        step(f'rebuilding PHP {branch} with fresh packages')
        fresh = build_fresh(branch, recipe, context, stamp)
        current = runtimes[branch]
        diff = compare(current.get('packages', []), fresh['packages'])
        if fresh['image_id'] == current['image_id'] or not (diff['changed'] or diff['added'] or diff['removed']):
            if fresh['image_id'] != current['image_id']: remove_image(fresh['image'])
            report['branches'][branch] = {'php_version': current.get('php_version'), 'changed': [], 'added': [], 'removed': [], 'kept': True}
            continue
        older = current.get('previous', {}).get('image')
        runtimes[branch] = {**fresh, 'previous': {k: current[k] for k in ('image', 'image_id') if k in current} | {'php_version': current.get('php_version'), 'built_at': current.get('built_at')}}
        atomic(CATALOG, json.dumps({'schema': 1, 'runtimes': runtimes}, indent=2))
        if older and older != current['image']: remove_image(older)
        report['branches'][branch] = {**diff, 'php_version': fresh['php_version'], 'previous_php_version': current.get('php_version'), 'kept': False}
    step('rolling sites onto the rebuilt images')
    for site in sites_on_older_images(ledger, runtimes):
        try:
            queued = ledger.submit_runtime(str(uuid.uuid4()), 'switch', site['id'], {'branch': site['branch']})
            report['sites'].append({**site, 'job': queued['id']})
        except ValueError as exc:
            report['skipped_sites'].append({**site, 'reason': str(exc)})
    report['infrastructure'] = rebuild_infrastructure(host, ledger, step)
    report['finished'] = time.time()
    atomic(REPORTS / (job['id'] + '.json'), json.dumps(report, indent=2))
    atomic(REPORTS / 'latest.json', json.dumps(report, indent=2))
    return report


def latest_report():
    path = REPORTS / 'latest.json'
    if not path.exists(): return None
    trusted(path); return json.loads(path.read_text())


def overview(ledger):
    """What the versions page shows per branch: image age, package count, the last run, sites behind."""
    runtimes = catalog()
    behind = {}
    for site in sites_on_older_images(ledger, runtimes): behind.setdefault(site['branch'], []).append(site['name'])
    branches = {b: {'php_version': r.get('php_version'), 'built_at': r.get('built_at'), 'packages': len(r.get('packages', [])),
                    'previous': r.get('previous'), 'sites_behind': sorted(behind.get(b, []))} for b, r in runtimes.items()}
    return {'branches': branches, 'policy': policy(), 'schedule': schedule(), 'latest': latest_report(),
            'next_due': next_window(time.time())}


def next_window(now):
    settings = policy()
    earliest = schedule()['last_started'] + settings['every_days'] * 86400 - 3600
    moment = datetime.fromtimestamp(max(now, earliest))
    candidate = moment.replace(hour=settings['hour'], minute=0, second=0, microsecond=0)
    if candidate.timestamp() < max(now, earliest): candidate += timedelta(days=1)
    return candidate.timestamp()
