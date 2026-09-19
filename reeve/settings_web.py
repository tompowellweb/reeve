"""The Settings page: the server's own settings, one form per group, saved through the worker."""
from fastapi import Request
from fastapi.responses import RedirectResponse

GROUP_TITLES = {'certificates': 'Certificates', 'mail': 'Outbound mail', 'profile': 'Server profile', 'backups': 'Backups', 'updates': 'PHP rebuilds'}


def routes(app, session, mutation, render, call):
    def page(request, error='', saved=None, note=''):
        try: settings = call({'op': 'settings'})
        except (ValueError, OSError) as exc: settings, error = None, error or str(exc)
        return render(request, 'settings.html', settings=settings, titles=GROUP_TITLES, error=error, saved=saved, note=note)

    @app.get('/settings')
    def settings_page(request: Request, saved: str = '', note: str = ''):
        session(request)
        return page(request, saved=saved or None, note=note)

    @app.post('/settings/{group}')
    async def settings_save(request: Request, group: str):
        form = await mutation(request)
        values = {k: str(v) for k, v in form.items() if k != 'csrf'}
        try: result = call({'op': 'settings-save', 'group': group, 'values': values})
        except (ValueError, OSError) as exc: return page(request, error=str(exc))
        from urllib.parse import urlencode
        return RedirectResponse('/settings?' + urlencode({'saved': group, 'note': result.get('note', '')}) + '#' + group, 303)
