# Install

From an empty Debian 13 machine to a running panel in three lines, or onto a machine that
already has an XFS data filesystem and Docker. The same steps begin [recovery](recover.md).

## What you need

- Debian 13 (trixie), amd64, an administrator with sudo, network access to the Debian and Docker
  package repositories and to Docker Hub. 2 GB of memory is the floor; the server profile
  (below) fits the defaults to the box.
- Somewhere for the data: a filesystem mounted at `/srv`, or an empty disk or partition the
  installer may format. An XFS `/srv` is adopted; one without project quotas gets the `prjquota`
  mount option added and is remounted (the installer asks for a reboot when it is busy); an
  empty non-XFS `/srv` is formatted as XFS after you say yes. Reeve never formats a filesystem
  that holds data or a device that carries a signature, and never partitions; carve a partition
  from free space with `fdisk` first if that is where the data must go.

## Install

From the repository (a Debian cloud image has no git; the first line adds it):

```sh
sudo apt-get install -y git
git clone https://github.com/tompowellweb/reeve.git && cd reeve && git checkout v1.1.4
sudo python3 install.py --data-device /dev/vdb
```

Or from a release tarball, with nothing but curl:

```sh
curl -fsSL https://github.com/tompowellweb/reeve/archive/refs/tags/v1.1.4.tar.gz | tar xz
cd reeve-1.1.4
sudo python3 install.py --data-device /dev/vdb
```

Leave out `--data-device` when something is already mounted at `/srv`: the installer adopts
an XFS filesystem, amends one without project quotas, or offers to format an empty non-XFS one
(`--format-data` says yes without a terminal). It then adopts Docker if it is installed as rootful overlay2 under
`/srv/docker`, or installs it with the daemon and containerd settings, the quota projects and a
journal cap; installs the checked-out tree as a release under `/opt/reeve/releases/<commit>`
with hash-locked Python dependencies; creates the `hosting-web` account, the settings file, the
local backup folder and the systemd units; initialises the PHP and database catalogues; sets up
the Caddy edge; starts the services; and prints the operator password once. About two minutes
on a cloud image, most of it Docker's packages.

The installer installs what is checked out and records its tag, or the tarball's version.
Uncommitted changes in a clone are refused unless `--allow-modified`, which marks the release
modified in the footer. Updates need git, which the installer adds to the machine.

## First access

The web UI listens on the machine's loopback only, port 8088. Reach it through an SSH forward
and sign in with the printed password:

```sh
ssh -N -L 127.0.0.1:8088:127.0.0.1:8088 admin@server
```

Then open http://127.0.0.1:8088. Change the password any time with `sudo reeve password`.
Public HTTPS access to the panel is a deployment concern outside the panel: keep it behind SSH
or a VPN.

## Settings

`/srv/ops/server.yaml` (root-only) holds the server's settings; `config/server.example.yaml`
documents every key. The ones to look at on a new machine:

- `profile`: `small` (2 to 4 GB), `standard` (8 to 16 GB, the default on a clean install) or
  `large`. A set of defaults for PHP memory and workers, the memory cap a new site gets and the
  usage a new database gets, each overridable per site.
- `mail.mode`: `direct` (send from this machine), `relay` with `relayhost`, `sink` (a local
  Mailpit for testing) or `"off"`. `mail.hostname` and `mail.public_ip` feed the SPF record
  offered on each site page. Apply with `sudo reeve mail setup`.
- `backups.local_path`: where local backups live, `/srv/backups` by default.
- `updates.hour` and `updates.every_days`: the weekly PHP patch rebuild.
- `site_backups.hour` and `retention`: the nightly complete backup and how long copies are kept.

The worker reads the file on start; restart `reeve-worker` after editing policy.

## Updating

The panel checks the repository for a newer release once a day and says so on the home page.
Apply it on the server:

```sh
sudo reeve update            # the newest release
sudo reeve update --to 1.2.0 # a named one, also the way back
```

The update fetches the release into `/opt/reeve/src` and runs its installer, which refuses a
release that cannot read the current database and returns to the running one if the new one
does not come up. Sites keep serving throughout; the panel's two processes restart.

## Then

Create a site from the home page, or connect the backup destination first from the Backups
page so every backup goes off the machine from the beginning. [Operate](operate.md) covers both.
