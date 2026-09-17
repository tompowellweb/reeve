"""Private, digest-pinned DB services. Retained data shares the site's XFS project."""
import json
import os
import secrets
from pathlib import Path

import yaml

from .host import OPS, SITES, atomic, command, container_limits, trusted, verify_quota
from .database_jobs import validate_database
from .database_versions import architecture, resolve, select, series

STATE = OPS / 'panel/worker/databases'


def paths(row):
    return SITES / row['name'] / 'database', STATE / (row['id'] + '.json')


def state(row):
    _, path = paths(row)
    if not path.exists(): return None
    trusted(path)
    return json.loads(path.read_text())


def public(row, host):
    info = state(row)
    if not info: return None
    live = host.inspect('hosting-db-' + row['name'])
    return {**{k: info[k] for k in ('engine', 'version', 'series', 'image', 'image_id', 'uid', 'gid', 'stage', 'auth_plugin') if k in info},
            'host': 'db', 'port': 5432 if info['engine'] == 'postgres' else 3306, 'name': 'site', 'user': 'site',
            'limits': {k: info.get('spec', {}).get(k) for k in ('memory_mb','cpus','layer_mb','pids_limit')}, 'usage': info.get('spec', {}).get('usage', 'standard'), 'usages': list(USAGES),
            'data_path': str(paths(row)[0] / 'data'), 'quota': 'Shared site-data XFS quota',
            'container': live['State']['Status'] if live else 'absent',
            'health': live['State'].get('Health', {}).get('Status', 'unknown') if live else 'absent'}


def credentials(row):
    info = state(row)
    if not info or info.get('stage') != 'ready': raise ValueError('Database setup is not complete')
    return {'engine': info['engine'], 'host': 'db', 'port': 5432 if info['engine'] == 'postgres' else 3306,
            'database': 'site', 'user': 'site', 'password': info['app_password']}


def legacy_php(branch):
    """PHP before 7.2 (7.0, 7.1) ships a MySQL client without caching_sha2_password support."""
    try: return branch is not None and tuple(int(x) for x in str(branch).split('.')[:2]) < (7, 2)
    except ValueError: return False


def mysql_auth(engine, series, php_branch):
    """The authentication plugin the application user needs, or None for the image default.

    MySQL 8.0 still offers mysql_native_password for old clients; 8.4 disables it and 9 removes it,
    so a legacy-PHP site must pin MySQL 8.0 (or choose MariaDB, whose default works everywhere)."""
    if engine != 'mysql' or not legacy_php(php_branch): return None
    if str(series) != '8.0':
        raise ValueError('PHP ' + str(php_branch) + ' cannot authenticate to MySQL ' + str(series) + '; choose the MySQL 8.0 series or MariaDB for this site')
    return 'mysql_native_password'


USAGES = {  # a database's usage: what it is sized for, rendered as server flags; the profile picks the default
    'light': {'label': 'Light', 'pool_mb': 64, 'connections': 40, 'performance_schema': False, 'pg_shared_mb': 32},
    'standard': {'label': 'Standard', 'pool_mb': 256, 'connections': 150, 'performance_schema': False, 'pg_shared_mb': 128},
    'high': {'label': 'High', 'pool_mb': 1024, 'connections': 300, 'performance_schema': True, 'pg_shared_mb': 512},
}


def server_options(engine, plugin, usage='standard'):
    """Container command line: explicit socket/pid paths; old clients also need the server's
    advertised default plugin to be native, since they reject an unknown name in the handshake;
    the usage's buffer pool, connections and (MySQL) performance schema; the binary log off
    everywhere, because recovery is the panel's dumps, not log replay."""
    size = USAGES[usage or 'standard']
    if engine == 'postgres': return ['postgres', '-c', f"shared_buffers={size['pg_shared_mb']}MB", '-c', f"max_connections={size['connections']}"]
    options = ['--socket=/tmp/mysql.sock', '--pid-file=/tmp/mysqld.pid', f"--innodb-buffer-pool-size={size['pool_mb']}M", f"--max-connections={size['connections']}", '--skip-log-bin']
    if engine == 'mysql': options.append('--performance-schema=' + ('ON' if size['performance_schema'] else 'OFF'))
    if plugin: options.append('--default-authentication-plugin=' + plugin)
    return options


def secured(path, text, gid):
    atomic(path, text, 0o440)
    os.chown(path, 0, gid)


def provision(host, row, spec, step):
    spec = validate_database(spec)
    root, saved = paths(row)
    site = root.parent
    for path in (site, site / 'conf'):
        trusted(path, directory=True)
    STATE.mkdir(mode=0o700, exist_ok=True)
    trusted(STATE, directory=True)
    info = state(row)
    if info and info['spec'] != spec: raise ValueError('Database inputs differ from the saved setup; data retained')
    if info is None:
        if root.exists() or root.is_symlink(): raise ValueError('Unmanaged database path; nothing was changed')
        step('resolving database image')
        version = select(spec)
        info = {'schema': 1, 'operation_id': row['id'], 'spec': spec, 'engine': spec['engine'],
                'version': version, 'series': series(spec['engine'], version), 'image': resolve(spec['engine'], version),
                'admin_password': secrets.token_hex(24), 'app_password': secrets.token_hex(24), 'stage': 'resolved'}
        # Includes private passwords; never returned through routine listing or diagnostics.
        atomic(saved, json.dumps(info, indent=2))
    secret_values = (info['admin_password'], info['app_password'])
    try:
        _provision(host, row, root, saved, info, step)
    except Exception as exc:
        message = str(exc)
        for value in secret_values: message = message.replace(value, '[redacted]')
        raise RuntimeError(message) from None


def _provision(host, row, root, saved, info, step):
    spec, engine = info['spec'], info['engine']
    site = root.parent
    step('pulling pinned database image')
    if 'image_id' not in info:
        command(['docker', 'pull', info['image']], timeout=900)
        image = json.loads(command(['docker', 'image', 'inspect', info['image']]))[0]
        if image['Os'] != 'linux' or image['Architecture'] != architecture(): raise ValueError('Database image architecture mismatch')
        account = 'postgres' if engine == 'postgres' else 'mysql'
        ids = command(['docker', 'run', '--rm', '--network', 'none', '--user', '65534:65534', '--cap-drop', 'ALL',
                       '--entrypoint', 'sh', info['image'], '-c', f'id -u {account}; id -g {account}']).split()
        if len(ids) != 2 or any(not x.isdigit() or int(x) == 0 for x in ids): raise ValueError('Unsupported image database identity')
        uid, gid = map(int, ids)
        major = int(info['version'].split('.')[0])
        mount = ('/var/lib/postgresql' if major >= 18 else '/var/lib/postgresql/data') if engine == 'postgres' else '/var/lib/mysql'
        volumes = set(image['Config'].get('Volumes') or {})
        if volumes - {mount}: raise ValueError('Image declares unexpected retained volumes; requires a compatible template')
        info.update(image_id=image['Id'], uid=uid, gid=gid, mount=mount)
        atomic(saved, json.dumps(info, indent=2))
    elif command(['docker', 'image', 'inspect', info['image'], '--format', '{{.Id}}']).strip() != info['image_id']:
        raise ValueError('Pinned database image changed or is missing; restore the recorded artifact')

    step('database data ownership and quota')
    if not root.exists(): root.mkdir(mode=0o711)
    trusted(root, directory=True)
    marker = root / '.hosting-operation'
    if not marker.exists():
        if any(root.iterdir()): raise ValueError('Unmanaged database folder contents; retained')
        atomic(marker, row['id'])
    trusted(marker)
    if marker.read_text() != row['id']: raise ValueError('Database path belongs to another site')
    data = root / 'data'
    if data.is_symlink(): raise ValueError('Database data path is a symlink')
    if not data.exists():
        data.mkdir(mode=0o700); os.chown(data, info['uid'], info['gid'])
    data_stat = data.lstat()
    if not data.is_dir() or (data_stat.st_uid, data_stat.st_gid) != (info['uid'], info['gid']):
        raise ValueError('Database data ownership differs from its image identity; no recursive chown performed')
    site_settings = json.loads(row['payload'])
    verify_quota(data, row['project'], site_settings['data_mb'])
    conf = root / 'conf'; conf.mkdir(mode=0o700, exist_ok=True); trusted(conf, directory=True)
    secured(conf / 'admin-password', info['admin_password'], info['gid'])
    secured(conf / 'app-password', info['app_password'], info['gid'])
    env = {}; plugin = None
    if engine == 'postgres':
        env = {'POSTGRES_PASSWORD_FILE': '/run/hosting/admin-password', 'POSTGRES_USER': 'postgres',
               'PGDATA': info['mount'] + '/pgdata'}
        health = 'PGPASSWORD="$$(cat /run/hosting/admin-password)" psql -h 127.0.0.1 -U postgres -d postgres -tAc "SELECT 1"'
        bootstrap = '''#!/bin/sh
set -eu
export PGPASSWORD="$(cat /run/hosting/admin-password)"
if [ "$(psql -h 127.0.0.1 -U postgres -d postgres -tAc "SELECT count(*) FROM pg_roles WHERE rolname='site'")" = 0 ]; then
 psql -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 -v app_password="$(cat /run/hosting/app-password)" -f /run/hosting/create-user.sql
fi
if [ "$(psql -h 127.0.0.1 -U postgres -d postgres -tAc "SELECT count(*) FROM pg_database WHERE datname='site'")" = 0 ]; then
 psql -h 127.0.0.1 -U postgres -d postgres -v ON_ERROR_STOP=1 -c 'CREATE DATABASE site OWNER site'
fi
export PGPASSWORD="$(cat /run/hosting/app-password)"
psql -h 127.0.0.1 -U site -d site -v ON_ERROR_STOP=1 -tAc 'SELECT 1'
'''
        secured(conf / 'create-user.sql', "CREATE ROLE site LOGIN PASSWORD :'app_password';\n", info['gid'])
    else:
        prefix = 'MARIADB' if engine == 'mariadb' else 'MYSQL'
        env = {prefix + '_ROOT_PASSWORD_FILE': '/run/hosting/admin-password'}
        client = 'mariadb' if engine == 'mariadb' else 'mysql'
        # Old MariaDB images have mysql rather than mariadb; command selection is fixed, never user text.
        select_client = 'client=$(command -v mariadb || command -v mysql); ' if engine == 'mariadb' else 'client=mysql; '
        health = select_client.replace('$', '$$') + '$$client --defaults-extra-file=/run/hosting/admin.cnf -h 127.0.0.1 -Nse "SELECT 1"'
        secured(conf / 'admin.cnf', '[client]\nuser=root\npassword=' + info['admin_password'] + '\n', info['gid'])
        secured(conf / 'app.cnf', '[client]\nuser=site\npassword=' + info['app_password'] + '\ndatabase=site\n', info['gid'])
        plugin = mysql_auth(engine, info['series'], site_settings.get('php_version'))
        if info.get('auth_plugin') != plugin:
            info['auth_plugin'] = plugin; atomic(saved, json.dumps(info, indent=2))
        with_plugin = (" IDENTIFIED WITH " + plugin) if plugin else ' IDENTIFIED'
        secured(conf / 'create-user.sql', "CREATE USER 'site'@'%'" + with_plugin + " BY '" + info['app_password'] + "';\n", info['gid'])
        # An existing user created with the image default converts on the next setup run.
        convert = ('"$client" --defaults-extra-file=/run/hosting/admin.cnf -h 127.0.0.1 -e "ALTER USER \'site\'@\'%\' IDENTIFIED WITH ' + plugin + " BY '" + info['app_password'] + '\'"\n') if plugin else ''
        bootstrap = '''#!/bin/sh
set -eu
''' + select_client + '''
if [ "$("$client" --defaults-extra-file=/run/hosting/admin.cnf -h 127.0.0.1 -Nse "SELECT count(*) FROM mysql.user WHERE User='site' AND Host='%'")" = 0 ]; then
 "$client" --defaults-extra-file=/run/hosting/admin.cnf -h 127.0.0.1 < /run/hosting/create-user.sql
fi
''' + convert + '''"$client" --defaults-extra-file=/run/hosting/admin.cnf -h 127.0.0.1 -e "CREATE DATABASE IF NOT EXISTS site; GRANT ALL ON site.* TO 'site'@'%';"
"$client" --defaults-extra-file=/run/hosting/app.cnf -h 127.0.0.1 -Nse 'SELECT 1'
'''
    secured(conf / 'bootstrap.sh', bootstrap, info['gid'])
    backend = 'hosting-backend-' + row['name']
    if not command(['docker', 'network', 'ls', '--filter', 'name=^' + backend + '$', '-q']).strip():
        command(['docker', 'network', 'create', '--internal', '--label', 'hosting.operation=' + row['id'], backend])
    network = json.loads(command(['docker', 'network', 'inspect', backend]))[0]
    if network.get('Labels', {}).get('hosting.operation') != row['id'] or not network['Internal']: raise ValueError('Unmanaged database network collision')
    name = 'hosting-db-' + row['name']
    existing = host.inspect(name)
    if existing and existing['Config'].get('Labels', {}).get('hosting.operation') != row['id']: raise ValueError('Unmanaged database container collision')
    # File-level mounts expose only the needed secrets despite the root-only host directory.
    mounts = [f'{data}:{info["mount"]}'] + [f'{p}:/run/hosting/{p.name}:ro' for p in sorted(conf.iterdir())]
    service = {'image': info['image'], 'container_name': name, 'user': f"{info['uid']}:{info['gid']}",
               'labels': {'hosting.operation': row['id']}, 'restart': 'unless-stopped', 'cap_drop': ['ALL'],
               'security_opt': ['no-new-privileges:true'], 'environment': env, 'volumes': mounts,
               'networks': {'backend': {'aliases': ['db']}}, **container_limits(spec),
               'logging': {'driver': 'local', 'options': {'max-size': '10m', 'max-file': '3'}},
               'healthcheck': {'test': ['CMD-SHELL', health], 'interval': '3s', 'timeout': '3s', 'retries': 40, 'start_period': '60s'}}
    # Explicit socket path avoids image-specific /run permissions without extra retained volumes.
    service['command'] = server_options(engine, plugin, spec.get('usage'))
    compose = {'name': 'hosting-db-' + row['name'], 'services': {'database': service},
               'networks': {'backend': {'external': True, 'name': backend}}}
    atomic(root / 'compose.yml', yaml.safe_dump(compose))
    info['stage'] = 'initializing'; atomic(saved, json.dumps(info, indent=2))
    step('initializing database')
    command(['docker', 'compose', '-f', root / 'compose.yml', 'up', '-d', '--wait', '--wait-timeout', '240'], timeout=300)
    if spec.get('pids_limit') is None: command(['docker', 'update', '--pids-limit', '-1', name])
    step('creating and verifying application database')
    command(['docker', 'exec', name, 'sh', '/run/hosting/bootstrap.sh'])
    live = host.inspect(name)
    from .host import project_id, quota_record
    if live['Image'] != info['image_id'] or live['Config']['User'] != service['user'] or live['HostConfig'].get('PortBindings'):
        raise ValueError('Database image, identity or isolation differs from the selected template')
    if set(live['NetworkSettings']['Networks']) != {backend}: raise ValueError('Database joins unexpected networks')
    if any(m['Type'] != 'bind' for m in live['Mounts']): raise ValueError('Database has an untracked retained volume')
    if live['HostConfig']['Memory'] != (spec.get('memory_mb') or 0) * 1024**2: raise ValueError('DB memory cap differs from requested value')
    if live['HostConfig']['NanoCpus'] != int(round((spec.get('cpus') or 0) * 1e9)): raise ValueError('DB CPU cap differs from requested value')
    if spec.get('pids_limit') and live['HostConfig']['PidsLimit'] != spec['pids_limit']: raise ValueError('DB process cap differs from requested value')
    layer = Path(live['GraphDriver']['Data']['UpperDir']); project, _ = project_id(layer)
    if spec.get('layer_mb'): verify_quota(layer, project, spec['layer_mb'], inherit=False)
    elif quota_record(project)['hard_bytes']: raise ValueError('Unexpected DB writable-layer quota')
    if spec.get('pids_limit') is None and command(['docker', 'exec', name, 'cat', '/sys/fs/cgroup/pids.max']).strip() != 'max':
        raise ValueError('Unexpected DB process cap')
    verify_quota(data, row['project'], site_settings['data_mb'])
    step('connecting site to database')
    app_env = {'DATABASE_ENGINE': engine, 'DATABASE_HOST': 'db', 'DATABASE_PORT': '5432' if engine == 'postgres' else '3306',
               'DATABASE_NAME': 'site', 'DATABASE_USER': 'site', 'DATABASE_PASSWORD': info['app_password']}
    atomic(site / '.database-app.env', ''.join(k + '=' + v + '\n' for k, v in app_env.items()))
    if site_settings.get('runtime') == 'php':
        main = yaml.safe_load((site / 'compose.yml').read_text())
        php = main['services']['php']; env_file = str(site / '.database-app.env')
        if env_file not in php['env_file']:
            php['env_file'].append(env_file)
            atomic(site / 'compose.yml', yaml.safe_dump(main))
        # Also on retry: the worker may have died after publishing Compose but before recreation.
        command(['docker', 'compose', '-f', site / 'compose.yml', 'up', '-d', '--no-deps', '--wait', '--wait-timeout', '60', 'php'])
        if site_settings.get('pids_limit') is None: command(['docker', 'update', '--pids-limit', '-1', 'hosting-php-' + row['name']])
        command(['docker', 'exec', 'hosting-site-' + row['name'], 'nginx', '-s', 'reload'])
    info['stage'] = 'ready'; atomic(saved, json.dumps(info, indent=2))
    # Portable non-secret inventory belongs beside the site; full backups must include private files too.
    portable = {k: v for k, v in info.items() if k not in ('admin_password', 'app_password')}
    portable.update(data_path=str(data), credential_reference=str(saved), quota_project=row['project'])
    atomic(root / 'hosting.yaml', yaml.safe_dump(portable))


USAGE_SAVED = OPS / 'panel/worker/database-usage'


class UsageRecoveryFailed(RuntimeError):
    pass


def validate_usage(data):
    if not isinstance(data, dict) or set(data) != {'usage'} or data['usage'] not in USAGES: raise ValueError('Database usage must be light, standard or high')
    return {'usage': data['usage']}


def restart_database(row, root):
    command(['docker', 'compose', '-f', root / 'compose.yml', 'up', '-d', '--force-recreate', '--wait', '--wait-timeout', '240'], timeout=300)
    info = state(row)
    if info.get('spec', {}).get('pids_limit') is None: command(['docker', 'update', '--pids-limit', '-1', 'hosting-db-' + row['name']])


def apply_usage(host, row, data, ident=None):
    """The database's usage changed: its compose command is rewritten and the one container recreated
    (seconds of downtime for that site's database); a failure to come back healthy restores the previous."""
    settings = validate_usage(data)
    root, saved = paths(row)
    info = state(row)
    if not info or info.get('stage') != 'ready': raise ValueError('Database setup is not complete')
    trusted(root / 'compose.yml')
    compose = yaml.safe_load((root / 'compose.yml').read_text())
    prior = {'site_id': row['id'], 'compose': (root / 'compose.yml').read_text(), 'spec': info['spec']}
    if ident:
        from .core import request_id
        request_id(ident); USAGE_SAVED.mkdir(mode=0o700, exist_ok=True)
        atomic(USAGE_SAVED / (ident + '.json'), json.dumps(prior))
    compose['services']['database']['command'] = server_options(info['engine'], info.get('auth_plugin'), settings['usage'])
    atomic(root / 'compose.yml', yaml.safe_dump(compose))
    info['spec'] = {**info['spec'], 'usage': settings['usage']}; atomic(saved, json.dumps(info, indent=2))
    try:
        restart_database(row, root)
    except Exception:
        atomic(root / 'compose.yml', prior['compose']); info['spec'] = prior['spec']; atomic(saved, json.dumps(info, indent=2))
        try: restart_database(row, root)
        except Exception as recovery:
            raise UsageRecoveryFailed('Database usage rollback needs review: ' + str(recovery)) from None
        raise
    return settings


def rollback_usage(host, row, ident):
    path = USAGE_SAVED / (ident + '.json')
    if not path.exists(): return
    trusted(path); prior = json.loads(path.read_text())
    if prior['site_id'] != row['id']: raise ValueError('Database usage recovery belongs to another site')
    root, saved = paths(row)
    info = state(row)
    atomic(root / 'compose.yml', prior['compose'])
    if info: info['spec'] = prior['spec']; atomic(saved, json.dumps(info, indent=2))
    restart_database(row, root)


def perform_usage(host, row, job, step):
    from .content_site import OUTPUT, ContentFailed
    step('changing the database usage and restarting the database')
    try:
        settings = apply_usage(host, row, json.loads(job['payload']), job['id'])
    except UsageRecoveryFailed:
        raise
    except Exception as exc:
        OUTPUT.mkdir(mode=0o700, exist_ok=True); atomic(OUTPUT / (job['id'] + '.txt'), str(exc)[:4000])
        raise ContentFailed('Database usage was not changed; the previous setting stays in force. Inspect output.') from None
    OUTPUT.mkdir(mode=0o700, exist_ok=True)
    size = USAGES[settings['usage']]
    atomic(OUTPUT / (job['id'] + '.txt'), f"Database usage set to {settings['usage']}: buffer pool {size['pool_mb']} MiB, {size['connections']} connections. The database was restarted and answered.\n")


def recover_usage(ledger, host):
    for job in ledger.content_jobs():
        if job['kind'] == 'database-usage' and job['state'] == 'recovery-needed': rollback_usage(host, ledger.get(job['site_id']), job['id'])
