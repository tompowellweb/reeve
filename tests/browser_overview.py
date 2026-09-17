"""Site overview UX acceptance. Only m1-alpha receives disposable content/paused tasks."""
import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def run(password, base='http://127.0.0.1:8088', executable='/usr/bin/chromium', output='/var/lib/hosting-browser/results'):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    report = {'pages': {}, 'checks': []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, headless=True)
        context = browser.new_context(viewport={'width': 1365, 'height': 1000})
        page = context.new_page(); errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(base+'/login'); page.get_by_label('Password').fill(password)
        page.get_by_role('button', name='Sign in', exact=True).click(); page.wait_for_url(base+'/')
        for name in ('m24-wordpress', 'm25-static'):
            page.goto(base+'/sites/'+name)
            assert page.get_by_role('heading', name='Services', exact=True).is_visible()
            assert page.get_by_role('heading', name='Scheduled tasks', exact=False).is_visible()
            assert not page.get_by_role('heading', name='Setup: succeeded').count()
            if name == 'm24-wordpress':
                summary = page.locator('#scheduled-tasks').inner_text()
                assert 'wp cron event run --due-now' in summary and 'Every 1 minute' in summary
                assert 'Enabled' in summary and 'Latest run' in summary
                assert page.get_by_role('button', name='Change PHP', exact=True).is_visible()
                assert page.get_by_role('button', name='Show database credentials', exact=True).is_visible()
            else:
                assert 'No scheduled tasks.' in page.locator('#scheduled-tasks').inner_text()
                assert not page.locator('#php-dialog').count()
                assert not page.locator('#routing-dialog').count()
            # Every available overview modal opens in one click; Escape restores focus.
            for trigger in page.locator('[data-dialog]').all():
                if trigger.is_disabled(): continue
                ident = trigger.get_attribute('data-dialog')
                trigger.click(); dialog = page.locator('#'+ident)
                assert dialog.is_visible() and dialog.evaluate('(d) => d.open')
                page.keyboard.press('Escape'); assert not dialog.is_visible()
                assert trigger.evaluate('(b) => b === document.activeElement')
            page.screenshot(path=str(output/('overview-'+name+'.png')), full_page=True)
            report['pages'][name] = {'height': page.evaluate('document.documentElement.scrollHeight'),
                'width': 1365, 'headings': page.locator('.site-overview h2').all_text_contents(), 'dialogs': page.locator('dialog').count()}
            for suffix in ('/files', '/schedules', '/toolbox'):
                page.goto(base+'/sites/'+name+suffix)
                page.get_by_role('navigation', name='Site navigation').get_by_role('link', name='Overview', exact=True).click()
                page.wait_for_url(base+'/sites/'+name)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), name
            page.get_by_role('button', name='Add task', exact=True).click()
            dialog = page.locator('#schedule-new'); assert dialog.is_visible()
            assert dialog.evaluate('(d) => d.getBoundingClientRect().left >= 0 && d.getBoundingClientRect().right <= innerWidth')
            if name == 'm25-static':
                assert dialog.locator('[name=tool] option').all_text_contents() == ['Shell']
            page.screenshot(path=str(output/('overview-'+name+'-mobile-dialog.png')))
            page.keyboard.press('Escape'); page.set_viewport_size({'width': 1365, 'height': 1000})
        report['checks'].extend(['Both real site overviews', 'All modal open/Escape/focus return', 'Flat site navigation', '390px mobile without overflow', 'Static shell-only task form'])

        # Mutations stay on a disposable M1 fixture; WordPress schedule is only inspected.
        name = 'm1-alpha'; url = base+'/sites/'+name
        page.goto(url); page.get_by_role('button', name='Add task', exact=True).click()
        dialog = page.locator('#schedule-new'); form = dialog.locator('form')
        form.locator('[name=name]').fill('overview-check'); form.locator('[name=interval]').fill('15')
        form.locator('[name=arguments]').fill('printf overview-schedule'); form.locator('[name=enabled]').uncheck()
        with page.expect_navigation(wait_until='domcontentloaded'): form.get_by_role('button', name='Save schedule').click()
        assert page.url == url
        assert 'overview-check' in page.locator('#scheduled-tasks').inner_text()
        page.get_by_role('button', name='Edit task overview-check').click()
        dialog = page.locator('dialog[open]'); form = dialog.locator('form')
        assert form.locator('[name=name]').get_attribute('readonly') is not None
        form.locator('[name=interval]').fill('30')
        # Passing the refresh interval must not discard an open edit.
        page.clock.install(); page.clock.fast_forward(31000)
        assert dialog.is_visible() and form.locator('[name=interval]').input_value() == '30'
        with page.expect_navigation(wait_until='domcontentloaded'): form.get_by_role('button', name='Save schedule').click()
        assert page.url == url and 'Every 30 minutes' in page.locator('#scheduled-tasks').inner_text()
        assert 'Paused' in page.locator('#scheduled-tasks').inner_text()

        def finish():
            page.goto('about:blank')
            deadline = time.monotonic()+180
            while time.monotonic() < deadline:
                jobs = context.request.get(base+'/sites/'+name+'/content/status').json()
                if jobs and jobs[0]['state'] not in ('queued','running'):
                    assert jobs[0]['state'] == 'succeeded', jobs[0]
                    page.goto(url); return jobs[0]
                time.sleep(1)
            raise AssertionError('Operation did not finish')

        page.get_by_role('button', name='Upload', exact=True).click()
        form = page.locator('#upload-dialog form')
        form.locator('[name=path]').fill('overview-check.txt'); form.locator('[name=replace]').check()
        form.locator('[name=file]').set_input_files({'name':'overview-check.txt','mimeType':'text/plain','buffer':b'overview-upload-proof\n'})
        with page.expect_navigation(wait_until='domcontentloaded'): form.get_by_role('button', name='Upload', exact=True).click()
        assert page.url == url; upload = finish()
        page.get_by_role('button', name='Run command', exact=True).click(); form = page.locator('#command-dialog form')
        form.locator('[name=arguments]').fill('cat overview-check.txt')
        with page.expect_navigation(wait_until='domcontentloaded'): form.get_by_role('button', name='Run tool').click()
        assert page.url == url; tool = finish()
        page.locator('#recent-activity a[href$="'+tool['id']+'"]').click()
        assert 'overview-upload-proof' in page.locator('pre').inner_text()
        page.get_by_role('navigation', name='Site navigation').get_by_role('link', name='Overview', exact=True).click()
        assert page.url == url
        report['checks'].extend(['Paused task create/edit returns to overview', 'Refresh preserves open editor', 'Upload/command dialog return to overview', 'Operation output one level below overview'])
        report['fixture'] = {'site': name, 'schedule': 'overview-check', 'schedule_enabled': False, 'upload': upload['id'], 'tool': tool['id']}
        assert not errors, errors
        report['browser_errors'] = errors; report['checked_at'] = time.time()
        (output/'overview-acceptance.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report)); browser.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--password-file', required=True); p.add_argument('--base', default='http://127.0.0.1:8088')
    p.add_argument('--executable', default='/usr/bin/chromium'); p.add_argument('--output', default='/var/lib/hosting-browser/results')
    args = p.parse_args(); run(Path(args.password_file).read_text().strip(), args.base, args.executable, args.output)
