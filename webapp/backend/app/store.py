from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from .config import settings


class JobStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else settings.jobs_root
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "jobs.sqlite3"
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in db.execute("PRAGMA table_info({})".format(table))}

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, current_revision TEXT,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    accepted_revision TEXT, candidate_revision TEXT, session_id TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    job_id TEXT NOT NULL, id TEXT NOT NULL, parent_revision TEXT,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    error TEXT, PRIMARY KEY (job_id, id)
                );
                CREATE TABLE IF NOT EXISTS face_selections (
                    session_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                    selected_faces_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            # SQLite has no ADD COLUMN IF NOT EXISTS. Migrate databases created by
            # earlier backend versions one column at a time and preserve their
            # current_revision as the accepted revision.
            columns = self._columns(db, "jobs")
            for name in ("accepted_revision", "candidate_revision", "session_id"):
                if name not in columns:
                    db.execute("ALTER TABLE jobs ADD COLUMN {} TEXT".format(name))
            db.execute(
                "UPDATE jobs SET accepted_revision=current_revision "
                "WHERE accepted_revision IS NULL AND current_revision IS NOT NULL"
            )
            db.execute(
                "UPDATE jobs SET current_revision=accepted_revision "
                "WHERE accepted_revision IS NOT NULL"
            )
            # Backfill revision metadata for legacy jobs. Directory discovery below
            # fills in revisions beyond the former current revision.
            rows = db.execute("SELECT id, accepted_revision, created_at, updated_at FROM jobs").fetchall()
            for row in rows:
                revisions_dir = self.root / row["id"] / "revisions"
                ids = []
                if revisions_dir.is_dir():
                    ids = sorted(
                        (item.name for item in revisions_dir.iterdir() if item.is_dir() and self._valid_revision(item.name)),
                        key=lambda value: int(value[1:]),
                    )
                if row["accepted_revision"] and row["accepted_revision"] not in ids:
                    ids.append(row["accepted_revision"])
                for revision_id in ids:
                    status = "accepted" if revision_id == row["accepted_revision"] else "superseded"
                    db.execute(
                        "INSERT OR IGNORE INTO revisions(job_id,id,parent_revision,status,created_at,updated_at,error) "
                        "VALUES(?,?,?,?,?,?,NULL)",
                        (row["id"], revision_id, None, status, row["created_at"], row["updated_at"]),
                    )

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _valid_revision(revision_id: str) -> bool:
        return revision_id.startswith("r") and revision_id[1:].isdigit()

    def create_job(self, session_id: str | None = None) -> tuple[str, Path]:
        session_id = session_id.strip() if session_id else None
        job_id = uuid.uuid4().hex
        job_dir = self.root / job_id
        (job_dir / "revisions" / "r0").mkdir(parents=True)
        now = self.now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO jobs(id,status,current_revision,error,created_at,updated_at,accepted_revision,candidate_revision,session_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, "processing", "r0", None, now, now, "r0", None, session_id),
            )
            db.execute(
                "INSERT INTO revisions(job_id,id,parent_revision,status,created_at,updated_at,error) VALUES(?,?,?,?,?,?,NULL)",
                (job_id, "r0", None, "accepted", now, now),
            )
        self.event(job_id, "status", "Upload received")
        return job_id, job_dir

    def update(self, job_id: str, status: str, revision: str | None = None, error: str | None = None) -> None:
        """Update processing state without changing the accepted revision.

        The revision argument is retained for source compatibility. It identifies
        the revision being processed, but current_revision remains an alias for
        accepted_revision.
        """
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE jobs SET status=?, error=?, current_revision=accepted_revision, updated_at=? WHERE id=?",
                (status, error, self.now(), job_id),
            )
        self.event(job_id, "status", status if not error else "{}: {}".format(status, error))

    def begin_execute(self, job_id: str, revision_id: str) -> bool:
        """Atomically claim a ready accepted revision for execution."""
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET status='planning', error=NULL, current_revision=accepted_revision, updated_at=? "
                "WHERE id=? AND accepted_revision=? AND candidate_revision IS NULL "
                "AND status IN ('ready','succeeded')",
                (self.now(), job_id, revision_id),
            )
            claimed = cursor.rowcount == 1
        if claimed:
            self.event(job_id, "status", "planning")
        return claimed

    def get(self, job_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        # Defensive compatibility for a partially migrated database.
        result["current_revision"] = result.get("accepted_revision") or result.get("current_revision")
        return result

    def event(self, job_id: str, kind: str, message: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO events(job_id, created_at, kind, message) VALUES (?, ?, ?, ?)", (job_id, self.now(), kind, message))

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after)).fetchall()
        return [dict(row) for row in rows]

    def revision_dir(self, job_id: str, revision_id: str) -> Path:
        if not self._valid_revision(revision_id):
            raise ValueError("invalid revision")
        path = self.root / job_id / "revisions" / revision_id
        if not path.is_dir():
            raise FileNotFoundError(path)
        return path

    def next_revision(self, job_id: str, parent_revision: str | None = None) -> tuple[str, Path]:
        job = self.get(job_id)
        parent_revision = parent_revision or job["accepted_revision"]
        base = self.root / job_id / "revisions"
        numbers = [int(item.name[1:]) for item in base.iterdir() if item.is_dir() and self._valid_revision(item.name)]
        revision = "r{}".format(max(numbers, default=-1) + 1)
        path = base / revision
        path.mkdir()
        now = self.now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO revisions(job_id,id,parent_revision,status,created_at,updated_at,error) VALUES(?,?,?,?,?,?,NULL)",
                (job_id, revision, parent_revision, "processing", now, now),
            )
        return revision, path

    def set_revision_status(self, job_id: str, revision_id: str, status: str, error: str | None = None) -> None:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE revisions SET status=?, error=?, updated_at=? WHERE job_id=? AND id=?",
                (status, error, self.now(), job_id, revision_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(revision_id)

    def publish_candidate(self, job_id: str, revision_id: str) -> None:
        now = self.now()
        with self._lock, self._connect() as db:
            job = db.execute("SELECT candidate_revision FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["candidate_revision"] is not None:
                raise ValueError("job already has a candidate revision")
            cursor = db.execute(
                "UPDATE revisions SET status='candidate', error=NULL, updated_at=? WHERE job_id=? AND id=?",
                (now, job_id, revision_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(revision_id)
            db.execute(
                "UPDATE jobs SET status='succeeded', candidate_revision=?, current_revision=accepted_revision, error=NULL, updated_at=? WHERE id=?",
                (revision_id, now, job_id),
            )
        try:
            self.event(job_id, "candidate", "Candidate revision {} is ready for review".format(revision_id))
        except Exception:
            # Candidate publication is already committed atomically; an auxiliary
            # event-log failure must not make callers roll back the published state.
            pass

    def accept_candidate(self, job_id: str, revision_id: str) -> dict:
        now = self.now()
        with self._lock, self._connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["candidate_revision"] != revision_id:
                raise ValueError("revision is not the current candidate")
            if job["accepted_revision"]:
                db.execute(
                    "UPDATE revisions SET status='superseded', updated_at=? WHERE job_id=? AND id=?",
                    (now, job_id, job["accepted_revision"]),
                )
            db.execute(
                "UPDATE revisions SET status='accepted', updated_at=? WHERE job_id=? AND id=?",
                (now, job_id, revision_id),
            )
            db.execute(
                "UPDATE jobs SET status='ready', accepted_revision=?, current_revision=?, candidate_revision=NULL, error=NULL, updated_at=? WHERE id=?",
                (revision_id, revision_id, now, job_id),
            )
        self.event(job_id, "accepted", "Accepted candidate revision {}".format(revision_id))
        return self.get(job_id)

    def reject_candidate(self, job_id: str, revision_id: str) -> dict:
        now = self.now()
        with self._lock, self._connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["candidate_revision"] != revision_id:
                raise ValueError("revision is not the current candidate")
            db.execute(
                "UPDATE revisions SET status='rejected', updated_at=? WHERE job_id=? AND id=?",
                (now, job_id, revision_id),
            )
            db.execute(
                "UPDATE jobs SET status='ready', candidate_revision=NULL, current_revision=accepted_revision, error=NULL, updated_at=? WHERE id=?",
                (now, job_id),
            )
        self.event(job_id, "rejected", "Rejected candidate revision {}".format(revision_id))
        return self.get(job_id)

    def rollback_accepted(self, job_id: str, target_revision: str) -> dict:
        """Move Accepted to a strict ancestor without deleting revision history."""
        if not self._valid_revision(target_revision):
            raise ValueError("invalid target revision")
        now = self.now()
        with self._lock, self._connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["candidate_revision"] is not None:
                raise ValueError("cannot roll back while a candidate awaits review")
            if job["status"] not in {"ready", "succeeded"}:
                raise ValueError("accepted revision is not ready for rollback")
            current_revision = job["accepted_revision"]
            if current_revision == target_revision:
                raise ValueError("target revision is already accepted")

            rows = db.execute(
                "SELECT id,parent_revision FROM revisions WHERE job_id=?",
                (job_id,),
            ).fetchall()
            parents = {row["id"]: row["parent_revision"] for row in rows}
            if target_revision not in parents:
                raise ValueError("target revision does not belong to this job")
            ancestor = parents.get(current_revision)
            seen = set()
            while ancestor is not None and ancestor != target_revision:
                if ancestor in seen:
                    raise RuntimeError("revision ancestry contains a cycle")
                seen.add(ancestor)
                ancestor = parents.get(ancestor)
            if ancestor != target_revision:
                raise ValueError("target revision is not an ancestor of the accepted revision")

            target_dir = self.root / job_id / "revisions" / target_revision
            if not target_dir.is_dir() or not (target_dir / "model.stp").is_file():
                raise ValueError("target revision artifacts are unavailable")
            db.execute(
                "UPDATE revisions SET status='superseded', updated_at=? WHERE job_id=? AND id=?",
                (now, job_id, current_revision),
            )
            db.execute(
                "UPDATE revisions SET status='accepted', error=NULL, updated_at=? WHERE job_id=? AND id=?",
                (now, job_id, target_revision),
            )
            db.execute(
                "UPDATE jobs SET status='ready', accepted_revision=?, current_revision=?, candidate_revision=NULL, error=NULL, updated_at=? WHERE id=?",
                (target_revision, target_revision, now, job_id),
            )
            if job["session_id"]:
                db.execute("DELETE FROM face_selections WHERE session_id=?", (job["session_id"],))
        self.event(
            job_id,
            "rollback",
            "Rolled back accepted revision from {} to {}".format(current_revision, target_revision),
        )
        result = self.get(job_id)
        result["rolled_back_from"] = current_revision
        result["rolled_back_to"] = target_revision
        return result

    def revisions(self, job_id: str) -> list[dict]:
        job = self.get(job_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,parent_revision,status,created_at,updated_at,error FROM revisions WHERE job_id=?",
                (job_id,),
            ).fetchall()
        result = [dict(row) for row in rows]
        result.sort(key=lambda row: int(row["id"][1:]))
        for row in result:
            row["is_accepted"] = row["id"] == job["accepted_revision"]
            row["is_candidate"] = row["id"] == job["candidate_revision"]
        return result

    def get_by_session(self, session_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE session_id=? ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        result = dict(row)
        result["current_revision"] = result.get("accepted_revision") or result.get("current_revision")
        return result

    def bind_session(self, job_id: str, session_id: str | None) -> dict:
        session_id = session_id.strip() if session_id else None
        with self._lock, self._connect() as db:
            if session_id:
                db.execute("UPDATE jobs SET session_id=NULL WHERE session_id=? AND id<>?", (session_id, job_id))
            cursor = db.execute(
                "UPDATE jobs SET session_id=?, updated_at=? WHERE id=?",
                (session_id, self.now(), job_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(job_id)
        self.event(job_id, "session", "DSH session {}".format("bound" if session_id else "cleared"))
        return self.get(job_id)

    def set_face_selection(
        self, session_id: str, job_id: str, revision_id: str, selected_faces: list[dict]
    ) -> dict:
        payload = json.dumps(selected_faces, ensure_ascii=False, separators=(",", ":"))
        updated_at = self.now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO face_selections(session_id,job_id,revision_id,selected_faces_json,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "job_id=excluded.job_id,revision_id=excluded.revision_id,"
                "selected_faces_json=excluded.selected_faces_json,updated_at=excluded.updated_at",
                (session_id, job_id, revision_id, payload, updated_at),
            )
        return {
            "session_id": session_id,
            "job_id": job_id,
            "revision_id": revision_id,
            "selected_faces": selected_faces,
            "selected_face_count": len(selected_faces),
            "updated_at": updated_at,
        }

    def get_face_selection(self, session_id: str) -> dict:
        job = self.get_by_session(session_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM face_selections WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None or row["job_id"] != job["id"] or row["revision_id"] != job["accepted_revision"]:
            return {
                "session_id": session_id,
                "job_id": job["id"],
                "revision_id": job["accepted_revision"],
                "selected_faces": [],
                "selected_face_count": 0,
                "updated_at": None,
            }
        faces = json.loads(row["selected_faces_json"])
        return {
            "session_id": session_id,
            "job_id": row["job_id"],
            "revision_id": row["revision_id"],
            "selected_faces": faces,
            "selected_face_count": len(faces),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def write_json(path: Path, payload: object) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)


store = JobStore()
