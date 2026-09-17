import json
import uuid

import pytest

from reeve.core import Ledger, validate_create
from reeve.host import container_limits
from reeve.php_site import compose_services


@pytest.mark.parametrize("runtime", ["static", "php"])
def test_only_site_data_is_defaulted_and_zero_overrides_daemon_layer_cap(tmp_path, runtime):
    data = {"name": "limits", "domain": "limits.hosting.test", "runtime": runtime}
    if runtime == "php":
        data["php_version"] = "8.4"
    validated = validate_create(data)
    assert validated["data_mb"] == 1024
    assert all(key not in validated for key in ("memory_mb", "cpus", "layer_mb", "pids_limit"))
    limits = container_limits(validated)
    assert limits == {"storage_opt": {"size": "0"}, "pids_limit": -1}
    if runtime == "php":
        web = dict(limits, volumes=[], networks={"ingress": {}}, user="30000:30000", restart="unless-stopped",
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], logging={}, labels={})
        compose = {"services": {"web": web}, "networks": {}}
        compose_services(compose, tmp_path, validated, {"name": "limits"}, "hosting-php:test", "backend")
        for service in compose["services"].values():
            assert "mem_limit" not in service and "cpus" not in service
            assert service["storage_opt"]["size"] == "0" and service["pids_limit"] == -1


def test_explicit_caps_survive_default_change_and_retry(tmp_path):
    ledger = Ledger(tmp_path / "jobs.db", tmp_path / "sites")
    historical = {"name": "old", "domain": "old.hosting.test", "data_mb": 1024, "layer_mb": 128, "memory_mb": 128, "cpus": 0.5}
    row = ledger.submit(str(uuid.uuid4()), historical)
    assert json.loads(row["payload"]) == historical
    assert Ledger(ledger.path, ledger.sites).submit(row["id"], historical)["payload"] == row["payload"]
    optional = validate_create({**historical, "layer_mb": None, "memory_mb": None, "cpus": None, "pids_limit": None})
    assert container_limits(optional) == {"storage_opt": {"size": "0"}, "pids_limit": -1}
    limited = validate_create({**historical, "pids_limit": 100})
    assert container_limits(limited) == {"storage_opt": {"size": "128m"}, "pids_limit": 100, "mem_limit": "128m", "cpus": 0.5}
