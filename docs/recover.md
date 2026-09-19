# Recover

Restoration is a server feature. Reeve finds every site that was backed up to a repository, a
folder of backups or the server's own copies, and brings back the ones you choose, at the point
in time you choose, as new sites or into live ones.

## Keep these outside the server

- The backup destination address and its repository password.
- For SFTP, the credentials or a way to authorise a new server's key, and the host key fingerprint.

Nothing else. The repository holds a **server record** beside the site backups: the server's
settings, its release, and every site with its hostnames and latest complete backup. It is
written whenever something changes and copied with the backups.

## Replace a server

1. Install Reeve on a fresh Debian 13 server as in [Install](install.md); the release that
   made the backups or any newer one.
2. On **Backups**, connect the same destination and choose **existing repository password**.
   Leave uploads paused: two servers must not write to one repository.
3. On **Recover**, scan the repository. It lists the sites found, each with its complete
   backups and database dumps by time, and the server record. Apply the recorded settings,
   tick the sites, keep or change their names and hostnames, and press **Restore the selected
   sites**. Restores run one after another; the page shows each step.
4. Check the sites through the new server, point DNS at it, then resume uploads on **Backups**.

The same from the command line:

```sh
sudo reeve server scan
sudo reeve server restore --settings
sudo reeve server recoveries
```

`reeve server restore` takes every site found, newest backup, recorded hostnames; name sites
to restore only those.

## Roll a live site back

On **Recover**, scan the repository or this server's own copies, tick the site and choose:

- **files and database** from a chosen backup: the site as it was then;
- **files only** or **database only** from a chosen backup;
- **database from a chosen dump**: the most recent database dump, useful after rolling files
  back to an earlier day on a site whose orders must stay current.

A restore into a live site takes a complete backup of it first, listed on the site's Backups
page as `pre-restore`.

## A folder of backups

Backups copied out of a server's `/srv/backups/staging` folders, or exported from a site's
Backups page, can be scanned from any folder on this server: choose **A folder on this
server** and give its path. A `server-record.json` in that folder is read too.

## Verify

```sh
sudo reeve site list
sudo reeve site restores shop
```

Test every hostname through the new server's proxy and the application's login, database
content and uploads. Restoring does not rewrite URLs stored by an application; a hostname
change may need application-specific changes.
