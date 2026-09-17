"""Save a custom tools recipe and start its SSH session through the real panel."""
import argparse
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    p=argparse.ArgumentParser(); p.add_argument('--name',default='m2-db-postgres'); p.add_argument('--stop',action='store_true')
    p.add_argument('--reuse',action='store_true'); p.add_argument('--path',default='.')
    p.add_argument('--password-file',default='/var/lib/hosting-browser/acceptance-password')
    p.add_argument('--public-key-file',default='/var/lib/hosting-browser/operator.pub')
    args=p.parse_args()
    base='http://127.0.0.1:8088'; url=base+'/sites/'+args.name+'/toolbox'
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True)
        context=browser.new_context(viewport={'width':1280,'height':1000}); page=context.new_page(); errors=[]
        page.on('pageerror',lambda exc:errors.append(str(exc)))
        page.goto(base+'/login'); page.locator('[name=password]').fill(Path(args.password_file).read_text().strip())
        page.get_by_role('button',name='Sign in').click(); page.wait_for_url(base+'/'); page.goto(url)
        if args.stop:
            with page.expect_navigation(wait_until='domcontentloaded'): page.get_by_role('button',name='Stop toolbox').click()
        else:
            if not args.reuse:
                form=page.locator('form[action$="/toolbox/save"]').first
                form.locator('[name=name]').fill('wordpress-workbench')
                recipe=form.locator('[name=dockerfile]').input_value()+"\nRUN printf '#!/bin/sh\\necho custom-toolbox\\n' > /usr/local/bin/hosting-toolbox-proof && chmod 755 /usr/local/bin/hosting-toolbox-proof\n"
                form.locator('[name=dockerfile]').fill(recipe)
                with page.expect_navigation(wait_until='domcontentloaded'): form.get_by_role('button',name='Save recipe').click()
            form=page.locator('form[action$="/toolbox/start"]')
            form.locator('[name=recipe]').select_option('wordpress-workbench')
            form.locator('[name=public_key]').fill(Path(args.public_key_file).read_text().strip())
            form.locator('[name=jump]').fill('admin@203.0.113.10')
            form.locator('[name=path]').fill(args.path)
            with page.expect_navigation(wait_until='domcontentloaded'): form.get_by_role('button',name='Build and start toolbox').click()
        page.goto('about:blank')
        until=time.monotonic()+1200
        while time.monotonic()<until:
            jobs=context.request.get(base+'/sites/'+args.name+'/content/status').json()
            if jobs and jobs[0]['state'] not in ('queued','running'):
                job=jobs[0]
                if job['state']!='succeeded':
                    page.goto(base+'/sites/'+args.name+'/content/output/'+job['id'])
                    raise AssertionError(page.locator('pre').inner_text())
                break
            time.sleep(3)
        else: raise AssertionError('Toolbox did not finish')
        page.goto(url)
        if args.stop: assert page.get_by_role('heading',name='Start a toolbox').is_visible()
        else: assert page.get_by_role('button',name='Stop toolbox').is_visible()
        assert not errors,errors
        out=Path('/var/lib/hosting-browser/results')
        page.screenshot(path=str(out/(args.name+'-toolbox'+('-stopped' if args.stop else '')+'.png')),full_page=True)
        result={'site':args.name,'operation':job['id'],'state':job['state'],'action':'stop' if args.stop else 'start','browser_errors':errors}
        (out/(args.name+'-toolbox'+('-stopped' if args.stop else '')+'.json')).write_text(json.dumps(result,indent=2))
        print(json.dumps(result)); browser.close()


if __name__=='__main__': main()
