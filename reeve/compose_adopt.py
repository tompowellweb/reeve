"""Compose runtime for mode 2 packages: saved plans, the hosting overlay, retained volumes,
quota, verification, health and routing. Package deployment compiles the plan; this module
applies it. Source files stay unchanged.
"""
import hashlib
import copy
import json
import os
import re
import time
from pathlib import Path

import yaml

from . import compose_inspect as ci
from .core import request_id, validate_domains
from .host import OPS, SITES, PROXY, atomic, trusted, preflight, apply_quota, verify_quota, quota_record, project_id

STORE = OPS / 'panel/worker/adoptions'


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS adopted_projects (project_name TEXT PRIMARY KEY, site_id TEXT UNIQUE NOT NULL)')
    db.execute('pragma user_version=11')


def plan_path(ident):
    request_id(ident)
    return STORE / ident


def read(row):
    path = plan_path(row['id']) / 'plan.json'
    trusted(path)
    return json.loads(path.read_text())


def save(row, plan):
    atomic(plan_path(row['id']) / 'plan.json', json.dumps(plan, sort_keys=True))


def docker(args, **kwargs):
    return ci.run(['docker', *args], **kwargs)



def source_check(plan):
    root = SITES / plan['name']
    trusted(root, directory=True)
    for source in plan['sources']:
        path = ci.inside(root, source['file'])
        if hashlib.sha256(ci.regular(path)).hexdigest() != source['sha256']:
            raise ValueError('Original project inputs changed. Restore the reviewed inputs before retrying this adoption.')




def project_containers(plan):
    ids = docker(['container', 'ls', '--all', '--filter', 'label=com.docker.compose.project=' + plan['project_name'], '--format', '{{json .ID}}'])
    if len(ids) > 64: raise ValueError('Project container count exceeds the supported limit.')
    return docker(['container', 'inspect', *ids]) if ids else []


def volume_record(name):
    names = docker(['volume', 'ls', '--filter', 'name=^' + re.escape(name) + '$', '--format', '{{json .Name}}'], raw=False)
    return docker(['volume', 'inspect', name])[0] if name in names else None


def verify_volume(plan, record):
    owner = (record.get('Labels') or {}).get('com.docker.compose.project')
    if owner and owner != plan['project_name']:
        raise ValueError('Retained volume belongs to another Compose project.')
    if record['Driver'] != 'local' or record.get('Options'):
        raise ValueError('Retained volume has unsupported host storage options.')
    for other in STORE.glob('*/plan.json'):
        saved = json.loads(other.read_text())
        if saved['name'] != plan['name'] and any(v['name'] == record['Name'] for v in saved['volumes'].values()):
            raise ValueError('Retained volume is reserved by another adopted site.')
    path = Path(record['Mountpoint'])
    expected = Path('/srv/docker/volumes') / record['Name'] / '_data'
    if path != expected or path.resolve() != path or not path.is_dir():
        raise ValueError('Retained volume mountpoint is not the expected local Docker storage.')
    ids = docker(['container', 'ls', '--all', '--filter', 'volume=' + record['Name'], '--format', '{{json .ID}}'])
    if len(ids) > 64: raise ValueError('Retained volume has too many consumers.')
    for container in docker(['container', 'inspect', *ids]) if ids else []:
        if container['Config'].get('Labels', {}).get('com.docker.compose.project') != plan['project_name']:
            raise ValueError('Retained volume is shared with another project; adoption cannot change its quota.')
    return path


def filesystem_boundaries(roots, mountinfo=Path('/proc/self/mountinfo')):
    """Quota assignment must not cross bind mounts or change externally hard-linked files."""
    roots = list({Path(root) for root in roots})
    for line in mountinfo.read_text().splitlines():
        path = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4]))
        if any(path == root or path.is_relative_to(root) for root in roots):
            raise ValueError('Persistent storage contains a nested mount. Its real boundary needs review before quota assignment.')
    linked = {}
    for root in roots:
        for folder, directories, files in os.walk(root, followlinks=False):
            for name in files:
                info = (Path(folder) / name).lstat()
                if info.st_nlink > 1:
                    key = (info.st_dev, info.st_ino)
                    item = linked.setdefault(key, [info.st_nlink, 0])
                    item[1] += 1
    if any(count < total for total, count in linked.values()):
        raise ValueError('Persistent data has hard links outside the inventoried storage. Adoption cannot change another storage boundary.')


class Override(dict):
    pass


class Dumper(yaml.SafeDumper):
    pass


Dumper.add_representer(Override, lambda dumper, value: dumper.represent_mapping('!override', value))


def overlay(row, plan):
    ingress = 'hosting-ingress-' + row['name']
    services = {}
    for service, spec in plan['model']['services'].items():
        networks = copy.deepcopy(spec.get('networks', {'default': {}}))
        if service == plan['route']['web_service']:
            networks['hosting_ingress'] = {'aliases': ['web-' + row['name']]}
        # Preserve explicit service caps; unset values remain unlimited. Docker's inherited
        # layer default is explicitly disabled, as with managed Create.
        services[service] = {'image': plan['images'][service], 'networks': Override(networks),
            'labels': {'hosting.adopted': row['id']}, 'pull_policy': 'never',
            'logging': Override({'driver': 'local', 'options': {'max-size': '10m', 'max-file': '3'}}),
            'storage_opt': Override({'size': str(spec.get('storage_opt', {}).get('size', '0'))}),
            'pids_limit': spec.get('pids_limit', -1)}
    networks = copy.deepcopy(plan['model'].get('networks', {}))
    networks['hosting_ingress'] = {'name': ingress, 'internal': True}
    return {'services': services, 'networks': Override(networks),
        'volumes': Override({key: {'external': True, 'name': v['name']} for key, v in plan['volumes'].items()})}


def perform(host, row, step, before_up=None):
    plan = read(row)
    preflight()
    source_check(plan)
    path = plan_path(row['id'])
    project = plan['project_name']
    existing = project_containers(plan)
    original_ids = {c['id'] for c in plan['original_containers']}
    for container in existing:
        if plan['stage'] == 'reviewed' and not container['State']['Running']:
            raise ValueError('A prepared service was stopped after review; adoption will preserve that stop.')
        if container['Id'] not in original_ids and container['Config'].get('Labels', {}).get('hosting.adopted') != row['id']:
            raise ValueError('An unexpected container now uses the reserved project. Resolve it before retrying.')
    step('recording retained storage identities')
    records = {}
    for key, volume in plan['volumes'].items():
        record = volume_record(volume['name'])
        if record:
            verify_volume(plan, record)
            stat = Path(record['Mountpoint']).stat()
            identity = [stat.st_dev, stat.st_ino]
            if volume.get('inode') and volume['inode'] != identity:
                raise ValueError('Retained volume storage was replaced; its original data must be recovered.')
            volume['inode'] = identity
            if volume.get('created_at') and record['CreatedAt'] != volume['created_at']:
                raise ValueError('A retained volume was replaced; do not attach potentially empty replacement data.')
            if volume.get('create_intended') and (record.get('Labels') or {}).get('hosting.adopted') != row['id']:
                raise ValueError('A new volume identity was claimed by another operation.')
            volume['created_at'] = record['CreatedAt']
        elif volume.get('created_at') or volume['external'] or plan['original_containers']:
            raise ValueError('A retained or external volume is missing; adoption will not replace it with an empty volume.')
        if not record: volume['create_intended'] = True
        records[key] = record
    filesystem_boundaries([SITES / row['name'], *[Path(record['Mountpoint']) for record in records.values() if record]])
    # Persist original volume identities before limits, creation or container recreation.
    save(row, plan)
    step('applying site data quota')
    mb = json.loads(row['payload'])['data_mb']
    apply_quota(SITES / row['name'], row['project'], mb)
    for key, record in records.items():
        volume = plan['volumes'][key]
        if not record:
            docker(['volume', 'create', '--label', 'hosting.adopted=' + row['id'], volume['name']], raw=True)
            record = volume_record(volume['name'])
            stat = Path(record['Mountpoint']).stat()
            volume['inode'] = [stat.st_dev, stat.st_ino]
            volume['created_at'] = record['CreatedAt']
            users = {tuple(str(spec.get('user') or '').split(':')) for spec in plan['model']['services'].values()
                     if any(m.get('source') == key and m['type'] == 'volume' for m in spec.get('volumes', []))}
            # Image-default users (mode 2) leave the volume to the image's own entrypoint.
            if len(users) == 1 and all(p.isdigit() for p in next(iter(users))) and not any(Path(record['Mountpoint']).iterdir()):
                user = next(iter(users))
                uid, gid = int(user[0]), int(user[1] if len(user) > 1 else user[0])
                os.chown(record['Mountpoint'], uid, gid)
            save(row, plan)
        apply_quota(verify_volume(plan, record), row['project'], mb)
    step('generating hosting overlay')
    model = plan['model']
    if plan.get('package_id'):
        # Native config has already resolved variables. Keep literal dollar signs on reread.
        def literal(value):
            if isinstance(value, str): return value.replace('$', '$$')
            if isinstance(value, list): return [literal(v) for v in value]
            if isinstance(value, dict): return {k: literal(v) for k, v in value.items()}
            return value
        model = literal(model)
    atomic(path / 'resolved.compose.json', json.dumps(model, sort_keys=True))
    atomic(path / 'compose.hosting.yaml', yaml.dump(overlay(row, plan), Dumper=Dumper, sort_keys=False))
    args = ['compose', '--project-directory', str(SITES / row['name']), '--env-file', '/dev/null', '--project-name', project,
        '--file', str(path / 'resolved.compose.json'), '--file', str(path / 'compose.hosting.yaml')]
    resolved = docker([*args, 'config', '--format', 'json'])
    for service, spec in resolved['services'].items():
        if spec['image'] != plan['images'][service] or set(spec['networks']) != (set(plan['model']['services'][service].get('networks', {'default': {}})) | ({'hosting_ingress'} if service == plan['route']['web_service'] else set())):
            raise ValueError('Generated hosting overlay did not retain image or network isolation.')
    plan['stage'] = 'container recreation intended'
    save(row, plan)
    if before_up: before_up(plan, args)
    step('applying prepared project with retained volumes')
    docker([*args, 'up', '--detach', '--wait', '--wait-timeout', '90', '--pull', 'never', '--no-build'], raw=True, timeout=120)
    for container in project_containers(plan):
        service = container['Config']['Labels']['com.docker.compose.service']
        if plan['model']['services'][service].get('pids_limit', -1) == -1:
            docker(['update', '--pids-limit', '-1', container['Id']], raw=True)
    source_check(plan)
    verify_runtime(row, plan)
    plan['stage'] = 'runtime verified'
    save(row, plan)
    step('publishing and verifying HTTPS')
    route = plan['route']
    host.publish(row, 'hosting-ingress-' + row['name'], [route['domain'], *route['aliases']],
        upstream='web-' + row['name'] + ':' + str(route['internal_port']))
    verify_https([route['domain'], *route['aliases']])
    plan['stage'] = 'published'
    save(row, plan)


def verify_process_limit(container, expected, proc=Path('/proc'), cgroups=Path('/sys/fs/cgroup')):
    unlimited = expected in (None, 0, -1)
    actual = container['HostConfig'].get('PidsLimit')
    if (unlimited and actual not in (None, 0, -1)) or (not unlimited and actual != expected):
        raise ValueError('Container process limit differs from the requested limit.')
    pid = container['State'].get('Pid', 0)
    if not isinstance(pid, int) or pid <= 0: raise ValueError('Container has no live process for limit verification.')
    entries = (proc / str(pid) / 'cgroup').read_text().splitlines()
    unified = [line[3:] for line in entries if line.startswith('0::/')]
    if len(unified) != 1: raise ValueError('Container unified cgroup could not be verified.')
    actual_limit = (cgroups / unified[0].lstrip('/') / 'pids.max').read_text().strip()
    if actual_limit != ('max' if unlimited else str(expected)):
        raise ValueError('Container kernel process limit differs from the requested limit.')


def verify_runtime(row, plan):
    current = project_containers(plan)
    if len(current) != len(plan['model']['services']): raise ValueError('Project container count changed.')
    definitions = dict(plan['model'].get('networks', {}), hosting_ingress={'name': 'hosting-ingress-' + row['name'], 'internal': True})
    permitted = {c['Id'] for c in current}
    for key, definition in definitions.items():
        network_name = definition.get('name', plan['project_name'] + '_' + key)
        net = docker(['network', 'inspect', network_name])[0]
        if net['Driver'] != 'bridge' or net['Internal'] != bool(definition.get('internal')) or net.get('Options'):
            raise ValueError('Adopted network differs from the preserved application topology.')
        for ident, member in (net.get('Containers') or {}).items():
            if ident not in permitted and not (key == 'hosting_ingress' and member['Name'] == 'hosting-edge'):
                raise ValueError('Application network is shared with an unrelated container.')
    for container in current:
        service = container['Config'].get('Labels', {}).get('com.docker.compose.service')
        if service not in plan['images'] or container['Image'] != plan['images'][service]:
            raise ValueError('Running service image does not match the saved image identity.')
        config, hostconfig = container['Config'], container['HostConfig']
        expected_user = plan['model']['services'][service].get('user')
        if (expected_user is not None and config['User'] != str(expected_user)) or hostconfig['Privileged'] or hostconfig.get('PortBindings'):
            raise ValueError('Container user, privilege or published port check failed.')
        if hostconfig.get('CapAdd'): raise ValueError('Container gained capabilities.')
        spec = plan['model']['services'][service]
        logs = hostconfig.get('LogConfig', {})
        if logs.get('Type') != 'local' or logs.get('Config', {}).get('max-size') != '10m' or logs.get('Config', {}).get('max-file') != '3':
            raise ValueError('Container logging limits were not applied.')
        if hostconfig['ReadonlyRootfs'] != bool(spec.get('read_only')):
            raise ValueError('Container root filesystem mode differs from the reviewed configuration.')
        if hostconfig['Memory'] != int(spec.get('mem_limit', 0)) or hostconfig['NanoCpus'] != round(float(spec.get('cpus', 0)) * 1e9):
            raise ValueError('Container resource limits differ from the original explicit limits.')
        verify_process_limit(container, spec.get('pids_limit', -1))
        layer = container['GraphDriver']['Data']['UpperDir']
        layer_project, _ = project_id(layer)
        if str(spec.get('storage_opt', {}).get('size', '0')) == '0' and quota_record(layer_project)['hard_bytes'] != 0:
            raise ValueError('The inherited writable-layer cap was not disabled.')
        expected = {definitions[key].get('name', plan['project_name'] + '_' + key) for key in spec.get('networks', {'default': {}})}
        if service == plan['route']['web_service']: expected.add('hosting-ingress-' + row['name'])
        if set(container['NetworkSettings']['Networks']) != expected:
            raise ValueError('Container has unexpected network access.')
        if not container['State']['Running'] or container['State'].get('Health', {}).get('Status', 'healthy') != 'healthy':
            raise ValueError('An adopted service is not healthy.')
        declared = {m['target']: m for m in plan['model']['services'][service].get('volumes', [])}
        if {m['Destination'] for m in container['Mounts'] if m['Type'] != 'tmpfs'} != {target for target, mount in declared.items() if mount['type'] != 'tmpfs'}:
            raise ValueError('Container is missing a declared persistent mount.')
        for mount in container['Mounts']:
            if mount['Type'] == 'tmpfs': continue
            wanted = declared.get(mount['Destination'])
            if not wanted or wanted['type'] != mount['Type'] or bool(wanted.get('read_only')) == bool(mount['RW']):
                raise ValueError('Container mount coverage or write mode differs from the reviewed plan.')
            source = plan['volumes'][wanted['source']]['name'] if mount['Type'] == 'volume' else wanted['source']
            if source != (mount.get('Name') if mount['Type'] == 'volume' else mount['Source']):
                raise ValueError('Container attached different data from the retained mount identity.')
        if service == plan['route']['web_service']:
            ip = container['NetworkSettings']['Networks']['hosting-ingress-' + row['name']]['IPAddress']
            http_ready('http://' + ip + ':' + str(plan['route']['internal_port']) + '/', plan['route']['domain'])
    mb = json.loads(row['payload'])['data_mb']
    verify_quota(SITES / row['name'], row['project'], mb)
    for volume in plan['volumes'].values():
        record = volume_record(volume['name'])
        if not record or record['CreatedAt'] != volume['created_at']: raise ValueError('Retained volume identity changed.')
        stat = Path(record['Mountpoint']).stat()
        if volume['inode'] != [stat.st_dev, stat.st_ino]: raise ValueError('Retained volume directory was replaced.')
        verify_quota(verify_volume(plan, record), row['project'], mb)


def http_ready(url, host, timeout=120):
    """Images without a health check may still be starting after `up`; allow a bounded warm-up."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            ci.run(['curl', '--noproxy', '*', '--fail', '--silent', '--show-error', '--max-time', '10',
                    '--header', 'Host: ' + host, url], raw=True)
            return
        except ValueError:
            if time.monotonic() >= deadline:
                raise ValueError('The HTTP service did not answer on its internal port within ' + str(timeout) + ' seconds.') from None
            time.sleep(3)


def verify_https(domains):
    for domain in validate_domains(domains):
        ci.run(['curl', '--noproxy', '*', '--fail', '--silent', '--show-error', '--max-time', '10',
            '--cacert', str(PROXY / 'data/caddy/pki/authorities/local/root.crt'), '--resolve', domain + ':443:127.0.0.1',
            'https://' + domain + '/'], raw=True)


def health(row):
    result = {'application': 'unknown', 'container': 'unknown', 'quota': None}
    try:
        plan = read(row)
        current = project_containers(plan)
        running = len(current) == len(plan['images']) and all(c['State']['Running'] for c in current)
        checks = [c['State'].get('Health', {}).get('Status') for c in current]
        state = ('healthy' if all(h == 'healthy' for h in checks) else ('unhealthy' if 'unhealthy' in checks else 'running; health incomplete')) if running else 'unhealthy'
        result.update(application=state, container='running' if running else 'needs attention', quota=quota_record(row['project']))
    except (OSError, ValueError, RuntimeError, KeyError):
        pass
    return result


def public(row):
    plan = read(row)
    result = copy.deepcopy(plan['summary'])
    containers = project_containers(plan)
    for service in result['services']:
        current = [c for c in containers if c['Config'].get('Labels', {}).get('com.docker.compose.service') == service['name']]
        service['instances'] = [{'id': c['Id'][:12], 'state': c['State']['Status'], 'health': c['State'].get('Health', {}).get('Status', 'Not configured')} for c in current]
    result.pop('next_step', None)
    result.update(mode='adopted', route=plan['route'], stage=plan['stage'], sources=plan['sources'],
        volume_names=[v['name'] for v in plan['volumes'].values()])
    return result


def change_domains(host, row, domains):
    plan = read(row)
    verify_runtime(row, plan)
    host.publish(row, 'hosting-ingress-' + row['name'], domains,
        upstream='web-' + row['name'] + ':' + str(plan['route']['internal_port']))
    verify_https(domains)
    plan['route'].update(domain=domains[0], aliases=domains[1:])
    save(row, plan)
