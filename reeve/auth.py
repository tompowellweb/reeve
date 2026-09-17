import contextlib
import hashlib
import secrets
import sqlite3
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

HASHER = PasswordHasher()


class Auth:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            version = db.execute("pragma user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported authentication schema")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS operator (id INTEGER PRIMARY KEY CHECK(id=1), hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL, authenticated INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS failures (at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS intake_tokens (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token):
        if not token or len(token) > 100:
            return None
        with self.db() as db:
            row = db.execute("SELECT * FROM sessions WHERE token=? AND expires>?", (self.digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def new_session(self, authenticated=False):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = time.time() + (28800 if authenticated else 900)
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (self.digest(token), csrf, expires, int(authenticated)))
        return token, self.session(token)

    def logout(self, token):
        with self.db() as db:
            db.execute("DELETE FROM sessions WHERE token=?", (self.digest(token),))

    def login(self, password):
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM failures WHERE at<?", (now - 600,))
            if db.execute("SELECT count(*) FROM failures").fetchone()[0] >= 5:
                return "Too many attempts. Try again in ten minutes."
            row = db.execute("SELECT hash FROM operator WHERE id=1").fetchone()
            valid = False
            if row and len(password) <= 1024:
                try:
                    valid = HASHER.verify(row[0], password)
                except VerificationError:
                    pass
            if valid:
                db.execute("DELETE FROM failures")
                return None
            db.execute("INSERT INTO failures VALUES (?)", (now,))
            return "Sign-in failed. Check the operator password."

    def set_password(self, password):
        if not 14 <= len(password) <= 1024:
            raise ValueError("Use a password of 14–1024 characters")
        hashed = HASHER.hash(password)
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO operator VALUES (1,?)", (hashed,))
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM intake_tokens")
            db.execute("DELETE FROM failures")


    def new_api_token(self, name):
        import uuid
        name = name.strip()
        if not name or len(name) > 60 or any(ord(c) < 32 for c in name):
            raise ValueError("Use a token name of 1–60 plain characters.")
        token = "hp_" + secrets.token_urlsafe(32)
        now = time.time()
        with self.db() as db:
            db.execute('INSERT INTO intake_tokens VALUES (?,?,?,?,?)',
                       (str(uuid.uuid4()), name, self.digest(token), now, now + 30 * 86400))
        return token

    def api_token(self, token):
        if not isinstance(token, str) or not token.startswith('hp_') or len(token) > 100: return None
        with self.db() as db:
            row = db.execute('SELECT id,name,expires FROM intake_tokens WHERE digest=? AND expires>?',
                             (self.digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def api_tokens(self):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT id,name,created,expires FROM intake_tokens WHERE expires>? ORDER BY created DESC', (time.time(),))]

    def revoke_api_token(self, ident):
        with self.db() as db: db.execute('DELETE FROM intake_tokens WHERE id=?', (ident,))
