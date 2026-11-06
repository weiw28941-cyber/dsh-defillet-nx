from __future__ import annotations

import json
from pathlib import Path

import pytest

from webapp.backend.app import nx_runtime_coordinator as coordinator_module
from webapp.backend.app.nx_runtime_contracts import PublicRuntimeExecuteRequest, RuntimeMode
from webapp.backend.app.nx_runtime_coordinator import RuntimeCoordinator, RuntimeCoordinatorError
from webapp.backend.app.nx_runtime_store import RuntimeStore
from webapp.backend.app.store import JobStore


def ready_job(tmp_path: Path) -> tuple[JobStore, str, str]:
    jobs = JobStore(tmp_path)
    job_id, job_root = jobs.create_job("session-1")
    (job_root / "revisions" / "r0" / "model.stp").write_text("STEP", encoding="utf-8")
    jobs.update(job_id, "ready", "r0")
    return jobs, job_id, "session-1"


class FakeWorker:
    instances: list["FakeWorker"] = []

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.running = False
        self.runtime_state = 0
        self.start_count = 0
        self.requests: list[PublicRuntimeExecuteRequest] = []
        FakeWorker.instances.append(self)

    def start(self, _input_step: Path) -> dict:
        self.running = True
        self.start_count += 1
        return self.status()

    def is_running(self) -> bool:
        return self.running

    def status(self) -> dict:
        return {
            "status": "ready",
            "runtime_state": self.runtime_state,
            "pid": 1234,
            "worker_build": "fake",
            "work_part": "model",
            "active_operation_id": None,
        }

    def submit(self, request: PublicRuntimeExecuteRequest, *, timeout_seconds: float) -> dict:
        self.requests.append(request)
        if request.mode == RuntimeMode.COMMIT:
            self.runtime_state += 1
            return {
                "operation_id": request.operation_id,
                "state": "committed",
                "result_state": self.runtime_state,
                "retained": True,
                "rolled_back": False,
                "result": {"ok": True},
                "validation": {},
                "error": None,
            }
        return {
            "operation_id": request.operation_id,
            "state": "completed",
            "result_state": self.runtime_state,
            "retained": False,
            "rolled_back": False,
            "result": {"runtime_state": self.runtime_state},
            "validation": {},
            "error": None,
        }

    def stop(self, *, timeout_seconds: float) -> None:
        self.running = False


def request(operation_id: str, state: int, mode: RuntimeMode) -> PublicRuntimeExecuteRequest:
    return PublicRuntimeExecuteRequest(
        operation_id=operation_id,
        base_state=state,
        mode=mode,
        code="result = {'ok': True}",
    )


def commit_lease_state(store: RuntimeStore, worker_id: str, job_id: str) -> None:
    store.create_operation(
        operation_id="op-commit",
        request_id="request-commit",
        worker_id=worker_id,
        job_id=job_id,
        revision_id="r0",
        mode=RuntimeMode.COMMIT,
        base_state=0,
        code_hash="a" * 64,
    )
    from webapp.backend.app.nx_runtime_contracts import OperationState

    for state in (OperationState.QUEUED, OperationState.RUNNING, OperationState.VALIDATING):
        store.transition_operation("op-commit", state)
    store.transition_operation(
        "op-commit", OperationState.COMMITTED, result_state=1, retained=True
    )


def test_worker_starts_from_step_with_injected_face_identity(tmp_path: Path, monkeypatch):
    jobs, _job_id, session_id = ready_job(tmp_path)
    source = jobs.revision_dir(_job_id, "r0") / "model.stp"
    source.write_text("#42=ADVANCED_FACE('',(),$,.T.);", encoding="utf-8")
    FakeWorker.instances = []
    monkeypatch.setattr(coordinator_module, "PersistentNXWorker", FakeWorker)

    RuntimeCoordinator(jobs)._worker_for(session_id)

    worker_input = FakeWorker.instances[0].workspace / "input" / "model.stp"
    assert "DSH_STEP_FACE_42" in worker_input.read_text(encoding="utf-8")


def test_commit_then_read_reuses_same_worker_and_current_state(tmp_path: Path, monkeypatch):
    jobs, _job_id, session_id = ready_job(tmp_path)
    FakeWorker.instances = []
    monkeypatch.setattr(
        coordinator_module,
        "inject_step_face_names",
        lambda source, destination: destination.write_bytes(source.read_bytes()),
    )
    monkeypatch.setattr(coordinator_module, "PersistentNXWorker", FakeWorker)
    runtime = RuntimeCoordinator(jobs)

    first = runtime.execute(session_id, request("op-1", 0, RuntimeMode.COMMIT))
    second = runtime.execute(session_id, request("op-2", 1, RuntimeMode.READ))

    assert first["state"] == "committed"
    assert second["state"] == "completed"
    assert len(FakeWorker.instances) == 1
    assert FakeWorker.instances[0].start_count == 1
    assert [item.base_state for item in FakeWorker.instances[0].requests] == [0, 1]


def test_transport_failure_after_dispatch_is_ambiguous(tmp_path: Path, monkeypatch):
    jobs, _job_id, session_id = ready_job(tmp_path)
    FakeWorker.instances = []
    monkeypatch.setattr(
        coordinator_module,
        "inject_step_face_names",
        lambda source, destination: destination.write_bytes(source.read_bytes()),
    )
    monkeypatch.setattr(coordinator_module, "PersistentNXWorker", FakeWorker)
    runtime = RuntimeCoordinator(jobs)
    worker = runtime._worker_for(session_id)

    def fail_submit(_request, *, timeout_seconds):
        raise RuntimeError("transport disconnected")

    worker.submit = fail_submit
    with pytest.raises(RuntimeError, match="transport disconnected"):
        runtime.execute(session_id, request("op-uncertain", 0, RuntimeMode.COMMIT))

    operation = runtime.ledger.get_operation("op-uncertain")
    assert operation["state"] == "ambiguous"
    assert runtime.ledger.has_unresolved_operations(operation["worker_id"]) is True


def test_dead_worker_with_retained_commit_cannot_cold_start(tmp_path: Path, monkeypatch):
    jobs, job_id, session_id = ready_job(tmp_path)
    FakeWorker.instances = []
    monkeypatch.setattr(
        coordinator_module,
        "inject_step_face_names",
        lambda source, destination: destination.write_bytes(source.read_bytes()),
    )
    monkeypatch.setattr(coordinator_module, "PersistentNXWorker", FakeWorker)
    runtime = RuntimeCoordinator(jobs)
    runtime.execute(session_id, request("op-1", 0, RuntimeMode.COMMIT))
    worker = FakeWorker.instances[0]
    worker.running = False

    with pytest.raises(RuntimeCoordinatorError, match="NX_RUNTIME_STATE_LOST"):
        runtime.execute(session_id, request("op-2", 1, RuntimeMode.READ))

    assert len(FakeWorker.instances) == 1
    assert runtime.ledger.get_job_lease(job_id)["runtime_state"] == 1


def test_recovery_preserves_dead_committed_lease_and_status_reports_loss(tmp_path: Path, monkeypatch):
    jobs, job_id, session_id = ready_job(tmp_path)
    ledger = RuntimeStore(tmp_path / "nx-runtime" / "runtime.sqlite3")
    worker_id = "worker-dead"
    ledger.acquire_lease(
        worker_id=worker_id,
        job_id=job_id,
        revision_id="r0",
        session_id=session_id,
        part_fingerprint="fingerprint",
    )
    commit_lease_state(ledger, worker_id, job_id)
    worker_root = tmp_path / "nx-runtime" / job_id / worker_id
    worker_root.mkdir(parents=True)
    (worker_root / "heartbeat.json").write_text(
        json.dumps({"pid": 1234, "runtime_state": 1}), encoding="utf-8"
    )
    monkeypatch.setattr(RuntimeCoordinator, "_pid_alive", staticmethod(lambda _pid: False))

    runtime = RuntimeCoordinator(jobs)
    lease = runtime.ledger.get_job_lease(job_id)
    status = runtime.status(session_id)

    assert lease["runtime_state"] == 1
    assert lease["status"] == "lost"
    assert status["connected"] is False
    assert status["runtime_state"] == 1
    assert status["state_lost"] is True
    assert status["reset_required"] is True


def test_reset_refuses_to_replace_persisted_worker_that_is_still_alive(tmp_path: Path, monkeypatch):
    jobs, job_id, session_id = ready_job(tmp_path)
    ledger = RuntimeStore(tmp_path / "nx-runtime" / "runtime.sqlite3")
    worker_id = "worker-live"
    ledger.acquire_lease(
        worker_id=worker_id,
        job_id=job_id,
        revision_id="r0",
        session_id=session_id,
        part_fingerprint="fingerprint",
    )
    worker_root = tmp_path / "nx-runtime" / job_id / worker_id
    worker_root.mkdir(parents=True)
    (worker_root / "heartbeat.json").write_text(json.dumps({"pid": 1234}), encoding="utf-8")
    monkeypatch.setattr(RuntimeCoordinator, "_pid_alive", staticmethod(lambda _pid: True))
    FakeWorker.instances = []
    monkeypatch.setattr(coordinator_module, "PersistentNXWorker", FakeWorker)
    runtime = RuntimeCoordinator(jobs)

    with pytest.raises(RuntimeCoordinatorError, match="still alive"):
        runtime.reset(session_id)

    assert runtime.ledger.get_job_lease(job_id)["worker_id"] == worker_id
    assert FakeWorker.instances == []


def test_reset_can_discard_persisted_lost_state_after_backend_restart(tmp_path: Path, monkeypatch):
    jobs, job_id, session_id = ready_job(tmp_path)
    ledger = RuntimeStore(tmp_path / "nx-runtime" / "runtime.sqlite3")
    ledger.acquire_lease(
        worker_id="worker-dead",
        job_id=job_id,
        revision_id="r0",
        session_id=session_id,
        part_fingerprint="fingerprint",
    )
    commit_lease_state(ledger, "worker-dead", job_id)
    monkeypatch.setattr(RuntimeCoordinator, "_pid_alive", staticmethod(lambda _pid: False))
    FakeWorker.instances = []
    monkeypatch.setattr(
        coordinator_module,
        "inject_step_face_names",
        lambda source, destination: destination.write_bytes(source.read_bytes()),
    )
    monkeypatch.setattr(coordinator_module, "PersistentNXWorker", FakeWorker)
    runtime = RuntimeCoordinator(jobs)

    result = runtime.reset(session_id)

    assert result["runtime_state"] == 0
    assert len(FakeWorker.instances) == 1
    lease = runtime.ledger.get_job_lease(job_id)
    assert lease["worker_id"] != "worker-dead"
    assert lease["runtime_state"] == 0
