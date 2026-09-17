"""Domain-change failure/restart/retry on disposable PHP and multi-domain fixtures."""
import json
import time
import uuid

import yaml

from reeve.core import Ledger
from reeve.host import Host, PROXY, SITES, command
from reeve.worker import rpc
from tests.verify_php_vm import record, request


def wait(ledger, ident, state):
    for _ in range(300):
        job = next(j for j in ledger.domain_jobs() if j["id"] == ident)
        if job["state"] == state:
            return job
        if state == "succeeded" and job["state"] in ("failed", "recovery-needed"):
            raise AssertionError(job)
        time.sleep(0.1)
    raise AssertionError(job)


if __name__ == "__main__":
    ledger = Ledger("/srv/ops/panel/worker/jobs.sqlite3")
    rows = {row["name"]: row for row in rpc({"op": "list"})}
    row = rows["m2-php70"]
    routes_before = (PROXY / "routes.json").read_bytes()
    image_before = Host().inspect("hosting-php-m2-php70")["Image"]
    domains = [*row["domains"], f"m2-php70-preview{len(ledger.domain_jobs(row['id'])) + 1}.hosting.test"]
    try:
        command(["docker", "stop", "--time", "2", "hosting-php-m2-php70"])
        job = rpc({"op": "domains", "id": str(uuid.uuid4()), "site_id": row["id"], "domains": domains})
        failed = wait(ledger, job["id"], "recovery-needed")
        assert "healthy" in failed["error"]
        assert (PROXY / "routes.json").read_bytes() == routes_before
        try:
            rpc({"op": "create", "id": str(uuid.uuid4()), "data": {"name": "m2-domain-conflict", "domain": domains[-1]}})
        except ValueError as exc:
            assert "reserved" in str(exc)
        else:
            raise AssertionError("Pending hostname was not reserved")
        command(["systemctl", "restart", "reeve-worker"])
        assert wait(ledger, job["id"], "recovery-needed")["payload"] == job["payload"]
    finally:
        command(["docker", "start", "hosting-php-m2-php70"])
    for _ in range(30):
        if Host().health(row)["application"] == "healthy":
            break
        time.sleep(1)
    rpc({"op": "retry-domains", "id": job["id"]})
    wait(ledger, job["id"], "succeeded")
    for domain in domains:
        status, body = request(dict(row, domain=domain), "/acceptance.php")
        assert status == 200 and json.loads(body)["uid"] == row["uid"]
    assert Host().inspect("hosting-php-m2-php70")["Image"] == image_before
    metadata = yaml.safe_load((SITES / row["name"] / "hosting.yaml").read_text())
    assert metadata["domain"] == domains[0] and metadata["aliases"] == domains[1:]
    routes = json.loads((PROXY / "routes.json").read_text())
    assert "m2-domains.hosting.test" not in routes
    live = routes["m2-domains-live.hosting.test"]
    assert live == routes["m2-domains-preview.hosting.test"]
    try:
        status, body = request(dict(rows["m2-domains"], domain="m2-domains.hosting.test"))
        assert "m2-domains is ready" not in body
    except RuntimeError as exc:
        # Caddy may evict the removed certificate as well as its route.
        assert "curl failed (35)" in str(exc) and "TLS connect error" in str(exc), exc
        status = "TLS handshake refused"
    record("domain_recovery", {"job": job["id"], "failure": failed["error"], "failed_change_routes": "unchanged",
        "reservation": "held across worker restart", "retry": "succeeded", "domains": domains,
        "uid": row["uid"], "image_unchanged": image_before, "removed_hostname_http_status": status,
        "removed_hostname": "absent from routes and does not serve the former site"})
    final = rpc({"op": "list"})
    assert all(r["state"] == "succeeded" and r["health"]["application"] == "healthy" for r in final)
    assert all(j["state"] == "succeeded" for j in ledger.domain_jobs())
    record("final_sites", [{key: r[key] for key in ("id", "name", "uid", "project", "state", "health", "domains", "domain_job")} for r in final])
