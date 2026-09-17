# Install

This guide installs Reeve on a Debian 13 server and opens the panel through SSH.

## Requirements

- A minimal Debian 13 amd64 installation, with at least 2 GB of RAM.
- An administrator account with SSH key access and sudo.
- Access to Debian and Docker package repositories and container registries.
- An empty disk or partition for data, or an existing XFS filesystem mounted at `/srv`.

The installer sets up Docker, XFS project quotas and the panel services. An existing Docker
installation must use rootful overlay2 with its data under `/srv/docker`.

## Install Reeve

The example below installs release `v1.1.5`. Replace `/dev/vdb` with the empty data device
you intend to use: **the installer will format it**.

```sh
sudo apt-get install -y git
git clone https://github.com/tompowellweb/reeve.git
cd reeve
git checkout v1.1.5
sudo python3 install.py --data-device /dev/vdb
```

If `/srv` is already mounted, omit `--data-device`. The installer adopts XFS and enables
project quotas if needed; follow any reboot instruction. For an empty non-XFS `/srv`, it
asks before formatting. It refuses devices with existing signatures and does not partition disks.

Keep the operator password printed at the end of installation.

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

Site routes currently use Caddy's local CA. Public certificates, DNS and firewall rules
must be arranged before serving public traffic.
