"""Bounded, read-only discovery. An inventory is neither a backup nor restore proof.

Only root-owned reports are written. Never return Docker environments, commands,
labels, file contents or helper diagnostics through the worker protocol.
"""
import hashlib
import json
import stat
import time
from pathlib import Path

import yaml

from . import compose_inspect as ci
from .core import request_id
from .host import OPS, SITES, atomic, trusted

STORE = OPS / 'panel/worker/recovery-inventory'
VOLUMES = Path('/srv/docker/volumes')
MAX_REPORT = 512 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def location(row):
    request_id(row['id'])
    return STORE / (row['id'] + '.json')


def read(row):
    path = location(row)
    if not path.exists() and not path.is_symlink():
        return {'inventory': None, 'last_attempt': None, 'error': ''}
    trusted(STORE, directory=True)
    raw = ci.regular(path)
    result = json.loads(raw)
    if result.get('schema') != 1 or result.get('site_id') != row['id']:
        raise ValueError('Recovery inventory record needs operator review.')
    return result


def docker(args):
    return ci.run(['docker', *args])


def metadata(path):
    """Observe the boundary, never follow a link into another site's data."""
    path = Path(path)
    if path.resolve() != path:
        return {'state': 'symlink; needs review'}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {'state': 'missing'}
    return {'state': 'present', 'uid': info.st_uid, 'gid': info.st_gid,
            'mode': oct(stat.S_IMODE(info.st_mode)),
            'kind': 'directory' if stat.S_ISDIR(info.st_mode) else 'file',
            'device': info.st_dev, 'inode': info.st_ino}


def collect(ledger, row):
    deadline = time.monotonic() + 75

    def inspect_docker(args):
        if time.monotonic() >= deadline:
            raise ValueError('Recovery inspection time budget exceeded.')
        return docker(args)

    root = SITES / row['name']
    trusted(root, directory=True)
    data = json.loads(row['payload'])
    data.setdefault('runtime', 'static')
    report = {'schema': 1, 'kind': 'recovery-inventory', 'site_id': row['id'], 'name': row['name'],
              'observed_at': time.time(), 'coverage': 'incomplete', 'restore': 'not verified',
              'services': [], 'storage': [], 'inputs': [], 'gaps': [],
              'domains': ledger.domains(row), 'runtime': data['runtime'],
              'identity': {'uid': row['uid'], 'quota_project': row['project']},
              'scope': 'Storage boundaries and reconstruction references; no file contents or backup artifacts.'}

    def gap(code, subject, message):
        item = dict(code=code, subject=subject, message=message)
        if item not in report['gaps']:
            report['gaps'].append(item)

    inputs = {}

    def control(path, purpose):
        path = Path(path)
        # All paths come from retained trusted plans or generated template paths.
        # Parents must be trusted too: O_NOFOLLOW on the file alone is insufficient.
        for parent in path.parents:
            if parent == Path('/'): break
            trusted(parent, directory=True)
        raw = ci.regular(path)
        inputs[str(path)] = {'path': str(path), 'purpose': purpose,
                             'sha256': hashlib.sha256(raw).hexdigest(), **metadata(path)}
        return raw

    definitions, expected, projects = {}, {}, set()
    pinned_images, expected_networks = {}, {}
    engine_hints = {}
    if data['runtime'] == 'compose':
        from . import compose_adopt as ca
        saved = ca.plan_path(row['id'])
        plan = json.loads(control(saved / 'plan.json', 'Adoption plan; contains private configuration'))
        for filename in ('resolved.compose.json', 'compose.hosting.yaml'):
            control(saved / filename, 'Saved deployment configuration')
        for source in plan['sources']:
            path = ci.inside(root, source['file'])
            raw = control(path, 'Original Compose or environment input')
            if hashlib.sha256(raw).hexdigest() != source['sha256']:
                gap('source-changed', str(path.relative_to(root)), 'Original input differs from the adopted deployment. Review before recovery.')
        model, project = plan['model'], plan['project_name']
        definitions = {key: value['name'] for key, value in plan['volumes'].items()}
        engine_hints = {s['name']: s.get('database') for s in plan['summary']['services']}
        for name, spec in model['services'].items():
            expected[(project, name)] = spec
            pinned_images[(project, name)] = plan['images'][name]
            expected_networks[(project, name)] = sorted(
                [model.get('networks', {}).get(n, {}).get('name', project + '_' + n)
                 for n in spec.get('networks', {'default': {}})] +
                (['hosting-ingress-' + row['name']] if name == plan['route']['web_service'] else []))
        projects.add(project)
    else:
        from .database_site import paths, state
        database = state(row)
        site_metadata = yaml.safe_load(control(root / 'hosting.yaml', 'Site settings and resource limits'))
        files = [root / 'compose.yml']
        if database:
            dbroot, private = paths(row)
            control(private, 'Database image, identity and private credentials')
            files.append(dbroot / 'compose.yml')
            engine_hints['database'] = database['engine']
        for path in files:
            model = yaml.safe_load(control(path, 'Managed Compose configuration'))
            project = model['name']; projects.add(project)
            for name, spec in model['services'].items():
                expected[(project, name)] = spec
                if name == 'database' and database:
                    pinned_images[(project, name)] = database.get('image_id')
                elif name == 'php':
                    pinned_images[(project, name)] = site_metadata.get('php_image', {}).get('id')
                expected_networks[(project, name)] = sorted(model['networks'][n]['name'] for n in spec.get('networks', {}))
                for env in spec.get('env_file', []):
                    control(Path(env if isinstance(env, str) else env['path']), 'Private application environment')

    # The whole site directory matters, including files not mounted by a container.
    # Mount entries below describe relationships; they are not separate copy tasks.
    storage = {}

    def boundary(kind, source, service=None, target=None, writable=False, declared=False):
        key = kind + ':' + source
        if key not in storage:
            allowed = kind == 'bind' and Path(source).is_relative_to(root)
            if kind == 'volume':
                # Names are resolved identifiers, never arbitrary host paths.
                import re
                allowed = bool(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', source))
                path = VOLUMES / source / '_data' if allowed else None
            else:
                path = Path(source)
            item = {'kind': kind, 'source': source, 'mounts': [], 'declared': declared,
                    'classification': 'unclassified', 'method': 'not configured',
                    'boundary': metadata(path) if allowed else {'state': 'outside supported storage boundary'}}
            if item['boundary']['state'] != 'present':
                gap('storage-unavailable', source, 'Storage is missing or outside the supported boundary; operator review required.')
            storage[key] = item
        item = storage[key]
        item['declared'] |= declared
        if service:
            mount = {'service': service, 'target': target, 'writable': writable}
            if mount not in item['mounts']: item['mounts'].append(mount)
        return key

    boundary('bind', str(root), declared=True)
    # Normalize the generated template's short syntax and adopted native long syntax.
    def mounts(spec):
        result = []
        for mount in spec.get('volumes', []):
            if isinstance(mount, str):
                parts = mount.split(':')
                source, target = parts[:2]
                mount = {'type': 'bind' if source.startswith('/') else 'volume',
                         'source': source, 'target': target, 'read_only': len(parts) == 3 and parts[2] == 'ro'}
            source = mount.get('source', '')
            if mount['type'] == 'volume': source = definitions.get(source, source)
            result.append((mount['type'], source, mount['target'], not mount.get('read_only', False)))
        return result

    for (project, name), spec in expected.items():
        for kind, source, target, writable in mounts(spec):
            if kind in ('bind', 'volume'):
                boundary(kind, source, name, target, writable, declared=True)
                path = Path(source)
                # Hash mounted control files, but never traverse the app-owned data tree.
                if kind == 'bind' and (path.is_relative_to(root / 'conf') or path.is_relative_to(root / 'database/conf')):
                    control(path, 'Mounted server configuration')

    current = []
    for project in sorted(projects):
        ids = inspect_docker(['container', 'ls', '--all', '--filter', 'label=com.docker.compose.project=' + project,
                      '--format', '{{json .ID}}'])
        if len(ids) > 64: raise ValueError('Too many project containers for bounded inspection.')
        if ids: current.extend(inspect_docker(['container', 'inspect', *ids]))
    if len(current) > 64 or len(expected) > 64:
        raise ValueError('Too many services for bounded inspection.')
    found, images, networks = set(), {}, {}
    for container in sorted(current, key=lambda c: c['Name']):
        config, hc = container['Config'], container['HostConfig']
        labels = config.get('Labels') or {}
        project, name = labels.get('com.docker.compose.project'), labels.get('com.docker.compose.service')
        key = (project, name); found.add(key)
        spec = expected.get(key)
        if spec is None:
            gap('unexpected-service', name or 'Unknown service', 'Container is not in the saved deployment.')
        image_id = container['Image']
        if image_id not in images:
            image = inspect_docker(['image', 'inspect', image_id])[0]
            images[image_id] = {'id': image_id, 'digests': sorted(image.get('RepoDigests') or []),
                               'architecture': image['Architecture'], 'os': image['Os'],
                               'declared_volumes': sorted(image.get('Config', {}).get('Volumes') or {})}
        observed_mounts = []
        temporary = set(hc.get('Tmpfs') or {})
        for mount in container.get('Mounts', []):
            kind, target = mount['Type'], mount['Destination']
            if kind == 'tmpfs':
                temporary.add(target); continue
            source = mount.get('Name', '') if kind == 'volume' else mount.get('Source', '')
            observed_mounts.append((kind, source, target, bool(mount.get('RW'))))
            boundary(kind, source, name, target, bool(mount.get('RW')))
        if spec is not None:
            if sorted(observed_mounts) != sorted(m for m in mounts(spec) if m[0] != 'tmpfs'):
                gap('mounts-changed', name, 'Actual persistent mounts differ from the saved deployment.')
            # Tag names can move: compare the saved immutable identity where available.
            expected_image = pinned_images.get(key)
            if expected_image and image_id != expected_image:
                gap('image-changed', name, 'Actual image differs from the saved deployment.')
            elif config.get('Image') != spec['image']:
                gap('image-reference-changed', name, 'Image reference differs from the saved deployment; review exact image identity.')
            if config.get('User') != str(spec.get('user', '')) or bool(hc.get('ReadonlyRootfs')) != bool(spec.get('read_only')):
                gap('service-changed', name, 'Container identity or root filesystem mode differs from the saved deployment.')
        targets = {m[2] for m in observed_mounts} | temporary
        hidden = set(images[image_id]['declared_volumes']) - targets
        if hidden: gap('image-storage', name, 'Image-declared storage is not accounted for by an observed mount.')
        service_networks = []
        for network_name in sorted(container['NetworkSettings']['Networks']):
            if network_name not in networks:
                network = inspect_docker(['network', 'inspect', network_name])[0]
                networks[network_name] = {'name': network_name, 'internal': network['Internal'], 'driver': network['Driver']}
            service_networks.append(network_name)
        if spec is not None and service_networks != expected_networks[key]:
            gap('networks-changed', name, 'Actual network membership differs from the saved deployment.')
        read_only = bool(hc.get('ReadonlyRootfs'))
        if not read_only:
            gap('writable-layer', name, 'Writable container layer is unclassified. Check writes during representative application use.')
        hint = engine_hints.get(name)
        if hint:
            gap('database-method', name, 'Database candidate: inventory databases and credentials, then test a consistency and restore method.')
        report['services'].append({'name': name or 'Unknown service', 'project': project,
            'container_id': container['Id'], 'image_id': image_id, 'user': config.get('User', ''),
            'read_only': read_only, 'state': container['State']['Status'],
            'health': container['State'].get('Health', {}).get('Status', 'not declared'),
            'database_hint': hint, 'temporary_mounts': sorted(temporary),
            'networks': service_networks, 'depends_on': sorted((spec or {}).get('depends_on') or {}),
            'image_declared_volumes': images[image_id]['declared_volumes']})
    for project, name in sorted(set(expected) - found):
        gap('missing-service', name, 'Saved service has no container; actual storage and image could not be verified.')
    report['expected_services'] = len(expected)
    report['storage'] = sorted(storage.values(), key=lambda x: (x['kind'], x['source']))
    report['inputs'] = sorted(inputs.values(), key=lambda x: x['path'])
    report['images'] = sorted(images.values(), key=lambda x: x['id'])
    report['networks'] = sorted(networks.values(), key=lambda x: x['name'])
    from .schedules import list_schedules
    schedules = list_schedules(ledger, row['id'])
    # Keep task command text private; the ledger is a required recovery input.
    report['schedule_count'] = len(schedules)
    report['schedule_revision'] = digest([{k: s[k] for k in ('name', 'interval', 'enabled', 'settings')} for s in schedules])
    report['host_requirements'] = ['Panel release and SQLite state', 'Docker engine and network configuration',
        'XFS mount and quota configuration', 'Caddy routes, configuration and TLS state',
        'Toolbox recipes, access configuration and runtime artifacts', 'Independent backup credentials and recovery runbook']
    for code, message in (
        ('owner-inventory', 'Owner must account for external services, queues, indexes, hidden state and rebuildable data.'),
        ('consistency', 'Select and test a consistency method covering files and all stateful services together.'),
        ('reconstruction', 'Retain configuration, secrets, exact images and host prerequisites independently of this machine.'),
        ('restore-test', 'Prove an isolated application restore, including runtime-only data absent from seed files.')):
        gap(code, 'Application', message)
    if row['state'] != 'succeeded': gap('setup', 'Application', 'Finish or resolve site setup before backup preparation.')
    # Operational timestamps, process/container identities and data inode numbers must
    # not invalidate configuration review merely because a container was restarted.
    material = {k: report[k] for k in ('domains', 'runtime', 'identity', 'schedule_revision', 'networks', 'images')}
    material['inputs'] = [{k: v for k, v in item.items() if k not in ('device', 'inode')} for item in report['inputs']]
    material['storage'] = [dict(item, boundary={k: v for k, v in item['boundary'].items()
                                               if k not in ('device', 'inode')}) for item in report['storage']]
    material['services'] = [{k: v for k, v in item.items() if k not in ('container_id', 'state', 'health')} for item in report['services']]
    report['revision'] = digest(material)
    return report


def scan(ledger, row):
    """Persist last attempt independently; a failed inspection retains its predecessor."""
    previous = read(row)
    STORE.mkdir(mode=0o700, exist_ok=True)
    trusted(STORE, directory=True)
    record = {'schema': 1, 'site_id': row['id'], 'last_attempt': time.time(),
              'inventory': previous.get('inventory'), 'error': ''}
    try:
        result = collect(ledger, row)
        prior = record['inventory']
        result['changed'] = bool(prior and prior['revision'] != result['revision'])
        result['previous_revision'] = prior['revision'] if prior else None
        if len(json.dumps(result).encode()) > MAX_REPORT:
            raise ValueError('Inventory exceeds the bounded report size.')
        record['inventory'] = result
    except Exception:
        # Docker errors and parsing exceptions can contain private configuration.
        record['error'] = 'Inspection failed. Check saved configuration, container access and storage permissions; previous observation retained.'
    atomic(location(row), json.dumps(record, sort_keys=True))
    return record
