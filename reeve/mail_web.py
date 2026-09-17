"""The mail page: relay state, the queue and per-site sending counts."""
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse


def routes(app, session, mutation, render, call):
    @app.get('/mail')
    def mail_page(request: Request, error: str = ''):
        session(request)
        return render(request, 'mail.html', mail=call({'op': 'mail-status'}), error=error)

    @app.post('/mail/flush')
    async def mail_flush(request: Request):
        await mutation(request)
        try: call({'op': 'mail-flush'})
        except (ValueError, OSError) as exc: raise HTTPException(400, str(exc)) from None
        return RedirectResponse('/mail', 303)

    @app.post('/mail/delete')
    async def mail_delete(request: Request):
        form = await mutation(request)
        try: call({'op': 'mail-delete', 'id': str(form.get('id', ''))})
        except (ValueError, OSError) as exc: raise HTTPException(400, str(exc)) from None
        return RedirectResponse('/mail', 303)
