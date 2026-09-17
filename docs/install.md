# Install

This guide installs Reeve on a Debian 13 server and opens the panel through SSH.

## Requirements

- A minimal Debian 13 amd64 installation, with at least 2 GB of RAM.
- An administrator account with SSH key access and sudo.
- Access to Debian and Docker package repositories and container registries.
- Somewhere for the data: a separate partition or volume is best; a single-disk server works
  too. See [Where the data lives](#where-the-data-lives).

The installer sets up Docker, XFS project quotas and the panel services. An existing Docker
installation must use rootful overlay2 with its data under `/srv/docker`.

## Install Reeve

The example below installs release `v1.1.8`. Replace `/dev/vdb` with the empty data device
you intend to use: **the installer will format it**. On a single-disk server leave the option
out and accept the data image the installer offers.

```sh
sudo apt-get install -y git
git clone https://github.com/tompowellweb/reeve.git
cd reeve
git checkout v1.1.8
sudo python3 install.py --data-device /dev/vdb
```

Keep the operator password printed at the end of installation.

## Where the data lives

Reeve keeps sites, databases, Docker and local backups under `/srv` on an XFS filesystem with
project quotas, which is how each site gets a hard disk limit. Three ways to provide it, best
first:

1. **A separate partition or volume.** When installing Debian, give the root 15–20 GB and leave
   the rest for a second partition; or attach a block storage volume from your provider. Pass it
   to the installer with `--data-device`. It is formatted only if it is empty.
2. **An image file on the root filesystem**, for a VPS with one disk and no volume. When nothing
   separate is mounted at `/srv`, the installer offers to create `/var/lib/reeve/srv.img` as XFS
   and mount it there. It takes 80% of the root's free space by default and always leaves the
   system at least 10 GB; `--data-percent 60` changes the share and `--data-image` says yes
   without a terminal. The cost is one extra filesystem layer and a little throughput. To grow it
   later:

   ```sh
   sudo truncate -s +20G /var/lib/reeve/srv.img
   sudo losetup -c "$(findmnt -no SOURCE /srv)"
   sudo xfs_growfs /srv
   ```

   Keep the root filesystem from filling up under the image; the home page warns below 10 GB.
3. **An XFS filesystem you mounted at `/srv` yourself.** It is adopted, and the `prjquota`
   option is added if missing; follow any reboot instruction. An empty non-XFS `/srv` is
   formatted after you say yes.

The installer refuses devices with existing signatures and does not partition disks. Shrinking
a full-disk root needs the provider's rescue system and `resize2fs` offline; it does not do that.

## Sign in

On your own computer, open an SSH tunnel:

```sh
ssh -N -L 127.0.0.1:8088:127.0.0.1:8088 admin@server
```

Replace `admin@server` with your SSH account and server address. Open
[http://127.0.0.1:8088](http://127.0.0.1:8088) and enter the operator password.
The panel listens on the server's loopback address. Keep administration behind SSH or a VPN.

To change the password, run this on the server:

```sh
sudo reeve password
```

## Before adding sites

![The Backup destination page](images/backups.png)

Review `/srv/ops/server.yaml`. Set `profile` to `small`, `standard` or `large` to choose
resource defaults; use `small` for a 2–4 GB server. The default is `standard`.
The repository's `config/server.example.yaml` lists storage and retention settings.

Restart the worker after editing settings:

```sh
sudo systemctl restart reeve-worker
```

Connect a backup destination on the **Backups** page and save its repository password
outside the server. See [Operate](operate.md) for mail settings, site creation and updates.

Sites get certificates from the edge's own certificate authority until you switch to public
ones. When the sites' DNS points at this server and ports 80 and 443 are reachable from the
internet, set in `server.yaml`:

```yaml
tls:
  mode: public
  email: you@example.com
```

Then `sudo reeve doctor --repair`. Caddy obtains a Let's Encrypt certificate for each hostname
and renews it. Firewall rules and outgoing mail delivery still need arranging on a public server.
