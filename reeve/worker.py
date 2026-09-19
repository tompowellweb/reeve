"""Fixed Unix-socket protocol with kernel peer credentials; one mutator."""
import json
import os
import pwd
import socket
import socketserver
import struct
import threading
import time
import uuid

from .core import Ledger

SOCKET = "/run/reeve/worker.sock"


def rpc(message, path=SOCKET):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(600 if message.get("op") in ("site-backup-import", "site-backup-export", "mail-setup") else 120 if message.get("op") in ("content-submit", "recovery-inspect", "site-restore", "backup-connect", "request-certificate") else 20)
        conn.connect(path)
        conn.sendall(json.dumps(message).encode() + b"\n")
        with conn.makefile("rb") as stream:
            result = json.loads(stream.readline(1048576))
    if not result["ok"]:
        raise ValueError(result["error"])
    return result["result"]


def dispatch(message, ledger, host):
    if not isinstance(message, dict):
        raise ValueError("Expected an operation object")
    op = message.get("op")
    fields = {"package-list": {"op"}, "package-deploy": {"op", "id", "data", "sha256"}, "package-status": {"op", "id"}, "compose-site": {"op", "site_id"}, "list": {"op"}, "defaults": {"op"}, "create": {"op", "id", "data"}, "retry": {"op", "id"},
              "domains": {"op", "id", "site_id", "domains"}, "retry-domains": {"op", "id"},
              "certificates": {"op", "site_id"}, "request-certificate": {"op", "site_id", "domain"},
              "versions": {"op"}, "refresh-versions": {"op", "id"}, "php-rebuild": {"op", "id"}, "housekeeping": {"op"}, "sftp-key": {"op", "site_id"}, "backup-connect": {"op", "data"}, "backup-enabled": {"op", "enabled"}, "backup-disconnect": {"op"}, "backup-reveal": {"op"}, "backup-setup": {"op"}, "backup-server-key": {"op"}, "php-switch": {"op", "id", "site_id", "branch"},
              "php-rollback": {"op", "id", "site_id", "previous"}, "retry-runtime": {"op", "id"},
              "database-versions": {"op"}, "refresh-databases": {"op", "id"},
              "add-database": {"op", "id", "site_id", "data"}, "retry-database": {"op", "id"},
              "database-credentials": {"op", "site_id"},
              "content-submit": {"op", "id", "site_id", "kind", "data"},
              "content-site": {"op", "name"}, "content-status": {"op", "site_id"}, "content-files": {"op", "site_id", "action", "path"},
              "content-output": {"op", "id"}, "content-resolve": {"op", "id"}, "toolbox-recipes": {"op"},
              "toolbox-save": {"op", "name", "dockerfile"}, "toolbox-status": {"op", "site_id"},
              "schedules": {"op", "site_id"}, "site-traffic": {"op", "site_id"}, "schedule-save": {"op", "site_id", "data"},
              "recovery-status": {"op", "site_id"}, "recovery-inspect": {"op", "site_id"},
              "recovery-context": {"op", "site_id"}, "recovery-context-save": {"op", "site_id", "data"},
              "backup-destination": {"op"}, "mail-status": {"op"}, "mail-flush": {"op"}, "mail-delete": {"op", "id"}, "mail-setup": {"op"},
              "backup-status": {"op", "site_id"}, "backup-database": {"op", "site_id", "id"},
              "backup-schedule": {"op", "site_id", "interval", "enabled"},
              "backup-remote": {"op"},
              "site-backup": {"op", "id", "site_id"}, "site-backups": {"op", "site_id"},
              "site-restore": {"op", "snapshot", "name", "domain"},
              "site-delete": {"op", "id", "site_id"}, "site-deletes": {"op", "site_id"}, "retry-site-delete": {"op", "id"},
              "site-restores": {"op", "site_id"}, "retry-site-restore": {"op", "id"},
              "site-backup-list": {"op", "site_id"}, "site-backup-export": {"op", "snapshot"},
              "site-backup-import": {"op", "site_id", "token", "mode", "names"},
              "site-restore-into": {"op", "site_id", "snapshot", "scope"}, "deleted-sites": {"op"},
              "site-backup-options": {"op", "site_id", "quiesce"}}
    if op not in fields or set(message) != fields[op]:
        raise ValueError("Unsupported operation or fields")
    if op == 'package-deploy':
        from .package_deploy import submit
        return submit(ledger, host, message['id'], message['data'], message['sha256'])
    if op == 'package-list':
        return [{key: row[key] for key in ('id', 'name', 'state', 'step', 'error')}
                for row in ledger.list() if json.loads(row['payload']).get('package_id')]
    if op == 'package-status':
        from .core import request_id
        request_id(message['id'])
        try: row = ledger.get(message['id'])
        except ValueError: return None
        if not json.loads(row['payload']).get('package_id'): return None
        return {key: row[key] for key in ('id', 'name', 'state', 'step', 'error')}
    if op == 'site-backup': return ledger.submit_site_backup(message['id'], message['site_id'])
    if op == 'site-backups':
        row = ledger.get(message['site_id'])
        from .site_backup import status
        return status(ledger, row)
    if op == 'site-restore':
        from .site_backup import restore
        return ledger.get(restore(ledger, host, message['snapshot'], message['name'], message['domain'])['id'])
    if op == 'site-delete': return ledger.submit_site_delete(message['id'], message['site_id'])
    if op == 'site-deletes':
        ledger.get(message['site_id'])
        return ledger.site_deletes(message['site_id'])
    if op == 'retry-site-delete': return ledger.retry_site_delete(message['id'])
    if op == 'site-restores':
        ledger.get(message['site_id'])
        return ledger.site_restores(message['site_id'])
    if op == 'retry-site-restore': return ledger.retry_site_restore(message['id'])
    if op == 'site-backup-list':
        from .site_backup import listing
        return listing(ledger, ledger.get(message['site_id']))
    if op == 'site-backup-export':
        from .site_backup import export
        return export(message['snapshot'])
    if op == 'site-backup-import':
        from .site_backup import import_backup
        if not isinstance(message['names'], dict) or set(message['names']) - {'files', 'dump'} or any(not isinstance(v, str) or len(v) > 255 for v in message['names'].values()):
            raise ValueError('Invalid upload names.')
        return import_backup(ledger, host, message['site_id'], message['token'], message['mode'], message['names'])
    if op == 'site-restore-into':
        from .site_backup import restore_into
        return restore_into(ledger, host, message['snapshot'], message['site_id'], message['scope'])
    if op == 'site-backup-options':
        from .site_backup import set_options
        return set_options(ledger, ledger.get(message['site_id']), message['quiesce'])
    if op == 'deleted-sites':
        from .site_backup import deleted_sites
        return deleted_sites(ledger)
    if op == 'backup-destination':
        from .remote_backup import status
        from .host import BACKUPS, command
        result = status(ledger, None)
        local = {'path': str(BACKUPS), 'default': str(BACKUPS) == '/srv/backups', 'exists': BACKUPS.is_dir()}
        if local['exists']:
            free = os.statvfs(BACKUPS); local['free'] = free.f_bavail * free.f_frsize
            try: local['used'] = int(command(['du', '-sb', str(BACKUPS)]).split()[0])
            except RuntimeError: local['used'] = None
        return {**result, 'local': local}
    if op == 'mail-status':
        from .mail import status as mail_status
        return mail_status(host)
    if op == 'mail-flush':
        from .mail import flush
        return flush(host)
    if op == 'mail-delete':
        from .mail import delete as mail_delete
        return mail_delete(host, message['id'])
    if op == 'mail-setup':
        from .mail import setup as mail_setup
        return mail_setup(host, ledger)
    if op in ('recovery-context', 'recovery-context-save'):
        from .recovery_context import read, save
        row = ledger.get(message['site_id'])
        return read(row) if op == 'recovery-context' else save(ledger, row, message['data'])
    if op == 'backup-remote':
        from .remote_backup import request
        request(ledger)
        return {'state': 'requested'}
    if op in ('backup-status', 'backup-database', 'backup-schedule'):
        from .backup_jobs import status
        row = ledger.get(message['site_id'])
        if op == 'backup-database': return ledger.submit_backup(message['id'], row['id'])
        if op == 'backup-schedule': ledger.backup_schedule(row['id'], message['interval'], message['enabled'])
        return status(ledger, row)
    if op in ('recovery-status', 'recovery-inspect'):
        from .recovery_inventory import read, scan
        row = ledger.get(message['site_id'])
        return read(row) if op == 'recovery-status' else scan(ledger, row)
    if op == 'compose-site':
        from .compose_adopt import public
        row = ledger.get(message['site_id'])
        if json.loads(row['payload']).get('runtime') != 'compose': raise ValueError('Not a Compose package.')
        if json.loads(row['payload']).get('package_id'):
            from .package_deploy import public
        return public(row)
    # Managed PHP/static tools assume a generated html layout; never apply them to an
    # arbitrary adopted project. Read-only activity/schedule lists remain available.
    if op in ('content-submit', 'content-files', 'toolbox-status', 'schedule-save', 'add-database', 'database-credentials'):
        row = ledger.get(message['site_id'])
        if json.loads(row['payload']).get('runtime') == 'compose':
            raise ValueError('This operation is for managed static/PHP sites; maintain this application through its Compose source.')
    if op == 'site-traffic':
        from .traffic import site_traffic
        return site_traffic(ledger, message['site_id'])
    if op in ('schedules','schedule-save'):
        from .schedules import list_schedules,save
        return list_schedules(ledger,message['site_id']) if op=='schedules' else save(ledger,message['site_id'],message['data'])
    if op.startswith('toolbox-'):
        from . import toolbox
        if op == 'toolbox-recipes': return {'recipes': toolbox.recipes(), 'settings': toolbox.settings()}
        if op == 'toolbox-save': return toolbox.save_recipe(message['name'],message['dockerfile'])
        return toolbox.status(ledger,host,ledger.get(message['site_id']))
    if op.startswith('content-'):
        from . import content_site
        if op == 'content-site':
            row = next((r for r in ledger.list() if r['name'] == message['name']), None)
            if not row: raise ValueError('Site not found')
            from .database_site import public
            from .requests_site import public as web_settings
            from .site_rules import read as site_rules
            from .php_settings import public as php_settings
            from .sftp import public as sftp
            from .mail import site_line as mail_line
            return dict(row, web_settings=web_settings(row), site_rules=site_rules(row) if json.loads(row['payload']).get('runtime') != 'compose' else None, php_settings=php_settings(row), sftp=sftp(row), mail=mail_line(row),
                        php_branch=ledger.runtime_branch(row), database=public(row, host))
        if op == 'content-submit':
            if message['kind'] in ('toolbox-start','toolbox-stop'):
                from .toolbox import prepare
            else: prepare=content_site.prepare
            return ledger.submit_content(message['id'], message['site_id'], message['kind'], message['data'],
                lambda row: prepare(row, message['id'], message['kind'], message['data']))
        if op == 'content-status':
            ledger.get(message['site_id'])
            return ledger.content_jobs(message['site_id'])[:50]
        if op == 'content-files':
            return content_site.inspect_files(ledger.get(message['site_id']), message['action'], message['path'])
        job = next((j for j in ledger.content_jobs() if j['id'] == message['id']), None)
        if not job: raise ValueError('Unknown content operation')
        if op == 'content-output': return content_site.output(job['id'])
        if job['state'] != 'recovery-needed': raise ValueError('Only an interrupted operation needs resolving')
        if job['kind'] in ('toolbox-start','toolbox-stop'):
            from .toolbox import stop
            stop(ledger,host,ledger.get(job['site_id']))
        elif job['kind']=='web-settings':
            from .requests_site import rollback
            rollback(host,ledger.get(job['site_id']),job['id'])
        elif job['kind']=='site-rules':
            from .site_rules import rollback as rollback_rules
            rollback_rules(host,ledger.get(job['site_id']),job['id'])
        elif job['kind']=='php-settings':
            from .php_settings import rollback as rollback_php
            rollback_php(host,ledger.get(job['site_id']),job['id'])
        elif job['kind']=='database-usage':
            from .database_site import rollback_usage
            rollback_usage(host,ledger.get(job['site_id']),job['id'])
        elif job['kind']=='sftp-access':
            from .sftp import rollback as rollback_sftp
            rollback_sftp(host,ledger.get(job['site_id']),job['id'])
        elif job['kind']=='mail-senders':
            from .mail import rollback_senders
            rollback_senders(host,ledger.get(job['site_id']),job['id'])
        else: content_site.cleanup(ledger.get(job['site_id']), job['id'])
        ledger.update_content(job['id'], 'failed', 'reviewed by operator', 'Interrupted operation reviewed; not replayed')
        return job
    if op == 'database-versions':
        from .database_versions import read
        return dict(read(), job=next((j for j in ledger.database_jobs() if j['kind'] == 'refresh'), None))
    if op == 'refresh-databases':
        return ledger.submit_database(message['id'], 'refresh')
    if op == 'add-database':
        return ledger.submit_database(message['id'], 'add', message['site_id'], message['data'])
    if op == 'retry-database':
        return ledger.retry_database(message['id'])
    if op == 'database-credentials':
        from .database_site import credentials
        return credentials(ledger.get(message['site_id']))
    if op == "create":
        if not isinstance(message["data"], dict):
            raise ValueError("Create requires a settings object")
        if message['data'].get('runtime') == 'php':
            from .versions import require
            require(message['data'].get('php_version'))
        return ledger.submit(message["id"], {**host.defaults, **message["data"]})
    if op == 'versions':
        from .versions import read, choices
        from .php_updates import overview
        return dict(read(), choices=choices(), job=next((j for j in ledger.runtime_jobs() if j['kind'] == 'refresh'), None),
                    rebuild=overview(ledger), rebuild_job=next((j for j in ledger.runtime_jobs() if j['kind'] == 'rebuild'), None))
    if op == 'refresh-versions':
        return ledger.submit_runtime(message['id'], 'refresh')
    if op == 'php-rebuild':
        return ledger.submit_runtime(message['id'], 'rebuild')
    if op == 'housekeeping':
        from .housekeeping import prune
        return prune()
    if op == 'sftp-key':
        from .sftp import export_key
        return export_key(ledger.get(message['site_id']))
    if op == 'backup-connect':
        from .destination import connect
        return connect(message['data'])
    if op == 'backup-enabled':
        from .destination import set_enabled
        return set_enabled(bool(message['enabled']))
    if op == 'backup-disconnect':
        from .destination import disconnect
        return disconnect()
    if op == 'backup-reveal':
        from .destination import reveal
        return reveal()
    if op == 'backup-setup':
        from .destination import overview
        return overview()
    if op == 'backup-server-key':
        from .destination import server_key
        return {'public_key': server_key()}
    if op == 'php-switch':
        from .versions import require
        return ledger.submit_runtime(message['id'], 'switch', message['site_id'], {'branch': require(message['branch'])})
    if op == 'php-rollback':
        return ledger.submit_runtime(message['id'], 'rollback', message['site_id'], {'previous': message['previous']})
    if op == 'retry-runtime':
        return ledger.retry_runtime(message['id'])
    if op == "defaults":
        return host.defaults
    if op == "retry":
        return ledger.retry(message["id"])
    if op == "domains":
        return ledger.submit_domains(message["id"], message["site_id"], message["domains"])
    if op == "retry-domains":
        return ledger.retry_domains(message["id"])
    if op == "certificates":
        from .certificates import edge_issuance
        return edge_issuance(ledger.domains(ledger.get(message["site_id"])))
    if op == "request-certificate":
        from .certificates import request_public
        return request_public(message["domain"], ledger.domains(ledger.get(message["site_id"])))
    from .database_site import public as database_info
    def describe(row):
        database = database_info(row, host)
        latest_dump = next(iter(ledger.backup_jobs(row['id'])), None) if database else None
        backup_summary = ('Local DB: ' + latest_dump['state']) if latest_dump else ('Local DB: not run yet' if database else 'Not configured')
        health = host.health(row)
        if database:
            health['database'] = database['health']
            if database['health'] != 'healthy': health['application'] = 'unhealthy'
        return dict(row, database=database, backup_summary=backup_summary, database_job=next(iter(ledger.database_jobs(row['id'])), None),
                    health=health, domains=ledger.domains(row), domain_job=next(iter(ledger.domain_jobs(row['id'])), None),
                    php_branch=ledger.runtime_branch(row), runtime_job=next(iter(ledger.runtime_jobs(row['id'])), None))
    return [describe(row) for row in ledger.list()]


def package_prebuild(ledger, host, row, builder, spawn=None):
    """Image builds and pulls run beside the serial worker so a mode 2 build never delays mode 1 work.

    Returns False when the package already has its images (proceed serially), True when the row
    was handed to the build lane or failed, and None when the lane is busy (leave it queued).
    """
    from . import package_deploy as pd
    try: state = pd.load(row)
    except (OSError, ValueError): return False
    if state.get('images_ready') or state['stage'] not in ('reserved', 'extracted', 'files ready'): return False
    if builder['thread'] is not None and builder['thread'].is_alive(): return None
    ledger.update(row['id'], 'running', row['step'])
    def step(value): ledger.update(row['id'], 'running', value)
    try:
        from .host import preflight
        preflight()
        state, package, report = pd.inputs(row)
        pd.extract_stage(row, state, package, report, step)
    except Exception as exc:
        ledger.update(row['id'], 'recovery-needed', ledger.get(row['id'])['step'], str(exc))
        return True
    step('preparing images')
    def build():
        try:
            pd.build_images(host, row, step)
            ledger.update(row['id'], 'queued', 'images ready')
        except Exception as exc:
            ledger.update(row['id'], 'recovery-needed', ledger.get(row['id'])['step'], str(exc))
    if spawn: spawn(build)
    else:
        builder['thread'] = threading.Thread(target=build, daemon=True, name='package-build')
        builder['thread'].start()
    return True


def scheduled_backups(ledger, host):
    """The backup work the worker does every half minute: recover interrupted dumps, queue due dumps
    and nightly site backups, prune by retention. One function, so nothing here can quietly lose a
    name the loop expects (it did once, and the schedules stopped for a morning)."""
    from .database_backup import recover as recover_backups, retention_tick
    from .backup_jobs import tick as backup_tick
    from .site_backup import tick as site_tick, prune as site_prune
    recover_backups(ledger, host)
    backup_tick(ledger)
    site_tick(ledger); site_prune(ledger)
    retention_tick(ledger)


def startup_recovery(ledger, host, log=None):
    """Every module's interrupted-job recovery, one after another; a failing one is reported and
    leaves its job for review, never stopping the worker. Sites stay served whatever happened."""
    import sys
    log = log or (lambda text: print(text, file=sys.stderr, flush=True))
    from .database_backup import recover as recover_backups
    from .content_site import recover as recover_content
    from .toolbox import recover as recover_toolboxes
    from .requests_site import recover as recover_web
    from .site_rules import recover as recover_rules
    from .php_settings import recover as recover_php
    from .database_site import recover_usage
    from .sftp import recover as recover_sftp
    from .mail import recover as recover_mail
    from .site_backup import recover as recover_site_backups
    from .host import reconcile_edge
    steps = [('edge configuration', lambda: reconcile_edge(host)), ('database backups', lambda: recover_backups(ledger, host)), ('content jobs', lambda: recover_content(ledger)),
             ('toolboxes', lambda: recover_toolboxes(ledger, host)), ('web settings', lambda: recover_web(ledger, host)),
             ('site rules', lambda: recover_rules(ledger, host)), ('PHP limits', lambda: recover_php(ledger, host)), ('database usage', lambda: recover_usage(ledger, host)),
             ('customer SFTP', lambda: recover_sftp(ledger, host)), ('mail', lambda: recover_mail(ledger, host)),
             ('site backups', lambda: recover_site_backups(ledger))]
    failed = []
    for name, step in steps:
        try: step()
        except Exception as exc:
            failed.append(name); log(f'startup recovery of {name} failed and is left for review: {exc}')
    return failed


def run():
    from .host import Host
    os.umask(0o077)
    ledger = Ledger("/srv/ops/panel/worker/jobs.sqlite3")
    host = Host()
    # The installed worker unit has one process; a lock also prevents accidental CLI duplicates.
    import fcntl
    lock = open("/srv/ops/panel/worker/lock", "a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ledger.interrupted()
    startup_recovery(ledger, host)

    builder = {'thread': None}

    def process():
        next_catalogue_check = 0
        next_schedule_check = 0
        next_backup_check = 0
        while True:
            if time.monotonic() >= next_backup_check:
                from .backup_jobs import tick as backup_tick
                from .site_backup import tick as site_tick, prune as site_prune
                try: scheduled_backups(ledger, host)
                except Exception:
                    import logging
                    logging.exception('Database backup schedule/recovery failed; retrying next check')
                next_backup_check = time.monotonic() + 30
            if time.monotonic() >= next_schedule_check:
                from .schedules import tick,prune
                try:
                    tick(ledger); prune(ledger)
                except Exception:
                    import logging
                    logging.exception("Schedule reconciliation failed; retrying next check")
                next_schedule_check=time.monotonic()+5
            if time.monotonic() >= next_catalogue_check:
                from .versions import due
                if due():
                    ledger.submit_runtime(str(uuid.uuid4()), 'refresh')
                from .database_versions import due as databases_due
                if databases_due():
                    ledger.submit_database(str(uuid.uuid4()), 'refresh')
                from .php_updates import due as rebuild_due
                if rebuild_due():
                    ledger.submit_runtime(str(uuid.uuid4()), 'rebuild')
                from .sftp import expire as expire_sftp
                try: expire_sftp(host)
                except Exception:
                    import logging
                    logging.exception('Customer SFTP expiry failed; retrying next check')
                from .housekeeping import due as housekeeping_due, prune
                if housekeeping_due():
                    try: prune()
                    except Exception:
                        import logging
                        logging.exception('Housekeeping failed; retrying tomorrow')
                from .updates import due as update_due, check as update_check
                if update_due():
                    try: update_check()
                    except Exception:
                        import logging
                        logging.exception('Update check failed; retrying tomorrow')
                from .traffic import tick as traffic_tick
                try: traffic_tick(ledger)
                except Exception:
                    import logging
                    logging.exception('Traffic aggregation failed; retrying next minute')
                from .server_status import write as write_status
                try: write_status(ledger, host)
                except Exception:
                    import logging
                    logging.exception('Server summary failed; retrying next minute')
                next_catalogue_check = time.monotonic() + 60
            for row in reversed(ledger.list()):
                if row["state"] != "queued":
                    continue
                if json.loads(row['payload']).get('package_id') and package_prebuild(ledger, host, row, builder) is not False:
                    continue  # Building in the separate lane, or waiting for it; site work continues.
                ledger.update(row["id"], "running", row["step"])
                try:
                    host.create(row, lambda step: ledger.update(row["id"], "running", step))
                    ledger.update(row["id"], "succeeded", "published")
                except Exception as exc:
                    # Only controlled helper diagnostics: never command environments or file contents.
                    ledger.update(row["id"], "recovery-needed", ledger.get(row["id"])["step"], str(exc))
            for job in ledger.domain_jobs():
                if job["state"] != "queued":
                    continue
                ledger.update_domains(job["id"], "running")
                try:
                    host.change_domains(ledger.get(job["site_id"]), json.loads(job["payload"]))
                    ledger.finish_domains(job)
                except Exception as exc:
                    ledger.update_domains(job["id"], "recovery-needed", str(exc))
                    continue
                # The names and routes are changed and recorded. What follows informs the site of them and checks
                # it answers; a failure is a note on the change, shown with the certificate status, never a rollback.
                changed = ledger.get(job["site_id"])
                try:
                    host.settle_domains(changed, json.loads(job["payload"]))
                    if json.loads(changed["payload"]).get("runtime") == "php":
                        from .mail import attach as attach_mail
                        attach_mail(host, changed, ledger.domains(changed))
                except Exception as exc:
                    ledger.note_domains(job["id"], "Names and routes changed. The site did not answer on them yet: " + str(exc))
            for job in reversed(ledger.runtime_jobs()):
                if job['state'] != 'queued':
                    continue
                def step(value):
                    ledger.update_runtime(job['id'], 'running', value)
                step('starting')
                try:
                    if job['kind'] == 'refresh':
                        from .versions import refresh
                        step('checking signed repository metadata')
                        refresh()
                        ledger.finish_runtime(job)
                    elif job['kind'] == 'rebuild':
                        from .php_updates import rebuild
                        rebuild(ledger, host, job, step)
                        ledger.finish_runtime(job)
                    else:
                        from .php_switch import perform
                        branch = perform(host, ledger.get(job['site_id']), job, step)
                        ledger.finish_runtime(job, branch)
                except Exception as exc:
                    latest = next(j for j in ledger.runtime_jobs() if j['id'] == job['id'])
                    ledger.update_runtime(job['id'], 'recovery-needed', latest['step'], str(exc))
            for job in reversed(ledger.database_jobs()):
                if job['state'] != 'queued': continue
                def step(value): ledger.update_database(job['id'], 'running', value)
                step('starting')
                try:
                    if job['kind'] == 'refresh':
                        from .database_versions import refresh
                        step('checking official database image catalogues'); refresh()
                    else:
                        from .database_site import provision
                        row = ledger.get(job['site_id'])
                        provision(host, row, json.loads(job['payload']), step)
                        host.verify_domains(ledger.domains(row))
                    ledger.update_database(job['id'], 'succeeded', 'verified')
                except Exception as exc:
                    current = next(j for j in ledger.database_jobs() if j['id'] == job['id'])
                    ledger.update_database(job['id'], 'recovery-needed', current['step'], str(exc))
            for job in reversed(ledger.content_jobs()):
                if job['state'] != 'queued': continue
                def step(value): ledger.update_content(job['id'], 'running', value)
                step('starting')
                try:
                    from .content_site import perform, ContentFailed
                    if job['kind'] in ('toolbox-start','toolbox-stop'):
                        from .toolbox import perform as toolbox_perform
                        toolbox_perform(ledger,host,ledger.get(job['site_id']),job,step)
                    elif job['kind']=='web-settings':
                        from .requests_site import perform as web_perform
                        web_perform(host,ledger.get(job['site_id']),job,step)
                    elif job['kind']=='site-rules':
                        from .site_rules import perform as rules_perform
                        rules_perform(host,ledger.get(job['site_id']),job,step)
                    elif job['kind']=='php-settings':
                        from .php_settings import perform as php_perform
                        php_perform(host,ledger.get(job['site_id']),job,step)
                    elif job['kind']=='database-usage':
                        from .database_site import perform_usage
                        perform_usage(host,ledger.get(job['site_id']),job,step)
                    elif job['kind']=='sftp-access':
                        from .sftp import perform as sftp_perform
                        sftp_perform(host,ledger.get(job['site_id']),job,step)
                    elif job['kind']=='fix-ownership':
                        from .sftp import perform_fix
                        perform_fix(host,ledger.get(job['site_id']),job,step)
                    elif job['kind']=='mail-senders':
                        from .mail import perform_senders
                        perform_senders(host,ledger.get(job['site_id']),job,step)
                    else: perform(ledger.get(job['site_id']), job, step)
                    ledger.update_content(job['id'], 'succeeded', 'completed')
                except ContentFailed as exc:
                    ledger.update_content(job['id'], 'failed', 'stopped; inspect result', str(exc))
                except Exception as exc:
                    ledger.update_content(job['id'], 'recovery-needed', 'cleanup needs review', str(exc))
            for job in reversed(ledger.backup_jobs(active=True)):
                if job['state'] != 'queued': continue
                from .database_backup import perform as perform_backup
                perform_backup(ledger, host, job)
            for job in reversed(ledger.site_backups(active=True)):
                if job['state'] != 'queued': continue
                from .site_backup import perform as perform_site_backup
                perform_site_backup(ledger, host, job)
            for job in reversed(ledger.site_deletes(active=True)):
                if job['state'] != 'queued': continue
                from .site_backup import perform_delete
                perform_delete(ledger, host, job)
            for job in reversed(ledger.site_restores(active=True)):
                if job['state'] != 'queued': continue
                from .site_backup import perform_restore
                perform_restore(ledger, host, job)
            time.sleep(0.5)

    allowed_uid = pwd.getpwnam("hosting-web").pw_uid

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            try:
                if uid not in (0, allowed_uid):
                    raise ValueError("Peer is not permitted")
                raw = self.rfile.readline(32769)
                if len(raw) > 32768 or not raw.endswith(b"\n"):
                    raise ValueError("Request too large or incomplete")
                result = {"ok": True, "result": dispatch(json.loads(raw), ledger, host)}
            except Exception as exc:
                result = {"ok": False, "error": str(exc)[:2000]}
            self.wfile.write(json.dumps(result).encode() + b"\n")

    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    with socketserver.UnixStreamServer(SOCKET, Handler) as server:
        os.chown(SOCKET, 0, pwd.getpwnam("hosting-web").pw_gid)
        os.chmod(SOCKET, 0o660)
        threading.Thread(target=process, daemon=True).start()
        server.serve_forever()


if __name__ == "__main__":
    run()
