import json
import os
import secrets
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import Auth
from .core import DEFAULTS, validate_create
from .worker import rpc

BASE = Path(__file__).parent


def create_app(auth_path="/srv/ops/panel/web/auth.sqlite3", call=rpc, status_path=None, release_path="/srv/ops/panel/release.json"):
    from .server_status import STATUS, read as read_status
    status_path = status_path or STATUS
    def release():
        # Read per page: the installer writes the record after it has restarted this process.
        try: return json.loads(Path(release_path).read_text()) if Path(release_path).exists() else None
        except (OSError, ValueError): return None
    auth = Auth(auth_path)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")
    def timestamp(value):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(value, timezone.utc).strftime('%d %b %H:%M UTC') if value else 'Never'
    templates.env.filters['timestamp'] = timestamp
    def clock(value):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(value, timezone.utc).strftime('%H:%M UTC') if value else ''
    templates.env.filters['clock'] = clock
    def size(value):
        if value is None: return '–'
        # Binary units, named as such: the site page and the quota report speak MiB.
        for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
            if abs(value) < 1024 or unit == 'TiB': break
            value /= 1024
        return f'{value:.0f} {unit}' if unit in ('B', 'KiB') or value >= 100 else f'{value:.1f} {unit}'
    templates.env.filters['size'] = size
    templates.env.filters['number'] = lambda value: f'{int(value):,}' if value is not None else '–'

    @app.middleware("http")
    async def boundary(request, next_call):
        # Large raw bodies are accepted only by the authenticated streaming upload route.
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            from .content_jobs import UPLOAD_LIMIT
            import re
            from .backup_web import IMPORT_LIMIT
            maximum = IMPORT_LIMIT if re.fullmatch(r'/sites/[a-z0-9-]+/backups/import', request.url.path) else UPLOAD_LIMIT if (request.url.path == '/api/v1/imports' or re.fullmatch(r'/sites/[a-z0-9-]+/content/upload', request.url.path)) else (65536 if re.fullmatch(r'/sites/[a-z0-9-]+/toolbox/(save|start|stop)',request.url.path) else 69632 if re.fullmatch(r'/sites/[a-z0-9-]+/(rules|sftp)', request.url.path) else 16384 if request.url.path == '/backups/connect' else 8192)
            if not length.isdigit() or int(length) > maximum:
                return JSONResponse({"detail": "Form too large or missing length"}, status_code=413)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
        response = await next_call(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin", "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"})
        return response

    def session(request, required=True):
        found = auth.session(request.cookies.get("hosting_session"))
        if required and (not found or not found["authenticated"]):
            raise HTTPException(401, "Sign in to continue")
        return found

    async def mutation(request, required=True):
        found = session(request, required)
        form = await request.form()
        if not found or not secrets.compare_digest(str(form.get("csrf", "")), found["csrf"]):
            raise HTTPException(403, "Form expired. Reload the page and try again.")
        return form

    def cookie(response, token):
        # HTTP is confined to loopback inside the SSH tunnel. Public deployment requires HTTPS.
        response.set_cookie("hosting_session", token, httponly=True, samesite="strict", max_age=28800, path="/")

    def render(request, template, **values):
        summary = read_status(status_path) or {}
        return templates.TemplateResponse(request=request, name=template, context={"session": session(request, False), "release": release(), "hostname": summary.get('hostname'),
                                                                              "profile": summary.get('profile') or {'name': 'standard', 'database_usage': 'standard'}, **values})

    @app.get("/login")
    def login_page(request: Request):
        token, found = auth.new_session()
        response = templates.TemplateResponse(request=request, name="login.html", context={"session": found})
        cookie(response, token)
        return response

    @app.post("/login")
    async def login(request: Request):
        form = await mutation(request, required=False)
        error = auth.login(str(form.get("password", "")))
        if error:
            return render(request, "login.html", error=error)
        auth.logout(request.cookies["hosting_session"])
        token, _ = auth.new_session(authenticated=True)
        response = RedirectResponse("/", 303)
        cookie(response, token)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        await mutation(request)
        auth.logout(request.cookies["hosting_session"])
        response = RedirectResponse("/login", 303)
        response.delete_cookie("hosting_session")
        return response

    @app.get("/")
    def index(request: Request):
        found = session(request, False)
        if not found or not found["authenticated"]:
            return RedirectResponse("/login", 303)
        # The server summary is the worker's minute-by-minute file, readable while the worker is busy;
        # the live list refreshes each site's state when the worker answers.
        status = read_status(status_path) or {}
        figures = {site['id']: site for site in status.get('sites') or []}
        try:
            sites = [dict(figures.get(site['id'], {}), **site) for site in call({"op": "list"})]
            error = None
        except (ValueError, OSError) as exc:
            sites = [dict(site, health={'application': site['health']}, backup_summary='') for site in figures.values()]
            error = f"Worker busy or unavailable: {exc}. Sites and figures are from the last summary."
        for site in sites:
            usage = figures.get(site['id'], {})
            site['usage'] = {k: usage.get(k) for k in ('disk_used', 'disk_hard', 'memory', 'cpu', 'requests_24h', 'errors_24h', 'bytes_24h')}
        sites.sort(key=lambda site: site['name'])  # a server page lists its sites by name, not by creation
        return render(request, "index.html", sites=sites, status=status, error=error)

    def database_form(form):
        from .database_jobs import validate_database
        engine = str(form.get('db_engine', ''))
        spec = {'engine': engine, 'series': str(form.get('db_series_' + engine, '')), 'exact': str(form.get('db_exact', '')).strip()}
        if form.get('db_usage'): spec['usage'] = str(form['db_usage'])
        for key in ('memory_mb','cpus','layer_mb','pids_limit'):
            if form.get('db_' + key): spec[key] = float(form['db_' + key]) if key == 'cpus' else int(form['db_' + key])
        return validate_database(spec)

    @app.get('/databases/versions')
    def database_versions_page(request: Request):
        session(request)
        from datetime import datetime, timezone
        info = call({'op': 'database-versions'})
        checked = datetime.fromtimestamp(info['checked_at'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if info.get('checked_at') else 'Not checked'
        return render(request, 'database_versions.html', catalogue=info, checked=checked, ident=str(uuid.uuid4()))

    @app.post('/databases/refresh')
    async def database_refresh(request: Request):
        form = await mutation(request)
        call({'op': 'refresh-databases', 'id': form.get('id', '')})
        return RedirectResponse('/databases/versions', 303)

    @app.post('/sites/{name}/database')
    async def database_add(request: Request, name: str):
        form = await mutation(request)
        row = next((r for r in call({'op': 'list'}) if r['name'] == name), None)
        if not row: raise HTTPException(404, 'Site not found')
        try: call({'op': 'add-database', 'id': form.get('id', ''), 'site_id': row['id'], 'data': database_form(form)})
        except (ValueError, OSError) as exc: return render_site(request, row, error=str(exc))
        return RedirectResponse('/sites/' + name, 303)

    @app.post('/sites/{name}/database/credentials')
    async def database_credentials(request: Request, name: str):
        await mutation(request)
        row = next((r for r in call({'op': 'list'}) if r['name'] == name), None)
        if not row: raise HTTPException(404, 'Site not found')
        return render(request, 'database_credentials.html', site=row, credentials=call({'op': 'database-credentials', 'site_id': row['id']}))

    @app.post('/retry-database/{ident}')
    async def database_retry(request: Request, ident: str):
        await mutation(request)
        job = call({'op': 'retry-database', 'id': ident})
        if job['kind'] == 'refresh': return RedirectResponse('/databases/versions', 303)
        row = next(r for r in call({'op': 'list'}) if r['id'] == job['site_id'])
        return RedirectResponse('/sites/' + row['name'], 303)

    @app.get("/create")
    def create_page(request: Request):
        session(request)
        return render(request, "create.html", defaults=call({"op": "defaults"}), ident=str(uuid.uuid4()), values={}, php_choices=call({"op": "versions"})["choices"], db_catalogue=call({"op": "database-versions"}))

    @app.post("/create")
    async def create(request: Request):
        form = await mutation(request)
        try:
            defaults = call({"op": "defaults"})
            data = {**defaults, "name": form.get("name", ""), "domain": form.get("domain", "")}
            data["aliases"] = str(form.get("aliases", "")).split()
            data["runtime"] = form.get("runtime", "static")
            if data["runtime"] == "php":
                data["php_version"] = form.get("php_version", "")
            for key in DEFAULTS:
                if key != "data_mb" and not form.get(key):
                    data[key] = None
                elif form.get(key):
                    data[key] = float(form[key]) if key == "cpus" else int(form[key])
            if form.get("db_engine"):
                data["database"] = database_form(form)
            data = validate_create(data)
            job = call({"op": "create", "id": form.get("id", ""), "data": data})
        except (ValueError, OSError) as exc:
            return render(request, "create.html", defaults=locals().get("defaults", DEFAULTS), ident=form.get("id", str(uuid.uuid4())), values=form, php_choices=call({"op": "versions"})["choices"], db_catalogue=call({"op": "database-versions"}), error=str(exc))
        return RedirectResponse("/sites/" + job["name"], 303)

    def render_site(request, row, **extra):
        data = json.loads(row["payload"])
        try:
            recovery = call({'op': 'recovery-status', 'site_id': row['id']})
        except (ValueError, OSError):
            recovery = {'inventory': None, 'error': 'Recovery inventory is unavailable. Inspect again after checking the worker.'}
        extra['recovery'] = recovery
        try:
            extra['recovery_context'] = call({'op': 'recovery-context', 'site_id': row['id']})
        except (ValueError, OSError):
            extra['recovery_context'] = {'error': 'Site notes are unavailable. Reload before editing them.', 'external': 'unknown', 'revision': '', 'notes': '', 'checks': ''}
        try:
            extra['backup'] = call({'op': 'backup-status', 'site_id': row['id']})
        except (ValueError, OSError):
            extra['backup'] = {'supported': False, 'error': 'Database backup state is unavailable. Check worker and storage access.', 'remote': {}}
        from .recovery_context import summary
        extra['protection'] = summary(extra['backup'])
        extra['backup_ident'] = str(uuid.uuid4()); extra['delete_ident'] = str(uuid.uuid4()); extra['usage_ident'] = str(uuid.uuid4())
        try: extra['traffic'] = call({'op': 'site-traffic', 'site_id': row['id']})
        except (ValueError, OSError): extra['traffic'] = None
        try:
            extra['deletion'] = (call({'op': 'site-deletes', 'site_id': row['id']}) or [None])[0]
            extra['restore_job'] = (call({'op': 'site-restores', 'site_id': row['id']}) or [None])[0]
        except (ValueError, OSError): extra['deletion'] = extra['restore_job'] = None
        if data.get('runtime') == 'compose':
            return render(request, 'compose_site.html', site=row, data=data,
                project=call({'op': 'compose-site', 'site_id': row['id']}), domain_ident=str(uuid.uuid4()), **extra)
        if row.get("php_branch"):
            data["php_version"] = row["php_branch"]
        schedules, jobs, toolbox, web_settings, site_rules, php_settings, sftp, mail = [], [], None, None, None, None, None, None
        summary_error = ''
        if row['state'] == 'succeeded':
            try:
                schedules = call({'op': 'schedules', 'site_id': row['id']})
                jobs = call({'op': 'content-status', 'site_id': row['id']})
                toolbox = call({'op': 'toolbox-status', 'site_id': row['id']})
                summary = call({'op': 'content-site', 'name': row['name']})
                web_settings, site_rules, php_settings, sftp, mail = summary.get('web_settings'), summary.get('site_rules'), summary.get('php_settings'), summary.get('sftp'), summary.get('mail')
            except (ValueError, OSError) as exc:
                summary_error = 'Some site details could not be loaded: ' + str(exc)
        pending = any(j['state'] in ('queued', 'running', 'recovery-needed') for j in jobs)
        refresh_pending = any(j['state'] in ('queued', 'running') for j in jobs)
        busy = pending or bool(toolbox and toolbox['state'] in ('starting', 'active'))
        return render(request, "detail.html", site=row, data=data, database_ident=str(uuid.uuid4()), domain_ident=str(uuid.uuid4()),
            runtime_ident=str(uuid.uuid4()), content_ident=str(uuid.uuid4()), php_choices=call({"op": "versions"})["choices"],
            db_catalogue=call({"op": "database-versions"}), schedules=schedules, jobs=jobs, toolbox=toolbox,
            web_settings=web_settings, site_rules=site_rules, php_settings=php_settings, sftp=sftp, mail=mail, pending=pending, refresh_pending=refresh_pending, busy=busy, summary_error=summary_error, **extra)

    @app.get("/versions")
    def versions_page(request: Request):
        session(request)
        from datetime import datetime, timezone
        info = call({"op": "versions"})
        checked = datetime.fromtimestamp(info["checked_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if info.get("checked_at") else "Not checked"
        return render(request, "versions.html", versions=info, checked=checked, ident=str(uuid.uuid4()))

    @app.post("/versions/refresh")
    async def refresh_versions(request: Request):
        form = await mutation(request)
        call({"op": "refresh-versions", "id": form.get("id", "")})
        return RedirectResponse("/versions", 303)

    @app.post("/versions/rebuild")
    async def rebuild_php(request: Request):
        form = await mutation(request)
        call({"op": "php-rebuild", "id": form.get("id", "")})
        return RedirectResponse("/versions", 303)

    @app.post("/sites/{name}/php")
    async def switch_php(request: Request, name: str):
        form = await mutation(request)
        row = next((x for x in call({"op": "list"}) if x["name"] == name), None)
        if not row:
            raise HTTPException(404, "Site not found")
        try:
            if form.get("previous"):
                call({"op": "php-rollback", "id": form.get("id", ""), "site_id": row["id"], "previous": form["previous"]})
            else:
                call({"op": "php-switch", "id": form.get("id", ""), "site_id": row["id"], "branch": form.get("branch", "")})
        except (ValueError, OSError) as exc:
            return render_site(request, row, error=str(exc))
        return RedirectResponse("/sites/" + name, 303)

    @app.post("/retry-runtime/{ident}")
    async def retry_runtime(request: Request, ident: str):
        await mutation(request)
        job = call({"op": "retry-runtime", "id": ident})
        if job["kind"] == "refresh":
            return RedirectResponse("/versions", 303)
        row = next(x for x in call({"op": "list"}) if x["id"] == job["site_id"])
        return RedirectResponse("/sites/" + row["name"], 303)

    @app.get("/sites/{name}")
    def detail(request: Request, name: str):
        session(request)
        rows = call({"op": "list"})
        row = next((x for x in rows if x["name"] == name), None)
        if not row:
            raise HTTPException(404, "Site not found")
        return render_site(request, row)

    @app.post("/sites/{name}/domains")
    async def domains(request: Request, name: str):
        form = await mutation(request)
        row = next((x for x in call({"op": "list"}) if x["name"] == name), None)
        if not row:
            raise HTTPException(404, "Site not found")
        try:
            call({"op": "domains", "id": form.get("id", ""), "site_id": row["id"],
                  "domains": [str(form.get("domain", "")), *str(form.get("aliases", "")).split()]})
        except (ValueError, OSError) as exc:
            return render_site(request, row, error=str(exc))
        return RedirectResponse("/sites/" + name, 303)

    @app.post('/sites/{name}/recovery/inspect')
    async def inspect_recovery(request: Request, name: str):
        await mutation(request)
        row = next((x for x in call({'op': 'list'}) if x['name'] == name), None)
        if not row:
            raise HTTPException(404, 'Site not found')
        call({'op': 'recovery-inspect', 'site_id': row['id']})
        return RedirectResponse('/sites/' + name + '#recovery', 303)

    @app.post('/sites/{name}/recovery/context')
    async def save_recovery_context(request: Request, name: str):
        form = await mutation(request)
        row = next((x for x in call({'op': 'list'}) if x['name'] == name), None)
        if not row: raise HTTPException(404, 'Site not found')
        try:
            value = {'revision': str(form.get('revision', ''))}
            if form.get('section') == 'external':
                value.update(external=str(form.get('external', 'unknown')), notes=str(form.get('notes', '')))
            elif form.get('section') == 'checks': value['checks'] = str(form.get('checks', ''))
            else: raise ValueError('Unknown site notes section.')
            call({'op': 'recovery-context-save', 'site_id': row['id'], 'data': value})
        except (ValueError, OSError) as exc:
            return render_site(request, row, error=str(exc), context_form=locals().get('value'), context_section=form.get('section'))
        return RedirectResponse('/sites/' + name + '#recovery', 303)

    def destination_page(request, error='', connected=None, revealed=None):
        return render(request, 'backup_destination.html', destination=call({'op': 'backup-destination'}), setup=call({'op': 'backup-setup'}), error=error, connected=connected, revealed=revealed)

    @app.get('/backups')
    def backup_destination(request: Request):
        session(request)
        return destination_page(request)

    @app.post('/backups/connect')
    async def backup_connect(request: Request):
        form = await mutation(request)
        try: result = call({'op': 'backup-connect', 'data': {k: str(v) for k, v in form.items() if k != 'csrf'}})
        except (ValueError, OSError) as exc: return destination_page(request, error=str(exc))
        return destination_page(request, connected=result)

    @app.post('/backups/enabled')
    async def backup_enabled(request: Request):
        form = await mutation(request)
        try: call({'op': 'backup-enabled', 'enabled': form.get('enabled') == 'yes'})
        except (ValueError, OSError) as exc: return destination_page(request, error=str(exc))
        return RedirectResponse('/backups', 303)

    @app.post('/backups/disconnect')
    async def backup_disconnect(request: Request):
        form = await mutation(request)
        if form.get('confirm') != 'disconnect': return destination_page(request, error='Type disconnect to confirm')
        try: call({'op': 'backup-disconnect'})
        except (ValueError, OSError) as exc: return destination_page(request, error=str(exc))
        return RedirectResponse('/backups', 303)

    @app.post('/backups/server-key')
    async def backup_server_key(request: Request):
        await mutation(request)
        try: call({'op': 'backup-server-key'})
        except (ValueError, OSError) as exc: return destination_page(request, error=str(exc))
        return RedirectResponse('/backups', 303)

    @app.post('/backups/reveal')
    async def backup_reveal(request: Request):
        await mutation(request)
        try: result = call({'op': 'backup-reveal'})
        except (ValueError, OSError) as exc: return destination_page(request, error=str(exc))
        return destination_page(request, revealed=result['password'])

    @app.post('/backups/copy')
    async def copy_backups(request: Request):
        await mutation(request)
        try: call({'op': 'backup-remote'})
        except (ValueError, OSError) as exc: return destination_page(request, error=str(exc))
        return RedirectResponse('/backups', 303)

    @app.post('/sites/{name}/backup/{action}')
    async def backup_action(request: Request, name: str, action: str):
        form = await mutation(request)
        row = next((x for x in call({'op': 'list'}) if x['name'] == name), None)
        if not row: raise HTTPException(404, 'Site not found')
        try:
            if action == 'database':
                call({'op': 'backup-database', 'site_id': row['id'], 'id': form.get('id', '')})
            elif action == 'site':
                call({'op': 'site-backup', 'site_id': row['id'], 'id': form.get('id', '')})
            elif action == 'schedule':
                call({'op': 'backup-schedule', 'site_id': row['id'], 'interval': int(form.get('interval', 15)),
                      'enabled': form.get('enabled') == 'on'})
            elif action == 'remote':
                call({'op': 'backup-remote'})
            else: raise HTTPException(404, 'Unknown backup action')
        except (ValueError, OSError) as exc:
            return render_site(request, row, error=str(exc))
        return RedirectResponse('/sites/' + name + '#recovery', 303)

    @app.post('/sites/{name}/delete')
    async def delete_site(request: Request, name: str):
        form = await mutation(request)
        row = next((x for x in call({'op': 'list'}) if x['name'] == name), None)
        if not row: raise HTTPException(404, 'Site not found')
        if form.get('confirm', '') != name:
            return render_site(request, row, error='Type the site name exactly to confirm deletion.')
        try: call({'op': 'site-delete', 'site_id': row['id'], 'id': form.get('id', '')})
        except (ValueError, OSError) as exc: return render_site(request, row, error=str(exc))
        return RedirectResponse('/sites/' + name + '#deletion', 303)

    @app.post('/sites/{name}/restore')
    async def restore_site(request: Request, name: str):
        form = await mutation(request)
        row = next((x for x in call({'op': 'list'}) if x['name'] == name), None)
        if not row: raise HTTPException(404, 'Site not found')
        try:
            new = call({'op': 'site-restore', 'snapshot': str(form.get('snapshot', '')), 'name': str(form.get('name', '')), 'domain': str(form.get('domain', ''))})
        except (ValueError, OSError) as exc: return render_site(request, row, error=str(exc))
        return RedirectResponse('/sites/' + new['name'], 303)

    @app.post('/retry-site-restore/{ident}')
    async def retry_site_restore(request: Request, ident: str):
        await mutation(request)
        try: job = call({'op': 'retry-site-restore', 'id': ident})
        except (ValueError, OSError) as exc: raise HTTPException(400, str(exc))
        row = next((x for x in call({'op': 'list'}) if x['id'] == job['site_id']), None)
        return RedirectResponse('/sites/' + row['name'] if row else '/', 303)

    @app.post('/retry-site-delete/{ident}')
    async def retry_site_delete(request: Request, ident: str):
        await mutation(request)
        try: job = call({'op': 'retry-site-delete', 'id': ident})
        except (ValueError, OSError) as exc: raise HTTPException(400, str(exc))
        row = next((x for x in call({'op': 'list'}) if x['id'] == job['site_id']), None)
        return RedirectResponse('/sites/' + row['name'] + '#deletion' if row else '/', 303)

    @app.post("/retry-domains/{ident}")
    async def retry_domains(request: Request, ident: str):
        await mutation(request)
        try:
            job = call({"op": "retry-domains", "id": ident})
            row = next(x for x in call({"op": "list"}) if x["id"] == job["site_id"])
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse("/sites/" + row["name"], 303)

    @app.post("/retry/{ident}")
    async def retry(request: Request, ident: str):
        await mutation(request)
        try:
            row = call({"op": "retry", "id": ident})
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse("/sites/" + row["name"], 303)

    @app.get("/api/sites")
    def status(request: Request):
        session(request)
        return call({"op": "list"})

    @app.get("/health")
    def health():
        return {"status": "ok", "schema": 1}

    from .import_web import routes as import_routes
    import_routes(app, auth, session, mutation, render, Path(auth_path).parent / 'imports', call)

    from .content_web import routes
    routes(app, session, mutation, render, call, Path(auth_path).parent / "uploads")
    from .backup_web import routes as backup_routes
    backup_routes(app, session, mutation, render, call, Path(auth_path).parent)
    from .mail_web import routes as mail_routes
    mail_routes(app, session, mutation, render, call)
    return app


def app():
    return create_app()
