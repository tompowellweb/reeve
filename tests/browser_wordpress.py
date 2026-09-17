"""Fresh WordPress archive via the real private panel, with no secrets in evidence."""
import argparse
import hashlib
import json
import os
import secrets
import shlex
import time
from urllib.parse import urlsplit,parse_qs
from pathlib import Path
from playwright.sync_api import sync_playwright

NAME='m24-wordpress'; DOMAIN=NAME+'.hosting.test'; BASE='http://127.0.0.1:8088'
OUT=Path('/var/lib/hosting-browser/results'); REPORT=OUT/(NAME+'-acceptance.json')


def main():
    p=argparse.ArgumentParser(); p.add_argument('phase',choices=('import','install','wpcli','verify','schedule'))
    args=p.parse_args(); report=json.loads(REPORT.read_text()) if REPORT.exists() else {'site':NAME,'operations':[]}
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--host-resolver-rules=MAP *.hosting.test 127.0.0.1'])
        context=browser.new_context(viewport={'width':1280,'height':1000}); page=context.new_page(); errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        failed_assets=[]
        page.on('response',lambda r:failed_assets.append({'url':r.url,'status':r.status}) if urlsplit(r.url).hostname==DOMAIN and r.status>=400 and r.request.resource_type in ('script','stylesheet','image','font') else None)
        page.goto(BASE+'/login'); page.locator('[name=password]').fill(Path('/var/lib/hosting-browser/acceptance-password').read_text().strip())
        page.get_by_role('button',name='Sign in').click(); page.wait_for_url(BASE+'/')
        files=BASE+'/sites/'+NAME+'/files'
        def settle(ident,expected='succeeded'):
            page.goto('about:blank')
            until=time.monotonic()+600
            while time.monotonic()<until:
                jobs=context.request.get(BASE+'/sites/'+NAME+'/content/status').json()
                job=next(j for j in jobs if j['id']==ident)
                if job['state'] not in ('queued','running'):
                    page.goto(BASE+'/sites/'+NAME+'/content/output/'+ident); output=page.locator('pre').inner_text()
                    assert job['state']==expected,(job['state'],output)
                    report['operations'].append({'id':ident,'kind':job['kind'],'state':job['state']})
                    page.goto(files); return output
                time.sleep(2)
            raise AssertionError('Operation timed out')
        def upload(kind,name,raw,path='.'):
            page.goto(files); form=page.locator('form.content-upload').filter(has=page.locator('select[name=kind]'))
            form.locator('[name=kind]').select_option(kind); form.locator('[name=path]').fill(path); form.locator('[name=replace]').check()
            form.locator('[name=file]').set_input_files({'name':name,'mimeType':'application/octet-stream','buffer':raw})
            with page.expect_response(lambda r:'/content/upload?' in r.url and r.request.method=='POST') as response:
                with page.expect_navigation(wait_until='domcontentloaded',timeout=120000): form.locator('button').click()
            assert response.value.ok
            return settle(parse_qs(urlsplit(response.value.url).query)['id'][0])
        def tool(command,kind='php',internet=False):
            page.goto(files); form=page.locator('form[action$="/content/tool"]'); ident=form.locator('[name=id]').input_value()
            form.locator('[name=tool]').select_option(kind); form.locator('[name=arguments]').fill(command)
            if internet: form.locator('[name=internet]').check()
            with page.expect_navigation(wait_until='domcontentloaded'): form.locator('button').click()
            return settle(ident)
        if args.phase=='import':
            raw=Path('/var/lib/hosting-browser/wordpress-7.1.zip').read_bytes(); digest=hashlib.sha256(raw).hexdigest()
            assert digest=='d1ae02b5ae18428031ffc3943659fa87ab361d827f4aa804adf9276e4dc75df6'
            upload('extract','wordpress-7.1.zip',raw)
            tool('cp -a wordpress/. . && rm -r wordpress','shell')
            salts='\n'.join("define('%s', '%s');"%(key,secrets.token_hex(48)) for key in ('AUTH_KEY','SECURE_AUTH_KEY','LOGGED_IN_KEY','NONCE_KEY','AUTH_SALT','SECURE_AUTH_SALT','LOGGED_IN_SALT','NONCE_SALT'))
            config="""<?php
foreach (array('NAME','USER','PASSWORD','HOST') as $part) define('DB_'.$part,getenv('DATABASE_'.$part));
define('DB_CHARSET','utf8mb4'); define('DB_COLLATE','');
$table_prefix='wp_';
define('WP_HOME','https://m24-wordpress.hosting.test');
define('WP_SITEURL','https://m24-wordpress.hosting.test');
define('DISABLE_WP_CRON',true); define('AUTOMATIC_UPDATER_DISABLED',true);
"""+salts+"\nif (!defined('ABSPATH')) define('ABSPATH',__DIR__.'/');\nrequire_once ABSPATH.'wp-settings.php';\n"
            upload('upload','wp-config.php',config.encode(),'wp-config.php')
            upload('upload','request-proof.php',(Path(__file__).parent/'fixtures/request-proof.php').read_bytes(),'request-proof.php')
            plugin=b'''<?php
// Development local CA only; certificate verification stays enabled.
add_filter('http_request_args', function($args,$url) {
    if (in_array(parse_url($url,PHP_URL_HOST),array('m24-wordpress.hosting.test','m24-preview.hosting.test'),true)) $args['sslcertificates']='/etc/ssl/certs/ca-certificates.crt';
    return $args;
},10,2);
add_action('hosting_acceptance_cron',function(){
    $runs=(int)get_option('hosting_acceptance_runs',0)+1;
    update_option('hosting_acceptance_runs',$runs);
    file_put_contents(WP_CONTENT_DIR.'/m24-cron-proof.json',json_encode(array('time'=>time(),'uid'=>posix_geteuid(),'runs'=>$runs)));
});
'''
            upload('upload','hosting-acceptance.php',plugin,'wp-content/mu-plugins/hosting-acceptance.php')
            report['archive']={'version':'7.1','sha256':digest,'source':'https://downloads.wordpress.org/release/wordpress-7.1.zip'}
            response=page.goto('https://'+DOMAIN+'/request-proof.php?loopback=1'); proof=response.json()
            assert proof['HTTPS']=='on' and proof['SERVER_PORT']=='443' and proof['REQUEST_SCHEME']=='https',proof
            assert proof['loopback']['status']==200 and proof['loopback']['verify']==0,proof
            report['request']=proof
        if args.phase=='install':
            page.goto(files); form=page.locator('form[action$="/web-settings"]'); ident=form.locator('[name=id]').input_value()
            form.locator('[name=profile]').select_option('wordpress')
            with page.expect_navigation(wait_until='domcontentloaded'): form.locator('button').click()
            settle(ident)
            password_path=Path('/var/lib/hosting-browser/wordpress-acceptance-password')
            if not password_path.exists():
                password_path.write_text(secrets.token_urlsafe(24)+'\n'); password_path.chmod(0o600)
            password=password_path.read_text().strip()
            page.goto('https://'+DOMAIN+'/wp-admin/install.php')
            if page.locator('#language-continue').count():
                page.locator('#language').select_option('')
                page.locator('#language-continue').click(); page.wait_for_load_state('domcontentloaded')
            if page.locator('#weblog_title').count():
                page.locator('#weblog_title').fill('Hosting WordPress acceptance')
                page.locator('#user_login').fill('hosting-admin')
                page.locator('[name=admin_password]').fill(password)
                if page.locator('[name=admin_password2]').is_visible(): page.locator('[name=admin_password2]').fill(password)
                page.locator('#admin_email').fill('wordpress-test@example.invalid')
                if page.locator('#blog-norobots').count(): page.locator('#blog-norobots').check()
                elif page.locator('#blog_public').count(): page.locator('#blog_public').check()
                page.get_by_role('button',name='Install WordPress').click(); page.wait_for_load_state('domcontentloaded')
                assert page.get_by_text('Success!',exact=True).is_visible()
            else: assert page.get_by_role('heading',name='Already Installed',exact=True).is_visible()
            page.goto('https://'+DOMAIN+'/wp-login.php'); page.locator('#user_login').fill('hosting-admin'); page.locator('#user_pass').fill(password)
            page.locator('#wp-submit').click(); page.wait_for_url('**/wp-admin/')
            cookies=[{'name':c['name'].split('_')[0],'secure':c['secure'],'httpOnly':c['httpOnly']} for c in context.cookies() if c['domain']==DOMAIN and (c['name'].startswith('wordpress_logged_in_') or c['name'].startswith('wordpress_sec_'))]
            assert len(cookies)>=2 and all(c['secure'] and c['httpOnly'] for c in cookies),cookies
            report['login']={'admin':'hosting-admin','secure_cookies':cookies}
            page.screenshot(path=str(OUT/(NAME+'-admin.png')),full_page=True)
            command="require 'wp-load.php'; update_option('permalink_structure','/%postname%/'); $existing=get_page_by_path('hosting-permalink-proof',OBJECT,'post'); $p=$existing?$existing->ID:wp_insert_post(array('post_title'=>'Hosting permalink proof','post_name'=>'hosting-permalink-proof','post_content'=>'Fresh archive and PHP hosting work','post_status'=>'publish')); echo json_encode(array('id'=>$p,'url'=>get_permalink($p)));"
            report['post']=json.loads(tool('-r '+shlex.quote(command)))
        if args.phase=='wpcli':
            commands=['--info', 'core version', 'core update', 'core update --version=7.1 --force',
                      'plugin delete hello-dolly', 'plugin install akismet --version=5.3 --force', 'plugin update --all',
                      'plugin list --format=json', "rewrite structure '/%postname%/'", 'rewrite flush']
            report['wp_cli']={command:tool(command,'wp',internet=True) for command in commands}
            assert '2.12.0' in report['wp_cli']['--info']
            assert 'WordPress updated successfully' in report['wp_cli']['core update --version=7.1 --force']
            assert 'Updated 1 of 1 plugins' in report['wp_cli']['plugin update --all']
        if args.phase=='verify':
            report['current_request']=page.goto('https://'+DOMAIN+'/request-proof.php').json()

            response=page.goto('https://'+DOMAIN+'/hosting-permalink-proof/'); assert response.ok
            assert 'Fresh archive and PHP hosting work' in page.locator('body').inner_text()
            response=page.goto('https://m24-preview.hosting.test/hosting-permalink-proof/'); assert response.ok
            assert 'Fresh archive and PHP hosting work' in page.locator('body').inner_text()
            canonical=page.locator('link[rel=canonical]').get_attribute('href'); assert canonical=='https://'+DOMAIN+'/hosting-permalink-proof/'
            report['preview_alias']={'url':page.url,'canonical':canonical,'status':response.status}
            # WordPress uses its own CA bundle unless an application filter supplies the local test CA.
            code="require 'wp-load.php'; $r=wp_remote_get(home_url('/request-proof.php?wordpress=1')); if(is_wp_error($r)){fwrite(STDERR,$r->get_error_message());exit(1);} echo json_encode(array('status'=>wp_remote_retrieve_response_code($r),'body'=>json_decode(wp_remote_retrieve_body($r),true)));"
            proof=json.loads(tool('-r '+shlex.quote(code),internet=True)); assert proof['status']==200 and proof['body']['HTTPS']=='on',proof
            report['wordpress_loopback']=proof
            page.goto('https://'+DOMAIN+'/hosting-permalink-proof/'); page.screenshot(path=str(OUT/(NAME+'-page.png')),full_page=True)
        if args.phase=='schedule':
            code="require 'wp-load.php'; wp_clear_scheduled_hook('hosting_acceptance_cron'); wp_schedule_single_event(time()+10,'hosting_acceptance_cron'); echo json_encode(array('scheduled'=>wp_next_scheduled('hosting_acceptance_cron'),'page_cron_disabled'=>DISABLE_WP_CRON));"
            report['cron_event']=json.loads(tool('-r '+shlex.quote(code))); assert report['cron_event']['page_cron_disabled']
            page.goto(BASE+'/sites/'+NAME+'/schedules'); form=page.locator('section').filter(has=page.get_by_role('heading',name='Add schedule')).locator('form')
            form.locator('[name=name]').fill('wordpress'); form.locator('[name=interval]').fill('1'); form.locator('[name=tool]').select_option('wp'); form.locator('[name=arguments]').fill('cron event run --due-now'); form.locator('[name=internet]').check()
            with page.expect_navigation(wait_until='domcontentloaded'): form.get_by_role('button',name='Save schedule').click()
            assert page.get_by_role('heading',name='wordpress',exact=True).is_visible()
            page.screenshot(path=str(OUT/(NAME+'-schedule.png')),full_page=True)
            report['schedule_details']={'name':'wordpress','interval_minutes':1,'page_closed_at':time.time()}
        assert not errors,errors
        assert not failed_assets,failed_assets
        report[args.phase+'_failed_assets']=failed_assets
        report[args.phase]='passed'; REPORT.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps({'phase':args.phase,'result':'passed','browser_errors':errors})); browser.close()


if __name__=='__main__': main()
