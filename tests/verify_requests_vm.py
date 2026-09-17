"""Request/isolation checks with synthetic files and a brief FPM outage on the disposable WP fixture."""
import json
import tempfile
from pathlib import Path
from reeve.core import Ledger
from reeve.host import Host,PROXY,atomic,command

ledger=Ledger('/srv/ops/panel/worker/jobs.sqlite3'); host=Host()
row=next(r for r in ledger.list() if r['name']=='m24-wordpress'); domain=row['domain']; name=row['name']
report={}; output=Path('/srv/ops/panel/worker/requests-acceptance.json')
if output.exists(): report=json.loads(output.read_text())

def fetch(path,headers=(),scheme='https'):
    with tempfile.TemporaryDirectory() as tmp:
        header=Path(tmp)/'headers'; body=Path(tmp)/'body'; port='443' if scheme=='https' else '80'
        args=['curl','--noproxy','*','--silent','--show-error','--max-time','15','--cacert',PROXY/'data/caddy/pki/authorities/local/root.crt',
            '--resolve',domain+':'+port+':127.0.0.1','--dump-header',header,'--output',body,'--write-out','%{http_code}']
        for k,v in headers: args.extend(['--header',k+': '+v])
        code=int(command([*args,scheme+'://'+domain+path]).strip()); fields={}
        for line in header.read_text().splitlines():
            if ':' in line:
                k,v=line.split(':',1); fields[k.lower()]=v.strip()
        return code,fields,body.read_text()

status,headers,body=fetch('/request-proof.php?probe=normal'); assert status==200
normal=json.loads(body)
for k,v in {'HTTPS':'on','SERVER_PORT':'443','REQUEST_SCHEME':'https','HTTP_HOST':domain,'HTTP_X_FORWARDED_PROTO':'https','HTTP_X_FORWARDED_PORT':'443'}.items(): assert normal[k]==v,(k,normal)
assert normal['uid']==row['uid']
forged=[('X-Forwarded-For','203.0.113.99'),('X-Forwarded-Proto','http'),('X-Forwarded-Host','attacker.invalid'),('X-Forwarded-Port','81'),('Forwarded','for=203.0.113.99;proto=http;host=attacker.invalid'),('X-Real-IP','203.0.113.99')]
status,_,body=fetch('/request-proof.php?probe=forged',forged); assert status==200
proof=json.loads(body)
assert proof['REMOTE_ADDR']==normal['REMOTE_ADDR'] and proof['HTTP_X_FORWARDED_FOR']==normal['REMOTE_ADDR'],proof
assert proof['HTTP_FORWARDED'] is None and proof['HTTP_X_REAL_IP']==normal['REMOTE_ADDR'],proof
for k in ('HTTPS','SERVER_PORT','REQUEST_SCHEME','HTTP_HOST','HTTP_X_FORWARDED_PROTO','HTTP_X_FORWARDED_HOST','HTTP_X_FORWARDED_PORT'): assert proof[k]==normal[k],proof
assert 'secure' in headers.get('set-cookie','').lower() and 'httponly' in headers.get('set-cookie','').lower()
status,headers,_=fetch('/wp-admin'); assert status in (301,308) and headers['location']=='/wp-admin/',(status,headers)
status,headers,_=fetch('/hosting-permalink-proof/?v=one',scheme='http'); assert status in (301,308) and headers['location']=='https://'+domain+'/hosting-permalink-proof/?v=one',(status,headers)
command(['docker','exec','hosting-php-'+name,'sh','-c',"mkdir -p /site/wp-content/uploads /site/sites/default/files; printf '%s' '<?php echo \"MUST-NOT-EXECUTE\";' > /site/wp-content/uploads/m24-blocked.php; cp /site/wp-content/uploads/m24-blocked.php /site/sites/default/files/m24-blocked.php; printf private > /site/.htaccess"])
for path in ('/wp-config.php','/.htaccess','/missing.php','/wp-content/uploads/m24-blocked.php','/sites/default/files/m24-blocked.php'):
    status,_,_=fetch(path); assert status in (403,404),(path,status)
web=host.inspect('hosting-site-'+name); php=host.inspect('hosting-php-'+name)
assert not web['HostConfig']['PortBindings'] and not php['HostConfig']['PortBindings']
assert 'hosting-ingress-'+name not in php['NetworkSettings']['Networks']
assert php['HostConfig']['Memory']==0 and php['HostConfig']['NanoCpus']==0
assert command(['docker','exec','hosting-php-'+name,'cat','/sys/fs/cgroup/pids.max']).strip()=='max'
neighbor=host.inspect('hosting-php-m2-db-postgres')['NetworkSettings']['Networks']['hosting-backend-m2-db-postgres']['IPAddress']
probe='$s=@fsockopen('+json.dumps(neighbor)+',9000,$n,$e,1); echo $s?"reachable":"isolated";'
assert command(['docker','exec','hosting-php-'+name,'php','-r',probe]).strip()=='isolated'
backend_ip=web['NetworkSettings']['Networks']['hosting-backend-'+name]['IPAddress']
code='$c=curl_init('+json.dumps('http://'+backend_ip+':8080/request-proof.php')+'); curl_setopt_array($c,array(CURLOPT_RETURNTRANSFER=>true,CURLOPT_HTTPHEADER=>array("X-Forwarded-For: 203.0.113.99","X-Forwarded-Proto: https"))); curl_exec($c); echo curl_getinfo($c,CURLINFO_HTTP_CODE);'
assert command(['docker','exec','hosting-php-'+name,'php','-r',code]).strip()=='403'
try:
    command(['docker','stop','hosting-php-'+name])
    status,_,body=fetch('/request-proof.php'); assert status in (502,503,504) and '<?php' not in body,(status,body[:1000])
    outage_status=status
finally: command(['docker','start','hosting-php-'+name])
host.verify_domains([domain,'m24-preview.hosting.test'])
report['request']={'normal':normal,'spoofed':proof,'session_cookie_secure':True,'session_cookie_httponly':True,
    'http_redirect':True,'directory_redirect_relative':True,'direct_backend_rejected':True,'private_sources_blocked':True,'fpm_outage_no_source':True,'fpm_outage_status':outage_status,'neighbor_php_isolated':True,'unlimited_caps_preserved':True,
    'php_networks':list(php['NetworkSettings']['Networks'])}
atomic(output,json.dumps(report,indent=2)); print(json.dumps({'request':'passed','uid':row['uid']}))
