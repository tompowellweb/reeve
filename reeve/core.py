"""Validated protocol and durable worker ledger; no subprocesses in this module."""
import contextlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

DEFAULTS = {"data_mb": 1024, "layer_mb": None, "memory_mb": None, "cpus": None, "pids_limit": None}
BOUNDS = {"data_mb": (16, 102400), "layer_mb": (16, 2048), "memory_mb": (32, 4096), "cpus": (0.1, 4), "pids_limit": (16, 4096)}
STATES = ("queued", "running", "succeeded", "failed", "recovery-needed", "deleted")
def php_branch(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]?\.[0-9]{1,2}", value):
        raise ValueError("Choose a PHP branch such as 8.2")
    return value


def request_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Use a canonical UUID request identifier")
    return value


def validate_domains(domains):
    if not isinstance(domains, list) or not 1 <= len(domains) <= 20:
        raise ValueError("Use 1–20 domain names per site")
    for domain in domains:
        if not isinstance(domain, str) or len(domain) > 253 or domain != domain.lower():
            raise ValueError("Use a lowercase DNS hostname")
        labels = domain.split(".")
        if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels) or not re.search("[a-z]", labels[-1]):
            raise ValueError("Use a DNS hostname without scheme, port, wildcard or path")
        if domain in ("a.hosting.test", "b.hosting.test"):
            raise ValueError("This hostname belongs to a retained infrastructure fixture")
    if len(set(domains)) != len(domains):
        raise ValueError("List each domain only once")
    return domains[:1] + sorted(domains[1:])


def validate_create(data):
    if not isinstance(data, dict) or set(data) - {"name", "domain", "aliases", "runtime", "php_version", "database", *DEFAULTS}:
        raise ValueError("Unknown Create fields")
    name, domain = data.get("name", ""), data.get("domain", "")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}[a-z0-9]|[a-z]", name):
        raise ValueError("Name must be 1–32 lowercase letters, digits or internal hyphens")
    if name.startswith(("hosting-", "edge-proof-", "podman-proof-")):
        raise ValueError("This name is reserved for infrastructure")
    aliases = data.get("aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("Additional domains must be a list")
    domains = validate_domains([domain, *aliases])
    result = {"name": name, "domain": domains[0]}
    if len(domains) > 1:
        result["aliases"] = domains[1:]
    runtime = data.get("runtime", "static")
    if runtime not in ("static", "php"):
        raise ValueError("Choose static or PHP hosting")
    if runtime == "php":
        php_branch(data.get("php_version"))
        result.update(runtime="php", php_version=data["php_version"])
    elif data.get("php_version"):
        raise ValueError("A static site cannot select a PHP branch")
    for key, default in DEFAULTS.items():
        value = data.get(key, default)
        if value is None and key != "data_mb":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Invalid {key}")
        low, high = BOUNDS[key]
        if not low <= value <= high or (key != "cpus" and int(value) != value):
            raise ValueError(f"{key} must be between {low} and {high}")
        result[key] = value if key == "cpus" else int(value)
    if runtime == "php" and result.get("memory_mb") is not None and result["memory_mb"] < 128:
        raise ValueError("PHP sites need at least 128 MiB total memory")
    if data.get("database") is not None:
        from .database_jobs import validate_database
        result["database"] = validate_database(data["database"])
    return result


from .domains import DomainLedger
from .runtime_jobs import RuntimeLedger


from .database_jobs import DatabaseLedger


from .content_jobs import ContentLedger
from .backup_jobs import BackupLedger


class Ledger(DomainLedger, RuntimeLedger, DatabaseLedger, ContentLedger, BackupLedger):
    def __init__(self, path, sites=Path("/srv/sites")):
        self.path, self.sites = Path(path), Path(sites)
        with self.db() as db:
            version = db.execute("pragma user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17):
                raise RuntimeError("Unsupported worker schema; select a compatible release")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, domain TEXT UNIQUE NOT NULL,
                  payload TEXT NOT NULL, uid INTEGER UNIQUE NOT NULL, project INTEGER UNIQUE NOT NULL,
                  state TEXT NOT NULL, step TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                  created REAL NOT NULL, updated REAL NOT NULL);
            """)
            self.initialize_domains(db, version)
            self.initialize_runtime_jobs(db)
            self.initialize_database_jobs(db)
            self.initialize_content_jobs(db)
            from .schedules import initialize
            initialize(db)
            from .compose_adopt import initialize as initialize_adoption
            initialize_adoption(db)
            self.initialize_backups(db)
            from .traffic import initialize as initialize_traffic
            initialize_traffic(db)
            from .restoration import initialize as initialize_recoveries
            initialize_recoveries(db)   # additive; older workers ignore it
            # Site backup/restore/delete records require this worker; older workers must refuse them.
            db.execute('PRAGMA user_version=17')

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, ident, data):
        ident, data = request_id(ident), validate_create(data)
        payload = json.dumps(data, sort_keys=True)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
            if prior:
                if prior["payload"] != payload:
                    raise ValueError("Request identifier already belongs to different inputs")
                return dict(prior)
            root = self.sites / data["name"]
            if root.exists() or root.is_symlink():
                raise ValueError("An unmanaged folder already uses this name; nothing was changed")
            # Separate from proof IDs, Docker's million-range and normal login users.
            ordinal = db.execute("SELECT coalesce(max(uid),29999)+1 FROM jobs").fetchone()[0]
            if ordinal >= 60000:
                raise ValueError("Site identity range exhausted")
            now = time.time()
            try:
                db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,'queued','reserved','',?,?)",
                           (ident, data["name"], data["domain"], payload, ordinal, ordinal + 70000, now, now))
                for domain in [data["domain"], *data.get("aliases", [])]:
                    db.execute("INSERT INTO site_domains VALUES (?,?,1)", (domain, ident))
            except sqlite3.IntegrityError:
                raise ValueError("Site name or domain is already reserved") from None
        return self.get(ident)

    def get(self, ident):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise ValueError("Unknown operation")
        return dict(row)

    def list(self):
        with self.db() as db:
            return [dict(x) for x in db.execute("SELECT * FROM jobs WHERE state!='deleted' ORDER BY created DESC")]

    def update(self, ident, state, step, error=""):
        if state not in STATES:
            raise ValueError("Unknown operation state")
        with self.db() as db:
            db.execute("UPDATE jobs SET state=?, step=?, error=?, updated=? WHERE id=?",
                       (state, step, error[:2000], time.time(), ident))

    def interrupted(self):
        with self.db() as db:
            db.execute("UPDATE backup_jobs SET state='recovery-needed' WHERE state='running'")
            db.execute("UPDATE site_backups SET state='recovery-needed' WHERE state='running'")
            db.execute("UPDATE site_deletes SET state='recovery-needed', error='Worker interrupted; retry the saved deletion' WHERE state='running'")
            db.execute("UPDATE content_jobs SET state='recovery-needed', error='Worker interrupted; inspect the result and resolve before submitting another operation. Never automatically replay SQL or commands.' WHERE state='running'")
            db.execute("UPDATE database_jobs SET state='recovery-needed', error='Worker interrupted; retry the saved database setup' WHERE state='running'")
            db.execute("UPDATE runtime_jobs SET state='recovery-needed', error='Worker interrupted; retry the saved operation' WHERE state='running'")
            db.execute("UPDATE domain_jobs SET state='recovery-needed', error='Worker interrupted; retry the saved domain change', updated=? WHERE state='running'", (time.time(),))
            db.execute("UPDATE jobs SET state='recovery-needed', error=?, updated=? WHERE state='running'",
                       ("Worker interrupted. Retry reconciles the same reserved resources.", time.time()))

    def retry(self, ident):
        request_id(ident)
        with self.db() as db:
            db.execute("UPDATE jobs SET state='queued', error='', updated=? WHERE id=? AND state IN ('failed','recovery-needed')",
                       (time.time(), ident))
        return self.get(ident)
