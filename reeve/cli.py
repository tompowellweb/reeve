"""`reeve`: the operator's command line. Nouns and verbs over the same worker the pages use; site names, not ids."""
import argparse
import getpass
import json
import os
import pwd
import sys
import uuid

from .core import DEFAULTS
from .worker import rpc


def out(value):
    print(json.dumps(value, indent=2))


def site_row(name):
    row = next((x for x in rpc({'op': 'list'}) if x['name'] == name), None)
    if not row: raise SystemExit(f'No site named {name}')
    return row


def table(rows, columns):
    widths = [max(len(c), *(len(str(r.get(c, ''))) for r in rows)) for c in columns] if rows else [len(c) for c in columns]
    print('  '.join(c.upper().ljust(w) for c, w in zip(columns, widths)))
    for r in rows: print('  '.join(str(r.get(c, '')).ljust(w) for c, w in zip(columns, widths)))


def add_site(sub):
    site = sub.add_parser('site', help='sites by name').add_subparsers(dest='verb', required=True)
    site.add_parser('list', help='every site with its state').add_argument('--json', action='store_true')
    create = site.add_parser('create', help='create a site')
    create.add_argument('name'); create.add_argument('domain')
    create.add_argument('--alias', action='append', default=[], help='an additional hostname; repeat as needed')
    create.add_argument('--runtime', choices=('static', 'php'), default='static')
    create.add_argument('--php-version')
    create.add_argument('--database', choices=('mariadb', 'mysql', 'postgres'))
    create.add_argument('--database-series'); create.add_argument('--database-exact'); create.add_argument('--database-usage', choices=('light', 'standard', 'high'))
    for key in DEFAULTS: create.add_argument('--' + key.replace('_', '-'), type=float if key == 'cpus' else int)
    site.add_parser('retry', help="retry the site's failed or interrupted operation").add_argument('name')
    domains = site.add_parser('domains', help="replace the site's hostnames; the first is primary")
    domains.add_argument('name'); domains.add_argument('domains', nargs='+')
    site.add_parser('delete', help='delete a site after a final backup').add_argument('name')
    site.add_parser('backup', help='take a complete backup now').add_argument('name')
    site.add_parser('backups', help="the site's complete backups").add_argument('name')
    restore = site.add_parser('restore', help='restore a backup as a new site')
    restore.add_argument('backup'); restore.add_argument('--name', required=True); restore.add_argument('--domain', required=True)
    site.add_parser('restores', help="the site's restores").add_argument('name')
    quiesce = site.add_parser('quiesce', help='pause the site while backing up, or not'); quiesce.add_argument('name'); quiesce.add_argument('setting', choices=('on', 'off'))


def add_others(sub):
    backup = sub.add_parser('backup', help='the backup destinations').add_subparsers(dest='verb', required=True)
    backup.add_parser('status', help='every destination, pending copies, last copies')
    backup.add_parser('copy', help='copy waiting backups to every enabled destination now')
    backup.add_parser('card', help='print the recovery card (the whole way into every repository; keep it private)')
    backup.add_parser('connect-card', help='connect every destination a recovery card names').add_argument('file')
    schedule = backup.add_parser('schedule', help="a site's database dump interval"); schedule.add_argument('name'); schedule.add_argument('--interval', type=int, choices=(15, 60), default=15); schedule.add_argument('--paused', action='store_true')
    php = sub.add_parser('php', help='PHP branches and rebuilds').add_subparsers(dest='verb', required=True)
    php.add_parser('versions', help='the catalogue and built images'); php.add_parser('refresh', help='refresh the catalogue')
    switch = php.add_parser('switch', help="change a site's PHP branch"); switch.add_argument('name'); switch.add_argument('branch')
    php.add_parser('rollback', help="put a site back on its previous PHP image").add_argument('name')
    php.add_parser('rebuild', help='rebuild every branch with fresh packages now')
    db = sub.add_parser('db', help='databases').add_subparsers(dest='verb', required=True)
    add = db.add_parser('add', help='add a database to a site'); add.add_argument('name'); add.add_argument('engine', choices=('mariadb', 'mysql', 'postgres')); add.add_argument('--series'); add.add_argument('--exact'); add.add_argument('--usage', choices=('light', 'standard', 'high'))
    db.add_parser('credentials', help="a site's application credentials (prints the secret)").add_argument('name')
    usage = db.add_parser('usage', help="change what a site's database is sized for"); usage.add_argument('name'); usage.add_argument('usage', choices=('light', 'standard', 'high'))
    db.add_parser('versions', help='the engine catalogue'); db.add_parser('refresh', help='refresh the engine catalogue')
    mail = sub.add_parser('mail', help='the relay').add_subparsers(dest='verb', required=True)
    mail.add_parser('setup', help='apply the mail settings: build, start, attach every PHP site'); mail.add_parser('status', help='queue and per-site counts')
    sub.add_parser('status', help="the server as the home page shows it")
    update = sub.add_parser('update', help='install the newest release, or a named one'); update.add_argument('--to', help='a version such as 1.2.0'); update.add_argument('--check', action='store_true', help='only ask what is available')
    password = sub.add_parser('password', help='set the operator password'); password.add_argument('--stdin', action='store_true', help='read it from a private pipe')
    doctor = sub.add_parser('doctor', help='check the machine and the panel'); doctor.add_argument('--repair', action='store_true', help='rebuild the edge from the recorded routes')
    settings = sub.add_parser('settings', help="the server's settings").add_subparsers(dest='verb', required=True)
    settings.add_parser('show', help='every group as the Settings page shows it')
    save = settings.add_parser('set', help='save one group: reeve settings set certificates mode=public email=you@example.com'); save.add_argument('group'); save.add_argument('values', nargs='+', help='key=value pairs')
    server = sub.add_parser('server', help='recover what was hosted: scan a repository or folder, restore sites').add_subparsers(dest='verb', required=True)
    scan = server.add_parser('scan', help='look for backups in the first repository, a folder or on this server'); scan.add_argument('--folder', help='a folder of backups on this server'); scan.add_argument('--local', action='store_true', help="this server's own copies only")
    server.add_parser('found', help='what the last scan found')
    restore = server.add_parser('restore', help='restore sites from the last scan as new sites, newest backup, recorded hostnames'); restore.add_argument('names', nargs='*', help='site names as found; none means every site'); restore.add_argument('--settings', action='store_true', help='also apply the recorded server settings')
    server.add_parser('recoveries', help='the recovery queue')
    server.add_parser('actions', help='downloads and deletions of backups from the Recover page')


def status():
    from .server_status import read
    summary = read() or {}
    if not summary: print('No summary yet: the worker writes one a minute after it starts.'); return
    cpu, mem, disk = summary.get('cpu') or {}, summary.get('memory') or {}, summary.get('disk') or {}
    gib = lambda v: f'{v / 2 ** 30:.1f} GiB' if v is not None else '–'
    print(f"{summary.get('hostname', '')} · {((summary.get('profile') or {}).get('name') or 'standard')} profile · figures from {summary.get('at', 0):.0f}")
    if cpu: print(f"cpu: load {cpu['load'][0]} of {cpu['cores']} cores, {cpu['pressure']}% waiting")
    if mem: print(f"memory: {gib(mem['used'])} used of {gib(mem['total'])}, {gib(mem['available'])} available")
    if disk: print(f"disk: {gib(disk['srv']['free'])} free of {gib(disk['srv']['total'])} on /srv; sites {gib(disk['sites_used'])}, backups {gib(disk['backups_used'])}")
    for key in ('backups', 'mail', 'sftp', 'update'):
        if summary.get(key) is not None: print(f'{key}: ' + json.dumps(summary[key]))
    rows = [{'site': s['name'], 'state': s['state'], 'health': s['health'], 'requests_24h': s.get('requests_24h', 0), 'disk': gib(s['disk_used']), 'memory': gib(s['memory'])} for s in sorted(summary.get('sites') or [], key=lambda s: s['name'])]
    print(); table(rows, ['site', 'state', 'health', 'requests_24h', 'disk', 'memory'])


def retry(row):
    """The site's first failed or interrupted operation, whatever its kind."""
    bad = ('failed', 'recovery-needed')
    if row['state'] in bad: return rpc({'op': 'retry', 'id': row['id']})
    for key, op in (('domain_job', 'retry-domains'), ('runtime_job', 'retry-runtime'), ('database_job', 'retry-database')):
        job = row.get(key)
        if job and job['state'] in bad: return rpc({'op': op, 'id': job['id']})
    for op, listing in (('retry-site-restore', 'site-restores'), ('retry-site-delete', 'site-deletes')):
        for job in rpc({'op': listing, 'site_id': row['id']}):
            if job['state'] in bad: return rpc({'op': op, 'id': job['id']})
    raise SystemExit('Nothing to retry for ' + row['name'])


def main():
    parser = argparse.ArgumentParser(prog='reeve', description='Reeve: the hosting panel from the command line.')
    sub = parser.add_subparsers(dest='noun', required=True)
    add_site(sub); add_others(sub)
    args = parser.parse_args()
    noun, verb = args.noun, getattr(args, 'verb', None)
    if noun == 'status': return status()
    if noun == 'site':
        if verb == 'list':
            rows = rpc({'op': 'list'})
            if args.json: return out(rows)
            return table([{'name': r['name'], 'domain': r['domain'], 'runtime': json.loads(r['payload']).get('runtime', 'static'), 'state': r['state'], 'health': r['health']['application'], 'id': r['id']} for r in sorted(rows, key=lambda r: r['name'])],
                         ['name', 'domain', 'runtime', 'state', 'health', 'id'])
        if verb == 'create':
            data = {'name': args.name, 'domain': args.domain, 'aliases': args.alias, 'runtime': args.runtime, **{k: getattr(args, k) for k in DEFAULTS if getattr(args, k) is not None}}
            if args.php_version: data['php_version'] = args.php_version
            if args.database: data['database'] = {'engine': args.database, 'series': args.database_series, 'exact': args.database_exact, **({'usage': args.database_usage} if args.database_usage else {})}
            return out(rpc({'op': 'create', 'id': str(uuid.uuid4()), 'data': data}))
        row = site_row(args.name) if verb != 'restore' else None
        if verb == 'retry': return out(retry(row))
        if verb == 'domains': return out(rpc({'op': 'domains', 'id': str(uuid.uuid4()), 'site_id': row['id'], 'domains': args.domains}))
        if verb == 'delete': return out(rpc({'op': 'site-delete', 'site_id': row['id'], 'id': str(uuid.uuid4())}))
        if verb == 'backup': return out(rpc({'op': 'site-backup', 'site_id': row['id'], 'id': str(uuid.uuid4())}))
        if verb == 'backups': return out(rpc({'op': 'site-backups', 'site_id': row['id']}))
        if verb == 'restores': return out(rpc({'op': 'site-restores', 'site_id': row['id']}))
        if verb == 'quiesce': return out(rpc({'op': 'site-backup-options', 'site_id': row['id'], 'quiesce': args.setting == 'on'}))
        if verb == 'restore': return out(rpc({'op': 'site-restore', 'snapshot': args.backup, 'name': args.name, 'domain': args.domain}))
    if noun == 'backup':
        if verb == 'status': return out(rpc({'op': 'backup-destination'}))
        if verb == 'copy': return out(rpc({'op': 'backup-remote', 'id': ''}))
        if verb == 'card': return out(rpc({'op': 'backup-card'}))
        if verb == 'connect-card': return out(rpc({'op': 'backup-connect-card', 'card': open(args.file).read()}))
        if verb == 'schedule': return out(rpc({'op': 'backup-schedule', 'site_id': site_row(args.name)['id'], 'interval': args.interval, 'enabled': not args.paused}))
    if noun == 'php':
        if verb == 'versions': return out(rpc({'op': 'versions'}))
        if verb == 'refresh': return out(rpc({'op': 'refresh-versions', 'id': str(uuid.uuid4())}))
        if verb == 'rebuild': return out(rpc({'op': 'php-rebuild', 'id': str(uuid.uuid4())}))
        row = site_row(args.name)
        if verb == 'switch': return out(rpc({'op': 'php-switch', 'id': str(uuid.uuid4()), 'site_id': row['id'], 'branch': args.branch}))
        if verb == 'rollback':
            previous = next((j for j in rpc({'op': 'list'}) if j['id'] == row['id']), row).get('runtime_job')
            if not previous or previous['state'] != 'succeeded': raise SystemExit('No successful PHP change to roll back')
            return out(rpc({'op': 'php-rollback', 'id': str(uuid.uuid4()), 'site_id': row['id'], 'previous': previous['id']}))
    if noun == 'db':
        if verb == 'versions': return out(rpc({'op': 'database-versions'}))
        if verb == 'refresh': return out(rpc({'op': 'refresh-databases', 'id': str(uuid.uuid4())}))
        row = site_row(args.name)
        if verb == 'add': return out(rpc({'op': 'add-database', 'id': str(uuid.uuid4()), 'site_id': row['id'], 'data': {'engine': args.engine, 'series': args.series, 'exact': args.exact, **({'usage': args.usage} if args.usage else {})}}))
        if verb == 'credentials': return out(rpc({'op': 'database-credentials', 'site_id': row['id']}))
        if verb == 'usage': return out(rpc({'op': 'content-submit', 'id': str(uuid.uuid4()), 'site_id': row['id'], 'kind': 'database-usage', 'data': {'usage': args.usage}}))
    if noun == 'mail':
        return out(rpc({'op': 'mail-setup' if verb == 'setup' else 'mail-status'}))
    if noun == 'update':
        from . import updates
        if args.check: return out(updates.check())
        if os.getuid() != 0: raise SystemExit('Run as root: sudo reeve update')
        return out(updates.apply(args.to))
    if noun == 'password':
        if os.getuid() == 0:  # the web account owns the store; root administration must not leave root-owned files
            account = pwd.getpwnam('hosting-web'); os.setgroups([]); os.setgid(account.pw_gid); os.setuid(account.pw_uid)
        from .auth import Auth
        secret = sys.stdin.readline(1026).rstrip('\n') if args.stdin else getpass.getpass('New operator password: ')
        Auth('/srv/ops/panel/web/auth.sqlite3').set_password(secret)
        return out({'password': 'changed; existing sessions revoked'})
    if noun == 'settings':
        if verb == 'show': return out(rpc({'op': 'settings'}))
        values = dict(v.split('=', 1) for v in args.values if '=' in v)
        return out(rpc({'op': 'settings-save', 'group': args.group, 'values': values}))
    if noun == 'server':
        if verb == 'scan':
            source = 'folder' if args.folder else 'local' if args.local else 'repository'
            rpc({'op': 'recover-scan', 'source': source, 'folder': args.folder or ''})
            import time
            for _ in range(600):
                scan = rpc({'op': 'recover-status'})['scan']
                if scan and scan['state'] in ('succeeded', 'failed'): break
                time.sleep(2)
            return out({k: scan.get(k) for k in ('state', 'source', 'error', 'unread')} | {'sites': [{'name': s['name'], 'backups': len(s['backups']), 'dumps': len(s['dumps']), 'live': bool(s.get('live')), 'domains': s['domains']} for s in scan.get('sites') or []], 'record': bool(scan.get('record'))})
        if verb == 'found': return out(rpc({'op': 'recover-status'})['scan'])
        if verb == 'recoveries': return out(rpc({'op': 'recover-status'})['recoveries'])
        if verb == 'actions': return out(rpc({'op': 'recover-status'})['actions'])
        if verb == 'restore':
            scan = rpc({'op': 'recover-status'})['scan']
            if not scan or scan['state'] != 'succeeded': raise SystemExit('Scan first: reeve server scan')
            if args.settings: out(rpc({'op': 'recover-settings'}))
            chosen = [s for s in scan['sites'] if (not args.names or s['name'] in args.names) and s['backups'] and not s.get('live')]
            missing = set(args.names) - {s['name'] for s in scan['sites']}
            if missing: raise SystemExit('Not found in the scan: ' + ', '.join(sorted(missing)))
            items = [{'backup': s['backups'][0]['id'], 'mode': 'new', 'name': s['name'], 'domains': ' '.join(s['domains'])} for s in chosen]
            if not items: raise SystemExit('Nothing to restore: every named site is live here already or has no complete backup')
            return out({'queued': rpc({'op': 'recover-submit', 'items': items}), 'sites': [s['name'] for s in chosen]})
    if noun == 'doctor':
        from .host import preflight
        result = {'preflight': preflight()}
        if args.repair:
            from .edge_setup import setup
            setup(); result['edge'] = 'rebuilt from the recorded routes'
        result['housekeeping'] = rpc({'op': 'housekeeping'})
        return out(result)
    parser.error('Unknown command')


if __name__ == '__main__':
    main()
