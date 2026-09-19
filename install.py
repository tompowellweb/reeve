#!/usr/bin/env python3
"""Install or update Reeve on this Debian 13 machine from this checkout.

    sudo python3 install.py [--data-device /dev/vdb | --data-image [--data-percent 80]] [--commit HASH] [--allow-modified]

The installer adopts what exists and builds what is missing:

- A quota-enabled XFS filesystem mounted at /srv is adopted. An XFS /srv without project quotas
  gets prjquota added to its mount options and is remounted (or asks for a reboot when busy). An
  empty non-XFS /srv is formatted as XFS with the operator's yes (--format-data without a
  terminal). Without a mount, the installer lists the empty disks and partitions it can see and
  an XFS image file on the root filesystem, and asks which should hold the data; a device is
  formatted, added to fstab and mounted, the image is created (--data-percent sizes it, the root
  keeps 10 GB). Without a terminal, --data-device names the device or --data-image accepts the
  image. Anything carrying a signature is refused; the installer never partitions.
- Rootful Docker with overlay2 under /srv/docker is adopted. Without Docker, it is installed with
  the daemon and containerd settings, the quota projects and the journal cap.
- The checked-out tree becomes a release under /opt/reeve/releases/<commit>, its tag (or version
  and commit) recorded. Uncommitted changes are refused unless --allow-modified, which marks the
  release modified. --commit installs a specific commit from the history instead.
- Settings, credentials, sites and the operation ledger survive a reinstall. The first install
  prints the operator password once.
"""
import argparse
import io
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

BASE = Path("/opt/reeve")
RECORD = Path("/srv/ops/panel/release.json")
PACKAGES = ["ca-certificates", "curl", "gnupg", "git", "xfsprogs", "python3-venv", "rsync", "restic"]
DOCKER_PACKAGES = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"]
DAEMON = {"data-root": "/srv/docker", "storage-driver": "overlay2", "storage-opts": ["overlay2.size=1G"],
          "default-address-pools": [{"base": "10.240.0.0/12", "size": 24}], "features": {"containerd-snapshotter": False},
          "log-driver": "journald", "log-opts": {"labels": "com.docker.compose.project,com.docker.compose.service"},
          "userland-proxy": False}
CONTAINERD = 'version = 4\nroot = "/srv/containerd"\nstate = "/run/containerd"\ndisabled_plugins = ["io.containerd.cri.v1.images", "io.containerd.cri.v1.runtime"]\n'
DROPIN = "[Unit]\nRequiresMountsFor=/srv\nAfter=srv.mount\nBindsTo=srv.mount\nConditionPathIsMountPoint=/srv\n"
JOURNAL = "[Journal]\nStorage=persistent\nSystemMaxUse=2G\nSystemKeepFree=1G\nRuntimeMaxUse=256M\n"
UNITS = ("reeve-web.service", "reeve-worker.service", "reeve-remote.service", "reeve-remote.timer")
OLD_UNITS = ("hosting-panel-remote.timer", "hosting-panel-remote.service", "hosting-panel-web.service", "hosting-panel-worker.service")


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def say(text):
    print(text, flush=True)


# ---- the data filesystem

def plan_data(mount, device, empty=False):
    """Pure decision about /srv: adopt it, amend its mount options, format what is mounted there (if empty),
    format the named device, or stop with advice."""
    if mount:
        options = set(mount.get("options", "").split(","))
        if mount.get("fstype") == "xfs":
            return "adopt" if {"prjquota", "pquota"} & options else "amend"
        if empty: return "format-mounted"
        raise SystemExit(f"/srv is mounted from {mount.get('source')} as {mount.get('fstype')}, which cannot carry XFS project quotas, "
                         "and it holds data. Move the data away and run the installer again (it will offer to format it), "
                         "or mount an XFS filesystem at /srv, or pass --data-device with an empty disk or partition.")
    if not device:
        return "image"
    return "format"


def mounted(path):
    result = subprocess.run(["findmnt", "-J", "-M", path], capture_output=True, text=True)
    return json.loads(result.stdout)["filesystems"][0] if result.returncode == 0 and result.stdout.strip() else None


def amend_fstab(text, mountpoint, source, fstype="xfs", options="defaults,prjquota"):
    """Pure: the fstab text with the entry for the mountpoint carrying the type and options, added when absent."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        fields = line.split()
        if line.strip().startswith("#") or len(fields) < 4 or fields[1] != mountpoint: continue
        current = [o for o in fields[3].split(",") if o not in ("prjquota", "pquota")]
        fields[0] = source
        fields[2] = fstype
        fields[3] = ",".join([*current, "prjquota"]) if fstype == "xfs" else fields[3]
        lines[index] = "  ".join(fields[:4] + (fields[4:] or ["0", "0"]))
        return "\n".join(lines) + "\n"
    return text.rstrip("\n") + f"\n# Reeve data: XFS with hard project quotas.\n{source} {mountpoint} {fstype} {options} 0 0\n"


def write_fstab(source, fstype="xfs"):
    fstab = Path("/etc/fstab")
    fstab.write_text(amend_fstab(fstab.read_text(), "/srv", source, fstype))
    run("systemctl", "daemon-reload")


def uuid_source(device):
    return "UUID=" + run("blkid", "-s", "UUID", "-o", "value", device)


def remount_srv():
    """A quota option cannot be added to a mounted XFS: unmount and mount again, or ask for a reboot when it is busy."""
    result = subprocess.run(["umount", "/srv"], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("/srv is busy, so its new mount options cannot take effect now. They are in /etc/fstab: "
                         "reboot, then run the installer again.")
    run("mount", "/srv")


def format_device(device):
    path = Path(device)
    if not path.exists() or run("lsblk", "-dn", "-o", "TYPE", device) not in ("disk", "part"):
        raise SystemExit(f"{device} is not a disk or a partition")
    if run("wipefs", "--no-act", "--noheadings", "--output", "TYPE", device):
        raise SystemExit(f"{device} carries a filesystem or partition-table signature; the installer formats only an empty device")
    if run("lsblk", "-n", "-o", "MOUNTPOINT", device):
        raise SystemExit(f"{device} is mounted")
    srv = Path("/srv")
    if srv.exists() and any(srv.iterdir()):
        raise SystemExit("/srv exists and is not empty; move its contents away before the installer mounts the data filesystem there")
    say(f"Formatting {device} as XFS with project quotas")
    run("mkfs.xfs", "-q", "-L", "reeve-srv", device)
    write_fstab(uuid_source(device))
    srv.mkdir(mode=0o755, exist_ok=True)
    run("mount", "/srv")


IMAGE = Path("/var/lib/reeve/srv.img")
ROOT_RESERVE = 10 * 1024 ** 3   # the root filesystem keeps at least this much free beside the image
IMAGE_MINIMUM = 8 * 1024 ** 3   # below this the data space is not worth having


DEVICE_MINIMUM = 1024 ** 3      # smaller empty devices are leftovers, not offered


def root_free():
    info = os.statvfs(IMAGE.parent.parent if IMAGE.parent.exists() else "/var")
    return info.f_bavail * info.f_frsize


def empty_devices(devices):
    """Pure: from lsblk's flat list, the disks and partitions with no filesystem, no partition table (a partition
    reports its table's type, so only disks are judged by it), no mount and a usable size, as (path, bytes, kind)."""
    found = []
    for device in devices:
        kind = device.get("type")
        if kind not in ("disk", "part") or device.get("fstype") or device.get("mountpoint") or device.get("children"): continue
        if kind == "disk" and device.get("pttype"): continue
        size = int(device.get("size") or 0)
        if size < DEVICE_MINIMUM: continue
        found.append((device["path"], size, "empty disk" if kind == "disk" else "empty partition"))
    return found


def data_choices(candidates, image):
    """Pure: the menu of places the data can live, each empty device then the image file when the root has room,
    as (label, (action, device))."""
    options = [(f"{path:<14} {size / 1024 ** 3:5.0f} GB  {kind}", ("format", path)) for path, size, kind in candidates]
    if image is not None:
        options.append((f"An XFS image file on the root filesystem (about {image / 1024 ** 3:.0f} GB)", ("image", None)))
    return options


def choose_data(percent):
    """Nothing is mounted at /srv and no flag decided: show the empty devices and the image file, ask which.
    Returns (action, device); ("image", "menu") when the image was picked from a real choice, ("image", None) when
    it was the only option, so the image's own question still asks."""
    listing = json.loads(run("lsblk", "-J", "-b", "-o", "PATH,TYPE,SIZE,FSTYPE,PTTYPE,MOUNTPOINT"))["blockdevices"]
    candidates = empty_devices(listing)
    try: image = image_size(root_free(), percent)
    except SystemExit as stop: image, why = None, str(stop)
    options = data_choices(candidates, image)
    if not options: raise SystemExit(why)
    if len(options) == 1 and options[0][1] == ("image", None): return ("image", None)
    say("Where should the site data live? Devices carrying a filesystem or partition table are not offered; "
        "wipe a spare one with wipefs -a to offer it.")
    for number, (label, _) in enumerate(options, 1): say(f"  {number}. {label}")
    for _ in range(3):
        answer = input(f"Choose [1-{len(options)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options): action, device = options[int(answer) - 1][1]
        elif any(device == answer for _, (action, device) in options): action, device = "format", answer
        else: continue
        return (action, device or "menu")
    raise SystemExit("Nothing chosen. Run the installer again, or pass --data-device or --data-image.")


def image_size(free, percent):
    """Pure: the image file's size from the root's free space and the chosen share, keeping the reserve."""
    size = min(free * percent // 100, free - ROOT_RESERVE)
    if size < IMAGE_MINIMUM:
        raise SystemExit(f"Only {free / 1024 ** 3:.1f} GB is free on the root filesystem; an image would leave less than "
                         f"{ROOT_RESERVE // 1024 ** 3} GB to the system or be under {IMAGE_MINIMUM // 1024 ** 3} GB. "
                         "Free space, attach a volume, or pass --data-device.")
    return size // (1024 ** 2) * (1024 ** 2)


def data_image(percent, assume_yes):
    """No separate filesystem: an XFS image file on the root filesystem, mounted at /srv through a loop device."""
    if IMAGE.exists():
        raise SystemExit(f"{IMAGE} exists but is not mounted at /srv; mount it (its fstab entry may be missing) or move it away")
    free = root_free()
    size = image_size(free, percent)
    question = (f"Nothing separate is mounted at /srv. Create a {size / 1024 ** 3:.0f} GB data image ({percent}% of the root's "
                f"{free / 1024 ** 3:.0f} GB free, keeping {ROOT_RESERVE // 1024 ** 3} GB for the system) at {IMAGE}? "
                "A separate partition or volume is better when you have one (--data-device).")
    if not confirm(question, assume_yes, "--data-image"):
        raise SystemExit("Not created. Mount an XFS filesystem at /srv, or pass --data-device, or --data-image to accept.")
    IMAGE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    say(f"Creating {IMAGE} ({size / 1024 ** 3:.0f} GB, sparse) as XFS with project quotas")
    run("truncate", "-s", str(size), str(IMAGE)); os.chmod(IMAGE, 0o600)
    run("mkfs.xfs", "-q", "-L", "reeve-srv", str(IMAGE))
    fstab = Path("/etc/fstab")
    fstab.write_text(amend_fstab(fstab.read_text(), "/srv", str(IMAGE), "xfs", "loop,prjquota"))
    run("systemctl", "daemon-reload")
    Path("/srv").mkdir(mode=0o755, exist_ok=True)
    run("mount", "/srv")


def confirm(question, assume_yes, flag="--format-data"):
    if assume_yes: return True
    if not sys.stdin.isatty(): raise SystemExit(question + f" Pass {flag} to say yes without a terminal.")
    return input(question + " [yes/no] ").strip().lower() in ("y", "yes")


def format_mounted(mount, assume_yes):
    """What is mounted at /srv is empty and not XFS: with the operator's yes, it becomes XFS with quotas."""
    source = mount["source"]
    if not confirm(f"/srv is {mount['fstype']} on {source} and empty. Format it as XFS with project quotas? Everything on it is lost.", assume_yes):
        raise SystemExit("Not formatted. Mount an XFS filesystem at /srv or pass --data-device.")
    run("umount", "/srv")
    say(f"Formatting {source} as XFS with project quotas")
    run("mkfs.xfs", "-q", "-f", "-L", "reeve-srv", source)
    write_fstab(uuid_source(source))
    run("mount", "/srv")


def data_filesystem(device, assume_yes=False, image=False, percent=80):
    mount = mounted("/srv")
    empty = not any(Path("/srv").iterdir()) if mount else False
    if mount and mount.get("fstype") != "xfs" and not empty:
        # lost+found alone is an empty ext filesystem
        empty = [p.name for p in Path("/srv").iterdir()] == ["lost+found"]
    plan = plan_data(mount, device, empty)
    if plan == "format": format_device(device)
    elif plan == "image":
        action, chosen = ("image", None) if image or not sys.stdin.isatty() else choose_data(percent)
        if action == "format": format_device(chosen)
        else: data_image(percent, assume_yes or image or chosen == "menu")
    elif plan == "format-mounted": format_mounted(mount, assume_yes)
    elif plan == "amend":
        say(f"/srv is XFS on {mount['source']} without project quotas: adding prjquota to its mount options")
        write_fstab(mount["source"] if mount["source"].startswith(("UUID=", "LABEL=")) else uuid_source(mount["source"]))
        remount_srv()
    for name, mode in (("/srv/ops", 0o755), ("/srv/sites", 0o755)):
        Path(name).mkdir(mode=mode, exist_ok=True)
    state = run("xfs_quota", "-x", "-c", "state -p", "/srv")
    if "Enforcement: ON" not in state:
        raise SystemExit("XFS project quota enforcement is off on /srv although it is mounted with prjquota; check dmesg and the mount")
    say("Data filesystem: /srv (XFS, project quotas" + (", adopted" if plan == "adopt" else "") + ")")


# ---- Docker

def docker_engine():
    if shutil.which("docker"):
        info = json.loads(run("docker", "info", "--format", "{{json .}}"))
        if info.get("Driver") != "overlay2" or info.get("DockerRootDir") != "/srv/docker":
            raise SystemExit("Docker is installed but not as Reeve expects (overlay2 with data-root /srv/docker). "
                             "Set data-root to /srv/docker in /etc/docker/daemon.json and restart Docker, or install on a machine without Docker.")
        say(f"Docker: engine {info.get('ServerVersion')} adopted")
        return
    say("Installing Docker")
    subprocess.run(["systemctl", "mask", "docker.service", "docker.socket", "containerd.service"], check=False, capture_output=True)
    Path("/etc/apt/keyrings").mkdir(mode=0o755, exist_ok=True)
    run("curl", "-fsSL", "https://download.docker.com/linux/debian/gpg", "-o", "/etc/apt/keyrings/docker.asc")
    os.chmod("/etc/apt/keyrings/docker.asc", 0o644)
    arch = run("dpkg", "--print-architecture")
    Path("/etc/apt/sources.list.d/docker.sources").write_text(
        f"Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: trixie\nComponents: stable\nArchitectures: {arch}\nSigned-By: /etc/apt/keyrings/docker.asc\n")
    run("apt-get", "update", "-qq")
    run("apt-get", "install", "-y", "-qq", "--no-install-recommends", *DOCKER_PACKAGES)
    if Path("/srv/docker").exists():
        raise SystemExit("/srv/docker exists although Docker was not installed; remove or move it first")
    for name, mode in (("/srv/docker", 0o710), ("/srv/docker/overlay2", 0o710), ("/srv/docker/volumes", 0o700), ("/srv/containerd", 0o700)):
        Path(name).mkdir(mode=mode, exist_ok=True)
    Path("/etc/projects").write_text("100:/srv/sites\n101:/srv/docker/volumes\n1000000:/srv/docker/overlay2\n")
    Path("/etc/projid").write_text("unadopted-sites:100\nunassigned-volumes:101\ndocker-overlay-base:1000000\n")
    for project, limit in (("unadopted-sites", "10g"), ("unassigned-volumes", "10g"), ("docker-overlay-base", None)):
        run("xfs_quota", "-x", "-c", f"project -s {project}", "/srv")
        if limit: run("xfs_quota", "-x", "-c", f"limit -p bsoft=0 bhard={limit} {project}", "/srv")
    Path("/etc/docker").mkdir(mode=0o755, exist_ok=True)
    Path("/etc/docker/daemon.json").write_text(json.dumps(DAEMON, indent=2) + "\n")
    Path("/etc/containerd/config.toml").write_text(CONTAINERD)
    for service in ("docker", "containerd"):
        folder = Path(f"/etc/systemd/system/{service}.service.d"); folder.mkdir(mode=0o755, exist_ok=True)
        (folder / "reeve-storage.conf").write_text(DROPIN)
    Path("/etc/systemd/journald.conf.d").mkdir(mode=0o755, exist_ok=True)
    Path("/etc/systemd/journald.conf.d/reeve-limits.conf").write_text(JOURNAL)
    run("systemctl", "restart", "systemd-journald")
    run("dockerd", "--validate", "--config-file=/etc/docker/daemon.json")
    run("systemctl", "unmask", "docker.service", "docker.socket", "containerd.service")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "containerd.service", "docker.service")
    say("Docker: " + run("docker", "info", "--format", "engine {{.ServerVersion}}, {{.Driver}} under {{.DockerRootDir}}"))


def daemon_setting(key, value):
    """One Docker daemon setting Reeve wants on an adopted engine, written when absent; Docker reads it at its next restart."""
    path = Path("/etc/docker/daemon.json")
    try: current = json.loads(path.read_text()) if path.exists() else {}
    except ValueError: raise SystemExit("/etc/docker/daemon.json is not valid JSON; fix it before installing")
    if key in current: return
    current[key] = value
    temp = path.with_suffix(".new"); temp.write_text(json.dumps(current, indent=2) + "\n"); temp.chmod(0o644); temp.replace(path)
    say(f"Docker daemon setting {key}={json.dumps(value)} written; it takes effect when Docker next restarts")


# ---- the release from this tree

def describe(source, git, commit):
    """The release's version: its tag when one points at the commit, else the project version plus the short commit."""
    tags = [t for t in run(*git, "tag", "--points-at", commit).splitlines() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    if tags: return sorted(tags)[-1][1:]
    text = (source / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    return (match.group(1) if match else "0") + "+" + commit[:7]


def tarball_tree(source):
    """A tree without git, such as GitHub's release tarball: the version from the project file, the files as they are."""
    text = (source / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    version = match.group(1) if match else "0"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(part in (".git", ".venv", "__pycache__", ".pytest_cache") for part in relative.parts) or not path.is_file(): continue
            tar.add(path, arcname=str(relative))
    archive = buffer.getvalue()
    import hashlib
    commit = hashlib.sha256(archive).hexdigest()[:40]
    return commit, version, False, archive, []


def tree(source, commit, allow_modified):
    """What to install: the commit, its version, whether the working tree's changes are included, and the archive."""
    if not (source / ".git").exists() and not commit:
        return tarball_tree(source)
    if not shutil.which("git"):
        raise SystemExit("git is not installed; install it (apt-get install git) or install from a release tarball")
    git = ["git", "-c", f"safe.directory={source}", "-C", str(source)]
    head = run(*git, "rev-parse", "HEAD")
    status = subprocess.check_output([*git, "status", "--porcelain", "--untracked-files=no"], text=True)
    changed = [line[3:] for line in status.splitlines() if len(line) > 3]
    if commit and commit != head:
        return commit, describe(source, git, commit), False, subprocess.check_output([*git, "archive", commit]), []
    if changed and not allow_modified:
        raise SystemExit("The checkout has uncommitted changes: " + ", ".join(changed[:5]) + ("…" if len(changed) > 5 else "")
                         + ". Commit them, or pass --allow-modified to install the working tree as a modified release.")
    return head, describe(source, git, head), bool(changed), subprocess.check_output([*git, "archive", head]), changed


def install_release(source, commit, version, modified, archive, changed):
    (BASE / "releases").mkdir(parents=True, exist_ok=True)
    name = commit if not modified else f"{commit}-modified-{int(time.time())}"
    release = BASE / "releases" / name
    release.mkdir(exist_ok=True)
    if not (release / ".installed").exists():
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(release, filter="data")
        for path in changed:  # a modified release carries the working tree's version of each changed file
            if (source / path).is_file(): shutil.copyfile(source / path, release / path)
        run("python3", "-m", "venv", str(release / ".venv"))
        run(str(release / ".venv/bin/pip"), "install", "--require-hashes", "--only-binary=:all:", "-r", str(release / "requirements.lock"))
        (release / ".installed").write_text(f"{version} {commit}{' modified' if modified else ''}\n")
    say(f"Release {version} ({commit[:7]}{', modified' if modified else ''}) under {release}")
    return release


# ---- the machine's Reeve state

def foundation(release):
    try: account = pwd.getpwnam("hosting-web")
    except KeyError:
        run("useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "hosting-web")
        account = pwd.getpwnam("hosting-web")
    for path in ("/srv/ops/panel", "/srv/ops/panel/web", "/srv/ops/panel/worker"):
        Path(path).mkdir(mode=0o700 if path.endswith(("web", "worker")) else 0o755, parents=True, exist_ok=True)
    os.chown("/srv/ops/panel/web", account.pw_uid, account.pw_gid)
    server = Path("/srv/ops/server.yaml")
    if not server.exists():
        shutil.copyfile(release / "config/server.example.yaml", server)
        server.chmod(0o600)
    python = str(release / ".venv/bin/python")
    backups = Path(run(python, "-m", "reeve.setup", "backup-root", cwd=release))
    for path in (backups, backups / "staging", backups / "staging/db", backups / "staging/site"):
        if not path.exists(): path.mkdir(mode=0o700, parents=(path == backups))
        info = path.lstat()
        if path.is_symlink() or not path.is_dir() or info.st_uid != 0 or info.st_mode & 0o077:
            raise SystemExit("Backup staging must be private root-owned directories; existing data retained")
    return python


def schema_compatible(release):
    """Refuse a release whose worker cannot read the ledger before stopping anything."""
    import sqlite3
    supported = json.loads((release / "config/capabilities.json").read_text())
    for dbpath, allowed in ((Path("/srv/ops/panel/web/auth.sqlite3"), supported["auth_schemas"]), (Path("/srv/ops/panel/worker/jobs.sqlite3"), supported["worker_schemas"])):
        if dbpath.exists():
            with sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True) as db:
                if db.execute("pragma user_version").fetchone()[0] not in allowed:
                    raise SystemExit("This release cannot read the current database schema; the running release is kept")


def retire_old_names():
    present = [u for u in OLD_UNITS if (Path("/etc/systemd/system") / u).exists()]
    if present:
        subprocess.run(["systemctl", "disable", "--now", *present], check=False, capture_output=True)
        for unit in present: (Path("/etc/systemd/system") / unit).unlink()
        run("systemctl", "daemon-reload")
        say("Retired the hosting-panel units: " + " ".join(present))
    old = Path("/usr/local/bin/hosting-panel")
    if old.exists(): old.unlink()


def select(target):
    retire_old_names()
    for unit in UNITS:
        if (target / "systemd" / unit).exists():
            shutil.copyfile(target / "systemd" / unit, Path("/etc/systemd/system") / unit)
    current = BASE / "current"
    if (BASE / "next").is_symlink(): (BASE / "next").unlink()
    (BASE / "next").symlink_to(target)
    (BASE / "next").replace(current)
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "reeve-worker", "reeve-web")
    if (target / "systemd/reeve-remote.timer").exists():
        run("systemctl", "enable", "--now", "reeve-remote.timer")


def activate(release, python):
    """Stop the running release, start this one, prove it answers; on failure go back to the previous one."""
    current = BASE / "current"
    previous = current.resolve() if current.exists() else None
    run(python, "-m", "reeve.setup", "preflight", cwd=release)
    say(run(python, "-m", "reeve.setup", "versions-init", cwd=release))
    say(run(python, "-m", "reeve.setup", "content-init", cwd=release))
    schema_compatible(release)
    subprocess.run(["systemctl", "stop", "reeve-remote.timer", "reeve-remote.service", "reeve-web", "reeve-worker", *OLD_UNITS], check=False, capture_output=True)
    try:
        run(python, "-m", "reeve.setup", "edge", cwd=release)
        select(release)
        run("curl", "--fail", "--silent", "--retry", "5", "--retry-connrefused", "http://127.0.0.1:8088/health")
        for attempt in range(10):
            try:
                run(python, "-m", "reeve.setup", "list", cwd=release); break
            except subprocess.CalledProcessError:
                if attempt == 9: raise
                time.sleep(1)
    except Exception:
        subprocess.run(["systemctl", "stop", "reeve-remote.timer", "reeve-remote.service", "reeve-web", "reeve-worker"], check=False, capture_output=True)
        if previous and previous != release and (previous / ".installed").exists():
            try:
                schema_compatible(previous); select(previous)
                say("The new release did not come up; the previous release is running again")
            except SystemExit:
                say("The new release did not come up and the previous one cannot read the current schema; nothing is running")
        raise
    return previous


def mail(python, release):
    """The relay as the settings say: deployed when absent, refreshed when running, removed when mail is off."""
    try: say("Mail: " + run(python, "-m", "reeve.setup", "mail", cwd=release))
    except subprocess.CalledProcessError as exc:
        say("Mail relay setup did not complete; the panel runs without it. Run: sudo reeve mail setup")


def record(release, commit, version, modified, previous, source_url):
    wrapper = Path("/usr/local/bin/reeve")
    wrapper.write_text('#!/bin/sh\ncd /opt/reeve/current\nexec .venv/bin/python -m reeve.cli "$@"\n')
    wrapper.chmod(0o755)
    prior = json.loads(RECORD.read_text()) if RECORD.exists() else {}
    old = prior.get("previous") if previous == release else (prior.get("current") if previous else None)
    entry = {"schema": 2, "version": version, "current": commit, "modified": modified, "previous": old,
             "previous_version": prior.get("version") if previous and previous != release else prior.get("previous_version"),
             "source": source_url, "installed_at": time.time(),
             "database_schema": {"web": 1, "worker_supported": json.loads((release / "config/capabilities.json").read_text())["worker_schemas"]}}
    temp = RECORD.with_suffix(".new"); temp.write_text(json.dumps(entry, indent=2) + "\n"); temp.replace(RECORD)
    check = Path("/srv/ops/panel/worker/update-check.json")
    if check.exists(): check.unlink()  # the worker checks again within a minute against the new version
    return entry


def server_address():
    """The address an SSH forward should use: the source address of the default route, else the hostname."""
    try:
        return json.loads(run("ip", "-j", "route", "get", "1.1.1.1"))[0].get("prefsrc") or socket.gethostname()
    except (subprocess.CalledProcessError, ValueError, IndexError, OSError):
        return socket.gethostname()


def forward_command(user, address):
    """Pure: the SSH forward that reaches the panel from the operator's own computer."""
    return f"ssh -N -L 127.0.0.1:8088:127.0.0.1:8088 {user}@{address}"


def first_password(python, release):
    """The first install makes the operator password and prints it once; a reinstall leaves it alone."""
    output = run("runuser", "-u", "hosting-web", "--", python, "-m", "reeve.setup", "password-if-missing", cwd=release)
    if output:
        say("\n" + "=" * 72 + f"\nOperator password (shown once, not stored anywhere else):\n\n    {output}\n\n"
            "Change it any time with: sudo reeve password\n" + "=" * 72)


def main():
    parser = argparse.ArgumentParser(description="Install or update Reeve from this checkout.")
    parser.add_argument("--data-device", help="the empty disk or partition to format as the XFS data filesystem when /srv is not mounted (otherwise the installer lists them and asks)")
    parser.add_argument("--format-data", action="store_true", help="say yes to formatting an empty non-XFS filesystem mounted at /srv")
    parser.add_argument("--data-image", action="store_true", help="say yes to a data image file on the root filesystem when nothing separate exists")
    parser.add_argument("--data-percent", type=int, default=80, help="the share of the root's free space the image may take (default 80)")
    parser.add_argument("--commit", help="install this commit from the history instead of the checked-out tree")
    parser.add_argument("--allow-modified", action="store_true", help="install the working tree even with uncommitted changes, as a modified release")
    args = parser.parse_args()
    if os.getuid() != 0:
        raise SystemExit("Run as root: sudo python3 install.py")
    if args.commit and not re.fullmatch("[0-9a-f]{40}", args.commit):
        raise SystemExit("--commit takes a full 40-character commit")
    os.umask(0o022)
    os.environ.update(PATH="/usr/sbin:/usr/bin:/sbin:/bin", HOME="/root", LANG="C.UTF-8", DEBIAN_FRONTEND="noninteractive")
    source = Path(__file__).resolve().parent
    if not (source / "reeve").is_dir() or not (source / "requirements.lock").exists():
        raise SystemExit("Run the installer from a Reeve checkout")
    if not Path("/etc/debian_version").exists():
        raise SystemExit("Reeve installs on Debian 13")
    missing = [p for p in PACKAGES if subprocess.run(["dpkg-query", "-W", "-f=${Status}", p], capture_output=True, text=True).stdout.strip() != "install ok installed"]
    if missing:
        say("Installing packages: " + " ".join(missing))
        run("apt-get", "update", "-qq"); run("apt-get", "install", "-y", "-qq", "--no-install-recommends", *missing)
    if not 10 <= args.data_percent <= 95: raise SystemExit("--data-percent takes 10 to 95")
    data_filesystem(args.data_device, args.format_data, args.data_image, args.data_percent)
    docker_engine()
    daemon_setting("userland-proxy", False)
    commit, version, modified, archive, changed = tree(source, args.commit, args.allow_modified)
    source_url = (subprocess.run(["git", "-c", f"safe.directory={source}", "-C", str(source), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip() or None) if (source / ".git").exists() else None
    release = install_release(source, commit, version, modified, archive, changed)
    python = foundation(release)
    previous = activate(release, python)
    entry = record(release, commit, version, modified, previous, source_url)
    mail(python, release)
    first_password(python, release)
    say(json.dumps({k: entry[k] for k in ("version", "current", "previous", "modified")}))
    say(f"\nReach the panel from your own computer with this forward, then open http://127.0.0.1:8088\n\n"
        f"    {forward_command(os.environ.get('SUDO_USER') or 'root', server_address())}\n")


if __name__ == "__main__":
    main()
