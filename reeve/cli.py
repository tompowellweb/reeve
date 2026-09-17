import argparse
import getpass
import json
import os
import pwd
import sys
import uuid

from .core import DEFAULTS
from .worker import rpc


def main():
    parser = argparse.ArgumentParser(description="Hosting panel administration")
    sub = parser.add_subparsers(dest="op", required=True)
    sub.add_parser("preflight")
    sub.add_parser("list")
    for operation in ('recovery-status', 'recovery-inspect'):
        sub.add_parser(operation).add_argument('site_id')
    sub.add_parser('backup-remote')
    sub.add_parser('site-backup').add_argument('site_id')
    sub.add_parser('site-backups').add_argument('site_id')
    restore = sub.add_parser('site-restore'); restore.add_argument('snapshot'); restore.add_argument('--name', required=True); restore.add_argument('--domain', required=True)
    sub.add_parser('site-delete').add_argument('site_id')
    sub.add_parser('site-deletes').add_argument('site_id')
    sub.add_parser('retry-site-delete').add_argument('id')
    sub.add_parser('site-restores').add_argument('site_id')
    opts = sub.add_parser('site-backup-options'); opts.add_argument('site_id'); opts.add_argument('--quiesce', choices=('on', 'off'), required=True)
    sub.add_parser('retry-site-restore').add_argument('id')
    sub.add_parser('backup-status').add_argument('site_id')
    backup = sub.add_parser('backup-database'); backup.add_argument('site_id'); backup.add_argument('--request-id')
    schedule = sub.add_parser('backup-schedule'); schedule.add_argument('site_id')
    schedule.add_argument('--interval', type=int, choices=(15, 60), default=15)
    schedule.add_argument('--paused', action='store_true')
    sub.add_parser("build-php").add_argument("branch", nargs="?")
    sub.add_parser("database-versions")
    sub.add_parser("refresh-databases")
    sub.add_parser("retry-database").add_argument("id")
    sub.add_parser("database-credentials").add_argument("site_id")
    dbadd = sub.add_parser("add-database")
    dbadd.add_argument("site_id")
    dbadd.add_argument("engine", choices=("mysql", "mariadb", "postgres"))
    dbadd.add_argument("--series")
    dbadd.add_argument("--exact")
    sub.add_parser("php-rebuild")
    sub.add_parser("housekeeping")
    sub.add_parser("versions-init")
    sub.add_parser("content-init")
    sub.add_parser("versions")
    sub.add_parser("refresh-versions")
    switch = sub.add_parser("php-switch")
    switch.add_argument("site_id")
    switch.add_argument("branch")
    rollback = sub.add_parser("php-rollback")
    rollback.add_argument("site_id")
    rollback.add_argument("previous")
    sub.add_parser("retry-runtime").add_argument("id")
    create = sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("domain")
    create.add_argument("--alias", action="append", default=[], help="Additional hostname; repeat for each alias")
    create.add_argument("--request-id", default=None)
    create.add_argument("--runtime", choices=("static", "php"), default="static")
    create.add_argument("--php-version")
    create.add_argument("--database", choices=("mysql", "mariadb", "postgres"))
    create.add_argument("--database-series")
    create.add_argument("--database-exact")
    for key in DEFAULTS:
        create.add_argument("--" + key.replace("_", "-"), type=float if key == "cpus" else int)
    retry = sub.add_parser("retry")
    retry.add_argument("id")
    domains = sub.add_parser("domains", help="Replace the site's full domain list; first hostname is primary")
    domains.add_argument("site_id")
    domains.add_argument("domains", nargs="+")
    domains.add_argument("--request-id")
    sub.add_parser("retry-domains").add_argument("id")
    password = sub.add_parser("set-password")
    password.add_argument("--stdin", action="store_true", help="Read a password from a private pipe")
    sub.add_parser("mail-setup")
    sub.add_parser("mail-status")
    edge = sub.add_parser("edge-setup")
    edge.add_argument("--take-over-proof", action="store_true")
    args = parser.parse_args()
    if args.op == 'backup-remote':
        result = rpc({'op': args.op})
    elif args.op in ('site-backup', 'site-delete'):
        result = rpc({'op': args.op, 'site_id': args.site_id, 'id': str(uuid.uuid4())})
    elif args.op in ('site-backups', 'site-deletes', 'site-restores'):
        result = rpc({'op': args.op, 'site_id': args.site_id})
    elif args.op == 'site-restore':
        result = rpc({'op': args.op, 'snapshot': args.snapshot, 'name': args.name, 'domain': args.domain})
    elif args.op == 'site-backup-options':
        result = rpc({'op': args.op, 'site_id': args.site_id, 'quiesce': args.quiesce == 'on'})
    elif args.op in ('retry-site-delete', 'retry-site-restore'):
        result = rpc({'op': args.op, 'id': args.id})
    elif args.op == 'backup-database':
        result = rpc({'op': args.op, 'site_id': args.site_id, 'id': args.request_id or str(uuid.uuid4())})
    elif args.op == 'backup-schedule':
        result = rpc({'op': args.op, 'site_id': args.site_id, 'interval': args.interval, 'enabled': not args.paused})
    elif args.op in ('recovery-status', 'recovery-inspect', 'backup-status'):
        result = rpc({'op': args.op, 'site_id': args.site_id})
    elif args.op == "preflight":
        from .host import preflight
        result = preflight()
    elif args.op == "build-php":
        from .php_runtime import build
        result = build([args.branch] if args.branch else None)
    elif args.op == "content-init":
        from .content_site import tools_image
        result = {"image": tools_image()}
    elif args.op == "versions-init":
        from .versions import read, refresh
        result = read()
        if not result.get("checked_at"):
            result = refresh()
        result = {"branches": len(result["branches"]), "checked_at": result["checked_at"]}
    elif args.op == 'add-database':
        result = rpc({'op': args.op, 'id': str(uuid.uuid4()), 'site_id': args.site_id,
                      'data': {'engine': args.engine, 'series': args.series, 'exact': args.exact}})
    elif args.op == 'database-credentials':
        result = rpc({'op': args.op, 'site_id': args.site_id})
    elif args.op in ('database-versions', 'refresh-databases'):
        result = rpc({'op': args.op, **({'id': str(uuid.uuid4())} if args.op == 'refresh-databases' else {})})
    elif args.op in ("versions", "refresh-versions", "php-rebuild"):
        result = rpc({"op": args.op, **({"id": str(uuid.uuid4())} if args.op != "versions" else {})})
    elif args.op == "php-switch":
        result = rpc({"op": args.op, "id": str(uuid.uuid4()), "site_id": args.site_id, "branch": args.branch})
    elif args.op == "php-rollback":
        result = rpc({"op": args.op, "id": str(uuid.uuid4()), "site_id": args.site_id, "previous": args.previous})
    elif args.op in ("mail-setup", "mail-status", "housekeeping"):
        result = rpc({"op": args.op})
    elif args.op == "edge-setup":
        from .edge_setup import setup
        setup(args.take_over_proof)
        result = {"edge": "installed"}
    elif args.op == "set-password":
        # Always write the web DB as its owner, so root administration cannot leave root-owned journals.
        if os.getuid() == 0:
            account = pwd.getpwnam("hosting-web")
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
        from .auth import Auth
        secret = sys.stdin.readline(1026).rstrip("\n") if args.stdin else getpass.getpass("New operator password: ")
        Auth("/srv/ops/panel/web/auth.sqlite3").set_password(secret)
        result = {"password": "changed; existing sessions revoked"}
    elif args.op == "create":
        data = {"name": args.name, "domain": args.domain, **{key: getattr(args, key) for key in DEFAULTS if getattr(args, key) is not None}}
        if args.database:
            data["database"] = {"engine": args.database, "series": args.database_series, "exact": args.database_exact}
        data["runtime"] = args.runtime
        data["aliases"] = args.alias
        if args.php_version:
            data["php_version"] = args.php_version
        result = rpc({"op": "create", "id": args.request_id or str(uuid.uuid4()), "data": data})
    elif args.op == "domains":
        result = rpc({"op": "domains", "id": args.request_id or str(uuid.uuid4()), "site_id": args.site_id, "domains": args.domains})
    elif args.op in ("retry", "retry-domains", "retry-runtime", "retry-database"):
        result = rpc({"op": args.op, "id": args.id})
    else:
        result = rpc({"op": "list"})
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
