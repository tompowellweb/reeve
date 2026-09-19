import json
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from reeve.core import Ledger, validate_create, validate_domains


def ident():
    return str(uuid.uuid4())


@pytest.mark.parametrize("domains", [[], ["a.example.com"] * 2, ["*.example.com"], ["UPPER.example.com"],
    ["a.example.com\n{evil}"], ["https://example.com"], ["example.com:443"], ["127.0.0.1"], ["a.hosting.test"],
    [f"a{i}.example.com" for i in range(21)]])
def test_invalid_domains(domains):
    with pytest.raises(ValueError):
        validate_domains(domains)


def test_create_alias_uniqueness_and_atomic_reservation(tmp_path):
    ledger = Ledger(tmp_path / "jobs.db", tmp_path / "sites")
    def create(i):
        try:
            return ledger.submit(ident(), {"name": f"site{i}", "domain": f"site{i}.example.com", "aliases": ["shared.example.com"]})
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (1, 2)))
    assert len([row for row in results if row]) == 1
    assert len(ledger.list()) == 1
    row = ledger.list()[0]
    assert "shared.example.com" in ledger.domains(row)
    with pytest.raises(ValueError, match="reserved"):
        ledger.submit(ident(), {"name": "third", "domain": "shared.example.com"})


def test_domain_change_recovery_primary_removal_and_idempotency(tmp_path):
    ledger = Ledger(tmp_path / "jobs.db", tmp_path / "sites")
    initial = {"name": "live", "domain": "site.yoursitepreview.co.uk", "aliases": ["livesite.co.uk"]}
    row = ledger.submit(ident(), initial)
    ledger.update(row["id"], "succeeded", "published")
    job = ledger.submit_domains(ident(), row["id"], ["livesite.co.uk", "www.livesite.co.uk"])
    assert ledger.submit_domains(job["id"], row["id"], json.loads(job["payload"]))["id"] == job["id"]
    with pytest.raises(ValueError, match="different inputs"):
        ledger.submit_domains(job["id"], row["id"], ["different.co.uk"])
    with pytest.raises(ValueError, match="pending"):
        ledger.submit_domains(ident(), row["id"], ["other.co.uk"])
    with pytest.raises(ValueError, match="reserved"):
        ledger.submit(ident(), {"name": "conflict", "domain": "www.livesite.co.uk"})
    assert ledger.domains(row) == [initial["domain"], "livesite.co.uk"]
    ledger.update_domains(job["id"], "running")
    reopened = Ledger(ledger.path, ledger.sites)
    reopened.interrupted()
    assert reopened.domain_jobs()[0]["state"] == "recovery-needed"
    reopened.retry_domains(job["id"])
    reopened.finish_domains(job)
    current = reopened.get(row["id"])
    assert reopened.domains(current) == ["livesite.co.uk", "www.livesite.co.uk"]
    assert reopened.submit(row["id"], initial)["payload"] == row["payload"]
    assert reopened.retry_domains(job["id"])["state"] == "succeeded"
    reused = reopened.submit(ident(), {"name": "reuse", "domain": initial["domain"]})
    assert reused["domain"] == initial["domain"]


def test_existing_schema_migrates_without_changing_create_inputs(tmp_path):
    path = tmp_path / "jobs.db"
    rowid = ident()
    payload = json.dumps(validate_create({"name": "old", "domain": "old.hosting.test"}), sort_keys=True)
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE jobs(id TEXT PRIMARY KEY,name TEXT UNIQUE,domain TEXT UNIQUE,payload TEXT,uid INTEGER UNIQUE,project INTEGER UNIQUE,state TEXT,step TEXT,error TEXT,created REAL,updated REAL); PRAGMA user_version=2;")
        db.execute("INSERT INTO jobs VALUES (?,?,?,?,30000,100000,'succeeded','published','',1,1)", (rowid, "old", "old.hosting.test", payload))
    ledger = Ledger(path)
    assert ledger.get(rowid)["payload"] == payload
    assert ledger.domains(ledger.get(rowid)) == ["old.hosting.test"]
    with ledger.db() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 17


def test_a_note_on_a_finished_change_keeps_the_record_and_explains(tmp_path):
    ledger = Ledger(tmp_path / 'jobs.db', tmp_path / 'sites')
    site = ledger.submit(ident(), {'name': 'shop', 'domain': 'shop.example.com'})
    ledger.update(site['id'], 'succeeded', 'published')
    job = ledger.submit_domains(ident(), site['id'], ['new.example', 'www.new.example'])
    ledger.note_domains(job['id'], 'not yet')  # not finished: nothing to annotate
    assert ledger.domain_jobs(site['id'])[0]['error'] == ''
    ledger.finish_domains(job)
    ledger.note_domains(job['id'], 'Names and routes changed. The site did not answer on them yet: curl (35)')
    latest = ledger.domain_jobs(site['id'])[0]
    assert latest['state'] == 'succeeded' and latest['error'].startswith('Names and routes changed')
    assert ledger.domains(ledger.get(site['id'])) == ['new.example', 'www.new.example']
