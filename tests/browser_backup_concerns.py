"""Current replacement for the retired recovery-plan modal browser acceptance."""
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
    report = {'errors': [], 'sites': {}}
    base = 'http://127.0.0.1:8088'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
        page = browser.new_page(viewport={'width': 1365, 'height': 1000})
        page.on('pageerror', lambda error: report['errors'].append(str(error)))
        page.goto(base + '/login')
        page.get_by_label('Password').fill(Path(args.password_file).read_text().strip())
        page.get_by_role('button', name='Sign in', exact=True).click(); page.wait_for_url(base + '/')
        for name in ('m24-wordpress', 'm25-static'):
            page.goto(base + '/sites/' + name)
            section = page.locator('#recovery')
            assert section.locator('th[scope=row]').count() == 5
            assert not page.locator('select[name^=storage_class_], #recovery-plan-dialog').count()
            expected = 'Partially protected' if name == 'm24-wordpress' else 'Not yet protected'
            assert expected in section.inner_text()
            for width in (1365, 390):
                page.set_viewport_size({'width': width, 'height': 1000 if width == 1365 else 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                section.screenshot(path=str(out / f'{name}-backup-concerns-{width}.png'))
                button = section.get_by_role('button', name='Review', exact=True)
                button.focus(); page.keyboard.press('Enter')
                assert page.locator('#external-detail').is_visible()
                assert not page.locator('dialog[open]').count()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if name == 'm25-static':
                    page.locator('#external-detail').screenshot(path=str(out / f'backup-question-{width}.png'))
                button.click(); assert not page.locator('#external-detail').is_visible()
            report['sites'][name] = {'five_concerns': True, 'desktop_mobile': True, 'inline_keyboard_action': True, 'protection': expected}
        page.set_viewport_size({'width': 1365, 'height': 1000})
        page.goto(base + '/sites/m25-static')
        page.get_by_role('button', name='Review', exact=True).click()
        detail = page.locator('#external-detail')
        original = {'external': detail.locator('[name=external]:checked').input_value(), 'notes': detail.locator('[name=notes]').input_value()}
        detail.get_by_label('Yes', exact=True).check()
        detail.locator('[name=notes]').fill('Synthetic acceptance: photos held by another service.')
        page.evaluate('window.acceptanceEditor = true')
        page.wait_for_timeout(31000)
        assert page.evaluate('window.acceptanceEditor === true')
        detail.get_by_role('button', name='Save answer', exact=True).click()
        page.wait_for_url(base + '/sites/m25-static#recovery')
        assert 'Other services need to be covered.' in page.locator('#recovery').inner_text()
        page.get_by_role('button', name='Review', exact=True).click()
        detail.locator('[name=revision]').evaluate('(e) => e.value = "outdated"')
        detail.locator('[name=notes]').fill('Retain this unsaved text.')
        detail.get_by_role('button', name='Save answer', exact=True).click()
        page.wait_for_selector('#external-detail:not([hidden])')
        assert 'changed in another window' in page.locator('main').inner_text()
        assert detail.locator('[name=notes]').input_value() == 'Retain this unsaved text.'
        detail.locator('[name=external][value="' + original['external'] + '"]').check()
        detail.locator('[name=notes]').fill(original['notes'])
        detail.get_by_role('button', name='Save answer', exact=True).click()
        page.wait_for_url(base + '/sites/m25-static#recovery')
        page.locator('[data-concern-toggle=restore-detail]').click()
        checks = page.locator('#restore-detail')
        original_checks = checks.locator('[name=checks]').input_value()
        checks.locator('[name=checks]').fill('Synthetic acceptance: open an uploaded photo.')
        checks.get_by_role('button', name='Save checks', exact=True).click()
        page.wait_for_url(base + '/sites/m25-static#recovery')
        assert 'Not tested for the whole site.' in page.locator('#recovery').inner_text()
        assert 'Not yet protected' in page.locator('#recovery').inner_text()
        page.locator('[data-concern-toggle=restore-detail]').click()
        checks.locator('[name=checks]').fill(original_checks)
        checks.get_by_role('button', name='Save checks', exact=True).click()
        page.wait_for_url(base + '/sites/m25-static#recovery')
        report.update(owner_notes_saved_and_restored=True, stale_form_retains_text_inline=True,
                      open_editor_survives_refresh=True, notes_never_mark_restore_tested=True)
        assert not report['errors']; browser.close()
    (out / 'backup-concerns-browser.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__': main()
