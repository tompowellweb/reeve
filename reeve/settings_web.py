"""The Settings page: the server's own settings, one form per group, saved through the worker."""
from fastapi import Request
from fastapi.responses import RedirectResponse, PlainTextResponse

GROUP_TITLES = {'certificates': 'Certificates', 'mail': 'Outbound mail', 'profile': 'Server profile', 'backups': 'Backups', 'updates': 'PHP rebuilds'}


def routes(app, session, mutation, render, call):
    def requester(request):
        return request.client.host if request.client else ''

    def page(request, error='', saved=None, note='', secure_extra=None, retention_surplus=None, pending=None):
        try: settings = call({'op': 'settings'})
        except (ValueError, OSError) as exc: settings, error = None, error or str(exc)
        try: secure = call({'op': 'secure-status', 'requester': requester(request)})
        except (ValueError, OSError) as exc: secure, error = None, error or str(exc)
        return render(request, 'settings.html', settings=settings, titles=GROUP_TITLES, error=error, saved=saved, note=note, secure=secure, secure_extra=secure_extra or {},
                      retention_surplus=retention_surplus, pending=pending or {})

    @app.post('/settings/secure/{action}')
    async def secure_action(request: Request, action: str):
        form = await mutation(request)
        ops = {'enable': {'op': 'secure-enable'}, 'lockdown': {'op': 'secure-lockdown', 'requester': requester(request)},
               'confirm': {'op': 'secure-confirm', 'requester': requester(request)}, 'revert': {'op': 'secure-revert'}, 'disable': {'op': 'secure-disable'},
               'add-client': {'op': 'secure-add-client', 'name': str(form.get('name', ''))}, 'token': {'op': 'secure-token'}, 'close-unlock': {'op': 'secure-close-unlock'}}
        if action not in ops: return page(request, error='Unknown action')
        if action == 'disable' and str(form.get('confirm', '')) != 'disable': return page(request, error='Type disable to turn secure mode off')
        try: result = call(ops[action])
        except (ValueError, OSError) as exc: return page(request, error=str(exc))
        extra = {}
        if action == 'token': extra = {'token': result}
        if action in ('enable', 'add-client'):
            name = str(form.get('name', '')) if action == 'add-client' else 'first'
            try: extra = {'client': call({'op': 'secure-client', 'name': name})}
            except (ValueError, OSError) as exc: extra = {'client_error': str(exc)}
        if extra: return page(request, secure_extra=extra)
        return RedirectResponse('/settings#secure', 303)

    @app.get('/settings/secure/client/{name}.conf')
    def secure_client_download(request: Request, name: str):
        session(request)
        try: result = call({'op': 'secure-client', 'name': name})
        except (ValueError, OSError) as exc: return PlainTextResponse(str(exc), status_code=400)
        return PlainTextResponse(result['text'], headers={'Content-Disposition': f'attachment; filename="{name}.conf"'})

    @app.get('/settings')
    def settings_page(request: Request, saved: str = '', note: str = ''):
        session(request)
        return page(request, saved=saved or None, note=note)

    @app.post('/settings/{group}')
    async def settings_save(request: Request, group: str):
        form = await mutation(request)
        values = {k: str(v) for k, v in form.items() if k != 'csrf'}
        if group == 'backups' and 'existing' not in values:
            # Tighter counts remove backups that exist now: say how many and how much before doing it.
            try: found = call({'op': 'settings-surplus', 'values': values})
            except (ValueError, OSError) as exc: return page(request, error=str(exc))
            if found['local']['count'] or found['remote']['count']:
                return page(request, retention_surplus=found, pending=values)
            values['existing'] = 'remove'
        try: result = call({'op': 'settings-save', 'group': group, 'values': values})
        except (ValueError, OSError) as exc: return page(request, error=str(exc))
        from urllib.parse import urlencode
        return RedirectResponse('/settings?' + urlencode({'saved': group, 'note': result.get('note', '')}) + '#' + group, 303)
