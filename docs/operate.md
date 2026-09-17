# Operate

The panel from the operator's chair: what each page does, what the worker does behind it, and
the commands that do the same from the server. Every command below runs on the server as root
through `reeve`, which calls the same worker the pages use.

## The home page

The home page is the server. Six cards show the box: **CPU** (load and the share of the last
minute tasks waited for CPU), **Memory** (used of total, available, swap in use), **Disk left**
(free space on `/srv`, with what sites, backups and Docker's images and build cache hold, how
much of Docker's is reclaimable, and the system disk), **Backups** (the destination's state,
copies pending, the last copy), **Mail** (queued, mode, the last hour's and today's counts) and
**Customer SFTP** (the sites on). The sites table carries each site's setup state, application
health, last day's traffic, disk used of its quota, memory held and CPU share, and its backup
summary. The worker gathers all of it once a minute from the kernel, Docker and the quota report
and writes a summary the web process reads directly, so the page stays current while the worker
is busy and says "Figures as of HH:MM". The hostname and the server profile head the page.

The masthead's sections are **Sites** (home), **Backups** (the destination), **Mail**, **PHP**
(the version catalogue and rebuilds), **Databases** (the engine catalogue) and **History**
(deleted sites). The footer names the running release and the previous one kept for rollback.

## Sites

**Create site** takes a name, a primary hostname and up to 19 more, static or PHP with a branch
from the catalogue, an optional database, and optional caps (memory, CPU, writable layer,
processes). Only the site's data quota has a built-in default. Creation is a durable job: a
closed page does not cancel it, and a failed or interrupted setup offers **Retry setup**, which
reuses the same identity, quota and content.

```sh
reeve create example example.com --alias www.example.com
reeve create legacy legacy.example.com --runtime php --php-version 7.0 --memory-mb 256
reeve create shop shop.example.com --runtime php --php-version 8.4 --database mariadb
reeve list
reeve retry <request uuid>
```

Each site gets a numeric identity from 30000 and an XFS project from 100000; its files live
under `/srv/sites/<name>/html`, its configuration root-owned beside them. nginx serves content
read-only; PHP-FPM runs as the site identity in its own container on the site's internal
network; a database is a third container on the same network, reachable as `db`. No container
publishes a port; the edge reaches the web container over an ingress network.

The site page shows services, domains and routing, files and access, scheduled tasks, storage,
traffic, backups and recent activity, and refreshes every 30 seconds while open. Pages below it:
Files and tools, the SSH toolbox, Backups, and each operation's output.

### Domains and routing

**Domains** replaces the site's full hostname list; the first is primary. Names are reserved the
moment they are asked for, a name cannot belong to two sites, and a failed change offers a retry
that reconciles the saved set. Removing a name removes its route, never data.

**Site rules** edits the site's own nginx directives (`conf/site.nginx.conf`), included before
the template's locations: redirects, rewrites, `try_files` fallbacks, an `error_page`. The
candidate is checked with `nginx -t` as the site's identity in a throwaway container, the web
container is recreated, the site's names are verified over HTTPS, and any failure restores the
previous rules with nginx's message in the job output. Backups carry the rules.

```sh
reeve domains <site uuid> example.com www.example.com
reeve retry-domains <change uuid>
```

### PHP

PHP sites run branch-pinned images (7.0 through 8.5) built from Debian and Surý packages with
the common extensions, matching CLI, database clients and the `mail()` shim. **Change PHP**
builds and checks the replacement image before a brief FPM restart and keeps the exact previous
image for **Restore previous PHP runtime**. A generic check cannot prove an application likes a
new branch; that is the operator's test.

**PHP limits** are the five values customers ask for: execution time, largest upload, largest
POST, input variables and memory per request. Defaults are 120 s, 128 MB, 136 MB, 3000 and the
profile's memory per request (512 MB on standard). They render `php.ini` together with the nginx
body size and FastCGI timeout that must agree with them, as a durable job with rollback, and
backups carry them.

The pool runs on demand: an idle PHP site holds one master process. The number of workers is the
profile's (8 on standard, 3 on small) or, for a memory-capped site, derived from the cap.

```sh
reeve php-switch <site uuid> 8.3
reeve php-rollback <site uuid> <previous change uuid>
reeve versions
```

### Databases

One optional server per site: MariaDB (offered first), MySQL or PostgreSQL, a series from the
cached official catalogue or an exact release, resolved to an immutable digest before anything
starts. Data lives under the site's quota, owned by the image's own user; the application account
and database are created once and never reset on retry. **Credentials** shows the application
account; PHP also receives it as `DATABASE_*` variables.

Each database has a **usage**: light (64 MiB buffer pool, 40 connections), standard (256 MiB,
150) or high (1 GiB, 300, MySQL's performance schema on), defaulting from the server profile.
The binary log is off everywhere because recovery is the panel's dumps. Changing the usage
recreates that one container, a few seconds without the database, and rolls back if it does not
come back healthy.

A PHP 7.0 or 7.1 site with MySQL gets a native-password application user and must use the 8.0
series or MariaDB, because that PHP generation cannot speak MySQL 8's default authentication.

```sh
reeve add-database <site uuid> mariadb --series 11.8
reeve database-credentials <site uuid>   # prints the secret; private terminal only
reeve database-versions
```

### Files and tools

**Upload** copies a file or unpacks a ZIP or tar into the content root, keeping existing files
unless told to replace them; limits are 512 MiB per upload, 20,000 entries and the smaller of the
quota or 4 GiB expanded. Unsafe paths, links and special files are refused. The file list opens
folders and edits text files up to 64 KiB with a digest check against concurrent edits.

**Import SQL** loads a plain single-database dump into the site's database as the application
account. **Run a site tool** offers the site's PHP, Composer, Symfony console, WP-CLI and a shell,
all as the site identity inside a container, with optional outgoing network access; output is
private and capped at 64 KiB. **Scheduled tasks** run the same tools on an interval with no
overlap and no replay backlog; a WordPress site typically schedules `cron event run --due-now`.

**Fix ownership** makes everything under `html/` the site user's with sane modes, for content
uploaded as an administrator over SSH.

The **SSH toolbox** builds a container from a Dockerfile recipe (the supplied one starts from the
site's PHP image and adds OpenSSH, Git, unzip and Composer), mounts the site content at `/site`
and a retained home, and offers SSH as the site identity through the server's SSH as a jump.
Stop before other changes to the site.

### Customer SFTP

Key-only SFTP into the site's `html/` as the site user, for legacy work such as a plugin
uploaded the old way. One shared SFTPGo container serves every site that is on, on port 2222,
login is the site name, no shell, no forwarding, no passwords, each login confined to its own
folder. **Turn on** makes the site's own key and stays on until turned off (a duration can be
chosen instead); the page hands out the private half; a developer's public key can be added as
a secondary; **Rotate** replaces the site key. SFTPGo's banner names the version that runs, so
a scanner sees the truth rather than Debian's unchanging OpenSSH banner. The port's allow-list
and fail2ban are firewall policy outside the panel.

### Mail

One send-only Postfix relay, reached from every PHP site as `mail` on port 25 with no
authentication: PHP `mail()` uses it through the images' sendmail shim without changing the
application, and an application's own SMTP settings can point at `mail` too. The relay accepts
a sender only at the site's hostnames plus its **Allowed senders**, and limits each site to
`mail.rate_per_hour` messages an hour. The **Mail** page shows the queue with reasons and a Drop
button, per-site sent, deferred, bounced and rate-limited counts for the hour, day and month, and
the relay log. A site that sends more than it should stands out there and on the home card.

`mail.mode` is `direct`, `relay` (through `relayhost`), `sink` (a local Mailpit for a test
server) or `"off"` (no relay; the shim fails with a clear message). Each site page offers the
SPF record to publish. Deliverability beyond SPF, such as DKIM or a paid relay, is the site
owner's choice; `relay` mode carries it for every site at once.

```sh
reeve mail-setup     # after changing mail settings
reeve mail-status
```

## Backups

### What runs by itself

- **Database dumps** every 15 minutes (or hourly, or paused) per database, with the native
  client in a disposable container; MySQL and MariaDB hold a read lock for the copy, PostgreSQL
  uses its snapshot. Only the site's own database is in a dump.
- **Complete site backups** nightly at `site_backups.hour`: files, volumes, a fresh dump, and the
  site's configuration and settings (domains, rules, PHP limits, SFTP keys, allowed senders). A
  site can be paused around the copy for an application-consistent backup.
- **Off-machine copies** hourly through restic to the connected destination, verified by reading
  back. **Retention**, the same locally and remotely: dumps two days, complete backups everything
  within two days, then daily to a week, weekly to a month, monthly to a year; final and imported
  backups never expire.

### The destination

The **Backups** page connects one destination for the server: SFTP with the server's own key
(shown on the page to authorise on the destination) or a password, or Amazon S3 with a key pair.
Connecting probes and pins the host key, writes the secrets as private root files, opens or
initialises the repository, and shows the repository password once; keep it in a password
manager under the repository id, because [recovery](recover.md) needs it. The page also shows
where local backups live (`backups.local_path`, `/srv/backups` by default) with the space they
use and what is free. Pause, resume and disconnect are the other actions.

### A site's backups

The site's **Backups** page lists every backup with its contents: **Download** (a six-hour
web-readable copy), **New site**, **Files into this site** and **Database into this site** (a
safety backup is taken first), and **Import a backup** from elsewhere (a panel backup, or site
content plus a dump; a MySQL dump must be a plain single-database export). **Delete site** takes
a final backup and keeps it; **History** lists deleted sites and restores them.

```sh
reeve site-backup <site uuid>
reeve site-backups <site uuid>
reeve site-restore <backup uuid> --name new-name --domain new.example.com
reeve backup-database <site uuid>
reeve backup-status <site uuid>
```

## Compose applications

**Import application** takes one ZIP or tar holding a Compose project, its files, Dockerfiles
and dumps, plus the site name, hostname and which service and port to route. **Upload and
review** keeps the archive and reports what it found; **Deploy application** runs the project as
supplied: images built or pulled, named volumes under the site's quota, recognised MySQL,
MariaDB and PostgreSQL services restored once from `dumps/<service>.*`, the chosen service
routed. Only host hazards are refused (published ports, host paths, privilege). Container logs
are bounded. The application's databases join the dump schedule; its backups and restores work
like a managed site's, rebuilding from the Dockerfiles on restore. Image builds run in a lane
beside the serial worker, up to 30 minutes.

An agent can do the intake through a token from **Agent access**, valid 30 days, with only that
scope.

## Updates and logs

- **PHP patch releases within a branch: automatic.** Every `updates.every_days` at
  `updates.hour` the worker rebuilds each branch image with fresh packages, keeps an unchanged
  branch, replaces a changed one (the previous image kept), and rolls each site onto it one at a
  time with the same validation and rollback as a branch change. The PHP page shows the last run
  and **Rebuild now**. The mail and SFTP images move the same way, or with the panel's pins.
- **A branch change, a database series change, application code, a Compose package's images:
  never automatic.**
- **Logs are bounded where they are written**: container logs by Docker's local driver, the
  edge's per-hostname access logs by rolling for 30 days, the relay's log in its spool, the
  journal by its cap, and the panel's own outputs by a daily housekeeping pass.

```sh
reeve php-rebuild
reeve housekeeping
```

## Traffic

Each request the edge handles goes into an hourly bucket for its site: requests, bytes, 2xx and
3xx, 4xx, 5xx, and requests slower than a second, read from the edge's access logs once a minute
by offset, kept 30 days in the ledger. Nothing per request is stored and no client address. The
home page shows the last day per site; the site page shows the day by hour and the month by day,
with an hour or day that had a server error marked.

## Settings and profile

`/srv/ops/server.yaml` holds the server's settings; `config/server.example.yaml` documents every
key. `profile` is `small`, `standard` or `large`: a set of defaults for PHP memory and workers,
the memory cap a new site gets and the usage a new database gets. A profile is never a limit on
how many sites a box hosts; a small box is expected to host a few.

## When something is wrong

- **A job says recovery-needed.** The worker was interrupted mid-operation. The site is still
  serving what it served before. Read the job's output, then **Retry** (creates and domain
  changes reconcile the saved request) or mark it reviewed (content operations are never
  replayed automatically).
- **The home page says the worker is busy.** A long job (a rebuild, a package build) holds it;
  the page shows the last summary and says so. Nothing is lost.
- **A site is unhealthy.** The site page shows which container: web, PHP or database. Recent
  activity has the last operations; `journalctl -u reeve-worker` has the worker's log;
  `docker compose -f /srv/sites/<name>/compose.yml ps` the containers.
- **The quota is full.** Use the shell tool without outgoing network to remove content; the hard
  limit stays enforced. Do not raise a quota silently.
- **The edge lost a route.** `reeve edge-setup` rebuilds the edge from the recorded routes, and
  the worker reconciles the edge's configuration with the release's at every start.

```sh
reeve preflight
journalctl -u reeve-worker -u reeve-web -n 100 --no-pager
xfs_quota -x -c 'report -p -n -h' /srv
```
