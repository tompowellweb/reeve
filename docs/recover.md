# Recover

The runbook for a lost machine: from an empty Debian 13 machine to the sites serving again.
Measured on a test VM at 190 seconds for three sites, 128 seconds for three real sites totalling
2.2 GB.

## What must exist outside the server

| Input | Where it lives |
|---|---|
| This repository at the tested commit | Git |
| The backup repository | The SFTP server or the S3 bucket the panel copied to |
| The repository password | The operator's password manager, one entry per repository, named by the repository id the Backups page showed |
| The destination's host key fingerprint (SFTP) | Beside the password |
| A way to authorise a new SSH key on the destination account (SFTP), or the S3 key pair | The operator |
| Each site's backup id and hostnames | The Backups page of the old panel; or a `restic snapshots --tag hosting-site:<site id>` listing plus the `domains` field of a backup's manifest |

Nothing else: not the old server's ledger, disk or keys.

## Steps

1. **Provision** a Debian 13 machine with an administrator and an empty second disk.
2. **Install**, as root, with the source cloned and checked out at the release that wrote the
   backups or a newer one (an older release lacks the restore fixes newer backups depend on):

   ```sh
   sudo apt-get install -y git
   git clone https://github.com/tompowellweb/reeve.git && cd reeve && git checkout v1.1.3
   sudo python3 install.py --data-device /dev/vdb
   ```

   The installer formats the empty data disk, installs Docker and the release, and prints the
   operator password once. About two minutes.
3. **Connect the repository** on the Backups page, choosing "existing repository password" and
   pasting it. The page probes and pins the destination's host key (compare the fingerprint it
   shows with the recorded one), opens the repository, records its identity, and leaves uploads
   paused. For SFTP with a key, the page shows this machine's public key to authorise on the
   destination account first. The same can be done by hand under
   `/srv/ops/panel/worker/remote-secrets/` and `remote-backup.json`.
4. **Restore**, as root:

   ```sh
   sh scripts/restore-sites.sh "<backup id>=<name>=<hostname>[,<alias>...]" ...
   ```

   Each backup is fetched into local staging and restored as a new site through the ordinary
   path: managed sites are created and refilled, Compose packages rebuilt from their Dockerfiles.
   A restore gives the site one hostname; when the spec lists aliases the script applies the full
   list afterwards as a domains job. Aliases are never restored by themselves because they belong
   to the old site's identity.
5. **Verify**: `reeve site list` shows every site succeeded and healthy; open each hostname over HTTPS
   through the machine's own edge (`curl --resolve <host>:443:127.0.0.1` with the local CA from
   `/srv/ops/proxy/data/caddy/pki/authorities/local/root.crt`); log in to one application.
6. **Set the operator password**: the installer printed one; change it with `reeve password`. Point DNS at the machine when it is the
   real server.
7. **Turn uploads on** from the Backups page only when this machine owns the repository from now
   on. Two panels uploading to one repository is not supported.

## What can go wrong

- The installer refuses a data device with any signature and a checkout with uncommitted changes.
  It never formats over data; fix the cause.
- A missing PHP branch image is built during the site's create (a minute or two); a Compose
  package with a Dockerfile builds it.
- Restore keeps the application's stored URLs. WordPress under a new hostname needs its own
  search-and-replace.
- A destination that is unreachable fails the fetch before anything is created; nothing to undo.
- A repository left locked by a crashed uploader is unlocked at the start of each copy cycle.

## Everyday restores

The same mechanism serves smaller cases from the panel: a site's Backups page offers **New site**
from any backup, **Files into this site** and **Database into this site** (a safety backup is
taken first), **Download** of a backup, and **Import a backup** from elsewhere. **History** lists
deleted sites with their final backups and restores them.
