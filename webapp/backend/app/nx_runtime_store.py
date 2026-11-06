from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from .nx_runtime_contracts import (
    BLOCKING_OPERATION_STATES,
    OperationState,
    RuntimeMode,
    validate_operation_transition,
)


class RuntimeStore:
    """Durable runtime leases and operation ledger.

    This store is independent from the NX transport. It is usable in offline
    tests and becomes the reconciliation source when a bridge call times out.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS nx_runtime_leases (
                    worker_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    revision_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    runtime_state INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    work_part TEXT,
                    part_fingerprint TEXT,
                    acquired_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nx_runtime_operations (
                    operation_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    base_state INTEGER NOT NULL,
                    result_state INTEGER,
                    code_hash TEXT NOT NULL,
                    preview_operation_id TEXT,
                    state TEXT NOT NULL,
                    retained INTEGER NOT NULL DEFAULT 0,
                    rolled_back INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    validation_json TEXT,
                    error_json TEXT,
                    deadline TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id, operation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_nx_runtime_operations_job_state
                    ON nx_runtime_operations(job_id, state);
                """
            )

    @staticmethod
    def _decode_json(value: str | None) -> Any:
        return json.loads(value) if value else None

    @classmethod
    def _operation_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["retained"] = bool(result["retained"])
        result["rolled_back"] = bool(result["rolled_back"])
        result["result"] = cls._decode_json(result.pop("result_json"))
        result["validation"] = cls._decode_json(result.pop("validation_json")) or {}
        result["error"] = cls._decode_json(result.pop("error_json"))
        return result

    def acquire_lease(
        self,
        *,
        worker_id: str,
        job_id: str,
        revision_id: str,
        session_id: str,
        work_part: str | None = None,
        part_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        now = self.now()
        with self._lock, self._connect() as db:
            existing_worker = db.execute(
                "SELECT * FROM nx_runtime_leases WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if existing_worker is not None and (
                existing_worker["job_id"] != job_id
                or existing_worker["session_id"] != session_id
            ):
                raise ValueError("NX worker is already leased to another job or session")
            existing_job = db.execute(
                "SELECT * FROM nx_runtime_leases WHERE job_id=?", (job_id,)
            ).fetchone()
            if existing_job is not None and existing_job["worker_id"] != worker_id:
                raise ValueError("job is already leased to another NX worker")
            db.execute(
                """
                INSERT INTO nx_runtime_leases(
                    worker_id,job_id,revision_id,session_id,runtime_state,status,
                    work_part,part_fingerprint,acquired_at,updated_at
                ) VALUES(?,?,?,?,0,'ready',?,?,?,?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    revision_id=excluded.revision_id,
                    work_part=excluded.work_part,
                    part_fingerprint=excluded.part_fingerprint,
                    updated_at=excluded.updated_at
                """,
                (
                    worker_id,
                    job_id,
                    revision_id,
                    session_id,
                    work_part,
                    part_fingerprint,
                    now,
                    now,
                ),
            )
        return self.get_lease(worker_id)

    def get_lease(self, worker_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM nx_runtime_leases WHERE worker_id=?", (worker_id,)
            ).fetchone()
        if row is None:
            raise KeyError(worker_id)
        return dict(row)

    def get_job_lease(self, job_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM nx_runtime_leases WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def has_unresolved_operations(self, worker_id: str) -> bool:
        blocking_values = tuple(state.value for state in BLOCKING_OPERATION_STATES)
        placeholders = ",".join("?" for _ in blocking_values)
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM nx_runtime_operations "
                "WHERE worker_id=? AND state IN ({}) LIMIT 1".format(placeholders),
                (worker_id, *blocking_values),
            ).fetchone()
        return row is not None

    def update_lease_metadata(
        self, worker_id: str, *, work_part: str | None, part_fingerprint: str | None
    ) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE nx_runtime_leases SET work_part=?,part_fingerprint=?,updated_at=? WHERE worker_id=?",
                (work_part, part_fingerprint, self.now(), worker_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(worker_id)
        return self.get_lease(worker_id)

    def set_lease_status(self, worker_id: str, status: str) -> dict[str, Any]:
        if status not in {"ready", "busy", "diverged", "quarantined", "lost"}:
            raise ValueError("invalid runtime lease status")
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE nx_runtime_leases SET status=?, updated_at=? WHERE worker_id=?",
                (status, self.now(), worker_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(worker_id)
        return self.get_lease(worker_id)

    def release_lease(self, worker_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM nx_runtime_leases WHERE worker_id=?", (worker_id,))

    def create_operation(
        self,
        *,
        operation_id: str,
        request_id: str,
        worker_id: str,
        job_id: str,
        revision_id: str,
        mode: RuntimeMode,
        base_state: int,
        code_hash: str,
        preview_operation_id: str | None = None,
        deadline: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = self.now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM nx_runtime_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing is not None:
                operation = self._operation_dict(existing)
                # request_id is a transport correlation ID and may change when
                # an identical operation is safely replayed after reconnecting.
                immutable = (
                    operation["worker_id"],
                    operation["job_id"],
                    operation["revision_id"],
                    operation["mode"],
                    operation["base_state"],
                    operation["code_hash"],
                    operation["preview_operation_id"],
                )
                requested = (
                    worker_id,
                    job_id,
                    revision_id,
                    mode.value,
                    base_state,
                    code_hash,
                    preview_operation_id,
                )
                if immutable != requested:
                    raise ValueError("operation_id is already bound to a different request")
                return operation, False

            lease = db.execute(
                "SELECT * FROM nx_runtime_leases WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if lease is None:
                raise KeyError(worker_id)
            if lease["job_id"] != job_id or lease["revision_id"] != revision_id:
                raise ValueError("operation does not match the active runtime lease")
            if int(lease["runtime_state"]) != base_state:
                raise ValueError("operation base_state does not match current runtime state")
            blocking_values = tuple(state.value for state in BLOCKING_OPERATION_STATES)
            placeholders = ",".join("?" for _ in blocking_values)
            blocking = db.execute(
                "SELECT operation_id,state FROM nx_runtime_operations "
                "WHERE worker_id=? AND state IN ({}) LIMIT 1".format(placeholders),
                (worker_id, *blocking_values),
            ).fetchone()
            if blocking is not None:
                raise ValueError(
                    "runtime is blocked by operation {} in state {}".format(
                        blocking["operation_id"], blocking["state"]
                    )
                )
            db.execute(
                """
                INSERT INTO nx_runtime_operations(
                    operation_id,request_id,worker_id,job_id,revision_id,mode,
                    base_state,result_state,code_hash,preview_operation_id,state,
                    retained,rolled_back,result_json,validation_json,error_json,
                    deadline,started_at,completed_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,NULL,?,?,?,0,0,NULL,'{}',NULL,?,NULL,NULL,?,?)
                """,
                (
                    operation_id,
                    request_id,
                    worker_id,
                    job_id,
                    revision_id,
                    mode.value,
                    base_state,
                    code_hash,
                    preview_operation_id,
                    OperationState.RECEIVED.value,
                    deadline,
                    now,
                    now,
                ),
            )
        return self.get_operation(operation_id), True

    def latest_retained_commit(self, worker_id: str) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM nx_runtime_operations WHERE worker_id=? AND state=? "
                "AND retained=1 ORDER BY result_state DESC, created_at DESC",
                (worker_id, OperationState.COMMITTED.value),
            ).fetchall()
        for row in rows:
            operation = self._operation_dict(row)
            result = operation.get("result") or {}
            if result.get("method") and result.get("selected_count"):
                return operation
        raise KeyError(worker_id)

    def mark_commit_undone(self, operation_id: str, rollback_operation_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT validation_json FROM nx_runtime_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            validation = self._decode_json(row["validation_json"]) or {}
            validation["undone_by"] = rollback_operation_id
            cursor = db.execute(
                "UPDATE nx_runtime_operations SET retained=0,validation_json=?,updated_at=? "
                "WHERE operation_id=? AND state=? AND retained=1",
                (
                    json.dumps(validation, ensure_ascii=False), self.now(), operation_id,
                    OperationState.COMMITTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("commit is no longer retained")
        return self.get_operation(operation_id)

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM nx_runtime_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return self._operation_dict(row)

    def transition_operation(
        self,
        operation_id: str,
        target: OperationState,
        *,
        result_state: int | None = None,
        retained: bool | None = None,
        rolled_back: bool | None = None,
        result: Any = None,
        validation: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self.now()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM nx_runtime_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            current = OperationState(row["state"])
            if current == target and current not in BLOCKING_OPERATION_STATES:
                return self._operation_dict(row)
            validate_operation_transition(current, target)
            mode = RuntimeMode(row["mode"])
            if target == OperationState.COMPLETED and mode != RuntimeMode.READ:
                raise ValueError("only read operations may complete without rollback")
            if target == OperationState.COMMITTED and mode != RuntimeMode.COMMIT:
                raise ValueError("only commit operations may retain changes")
            if mode == RuntimeMode.PREVIEW and retained is True:
                raise ValueError("preview operations cannot retain changes")
            if retained is True and rolled_back is True:
                raise ValueError("an operation cannot be retained and rolled back")
            started_at = row["started_at"]
            if target == OperationState.RUNNING and started_at is None:
                started_at = now
            completed_at = row["completed_at"]
            if target in {
                OperationState.REJECTED,
                OperationState.EXPIRED_BEFORE_START,
                OperationState.COMPLETED,
                OperationState.COMMITTED,
                OperationState.ROLLED_BACK,
                OperationState.ROLLBACK_FAILED,
            }:
                completed_at = now
            effective_result_state = row["result_state"] if result_state is None else result_state
            effective_retained = bool(row["retained"]) if retained is None else retained
            effective_rolled_back = bool(row["rolled_back"]) if rolled_back is None else rolled_back
            db.execute(
                """
                UPDATE nx_runtime_operations SET
                    state=?,result_state=?,retained=?,rolled_back=?,result_json=?,
                    validation_json=?,error_json=?,started_at=?,completed_at=?,updated_at=?
                WHERE operation_id=?
                """,
                (
                    target.value,
                    effective_result_state,
                    int(effective_retained),
                    int(effective_rolled_back),
                    json.dumps(result, ensure_ascii=False) if result is not None else row["result_json"],
                    json.dumps(validation, ensure_ascii=False)
                    if validation is not None
                    else row["validation_json"],
                    json.dumps(error, ensure_ascii=False) if error is not None else row["error_json"],
                    started_at,
                    completed_at,
                    now,
                    operation_id,
                ),
            )
            if target == OperationState.COMMITTED:
                if effective_result_state != int(row["base_state"]) + 1:
                    raise ValueError("a committed operation must advance runtime state exactly once")
                cursor = db.execute(
                    """
                    UPDATE nx_runtime_leases SET runtime_state=?,status='ready',updated_at=?
                    WHERE worker_id=? AND runtime_state=?
                    """,
                    (effective_result_state, now, row["worker_id"], row["base_state"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("runtime state changed before operation commit")
            elif target == OperationState.ROLLBACK_FAILED:
                db.execute(
                    "UPDATE nx_runtime_leases SET status='quarantined',updated_at=? WHERE worker_id=?",
                    (now, row["worker_id"]),
                )
            elif target in {
                OperationState.RUNNING,
                OperationState.VALIDATING,
                OperationState.ROLLING_BACK,
                OperationState.AMBIGUOUS,
            }:
                db.execute(
                    "UPDATE nx_runtime_leases SET status='busy',updated_at=? WHERE worker_id=?",
                    (now, row["worker_id"]),
                )
            elif target in {
                OperationState.REJECTED,
                OperationState.EXPIRED_BEFORE_START,
                OperationState.COMPLETED,
                OperationState.ROLLED_BACK,
            }:
                db.execute(
                    "UPDATE nx_runtime_leases SET status='ready',updated_at=? WHERE worker_id=?",
                    (now, row["worker_id"]),
                )
        return self.get_operation(operation_id)
