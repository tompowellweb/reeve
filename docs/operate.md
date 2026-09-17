# Operate

Use the panel for everyday administration. The commands below run on the server and provide
shortcuts for common tasks. Use `sudo reeve --help` for the full command list.

## Check the server

The home page shows resource use, backup status, mail and the sites on the server.
Each site shows its health, traffic and disk usage against its quota. Check the timestamp
on the figures: the page can display the last collected summary while the worker is busy.

## Deploy a Compose application

Use **Import application** for an application with its own Docker Compose setup. Reeve
builds or pulls its images, restores supplied database dumps, and routes a hostname to its
HTTP service. Uploading the project reviews it; deployment is a separate step.

[Host a Compose application](compose.md) walks through preparing the folder, including
configuration and existing data, creating the archive, and deploying it. It includes a
complete WordPress/MariaDB example and explains the supported formats and current limits.

## Create a managed static or PHP site

Choose **Create site**, enter a name and hostname, then select static or PHP hosting.
For PHP, choose a branch and optionally a database. Set the disk quota and any resource limits.

```sh
sudo reeve site create example example.com --alias www.example.com
sudo reeve site create shop shop.example.com --runtime php --php-version 8.4 --database mariadb
sudo reeve site list
```

Creation continues if you close the page. If it fails, read the operation's output before
using **Retry setup**. Site content lives at `/srv/sites/<name>/html`.

## Change domains, PHP or databases

**Domains** replaces the complete hostname list; put the primary name first and include
every alias you want to keep. **Site rules** accepts nginx redirects and routing rules;
Reeve checks the configuration before applying it.

```sh
sudo reeve site domains shop shop.example.com www.shop.example.com
```

Use **Change PHP** to switch branches and **PHP limits** to adjust memory, uploads and
timeouts. Test the application after switching; container health does not prove compatibility.
The previous runtime is available for rollback.

```sh
sudo reeve php switch shop 8.3
sudo reeve php rollback shop
```

A managed site can have one MariaDB, MySQL or PostgreSQL server. **Credentials** shows its
application login; PHP receives the connection settings as `DATABASE_*` variables.
Changing database usage restarts that database briefly. PHP 7.0 and 7.1 sites using MySQL
require the 8.0 series with legacy authentication, or use MariaDB.

## Files and access

Use **Files and tools** to upload files or archives, edit text, import a single-database SQL
dump, and run PHP, Composer, WP-CLI or shell commands. Uploads are limited to 512 MiB.
**Scheduled tasks** runs site commands at intervals without overlapping runs.
Use **Fix ownership** after uploading content as an administrator.

**Customer SFTP** provides key-only access to the site's content on port 2222, using the
site name as the login. Enable it on the site page, save the generated key or add a developer's
public key, and turn it off when no longer needed.

The **SSH toolbox** provides a shell through the server's SSH connection. Stop the toolbox
before making other changes to the site.

## Backups and restores

Connect an SFTP or Amazon S3 destination on the server's **Backups** page. Save the repository
password outside the server. For SFTP, also keep the destination's host key fingerprint and
a way to authorise access from a replacement machine.

The default schedule is:

| Backup | Frequency | Retention |
| --- | --- | --- |
| Database dump | Every 15 minutes | Two days |
| Complete site backup | Nightly | All for two days, then daily to a week, weekly to a month, monthly to a year |
| Copy to SFTP or S3 | Hourly | Same retention as local backups |

Complete backups contain site files, volumes, a fresh database dump and site settings.
Enable pausing during backup when the application needs writes stopped for a consistent copy.
Final backups from deleted sites and imported backups do not expire automatically.
Local backups use `/srv/backups` unless `backups.local_path` is changed.

A site's **Backups** page offers download, import, restore to a new site, and restore of
files or a database into the current site. Restoring into the current site takes a safety
backup first. **History** lists deleted sites and their retained backups.

```sh
sudo reeve site backup shop
sudo reeve site backups shop
sudo reeve backup status
sudo reeve backup copy
```

See [Recover](recover.md) for a replacement server. Check that remote copies succeed and
rehearse a restore before relying on them.

## Outgoing mail

PHP sites send through the shared relay using `mail()` or SMTP at `mail:25`.
Use **Allowed senders** for sender domains beyond the site's hostnames. The **Mail** page
shows the queue, delivery failures and per-site counts.

In `/srv/ops/server.yaml`, `mail.mode` selects `direct`, `relay`, `sink` for testing, or
`"off"`. Relay mode also needs `mail.relayhost`. Set `mail.hostname` and `mail.public_ip`
for the SPF guidance shown in the panel. After editing, restart the worker and apply the setup:

```sh
sudo systemctl restart reeve-worker
sudo reeve mail setup
```

Public delivery also depends on the server's mail and DNS configuration.

## Updates

Reeve reports new releases but installs them only when requested:

```sh
sudo reeve update --check
sudo reeve update
```

Use `sudo reeve update --to <version>` for a specific release, including a compatible
previous version. An update restarts the panel services; it does not roll back site content.

PHP patch rebuilds run weekly by default, with verification and a previous image retained
for rollback. Mail and SFTP images are also rebuilt in that cycle. PHP branch changes,
database series changes, application code and Compose application images require operator action.

## Troubleshooting

Read the site's recent activity and the failed operation's output first.

- **Recovery needed:** an operation was interrupted. Inspect the site and job output before
  retrying. Content imports and site commands are not replayed automatically.
- **Unhealthy site:** identify whether web, PHP or the database failed on the site page.
- **Quota full:** remove unneeded content or deliberately increase the quota.
- **Missing proxy route:** `sudo reeve doctor --repair` rebuilds the proxy from recorded routes.

```sh
sudo reeve status
sudo journalctl -u reeve-worker -u reeve-web -n 100 --no-pager
```
