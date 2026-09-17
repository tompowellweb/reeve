"""Managed HTTPS/proxy/loopback settings and explicit PHP routing profiles."""
import copy
import ipaddress
import json
from pathlib import Path
import yaml
from .host import OPS,SITES,PROXY,NGINX,atomic,command,trusted

SAVED=OPS/'panel/worker/web-settings'

class WebRecoveryFailed(RuntimeError):
    pass

def public(row):
    path=SITES/row['name']/'hosting.yaml'
    return yaml.safe_load(path.read_text()).get('web_settings') if path.exists() else None


def validate(data):
    if not isinstance(data,dict) or set(data)!={'profile'} or data['profile'] not in ('php','wordpress','drupal7'):
        raise ValueError('Choose PHP, WordPress or Drupal 7 routing')
    return dict(data)


def render(cidr,profile,settings=None):
    from .php_site import nginx
    validate({'profile':profile}); ipaddress.ip_network(cidr)
    text=nginx(NGINX,settings)
    trust=f'''    set_real_ip_from {cidr};
    real_ip_header X-Forwarded-For;
    real_ip_recursive off;
    geo $realip_remote_addr $hosting_proxy {{ default 0; {cidr} 1; 127.0.0.1 1; }}
    absolute_redirect off;
'''
    text=text.replace('http {\n','http {\n'+trust)
    text=text.replace('        root /site;', '        if ($hosting_proxy = 0) { return 403; }\n        root /site;')
    # Supply each CGI parameter exactly once: nginx's stock include reports HTTP/8080.
    params='''fastcgi_param QUERY_STRING $query_string;
            fastcgi_param REQUEST_METHOD $request_method;
            fastcgi_param CONTENT_TYPE $content_type;
            fastcgi_param CONTENT_LENGTH $content_length;
            fastcgi_param SCRIPT_NAME $fastcgi_script_name;
            fastcgi_param REQUEST_URI $request_uri;
            fastcgi_param DOCUMENT_URI $document_uri;
            fastcgi_param DOCUMENT_ROOT /site;
            fastcgi_param SERVER_PROTOCOL $server_protocol;
            fastcgi_param REQUEST_SCHEME https;
            fastcgi_param GATEWAY_INTERFACE CGI/1.1;
            fastcgi_param SERVER_SOFTWARE nginx;
            fastcgi_param REMOTE_ADDR $remote_addr;
            fastcgi_param REMOTE_PORT $remote_port;
            fastcgi_param SERVER_ADDR $server_addr;
            fastcgi_param SERVER_NAME $host;
            fastcgi_param REDIRECT_STATUS 200;'''
    text=text.replace('include fastcgi_params;',params)
    if profile=='drupal7':
        text=text.replace('try_files $uri $uri/ /index.php?$query_string;', 'try_files $uri $uri/ /index.php?q=$uri&$args;')
        text=text.replace('        location ~ (^|/)\\.',r'''        location ~* \.(engine|inc|info|install|make|module|profile|po|sh|test|theme|xtmpl|tpl(\.php)?)([./~]|$) { return 404; }
        location ~* (^|/)(web\.config|Entries[^/]*|Repository|Root|Tag|Template|\#[^/]*\#)$ { return 404; }
        location ~* \.php(~|\.sw[op]|\.bak|\.orig|\.save)$ { return 404; }
        location ~* ^/(CHANGELOG|COPYRIGHT|INSTALL[^/]*|LICENSE|MAINTAINERS|UPGRADE)\.txt$ { return 404; }
        location ~ (^|/)\.''')
    return text


def cli_access(args,root):
    """Give tools the same CA trust and own-domain loopback mapping as managed PHP."""
    ca=root/'conf/ca-bundle.crt'
    if not ca.exists(): return
    trusted(ca)
    meta=yaml.safe_load((root/'hosting.yaml').read_text())
    if not meta.get('web_settings'): return
    from .content_site import mount
    mount(args,ca,'/etc/ssl/certs/ca-certificates.crt')
    for domain in [meta['domain'],*meta.get('aliases',[])]: args.extend(['--add-host',domain+':host-gateway'])


def deploy(host,row,compose,nginx):
    root=SITES/row['name']
    atomic(root/'conf/nginx.conf',nginx,0o644)
    atomic(root/'compose.yml',yaml.safe_dump(compose))
    command(['docker','compose','-f',root/'compose.yml','up','-d','--no-deps','--force-recreate','--wait','--wait-timeout','60','php','web'],timeout=120)
    for name in ('php','web'):
        service=compose['services'][name]
        if service.get('pids_limit')==-1: command(['docker','update','--pids-limit','-1',service['container_name']])
    host.verify_domains([yaml.safe_load((root/'hosting.yaml').read_text())['domain']])


def rollback(host,row,ident):
    path=SAVED/(ident+'.json')
    if not path.exists(): return
    trusted(path); old=json.loads(path.read_text())
    if old['site_id']!=row['id']: raise ValueError('Web settings recovery belongs to another site')
    deploy(host,row,old['compose'],old['nginx'])
    atomic(SITES/row['name']/'hosting.yaml',yaml.safe_dump(old['metadata']))


def apply(host,row,profile,ident=None,domains=None,publish=True):
    validate({'profile':profile}); root=SITES/row['name']; conf=root/'conf'
    for p in (root,conf): trusted(p,directory=True)
    for p in ('hosting.yaml','compose.yml','conf/nginx.conf','conf/site.nginx.conf'): trusted(root/p)
    meta=yaml.safe_load((root/'hosting.yaml').read_text()); compose=yaml.safe_load((root/'compose.yml').read_text())
    if meta.get('runtime')!='php' or meta['operation_id']!=row['id']: raise ValueError('This operation needs a managed PHP site')
    names=domains or [meta['domain'],*meta.get('aliases',[])]
    from .core import validate_domains
    validate_domains(names)
    network='hosting-ingress-'+row['name']; info=json.loads(command(['docker','network','inspect',network]))[0]
    if info.get('Labels',{}).get('hosting.operation')!=row['id'] or not info['Internal']: raise ValueError('Untrusted ingress network')
    from .php_settings import effective
    cidr=info['IPAM']['Config'][0]['Subnet']; nginx=render(cidr,profile,effective(meta,json.loads(row['payload'])))
    atomic(conf/'nginx.candidate.conf',nginx,0o644)
    web=compose['services']['web']
    command(['docker','run','--rm','--read-only','--network','hosting-backend-'+row['name'],'--user',web['user'],
        '--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/tmp',
        '--volume',str(conf/'nginx.candidate.conf')+':/etc/nginx/nginx.conf:ro',
        '--volume',str(conf/'site.nginx.conf')+':/etc/hosting/site.nginx.conf:ro',
        '--entrypoint','nginx',web['image'],'-t'])
    if publish: host.publish(row,network,names)
    prior={'site_id':row['id'],'compose':compose,'metadata':meta,'nginx':(conf/'nginx.conf').read_text()}
    if ident:
        from .core import request_id
        request_id(ident); SAVED.mkdir(mode=0o700,exist_ok=True); atomic(SAVED/(ident+'.json'),json.dumps(prior,indent=2))
    updated=copy.deepcopy(compose); php=updated['services']['php']; egress='hosting-egress-'+row['name']
    found=command(['docker','network','ls','-q','--filter','name=^'+egress+'$']).strip()
    if found:
        net=json.loads(command(['docker','network','inspect',egress]))[0]
        if net.get('Labels',{}).get('hosting.operation')!=row['id'] or net['Internal']: raise ValueError('Unmanaged egress network collision')
    else: command(['docker','network','create','--label','hosting.operation='+row['id'],egress])
    # Trust the operating system CAs plus this development edge's local CA. Never disable TLS verification.
    ca=Path('/etc/ssl/certs/ca-certificates.crt').read_text()+'\n'+(PROXY/'data/caddy/pki/authorities/local/root.crt').read_text()
    atomic(conf/'ca-bundle.crt',ca,0o444)
    php['volumes']=[v for v in php['volumes'] if v.split(':')[1]!='/etc/ssl/certs/ca-certificates.crt']
    php['volumes'].append(str(conf/'ca-bundle.crt')+':/etc/ssl/certs/ca-certificates.crt:ro')
    php['networks']['egress']={}; updated['networks']['egress']={'external':True,'name':egress}
    php['extra_hosts']={name:'host-gateway' for name in names}
    try:
        deploy(host,row,updated,nginx)
        meta.update(web_settings={'version':1,'profile':profile,'trusted_ingress':cidr},domain=names[0],aliases=names[1:])
        atomic(root/'hosting.yaml',yaml.safe_dump(meta))
    except Exception:
        try:
            deploy(host,row,prior['compose'],prior['nginx'])
            atomic(root/'hosting.yaml',yaml.safe_dump(prior['metadata']))
        except Exception as recovery:
            raise WebRecoveryFailed('Web settings rollback needs review: '+str(recovery)) from None
        raise


def perform(host,row,job,step):
    from .content_site import OUTPUT,ContentFailed
    step('validating and applying web settings')
    try: apply(host,row,validate(json.loads(job['payload']))['profile'],job['id'])
    except WebRecoveryFailed:
        raise
    except Exception as exc:
        OUTPUT.mkdir(mode=0o700,exist_ok=True); atomic(OUTPUT/(job['id']+'.txt'),str(exc)[:2000])
        raise ContentFailed('Web settings failed; previous configuration restored. Inspect output.') from None
    OUTPUT.mkdir(mode=0o700,exist_ok=True); atomic(OUTPUT/(job['id']+'.txt'),'Web settings validated and applied; PHP image, data and credentials retained.\n')


def recover(ledger,host):
    for job in ledger.content_jobs():
        if job['kind']=='web-settings' and job['state']=='recovery-needed': rollback(host,ledger.get(job['site_id']),job['id'])
