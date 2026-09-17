"""M2.5 empty environment -> native SQL/archive import through the real UI.

Run as hosting-browser after verify_imports_vm prepare. Fixed disposable names;
--resume skips existing successful work only after checking the durable report.
"""
import argparse
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:8088'
INPUT = Path('/var/lib/hosting-browser/m25-inputs')
OUTPUT = Path('/var/lib/hosting-browser/results/m25-imports.json')
CASES = [('m25-legacy', '7.0', 'mariadb', '10.11'), ('m25-mysql', '8.5', 'mysql', '9.7'),
         ('m25-mariadb', '8.5', 'mariadb', '12.3'), ('m25-postgres', '8.5', 'postgres', '18'),
         ('m25-static', None, None, None)]
ROWS = [[i, 'record-'+str(i)] for i in range(1, 201)]
HASH = hashlib.sha256(json.dumps(ROWS, separators=(',', ':')).encode()).hexdigest()
FILE = 'Restored attachment: séjour, £, <>&\n'.encode()


def archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as tar:
        root = tarfile.TarInfo('.'); root.type = tarfile.DIRTYPE; tar.addfile(root)
        for path, content in files.items():
            raw = content.encode() if isinstance(content, str) else content
            item = tarfile.TarInfo('./'+path); item.size = len(raw); item.uid = 1002; item.gid = 1002
            tar.addfile(item, io.BytesIO(raw))
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    sources = json.loads((INPUT/'sources.json').read_text())
    report = json.loads(OUTPUT.read_text()) if args.resume and OUTPUT.exists() else {'cases': {}}
    if not args.resume: assert not OUTPUT.exists(), 'Use --resume for an existing run'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
            args=['--host-resolver-rules=MAP *.hosting.test 127.0.0.1', '--no-proxy-server'])
        context = browser.new_context(viewport={'width': 1280, 'height': 1000})
        page = context.new_page(); errors = []; page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.goto(BASE+'/login'); page.locator('[name=password]').fill(Path('/var/lib/hosting-browser/acceptance-password').read_text().strip())
        page.get_by_role('button', name='Sign in', exact=True).click(); page.wait_for_url(BASE+'/')
        for name, php, engine, series in CASES:
            if report['cases'].get(name, {}).get('passed'): continue
            result = report['cases'].setdefault(name, {'operations': []})

            def save():
                OUTPUT.write_text(json.dumps(report, indent=2))

            existing = next((r for r in context.request.get(BASE+'/api/sites').json() if r['name'] == name), None)
            if not existing:
                page.goto(BASE+'/'); page.get_by_role('link', name='Create site', exact=True).click()
                page.get_by_label('Site name', exact=True).fill(name)
                page.get_by_label('Primary domain', exact=True).fill(name+'.hosting.test')
                page.get_by_label('Additional domains (optional)', exact=True).fill(name+'-preview.hosting.test')
                if php:
                    page.get_by_label('Site type', exact=True).select_option('php')
                    page.get_by_label('PHP branch', exact=True).select_option(php)
                    page.get_by_label('Database (optional)', exact=True).select_option(engine)
                    page.locator('#db-series-'+engine).select_option(series)
                page.get_by_role('button', name='Create site', exact=True).click()
                page.wait_for_url(BASE+'/sites/'+name); page.goto('about:blank')
            else:
                assert args.resume, 'Fixture already exists: '+name
            deadline = time.monotonic()+900
            while time.monotonic() < deadline:
                site = next(r for r in context.request.get(BASE+'/api/sites').json() if r['name'] == name)
                if site['state'] == 'succeeded': break
                assert site['state'] not in ('failed', 'recovery-needed'), site['state']
                time.sleep(2)
            assert site['state'] == 'succeeded'
            assert site['php_branch'] == php and site['domain'] == name+'.hosting.test'
            if engine: assert site['database']['engine'] == engine and site['database']['series'] == series
            result.update({'site_id': site['id'], 'uid': site['uid'], 'project': site['project'], 'php': site['health'].get('php_version'), 'database': site['database']})
            files_url = BASE+'/sites/'+name+'/files'

            def settle(ident, expected='succeeded'):
                page.goto('about:blank'); deadline = time.monotonic()+600
                while time.monotonic() < deadline:
                    jobs = context.request.get(BASE+'/sites/'+name+'/content/status').json()
                    job = next(j for j in jobs if j['id'] == ident)
                    if job['state'] not in ('queued', 'running'):
                        page.goto(BASE+'/sites/'+name+'/content/output/'+ident)
                        output = page.locator('pre').inner_text()
                        result['operations'].append({'id': ident, 'kind': job['kind'], 'state': job['state']}); save()
                        assert job['state'] == expected, (name, job['state'], output)
                        page.goto(files_url); return output
                    time.sleep(1)
                raise AssertionError('Job timed out: '+ident)

            def upload(kind, filename, raw, path='.'):
                page.goto(files_url)
                form = page.locator('form.content-upload').filter(has=page.locator('input[value=sql]' if kind == 'sql' else 'select[name=kind]'))
                if kind == 'sql': form.locator('[name=confirm]').check()
                else:
                    form.locator('[name=kind]').select_option(kind); form.locator('[name=path]').fill(path)
                    form.locator('[name=replace]').check()
                form.locator('[name=file]').set_input_files({'name': filename, 'mimeType': 'application/octet-stream', 'buffer': raw})
                with page.expect_response(lambda r: '/content/upload?' in r.url and r.request.method == 'POST') as response:
                    with page.expect_navigation(wait_until='domcontentloaded'): form.locator('button').click()
                ident = parse_qs(urlsplit(response.value.url).query)['id'][0]
                return settle(ident)

            def tool(arguments, kind='shell'):
                page.goto(files_url); form = page.locator('form[action$="/content/tool"]')
                ident = form.locator('[name=id]').input_value()
                form.locator('[name=tool]').select_option(kind); form.locator('[name=arguments]').fill(arguments)
                with page.expect_navigation(wait_until='domcontentloaded'): form.locator('button').click()
                return settle(ident)

            if php:
                raw = archive({'index.php': (Path(__file__).parent/'fixtures/import-app.php').read_bytes(),
                    'sites/default/settings.php': "<?php return array('label' => 'before configuration');\n",
                    'sites/default/files/restored.txt': FILE,
                    'sites/default/files/blocked.php': '<?php echo "MUST-NOT-EXECUTE";',
                    'private.module': 'MUST-NOT-SERVE', 'index.php.save': 'MUST-NOT-SERVE'})
                upload('extract', 'legacy-site.tar.gz', raw)
                if not result.get('sql_imported'):
                    sql = (INPUT/(name+'.sql')).read_bytes()
                    assert hashlib.sha256(sql).hexdigest() == sources['sources'][name]['sha256']
                    upload('sql', name+'.sql', sql)
                    result['sql_imported'] = True; result['source'] = sources['sources'][name]; save()
                page.goto(files_url+'?path=sites/default/settings.php&edit=true')
                page.locator('textarea[name=text]').fill("<?php return array('label' => 'imported and configured');\n")
                with page.expect_response(lambda r: '/content/upload?' in r.url and r.request.method == 'POST') as response:
                    with page.expect_navigation(wait_until='domcontentloaded'): page.get_by_role('button', name='Save file').click()
                ident = parse_qs(urlsplit(response.value.url).query)['id'][0]
                settle(ident)
                if php == '7.0':
                    form = page.locator('form[action$="/web-settings"]'); ident = form.locator('[name=id]').input_value()
                    form.locator('[name=profile]').select_option('drupal7')
                    with page.expect_navigation(wait_until='domcontentloaded'): form.locator('button').click()
                    settle(ident)
                upload('upload', 'panel-upload.txt', FILE, 'sites/default/files/panel-upload.txt')
                tool("cp sites/default/files/panel-upload.txt sites/default/files/cli-upload.txt")
                cli = json.loads(tool('index.php', 'php'))
                assert cli['rows'] == ROWS and cli['export_sha256'] == HASH and cli['uid'] == site['uid']
                # Browser fetch sends a real multipart upload through verified Caddy HTTPS.
                page.goto('https://'+site['domain']+'/')
                web = page.evaluate('''async value => {
                    const data = new FormData(); data.append('upload', new File([value], 'web-upload.txt'));
                    const response = await fetch('/', {method:'POST',body:data});
                    if (!response.ok) throw new Error('Application upload failed: '+response.status);
                    return response.json();
                }''', FILE.decode())
                expected_file_hash = hashlib.sha256(FILE).hexdigest()
                assert len(web['files']) == 4
                assert all(f['uid'] == site['uid'] and f['sha256'] == expected_file_hash for f in web['files'].values())
                for domain in site['domains']:
                    response = page.goto('https://'+domain+'/node/42?value=a%20b')
                    assert response.status == 200
                    proof = response.json()
                    assert proof['rows'] == ROWS and proof['export_sha256'] == HASH
                    assert proof['uid'] == site['uid'] and proof['https'] == 'on'
                    assert proof['label'] == 'imported and configured' and proof['query']['value'] == 'a b'
                    if php == '7.0': assert proof['query']['q'] == '/node/42'
                paths = ['/sites/default/files/blocked.php', '/index.php.save']
                if php == '7.0': paths.append('/private.module')
                for path in paths:
                    response = page.goto('https://'+site['domain']+path)
                    assert response.status == 404 and 'MUST-NOT-' not in response.text(), path
                result.update({'rows_compared': 200, 'export_sha256': HASH, 'application_uploads': web['files'],
                    'domains': site['domains'], 'clean_urls': True, 'protected_paths': paths,
                    'archive_sha256': hashlib.sha256(raw).hexdigest(), 'archive_source_uid': 1002})
            else:
                raw = archive({'index.html': '<h1>Synthetic static import home</h1>', 'planning.html': '<h1>Synthetic planning page</h1>',
                    '.htaccess': 'ErrorDocument 404 /index.html\n', 'asset.txt': FILE})
                upload('extract', 'static-site.tar.gz', raw)
                checks = {}
                for path in ('/', '/planning', '/planning/', '/missing', '/.htaccess'):
                    response = page.goto('https://'+site['domain']+path)
                    checks[path] = {'status': response.status, 'home_content': 'Synthetic static import home' in response.text()}
                assert checks['/']['status'] == checks['/planning']['status'] == 200
                assert checks['/.htaccess']['status'] == 404
                # Record missing features explicitly; this profile does not execute Apache rules.
                assert checks['/planning/']['status'] == checks['/missing']['status'] == 404
                assert not checks['/missing']['home_content']
                result['routing'] = checks
                result['gaps'] = ['No trailing-slash redirect for extensionless HTML', 'No configured home-page 404 body; .htaccess is ignored']
            page.goto('https://'+site['domain']+'/')
            page.screenshot(path=str(OUTPUT.parent/(name+'.png')), full_page=True)
            assert not errors, errors
            result['passed'] = True; result['checked_at'] = time.time(); save()
            print(json.dumps({'site': name, 'passed': True, 'rows': result.get('rows_compared'), 'gaps': result.get('gaps', [])}), flush=True)
        report['browser_errors'] = errors; save(); browser.close()


if __name__ == '__main__': main()
