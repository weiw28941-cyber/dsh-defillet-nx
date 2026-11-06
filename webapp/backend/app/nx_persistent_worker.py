from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any

from .config import REPO_ROOT, settings
from .nx_runtime_contracts import PublicRuntimeExecuteRequest, code_digest


WORKER_JOURNAL = REPO_ROOT / "cad" / "nx_persistent_worker.py"


class PersistentWorkerError(RuntimeError):
    pass


class PersistentWorkerTimeout(PersistentWorkerError):
    def __init__(self, operation_id: str) -> None:
        super().__init__(
            "Timed out waiting for NX operation {}; its outcome is ambiguous".format(operation_id)
        )
        self.operation_id = operation_id


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(temporary, path)


@dataclass(frozen=True)
class WorkerPaths:
    root: Path

    @property
    def requests(self) -> Path:
        return self.root / "requests"

    @property
    def running(self) -> Path:
        return self.root / "running"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def heartbeat(self) -> Path:
        return self.root / "heartbeat.json"

    @property
    def control(self) -> Path:
        return self.root / "control.json"

    @property
    def log(self) -> Path:
        return self.root / "worker.log"

    def prepare(self) -> None:
        for path in (self.root, self.requests, self.running, self.results):
            path.mkdir(parents=True, exist_ok=True)


class PersistentNXWorker:
    """Owns one long-lived run_journal.exe process and its file queue."""

    def __init__(self, workspace: Path, *, run_journal: Path | None = None) -> None:
        self.paths = WorkerPaths(Path(workspace).resolve())
        self.run_journal = Path(run_journal or settings.run_journal)
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle = None
        self._lock = threading.RLock()
        self.paths.prepare()

    def start(self, input_step: Path, *, startup_timeout: float = 180.0) -> dict[str, Any]:
        input_step = Path(input_step).resolve()
        if not input_step.is_file():
            raise FileNotFoundError(input_step)
        if not self.run_journal.is_file():
            raise PersistentWorkerError("NX runner missing: {}".format(self.run_journal))
        with self._lock:
            if self.is_running():
                raise PersistentWorkerError("NX worker is already running")
            for stale in (self.paths.heartbeat, self.paths.control):
                stale.unlink(missing_ok=True)
            self._log_handle = self.paths.log.open("ab", buffering=0)
            environment = os.environ.copy()
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONPATH", None)
            command = [
                str(self.run_journal),
                str(WORKER_JOURNAL),
                "-args",
                "--workspace",
                str(self.paths.root),
                "--input",
                str(input_step),
            ]
            self._process = subprocess.Popen(
                command,
                cwd=str(REPO_ROOT),
                env=environment,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise PersistentWorkerError(self._failure_detail("NX worker exited during startup"))
            status = self.status()
            if status and status.get("status") in {"ready", "quarantined"}:
                return status
            time.sleep(0.1)
        raise PersistentWorkerError("NX worker did not publish a heartbeat before startup timeout")

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def status(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.paths.heartbeat.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            return None

    def submit(
        self,
        request: PublicRuntimeExecuteRequest,
        *,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        if not self.is_running():
            raise PersistentWorkerError("NX worker is not running")
        request_path = self.paths.requests / (request.operation_id + ".request.json")
        running_path = self.paths.running / request_path.name
        result_path = self.paths.results / (request.operation_id + ".result.json")
        payload = request.model_dump(mode="json")
        payload.update(
            {
                "protocol_version": 1,
                "code_hash": code_digest(request.code),
                "submitted_at": time.time(),
                "deadline_at": request.deadline_at.timestamp() if request.deadline_at else None,
            }
        )
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if (
                result.get("code_hash") != payload["code_hash"]
                or result.get("base_state") != request.base_state
                or result.get("mode") != request.mode.value
            ):
                raise PersistentWorkerError("operation ID conflicts with an existing result")
            return result
        if request_path.exists() or running_path.exists():
            queued_path = request_path if request_path.exists() else running_path
            existing = json.loads(queued_path.read_text(encoding="utf-8"))
            if (
                existing.get("code_hash") != payload["code_hash"]
                or existing.get("base_state") != request.base_state
                or existing.get("mode") != request.mode.value
            ):
                raise PersistentWorkerError("operation ID conflicts with an existing request")
            return {"operation_id": request.operation_id, "state": "running" if running_path.exists() else "queued"}
        atomic_json(request_path, payload)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if result_path.is_file():
                return json.loads(result_path.read_text(encoding="utf-8"))
            if not self.is_running():
                raise PersistentWorkerError(self._failure_detail("NX worker exited during operation"))
            time.sleep(0.1)
        raise PersistentWorkerTimeout(request.operation_id)

    def export_step(
        self,
        *,
        operation_id: str,
        base_state: int,
        output_path: Path,
        settings_file: Path,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        trusted_code = "__trusted_export_step__"
        request_path = self.paths.requests / (operation_id + ".request.json")
        running_path = self.paths.running / request_path.name
        result_path = self.paths.results / (operation_id + ".result.json")
        payload = {
            "protocol_version": 1,
            "operation_id": operation_id,
            "base_state": base_state,
            "mode": "read",
            "action": "export_step",
            "code": trusted_code,
            "code_hash": code_digest(trusted_code),
            "preview_operation_id": None,
            "deadline_at": time.time() + timeout_seconds,
            "submitted_at": time.time(),
            "output_path": str(Path(output_path).resolve()),
            "settings_file": str(Path(settings_file).resolve()),
        }
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("code_hash") != payload["code_hash"] or result.get("base_state") != base_state:
                raise PersistentWorkerError("export operation ID conflicts with an existing result")
            return result
        if request_path.exists() or running_path.exists():
            raise PersistentWorkerError("export operation is already queued or running")
        atomic_json(request_path, payload)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if result_path.is_file():
                return json.loads(result_path.read_text(encoding="utf-8"))
            if not self.is_running():
                raise PersistentWorkerError(self._failure_detail("NX worker exited during export"))
            time.sleep(0.1)
        raise PersistentWorkerTimeout(operation_id)

    def rollback_latest(
        self,
        *,
        operation_id: str,
        base_state: int,
        rollback_operation_id: str,
        timeout_seconds: float = 120.0,
    ) -> dict[str, Any]:
        trusted_code = "__trusted_rollback_latest__"
        request_path = self.paths.requests / (operation_id + ".request.json")
        running_path = self.paths.running / request_path.name
        result_path = self.paths.results / (operation_id + ".result.json")
        payload = {
            "protocol_version": 1,
            "operation_id": operation_id,
            "base_state": base_state,
            "mode": "commit",
            "action": "rollback_latest",
            "code": trusted_code,
            "code_hash": code_digest(trusted_code),
            "preview_operation_id": None,
            "rollback_operation_id": rollback_operation_id,
            "deadline_at": time.time() + timeout_seconds,
            "submitted_at": time.time(),
        }
        if result_path.is_file():
            return json.loads(result_path.read_text(encoding="utf-8"))
        if request_path.exists() or running_path.exists():
            raise PersistentWorkerError("rollback operation is already queued or running")
        atomic_json(request_path, payload)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if result_path.is_file():
                return json.loads(result_path.read_text(encoding="utf-8"))
            if not self.is_running():
                raise PersistentWorkerError(self._failure_detail("NX worker exited during rollback"))
            time.sleep(0.1)
        raise PersistentWorkerTimeout(operation_id)

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        result = self.paths.results / (operation_id + ".result.json")
        if result.is_file():
            return json.loads(result.read_text(encoding="utf-8"))
        if (self.paths.running / (operation_id + ".request.json")).is_file():
            return {"operation_id": operation_id, "state": "running"}
        if (self.paths.requests / (operation_id + ".request.json")).is_file():
            return {"operation_id": operation_id, "state": "queued"}
        return None

    def stop(self, *, timeout_seconds: float = 30.0) -> None:
        process = self._process
        if process is None:
            return
        atomic_json(self.paths.control, {"command": "shutdown", "requested_at": time.time()})
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            # Preserve process ownership: NX may still be consuming the queue.
            # A later start must remain prohibited until reconciliation proves it died.
            raise PersistentWorkerError("NX worker did not stop cleanly; shutdown is ambiguous") from exc
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self._process = None

    def _failure_detail(self, prefix: str) -> str:
        try:
            content = self.paths.log.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            content = ""
        return "{}{}".format(prefix, ": " + content if content else "")
