"""Prepare a PHP replacement before cutover; retain exact rollback images and mappings."""
import copy
import json
import time
from pathlib import Path

import yaml

from .host import OPS, SITES, atomic, command, trusted, project_id, quota_record, verify_quota
from .php_runtime import REQUIRED_EXTENSIONS, ensure

SAVED = OPS / 'panel/worker/php-switches'


def snapshot_path(ident):
    from .core import request_id
    return SAVED / (request_id(ident) + '.json')


def validate_service(service, branch):
    image_id = command(['docker', 'image', 'inspect', service['image'], '--format', '{{.Id}}']).strip()
    run = ['docker', 'run', '--rm', '--network', 'none', '--user', service['user'], '--cap-drop', 'ALL',
           '--security-opt', 'no-new-privileges:true', '--memory', '256m', '--pids-limit', '128', '--storage-opt', 'size=128m']
    for mount in service['volumes']:
        # Validation never writes to application content.
        run += ['--volume', mount if mount.endswith(':ro') else mount + ':ro']
    for env in service.get('env_file', []):
        run += ['--env-file', env]
    info = json.loads(command([*run, '--entrypoint', 'php', service['image'], '-r',
        'echo json_encode(array("version"=>PHP_VERSION,"extensions"=>get_loaded_extensions()));']))
    if not info['version'].startswith(branch + '.') or not REQUIRED_EXTENSIONS.issubset(info['extensions']):
        raise RuntimeError('Candidate PHP version/extensions differ from the selected branch')
    command([*run, '--entrypoint', 'php-fpm', service['image'], '--test', '--fpm-config', '/etc/hosting/php-fpm.conf'])
    return image_id


def perform(host, row, job, step):
    root = SITES / row['name']
    for path in (root, root / 'conf'):
        trusted(path, directory=True)
    for name in ('hosting.yaml', 'compose.yml', 'conf/Containerfile', 'conf/php.ini', 'conf/php-fpm.conf', 'conf/pool.conf', '.env'):
        trusted(root / name)
    SAVED.mkdir(mode=0o700, exist_ok=True)
    trusted(SAVED, directory=True)
    saved = snapshot_path(job['id'])
    intent = None
    if saved.exists():
        trusted(saved)
        intent = json.loads(saved.read_text())
        if intent.get('rolled_back'):
            atomic(saved.with_name(saved.stem + '-attempt-' + str(time.time_ns()) + '.json'), json.dumps(intent, indent=2))
            intent = None
    if intent is None:
        old_meta = yaml.safe_load((root / 'hosting.yaml').read_text())
        old_compose = yaml.safe_load((root / 'compose.yml').read_text())
        if old_meta.get('operation_id') != row['id'] or old_meta.get('runtime') != 'php':
            raise ValueError('Unexpected site identity or runtime')
        target = json.loads(job['payload'])
        if job['kind'] == 'rollback':
            prior = snapshot_path(target['previous'])
            trusted(prior)
            previous = json.loads(prior.read_text())
            if previous['site_id'] != row['id']:
                raise ValueError('Rollback belongs to another site')
            new_compose = copy.deepcopy(old_compose)
            new_compose['services']['php'] = copy.deepcopy(previous['old_compose']['services']['php'])
            # DB attachment can postdate the saved PHP runtime; keep current credential mounts.
            new_compose['services']['php']['env_file'] = old_compose['services']['php']['env_file']
            # Web compatibility settings can postdate the saved PHP image.
            for key in ('networks','extra_hosts'):
                if key in old_compose['services']['php']: new_compose['services']['php'][key]=copy.deepcopy(old_compose['services']['php'][key])
            mounts=new_compose['services']['php']['volumes']
            for mount in old_compose['services']['php']['volumes']:
                if ':/etc/ssl/certs/ca-certificates.crt:' in mount and mount not in mounts: mounts.append(mount)
            new_meta = dict(old_meta, **{key: previous['old_meta'][key] for key in ('php_version', 'php_runtime', 'php_image')})
        else:
            step('building replacement runtime')
            runtime = ensure(target['branch'])
            step('building replacement site image')
            tag = 'hosting-php-site:' + job['id']
            command(['docker', 'build', '--build-arg', 'PHP_BASE=' + runtime['image'], '--tag', tag,
                     '--file', root / 'conf/Containerfile', root / 'conf'], timeout=900)
            new_compose = copy.deepcopy(old_compose)
            service = new_compose['services']['php']
            service['image'] = tag
            old_branch = old_meta['php_version']
            service['volumes'] = [mount.replace('/etc/php/' + old_branch + '/', '/etc/php/' + target['branch'] + '/') for mount in service['volumes']]
            new_meta = dict(old_meta, php_version=target['branch'], php_runtime={k: v for k, v in runtime.items() if k != 'packages'},
                php_image={'name': tag, 'id': command(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']).strip()})
        step('validating replacement configuration')
        actual = validate_service(new_compose['services']['php'], new_meta['php_version'])
        if actual != new_meta['php_image']['id']:
            raise RuntimeError('Pinned replacement image changed')
        intent = {'site_id': row['id'], 'old_meta': old_meta, 'old_compose': old_compose,
                  'new_meta': new_meta, 'new_compose': new_compose}
        # Durable rollback intent precedes any live replacement.
        atomic(saved, json.dumps(intent, indent=2))
    if intent['site_id'] != row['id']:
        raise ValueError('Saved switch belongs to another site')
    for side in ('old', 'new'):
        service = intent[side + '_compose']['services']['php']
        expected = intent[side + '_meta']['php_image']['id']
        if command(['docker', 'image', 'inspect', service['image'], '--format', '{{.Id}}']).strip() != expected:
            raise RuntimeError('Retained PHP image changed; restore its pinned artifact')

    def deploy(compose):
        atomic(root / 'compose.yml', yaml.safe_dump(compose))
        command(['docker', 'compose', '-f', root / 'compose.yml', 'up', '-d', '--no-deps', '--force-recreate',
                 '--wait', '--wait-timeout', '45', 'php'], timeout=90)
        service = compose['services']['php']
        if service.get('pids_limit') == -1:
            command(['docker', 'update', '--pids-limit', '-1', service['container_name']])
        live = host.inspect(service['container_name'])
        layer = Path(live['GraphDriver']['Data']['UpperDir'])
        if not layer.is_relative_to('/srv/docker/overlay2'):
            raise RuntimeError('Unexpected PHP layer location')
        project, _ = project_id(layer)
        size = service['storage_opt']['size']
        if size == '0':
            if quota_record(project)['hard_bytes'] != 0:
                raise RuntimeError('Unexpected PHP layer cap')
        else:
            verify_quota(layer, project, int(size[:-1]), inherit=False)
        if live['Config']['User'] != service['user'] or live['HostConfig'].get('PortBindings'):
            raise RuntimeError('Unexpected PHP identity or published port')
        if live['HostConfig']['Memory'] != int(service.get('mem_limit', '0m')[:-1]) * 1024**2:
            raise RuntimeError('PHP memory cap changed')
        if live['HostConfig']['NanoCpus'] != int(round(service.get('cpus', 0) * 1e9)):
            raise RuntimeError('PHP CPU cap changed')
        # nginx resolves the replacement FPM address at reload, including changed Docker IPs.
        command(['docker', 'exec', 'hosting-site-' + row['name'], 'nginx', '-s', 'reload'])

    step('deploying replacement')
    try:
        validate_service(intent['new_compose']['services']['php'], intent['new_meta']['php_version'])
        deploy(intent['new_compose'])
        step('verifying replacement HTTPS')
        host.verify_domains([intent['new_meta']['domain'], *intent['new_meta'].get('aliases', [])])
        atomic(root / 'hosting.yaml', yaml.safe_dump(intent['new_meta']))
    except Exception as exc:
        step('restoring previous runtime')
        try:
            deploy(intent['old_compose'])
            atomic(root / 'hosting.yaml', yaml.safe_dump(intent['old_meta']))
            host.verify_domains([intent['old_meta']['domain'], *intent['old_meta'].get('aliases', [])])
        except Exception as rollback:
            raise RuntimeError('PHP switch and runtime recovery failed; retry the saved operation. ' + str(rollback)) from None
        intent['rolled_back'] = True
        atomic(saved, json.dumps(intent, indent=2))
        raise RuntimeError('PHP switch failed; previous runtime restored. ' + str(exc)) from None
    return intent['new_meta']['php_version']
