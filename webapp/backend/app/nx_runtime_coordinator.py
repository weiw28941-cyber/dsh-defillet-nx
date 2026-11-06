from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
import uuid
from typing import Any

from .config import settings
from .fillet_detection import inject_step_face_names
from .geometry import build_face_mesh
from .nx_persistent_worker import (
    PersistentNXWorker,
    PersistentWorkerError,
    PersistentWorkerTimeout,
)
from .nx_runtime_contracts import (
    OperationState,
    PublicRuntimeExecuteRequest,
    RuntimeMode,
    code_digest,
)
from .nx_runtime_store import RuntimeStore
from .store import JobStore


class RuntimeCoordinatorError(RuntimeError):
    pass


RUNTIME_STATE_LOST = (
    "NX_RUNTIME_STATE_LOST: the persistent NX worker stopped after retaining "
    "unpublished commits; the in-memory model cannot be cold-started from the "
    "Accepted revision. Explicitly reset the runtime to discard those changes."
)


class RuntimeCoordinator:
    """Session-bound authority for one persistent NX worker per Job baseline."""

    def __init__(self, job_store: JobStore) -> None:
        self.jobs = job_store
        self.root = self.jobs.root / "nx-runtime"
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = RuntimeStore(self.root / "runtime.sqlite3")
        self._workers: dict[str, PersistentNXWorker] = {}
        self._bindings: dict[str, dict[str, str]] = {}
        self._lock = threading.RLock()
        self._recover_stale_leases()

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", "PID eq {}".format(pid), "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return completed.returncode == 0 and str(pid) in completed.stdout
        except Exception:
            return True

    def _recover_stale_leases(self) -> None:
        """Release only leases whose recorded worker is provably not alive."""
        with self.ledger._connect() as db:
            leases = [dict(row) for row in db.execute("SELECT * FROM nx_runtime_leases")]
        for lease in leases:
            worker_root = self.root / lease["job_id"] / lease["worker_id"]
            heartbeat_path = worker_root / "heartbeat.json"
            try:
                heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                pid = int(heartbeat.get("pid", 0))
            except Exception:
                pid = 0
            # A stale heartbeat is expected during a long synchronous NX call. A
            # live PID is therefore sufficient reason to preserve the lease.
            if pid > 0 and self._pid_alive(pid):
                continue
            uncertain_operation = self.ledger.has_unresolved_operations(lease["worker_id"])
            for claimed in (worker_root / "running").glob("*.request.json"):
                operation_id = claimed.name[: -len(".request.json")]
                try:
                    operation = self.ledger.get_operation(operation_id)
                    if operation["state"] in {
                        OperationState.RECEIVED.value,
                        OperationState.QUEUED.value,
                        OperationState.RUNNING.value,
                        OperationState.VALIDATING.value,
                        OperationState.ROLLING_BACK.value,
                    }:
                        self.ledger.transition_operation(operation_id, OperationState.AMBIGUOUS)
                        uncertain_operation = True
                except (KeyError, ValueError):
                    uncertain_operation = True
            if pid <= 0 or int(lease["runtime_state"]) > 0 or uncertain_operation:
                # Missing process evidence, an uncertain operation, or retained
                # edits must fail closed. Keep the lease as a durable tombstone so
                # no state-0 worker can silently replace the in-memory Work Part.
                self.ledger.set_lease_status(lease["worker_id"], "lost")
            else:
                # The PID is known and provably dead, state is still the Accepted
                # baseline, and no operation outcome needs reconciliation.
                self.ledger.release_lease(lease["worker_id"])

    @staticmethod
    def _fingerprint(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _resolve(self, session_id: str) -> tuple[dict[str, Any], str, Path]:
        job = self.jobs.get_by_session(session_id)
        revision_id = job.get("accepted_revision")
        if not revision_id:
            raise RuntimeCoordinatorError("session Job has no Accepted revision")
        revision = self.jobs.revision_dir(job["id"], revision_id)
        return job, revision_id, revision / "model.stp"

    def _worker_for(self, session_id: str, *, allow_candidate: bool = False) -> PersistentNXWorker:
        job, revision_id, step_path = self._resolve(session_id)
        if not allow_candidate and job.get("candidate_revision") is not None:
            raise RuntimeCoordinatorError("a Candidate awaits review")
        binding = self._bindings.get(session_id)
        worker = self._workers.get(session_id)
        expected = {
            "job_id": job["id"],
            "revision_id": revision_id,
            "step_fingerprint": self._fingerprint(step_path),
        }
        if (
            worker is not None
            and binding is not None
            and all(binding.get(key) == value for key, value in expected.items())
            and worker.is_running()
        ):
            return worker
        if worker is not None and worker.is_running():
            raise RuntimeCoordinatorError("runtime baseline changed; reset the runtime first")
        if worker is not None and not worker.is_running() and binding:
            stale_worker_id = binding.get("worker_id")
            stale_lease = None
            if stale_worker_id:
                try:
                    stale_lease = self.ledger.get_lease(stale_worker_id)
                except KeyError:
                    pass
            self._workers.pop(session_id, None)
            self._bindings.pop(session_id, None)
            if stale_lease is not None:
                if int(stale_lease["runtime_state"]) > 0 or stale_lease["status"] == "lost":
                    # A committed runtime state exists only in this worker's Work
                    # Part. Never replace it with a state-0 cold start.
                    self.ledger.set_lease_status(stale_worker_id, "lost")
                    raise RuntimeCoordinatorError(RUNTIME_STATE_LOST)
                if self.ledger.has_unresolved_operations(stale_worker_id):
                    raise RuntimeCoordinatorError(
                        "NX_RUNTIME_RECONCILIATION_REQUIRED: the stopped worker has an unresolved "
                        "operation; reconcile it or explicitly reset the runtime."
                    )
            if stale_worker_id:
                self.ledger.release_lease(stale_worker_id)
        try:
            stale_lease = self.ledger.get_job_lease(job["id"])
        except KeyError:
            stale_lease = None
        if stale_lease is not None:
            if int(stale_lease["runtime_state"]) > 0 or stale_lease["status"] == "lost":
                raise RuntimeCoordinatorError(RUNTIME_STATE_LOST)
            if self.ledger.has_unresolved_operations(stale_lease["worker_id"]):
                raise RuntimeCoordinatorError(
                    "NX_RUNTIME_RECONCILIATION_REQUIRED: the previous worker has an unresolved operation; "
                    "reconcile it or explicitly reset the runtime before starting NX."
                )
            raise RuntimeCoordinatorError(
                "a persisted runtime lease already exists; reconcile or reset it before starting NX"
            )
        incarnation = "worker-{}".format(uuid.uuid4().hex)
        self.ledger.acquire_lease(
            worker_id=incarnation,
            job_id=job["id"],
            revision_id=revision_id,
            session_id=session_id,
            part_fingerprint=expected["step_fingerprint"],
        )
        worker_root = self.root / job["id"] / incarnation
        worker_input = worker_root / "input" / "model.stp"
        worker_input.parent.mkdir(parents=True, exist_ok=True)
        # Runtime state 0 must carry the same exact ADVANCED_FACE identity used
        # by Accepted-revision component analysis. NX preserves these injected
        # names on import, allowing fc-xxx to be mapped by face-set equality
        # rather than by unrelated component ordinal.
        inject_step_face_names(step_path, worker_input)
        worker = PersistentNXWorker(worker_root)
        self._workers[session_id] = worker
        expected["worker_id"] = incarnation
        self._bindings[session_id] = expected
        try:
            status = worker.start(worker_input)
            if status.get("status") != "ready" or int(status.get("runtime_state", -1)) != 0:
                raise RuntimeCoordinatorError("NX worker did not establish a clean state-0 baseline")
        except Exception:
            if worker.is_running():
                self.ledger.set_lease_status(incarnation, "quarantined")
            else:
                self._workers.pop(session_id, None)
                self._bindings.pop(session_id, None)
                self.ledger.release_lease(incarnation)
            raise
        self.ledger.update_lease_metadata(
            incarnation,
            work_part=status.get("work_part"),
            part_fingerprint=expected["step_fingerprint"],
        )
        self.jobs.event(job["id"], "nx_runtime", "Persistent NX runtime started")
        return worker

    def status(self, session_id: str) -> dict[str, Any]:
        job, revision_id, _step = self._resolve(session_id)
        worker = self._workers.get(session_id)
        binding = self._bindings.get(session_id)
        heartbeat = worker.status() if worker is not None else None
        connected = bool(worker is not None and worker.is_running() and heartbeat)
        try:
            lease = self.ledger.get_job_lease(job["id"])
        except KeyError:
            lease = None
        persisted_state = int(lease["runtime_state"]) if lease is not None else 0
        lease_status = str(lease["status"]) if lease is not None else "offline"
        state_lost = bool(not connected and lease is not None and (
            persisted_state > 0 or lease_status == "lost"
        ))
        effective_binding = binding
        if effective_binding is None and lease is not None:
            effective_binding = {
                "job_id": lease["job_id"],
                "revision_id": lease["revision_id"],
                "worker_id": lease["worker_id"],
                "step_fingerprint": lease.get("part_fingerprint"),
            }
        return {
            "connected": connected,
            "status": (heartbeat or {}).get("status", lease_status),
            "job_id": job["id"],
            "revision_id": revision_id,
            "runtime_state": int((heartbeat or {}).get("runtime_state", persisted_state)),
            "worker_pid": (heartbeat or {}).get("pid"),
            "worker_build": (heartbeat or {}).get("worker_build"),
            "normalization": (heartbeat or {}).get("normalization"),
            "work_part": (heartbeat or {}).get("work_part") or (lease or {}).get("work_part"),
            "binding": effective_binding,
            "candidate_revision": job.get("candidate_revision"),
            "state_lost": state_lost,
            "reset_required": state_lost or lease_status in {"quarantined", "lost"},
        }

    def execute(self, session_id: str, request: PublicRuntimeExecuteRequest) -> dict[str, Any]:
        with self._lock:
            job, revision_id, _step = self._resolve(session_id)
            if job.get("candidate_revision") is not None:
                raise RuntimeCoordinatorError("a Candidate awaits review")
            if job.get("status") not in {"ready", "succeeded"}:
                raise RuntimeCoordinatorError("Job is not ready for runtime execution")
            worker = self._worker_for(session_id)
            binding = self._bindings[session_id]
            worker_id = binding["worker_id"]
            heartbeat = worker.status() or {}
            if int(heartbeat.get("runtime_state", -1)) != request.base_state:
                raise RuntimeCoordinatorError("runtime base_state is stale")
            if request.mode == RuntimeMode.COMMIT and request.preview_operation_id:
                try:
                    preview = self.ledger.get_operation(request.preview_operation_id)
                except KeyError as exc:
                    raise RuntimeCoordinatorError("preview operation was not found") from exc
                if (
                    preview["job_id"] != job["id"]
                    or preview["revision_id"] != revision_id
                    or preview["worker_id"] != worker_id
                    or preview["mode"] != RuntimeMode.PREVIEW.value
                    or preview["state"] != OperationState.ROLLED_BACK.value
                    or preview["base_state"] != request.base_state
                    or preview["code_hash"] != code_digest(request.code)
                ):
                    raise RuntimeCoordinatorError("preview provenance does not match commit")
            operation, created = self.ledger.create_operation(
                operation_id=request.operation_id,
                request_id=uuid.uuid4().hex,
                worker_id=worker_id,
                job_id=job["id"],
                revision_id=revision_id,
                mode=request.mode,
                base_state=request.base_state,
                code_hash=code_digest(request.code),
                preview_operation_id=request.preview_operation_id,
                deadline=request.deadline_at.isoformat() if request.deadline_at else None,
            )
            if not created:
                return operation
            self.ledger.transition_operation(request.operation_id, OperationState.QUEUED)
            try:
                self.ledger.transition_operation(request.operation_id, OperationState.RUNNING)
                result = worker.submit(request, timeout_seconds=settings.nx_timeout_seconds)
            except PersistentWorkerTimeout:
                self.ledger.transition_operation(request.operation_id, OperationState.AMBIGUOUS)
                return {
                    "operation_id": request.operation_id,
                    "state": "ambiguous",
                    "retry_class": "status_then_decide",
                }
            except Exception as exc:
                # Once a request has reached the worker, a transport failure does
                # not prove that NX made no change. Keep the operation blocking and
                # require reconciliation/reset rather than risking a cold restart.
                self.ledger.transition_operation(
                    request.operation_id,
                    OperationState.AMBIGUOUS,
                    error={"code": "NX_TRANSPORT_ERROR", "message": str(exc)},
                )
                raise
            self._record_worker_result(request, result)
            self.jobs.event(
                job["id"],
                "nx_runtime",
                "NX runtime {} operation {} finished as {}".format(
                    request.mode.value, request.operation_id, result.get("state")
                ),
            )
            return result

    def _record_worker_result(
        self, request: PublicRuntimeExecuteRequest, result: dict[str, Any]
    ) -> dict[str, Any]:
        state = OperationState(result["state"])
        current = OperationState(self.ledger.get_operation(request.operation_id)["state"])
        if current in {OperationState.RUNNING, OperationState.AMBIGUOUS} and state in {
            OperationState.COMPLETED,
            OperationState.COMMITTED,
        }:
            self.ledger.transition_operation(request.operation_id, OperationState.VALIDATING)
        elif current in {OperationState.RUNNING, OperationState.AMBIGUOUS} and state in {
            OperationState.ROLLED_BACK,
            OperationState.ROLLBACK_FAILED,
        }:
            self.ledger.transition_operation(request.operation_id, OperationState.ROLLING_BACK)
        return self.ledger.transition_operation(
            request.operation_id,
            state,
            result_state=result.get("result_state"),
            retained=result.get("retained"),
            rolled_back=result.get("rolled_back"),
            result=result.get("result"),
            validation=result.get("validation") or {},
            error=result.get("error"),
        )

    def get_operation(self, session_id: str, operation_id: str) -> dict[str, Any]:
        job, _revision_id, _step = self._resolve(session_id)
        operation = self.ledger.get_operation(operation_id)
        if operation["job_id"] != job["id"]:
            raise KeyError(operation_id)
        if operation["state"] != OperationState.AMBIGUOUS.value:
            return operation
        worker = self._workers.get(session_id)
        if worker is None:
            return operation
        result = worker.get_operation(operation_id)
        if result is None or result.get("state") in {"queued", "running"}:
            return operation
        return self._record_worker_result(
            PublicRuntimeExecuteRequest.model_construct(
                operation_id=operation_id,
                base_state=operation["base_state"],
                mode=RuntimeMode(operation["mode"]),
                code="",
                preview_operation_id=operation.get("preview_operation_id"),
                deadline_at=None,
            ),
            result,
        )

    def rollback_latest_commit(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            job, revision_id, _step = self._resolve(session_id)
            if job.get("candidate_revision") is not None:
                raise RuntimeCoordinatorError("a Candidate awaits review")
            worker = self._worker_for(session_id)
            binding = self._bindings[session_id]
            worker_id = binding["worker_id"]
            heartbeat = worker.status() or {}
            base_state = int(heartbeat.get("runtime_state", -1))
            try:
                target = self.ledger.latest_retained_commit(worker_id)
            except KeyError as exc:
                raise RuntimeCoordinatorError("no retained commit is available to roll back") from exc
            operation_id = "op-cad-rollback-{}".format(uuid.uuid4().hex)
            trusted_code = "__trusted_rollback_latest__"
            request = PublicRuntimeExecuteRequest(
                operation_id=operation_id,
                base_state=base_state,
                mode=RuntimeMode.COMMIT,
                code=trusted_code,
            )
            self.ledger.create_operation(
                operation_id=operation_id,
                request_id=uuid.uuid4().hex,
                worker_id=worker_id,
                job_id=job["id"],
                revision_id=revision_id,
                mode=RuntimeMode.COMMIT,
                base_state=base_state,
                code_hash=code_digest(trusted_code),
            )
            self.ledger.transition_operation(operation_id, OperationState.QUEUED)
            self.ledger.transition_operation(operation_id, OperationState.RUNNING)
            try:
                result = worker.rollback_latest(
                    operation_id=operation_id,
                    base_state=base_state,
                    rollback_operation_id=target["operation_id"],
                    timeout_seconds=settings.nx_timeout_seconds,
                )
            except PersistentWorkerTimeout:
                self.ledger.transition_operation(operation_id, OperationState.AMBIGUOUS)
                return {"operation_id": operation_id, "state": "ambiguous", "retry_class": "status_then_decide"}
            except Exception as exc:
                self.ledger.transition_operation(
                    operation_id, OperationState.AMBIGUOUS,
                    error={"code": "NX_TRANSPORT_ERROR", "message": str(exc)},
                )
                raise
            self._record_worker_result(request, result)
            if result.get("state") == "committed":
                self.ledger.mark_commit_undone(target["operation_id"], operation_id)
                self.jobs.event(
                    job["id"], "nx_runtime",
                    "Rolled back retained NX operation {} as {}".format(target["operation_id"], operation_id),
                )
            return result

    def reset(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            job, _revision_id, _step = self._resolve(session_id)
            old = self._workers.get(session_id)
            binding = self._bindings.get(session_id)
            if old is not None and old.is_running():
                old.stop(timeout_seconds=60)
                if old.is_running():
                    raise RuntimeCoordinatorError("NX worker did not stop; runtime lease was preserved")
            worker_id = binding.get("worker_id") if binding else None
            if not worker_id:
                try:
                    worker_id = self.ledger.get_job_lease(job["id"])["worker_id"]
                except KeyError:
                    pass
            if worker_id and old is None:
                worker_root = self.root / job["id"] / worker_id
                try:
                    heartbeat = json.loads(
                        (worker_root / "heartbeat.json").read_text(encoding="utf-8")
                    )
                    persisted_pid = int(heartbeat.get("pid", 0))
                except Exception:
                    persisted_pid = 0
                if persisted_pid > 0 and self._pid_alive(persisted_pid):
                    raise RuntimeCoordinatorError(
                        "NX_RUNTIME_RECONCILIATION_REQUIRED: the persisted NX worker is still alive; "
                        "it cannot be replaced by a cold-started worker."
                    )
            if worker_id:
                self.ledger.release_lease(worker_id)
            self._workers.pop(session_id, None)
            self._bindings.pop(session_id, None)
            worker = self._worker_for(session_id, allow_candidate=True)
            return worker.status() or {}

    def invalidate(self, session_id: str) -> None:
        with self._lock:
            worker = self._workers.get(session_id)
            binding = self._bindings.get(session_id)
            if worker is not None and worker.is_running():
                worker.stop(timeout_seconds=60)
            self._workers.pop(session_id, None)
            self._bindings.pop(session_id, None)
            if binding and binding.get("worker_id"):
                self.ledger.release_lease(binding["worker_id"])

    def publish_candidate(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            job, revision_id, _step = self._resolve(session_id)
            if job.get("candidate_revision") is not None:
                raise RuntimeCoordinatorError("a Candidate already awaits review")
            worker = self._worker_for(session_id)
            binding = self._bindings[session_id]
            lease = self.ledger.get_lease(binding["worker_id"])
            heartbeat = worker.status() or {}
            if heartbeat.get("status") != "ready" or heartbeat.get("active_operation_id"):
                raise RuntimeCoordinatorError("NX runtime is not ready for publication")
            if lease["status"] != "ready":
                raise RuntimeCoordinatorError("runtime ledger has a blocking or quarantined operation")
            state = int(heartbeat.get("runtime_state", 0))
            if int(lease["runtime_state"]) != state:
                raise RuntimeCoordinatorError("runtime heartbeat and ledger state diverged")
            new_revision, revision_dir = self.jobs.next_revision(
                job["id"], parent_revision=revision_id
            )
            staging = self.root / job["id"] / "publication" / uuid.uuid4().hex
            staging.mkdir(parents=True)
            staged_step = staging / "model.stp"
            try:
                result = worker.export_step(
                    operation_id="op-export-{}".format(uuid.uuid4().hex),
                    base_state=state,
                    output_path=staged_step,
                    settings_file=settings.nx_base / "STEP214UG" / "ugstep214.def",
                    timeout_seconds=settings.nx_timeout_seconds,
                )
                if result.get("state") != "completed":
                    raise RuntimeCoordinatorError(
                        "NX export did not complete: {}".format(result.get("error"))
                    )
                if not staged_step.is_file() or staged_step.stat().st_size <= 0:
                    raise RuntimeCoordinatorError("NX exported an empty STEP file")
                final_step = revision_dir / "model.stp"
                shutil.move(str(staged_step), str(final_step))
                mesh = build_face_mesh(final_step, new_revision)
                if int(mesh.get("faceCount", 0)) <= 0:
                    raise RuntimeCoordinatorError("Candidate STEP contains no BREP faces")
                self.jobs.write_json(revision_dir / "mesh.json", mesh)
                self.jobs.write_json(
                    revision_dir / "nx_runtime_publication.json",
                    {
                        "schema": "nx_runtime_publication_v1",
                        "source_revision": revision_id,
                        "candidate_revision": new_revision,
                        "runtime_state": state,
                        "operation_id": result.get("operation_id"),
                        "step_fingerprint": self._fingerprint(final_step),
                        "published_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                self.jobs.publish_candidate(job["id"], new_revision)
                return {
                    "status": "succeeded",
                    "candidate_revision": new_revision,
                    "runtime_state": state,
                    "face_count": mesh["faceCount"],
                }
            except Exception as exc:
                self.jobs.set_revision_status(job["id"], new_revision, "failed", str(exc))
                raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)
