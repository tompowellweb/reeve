"""The Recover page: what the repository, a folder or this server holds, and the recoveries in progress."""
import json

from fastapi import Request
from fastapi.responses import RedirectResponse


def routes(app, session, mutation, render, call):
    def page(request, error=''):
        try: status = call({'op': 'recover-status'})
        except (ValueError, OSError) as exc: status, error = {'scan': None, 'recoveries': []}, error or str(exc)
        try: destination = call({'op': 'backup-destination'})
        except (ValueError, OSError): destination = {'state': 'unknown'}
        return render(request, 'recover.html', status=status, destination=destination, error=error)

    @app.get('/recover')
    def recover_page(request: Request, error: str = ''):
        session(request)
        return page(request, error=error)

    @app.post('/recover/scan')
    async def recover_scan(request: Request):
        form = await mutation(request)
        try: call({'op': 'recover-scan', 'source': str(form.get('source', 'repository')), 'folder': str(form.get('folder', ''))})
        except (ValueError, OSError) as exc: return page(request, error=str(exc))
        return RedirectResponse('/recover', 303)

    @app.post('/recover/submit')
    async def recover_submit(request: Request):
        form = await mutation(request)
        items = []
        for key, value in form.multi_items() if hasattr(form, 'multi_items') else form.items():
            if key == 'include': items.append(str(value))
        picks = []
        for index in items:
            mode = str(form.get('as-' + index, 'new'))
            picks.append({'backup': str(form.get('from-' + index, '')), 'mode': mode,
                          'name': str(form.get('name-' + index, '')), 'domains': str(form.get('domains-' + index, '')),
                          'target': str(form.get('target-' + index, '')) if mode != 'new' else ''})
        try: call({'op': 'recover-submit', 'items': picks})
        except (ValueError, OSError) as exc: return page(request, error=str(exc))
        return RedirectResponse('/recover#progress', 303)

    @app.post('/recover/retry/{ident}')
    async def recover_retry(request: Request, ident: str):
        await mutation(request)
        try: call({'op': 'recover-retry', 'id': ident})
        except (ValueError, OSError) as exc: return page(request, error=str(exc))
        return RedirectResponse('/recover#progress', 303)

    @app.post('/recover/settings')
    async def recover_settings(request: Request):
        await mutation(request)
        try: call({'op': 'recover-settings'})
        except (ValueError, OSError) as exc: return page(request, error=str(exc))
        return RedirectResponse('/settings', 303)
