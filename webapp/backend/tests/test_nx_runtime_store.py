from pathlib import Path

import pytest

from webapp.backend.app.nx_runtime_contracts import OperationState, RuntimeMode
from webapp.backend.app.nx_runtime_store import RuntimeStore


def make_store(tmp_path: Path) -> RuntimeStore:
    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    store.acquire_lease(
        worker_id="worker-1",
        job_id="job-1",
        revision_id="r0",
        session_id="session-1",
        work_part="model",
        part_fingerprint="fingerprint",
    )
    return store


def create_operation(store: RuntimeStore, operation_id: str = "op-one"):
    return store.create_operation(
        operation_id=operation_id,
        request_id="request-one",
        worker_id="worker-1",
        job_id="job-1",
        revision_id="r0",
        mode=RuntimeMode.PREVIEW,
        base_state=0,
        code_hash="a" * 64,
    )


def test_lease_is_exclusive_by_worker_and_job(tmp_path: Path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="another job or session"):
        store.acquire_lease(
            worker_id="worker-1",
            job_id="job-2",
            revision_id="r0",
            session_id="session-2",
        )
    with pytest.raises(ValueError, match="another NX worker"):
        store.acquire_lease(
            worker_id="worker-2",
            job_id="job-1",
            revision_id="r0",
            session_id="session-1",
        )


def test_operation_id_is_idempotent_only_for_identical_request(tmp_path: Path):
    store = make_store(tmp_path)
    operation, created = create_operation(store)
    assert created is True
    assert operation["state"] == "received"

    repeated, created = create_operation(store)
    assert created is False
    assert repeated["operation_id"] == operation["operation_id"]

    replayed, created = store.create_operation(
        operation_id="op-one",
        request_id="different-transport-request",
        worker_id="worker-1",
        job_id="job-1",
        revision_id="r0",
        mode=RuntimeMode.PREVIEW,
        base_state=0,
        code_hash="a" * 64,
    )
    assert created is False
    assert replayed["operation_id"] == "op-one"

    with pytest.raises(ValueError, match="different request"):
        store.create_operation(
            operation_id="op-one",
            request_id="different-request",
            worker_id="worker-1",
            job_id="job-1",
            revision_id="r0",
            mode=RuntimeMode.PREVIEW,
            base_state=0,
            code_hash="b" * 64,
        )


def test_blocking_operation_prevents_new_operation(tmp_path: Path):
    store = make_store(tmp_path)
    create_operation(store)

    with pytest.raises(ValueError, match="blocked by operation"):
        create_operation(store, "op-two")

    store.transition_operation("op-one", OperationState.QUEUED)
    with pytest.raises(ValueError, match="blocked by operation"):
        create_operation(store, "op-three")


def test_commit_advances_runtime_state_once(tmp_path: Path):
    store = make_store(tmp_path)
    store.create_operation(
        operation_id="op-commit",
        request_id="request-commit",
        worker_id="worker-1",
        job_id="job-1",
        revision_id="r0",
        mode=RuntimeMode.COMMIT,
        base_state=0,
        code_hash="b" * 64,
    )
    store.transition_operation("op-commit", OperationState.QUEUED)
    store.transition_operation("op-commit", OperationState.RUNNING)
    store.transition_operation("op-commit", OperationState.VALIDATING)
    operation = store.transition_operation(
        "op-commit",
        OperationState.COMMITTED,
        result_state=1,
        retained=True,
        validation={"passed": True},
    )

    assert operation["state"] == "committed"
    assert operation["retained"] is True
    assert store.get_lease("worker-1")["runtime_state"] == 1


def test_latest_delete_commit_can_be_marked_undone(tmp_path: Path):
    store = make_store(tmp_path)
    store.create_operation(
        operation_id="op-delete", request_id="request-delete", worker_id="worker-1",
        job_id="job-1", revision_id="r0", mode=RuntimeMode.COMMIT,
        base_state=0, code_hash="b" * 64,
    )
    for state in (OperationState.QUEUED, OperationState.RUNNING, OperationState.VALIDATING):
        store.transition_operation("op-delete", state)
    store.transition_operation(
        "op-delete", OperationState.COMMITTED, result_state=1, retained=True,
        result={"method": "face_edge_blend", "selected_count": 2},
    )
    assert store.latest_retained_commit("worker-1")["operation_id"] == "op-delete"
    undone = store.mark_commit_undone("op-delete", "op-rollback")
    assert undone["retained"] is False
    assert undone["validation"]["undone_by"] == "op-rollback"
    with pytest.raises(KeyError):
        store.latest_retained_commit("worker-1")


def test_terminal_commit_replay_is_idempotent(tmp_path: Path):
    store = make_store(tmp_path)
    store.create_operation(
        operation_id="op-commit",
        request_id="request-commit",
        worker_id="worker-1",
        job_id="job-1",
        revision_id="r0",
        mode=RuntimeMode.COMMIT,
        base_state=0,
        code_hash="b" * 64,
    )
    for state in (OperationState.QUEUED, OperationState.RUNNING, OperationState.VALIDATING):
        store.transition_operation("op-commit", state)
    first = store.transition_operation(
        "op-commit", OperationState.COMMITTED, result_state=1, retained=True
    )
    replay = store.transition_operation(
        "op-commit", OperationState.COMMITTED, result_state=1, retained=True
    )
    assert replay == first
    assert store.get_lease("worker-1")["runtime_state"] == 1


def test_unresolved_operation_query_tracks_blocking_states(tmp_path: Path):
    store = make_store(tmp_path)
    create_operation(store)
    assert store.has_unresolved_operations("worker-1") is True

    store.transition_operation("op-one", OperationState.QUEUED)
    store.transition_operation("op-one", OperationState.RUNNING)
    store.transition_operation("op-one", OperationState.ROLLING_BACK)
    store.transition_operation("op-one", OperationState.ROLLED_BACK, rolled_back=True)

    assert store.has_unresolved_operations("worker-1") is False


def test_lost_is_a_valid_lease_status(tmp_path: Path):
    store = make_store(tmp_path)
    lease = store.set_lease_status("worker-1", "lost")
    assert lease["status"] == "lost"


def test_store_rejects_mode_terminal_mismatch(tmp_path: Path):
    store = make_store(tmp_path)
    create_operation(store)
    store.transition_operation("op-one", OperationState.QUEUED)
    store.transition_operation("op-one", OperationState.RUNNING)
    store.transition_operation("op-one", OperationState.VALIDATING)
    with pytest.raises(ValueError, match="only commit"):
        store.transition_operation(
            "op-one", OperationState.COMMITTED, result_state=1, retained=True
        )


def test_rollback_failure_quarantines_worker(tmp_path: Path):
    store = make_store(tmp_path)
    create_operation(store)
    store.transition_operation("op-one", OperationState.QUEUED)
    store.transition_operation("op-one", OperationState.RUNNING)
    store.transition_operation("op-one", OperationState.ROLLING_BACK)
    operation = store.transition_operation(
        "op-one",
        OperationState.ROLLBACK_FAILED,
        error={"code": "NX_ROLLBACK_FAILED", "message": "baseline mismatch"},
    )

    assert operation["state"] == "rollback_failed"
    assert store.get_lease("worker-1")["status"] == "quarantined"


def test_base_state_conflict_is_rejected(tmp_path: Path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="base_state"):
        store.create_operation(
            operation_id="op-stale",
            request_id="request-stale",
            worker_id="worker-1",
            job_id="job-1",
            revision_id="r0",
            mode=RuntimeMode.READ,
            base_state=3,
            code_hash="c" * 64,
        )
