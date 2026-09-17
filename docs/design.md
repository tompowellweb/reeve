# Design

## Two processes, one privilege boundary

Reeve is a web UI and a worker.

The **web process** runs as `hosting-web`, an unprivileged system account, under a hardened
systemd unit: no capabilities, no new privileges, the site data, Docker socket, worker state and
edge folders made inaccessible. It renders pages, checks the operator's password (Argon2, with
throttling after failed attempts) and session cookie (HttpOnly, SameSite strict, CSRF token on
every mutation, same-origin checks, a strict content security policy), streams uploads into its
own spool, and asks the worker to do anything else.

The **worker** runs as root, serves a Unix socket whose peer credentials it checks (root or the
web account only), and accepts one fixed message shape per operation, listed in a table at the
top of `reeve/worker.py`. Everything privileged is a function in the worker's modules: Docker,
quotas, files owned by sites, the edge, backups. The web process never reads a site's files and
never runs a command. A third unit runs the off-machine copies on a timer as its own process, so
a stuck upload never stalls the worker.

## Durable jobs

Every operation that can fail half-way is a row in a SQLite ledger (`/srv/ops/panel/worker/`)
with a state, a step and an error. Creates, domain changes, PHP switches, database setups,
content operations, backups, restores and deletes each have their own table; smaller settings
(routing profile, site rules, PHP limits, SFTP keys, allowed senders, database usage) share the
content-job table with a kind. A job is submitted with a request id the client chooses, so a
repeated submission with the same inputs returns the same job and one with different inputs is
refused.

Each kind follows the same shape: **validate** the inputs before touching anything; **apply**
with the previous state saved first; **verify** the result (a candidate nginx configuration
under `nginx -t`, a container's health, the site's hostnames over HTTPS); **roll back** on any
failure; and at worker start **recover** any job left running, which marks it for review rather
than replaying it. Startup recovery runs every module's recover under one guard, so a module
that cannot recover reports it and the worker still starts. Content operations, imports and
site commands are never replayed automatically: an interrupted one waits for an operator to
read its output.

## Sites

A site is a numeric identity (from 30000), an XFS project (from 100000) with a hard quota over
`/srv/sites/<name>`, and one to three containers on a private internal network: nginx serving
`html/` read-only, PHP-FPM as the site identity, a database as the image's own user. Each site's
configuration (`hosting.yaml`, the Compose file, nginx and PHP files) is root-owned and
generated; the operator's deliberate settings (site rules, PHP limits, the Containerfile, the
environment file) survive retries and are carried by backups. Containers drop all capabilities,
run with no new privileges, get Docker's local log driver with a size cap, and publish no ports.

The **edge** is one Caddy container with the routes rendered from a root-owned route manifest,
validated before an atomic replacement and a live reload, with the previous configuration
restored on a failed reload. It terminates TLS (a local CA on a test machine; public TLS is the
deployment's setting), writes a rolling JSON access log per hostname, and reaches each site over
an ingress network. The worker reconciles the edge's configuration with what the release renders
at every start, so a change in the template reaches an existing machine.

A **Compose package** is the second hosting mode: the project runs as its author wrote it, with
a hosting overlay that replaces published ports and container names with a private project
identity and Caddy routing, puts named volumes under the site's quota, and refuses only host
hazards. Its images are built or pulled as declared and never upgraded by the panel.

Infrastructure containers (the edge, the mail relay, the SFTP server) are shared, run from
pinned images, and are recreated rather than edited: their generated configuration lives in
root-owned folders mounted whole, and files bind-mounted into a running container are updated
in place, never replaced by rename, because a renamed file is a stale inode inside the container.

## Backups and recovery

Recovery is designed backwards from an empty machine. A **database dump** is a private artifact
under the local backup root with a checksum manifest, made by the engine's own client in a
disposable container. A **complete site backup** is a folder with a manifest, the files as a
tar, each volume as a tar, a fresh dump, and the site's configuration and settings; a restore is
always a fresh-folder create followed by a refill, so a restore into a new site never touches an
existing one, and an in-place restore takes a safety backup first. **Off-machine copies** are
restic snapshots tagged by site, uploaded and then read back to verify, with the same retention
applied to the repository as to local copies. The destination and its secrets are files under
the worker's private folder that the Backups page writes; the uploader reads only those files.

Recovery needs the source, the repository and its password, nothing from the old machine: the
installer rebuilds the foundation and the release, the Backups page the repository access, and
the restore script the sites. The measured path is a few minutes.

## Updates

A site pins a PHP branch. Patch releases within it arrive by a weekly rebuild of the branch image
from the same recipe with fresh packages; an unchanged package list keeps the image, a changed
one replaces it with the previous image kept, and each site rolls onto it through the same
switch mechanism as a branch change, one at a time, with verification and rollback. Base images
are pinned by digest and move with releases. Nothing else updates by itself.

## Observability without agents

The worker writes one world-readable summary a minute: load and pressure from the kernel, memory
from `/proc`, disk from statvfs and the quota report, Docker's own accounting of images and
build cache, each site's containers from one listing and their cgroup files, and traffic from
the edge's access logs aggregated into hourly buckets in the ledger. The web process reads the
summary directly, so the home page works while the worker is busy and says how old its figures
are. There is no metrics endpoint, no exporter and no per-request storage.

## Server profile

One word in the settings, `small`, `standard` or `large`, sets the defaults for the box's size:
PHP memory per request and workers per site, the memory cap a new site gets, the usage a new
database gets. It is only defaults; every site can override; MariaDB is offered first on every
profile because it idles at a tenth of MySQL 8's stock footprint.

## What is deliberately not here

No DNS management, no public TLS policy inside the panel, no firewall management, no inbound
mail, no per-URL analytics, no memory scheduler, no multi-server. Each is either the deployment's
setting or a product of its own.
