from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from webapp.backend.app.nx_persistent_worker import (
    PersistentNXWorker,
    PersistentWorkerError,
    atomic_json,
)
from webapp.backend.app.nx_runtime_contracts import PublicRuntimeExecuteRequest, code_digest


def request(operation_id="op-one", **overrides):
    payload = {
        "operation_id": operation_id,
        "base_state": 0,
        "mode": "read",
        "code": "result = {'body_count': len(list(workPart.Bodies))}",
    }
    payload.update(overrides)
    return PublicRuntimeExecuteRequest.model_validate(payload)


def test_atomic_json_replaces_complete_document(tmp_path: Path):
    path = tmp_path / "state.json"
    atomic_json(path, {"state": "queued"})
    atomic_json(path, {"state": "completed", "value": 7})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "state": "completed",
        "value": 7,
    }
    assert not path.with_suffix(".json.tmp").exists()


def test_submit_writes_protocol_request_and_returns_existing_result(tmp_path: Path):
    executable = tmp_path / "run_journal.exe"
    executable.write_bytes(b"stub")
    worker = PersistentNXWorker(tmp_path / "worker", run_journal=executable)
    worker._process = type("Process", (), {"poll": lambda self: None})()
    result_path = worker.paths.results / "op-one.result.json"
    operation = request()
    existing = {
        "operation_id": "op-one",
        "state": "completed",
        "mode": "read",
        "base_state": 0,
        "code_hash": code_digest(operation.code),
    }
    atomic_json(result_path, existing)
    assert worker.submit(operation) == existing


def test_get_operation_distinguishes_queue_running_and_result(tmp_path: Path):
    executable = tmp_path / "run_journal.exe"
    executable.write_bytes(b"stub")
    worker = PersistentNXWorker(tmp_path / "worker", run_journal=executable)
    atomic_json(worker.paths.requests / "op-one.request.json", {})
    assert worker.get_operation("op-one")["state"] == "queued"
    (worker.paths.requests / "op-one.request.json").rename(
        worker.paths.running / "op-one.request.json"
    )
    assert worker.get_operation("op-one")["state"] == "running"
    atomic_json(worker.paths.results / "op-one.result.json", {"state": "rolled_back"})
    assert worker.get_operation("op-one")["state"] == "rolled_back"


def test_submit_rejects_duplicate_nonterminal_operation(tmp_path: Path):
    executable = tmp_path / "run_journal.exe"
    executable.write_bytes(b"stub")
    worker = PersistentNXWorker(tmp_path / "worker", run_journal=executable)
    worker._process = type("Process", (), {"poll": lambda self: None})()
    atomic_json(worker.paths.requests / "op-one.request.json", {})
    with pytest.raises(PersistentWorkerError, match="conflicts with an existing request"):
        worker.submit(request(), timeout_seconds=0)


def test_stop_timeout_preserves_live_process_ownership(tmp_path: Path):
    executable = tmp_path / "run_journal.exe"
    executable.write_bytes(b"stub")
    worker = PersistentNXWorker(tmp_path / "worker", run_journal=executable)

    class Process:
        def poll(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("run_journal", timeout)

    process = Process()
    worker._process = process
    with pytest.raises(PersistentWorkerError, match="ambiguous"):
        worker.stop(timeout_seconds=0)
    assert worker._process is process
    assert worker.is_running()


def test_public_request_deadline_and_hash_are_encoded(tmp_path: Path, monkeypatch):
    executable = tmp_path / "run_journal.exe"
    executable.write_bytes(b"stub")
    worker = PersistentNXWorker(tmp_path / "worker", run_journal=executable)
    worker._process = type("Process", (), {"poll": lambda self: None})()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=1)
    operation = request(deadline_at=deadline)

    def capture(path, payload):
        assert payload["code_hash"] == code_digest(operation.code)
        assert payload["deadline_at"] == pytest.approx(deadline.timestamp())
        worker.paths.results.joinpath("op-one.result.json").write_text(
            '{"state":"completed"}', encoding="utf-8"
        )

    monkeypatch.setattr("webapp.backend.app.nx_persistent_worker.atomic_json", capture)
    assert worker.submit(operation, timeout_seconds=1)["state"] == "completed"
