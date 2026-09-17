"""Per-site backups page, downloads, imports, in-place restores and the deleted-sites history."""
import asyncio
import hashlib
import os
import re
import uuid
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .core import request_id

IMPORT_LIMIT = 4 * 1024**3
FILENAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}\Z')


def routes(app, session, mutation, render, call, web_root):
    web_root = Path(web_root)
    downloads = web_root / 'downloads'
    uploads = web_root / 'backup-uploads'

    def site(name):
        row = next((x for x in call({'op': 'list'}) if x['name'] == name), None)
        if not row: raise HTTPException(404, 'Site not found')
        return row

    def page(request, row, error='', export=None):
        backups = call({'op': 'site-backup-list', 'site_id': row['id']})
        status = call({'op': 'site-backups', 'site_id': row['id']})
        restores = call({'op': 'site-restores', 'site_id': row['id']})
        links = None
        if export:
            try:
                request_id(export)
                folder = downloads / export
                if folder.is_dir():
                    links = [{'name': p.name, 'bytes': p.stat().st_size} for p in sorted(folder.iterdir()) if p.is_file()]
            except ValueError: links = None
        return render(request, 'backups.html', site=row, backups=backups, status=status, restores=restores,
                      export=export, links=links, ident=str(uuid.uuid4()), error=error)

    @app.get('/sites/{name}/backups')
    def backups(request: Request, name: str, export: str = ''):
        session(request)
        return page(request, site(name), export=export or None)

    @app.post('/sites/{name}/backups/export')
    async def export_backup(request: Request, name: str):
        form = await mutation(request); row = site(name)
        try: result = await run_in_threadpool(call, {'op': 'site-backup-export', 'snapshot': str(form.get('snapshot', ''))})
        except (ValueError, OSError) as exc: return page(request, row, error=str(exc))
        return RedirectResponse('/sites/' + name + '/backups?export=' + result['token'], 303)

    @app.get('/downloads/{token}/{filename}')
    def download(request: Request, token: str, filename: str):
        session(request)
        try: request_id(token)
        except ValueError: raise HTTPException(404, 'Not found') from None
        if not FILENAME.fullmatch(filename): raise HTTPException(404, 'Not found')
        path = downloads / token / filename
        if not path.is_file() or path.is_symlink(): raise HTTPException(404, 'Not found')
        return FileResponse(path, filename=filename, media_type='application/octet-stream')

    @app.post('/sites/{name}/backups/options')
    async def backup_options(request: Request, name: str):
        form = await mutation(request); row = site(name)
        try: call({'op': 'site-backup-options', 'site_id': row['id'], 'quiesce': form.get('quiesce') == 'on'})
        except (ValueError, OSError) as exc: return page(request, row, error=str(exc))
        return RedirectResponse('/sites/' + name + '/backups', 303)

    @app.post('/sites/{name}/backups/restore-into')
    async def restore_into(request: Request, name: str):
        form = await mutation(request); row = site(name)
        if form.get('confirm', '') != name:
            return page(request, row, error='Type the site name exactly to confirm an in-place restore.')
        try: call({'op': 'site-restore-into', 'site_id': row['id'], 'snapshot': str(form.get('snapshot', '')), 'scope': str(form.get('scope', ''))})
        except (ValueError, OSError) as exc: return page(request, row, error=str(exc))
        return RedirectResponse('/sites/' + name + '/backups', 303)

    @app.post('/sites/{name}/backups/import')
    async def import_backup(request: Request, name: str):
        form = await mutation(request); row = site(name)
        mode = str(form.get('mode', ''))
        token = str(uuid.uuid4()); names = {}
        uploads.mkdir(mode=0o700, exist_ok=True)
        written = []
        try:
            for field in ('files', 'dump'):
                upload = form.get(field)
                if upload is None or not getattr(upload, 'filename', ''): continue
                if not FILENAME.fullmatch(Path(upload.filename).name): raise ValueError('Use a plain archive file name.')
                target = uploads / (token + '.' + field)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600); written.append(target)
                size = 0
                with os.fdopen(fd, 'wb') as output:
                    while chunk := await upload.read(1048576):
                        size += len(chunk)
                        if size > IMPORT_LIMIT: raise ValueError('The upload exceeds the 4 GiB import limit.')
                        output.write(chunk)
                    output.flush(); os.fsync(output.fileno())
                if size == 0: target.unlink(); written.remove(target); continue
                names[field] = Path(upload.filename).name
            if not names: raise ValueError('Choose a snapshot archive, or site content and/or a database dump.')
            if mode == 'snapshot' and 'files' not in names: raise ValueError('A snapshot import needs the snapshot archive.')
            result = await run_in_threadpool(call, {'op': 'site-backup-import', 'site_id': row['id'], 'token': token, 'mode': mode, 'names': names})
        except (ValueError, OSError) as exc:
            for target in written: target.unlink(missing_ok=True)
            return page(request, row, error=str(exc))
        return RedirectResponse('/sites/' + name + '/backups', 303)

    @app.get('/history')
    def history(request: Request, error: str = ''):
        session(request)
        return render(request, 'history.html', deleted=call({'op': 'deleted-sites'}), error=error)

    @app.post('/history/restore')
    async def history_restore(request: Request):
        form = await mutation(request)
        try:
            new = call({'op': 'site-restore', 'snapshot': str(form.get('snapshot', '')), 'name': str(form.get('name', '')), 'domain': str(form.get('domain', ''))})
        except (ValueError, OSError) as exc:
            return render(request, 'history.html', deleted=call({'op': 'deleted-sites'}), error=str(exc))
        return RedirectResponse('/sites/' + new['name'], 303)
