from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading

from .config import REPO_ROOT, settings


NX_JOURNAL = REPO_ROOT / "cad" / "Defillet" / "nx_web_delete_fillet.py"
NX_CLASSIFY_JOURNAL = REPO_ROOT / "cad" / "Defillet" / "nx_web_classify_fillet_faces.py"
NX_COMPONENT_JOURNAL = REPO_ROOT / "cad" / "Defillet" / "nx_web_detect_fillet_components.py"
NX_COMPONENT_DELETE_JOURNAL = REPO_ROOT / "cad" / "Defillet" / "nx_web_delete_fillet_component.py"
NX_REMOVE_ALL_FILLETS_JOURNAL = REPO_ROOT / "cad" / "Defillet" / "nx_web_remove_all_fillets.py"
_lock = threading.Lock()


class NxInfrastructureError(RuntimeError):
    """NX could not start or reach required runtime infrastructure."""


_INFRASTRUCTURE_ERROR_MARKERS = (
    "unable to reserve license",
    "license server machine is down",
    "license checkout",
    "flexnet licensing error",
    "failed to initialize ufun",
    "general fault exception",
    "fatal error detected",
)


def is_nx_infrastructure_error(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in _INFRASTRUCTURE_ERROR_MARKERS)


def _run_journal(command: list[str], log_path: Path, timeout_seconds: int | None = None) -> None:
    if not settings.run_journal.is_file():
        raise NxInfrastructureError("NX runner missing: {}".format(settings.run_journal))
    timeout_seconds = timeout_seconds or settings.nx_timeout_seconds
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    with _lock, log_path.open("w", encoding="utf-8", errors="replace") as log:
        try:
            completed = subprocess.run(
                command,
                cwd=str(REPO_ROOT),
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise NxInfrastructureError(
                "NX journal timed out after {} seconds".format(timeout_seconds)
            ) from exc
        except OSError as exc:
            raise NxInfrastructureError("NX journal could not start: {}".format(exc)) from exc
    if completed.returncode != 0:
        detail = ""
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            marker = content.rfind("ERROR ")
            detail = content[marker : marker + 2000].strip() if marker >= 0 else content[-2000:].strip()
        except OSError:
            pass
        message = "NX journal failed with exit code {}".format(completed.returncode)
        if detail:
            message = "{}: {}".format(message, detail)
        if is_nx_infrastructure_error(message):
            raise NxInfrastructureError(message)
        raise RuntimeError(message)


def execute_fillet(request_path: Path, log_path: Path) -> None:
    command = [
        str(settings.run_journal),
        str(NX_JOURNAL),
        "-args",
        "--request",
        str(request_path),
    ]
    _run_journal(command, log_path)


def classify_fillet_faces(step_path: Path, report_path: Path, log_path: Path) -> None:
    command = [
        str(settings.run_journal),
        str(NX_CLASSIFY_JOURNAL),
        "-args",
        "--input",
        str(step_path),
        "--report",
        str(report_path),
    ]
    _run_journal(command, log_path)
    if not report_path.is_file() or report_path.stat().st_size <= 0:
        raise RuntimeError("NX did not publish a fillet candidate report")


def detect_fillet_components(step_path: Path, report_path: Path, log_path: Path) -> None:
    command = [
        str(settings.run_journal),
        str(NX_COMPONENT_JOURNAL),
        "-args",
        "--input",
        str(step_path),
        "--report",
        str(report_path),
    ]
    _run_journal(command, log_path)
    if not report_path.is_file() or report_path.stat().st_size <= 0:
        raise RuntimeError("NX did not publish a fillet component report")


def remove_all_fillets(request_path: Path, report_path: Path, log_path: Path) -> None:
    command = [
        str(settings.run_journal),
        str(NX_REMOVE_ALL_FILLETS_JOURNAL),
        "-args",
        "--request",
        str(request_path),
    ]
    _run_journal(command, log_path, timeout_seconds=settings.nx_global_timeout_seconds)
    if not report_path.is_file() or report_path.stat().st_size <= 0:
        raise RuntimeError("NX did not publish a global fillet deletion report")


def delete_fillet_component(request_path: Path, report_path: Path, log_path: Path) -> None:
    command = [
        str(settings.run_journal),
        str(NX_COMPONENT_DELETE_JOURNAL),
        "-args",
        "--request",
        str(request_path),
    ]
    _run_journal(command, log_path)
    if not report_path.is_file() or report_path.stat().st_size <= 0:
        raise RuntimeError("NX did not publish a fillet component deletion report")
