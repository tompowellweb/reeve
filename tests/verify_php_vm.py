"""M2.1 acceptance using only disposable m2-php* sites on the hosting test VM."""
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

from reeve.core import Ledger
from reeve.host import Host, PROXY, SITES, NGINX, atomic, command, quota_record
from reeve.php_site import nginx
from reeve.php_runtime import REQUIRED_EXTENSIONS, catalog
from reeve.worker import rpc

RESULTS = Path("/srv/ops/panel/worker/php-acceptance.json")


def record(key, value):
    data = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    data[key] = value
    atomic(RESULTS, json.dumps(data, indent=2))
    print(key + ": " + json.dumps(value), flush=True)


def request(row, path="/"):
    result = command(["curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "5",
        "--cacert", PROXY / "data/caddy/pki/authorities/local/root.crt", "--resolve",
        f"{row['domain']}:443:127.0.0.1", "--write-out", "\n%{http_code}", f"https://{row['domain']}{path}"])
    body, code = result.rsplit("\n", 1)
    return int(code), body


def write(row, name, content):
    assert row["name"].startswith("m2-php")
    command(["setpriv", f"--reuid={row['uid']}", f"--regid={row['uid']}", "--clear-groups", "python3", "-c",
        "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(sys.argv[2])",
        SITES / row["name"] / "html" / name, content])


def wait(ident, state="succeeded"):
    ledger = Ledger(Path("/srv/ops/panel/worker/jobs.sqlite3"))
    for _ in range(600):
        row = ledger.get(ident)
        if row["state"] == state:
            return row
        if state == "succeeded" and row["state"] in ("failed", "recovery-needed"):
            raise AssertionError(row)
        time.sleep(0.1)
    raise AssertionError("Timed out: " + ident)


def execution():
    rows = {r["name"]: r for r in rpc({"op": "list"})}
    for name, branch in (("m2-php70", "7.0"), ("m2-php84", "8.4")):
        row = rows[name]
        assert row["state"] == "succeeded"
        php = "hosting-php-" + name
        # Refresh only these known test fixtures after the bounded FastCGI-connect fix.
        atomic(SITES / name / "conf/nginx.conf", nginx(NGINX), 0o644)
        command(["docker", "compose", "-f", SITES / name / "compose.yml", "up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "30", "web"])
        write(row, "acceptance.php", '<?php header("Content-Type: application/json"); echo json_encode(array("version"=>PHP_VERSION,"uid"=>posix_geteuid(),"extensions"=>get_loaded_extensions()));')
        code, body = request(row, "/acceptance.php")
        info = json.loads(body)
        assert code == 200 and info["version"].startswith(branch + ".") and info["uid"] == row["uid"], info
        assert REQUIRED_EXTENSIONS.issubset(info["extensions"]), info
        cli = command(["docker", "exec", php, "php", "-r", 'file_put_contents("/site/cli-upload.txt","written by matching CLI"); echo PHP_VERSION;'])
        assert cli == info["version"]
        assert (SITES / name / "html/cli-upload.txt").stat().st_uid == row["uid"]
        assert request(row, "/cli-upload.txt") == (200, "written by matching CLI")
        guarded = ("secret.php.jpg", "secret.PHP", ".env", "wp-config.php", "settings.php", "uploads/code.php", "wp-content/uploads/code.php", "sites/default/files/code.php")
        for filename in guarded:
            write(row, filename, "source-must-not-leak")
            assert request(row, "/" + filename)[0] == 404, filename
        command(["setpriv", f"--reuid={row['uid']}", f"--regid={row['uid']}", "--clear-groups", "ln", "-sf", "/etc/passwd", SITES / name / "html/linked.txt"])
        status, body = request(row, "/linked.txt")
        # The front controller may handle a missing/symlink path, but never serves its target.
        assert "root:x:" not in body and (status in (403, 404) or (status == 200 and name + " is ready" in body))
        command(["setpriv", f"--reuid={row['uid']}", f"--regid={row['uid']}", "--clear-groups", "ln", "-sf", "/etc/passwd", SITES / name / "html/linked.php"])
        assert request(row, "/linked.php")[0] == 404
        try:
            command(["docker", "stop", "--time", "2", php])
            status, body = request(row, "/acceptance.php")
            assert status in (502, 504) and "get_loaded_extensions" not in body, (status, body)
            assert Host().health(row)["application"] == "unhealthy"
        finally:
            command(["docker", "start", php])
        for _ in range(30):
            if Host().health(row)["application"] == "healthy":
                break
            time.sleep(1)
        assert Host().health(row)["application"] == "healthy"
        details = Host().inspect(php)
        networks = details["NetworkSettings"]["Networks"]
        assert list(networks) == ["hosting-backend-" + name]
        assert not details["HostConfig"]["PortBindings"]
        assert details["Config"]["User"] == f"{row['uid']}:{row['uid']}"
        web = Host().inspect("hosting-site-" + name)
        assert details["HostConfig"]["Memory"] + web["HostConfig"]["Memory"] == 256 * 1024**2
        info.update(job=row["id"], cli=cli, protected_paths=list(guarded), stopped_fpm_status=status,
                    image_id=details["Image"], networks=list(networks), published_ports=False)
        record(name, info)
    neighbour = Host().inspect("hosting-php-m2-php84")
    neighbour_ip = next(iter(neighbour["NetworkSettings"]["Networks"].values()))["IPAddress"]
    for host in (neighbour_ip, "hosting-php-m2-php84"):
        result = command(["docker", "exec", "hosting-php-m2-php70", "php", "-r",
            '$s=@fsockopen($argv[1],9000,$e,$m,1); if($s){exit(1);} echo "blocked";', host])
        assert result == "blocked"
    record("isolation", "FPM has only its internal site backend; cross-site FPM DNS/IP blocked; no published FPM port")
    return rows


def limits(rows):
    row = rows["m2-php70"]
    for path, label in (("/tmp/php-quota-fill.bin", "fpm_layer"), ("/site/php-quota-fill.bin", "site_data")):
        try:
            result = subprocess.run(["docker", "exec", "hosting-php-m2-php70", "dd", "if=/dev/zero", "of=" + path, "bs=1M", "count=32"], capture_output=True, text=True, timeout=30)
            assert result.returncode != 0 and any(x in result.stderr.lower() for x in ("quota", "space")), result
            assert request(rows["m2-php84"])[0] == 200
            record(label, {"exit": result.returncode, "error": result.stderr.strip(), "neighbour": "healthy", "data_quota": quota_record(row["project"])})
        finally:
            command(["docker", "exec", "hosting-php-m2-php70", "rm", "-f", path])


def retry_configuration():
    name = "m2-php-retry"
    # A labelled fixture network deliberately causes the normal collision check to fail,
    # after config generation and before any site container or route is published.
    network = "hosting-ingress-" + name
    command(["docker", "network", "create", "--internal", "--label", "hosting.test=m2-php-retry", network])
    try:
        row = rpc({"op": "create", "id": str(uuid.uuid4()), "data": {"name": name, "domain": name + ".hosting.test",
            "runtime": "php", "php_version": "7.0", "memory_mb": 256, "data_mb": 16, "layer_mb": 16}})
        failed = wait(row["id"], "recovery-needed")
        assert failed["step"] == "isolated network", failed
        root = SITES / name
        assert not Host().inspect("hosting-php-" + name)
        assert row["domain"] not in json.loads((PROXY / "routes.json").read_text())
        updates = {"conf/php.ini": "\n; acceptance preserved\nprecision=12\n", "conf/pool.conf": "\n; acceptance preserved\n",
            "conf/site.nginx.conf": "\nlocation = /custom-rule { return 200 'preserved'; }\n",
            "conf/Containerfile": "\n# acceptance preserved\n", ".env": "\nACCEPTANCE_CONFIG=preserved\n"}
        for file, suffix in updates.items():
            with (root / file).open("a") as stream:
                stream.write(suffix)
        hashes = {file: hashlib.sha256((root / file).read_bytes()).hexdigest() for file in updates}
        write(row, "retained.txt", "content survived retry")
    finally:
        command(["docker", "network", "rm", network])
    rpc({"op": "retry", "id": row["id"]})
    settled = wait(row["id"])
    assert settled["uid"] == row["uid"] and settled["project"] == row["project"]
    assert hashes == {file: hashlib.sha256((root / file).read_bytes()).hexdigest() for file in updates}
    assert request(row, "/retained.txt") == (200, "content survived retry")
    assert request(row, "/custom-rule") == (200, "preserved")
    assert command(["docker", "exec", "hosting-php-" + name, "php", "-r", 'echo ini_get("precision").":".getenv("ACCEPTANCE_CONFIG");']) == "12:preserved"
    record("configuration_retry", {"job": row["id"], "uid": row["uid"], "project": row["project"], "failure_step": failed["step"],
        "preserved_sha256": hashes, "content_and_configuration": "preserved and active"})


if __name__ == "__main__":
    rows = execution()
    limits(rows)
    retry_configuration()
    live = rpc({"op": "list"})
    assert all(row["state"] == "succeeded" and row["health"]["application"] == "healthy" for row in live)
    record("final_sites", [{key: row[key] for key in ("id", "name", "uid", "project", "state", "health")} for row in live])
    record("runtimes", catalog())
