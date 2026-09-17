"""Private package intake shared by browser and agents; no target-host preparation."""
import asyncio
import fcntl
import hashlib
import json
import os
import secrets
import shutil
import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .application_package import UPLOAD_LIMIT, metadata, review
from .core import request_id
from .preparation import instructions

RETAINED_LIMIT = 2 * 1024**3


def routes(app, auth, session, mutation, render, root, call):
    root = Path(root)

    def principal(request, write=False):
        header = request.headers.get('authorization', '')
        if header:
            if not header.startswith('Bearer '): raise HTTPException(401, 'Use a bearer token.')
            found = auth.api_token(header[7:])
            if not found: raise HTTPException(401, 'Invalid or expired agent token.')
            return found['id']
        found = session(request)
        if write and not secrets.compare_digest(request.headers.get('x-csrf-token', ''), found['csrf']):
            raise HTTPException(403, 'Form expired; reload before uploading.')
        return 'operator'

    def stored(ident, owner):
        request_id(ident)
        p = root / ident / 'result.json'
        if not p.is_file(): raise HTTPException(404, 'Import not found.')
        value = json.loads(p.read_text())
        if owner != 'operator' and value['owner'] != owner: raise HTTPException(404, 'Import not found.')
        return value

    def public(value): return {k: v for k, v in value.items() if k != 'owner'}

    def deployment(ident):
        try: return call({'op': 'package-status', 'id': ident})
        except (ValueError, OSError): return None

    def detail(request, value, error=None):
        from .package_deploy import eligibility
        return render(request, 'import_detail.html', result=public(value), deployment=deployment(value['id']),
                      deploy_reason=eligibility(value), error=error)

    @app.get('/api/v1/preparation')
    def preparation(request: Request):
        principal(request)
        value = instructions()
        if 'text/plain' in request.headers.get('accept', ''): return PlainTextResponse(value['prompt'])
        return value

    @app.get('/imports')
    def imports_page(request: Request):
        session(request)
        values = []
        try: operations = {v['id']: v for v in call({'op': 'package-list'})}
        except (ValueError, OSError): operations = {}
        if root.exists():
            for p in root.glob('*/result.json'):
                try:
                    value = public(json.loads(p.read_text()))
                    value['deployment'] = operations.get(value['id'])
                    values.append(value)
                except (OSError, ValueError): continue
        return render(request, 'imports.html', imports=sorted(values, key=lambda v: v['created'], reverse=True), prompt=instructions()['prompt'])

    @app.get('/imports/{ident}')
    def import_page(request: Request, ident: str):
        session(request)
        try: value = stored(ident, 'operator')
        except ValueError: raise HTTPException(404, 'Import not found.') from None
        return detail(request, value)

    @app.post('/imports/{ident}/deploy')
    async def deploy(request: Request, ident: str):
        # Existing agent tokens are intake-only; a browser login and CSRF are required.
        if request.headers.get('authorization'): raise HTTPException(403, 'Agent tokens grant intake access only.')
        await mutation(request)
        value = stored(ident, 'operator')
        from .package_deploy import eligibility
        reason = eligibility(value)
        if reason: return detail(request, value, error=reason)
        try:
            row = await run_in_threadpool(call, {'op': 'package-deploy', 'id': ident,
                                                 'data': value['input'], 'sha256': value['sha256']})
        except (ValueError, OSError) as exc: return detail(request, value, error=str(exc))
        return RedirectResponse('/sites/' + row['name'], 303)

    @app.get('/api/v1/imports/{ident}')
    def import_status(request: Request, ident: str):
        owner = principal(request)
        try:
            result = public(stored(ident, owner))
            operation = deployment(ident)
            if operation:
                result['deployment'] = operation
                result['deployed'] = operation['state'] == 'succeeded'
            return result
        except ValueError: raise HTTPException(404, 'Import not found.') from None

    @app.post('/api/v1/imports')
    async def upload(request: Request):
        owner = principal(request, write=True)
        if request.headers.get('content-type') not in ('application/octet-stream', 'application/gzip', 'application/zip', 'application/x-tar'):
            raise HTTPException(415, 'Send one ZIP or tar archive as the request body.')
        try:
            ident = request_id(request.headers.get('idempotency-key', ''))
            data = metadata(dict(request.query_params))
        except ValueError as exc: raise HTTPException(400, str(exc)) from None
        length = int(request.headers['content-length'])
        root.mkdir(mode=0o700, exist_ok=True)
        lock = os.open(root / 'lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        partial = root / (ident + '.partial')
        owns_partial = False
        try:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise HTTPException(409, 'Another project upload is in progress.') from None
            # Under the shared lock, only interrupted uploads can have partial files.
            for path in root.glob('*.partial'):
                if path.is_file() or path.is_symlink(): path.unlink()
            for path in root.glob('*.staging'):
                try: request_id(path.name.removesuffix('.staging'))
                except ValueError: continue
                if path.is_symlink(): path.unlink()
                elif path.is_dir(): shutil.rmtree(path)
            existing = None
            if (root / ident).exists():
                existing = stored(ident, owner)
                if existing['input'] != data: raise HTTPException(409, 'This request ID already has different inputs.')
            retained = sum(p.stat().st_size for p in root.glob('*/package') if p.is_file())
            if not existing and (retained + length > RETAINED_LIMIT or len(list(root.glob("*/result.json"))) >= 200):
                raise HTTPException(507, 'Project intake storage is full. Retained inputs have been kept.')
            free = os.statvfs(root)
            if free.f_bavail * free.f_frsize < length + 256 * 1048576:
                raise HTTPException(507, 'Not enough space to receive this project.')
            digest = hashlib.sha256(); size = 0
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            owns_partial = True
            with os.fdopen(fd, 'wb') as stream:
                async with asyncio.timeout(1800):
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > length or size > UPLOAD_LIMIT: raise HTTPException(413, 'Upload exceeds declared size.')
                        stream.write(chunk); digest.update(chunk)
                stream.flush(); os.fsync(stream.fileno())
            if size != length: raise HTTPException(400, 'Upload was truncated.')
            checksum = digest.hexdigest()
            if existing:
                if existing['sha256'] != checksum: raise HTTPException(409, 'This request ID already has different file contents.')
                return JSONResponse(public(existing), headers={'Location': '/api/v1/imports/' + ident})
            try: report = await run_in_threadpool(review, partial, data)
            except ValueError as exc:
                report = {'state': 'needs_preparation', 'issues': [{'code': 'invalid_package', 'path': 'package', 'message': str(exc)}],
                          'services': [], 'deployed': False, 'checks': [], 'remaining_checks': ['package_review', 'deployment']}
            result = dict(report, review_version=2, id=ident, input=data, sha256=checksum, bytes=size, created=time.time(), owner=owner,
                          url='/imports/' + ident, status_url='/api/v1/imports/' + ident)
            path = root / (ident + '.staging'); path.mkdir(mode=0o700)
            partial.rename(path / 'package')
            target = path / 'result.json'
            with target.open('x') as stream:
                json.dump(result, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
            target.chmod(0o600)
            dirfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(dirfd)
            finally: os.close(dirfd)
            path.rename(root / ident)
            dirfd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(dirfd)
            finally: os.close(dirfd)
            return JSONResponse(public(result), status_code=201, headers={'Location': result['status_url']})
        except (OSError, TimeoutError):
            raise HTTPException(400, 'Upload could not be saved. Check available storage and retry.') from None
        finally:
            if owns_partial: partial.unlink(missing_ok=True)
            os.close(lock)

    @app.get('/agent-access')
    def access(request: Request):
        session(request)
        return render(request, 'agent_access.html', tokens=auth.api_tokens(), token=None)

    @app.post('/agent-access')
    async def new_token(request: Request):
        form = await mutation(request)
        try: token = auth.new_api_token(str(form.get('name', '')))
        except ValueError as exc: return render(request, 'agent_access.html', tokens=auth.api_tokens(), token=None, error=str(exc))
        return render(request, 'agent_access.html', tokens=auth.api_tokens(), token=token)

    @app.post('/agent-access/{ident}/revoke')
    async def revoke_token(request: Request, ident: str):
        await mutation(request)
        auth.revoke_api_token(ident)
        return RedirectResponse('/agent-access', 303)
