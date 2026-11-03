"""Durable, single-workspace job queue shared by the console and its worker."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


class Jobs:
    def __init__(self, path):
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, actor TEXT NOT NULL,
                    created TEXT NOT NULL, finished TEXT, state TEXT NOT NULL,
                    phase TEXT NOT NULL, report TEXT, error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON jobs ((1))
                    WHERE state IN ('queued', 'running');
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, mode, actor):
        if mode not in ("compare", "sync"):
            raise ValueError("invalid job mode")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT id FROM jobs WHERE state IN ('queued','running')"
            ).fetchone()
            if active:
                return active["id"]
            run = str(uuid.uuid4())
            db.execute(
                "INSERT INTO jobs(id,mode,actor,created,state,phase) VALUES(?,?,?,?,'queued','queued')",
                (run, mode, actor, now()),
            )
            return run

    def claim(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,mode FROM jobs WHERE state='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if row:
                uuid.UUID(row["id"])
                if row["mode"] not in ("compare", "sync"):
                    raise ValueError("invalid queued mode")
                db.execute(
                    "UPDATE jobs SET state='running',phase='source' WHERE id=?",
                    (row["id"],),
                )
                return dict(row)

    def interrupted(self):
        with self.connection() as db:
            db.execute(
                "UPDATE jobs SET state='failed',finished=?,error='interrupted' WHERE state='running'",
                (now(),),
            )

    def phase(self, run, phase):
        with self.connection() as db:
            db.execute("UPDATE jobs SET phase=? WHERE id=?", (phase, run))

    def finish(self, run, state, report=None, error=None):
        with self.connection() as db:
            db.execute(
                "UPDATE jobs SET state=?,finished=?,report=?,error=? WHERE id=?",
                (
                    state,
                    now(),
                    json.dumps(report) if report is not None else None,
                    error,
                    run,
                ),
            )

    def history(self):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,mode,created,finished,state,phase,error FROM jobs ORDER BY created DESC,rowid DESC LIMIT 30"
                )
            ]

    def get(self, run):
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (run,)).fetchone()
            if row:
                result = dict(row)
                result["report"] = (
                    json.loads(result["report"]) if result["report"] else None
                )
                return result
