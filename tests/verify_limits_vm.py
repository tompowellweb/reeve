"""Verify actual uncapped containers and retained explicit quotas on VM fixtures."""
import json
from pathlib import Path

from reeve.host import Host, command, project_id, quota_record
from reeve.worker import rpc
from tests.verify_php_vm import record, request


if __name__ == "__main__":
    rows = {r["name"]: r for r in rpc({"op": "list"})}
    results = {}
    for name in ("m2-unlimited-static", "m2-unlimited-php", "m2-unlimited-php70"):
        row = rows[name]
        assert row["state"] == "succeeded" and row["health"]["application"] == "healthy", row
        assert quota_record(row["project"])["hard_bytes"] == 1024**3
        names = ["hosting-site-" + name]
        if json.loads(row["payload"]).get("runtime") == "php":
            names.append("hosting-php-" + name)
        for container in names:
            live = Host().inspect(container)
            conf = live["HostConfig"]
            assert conf["Memory"] == 0 and conf["NanoCpus"] == 0 and conf["CpuQuota"] <= 0
            assert conf.get("PidsLimit") in (None, 0, -1)
            layer = Path(live["GraphDriver"]["Data"]["UpperDir"])
            project, _ = project_id(layer)
            quota = quota_record(project)
            assert quota["hard_bytes"] == 0
            cgroups = command(["docker", "exec", container, "sh", "-c", "cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max /sys/fs/cgroup/pids.max"]).splitlines()
            assert cgroups[0] == "max" and cgroups[1].startswith("max ") and cgroups[2] == "max", cgroups
            results[container] = {"memory_bytes": conf["Memory"], "nano_cpus": conf["NanoCpus"], "pids_limit": conf.get("PidsLimit"),
                "storage_opt": conf["StorageOpt"], "layer_project": project, "layer_hard_bytes": quota["hard_bytes"],
                "cgroup_limits": cgroups, "site_data_hard_bytes": 1024**3}
    php = "hosting-php-m2-unlimited-php"
    allocation = command(["docker", "exec", php, "php", "-r", '$x=str_repeat("x",160*1024*1024); echo strlen($x);'])
    assert allocation == str(160 * 1024**2)
    # Prove size=0 overrides the engine's 1 GiB default, then immediately remove test data.
    try:
        command(["docker", "exec", php, "dd", "if=/dev/zero", "of=/tmp/unlimited-proof.bin", "bs=1M", "count=1056"], timeout=60)
        size = command(["docker", "exec", php, "stat", "-c", "%s", "/tmp/unlimited-proof.bin"]).strip()
        assert int(size) == 1056 * 1024**2
        assert request(rows["m2-php84"])[0] == 200
    finally:
        command(["docker", "exec", php, "rm", "-f", "/tmp/unlimited-proof.bin"])
    for name in ("m2-php70", "m2-php84"):
        live = Host().inspect("hosting-php-" + name)
        assert live["HostConfig"]["Memory"] == 224 * 1024**2
        assert live["HostConfig"]["StorageOpt"]["size"] == "16m"
    record("unlimited_defaults", {"containers": results, "php_allocated_bytes": int(allocation),
        "layer_write_bytes": int(size), "test_file_removed": True, "explicit_caps": "retained", "neighbour": "healthy"})
    final = rpc({"op": "list"})
    assert all(r["state"] == "succeeded" and r["health"]["application"] == "healthy" for r in final)
    record("final_sites", [{key: r[key] for key in ("id", "name", "uid", "project", "state", "health", "domains", "domain_job")} for r in final])
