"""Apply the Drupal 7 profile to an existing PHP 7.0 fixture; preserve its original index."""
import io,json,time,zipfile
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
from playwright.sync_api import sync_playwright

NAME='m2-unlimited-php70'; BASE='http://127.0.0.1:8088'; DOMAIN=NAME+'.hosting.test'
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--host-resolver-rules=MAP *.hosting.test 127.0.0.1'])
    context=browser.new_context(); page=context.new_page(); errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE+'/login'); page.locator('[name=password]').fill(Path('/var/lib/hosting-browser/acceptance-password').read_text().strip())
    page.get_by_role('button',name='Sign in').click(); page.wait_for_url(BASE+'/'); files=BASE+'/sites/'+NAME+'/files'; records=[]
    def settle(ident):
        page.goto('about:blank'); until=time.monotonic()+300
        while time.monotonic()<until:
            job=next(j for j in context.request.get(BASE+'/sites/'+NAME+'/content/status').json() if j['id']==ident)
            if job['state'] not in ('queued','running'):
                page.goto(BASE+'/sites/'+NAME+'/content/output/'+ident); output=page.locator('pre').inner_text()
                assert job['state']=='succeeded',(job['state'],output)
                records.append({'id':ident,'kind':job['kind'],'state':job['state']}); page.goto(files); return output
            time.sleep(2)
        raise AssertionError('Operation timeout')
    def tool(command,kind="shell"):
        page.goto(files); form=page.locator('form[action$="/content/tool"]'); ident=form.locator('[name=id]').input_value()
        form.locator('[name=tool]').select_option(kind); form.locator('[name=arguments]').fill(command)
        with page.expect_navigation(wait_until='domcontentloaded'): form.locator('button').click()
        return settle(ident)
    wp_info=tool('--info','wp'); assert '2.12.0' in wp_info and '7.0.' in wp_info,wp_info
    raw=io.BytesIO(); probe=(Path(__file__).parent/'fixtures/request-proof.php').read_bytes()
    with zipfile.ZipFile(raw,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('request-proof.php',probe); z.writestr('m24/front-controller.php',probe)
        for name in ('m24/private.module','m24/private.inc','m24/private.info','m24/private.xtmpl','m24/web.config','m24/index.php.save','m24/.private','sites/default/files/m24-blocked.php'):
            z.writestr(name,'<?php echo "MUST-NOT-EXECUTE";')
    page.goto(files); form=page.locator('form.content-upload').filter(has=page.locator('select[name=kind]'))
    form.locator('[name=kind]').select_option('extract'); form.locator('[name=replace]').check()
    form.locator('[name=file]').set_input_files({'name':'legacy-request-fixture.zip','mimeType':'application/zip','buffer':raw.getvalue()})
    with page.expect_response(lambda r:'/content/upload?' in r.url and r.request.method=='POST') as response:
        with page.expect_navigation(wait_until='domcontentloaded'): form.locator('button').click()
    settle(parse_qs(urlsplit(response.value.url).query)['id'][0])
    form=page.locator('form[action$="/web-settings"]'); ident=form.locator('[name=id]').input_value(); form.locator('[name=profile]').select_option('drupal7')
    with page.expect_navigation(wait_until='domcontentloaded'): form.locator('button').click()
    settle(ident)
    tool('test ! -e m24/original-index.php && cp -p index.php m24/original-index.php && cp m24/front-controller.php index.php')
    try:
        result=page.goto('https://'+DOMAIN+'/m24/a-clean-path?value=a%20b').json()
        assert result['php'].startswith('7.0.') and result['HTTPS']=='on' and result['SERVER_PORT']=='443',result
        assert result['query']['q'].strip('/')=='m24/a-clean-path' and result['query']['value']=='a b',result
        loopback=page.goto('https://'+DOMAIN+'/request-proof.php?loopback=1').json()
        assert loopback['loopback']['status']==200 and loopback['loopback']['verify']==0,loopback
        for path in ('/m24/private.module','/m24/private.inc','/m24/private.info','/m24/private.xtmpl','/m24/web.config','/m24/index.php.save','/m24/.private','/sites/default/files/m24-blocked.php'):
            r=page.goto('https://'+DOMAIN+path); assert r.status==404,(path,r.status)
    finally:
        tool('test -f m24/original-index.php && mv m24/original-index.php index.php')
    assert not errors,errors
    report={'site':NAME,'wp_cli_info':wp_info,'clean_url':result,'loopback':loopback,'protected_paths':True,'original_index_restored':True,'operations':records,'browser_errors':errors}
    Path('/var/lib/hosting-browser/results/m24-legacy-requests.json').write_text(json.dumps(report,indent=2))
    print(json.dumps({'legacy_requests':'passed','php':result['php']})); browser.close()
