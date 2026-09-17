#!/usr/bin/env python3
"""Install or update Reeve on this Debian 13 machine from this checkout.

    sudo python3 install.py [--data-device /dev/vdb] [--commit HASH] [--allow-modified]

The installer adopts what exists and builds what is missing:

- A quota-enabled XFS filesystem mounted at /srv is adopted. Without one, --data-device names
  an empty disk or partition that is formatted as XFS with project quotas, added to fstab and
  mounted. Anything carrying a signature is refused; the installer never partitions.
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

def plan_data(mount, device):
    """Pure decision: adopt a mounted quota-enabled XFS /srv, format the named device, or stop with advice."""
    if mount:
        if mount.get("fstype") != "xfs" or not ({"prjquota", "pquota"} & set(mount.get("options", "").split(","))):
            raise SystemExit("/srv is mounted but is not XFS with project quotas (mount option prjquota). Reeve needs that for site quotas.")
        return "adopt"
    if not device:
        raise SystemExit("Nothing is mounted at /srv. Either mount an XFS filesystem there with the prjquota option, "
                         "or pass --data-device with an empty disk or partition for the installer to format "
                         "(carve one from free space with fdisk first).")
    return "format"


def mounted(path):
    result = subprocess.run(["findmnt", "-J", "-M", path], capture_output=True, text=True)
    return json.loads(result.stdout)["filesystems"][0] if result.returncode == 0 and result.stdout.strip() else None


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
    uuid = run("blkid", "-s", "UUID", "-o", "value", device)
    fstab = Path("/etc/fstab")
    fstab.write_text(fstab.read_text() + f"\n# Reeve data: XFS with hard project quotas.\nUUID={uuid} /srv xfs defaults,prjquota 0 0\n")
    run("systemctl", "daemon-reload")
    srv.mkdir(mode=0o755, exist_ok=True)
    run("mount", "/srv")


def data_filesystem(device):
    if plan_data(mounted("/srv"), device) == "format":
        format_device(device)
    for name, mode in (("/srv/ops", 0o755), ("/srv/sites", 0o755)):
        Path(name).mkdir(mode=mode, exist_ok=True)
    state = run("xfs_quota", "-x", "-c", "state -p", "/srv")
    if "Enforcement: ON" not in state:
        raise SystemExit("XFS project quota enforcement is off on /srv; mount it with the prjquota option")
    say("Data filesystem: /srv (XFS, project quotas)")


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


def first_password(python, release):
    """The first install makes the operator password and prints it once; a reinstall leaves it alone."""
    output = run("runuser", "-u", "hosting-web", "--", python, "-m", "reeve.setup", "password-if-missing", cwd=release)
    if output:
        say("\n" + "=" * 72 + f"\nOperator password (shown once, not stored anywhere else):\n\n    {output}\n\n"
            "Reach the panel through an SSH forward of port 8088 and sign in; change it any time with: sudo reeve password\n" + "=" * 72)


def main():
    parser = argparse.ArgumentParser(description="Install or update Reeve from this checkout.")
    parser.add_argument("--data-device", help="an empty disk or partition to format as the XFS data filesystem when /srv is not mounted")
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
    data_filesystem(args.data_device)
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


if __name__ == "__main__":
    main()
