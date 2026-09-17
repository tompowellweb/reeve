#!/usr/bin/env python3
"""Install an exact Git revision. Requires the already provisioned Docker/XFS VM."""
import argparse
import io
import json
import os
import pwd
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path


def run(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def daemon_setting(key, value):
    """One Docker daemon setting the panel wants (the userland proxy off: published ports work through the
    firewall rules alone, and a proxy process per port is memory for nothing). Written only when absent;
    Docker reads it at its next restart, which the installer never forces: running containers stay up."""
    path = Path("/etc/docker/daemon.json")
    try: current = json.loads(path.read_text()) if path.exists() else {}
    except ValueError: raise SystemExit("/etc/docker/daemon.json is not valid JSON; fix it before installing")
    if key in current: return
    current[key] = value
    temp = path.with_suffix(".new"); temp.write_text(json.dumps(current, indent=2) + "\n"); temp.chmod(0o644); temp.replace(path)
    print(f"Docker daemon setting {key}={json.dumps(value)} written; it takes effect when Docker next restarts", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--take-over-proof", action="store_true")
    args = parser.parse_args()
    if os.getuid() != 0 or not re.fullmatch("[0-9a-f]{40}", args.commit):
        raise SystemExit("Run as root and select a full 40-character tested commit")
    os.umask(0o022)
    os.environ.update(PATH="/usr/sbin:/usr/bin:/sbin:/bin", HOME="/root", LANG="C.UTF-8")
    source = Path(__file__).resolve().parent
    # Fail before creating anything under an absent data mount.
    mount = json.loads(run("findmnt", "-J", "-M", "/srv"))["filesystems"][0]
    if mount["fstype"] != "xfs" or "prjquota" not in mount["options"]:
        raise SystemExit("Expected existing quota-enabled XFS /srv; installer never formats disks")
    git = ["git", "-c", f"safe.directory={source}", "-C", str(source)]
    if run(*git, "rev-parse", "HEAD") != args.commit or run(*git, "status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("Checkout must be clean at the selected commit")
    if not Path("/usr/share/python-wheels").exists():
        run("apt-get", "update")
        run("apt-get", "install", "-y", "python3-venv")
    if not Path('/usr/bin/restic').exists():
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', 'restic')
    # Account creation is idempotent; never grant Docker membership or sudo.
    try:
        account = pwd.getpwnam("hosting-web")
    except KeyError:
        run("useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", "hosting-web")
        account = pwd.getpwnam("hosting-web")
    base = Path("/opt/reeve")
    (base / "releases").mkdir(parents=True, exist_ok=True)
    release = base / "releases" / args.commit
    release.mkdir(exist_ok=True)
    if not (release / ".installed").exists():
        archive = subprocess.check_output([*git, "archive", args.commit])
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(release, filter="data")
        run("python3", "-m", "venv", str(release / ".venv"))
        run(str(release / ".venv/bin/pip"), "install", "--require-hashes", "--only-binary=:all:", "-r", str(release / "requirements.lock"))
        (release / ".installed").write_text(args.commit + "\n")
    for path in ("/srv/ops/panel", "/srv/ops/panel/web", "/srv/ops/panel/worker"):
        Path(path).mkdir(mode=0o700 if path.endswith(("web", "worker")) else 0o755, parents=True, exist_ok=True)
    os.chown("/srv/ops/panel/web", account.pw_uid, account.pw_gid)
    server = Path("/srv/ops/server.yaml")
    if not server.exists():
        shutil.copyfile(release / "config/server.example.yaml", server)
        server.chmod(0o600)
    backups = Path(subprocess.run([str(release / ".venv/bin/python"), "-c", "from reeve.host import backup_root; print(backup_root())"],
                                  check=True, capture_output=True, text=True, cwd=release).stdout.strip())
    for path in (backups, backups / 'staging', backups / 'staging/db', backups / 'staging/site'):
        if not path.exists(): path.mkdir(mode=0o700, parents=(path == backups))
        info = path.lstat()
        if path.is_symlink() or not path.is_dir() or info.st_uid != 0 or info.st_mode & 0o077:
            raise SystemExit('Backup staging must be private root-owned directories; existing data retained')
    daemon_setting("userland-proxy", False)
    python = str(release / ".venv/bin/python")
    run(python, "-m", "reeve.cli", "preflight", cwd=release)
    # Prepare verified runtimes before stopping the current private panel. Existing image
    # identities are retained; this is not the future scheduled patch updater.
    print(run(python, "-m", "reeve.cli", "versions-init", cwd=release), flush=True)
    print(run(python, "-m", "reeve.cli", "content-init", cwd=release), flush=True)
    edge_args = [python, "-m", "reeve.cli", "edge-setup"]
    if args.take_over_proof:
        edge_args.append("--take-over-proof")
    current = base / "current"
    previous = current.resolve() if current.exists() else None
    def retire_old_names():
        """A machine installed before the product was named: its hosting-panel units and command go."""
        old_units = ("hosting-panel-remote.timer", "hosting-panel-remote.service", "hosting-panel-web.service", "hosting-panel-worker.service")
        present = [u for u in old_units if (Path("/etc/systemd/system") / u).exists()]
        if present:
            subprocess.run(["systemctl", "disable", "--now", *present], check=False, capture_output=True)
            for unit in present: (Path("/etc/systemd/system") / unit).unlink()
            run("systemctl", "daemon-reload")
            print("Retired the hosting-panel units: " + " ".join(present), flush=True)
        old_wrapper = Path("/usr/local/bin/hosting-panel")
        if old_wrapper.exists(): old_wrapper.unlink()

    def select(target):
        retire_old_names()
        for unit in ("reeve-web.service", "reeve-worker.service", "reeve-remote.service", "reeve-remote.timer"):
            if (target / "systemd" / unit).exists():
                shutil.copyfile(target / "systemd" / unit, Path("/etc/systemd/system") / unit)
        if (base / "next").is_symlink():
            (base / "next").unlink()
        (base / "next").symlink_to(target)
        (base / "next").replace(current)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", "reeve-worker", "reeve-web")
        if (target / "systemd/reeve-remote.timer").exists():
            run("systemctl", "enable", "--now", "reeve-remote.timer")

    # Schema 15 adds site backup/delete records; older workers are unsafe.
    import sqlite3
    for dbpath in (Path("/srv/ops/panel/web/auth.sqlite3"), Path("/srv/ops/panel/worker/jobs.sqlite3")):
        if dbpath.exists():
            with sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True) as db:
                allowed = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17) if dbpath.name == "jobs.sqlite3" else (1,)
                if db.execute("pragma user_version").fetchone()[0] not in allowed:
                    raise SystemExit("Database schema is incompatible; current services retained")
    subprocess.run(["systemctl", "stop", "reeve-remote.timer", "reeve-remote.service", "reeve-web", "reeve-worker",
                    "hosting-panel-remote.timer", "hosting-panel-remote.service", "hosting-panel-web", "hosting-panel-worker"], check=False, capture_output=True)
    try:
        run(*edge_args, cwd=release)
        select(release)
        run("curl", "--fail", "--silent", "--retry", "5", "--retry-connrefused", "http://127.0.0.1:8088/health")
        for attempt in range(10):
            try:
                run(python, "-m", "reeve.cli", "list", cwd=release)
                break
            except subprocess.CalledProcessError:
                if attempt == 9:
                    raise
                time.sleep(0.3)
    except Exception:
        subprocess.run(["systemctl", "stop", "reeve-remote.timer", "reeve-remote.service", "reeve-web", "reeve-worker"], check=False, capture_output=True)
        if previous:
            capability_path = previous / "config/capabilities.json"
            supported = json.loads(capability_path.read_text())["worker_schemas"] if capability_path.exists() else [1]
            with sqlite3.connect("file:/srv/ops/panel/worker/jobs.sqlite3?mode=ro", uri=True) as db:
                version = db.execute("pragma user_version").fetchone()[0]
            if version in supported:
                select(previous)
            else:
                print("Previous release cannot read the current job schema; automatic downgrade refused.", flush=True)
        raise
    wrapper = Path("/usr/local/bin/reeve")
    wrapper.write_text('#!/bin/sh\ncd /opt/reeve/current\nexec .venv/bin/python -m reeve.cli "$@"\n')
    wrapper.chmod(0o755)
    record_path = Path("/srv/ops/panel/release.json")
    prior_record = json.loads(record_path.read_text()) if record_path.exists() else {}
    old_commit = prior_record.get("previous") if previous == release else previous.name if previous else None
    record = {"schema": 1, "current": args.commit, "previous": old_commit, "database_schema": {
        "web": 1, "worker_supported": json.loads((release / 'config/capabilities.json').read_text())['worker_schemas']}}
    temp_record = record_path.with_suffix(".new")
    temp_record.write_text(json.dumps(record, indent=2) + "\n")
    temp_record.replace(record_path)
    print(json.dumps(record))


if __name__ == "__main__":
    main()
