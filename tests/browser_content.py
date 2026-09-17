"""Normal browser content workflow on disposable VM fixtures; never log credentials."""
import argparse
import io
import json
import time
import zipfile
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--password-file', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--php', action='store_true')
    parser.add_argument('--legacy', action='store_true')
    parser.add_argument('--database', action='store_true')
    parser.add_argument('--composer', action='store_true')
    parser.add_argument('--tar-only', action='store_true')
    parser.add_argument('--output', default='/var/lib/hosting-browser/results')
    args = parser.parse_args()
    output = Path(args.output); output.mkdir(exist_ok=True)
    base = 'http://127.0.0.1:8088'; url = base + '/sites/' + args.name + '/files'
    records = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
            args=['--host-resolver-rules=MAP *.hosting.test 127.0.0.1'])
        context = browser.new_context(viewport={'width': 1280, 'height': 1000})
        page = context.new_page(); errors = []
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.goto(base + '/login'); page.locator('[name=password]').fill(Path(args.password_file).read_text().strip())
        page.get_by_role('button', name='Sign in').click(); page.wait_for_url(base + '/')
        page.goto(url)
        assert page.get_by_role('heading', name='Files and tools').is_visible()

        def settle(kind, expected='succeeded'):
            # The operation was durably acknowledged before navigation. Poll from
            # a blank page to avoid racing the normal progress reload.
            page.goto('about:blank')
            until = time.monotonic() + 600
            while time.monotonic() < until:
                response = context.request.get(base + '/sites/' + args.name + '/content/status', timeout=30000)
                assert response.ok
                jobs = response.json()
                if jobs and jobs[0]['state'] not in ('queued', 'running'):
                    job = jobs[0]
                    assert job['kind'] == kind
                    result = context.request.get(base + '/sites/' + args.name + '/content/output/' + job['id'])
                    # Read text only through authenticated UI; synthetic fixture output.
                    page.goto(base + '/sites/' + args.name + '/content/output/' + job['id'])
                    text = page.locator('pre').inner_text()
                    assert job['state'] == expected, (job['state'], text)
                    records.append({'id': job['id'], 'kind': kind, 'state': job['state'], 'output': text[:2000]})
                    page.goto(url)
                    return text
                time.sleep(2)
            raise AssertionError('Content operation timeout')

        def upload(kind, filename, raw, path='.', expected='succeeded'):
            form = page.locator('form.content-upload').filter(has=page.locator('select[name=kind]')) if kind != 'sql' else page.locator('form.content-upload').filter(has=page.locator('input[value=sql]'))
            if kind != 'sql':
                form.locator('[name=kind]').select_option(kind)
                form.locator('[name=path]').fill(path)
                form.locator('[name=replace]').check()
            else: form.locator('[name=confirm]').check()
            form.locator('[name=file]').set_input_files({'name': filename, 'mimeType': 'application/octet-stream', 'buffer': raw})
            with page.expect_navigation(wait_until='domcontentloaded', timeout=120000): form.locator('button').click()
            return settle(kind, expected)

        if args.tar_only:
            import tarfile
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode='w:gz') as tar:
                root = tarfile.TarInfo('.'); root.type = tarfile.DIRTYPE; tar.addfile(root)
                member = tarfile.TarInfo('./m23-tar.txt'); body = b'Tar archive uploaded through the panel\n'; member.size = len(body)
                tar.addfile(member, io.BytesIO(body))
            upload('extract', 'site.tar.gz', raw.getvalue())
            page.goto(url + '?path=m23-tar.txt&edit=true')
            assert page.locator('[name=text]').input_value() == body.decode()
            assert not errors
            (output / (args.name + '-tar-content.json')).write_text(json.dumps({'site': args.name, 'operations': records, 'browser_errors': errors}, indent=2))
            print(json.dumps({'site': args.name, 'tar': 'passed', 'browser_errors': errors}))
            browser.close(); return

        archive = io.BytesIO()
        probe = b'''<?php
$config = require __DIR__.'/config.php';
file_put_contents(__DIR__.'/written-by-web.txt', 'web write');
header('Content-Type: application/json');
echo json_encode(array('message'=>$config['message'], 'owner'=>fileowner(__FILE__), 'write_owner'=>fileowner(__DIR__.'/written-by-web.txt'), 'php'=>PHP_VERSION));
'''
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('m23-demo/readme.txt', 'Uploaded through the panel\n')
            if args.php:
                z.writestr('m23-demo/probe.php', probe)
                z.writestr('m23-demo/config.php', "<?php return array('message'=>'before edit');\n")
                z.writestr('m23-demo/composer.json', json.dumps({'name': 'hosting/acceptance', 'require': {'symfony/console': '^3.4' if args.legacy else '^7.3'}}))
                z.writestr('m23-demo/bin/console', "<?php require __DIR__.'/../vendor/autoload.php'; $app = new Symfony\\Component\\Console\\Application('Hosting acceptance', '1.0'); $app->run();\n")
        upload('extract', 'site.zip', archive.getvalue())
        upload('upload', 'extra.txt', b'Additional uploaded content\n', 'm23-demo/extra.txt')
        # Application config or ordinary static text, edited in the real textarea.
        path = 'm23-demo/config.php' if args.php else 'm23-demo/readme.txt'
        page.goto(url + '?path=' + path + '&edit=true')
        text = "<?php return array('message'=>'after edit');\n" if args.php else 'Edited in the panel\n'
        page.locator('textarea[name=text]').fill(text)
        with page.expect_navigation(wait_until='domcontentloaded'): page.get_by_role('button', name='Save file').click()
        settle('edit')
        site = next(row for row in context.request.get(base + '/api/sites').json() if row['name'] == args.name)
        response = page.goto('https://' + site['domain'] + '/m23-demo/' + ('probe.php' if args.php else 'readme.txt'))
        assert response.ok
        if args.php:
            proof = response.json()
            assert proof['message'] == 'after edit' and proof['owner'] == proof['write_owner'] == site['uid']
            records.append({'web': proof})
        else: assert response.text() == 'Edited in the panel\n'
        page.goto(url)

        def tool(name, arguments, internet=False, directory='m23-demo', expected='succeeded'):
            form = page.locator('form[action$="/content/tool"]')
            form.locator('[name=tool]').select_option(name)
            form.locator('[name=arguments]').fill(arguments)
            form.locator('[name=path]').fill(directory)
            if internet: form.locator('[name=internet]').check()
            with page.expect_navigation(wait_until='domcontentloaded'): form.locator('button').click()
            return settle('tool', expected)

        result = tool('shell', 'id -u; printf cli-write > written-by-cli.txt; cat written-by-cli.txt; test ! -e /run/docker.sock; test ! -e /srv/sites')
        assert str(site['uid']) in result and 'cli-write' in result
        if args.php:
            assert 'PHP ' in tool('php', '-v')
        if args.database:
            upload('sql', 'synthetic.sql', b'CREATE TABLE IF NOT EXISTS m23_import (id INTEGER PRIMARY KEY, value VARCHAR(30)); DELETE FROM m23_import; INSERT INTO m23_import VALUES (1, \'uploaded SQL\'), (2, \'second row\'); SELECT COUNT(*) FROM m23_import;\n')
            # Demonstrate DB credentials in the matching PHP CLI, without exposing them.
            php = '''$e=getenv('DATABASE_ENGINE');$d=$e==='postgres'?'pgsql':'mysql';$p=new PDO($d.':host=db;dbname=site',getenv('DATABASE_USER'),getenv('DATABASE_PASSWORD'));echo $p->query('SELECT COUNT(*) FROM m23_import')->fetchColumn();'''
            import shlex
            assert tool('php', '-r ' + shlex.quote(php)).strip() == '2'
        if args.composer:
            assert 'Composer version' in tool('composer', '--version')
            tool('composer', 'install --prefer-dist --no-progress', internet=True)
            assert 'Hosting acceptance' in tool('console', '--version')
            assert 'console' in tool('shell', 'test -r vendor/autoload.php && ls vendor/symfony')
        # A rejected traversal archive must not write even its valid first member.
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, 'w') as z:
            z.writestr('m23-demo/must-not-exist', 'bad')
            z.writestr('../escaped', 'bad')
        assert 'Unsafe' in upload('extract', 'unsafe.zip', bad.getvalue(), expected='failed')
        tool('shell', 'test ! -e must-not-exist && test ! -e /escaped && printf traversal-rejected')
        page.screenshot(path=str(output / (args.name + '-content.png')), full_page=True)
        assert not errors, errors
        report = {'site': args.name, 'uid': site['uid'], 'operations': records, 'browser_errors': errors}
        (output / (args.name + '-content.json')).write_text(json.dumps(report, indent=2))
        print(json.dumps({'site': args.name, 'checks': len(records), 'browser_errors': errors}))
        browser.close()


if __name__ == '__main__': main()
