# Reeve

Reeve is a hosting panel for a single Debian server. It manages static sites, PHP sites
and Docker Compose applications, with databases, backups and file access from a web interface.

- Create sites with their own containers, domains and disk quotas.
- Choose a PHP version and an optional MariaDB, MySQL or PostgreSQL database.
- Upload files, use SFTP and run site tools or scheduled commands.
- Back up sites locally and to SFTP or Amazon S3, and restore them on a replacement server.
- Monitor server resources, site health, traffic and outgoing mail.

Reeve targets Debian 13 on amd64, with Docker and XFS project quotas. The installer sets up
these dependencies. Administration is through an SSH tunnel or VPN.

## Documentation

- [Install](docs/install.md) — set up a server and sign in.
- [Host a Compose application](docs/compose.md) — prepare an application, package its data and deploy it.
- [Operate](docs/operate.md) — manage sites, backups and updates.
- [Recover](docs/recover.md) — restore sites after losing a server.
- [Design](docs/design.md) — understand the architecture.
- [Decisions](docs/decisions.md) — understand the main engineering constraints.
- [Develop](docs/develop.md) — run tests and prepare changes.

## Project status

Reeve is being tested with real workloads ahead of a production migration. Site HTTPS
currently uses a local certificate authority; public TLS, DNS, firewall rules and mail
delivery need deployment work.

No licence has been selected. All rights reserved.
