import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--password-file', required=True)
    parser.add_argument('--output', default='/var/lib/hosting-browser/results'); args = parser.parse_args()
    out = Path(args.output); out.mkdir(exist_ok=True, parents=True)
    report = {'errors': [], 'configured_destination': False, 'desktop_mobile': True}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
        page = browser.new_page(viewport={'width': 1365, 'height': 1000})
        page.on('pageerror', lambda error: report['errors'].append(str(error)))
        page.goto('http://127.0.0.1:8088/login'); page.get_by_label('Password').fill(Path(args.password_file).read_text().strip())
        page.get_by_role('button', name='Sign in', exact=True).click(); page.wait_for_url('http://127.0.0.1:8088/')
        for width in (1365, 390):
            page.set_viewport_size({'width': width, 'height': 1000 if width == 1365 else 844})
            page.goto('http://127.0.0.1:8088/sites/m25-postgres')
            page.get_by_role('link', name='Destination settings', exact=True).click()
            dialog = page.locator('main'); assert dialog.is_visible()
            assert 'SFTP server or Amazon S3 bucket' in dialog.inner_text()
            assert not dialog.get_by_role('button', name='Copy waiting databases').count()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert dialog.evaluate('(e) => e.scrollWidth <= e.clientWidth')
            page.screenshot(path=str(out / f'remote-backups-{width}.png'))
            assert page.url.endswith('/backups')
        assert not report['errors']; browser.close()
    (out / 'remote-backups-browser.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__': main()
