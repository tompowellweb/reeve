"""Authenticated streaming uploads. One bounded shared spool, separate from worker data."""
import asyncio
import fcntl
import hashlib
import os
import secrets
import stat
import uuid
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .content_jobs import UPLOAD_LIMIT, EDIT_LIMIT, relative, validate_content
from .core import request_id


def routes(app, session, mutation, render, call, spool):
    spool = Path(spool)

    def site(name):
        try: return call({'op': 'content-site', 'name': name})
        except ValueError: raise HTTPException(404, 'Site not found') from None

    def page(request, row, path='.', edit=False, error=''):
        jobs = call({'op': 'content-status', 'site_id': row['id']})
        toolbox=call({'op':'toolbox-status','site_id':row['id']})
        pending = any(j['state'] in ('queued', 'running', 'recovery-needed') for j in jobs) or (toolbox and toolbox['state'] in ('starting','active'))
        files, document = [], None
        try:
            value = call({'op': 'content-files', 'site_id': row['id'], 'action': 'read' if edit else 'list', 'path': path})
            if edit: document = value
            else: files = value['entries']
        except (ValueError, OSError) as exc:
            error = error or str(exc)
        return render(request, 'content.html', site=row, jobs=jobs, pending=pending, files=files,
                      document=document, path=path, ident=str(uuid.uuid4()), error=error, edit_limit=EDIT_LIMIT, toolbox=toolbox)

    @app.get('/sites/{name}/files')
    def files(request: Request, name: str, path: str = '.', edit: bool = False):
        session(request)
        try: relative(path, root=not edit)
        except ValueError as exc: raise HTTPException(400, str(exc)) from None
        return page(request, site(name), path, edit)

    @app.get('/sites/{name}/content/status')
    def status(request: Request, name: str):
        session(request)
        return call({'op': 'content-status', 'site_id': site(name)['id']})

    @app.post('/sites/{name}/content/upload')
    async def upload(request: Request, name: str):
        found = session(request)
        if not secrets.compare_digest(request.headers.get('x-csrf-token', ''), found['csrf']): raise HTTPException(403, 'Form expired; reload before uploading')
        if request.headers.get('content-type') != 'application/octet-stream': raise HTTPException(415, 'Send one raw file')
        length = int(request.headers['content-length'])  # Required and bounded by the boundary middleware.
        query = request.query_params
        try:
            ident = request_id(query.get('id', '')); kind = query.get('kind', '')
            data = validate_content(kind, {'size': length, 'sha256': '0' * 64, 'path': query.get('path', '.'),
                'replace': query.get('replace') == 'yes', 'expected': query.get('expected', '')})
            if kind == 'sql' and query.get('confirm') != 'yes': raise ValueError('Confirm that this SQL may change existing database contents')
            row = await run_in_threadpool(site, name)
            jobs = await run_in_threadpool(call, {'op': 'content-status', 'site_id': row['id']})
            if any(j['state'] in ('queued','running','recovery-needed') for j in jobs): raise ValueError('Finish or resolve the pending content operation first')
        except (ValueError, OSError) as exc: raise HTTPException(400, str(exc)) from None
        spool.mkdir(mode=0o700, exist_ok=True)
        lockfd = os.open(spool / 'lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        target = None
        try:
            try: fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise HTTPException(409, 'Another upload is in progress; try when it completes') from None
            # With the lock held there can be no live upload. Remove only private
            # UUID spool entries left by an interrupted web request or process.
            for old in spool.iterdir():
                try: request_id(old.name)
                except ValueError: continue
                if stat.S_ISREG(old.lstat().st_mode) or old.is_symlink(): old.unlink()
            free = os.statvfs(spool)
            if free.f_bavail * free.f_frsize < length + 256 * 1048576: raise HTTPException(507, 'Not enough upload staging space')
            target = spool / ident
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            h = hashlib.sha256(); size = 0
            with os.fdopen(fd, 'wb') as output:
                async with asyncio.timeout(1800):
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > length or size > UPLOAD_LIMIT: raise HTTPException(413, 'Upload exceeds declared size')
                        output.write(chunk); h.update(chunk)
                output.flush(); os.fsync(output.fileno())
            if size != length: raise HTTPException(400, 'Upload was truncated')
            data['sha256'] = h.hexdigest()
            result = await run_in_threadpool(call, {'op': 'content-submit', 'id': ident, 'site_id': row['id'], 'kind': kind, 'data': data})
            return JSONResponse({'id': result['id'], 'url': '/sites/' + name + ('' if query.get('return_to') == 'overview' else '/files')})
        except (ValueError, OSError, TimeoutError) as exc: raise HTTPException(400, str(exc) or 'Upload timed out') from None
        finally:
            if target is not None: target.unlink(missing_ok=True)
            os.close(lockfd)

    @app.post('/sites/{name}/content/tool')
    async def tool(request: Request, name: str):
        form = await mutation(request); row = site(name)
        try:
            call({'op': 'content-submit', 'id': form.get('id', ''), 'site_id': row['id'], 'kind': 'tool',
                  'data': {'tool': form.get('tool', ''), 'arguments': form.get('arguments', ''),
                           'path': form.get('path', '.'), 'internet': form.get('internet') == 'yes'}})
        except (ValueError, OSError) as exc: return page(request, row, error=str(exc))
        return RedirectResponse('/sites/' + name + ('' if form.get('return_to') == 'overview' else '/files'), 303)

    @app.get('/sites/{name}/content/output/{ident}')
    def output(request: Request, name: str, ident: str):
        session(request); row = site(name)
        job = next((j for j in call({'op': 'content-status', 'site_id': row['id']}) if j['id'] == ident), None)
        if not job: raise HTTPException(404, 'Operation not found')
        return render(request, 'content_output.html', site=row, job=job, output=call({'op': 'content-output', 'id': ident}))

    @app.post('/sites/{name}/web-settings')
    async def web_settings(request: Request,name: str):
        form=await mutation(request); row=site(name)
        try: call({'op':'content-submit','id':form.get('id',''),'site_id':row['id'],'kind':'web-settings','data':{'profile':form.get('profile','php')}})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name+('' if form.get('return_to') == 'overview' else '/files'),303)

    @app.post('/sites/{name}/rules')
    async def site_rules(request: Request,name: str):
        form=await mutation(request); row=site(name)
        try: call({'op':'content-submit','id':form.get('id',''),'site_id':row['id'],'kind':'site-rules','data':{'text':str(form.get('text',''))}})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name+('' if form.get('return_to') == 'overview' else '/files'),303)

    @app.post('/sites/{name}/php-settings')
    async def php_settings(request: Request,name: str):
        from .php_settings import FIELDS
        form=await mutation(request); row=site(name)
        try: call({'op':'content-submit','id':form.get('id',''),'site_id':row['id'],'kind':'php-settings','data':{k:str(form.get(k,'')) for k in FIELDS}})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name+('' if form.get('return_to') == 'overview' else '/files'),303)

    @app.post('/sites/{name}/database-usage')
    async def database_usage(request: Request,name: str):
        form=await mutation(request); row=site(name)
        try: call({'op':'content-submit','id':form.get('id',''),'site_id':row['id'],'kind':'database-usage','data':{'usage':str(form.get('usage',''))}})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name,303)

    @app.post('/sites/{name}/sftp')
    async def sftp_access(request: Request,name: str):
        form=await mutation(request); row=site(name)
        try: call({'op':'content-submit','id':form.get('id',''),'site_id':row['id'],'kind':'sftp-access','data':{'action':str(form.get('action','on')),'secondary':str(form.get('secondary','')),'duration':str(form.get('duration','manual'))}})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name+('' if form.get('return_to') == 'overview' else '/files'),303)

    @app.get('/sites/{name}/sftp/key')
    def sftp_key(request: Request,name: str):
        session(request); row=site(name)
        try: export=call({'op':'sftp-key','site_id':row['id']})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/downloads/'+export['token']+'/'+export['filename'],303)

    @app.post('/sites/{name}/mail-senders')
    async def mail_senders(request: Request,name: str):
        form=await mutation(request); row=site(name)
        try: call({'op':'content-submit','id':form.get('id',''),'site_id':row['id'],'kind':'mail-senders','data':{'senders':str(form.get('senders',''))}})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name+('' if form.get('return_to') == 'overview' else '/files'),303)

    @app.post('/sites/{name}/fix-ownership')
    async def fix_ownership(request: Request,name: str):
        form=await mutation(request); row=site(name)
        try: call({'op':'content-submit','id':form.get('id',''),'site_id':row['id'],'kind':'fix-ownership','data':{}})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name+('' if form.get('return_to') == 'overview' else '/files'),303)

    @app.get('/sites/{name}/schedules')
    def schedules_page(request: Request,name: str):
        session(request); row=site(name)
        return render(request,'schedules.html',site=row,schedules=call({'op':'schedules','site_id':row['id']}))

    @app.post('/sites/{name}/schedules')
    async def schedules_save(request: Request,name: str):
        form=await mutation(request); row=site(name)
        try:
            data={'name':form.get('name',''),'interval':int(form.get('interval','0')),'enabled':form.get('enabled')=='yes',
                'tool':form.get('tool','php'),'arguments':form.get('arguments',''),'path':form.get('path','.'),'internet':form.get('internet')=='yes'}
            call({'op':'schedule-save','site_id':row['id'],'data':data})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name+('' if form.get('return_to') == 'overview' else '/schedules'),303)

    @app.get('/sites/{name}/toolbox')
    def toolbox_page(request: Request,name: str):
        session(request); row=site(name)
        info=call({'op':'toolbox-recipes'})
        return render(request,'toolbox.html',site=row,toolbox=call({'op':'toolbox-status','site_id':row['id']}),
            recipes=info['recipes'],settings=info['settings'],jobs=call({'op':'content-status','site_id':row['id']}),ident=str(uuid.uuid4()))

    @app.post('/sites/{name}/toolbox/save')
    async def toolbox_save(request: Request,name: str):
        form=await mutation(request); site(name)
        try: call({'op':'toolbox-save','name':form.get('name',''),'dockerfile':form.get('dockerfile','')})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name+'/toolbox',303)

    @app.post('/sites/{name}/toolbox/{action}')
    async def toolbox_change(request: Request,name: str,action: str):
        form=await mutation(request); row=site(name)
        if action not in ('start','stop'): raise HTTPException(404,'Unknown toolbox action')
        data={} if action=='stop' else {'recipe':form.get('recipe',''),'path':form.get('path','.'),
            'public_key':form.get('public_key',''),'jump':form.get('jump',''),'internet':form.get('internet')=='yes'}
        try: call({'op':'content-submit','id':form.get('id',''),'site_id':row['id'],'kind':'toolbox-'+action,'data':data})
        except (ValueError,OSError) as exc: raise HTTPException(400,str(exc)) from None
        return RedirectResponse('/sites/'+name+'/toolbox',303)

    @app.post('/sites/{name}/content/resolve/{ident}')
    async def resolve(request: Request, name: str, ident: str):
        await mutation(request); row = site(name)
        if not any(j['id'] == ident for j in call({'op': 'content-status', 'site_id': row['id']})): raise HTTPException(404, 'Operation not found')
        call({'op': 'content-resolve', 'id': ident})
        return RedirectResponse('/sites/' + name + '/files', 303)
