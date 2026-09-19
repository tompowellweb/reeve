"""The site Logs page."""
from fastapi import HTTPException, Request


def routes(app, session, render, call):
    @app.get('/sites/{name}/logs')
    def site_logs(request: Request, name: str, source: str = '', lines: str = '100', since: str = '', match: str = ''):
        session(request)
        row = next((x for x in call({'op': 'list'}) if x['name'] == name), None)
        if not row: raise HTTPException(404, 'Site not found')
        error = ''; logs = None
        try:
            logs = call({'op': 'site-logs', 'site_id': row['id'], 'source': source, 'lines': lines, 'since': since, 'match': match})
        except (ValueError, OSError) as exc:
            error = str(exc)
            try: logs = call({'op': 'site-logs', 'site_id': row['id'], 'source': '', 'lines': '100', 'since': '', 'match': ''})
            except (ValueError, OSError): logs = None
        return render(request, 'logs.html', site=row, logs=logs, error=error)
