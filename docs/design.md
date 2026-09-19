# Design

Reeve manages one server. It uses Docker for workloads, XFS project quotas for storage and
Caddy as the shared HTTP proxy.

## Processes and permissions

The FastAPI web process runs as `hosting-web`. It handles login, pages and uploads, and
requests privileged work over a Unix socket. It cannot access site data or the Docker socket.

The root worker authenticates socket peers, validates each request and manages sites,
containers, configuration and backups. A separate timer-driven process copies backups to
remote storage so a slow upload does not block the worker.

## Recorded operations

Operations are recorded as jobs in SQLite. Jobs validate inputs, save relevant previous
state, apply changes and verify the result. Operations with rollback support restore the
previous state on failure.

Request IDs make retries recognisable: the same ID and inputs return the existing job;
different inputs with that ID are rejected. Interrupted jobs are marked for review at startup.
Content imports and site commands are not automatically replayed.

## Hosting

A managed site has a Linux identity, a quota over `/srv/sites/<name>` and a private container
network. nginx serves its content; PHP-FPM and a database are added when selected.
Generated configuration is root-owned. Site containers reach the shared Caddy proxy without
publishing their own host ports.

Compose applications use the supplied project with an overlay for networking, proxy routing
and storage. Named volumes sit under the site's quota. Host access and privileged configurations
are restricted, but applications do not receive all the managed-site container restrictions.

Proxy changes are validated before reload, with the previous configuration retained for
rollback. Routes use Caddy's own certificate authority or Let's Encrypt, as the certificate
setting says; with public certificates on, a name Let's Encrypt refuses is served with the
edge's own certificate until it succeeds. A domain change is the names and the routes,
recorded at once; certificates and the containers' view of the names follow as status.

## Backups and recovery

A complete backup contains a manifest, files, volumes, database dumps and site settings.
Restic copies backup artifacts to SFTP or S3. Restores create a site and refill it from the
backup; restores into an existing site take a safety backup first.

A server record (settings, release, every site with its hostnames and latest backup) is copied
to the repository beside the site backups. A replacement server needs the source, the
repository and its password; it scans the repository and restores the sites it finds. It does
not need the old worker's database. See [Recover](recover.md).

## Status and scope

The worker gathers resource use from the kernel, Docker and quotas, and aggregates traffic
from proxy logs. It writes a summary the web process can read while jobs are running.

Server profiles provide resource defaults that sites can override. Every server setting is
changed on the Settings page through the worker, validated by the code that consumes it, and
applied at once. A site's logs are read on demand from its containers and the edge's access
log, never stored twice. Secure access is one nftables table (input and forward chains, both
families) and a WireGuard interface the worker manages; the lockdown is taken only from over
the tunnel and reverts unless confirmed. DNS management, inbound mail and multi-server
orchestration are outside the panel's scope.
