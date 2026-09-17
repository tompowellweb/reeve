import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from reeve.core import Ledger, validate_create
from reeve.worker import dispatch


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "jobs.db", sites=tmp_path / "sites")


def inputs(**overrides):
    return {"name": "alpha", "domain": "alpha.hosting.test", **overrides}


@pytest.mark.parametrize("overrides", [
    {"name": "../x"}, {"name": "alpha;id"}, {"domain": "alpha.hosting.test\n{ file_server }"},
    {"domain": "a.hosting.test"}, {"domain": "127.0.0.1"}, {"data_mb": 0}, {"layer_mb": True},
    {"cpus": float("nan")}, {"memory_mb": 128.5}, {"command": "id"}, {"name": "edge-proof-x"},
])
def test_invalid_requests(overrides):
    with pytest.raises(ValueError):
        validate_create(inputs(**overrides))


def test_idempotency_survives_reopen_and_concurrency(ledger):
    ident = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: ledger.submit(ident, inputs()), range(16)))
    assert len({row["uid"] for row in rows}) == 1
    reopened = Ledger(ledger.path, ledger.sites)
    assert len(reopened.list()) == 1
    assert reopened.submit(ident, inputs())["id"] == ident
    with pytest.raises(ValueError, match="different inputs"):
        reopened.submit(ident, inputs(name="beta"))


def test_conflicts_and_unmanaged_symlink(ledger):
    ledger.submit(str(uuid.uuid4()), inputs())
    for data in (inputs(domain="other.hosting.test"), inputs(name="beta")):
        with pytest.raises(ValueError, match="reserved"):
            ledger.submit(str(uuid.uuid4()), data)
    ledger.sites.mkdir()
    (ledger.sites / "unmanaged").symlink_to("/nonexistent")
    with pytest.raises(ValueError, match="unmanaged"):
        ledger.submit(str(uuid.uuid4()), inputs(name="unmanaged"))


def test_recovery_retains_identity_and_success_cannot_be_requeued(ledger):
    row = ledger.submit(str(uuid.uuid4()), inputs())
    ledger.update(row["id"], "running", "proxy validation")
    ledger.interrupted()
    interrupted = ledger.get(row["id"])
    assert interrupted["state"] == "recovery-needed"
    assert interrupted["step"] == "proxy validation"
    assert ledger.retry(row["id"])["uid"] == row["uid"]
    ledger.update(row["id"], "succeeded", "published")
    assert ledger.retry(row["id"])["state"] == "succeeded"


@pytest.mark.parametrize("message", [{"op": "exec", "command": "id"}, {"op": "list", "command": "id"}, [], {"op": "retry", "id": "../../root"}])
def test_fixed_worker_protocol(ledger, message):
    with pytest.raises(ValueError):
        dispatch(message, ledger, None)


def test_worker_resolves_server_defaults_before_recording(ledger):
    host = SimpleNamespace(defaults={"data_mb": 2048, "memory_mb": 256})
    row = dispatch({"op": "create", "id": str(uuid.uuid4()), "data": inputs(layer_mb=32)}, ledger, host)
    data = json.loads(row["payload"])
    assert data["data_mb"] == 2048 and data["memory_mb"] == 256 and data["layer_mb"] == 32
