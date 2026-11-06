import subprocess
from pathlib import Path

import pytest

from webapp.backend.app import nx_worker


def test_license_failures_are_classified_as_infrastructure():
    assert nx_worker.is_nx_infrastructure_error(
        "Caught unexpected error: 3615094: Unable to reserve license to run journal file"
    )
    assert nx_worker.is_nx_infrastructure_error(
        'FlexNet Licensing error:-96,7. System Error: 11001 "Host not found"'
    )


def test_modeling_failure_remains_retryable_operation_error():
    assert not nx_worker.is_nx_infrastructure_error(
        "DeleteFaceBuilder.Commit failed because the selected blend cannot be healed"
    )


def test_run_journal_wraps_timeout_as_infrastructure(monkeypatch, tmp_path: Path):
    assert nx_worker.settings.run_journal.is_file()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)

    monkeypatch.setattr(nx_worker.subprocess, "run", timeout)
    with pytest.raises(nx_worker.NxInfrastructureError, match="timed out"):
        nx_worker._run_journal(["run_journal.exe"], tmp_path / "nx.log")
