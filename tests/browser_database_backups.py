import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--password-file', required=True)
    parser.add_argument('--output', default='/var/lib/hosting-browser/results'); args = parser.parse_args()
    out = Path(args.output); out.mkdir(exist_ok=True, parents=True)
    base = 'http://127.0.0.1:8088'; report = {'errors': [], 'sites': {}}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
        page = browser.new_page(viewport={'width': 1365, 'height': 1000})
        page.on('pageerror', lambda error: report['errors'].append(str(error)))
        page.goto(base + '/login'); page.get_by_label('Password').fill(Path(args.password_file).read_text().strip())
        page.get_by_role('button', name='Sign in', exact=True).click(); page.wait_for_url(base + '/')
        for name in ('m25-postgres', 'm25-mysql'):
            page.goto(base + '/sites/' + name)
            button = page.get_by_role('button', name='Manage copies', exact=True); button.click()
            dialog = page.locator('#database-detail'); assert dialog.is_visible()
            button.click(); assert not dialog.is_visible()
            assert button.evaluate('(e) => e === document.activeElement')
            button.click(); dialog.get_by_role('button', name='Back up database now', exact=True).click()
            page.wait_for_url(base + '/sites/' + name + '#recovery')
            page.wait_for_selector('#backup-operation[data-state="succeeded"]', timeout=120000)
            assert 'Saved locally' in page.locator('#backup-operation').inner_text()
            button.click(); dialog.locator('select[name=interval]').select_option('60')
            dialog.get_by_role('button', name='Save frequency', exact=True).click()
            page.wait_for_url(base + '/sites/' + name + '#recovery')
            assert 'Every 60 minutes' in page.locator('#backup-operation').inner_text()
            button.click(); dialog.locator('select[name=interval]').select_option('15')
            dialog.get_by_role('button', name='Save frequency', exact=True).click()
            page.wait_for_url(base + '/sites/' + name + '#recovery')
            page.screenshot(path=str(out / (name + '-database-dumps.png')), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844}); button.click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert dialog.evaluate('(e) => e.scrollWidth <= e.clientWidth')
            page.screenshot(path=str(out / (name + '-database-dumps-mobile.png')))
            button.click(); page.set_viewport_size({'width': 1365, 'height': 1000})
            report['sites'][name] = {'manual_dump': True, 'schedule_edit': True, 'desktop_mobile': True, 'focus_return': True}
        for name in ('m25-static',):
            page.goto(base + '/sites/' + name)
            assert not page.get_by_role('button', name='Manage copies', exact=True).count()
            assert 'Not connected' in page.locator('#recovery').inner_text()
        assert not report['errors']; browser.close()
    (out / 'database-dumps-browser.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'browser': 'passed', 'sites': list(report['sites'])}))


if __name__ == '__main__': main()
