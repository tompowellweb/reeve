"""Destructive only to named disposable m1-* fixtures on the hosting test VM.

Run with the installed release Python as root. Never use on customer sites.
"""
import argparse
import errno
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from reeve.core import Ledger
from reeve.host import Host, PROXY, SITES, atomic, command, quota_record
from reeve.worker import rpc

RESULTS = Path("/srv/ops/panel/worker/acceptance.json")
LEDGER = Path("/srv/ops/panel/worker/jobs.sqlite3")


def record(key, value):
    data = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    data[key] = value
    atomic(RESULTS, json.dumps(data, indent=2))
    print(key + ": " + json.dumps(value), flush=True)


def wait(ident, state="succeeded", seconds=60):
    ledger = Ledger(LEDGER)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        row = ledger.get(ident)
        if row["state"] == state:
            return row
        if state == "succeeded" and row["state"] in ("failed", "recovery-needed"):
            raise AssertionError(row)
        time.sleep(0.1)
    raise AssertionError(f"Operation did not reach {state}")


def submit(name):
    data = {"name": name, "domain": name + ".hosting.test", "data_mb": 16, "layer_mb": 16}
    return rpc({"op": "create", "id": str(uuid.uuid4()), "data": data})


def https(domain, path="/"):
    return command(["curl", "--noproxy", "*", "--fail", "--silent", "--show-error", "--cacert", PROXY / "data/caddy/pki/authorities/local/root.crt",
        "--resolve", f"{domain}:443:127.0.0.1", f"https://{domain}{path}"])


def existing():
    rows = {row["name"]: row for row in rpc({"op": "list"})}
    for name in ("m1-alpha", "m1-beta"):
        assert rows[name]["state"] == "succeeded"
        assert rows[name]["health"]["application"] == "healthy"
    return rows


def upload_and_limits():
    rows = existing()
    for name in ("m1-alpha", "m1-beta"):
        row = rows[name]
        content = f"<!doctype html><title>{name}</title><h1>Uploaded {name}</h1>\n"
        # Write through the documented numeric-identity mechanism, not host-root ownership.
        script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])"
        command(["setpriv", f"--reuid={row['uid']}", f"--regid={row['uid']}", "--clear-groups", "python3", "-c", script,
                 SITES / name / "html/index.html", content])
        assert https(row["domain"]) == content
        for filename in ("secret.php", "secret.php.jpg", ".env", "hosting.yaml"):
            command(["setpriv", f"--reuid={row['uid']}", f"--regid={row['uid']}", "--clear-groups", "python3", "-c", script,
                     SITES / name / "html" / filename, "must-not-be-served"])
            code = command(["curl", "--noproxy", "*", "--silent", "--output", "/dev/null", "--write-out", "%{http_code}",
                "--cacert", PROXY / "data/caddy/pki/authorities/local/root.crt", "--resolve", f"{row['domain']}:443:127.0.0.1", f"https://{row['domain']}/{filename}"])
            assert code == "404", (filename, code)
        # Content-owned symlinks may not expose even another file inside the container.
        command(["setpriv", f"--reuid={row['uid']}", f"--regid={row['uid']}", "--clear-groups", "ln", "-sf", "/etc/passwd", SITES / name / "html/linked.txt"])
        code = command(["curl", "--noproxy", "*", "--silent", "--output", "/dev/null", "--write-out", "%{http_code}",
            "--cacert", PROXY / "data/caddy/pki/authorities/local/root.crt", "--resolve", f"{row['domain']}:443:127.0.0.1", f"https://{row['domain']}/linked.txt"])
        assert code in ("403", "404")
    record("uploads_and_protected_files", "both sites passed with their own numeric UID")
    row = rows["m1-alpha"]
    fill = SITES / "m1-alpha/html/quota-fill.bin"
    script = '''import errno,sys
path=sys.argv[1]
try:
 with open(path,'xb') as stream:
  for _ in range(32):
   stream.write(b'x'*1048576); stream.flush()
except OSError as exc:
 assert exc.errno in (errno.EDQUOT, errno.ENOSPC), exc
 print('refused',exc.errno)
else:
 raise AssertionError('quota did not stop write')
'''
    try:
        # Do not inherit the production helper's 1 MiB output-file limit for this
        # intentional 32 MiB test write; measure the filesystem's own hard refusal.
        result = subprocess.check_output(["setpriv", f"--reuid={row['uid']}", f"--regid={row['uid']}", "--clear-groups", "python3", "-c", script, str(fill)], text=True)
        assert "Uploaded m1-beta" in https("m1-beta.hosting.test")
        record("data_quota_refusal", {"result": result.strip(), "quota": quota_record(row["project"]), "neighbour": "healthy"})
    finally:
        fill.unlink(missing_ok=True)
    # /tmp is deliberately part of this nginx container's quota-limited writable layer.
    result = subprocess.run(["docker", "exec", "hosting-site-m1-alpha", "sh", "-c", "dd if=/dev/zero of=/tmp/quota-fill.bin bs=1M count=32"], capture_output=True, text=True)
    try:
        assert result.returncode != 0 and ("quota" in result.stderr.lower() or "space" in result.stderr.lower()), result
        assert "Uploaded m1-beta" in https("m1-beta.hosting.test")
        record("layer_quota_refusal", {"exit": result.returncode, "error": result.stderr[-400:], "neighbour": "healthy"})
    finally:
        command(["docker", "exec", "hosting-site-m1-alpha", "rm", "-f", "/tmp/quota-fill.bin"])
    assert "Uploaded m1-alpha" in https("m1-alpha.hosting.test")


def conflicts_and_boundary():
    rows = existing()
    row = rows["m1-alpha"]
    assert rpc({"op": "create", "id": row["id"], "data": json.loads(row["payload"])})["uid"] == row["uid"]
    for data in ({"name": "m1-alpha", "domain": "other.hosting.test"}, {"name": "other", "domain": row["domain"]},
                 {"name": "../unsafe", "domain": "bad.hosting.test"}):
        try:
            rpc({"op": "create", "id": str(uuid.uuid4()), "data": data})
        except ValueError:
            pass
        else:
            raise AssertionError("Conflict accepted")
    link = SITES / "m1-unmanaged-link"
    link.symlink_to("/nonexistent")
    try:
        try:
            submit("m1-unmanaged-link")
        except ValueError:
            pass
        else:
            raise AssertionError("Unmanaged symlink accepted")
    finally:
        link.unlink()
    bad_rpc = '''import json,socket
s=socket.socket(socket.AF_UNIX); s.connect('/run/reeve/worker.sock')
s.sendall(b'{"op":"exec","command":"id"}\\n')
r=json.loads(s.recv(8192)); assert not r['ok']; print(r['error'])
'''
    result = command(["runuser", "-u", "hosting-web", "--", "python3", "-c", bad_rpc])
    for path in (SITES / "m1-alpha/html/index.html", LEDGER, PROXY / "data/caddy/pki/authorities/local/root.key", "/run/docker.sock"):
        denied = subprocess.run(["runuser", "-u", "hosting-web", "--", "test", "-r", str(path)])
        assert denied.returncode != 0, path
    denied = subprocess.run(["runuser", "-u", "hosting-web", "--", "sudo", "-n", "true"], capture_output=True)
    assert denied.returncode != 0
    record("conflicts_and_web_boundary", {"fixed_protocol": result.strip(), "file_docker_sudo_access": "denied"})
    host = Host()
    alpha, beta = host.inspect("hosting-site-m1-alpha"), host.inspect("hosting-site-m1-beta")
    beta_ip = next(iter(beta["NetworkSettings"]["Networks"].values()))["IPAddress"]
    edge = host.inspect("hosting-edge")
    edge_ip = edge["NetworkSettings"]["Networks"]["hosting-ingress-m1-alpha"]["IPAddress"]
    for url in ("http://web-m1-beta:8080/", f"http://{beta_ip}:8080/", f"http://{edge_ip}:2019/config/"):
        blocked = subprocess.run(["docker", "exec", "hosting-site-m1-alpha", "wget", "-T", "1", "-q", "-O", "/dev/null", url], capture_output=True)
        assert blocked.returncode != 0, url
    assert not alpha["HostConfig"]["PortBindings"] and not beta["HostConfig"]["PortBindings"]
    record("network_isolation", "cross-site DNS/IP and Caddy admin unreachable; backend ports unpublished")


def quota_failure():
    # Simulate a interrupted setup whose data root lacks the promised project quota.
    # Only the named empty disposable fixture is modified. Ordinary retries use the worker.
    command(["systemctl", "stop", "reeve-worker"])
    ledger = Ledger(LEDGER)
    row = ledger.submit(str(uuid.uuid4()), {"name": "m1-quota-fault", "domain": "m1-quota-fault.hosting.test", "data_mb": 16, "layer_mb": 16})
    root = SITES / row["name"]
    root.mkdir(mode=0o711)
    atomic(root / ".hosting-operation", row["id"])
    (root / "html").mkdir(mode=0o700)
    os.chown(root / "html", row["uid"], row["uid"])
    try:
        command(["systemctl", "start", "reeve-worker"])
        failed = wait(row["id"], "recovery-needed")
        assert failed["step"] == "site data quota", failed
        assert not Host().inspect("hosting-site-m1-quota-fault")
        assert row["domain"] not in json.loads((PROXY / "routes.json").read_text())
        assert "Uploaded m1-beta" in https("m1-beta.hosting.test")
        record("quota_verification_failure", {"job": row["id"], "state": failed["state"], "container_and_route": "absent"})
    finally:
        command(["systemctl", "start", "reeve-worker"])
    # Undo only the empty fault-injection directory. Retry now applies the real quota.
    (root / "html").rmdir()
    rpc({"op": "retry", "id": row["id"]})
    assert wait(row["id"])["uid"] == row["uid"]
    record("quota_failure_retry", "same reserved identity succeeded")


def proxy_failure():
    path = PROXY / "routes.json"
    saved = path.read_text()
    old_config = (PROXY / "conf/Caddyfile").read_bytes()
    bad = json.loads(saved)
    bad["invalid {"] = {"upstream": "missing:8080", "network": "hosting-ingress-m1-alpha", "operation": "fault-fixture"}
    atomic(path, json.dumps(bad))
    try:
        row = submit("m1-proxy-fault")
        failed = wait(row["id"], "recovery-needed")
        assert failed["step"] == "proxy validation and publication", failed
        assert (PROXY / "conf/Caddyfile").read_bytes() == old_config
        assert "Uploaded m1-alpha" in https("m1-alpha.hosting.test")
        record("proxy_validation_failure", {"job": row["id"], "state": failed["state"], "working_route": "preserved"})
    finally:
        atomic(path, saved)
    rpc({"op": "retry", "id": row["id"]})
    assert wait(row["id"])["uid"] == row["uid"]
    record("proxy_failure_retry", "same identity succeeded")


def interrupt(reboot=False):
    row = submit("m1-reboot" if reboot else "m1-interrupt")
    ledger = Ledger(LEDGER)
    end = time.monotonic() + 30
    while time.monotonic() < end:
        current = ledger.get(row["id"])
        if current["state"] == "running" and (SITES / row["name"] / "hosting.yaml").exists():
            break
        time.sleep(0.01)
    else:
        raise AssertionError("Did not catch creation in progress")
    command(["systemctl", "kill", "--kill-whom=main", "--signal=SIGSTOP", "reeve-worker"])
    record("reboot_pending" if reboot else "interrupted", {"id": row["id"], "uid": row["uid"], "step": current["step"],
        "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()})
    if reboot:
        # SIGKILL avoids systemd's stop timeout waiting on the deliberately frozen worker.
        command(["systemctl", "kill", "--kill-whom=all", "--signal=SIGKILL", "reeve-worker"])
        command(["systemctl", "reboot"])
        return
    command(["systemctl", "kill", "--kill-whom=all", "--signal=SIGKILL", "reeve-worker"])
    wait(row["id"], "recovery-needed")
    rpc({"op": "retry", "id": row["id"]})
    restored = wait(row["id"])
    assert restored["uid"] == row["uid"]
    record("interrupted_retry", {"id": restored["id"], "uid": restored["uid"], "state": restored["state"]})


def after_reboot():
    data = json.loads(RESULTS.read_text())
    pending = data["reboot_pending"]
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert boot != pending["boot"]
    row = Ledger(LEDGER).get(pending["id"])
    assert row["state"] == "recovery-needed", row
    rpc({"op": "retry", "id": row["id"]})
    assert wait(row["id"])["uid"] == pending["uid"]
    for name in ("m1-alpha", "m1-beta"):
        assert f"Uploaded {name}" in https(name + ".hosting.test")
    existing()
    record("reboot_recovery", {"boot": boot, "pending_job": pending["id"], "state": "succeeded", "existing_sites": "healthy"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["basic", "quota", "proxy", "interrupt", "reboot", "after-reboot"])
    phase = parser.parse_args().phase
    assert os.getuid() == 0
    assert command(["hostname"]).strip() == "hosting", "Only run on the named test VM"
    if phase == "basic":
        upload_and_limits()
        conflicts_and_boundary()
    elif phase == "proxy":
        proxy_failure()
    elif phase == "quota":
        quota_failure()
    elif phase == "interrupt":
        interrupt()
    elif phase == "reboot":
        interrupt(reboot=True)
    else:
        after_reboot()
