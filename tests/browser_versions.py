"""Refresh, PHP switch and exact-image rollback on a disposable site."""
import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--password-file', required=True)
    parser.add_argument('--output', default='/var/lib/hosting-browser/results')
    args = parser.parse_args()
    base = 'http://127.0.0.1:8088'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path='/usr/bin/chromium',
            args=['--host-resolver-rules=MAP *.hosting.test 127.0.0.1', '--no-proxy-server'])
        context = browser.new_context(viewport={'width': 1280, 'height': 1000})
        page = context.new_page()
        page.goto(base)
        page.get_by_label('Password').fill(Path(args.password_file).read_text().strip())
        page.get_by_role('button', name='Sign in', exact=True).click()
        page.wait_for_url(base + '/')
        page.goto(base + '/versions')
        assert '8.2' in page.locator('table').inner_text() and '8.3' in page.locator('table').inner_text()
        page.get_by_role('button', name='Check for new versions', exact=True).click()
        for _ in range(600):
            page.goto(base + '/versions')
            state = page.locator('#catalogue-operation').get_attribute('data-state')
            if state == 'succeeded':
                break
            assert state != 'recovery-needed', page.locator('body').inner_text()
            time.sleep(1)
        assert state == 'succeeded'
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / 'm2-version-catalogue.png'), full_page=True)

        def row():
            return next(r for r in page.request.get(base + '/api/sites').json() if r['name'] == 'm2-versions')

        original = row()
        assert original['php_branch'] == '8.2'
        page.goto(base + '/sites/m2-versions')
        page.get_by_role('button', name='Change PHP', exact=True).click()
        page.get_by_label('Target PHP branch', exact=True).select_option('8.3')
        page.get_by_role('button', name='Switch PHP', exact=True).click()
        page.close()
        page = context.new_page()
        for _ in range(900):
            current = row()
            if current['runtime_job'] and current['runtime_job']['state'] == 'succeeded':
                break
            assert not current['runtime_job'] or current['runtime_job']['state'] != 'recovery-needed', current
            time.sleep(1)
        assert current['php_branch'] == '8.3' and current['health']['php_version'].startswith('8.3.')
        switched = current
        probe = context.new_page()
        for domain in current['domains']:
            assert probe.goto('https://' + domain + '/version-proof.php').status == 200
            assert json.loads(probe.locator('body').inner_text())['version'].startswith('8.3.')
        page.goto(base + '/sites/m2-versions')
        page.get_by_role('button', name='Change PHP', exact=True).click()
        page.screenshot(path=str(output / 'm2-php-switch.png'), full_page=True)
        page.get_by_role('button', name='Restore previous PHP runtime', exact=True).click()
        for _ in range(120):
            current = row()
            if current['runtime_job']['id'] != switched['runtime_job']['id'] and current['runtime_job']['state'] == 'succeeded':
                break
            assert current['runtime_job']['state'] != 'recovery-needed', current
            time.sleep(1)
        assert current['php_branch'] == '8.2' and current['health']['php_version'].startswith('8.2.')
        assert current['uid'] == original['uid'] and current['domains'] == original['domains']
        result = {'original': original, 'switched': switched, 'rolled_back': current}
        (output / 'm2-version-browser.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        browser.close()


if __name__ == '__main__':
    main()
