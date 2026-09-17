"""Deploy a supplied Compose package through the browser and prove the application with its own data.

Mode 2 acceptance: upload, deploy, wait, then log into the application over Caddy HTTPS with
credentials from its restored dump and look for records only that dump could have supplied.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--password-file', required=True)
    parser.add_argument('--package', required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--vm', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--service', required=True)
    parser.add_argument('--port', required=True)
    parser.add_argument('--login-path')
    parser.add_argument('--user-field')
    parser.add_argument('--pass-field')
    parser.add_argument('--app-user')
    parser.add_argument('--app-password-file')
    parser.add_argument('--pages', required=True, help='comma-separated paths to read after login')
    parser.add_argument('--expect', required=True, help='comma-separated strings only the dump could supply')
    parser.add_argument('--resume')
    parser.add_argument('--retry', action='store_true')
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args()
    out = Path(args.output); out.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = 'http://127.0.0.1:8088'
    report = {'name': args.name, 'domain': args.domain, 'target_host_preparation': False, 'errors': [], 'steps': []}
    def save(): (out / (args.name + '-browser.json')).write_text(json.dumps(report, indent=2) + '\n')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.browser, headless=True,
            args=['--host-resolver-rules=MAP ' + args.domain + ' ' + args.vm])
        # The separate native verifier checks the CA. This context exercises UI behaviour.
        context = browser.new_context(viewport={'width': 1365, 'height': 1000}, ignore_https_errors=True)
        page = context.new_page(); page.on('pageerror', lambda e: report['errors'].append(str(e)))
        page.goto(base + '/login'); page.get_by_label('Password').fill(Path(args.password_file).read_text().strip())
        page.get_by_role('button', name='Sign in', exact=True).click(); page.wait_for_url(base + '/')
        ident = args.resume
        if not ident:
            page.get_by_role('link', name='Import application', exact=True).click()
            for field, value in [('Site name', args.name), ('Domain', args.domain),
                                 ('HTTP service', args.service), ('Container HTTP port', args.port)]:
                page.get_by_label(field, exact=True).fill(value)
            page.get_by_label('Project archive', exact=True).set_input_files(args.package)
            page.get_by_role('button', name='Upload and review', exact=True).click()
            page.wait_for_url(base + '/imports/*', timeout=600000)
            ident = page.url.rsplit('/', 1)[1]
            report['id'] = ident; save(); print('Receipt ' + ident, flush=True)
            value = page.request.get(base + '/api/v1/imports/' + ident).json()
            assert value['state'] == 'reviewed', value.get('issues')
            assert value['sha256'] == hashlib.sha256(Path(args.package).read_bytes()).hexdigest()
            page.screenshot(path=str(out / (args.name + '-review.png')), full_page=True)
            page.get_by_role('button', name='Deploy application', exact=True).click()
            page.wait_for_url(base + '/sites/' + args.name)
        else:
            report['id'] = ident; page.goto(base + '/sites/' + args.name)
            if args.retry:
                page.get_by_role('button', name='Retry deployment', exact=True).click()
                page.wait_for_url(base + '/sites/' + args.name)
        report['archive_sha256'] = hashlib.sha256(Path(args.package).read_bytes()).hexdigest(); save()
        deadline = time.monotonic() + args.timeout; previous = None; started = time.time()
        intake = page.request.get(base + '/api/v1/imports/' + ident).status == 200
        while time.monotonic() < deadline:
            if intake:
                value = page.request.get(base + '/api/v1/imports/' + ident).json()
                operation = value.get('deployment')
            else:
                # A restored site has no intake receipt; read its state from the site page.
                page.goto(base + '/sites/' + args.name)
                heading = page.locator('#operation')
                operation = {'id': ident, 'name': args.name, 'state': heading.get_attribute('data-state'),
                             'step': heading.locator('p').first.inner_text().rsplit(' · ', 1)[-1], 'error': (page.locator('p.error').first.inner_text() if page.locator('p.error').count() else '')}
                value = {'deployed': operation['state'] == 'succeeded'}
            if operation != previous:
                print(json.dumps(operation), flush=True); previous = operation
                report['deployment'] = operation
                report['steps'].append({'at': round(time.time() - started), 'state': operation['state'], 'step': operation['step']})
                save()
            if operation and operation['state'] == 'succeeded': break
            if operation and operation['state'] in ('failed', 'recovery-needed'):
                page.reload(); page.screenshot(path=str(out / (args.name + '-failure.png')), full_page=True)
                raise AssertionError(operation['error'])
            page.wait_for_timeout(3000)
        else: raise AssertionError('Deployment did not finish within the acceptance timeout')
        report['deploy_seconds'] = round(time.time() - started); save()
        assert value['deployed']
        page.goto(base + '/sites/' + args.name)
        for width in (1365, 390):
            page.set_viewport_size({'width': width, 'height': 1000 if width == 1365 else 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(out / f'{args.name}-site-{width}.png'), full_page=True)
        report['desktop_mobile'] = True
        report['site_page_mentions_restore'] = 'Restored once from dumps/' in page.content()
        # Application proof over HTTPS: log in with the dump's own account and read its records.
        app = context.new_page(); app.on('pageerror', lambda e: report['errors'].append(str(e)))
        response = app.goto('https://' + args.domain + '/')
        report['front_status'] = response.status
        if args.login_path:
            app.goto('https://' + args.domain + args.login_path)
            app.locator('[name="' + args.user_field + '"]').fill(args.app_user)
            app.locator('[name="' + args.pass_field + '"]').fill(Path(args.app_password_file).read_text().strip())
            app.locator('[name="' + args.pass_field + '"]').press('Enter')
            app.wait_for_load_state('networkidle')
            report['after_login_url'] = app.url; save()
        found = {}
        for path in args.pages.split(','):
            app.goto('https://' + args.domain + path); app.wait_for_load_state('networkidle')
            text = app.content()
            found[path] = [s for s in args.expect.split(',') if s in text]
            report['records_found'] = found; save()
            try: app.screenshot(path=str(out / (args.name + '-app' + path.replace('/', '_').replace('?', '_') + '.png')))
            except Exception as exc: report.setdefault('screenshot_skipped', []).append(path + ': ' + str(exc).splitlines()[0])
        assert any(found.values()), 'No dump-supplied record was visible after login: ' + json.dumps(found)
        assert not report['errors']; report['application_https'] = True
        save(); browser.close()
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__': main()
