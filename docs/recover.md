# Recover

Use this guide to restore sites onto a replacement Debian 13 server. For an individual
site restore on a working server, use its **Backups** page.

## Keep these outside the server

- Access to the Reeve source at the release used for backups, or a compatible newer release.
- The backup destination address, repository password and SFTP or S3 access credentials.
- For SFTP, the trusted host key fingerprint and a way to authorise a new SSH key.
- The complete backup ID, site name and hostnames for each site you want to restore.

Record these when setting up backups. If the old panel is unavailable, the restic repository's
snapshot tags contain `hosting-site:<backup-id>`; backup manifests contain the site name and
domains. **The backup ID is not the restic snapshot ID.**

The old panel database and server disk are not required.

## 1. Install the replacement server

Follow [Install](install.md), using the release that produced the backups or a compatible
newer one. Keep the operator password and sign in through the SSH tunnel.

## 2. Connect the backup repository

![The Backup destination page with a repository connected](images/backups.png)

On **Backups**, enter the existing destination and choose **existing repository password**.
For SFTP, authorise this server's public key if needed and compare the host key fingerprint
with your saved copy.

Leave uploads paused until this server is ready to take over. Two panels uploading to one
repository are not supported.

## 3. Restore the sites

On the replacement server, run:

```sh
sudo sh /opt/reeve/current/scripts/restore-sites.sh \
  '<backup-id>=shop=shop.example.com,www.shop.example.com'
```

Replace the example with the backup ID, new site name and hostnames. The first hostname is
primary. Add one quoted argument per site.

The script downloads each backup and restores it as a new site. Missing PHP images and
Compose builds can add time. Read the reported site, restore and domain job states;
investigate any failure before proceeding.

## 4. Verify before switching traffic

```sh
sudo reeve site list
sudo reeve site restores shop
```

Confirm that site creation and restoration succeeded. Test every hostname through the new
server's proxy. With the current local CA setup, run this on the server:

```sh
sudo curl --cacert /srv/ops/proxy/data/caddy/pki/authorities/local/root.crt \
  --resolve shop.example.com:443:127.0.0.1 https://shop.example.com/
```

Also test application login, database content and uploaded files. Restoring does not rewrite
URLs stored by an application; a hostname change may need application-specific changes.

## 5. Take over

Complete public TLS and firewall configuration, then point DNS at the replacement server.
Once this is the only server writing to the repository, resume uploads on **Backups** and
confirm that a new backup is copied successfully.
