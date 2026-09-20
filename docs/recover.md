# Recover

Restoration is a server feature. Reeve finds every site that was backed up to a repository, a
folder of backups or the server's own copies, and brings back the ones you choose, at the point
in time you choose, as new sites or into live ones.

## Keep this outside the server

The **recovery card**: on **Backups**, next to the repository password, **Show recovery card**
gives one small text, shown on the page to copy or saved as a file, with the destination address, its pinned host keys, this server's
credentials and the repository password. Keep it in a password manager. It is the whole way into
the backups, so treat it as the backups themselves. `sudo reeve backup card` prints the same.

Nothing else. A replacement server makes its own WireGuard tunnel; the old client
configurations do not carry over. The repository holds a **server record** beside the site backups: the server's
settings, its release, and every site with its hostnames and latest complete backup. It is
written whenever something changes and copied with the backups.

## Replace a server

1. Install Reeve on a fresh Debian 13 server as in [Install](install.md); the release that
   made the backups or any newer one.
2. On **Backups**, **Connect from the card**. The server connects with the recorded host keys
   and credentials, to the very repository the card names, with uploads paused: two servers
   must not write to one repository. Without a card, connect the destination by hand and choose
   **existing repository password**.
3. On **Recover**, scan the repository. It lists the sites found, each with its complete
   backups and database dumps by time, and the server record. Apply the recorded settings,
   tick the sites, keep or change their names and hostnames, and press **Restore the selected
   sites**. Restores run one after another; the page shows each step.
4. Check the sites through the new server, point DNS at it, then resume uploads on **Backups**.

The same from the command line:

```sh
sudo reeve backup connect-card reeve-recovery-card.json
sudo reeve server scan
sudo reeve server restore --settings
sudo reeve server recoveries
```

`reeve server restore` takes every site found, newest backup, recorded hostnames; name sites
to restore only those.

## Roll a live site back

On **Recover**, scan the repository or this server's own copies and tick the site. **From** is
the point in time: a complete backup or a database dump. **Restore** depends on whether the
site is live on this server:

- a live site takes the backup as files and database, files only or database only, or the
  chosen dump as its database; the most recent dump is the usual pick after rolling files back
  to an earlier day on a site whose orders must stay current;
- a site that is not live here comes back as a new site, name and hostnames prefilled.

A restore into a live site takes a complete backup of it first, listed on the site's Backups
page as `pre-restore`.

## A folder of backups

Backups copied out of a server's `/srv/backups/staging` folders, or exported from a site's
Backups page, can be scanned from any folder on this server: choose **A folder on this
server** and give its path. A `server-record.json` in that folder is read too.

## Manage backups

**Recover → Manage** lists every backup the last scan found, by site: complete backups and
database dumps, each with its time, size and where it is (here, the repository or the folder),
and for a copy here whether it has been copied off-machine. Two actions:

- **Download** prepares the files for six hours: `snapshot.tar` is the whole backup and can be
  imported on any panel. A backup that is only in the repository or a folder is fetched here
  first and then stays as a local copy.
- **Delete** lets go of the one copy listed. Deleting a copy here leaves the repository's;
  deleting a repository snapshot forgets it and prunes the repository, and leaves the copy here.
  **Delete all** takes a site's every listed backup after you type its name. This is how a
  deleted site's final backup, which retention never touches, is let go once it is no longer
  wanted. A backup a recovery is using cannot be deleted until it finishes.

Downloads and deletions run in the worker one after another and are listed at the foot of the
page with their outcome.

```sh
sudo reeve server actions
```

## Verify

```sh
sudo reeve site list
sudo reeve site restores shop
```

Test every hostname through the new server's proxy and the application's login, database
content and uploads. Restoring does not rewrite URLs stored by an application; a hostname
change may need application-specific changes.
