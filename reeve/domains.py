"""Durable hostname reservations and changes; Create inputs remain immutable."""
import json
import time


class DomainLedger:
    def initialize_domains(self, db, version):
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS site_domains (domain TEXT PRIMARY KEY, site_id TEXT NOT NULL, active INTEGER NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS domain_jobs (
            id TEXT PRIMARY KEY, site_id TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL)""")
        if version < 3:
            for row in db.execute("SELECT id,domain FROM jobs").fetchall():
                db.execute("INSERT OR IGNORE INTO site_domains VALUES (?,?,1)", (row["domain"], row["id"]))


    def domains(self, row):
        with self.db() as db:
            aliases = [r[0] for r in db.execute("SELECT domain FROM site_domains WHERE site_id=? AND active=1 AND domain!=? ORDER BY domain", (row["id"], row["domain"]))]
        return [row["domain"], *aliases]

    def domain_jobs(self, site_id=None):
        with self.db() as db:
            rows = db.execute("SELECT * FROM domain_jobs WHERE site_id=? ORDER BY created DESC", (site_id,)) if site_id else db.execute("SELECT * FROM domain_jobs ORDER BY created")
            return [dict(row) for row in rows]

    def submit_domains(self, ident, site_id, domains):
        from .core import request_id, validate_domains
        ident, site_id = request_id(ident), request_id(site_id)
        payload = json.dumps(validate_domains(domains))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM domain_jobs WHERE id=?", (ident,)).fetchone()
            if prior:
                if prior["site_id"] != site_id or prior["payload"] != payload:
                    raise ValueError("Request identifier already belongs to different inputs")
                return dict(prior)
            row = db.execute("SELECT * FROM jobs WHERE id=?", (site_id,)).fetchone()
            if not row or row["state"] != "succeeded":
                raise ValueError("Finish site setup before changing domains")
            if self.content_pending(db, site_id) or self.runtime_pending(db, site_id) or self.database_pending(db, site_id):
                raise ValueError("Finish the pending PHP change first")
            if db.execute("SELECT 1 FROM domain_jobs WHERE site_id=? AND state!='succeeded'", (site_id,)).fetchone():
                raise ValueError("Finish or retry the pending domain change first")
            for domain in json.loads(payload):
                reserved = db.execute("SELECT site_id FROM site_domains WHERE domain=?", (domain,)).fetchone()
                if reserved and reserved[0] != site_id:
                    raise ValueError("Domain is already reserved by another site: " + domain)
                db.execute("INSERT OR IGNORE INTO site_domains VALUES (?,?,0)", (domain, site_id))
            now = time.time()
            db.execute("INSERT INTO domain_jobs VALUES (?,?,?,'queued','',?,?)", (ident, site_id, payload, now, now))
        return next(row for row in self.domain_jobs(site_id) if row["id"] == ident)

    def update_domains(self, ident, state, error=""):
        from .core import STATES
        if state not in STATES:
            raise ValueError("Unknown operation state")
        with self.db() as db:
            db.execute("UPDATE domain_jobs SET state=?, error=?, updated=? WHERE id=?", (state, error[:2000], time.time(), ident))

    def finish_domains(self, job):
        domains = json.loads(job["payload"])
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET domain=?, updated=? WHERE id=?", (domains[0], time.time(), job["site_id"]))
            db.execute("DELETE FROM site_domains WHERE site_id=?", (job["site_id"],))
            for domain in domains:
                db.execute("INSERT INTO site_domains VALUES (?,?,1)", (domain, job["site_id"]))
            db.execute("UPDATE domain_jobs SET state='succeeded',error='',updated=? WHERE id=?", (time.time(), job["id"]))

    def retry_domains(self, ident):
        from .core import request_id
        request_id(ident)
        with self.db() as db:
            row = db.execute("SELECT * FROM domain_jobs WHERE id=?", (ident,)).fetchone()
            if not row:
                raise ValueError("Unknown domain operation")
            db.execute("UPDATE domain_jobs SET state='queued',error='',updated=? WHERE id=? AND state IN ('failed','recovery-needed')", (time.time(), ident))
        return dict(row)
