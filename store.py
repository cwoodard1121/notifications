"""Persistent history, queue and watcher checkpoints, using only SQLite."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, title TEXT NOT NULL,
                    message TEXT NOT NULL, source TEXT NOT NULL, event TEXT NOT NULL,
                    priority INTEGER NOT NULL, dedupe_key TEXT UNIQUE, provider TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, error TEXT, provider_id TEXT
                );
                CREATE INDEX IF NOT EXISTS queue_due ON events(status, next_attempt);
                CREATE INDEX IF NOT EXISTS events_created ON events(created);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, event_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                    title TEXT NOT NULL, message TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                    submit_attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    gateway_id TEXT, digit TEXT, error TEXT, notified TEXT, next_call_at TEXT,
                    call_attempts INTEGER NOT NULL DEFAULT 0, with_text INTEGER NOT NULL DEFAULT 0, via TEXT
                );
                CREATE INDEX IF NOT EXISTS calls_due ON calls(status, next_attempt);
            """)
            # Columns added after the first release; CREATE TABLE IF NOT EXISTS leaves an existing table alone.
            columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
            for name, kind in (("with_text", "INTEGER NOT NULL DEFAULT 0"), ("via", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE calls ADD COLUMN {name} {kind}")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, event, provider, status=None):
        # status="held": suppressed by the user's state; the worker only ever sends 'queued' rows.
        row = dict(event, id=uuid.uuid4().hex, created=time.time(), provider=provider,
                   status=status or ("local" if provider == "local" else "queued"))
        with self.connect() as db:
            cursor = db.execute("""INSERT OR IGNORE INTO events
                (id,created,title,message,source,event,priority,dedupe_key,provider,status)
                VALUES (:id,:created,:title,:message,:source,:event,:priority,:dedupe_key,:provider,:status)""", row)
            duplicate = cursor.rowcount == 0
            saved = db.execute("SELECT * FROM events WHERE dedupe_key=?" if duplicate else "SELECT * FROM events WHERE id=?",
                               (row["dedupe_key"] if duplicate else row["id"],)).fetchone()
            return dict(saved), duplicate

    def recent(self, limit=50, since=0):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events WHERE created>=? ORDER BY created DESC LIMIT ?", (since, limit))]

    def held(self, limit=50):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events WHERE status='held' ORDER BY created DESC LIMIT ?", (limit,))]

    def recap_rows(self, since):
        with self.connect() as db:
            # Text escalation copies (SMS) are the same notification again, so recaps skip them too.
            count = db.execute("SELECT COUNT(*) FROM events WHERE created>=? AND event NOT IN ('recap','escalation')", (since,)).fetchone()[0]
            rows = db.execute("SELECT * FROM events WHERE created>=? AND event NOT IN ('recap','escalation') ORDER BY created DESC LIMIT 10", (since,))
            return count, [dict(r) for r in rows]

    def pending(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE status='queued' AND next_attempt<=? ORDER BY created LIMIT 1", (time.time(),)).fetchone()
            return dict(row) if row else None

    def finish(self, event_id, status, attempts, error=None, delay=0, provider_id=None):
        with self.connect() as db:
            db.execute("UPDATE events SET status=?, attempts=?, error=?, next_attempt=?, provider_id=? WHERE id=?",
                       (status, attempts, error, time.time() + delay, provider_id, event_id))

    def counts(self):
        with self.connect() as db:
            return {r[0]: r[1] for r in db.execute("SELECT status, COUNT(*) FROM events GROUP BY status")}

    def get_state(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def add_call(self, call):
        with self.connect() as db:
            db.execute(f"INSERT INTO calls ({','.join(call)}) VALUES ({','.join('?' * len(call))})", tuple(call.values()))
        return self.call(call["id"])

    def call(self, call_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None and len(call_id) >= 4:
                rows = db.execute("SELECT * FROM calls WHERE id LIKE ? LIMIT 2", (call_id.replace("%", "").replace("_", "") + "%",)).fetchall()
                row = rows[0] if len(rows) == 1 else None
            return dict(row) if row else None

    def update_call(self, call_id, **fields):
        with self.connect() as db:
            db.execute(f"UPDATE calls SET {', '.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), call_id))

    def calls(self, limit=50, statuses=None, due=None, updated_since=None):
        where, params = [], []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params += list(statuses)
        if due is not None:
            where.append("next_attempt<=?")
            params.append(due)
        if updated_since is not None:
            where.append("updated>=?")
            params.append(updated_since)
        sql = "SELECT * FROM calls" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created DESC LIMIT ?"
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, (*params, limit))]

    def call_count(self, since):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM calls WHERE created>=? AND status!='dry-run'", (since,)).fetchone()[0]

    def prune(self, days):
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE created<? AND status!='queued'", (time.time() - days * 86400,))
            db.execute("DELETE FROM calls WHERE created<? AND status IN ('dry-run','acked','unanswered','canceled','failed')",
                       (time.time() - days * 86400,))
