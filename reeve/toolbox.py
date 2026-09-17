"""Reusable Dockerfile toolboxes with a private, site-UID SSH daemon."""
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import time
from pathlib import Path

import yaml

from .content_site import ContentFailed, composer, mount
from .host import OPS, SITES, atomic, command, trusted, verify_quota

ROOT = OPS / 'panel/worker/toolboxes'
TEMPLATES = Path(__file__).resolve().parent.parent / 'templates/toolbox'
DEBIAN = 'debian:trixie-slim@sha256:d7e12182ce18b85b93007c1dedf31f2d29e01ccf3182cc4017c709b6259bc132'


def recipe_name(name):
    if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9-]{0,47}', name): raise ValueError('Use a lowercase toolbox recipe name, up to 48 characters')
    return name


def public_key(value):
    if not isinstance(value, str) or len(value) > 2048 or '\n' in value.strip(): raise ValueError('Supply one SSH public key')
    parts = value.strip().split()
    if len(parts) < 2 or parts[0] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521'):
        raise ValueError('Supply a public key, without authorized_keys options or private key material')
    try:
        raw = base64.b64decode(parts[1], validate=True)
        n = int.from_bytes(raw[:4], 'big')
        if len(raw) < 32 or raw[4:4+n].decode() != parts[0]: raise ValueError()
    except Exception: raise ValueError('Malformed SSH public key') from None
    return parts[0] + ' ' + parts[1]


def validate(kind, data):
    from .content_jobs import relative
    if kind == 'toolbox-stop':
        if data != {}: raise ValueError('Stop takes no toolbox settings')
        return {}
    if kind != 'toolbox-start' or not isinstance(data, dict) or set(data) != {'recipe','path','public_key','internet','jump'}:
        raise ValueError('Invalid toolbox settings')
    recipe_name(data['recipe']); relative(data['path'], root=True); public_key(data['public_key'])
    if type(data['internet']) is not bool: raise ValueError('Invalid toolbox network setting')
    if not isinstance(data['jump'], str) or not re.fullmatch(r'[a-z_][a-z0-9_-]*@[a-zA-Z0-9.-]+', data['jump']): raise ValueError('Use an SSH jump target such as admin@server.example.net')
    return dict(data, public_key=public_key(data['public_key']))


def initialize():
    ROOT.mkdir(mode=0o700, exist_ok=True); trusted(ROOT, directory=True)
    for name in ('recipes', 'images', 'builds', 'intents'):
        path = ROOT / name; path.mkdir(mode=0o700, exist_ok=True); trusted(path, directory=True)
    if not (ROOT / 'recipes/php-workbench.json').exists():
        save_recipe('php-workbench', (TEMPLATES / 'Dockerfile').read_text())


def save_recipe(name, dockerfile):
    recipe_name(name)
    if not isinstance(dockerfile, str) or not 1 <= len(dockerfile.encode()) <= 12000 or '\x00' in dockerfile:
        raise ValueError('Dockerfile must be between 1 byte and 12 KiB')
    path = ROOT / 'recipes'; path.mkdir(mode=0o700, parents=True, exist_ok=True); trusted(path, directory=True)
    info = {'name': name, 'dockerfile': dockerfile, 'sha256': hashlib.sha256(dockerfile.encode()).hexdigest()}
    atomic(path / (name + '.json'), json.dumps(info, indent=2))
    return info


def recipes():
    initialize()
    return [json.loads(p.read_text()) for p in sorted((ROOT / 'recipes').glob('*.json'))]


def settings():
    path = ROOT / 'settings.json'
    if not path.exists(): return {'public_key': '', 'jump': 'admin@server'}
    trusted(path); return json.loads(path.read_text())


def session(ledger, site_id):
    with ledger.db() as db:
        found = db.execute('SELECT * FROM toolbox_sessions WHERE site_id=?', (site_id,)).fetchone()
    return dict(found) if found else None


def update_session(ledger, row, ident, state, details):
    with ledger.db() as db:
        db.execute('INSERT OR REPLACE INTO toolbox_sessions VALUES (?,?,?,?,?,?)',
                   (row['id'], ident, state, json.dumps(details), time.time(), row['name']))


def status(ledger, host, row):
    saved = session(ledger, row['id'])
    if not saved: return None
    result = dict(saved, details=json.loads(saved['details']))
    live = host.inspect('hosting-toolbox-' + row['name'])
    result['container'] = live['State']['Status'] if live else 'absent'
    return result


def prepare(row, ident, kind, data):
    if kind == 'toolbox-start':
        initialize()
        recipe = next((r for r in recipes() if r['name'] == data['recipe']),None)
        if not recipe: raise ValueError('Save this recipe before starting it')
        atomic(ROOT/'intents'/(ident+'.json'),json.dumps(recipe))
        root = SITES / row['name']; trusted(root, directory=True)
        quota = verify_quota(root, row['project'], json.loads(row['payload'])['data_mb'])
        if quota['hard_bytes'] - quota['used_bytes'] < 1048576: raise ValueError('Free site storage before starting a toolbox')
        atomic(ROOT / 'settings.json', json.dumps({k: data[k] for k in ('public_key','jump')}))


def image(row, recipe, step):
    meta = yaml.safe_load((SITES / row['name'] / 'hosting.yaml').read_text())
    base = meta['php_image']['id'] if meta.get('runtime') == 'php' else DEBIAN
    digest = hashlib.sha256((recipe['dockerfile'] + '\n' + base + '\n' + str(row['uid']) + '\nwrapper-v1').encode()).hexdigest()
    saved = ROOT / 'images' / (digest + '.json')
    if saved.exists():
        trusted(saved); result = json.loads(saved.read_text())
        if command(['docker','image','inspect',result['image_id'],'--format','{{.Id}}']).strip() != result['image_id']: raise ValueError('Retained toolbox image is missing')
        return result
    context = ROOT / 'builds' / digest; context.mkdir(mode=0o700, exist_ok=True); trusted(context, directory=True)
    atomic(context / 'Dockerfile', recipe['dockerfile'])
    atomic(context / '.dockerignore', '*\n!Dockerfile\n')
    tag = 'hosting-toolbox-recipe:' + digest
    step('building toolbox recipe in Docker')
    base_reference=base
    if base.startswith('sha256:'):
        base_reference='hosting-toolbox-base:'+base.removeprefix('sha256:')
        command(['docker','tag',base,base_reference])
    command(['docker','build','--build-arg','SITE_BASE='+base_reference,'--tag',tag,'--file',context/'Dockerfile',context],timeout=900)
    info = json.loads(command(['docker','image','inspect',tag]))[0]
    if info['Config'].get('Volumes'): raise ValueError('Toolbox images cannot declare VOLUME; the panel supplies the quota-backed mounts')
    # This image layer establishes the chosen numeric identity, without site data or keys.
    wrapper = f'''FROM {tag}
USER root
RUN test -x /usr/sbin/sshd && test -x /bin/bash \\
 && mkdir -p /run/sshd /run/toolbox \\
 && chmod 755 /run/sshd /run/toolbox \\
 && groupadd -g {row['uid']} hosting-toolbox \\
 && useradd -u {row['uid']} -g {row['uid']} -d /toolhome -s /bin/bash site \\
 && passwd -d site
USER {row['uid']}:{row['uid']}
WORKDIR /site
'''
    atomic(context / 'Dockerfile', wrapper)
    final = 'hosting-toolbox:' + digest
    step('preparing unprivileged SSH identity')
    command(['docker','build','--tag',final,'--file',context/'Dockerfile',context],timeout=300)
    image_id = command(['docker','image','inspect',final,'--format','{{.Id}}']).strip()
    php = None
    try:
        php = command(['docker','run','--rm','--network','none','--read-only','--user',str(row['uid']), '--cap-drop','ALL',
                       '--entrypoint','php',image_id,'-n','-r','echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;']).strip()
        if not re.fullmatch(r'\d+\.\d+', php): php = None
    except RuntimeError: pass
    result = {'recipe':recipe['name'],'recipe_sha256':recipe['sha256'],'image_id':image_id,'php_branch':php,'base':base}
    atomic(saved,json.dumps(result,indent=2))
    return result


def stop(ledger, host, row):
    name = 'hosting-toolbox-' + row['name']; network = 'hosting-toolbox-net-' + row['name']
    live = host.inspect(name)
    if live:
        if live['Config'].get('Labels', {}).get('hosting.toolbox.site') != row['id']: raise ValueError('Unmanaged toolbox container collision')
        command(['docker','rm','-f',name])
    if command(['docker','network','ls','-q','--filter','name=^'+network+'$']).strip():
        info = json.loads(command(['docker','network','inspect',network]))[0]
        if info.get('Labels',{}).get('hosting.toolbox.site') != row['id']: raise ValueError('Unmanaged toolbox network collision')
        command(['docker','network','rm',network])
    previous = session(ledger,row['id'])
    if previous: update_session(ledger,row,previous['start_id'],'stopped',json.loads(previous['details']))
    # Retain home/config/key identity; remove only the disposable temporary directory.
    root = SITES / row['name'] / 'toolbox'
    if root.exists():
        trusted(root,directory=True)
        if (root/'tmp').exists(): shutil.rmtree(root/'tmp')


def start(ledger, host, row, job, step):
    data = validate('toolbox-start',json.loads(job['payload']))
    snapshot = ROOT / 'intents' / (job['id']+'.json'); trusted(snapshot)
    recipe = json.loads(snapshot.read_text())
    built = image(row,recipe,step)
    root = SITES / row['name'] / 'toolbox'; root.mkdir(mode=0o700,exist_ok=True); trusted(root,directory=True)
    for name in ('home','tmp','conf'):
        path = root/name
        if path.is_symlink(): raise ValueError('Unsafe toolbox directory')
        if not path.exists():
            path.mkdir(mode=0o700)
            if name != 'conf': os.chown(path,row['uid'],row['uid'])
        if name == 'conf': trusted(path,directory=True)
        elif path.stat().st_uid != row['uid']: raise ValueError('Unexpected toolbox data owner')
    verify_quota(root,row['project'],json.loads(row['payload'])['data_mb'])
    conf = root/'conf'
    key = conf/'host_key'
    if not key.exists():
        command(['ssh-keygen','-q','-t','ed25519','-N','','-f',key])
        os.chown(key,row['uid'],row['uid']); os.chmod(key,0o600)
    # Root-only parent and read-only mounts protect these runtime files.
    atomic(conf/'authorized_keys',data['public_key']+'\n',0o444)
    atomic(conf/'sshd_config','''Port 2222
ListenAddress 0.0.0.0
HostKey /run/toolbox/host_key
PidFile /tmp/sshd.pid
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers site
AuthorizedKeysFile /run/toolbox/authorized_keys
StrictModes yes
DisableForwarding yes
PermitTTY yes
PrintMotd no
PrintLastLog no
ForceCommand /run/toolbox/session.sh
Subsystem sftp /usr/lib/openssh/sftp-server
''',0o444)
    name = 'hosting-toolbox-'+row['name']; network = 'hosting-toolbox-net-'+row['name']
    if host.inspect(name): raise ValueError('Stop the existing toolbox first')
    update_session(ledger,row,job['id'],'starting',built)
    step('starting private SSH toolbox')
    command(['docker','network','create','--label','hosting.toolbox.site='+row['id'], *([] if data['internet'] else ['--internal']),network])
    settings = json.loads(row['payload'])
    args = ['docker','create','--name',name,'--label','hosting.toolbox.site='+row['id'],
            '--user',f"{row['uid']}:{row['uid']}",'--cap-drop','ALL','--security-opt','no-new-privileges:true',
            '--read-only','--restart','unless-stopped','--network',network,'--publish','127.0.0.1::2222',
            '--pids-limit',str(settings.get('pids_limit') or -1),'--storage-opt','size='+ (str(settings['layer_mb'])+'m' if settings.get('layer_mb') else '0'),
            '--log-driver','local','--log-opt','max-size=1m','--log-opt','max-file=2',
            '--env','HOME=/toolhome','--env','WP_CLI_CACHE_DIR=/toolhome/.cache/wp-cli','--env','COMPOSER_HOME=/toolhome/.composer','--env','COMPOSER_CACHE_DIR=/toolhome/.cache/composer',
            '--env','TOOLBOX_DIRECTORY=/site'+('/'+data['path'] if data['path']!='.' else ''),'--workdir','/site']
    if settings.get('memory_mb'): args.extend(['--memory',str(settings['memory_mb'])+'m'])
    if settings.get('cpus'): args.extend(['--cpus',str(settings['cpus'])])
    from .database_site import state as database_state
    database = database_state(row)
    if settings.get('runtime')=='php' or database: args.extend(['--network','hosting-backend-'+row['name']])
    if settings.get('runtime')=='php': args.extend(['--env-file',str(root.parent/'.env')])
    if database: args.extend(['--env-file',str(root.parent/'.database-app.env')])
    from .requests_site import cli_access
    cli_access(args,root.parent)
    mount(args,root.parent/'html','/site',False)
    mount(args,root/'home','/toolhome',False); mount(args,root/'tmp','/tmp',False)
    for filename in ('host_key','authorized_keys','sshd_config','session-env'): mount(args,conf/filename,'/run/toolbox/'+filename)
    mount(args,TEMPLATES/'session.sh','/run/toolbox/session.sh')
    if built['php_branch']:
        from .wp_cli import phar
        mount(args,phar(),'/run/toolbox/wp-cli.phar')
        mount(args,TEMPLATES/'wp.sh','/usr/local/bin/wp')
        mount(args,composer(built['php_branch']),'/run/toolbox/composer.phar')
        mount(args,TEMPLATES/'composer.sh','/usr/local/bin/composer')
        # Keep matching PHP CLI settings when the recipe actually uses that branch.
        meta=yaml.safe_load((root.parent/'hosting.yaml').read_text())
        if meta.get('php_version')==built['php_branch']:
            mount(args,root.parent/'conf/php.ini','/etc/php/'+built['php_branch']+'/cli/conf.d/99-hosting.ini')
        from .mail import SHIM
        if SHIM.exists(): mount(args,SHIM,'/usr/local/bin/hosting-sendmail')
    # Create first, inspect the effective image+site environment privately, then
    # install it for SSH sessions (sshd intentionally resets most inherited env).
    atomic(conf/'session-env','',0o444)
    args.extend(['--entrypoint','/usr/sbin/sshd',built['image_id'],'-D','-e','-f','/run/toolbox/sshd_config'])
    command(args)
    live=host.inspect(name)
    exports=[]
    for item in live['Config'].get('Env',[]):
        key,value=item.split('=',1)
        if re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',key) and not key.startswith('SSH_'):
            exports.append('export '+key+'='+shlex.quote(value))
    atomic(conf/'session-env','\n'.join(exports)+'\n',0o444)
    command(['docker','update','--pids-limit',str(settings.get('pids_limit') or -1),name])
    command(['docker','start',name])
    until=time.monotonic()+20
    while time.monotonic()<until:
        live=host.inspect(name)
        if not live['State']['Running']: raise ValueError('SSH daemon exited; check the recipe provides OpenSSH and bash')
        ports=live['NetworkSettings']['Ports'].get('2222/tcp') or []
        if ports:
            port=int(ports[0]['HostPort'])
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=1) as conn:
                    if conn.recv(256).startswith(b'SSH-2.0-'): break
            except OSError: pass
        time.sleep(.5)
    else: raise ValueError('Toolbox SSH did not become ready')
    if any(p['HostIp']!='127.0.0.1' for p in ports) or live['Config']['User']!=f"{row['uid']}:{row['uid']}": raise ValueError('Unexpected SSH binding or identity')
    if any(m['Type']!='bind' for m in live['Mounts']): raise ValueError('Toolbox has an untracked volume')
    pids=command(['docker','exec',name,'cat','/sys/fs/cgroup/pids.max']).strip()
    if pids!=str(settings.get('pids_limit') or 'max'): raise ValueError('Toolbox process limit does not match site policy')
    host_public=(conf/'host_key.pub').read_text().strip()
    fingerprint=command(['ssh-keygen','-lf',conf/'host_key.pub']).strip()
    details=dict(built,port=port,jump=data['jump'],path=data['path'],internet=data['internet'],
                 host_public_key=host_public,fingerprint=fingerprint,
                 connect=f"ssh -J {data['jump']} -p {port} site@127.0.0.1")
    update_session(ledger,row,job['id'],'active',details)
    return details


def perform(ledger,host,row,job,step):
    from .content_site import OUTPUT
    try:
        if job['kind']=='toolbox-stop': stop(ledger,host,row); result={'status':'stopped; site content and toolbox home retained'}
        else: result=start(ledger,host,row,job,step)
        text=json.dumps(result,indent=2)
    except Exception as exc:
        # Stop any partially started SSH service; never leave an untracked listener.
        stop(ledger,host,row)
        OUTPUT.mkdir(mode=0o700,exist_ok=True)
        atomic(OUTPUT/(job['id']+'.txt'),str(exc)[:2000])
        raise ContentFailed('Toolbox operation failed; inspect its output') from None
    OUTPUT.mkdir(mode=0o700,exist_ok=True); atomic(OUTPUT/(job['id']+'.txt'),text)


def recover(ledger,host):
    initialize()
    with ledger.db() as db: rows=[dict(r) for r in db.execute("SELECT * FROM toolbox_sessions WHERE state='starting'")]
    affected={saved['site_id'] for saved in rows}
    affected.update(j['site_id'] for j in ledger.content_jobs() if j['kind'] in ('toolbox-start','toolbox-stop') and j['state']=='recovery-needed')
    for site_id in affected: stop(ledger,host,ledger.get(site_id))
