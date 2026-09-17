# Install

From an empty Debian 13 machine to a running panel. The same steps are the first half of
[recovery](recover.md).

## What you need

- A Debian 13 (trixie) machine, amd64, with an administrator that has passwordless sudo, and
  an empty second disk that becomes the data filesystem. 2 GB of memory is the floor; see the
  server profile below.
- Network access to the Debian and Docker package repositories and to Docker Hub.
- This repository cloned on the machine at the commit you mean to run.

Reeve never formats a disk that carries a signature and never touches a mounted `/srv`.

## The data filesystem and Docker

`scripts/bootstrap.sh` does the whole foundation and the first install, as root:

```sh
sudo sh scripts/bootstrap.sh /dev/vdb /path/to/checkout $(git -C /path/to/checkout rev-parse HEAD)
```

It formats the data disk as XFS with project quotas and mounts it at `/srv` through fstab;
installs rootful Docker with its data root under `/srv/docker`, an address pool of its own
(`10.240.0.0/12`), the containerd config, the systemd drop-ins that bind Docker to the mount,
and a journal cap; then runs the installer. About 70 seconds on a cloud image.

If the machine already runs Docker on an XFS `/srv` with project quotas, skip the script and run
the installer alone.

## The installer

```sh
sudo python3 install.py --commit <full 40-character commit>
```

The installer requires the mounted quota-enabled XFS `/srv`, rootful Docker with overlay2 under
`/srv/docker`, and native Compose. It installs the release under `/opt/reeve/releases/<commit>`
with hash-locked Python dependencies in a virtual environment, creates the `hosting-web` account,
the settings file, the local backup folder and the systemd units, initialises the PHP and
database catalogues, sets up the Caddy edge, and starts the services. Reapplying it keeps the
settings, credentials, sites and the operation ledger. It writes `userland-proxy: false` into
Docker's daemon configuration when absent, which takes effect at Docker's next restart.

Never install a tree you have not committed and tested: the installer takes a commit, not a
working copy.

## First access

The web UI listens on the machine's loopback only, port 8088. Reach it through an SSH forward:

```sh
ssh -N -L 127.0.0.1:8088:127.0.0.1:8088 admin@server
```

Set the operator password on the server, then open http://127.0.0.1:8088:

```sh
sudo reeve set-password
```

There is no default password. Public HTTPS access to the panel is a deployment concern outside
the panel: keep it behind SSH or a VPN.

## Settings

`/srv/ops/server.yaml` (root-only) holds the server's settings; `config/server.example.yaml` is
the documented template. The ones to look at on a new machine:

- `profile`: `small` (2 to 4 GB), `standard` (8 to 16 GB, the default on a clean install) or
  `large`. A set of defaults for PHP memory and workers, the memory cap a new site gets and the
  usage a new database gets, each overridable per site.
- `mail.mode`: `direct` (send from this machine), `relay` with `relayhost`, `sink` (a local
  Mailpit for testing) or `"off"`. `mail.hostname` and `mail.public_ip` are what the SPF record
  offered on each site page uses. Apply with `sudo reeve mail-setup`.
- `backups.local_path`: where local backups live, `/srv/backups` by default.
- `updates.hour` and `updates.every_days`: the weekly PHP patch rebuild.
- `site_backups.hour` and `retention`: the nightly complete backup and how long copies are kept.

The worker reads the file on start; restart `reeve-worker` after editing policy.

## Then

Create a site from the home page, or connect the backup destination first from the Backups
page so every backup goes off the machine from the beginning. [Operate](operate.md) covers both.
