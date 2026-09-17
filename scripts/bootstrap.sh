#!/bin/sh
# Fresh-machine bootstrap for Reeve, from the tested playbook, on a clean Debian 13
# machine with an empty second disk. Run as root. Idempotent where the playbook allows it.
#
#   recover-bootstrap.sh DATA_DISK PANEL_SOURCE_DIR COMMIT
#
# It formats DATA_DISK as the quota-enabled XFS /srv (refusing an existing signature), installs
# rootful Docker on it exactly as the playbook does, then installs the Reeve release from a clean
# checkout at COMMIT. It does not restore sites; recover-sites.sh does that from the repository.
set -eu
DISK=$1; SOURCE=$2; COMMIT=$3
export DEBIAN_FRONTEND=noninteractive
log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

log "packages"
apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg git xfsprogs python3-venv rsync >/dev/null

if ! findmnt -rn /srv >/dev/null; then
    log "XFS data filesystem on $DISK"
    test "$(lsblk -dn -o TYPE "$DISK")" = disk
    test -z "$(wipefs --no-act --noheadings --output TYPE "$DISK")"
    test -z "$(find /srv -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null || true)"
    mkfs.xfs -q -L hosting-srv "$DISK"
    uuid=$(blkid -s UUID -o value "$DISK")
    printf '\n# Hosting data: required XFS mount with hard project quotas.\nUUID=%s /srv xfs defaults,prjquota 0 0\n' "$uuid" >> /etc/fstab
    systemctl daemon-reload
    install -d -m 0755 /srv
    mount /srv
fi
install -d -m 0755 /srv/ops /srv/sites
install -d -m 0700 /srv/backups /srv/backups/restic /srv/backups/staging
test "$(findmnt -no FSTYPE /srv)" = xfs
xfs_quota -x -c state /srv | grep -q 'Enforcement: ON'

if ! command -v docker >/dev/null; then
    log "Docker engine"
    systemctl mask docker.service docker.socket containerd.service >/dev/null 2>&1 || true
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
    chmod 0644 /etc/apt/keyrings/docker.asc
    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
    test ! -e /srv/docker
    install -d -m 0710 /srv/docker /srv/docker/overlay2
    install -d -m 0700 /srv/docker/volumes /srv/containerd
    cat > /etc/projects <<'EOF'
100:/srv/sites
101:/srv/docker/volumes
1000000:/srv/docker/overlay2
EOF
    cat > /etc/projid <<'EOF'
unadopted-sites:100
unassigned-volumes:101
docker-overlay-base:1000000
EOF
    xfs_quota -x -c 'project -s unadopted-sites' /srv
    xfs_quota -x -c 'limit -p bsoft=0 bhard=10g unadopted-sites' /srv
    xfs_quota -x -c 'project -s unassigned-volumes' /srv
    xfs_quota -x -c 'limit -p bsoft=0 bhard=10g unassigned-volumes' /srv
    xfs_quota -x -c 'project -s docker-overlay-base' /srv
    install -d -m 0755 /etc/docker
    cat > /etc/docker/daemon.json <<'EOF'
{
  "data-root": "/srv/docker",
  "storage-driver": "overlay2",
  "storage-opts": ["overlay2.size=1G"],
  "default-address-pools": [{"base": "10.240.0.0/12", "size": 24}],
  "features": {"containerd-snapshotter": false},
  "log-driver": "journald",
  "log-opts": {"labels": "com.docker.compose.project,com.docker.compose.service"}
}
EOF
    cat > /etc/containerd/config.toml <<'EOF'
version = 4
root = "/srv/containerd"
state = "/run/containerd"
disabled_plugins = ["io.containerd.cri.v1.images", "io.containerd.cri.v1.runtime"]
EOF
    for service in docker containerd; do
        install -d -m 0755 /etc/systemd/system/"$service".service.d
        cat > /etc/systemd/system/"$service".service.d/hosting-storage.conf <<'EOF'
[Unit]
RequiresMountsFor=/srv
After=srv.mount
BindsTo=srv.mount
ConditionPathIsMountPoint=/srv
EOF
    done
    install -d -m 0755 /etc/systemd/journald.conf.d
    printf '[Journal]\nStorage=persistent\nSystemMaxUse=2G\nSystemKeepFree=1G\nRuntimeMaxUse=256M\n' > /etc/systemd/journald.conf.d/hosting-limits.conf
    systemctl restart systemd-journald
    dockerd --validate --config-file=/etc/docker/daemon.json
    systemctl unmask docker.service docker.socket containerd.service
    systemctl daemon-reload
    systemctl enable --now containerd.service docker.service >/dev/null
fi
docker info --format 'Engine={{.ServerVersion}} Driver={{.Driver}} Root={{.DockerRootDir}}'

log "panel release $COMMIT"
git -C "$SOURCE" rev-parse HEAD | grep -q "^$COMMIT$"
python3 "$SOURCE/install.py" --commit "$COMMIT" >/tmp/install.log 2>&1 || { tail -20 /tmp/install.log; exit 1; }
tail -1 /tmp/install.log
systemctl is-active reeve-web reeve-worker >/dev/null
log "bootstrap complete"
