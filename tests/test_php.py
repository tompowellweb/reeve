import json
import uuid

import pytest

from reeve.core import Ledger, validate_create
from reeve.php_site import compose_services, nginx
from reeve.host import NGINX


@pytest.mark.parametrize("fields", [
    {"runtime": "php"}, {"runtime": "php", "php_version": "7.0;id"},
    {"runtime": "php", "php_version": "8.6.1"}, {"runtime": "static", "php_version": "7.0"},
    {"runtime": "php", "php_version": "8.4", "memory_mb": 64}, {"runtime": "docker"},
])
def test_reject_unsupported_or_unsafe_runtime(fields):
    with pytest.raises(ValueError):
        validate_create({"name": "php-test", "domain": "php-test.hosting.test", **fields})


def test_schema_upgrade_and_static_request_compatibility(tmp_path):
    ledger = Ledger(tmp_path / "jobs.db", tmp_path / "sites")
    static = {"name": "old-static", "domain": "old-static.hosting.test"}
    row = ledger.submit(str(uuid.uuid4()), static)
    assert ledger.submit(row["id"], {**static, "runtime": "static"})["payload"] == row["payload"]
    with ledger.db() as db:
        assert db.execute("pragma user_version").fetchone()[0] == 17
    php = ledger.submit(str(uuid.uuid4()), {"name": "php-test", "domain": "php-test.hosting.test", "runtime": "php", "php_version": "7.0"})
    reopened = Ledger(ledger.path, ledger.sites)
    with reopened.db() as db:
        assert db.execute("pragma user_version").fetchone()[0] == 17
    assert json.loads(reopened.get(php["id"])["payload"])["php_version"] == "7.0"
    assert reopened.get(row["id"])["payload"] == row["payload"]


def test_php_keeps_total_budget_and_has_no_ingress_membership(tmp_path):
    web = {"volumes": [], "networks": {"ingress": {}}, "user": "30000:30000", "restart": "unless-stopped",
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 64,
        "storage_opt": {"size": "16m"}, "logging": {"driver": "local"}, "labels": {"hosting.operation": "test"}}
    compose = {"services": {"web": web}, "networks": {"ingress": {}}}
    compose_services(compose, tmp_path, {"memory_mb": 256, "cpus": 0.5, "php_version": "7.0"},
                     {"name": "php-test"}, "hosting-php-site:test", "hosting-backend-php-test")
    php = compose["services"]["php"]
    assert int(web["mem_limit"][:-1]) + int(php["mem_limit"][:-1]) == 256
    assert web["cpus"] + php["cpus"] == 0.5
    assert set(php["networks"]) == {"backend"}
    assert "ports" not in php and "privileged" not in php
    assert php["user"] == web["user"]


def test_php_handler_never_falls_back_to_source_serving():
    config = nginx(NGINX)
    assert "fastcgi_pass php:9000;" in config
    assert "try_files $uri =404;" in config
    assert config.index("wp-config") < config.index(r"location ~ \.php$")
    assert "disable_symlinks on" in config
    assert "include /etc/hosting/site.nginx.conf" in config
