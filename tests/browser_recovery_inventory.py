"""Review recovery alongside site state, on desktop and mobile."""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--password-file', required=True)
    parser.add_argument('--output', default='/var/lib/hosting-browser/results')
    args = parser.parse_args()
    out = Path(args.output); out.mkdir(exist_ok=True, parents=True)
    base = 'http://127.0.0.1:8088'; report = {'sites': {}, 'errors': []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
        context = browser.new_context(viewport={'width': 1365, 'height': 1000})
        page = context.new_page(); page.on('pageerror', lambda error: report['errors'].append(str(error)))
        page.goto(base + '/login')
        page.get_by_label('Password').fill(Path(args.password_file).read_text().strip())
        page.get_by_role('button', name='Sign in', exact=True).click(); page.wait_for_url(base + '/')
        for name in ('m24-wordpress', 'm25-static'):
            page.goto(base + '/sites/' + name)
            assert 'Coverage incomplete' in page.locator('#recovery').inner_text()
            button = page.get_by_role('button', name='Review recovery', exact=True)
            button.click()
            dialog = page.locator('#recovery-dialog'); assert dialog.is_visible()
            assert 'Persistent storage' in dialog.inner_text() and 'Unresolved checks' in dialog.inner_text()
            page.keyboard.press('Escape'); assert not dialog.is_visible()
            assert button.evaluate('(element) => element === document.activeElement')
            page.screenshot(path=str(out / (name + '-recovery.png')), full_page=True)
            button.click(); dialog.get_by_role('button', name='Refresh inventory', exact=True).click()
            page.wait_for_url(base + '/sites/' + name + '#recovery', timeout=120000)
            assert not page.locator('#recovery .error').count()
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.get_by_role('button', name='Review recovery', exact=True).click()
            assert dialog.evaluate('(element) => element.scrollWidth <= element.clientWidth')
            page.screenshot(path=str(out / (name + '-recovery-mobile.png')), full_page=True)
            page.keyboard.press('Escape'); page.set_viewport_size({'width': 1365, 'height': 1000})
            report['sites'][name] = {'overview_and_dialog': True, 'refresh': True, 'mobile_no_overflow': True,
                                   'escape_and_focus_return': True}
        assert not report['errors']; browser.close()
    (out / 'recovery-inventory-browser.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'browser': 'passed', 'sites': list(report['sites'])}))


if __name__ == '__main__': main()
