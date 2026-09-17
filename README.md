# Reeve

A small hosting panel for one Debian server: static and PHP sites, optional databases, and
Docker Compose applications, each in its own confined containers behind one Caddy edge, with
backups that restore on a clean machine.

A reeve ran an estate and answered for its tenants. This one runs a box.

## What it does

- **Sites.** Static or PHP (7.0 to 8.5, branch-pinned images built from Surý packages), one
  Linux identity and one XFS hard quota per site, nginx and PHP-FPM in separate containers, an
  optional MariaDB, MySQL or PostgreSQL server per site.
- **Compose applications.** A packaged Compose project runs as supplied, behind the same edge,
  under the same quota, with its databases dumped on the same schedule.
- **Backups.** Database dumps every 15 minutes, a complete site backup nightly, retention by
  age, an off-machine copy to SFTP or Amazon S3 through restic, restore to a new site or into
  the same one, and a deleted site's final backup kept.
- **Operations.** Per-site domains and routing rules, PHP limits, scheduled commands, WP-CLI, a
  site SSH toolbox, key-only customer SFTP (SFTPGo), a send-only mail relay, weekly PHP patch
  rebuilds with rollback, bounded logs, and a home page that shows the server's load, memory,
  disk and each site's traffic.
- **Recovery.** From an empty Debian machine to serving sites in a few minutes with the source,
  the backup repository and its password. Nothing else from the old server is needed.

## How it is built

Two processes: a web UI (FastAPI, no JavaScript framework, runs as an unprivileged user) and a
root worker that owns every privileged operation, reached over a Unix socket with peer
credentials. Every operation that can fail half-way is a durable job in a SQLite ledger with
validate, apply, verify and rollback steps, so a crash mid-operation leaves a site serving and
a job marked for review, never a half-changed site. There is no agent in the containers and no
daemon beyond the two processes; the figures on the home page come from the kernel, Docker and
the quota report.

## Documents

- [Install](docs/install.md): an empty Debian 13 machine to a running panel.
- [Operate](docs/operate.md): sites, domains, PHP, databases, backups, SFTP, mail, updates.
- [Recover](docs/recover.md): the runbook for a lost machine.
- [Design](docs/design.md): the architecture, the security model, the job pattern.
- [Decisions](docs/decisions.md): rules learned from real failures, kept on purpose.
- [Develop](docs/develop.md): tests, releases, the rules for changing it.

## Status

Reeve is in use on a test server with real workloads rehearsed through it, ahead of a
production migration. The public-facing gates (TLS with real DNS, firewall policy, real mail
delivery) are validated per deployment; the panel itself is complete for the workloads it was
built for. No licence has been chosen yet; all rights reserved until one is.
