"""Install-time steps the installer runs inside the release's environment. Not the operator's command line."""
import json
import os
import pwd
import secrets
import sys


def main():
    step = sys.argv[1] if len(sys.argv) > 1 else ''
    if step == 'backup-root':
        from .host import backup_root
        print(backup_root())
    elif step == 'preflight':
        from .host import preflight
        print(json.dumps(preflight()))
    elif step == 'versions-init':
        from .versions import read, refresh
        result = read()
        if not result.get('checked_at'): result = refresh()
        print(json.dumps({'php_branches': len(result['branches']), 'checked_at': result['checked_at']}))
    elif step == 'content-init':
        from .content_site import tools_image
        print(json.dumps({'tools_image': tools_image()}))
    elif step == 'edge':
        from .edge_setup import setup
        setup()
        print(json.dumps({'edge': 'installed'}))
    elif step == 'mail':
        # Mail defaults to on: the relay is deployed on a fresh machine and refreshed, not restarted, on a reinstall.
        from .core import Ledger
        from .host import Host
        from .mail import php_sites, refresh, install_shim
        ledger = Ledger('/srv/ops/panel/worker/jobs.sqlite3'); host = Host()
        install_shim()
        print(json.dumps(refresh(host, php_sites(ledger))))
    elif step == 'list':
        from .worker import rpc
        print(json.dumps({'sites': len(rpc({'op': 'list'}))}))
    elif step == 'password-if-missing':
        # Runs as the web account, which owns the authentication store. Prints the new password once, or nothing.
        if os.getuid() == 0:
            account = pwd.getpwnam('hosting-web'); os.setgroups([]); os.setgid(account.pw_gid); os.setuid(account.pw_uid)
        from .auth import Auth
        auth = Auth('/srv/ops/panel/web/auth.sqlite3')
        if not auth.has_password():
            password = secrets.token_urlsafe(18)
            auth.set_password(password)
            print(password)
    else:
        raise SystemExit('Unknown setup step')


if __name__ == '__main__':
    main()
