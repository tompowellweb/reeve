"""Authenticated DB addition, credentials privacy, and catalogue UI acceptance."""
import argparse
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    p=argparse.ArgumentParser();p.add_argument('--password-file',required=True);p.add_argument('--name',required=True)
    p.add_argument('--limited',action='store_true');p.add_argument('--engine',required=True);p.add_argument('--series');p.add_argument('--output',default='/var/lib/hosting-browser/results')
    args=p.parse_args();base='http://127.0.0.1:8088';output=Path(args.output)
    with sync_playwright() as play:
        browser=play.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--host-resolver-rules=MAP *.hosting.test 127.0.0.1','--no-proxy-server'])
        context=browser.new_context(viewport={'width':1280,'height':1000});page=context.new_page()
        page.goto(base);page.get_by_label('Password').fill(Path(args.password_file).read_text().strip())
        page.get_by_role('button',name='Sign in',exact=True).click();page.wait_for_url(base+'/')
        page.goto(base+'/databases/versions')
        assert page.locator('table').count()==3
        page.screenshot(path=str(output/'m2-database-catalogue.png'),full_page=True)
        page.goto(base+'/sites/'+args.name)
        page.get_by_role('button',name='Add database',exact=True).click()
        page.get_by_label('Database (optional)',exact=True).select_option(args.engine)
        if args.series:page.locator('#db-series-'+args.engine).select_option(args.series)
        if args.limited:
            page.get_by_text('Database compatibility and limits',exact=True).click()
            for key,value in [('memory_mb','768'),('cpus','0.8'),('layer_mb','64'),('pids_limit','128')]:page.locator('[name=db_'+key+']').fill(value)
        with page.expect_navigation(wait_until='domcontentloaded'):
            page.locator('#database-dialog').get_by_role('button',name='Add database',exact=True).click()
        page.close();page=context.new_page()
        for _ in range(900):
            row=next(r for r in page.request.get(base+'/api/sites').json() if r['name']==args.name)
            status=row['database_job']['state']
            if status=='succeeded':break
            assert status not in ('failed','recovery-needed'),row
            time.sleep(1)
        assert status=='succeeded'
        row=next(r for r in page.request.get(base+'/api/sites').json() if r['name']==args.name)
        assert row['database']['health']=='healthy',row
        page.goto(base+'/sites/'+args.name)
        body=page.locator('body').inner_text()
        page.screenshot(path=str(output/'m2-database-added.png'),full_page=True)
        page.get_by_role('button',name='Show database credentials',exact=True).click()
        password=page.locator('dd').last.inner_text()
        assert len(password)==48 and password not in body and password not in json.dumps(row)
        # Never record the credential page or password.
        output.joinpath('m2-database-added.json').write_text(json.dumps({'site':row,'credentials':'authenticated explicit reveal; absent from normal page/API'},indent=2))
        print(json.dumps({'name':args.name,'database':row['database'],'credentials_privacy':'passed'}))
        browser.close()


if __name__=='__main__':main()
