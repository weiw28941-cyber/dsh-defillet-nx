from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from webapp.backend.app.nx_runtime_contracts import (
    BridgeDescriptor,
    OperationState,
    PublicRuntimeExecuteRequest,
    RuntimeExecuteRequest,
    RuntimeExecuteResponse,
    RuntimeMode,
    code_digest,
    validate_operation_transition,
)


def execute_request(**overrides):
    code = overrides.pop("code", "result = {'body_count': 1}")
    payload = {
        "request_id": "request-1",
        "operation_id": "op-operation-1",
        "job_id": "job-1",
        "revision_id": "r0",
        "worker_id": "worker-1",
        "base_state": 0,
        "mode": "read",
        "code": code,
        "code_hash": code_digest(code),
        "capability_grant": "test-grant",
    }
    payload.update(overrides)
    return RuntimeExecuteRequest.model_validate(payload)


def response_payload(**overrides):
    payload = {
        "request_id": "request-1",
        "operation_id": "op-operation-1",
        "state": "committed",
        "mode": "commit",
        "base_state": 3,
        "result_state": 4,
        "code_hash": "a" * 64,
        "retained": True,
        "rolled_back": False,
        "received_at": datetime.now(timezone.utc),
    }
    payload.update(overrides)
    return payload


def test_public_request_does_not_accept_backend_binding_fields():
    with pytest.raises(ValidationError):
        PublicRuntimeExecuteRequest.model_validate(
            {
                "operation_id": "op-operation-1",
                "base_state": 0,
                "mode": "read",
                "code": "result = 1",
                "job_id": "must-not-be-user-controlled",
            }
        )


def test_execute_request_rejects_unknown_fields_and_wrong_digest():
    with pytest.raises(ValidationError):
        execute_request(extra_field=True)
    with pytest.raises(ValidationError, match="code_hash"):
        execute_request(code_hash="0" * 64)


def test_preview_reference_is_commit_only():
    with pytest.raises(ValidationError, match="commit mode"):
        execute_request(mode="preview", preview_operation_id="op-preview-1")
    request = execute_request(mode="commit", preview_operation_id="op-preview-1")
    assert request.mode == RuntimeMode.COMMIT


def test_read_request_can_omit_preview_reference():
    request = execute_request(mode="read")
    assert request.preview_operation_id is None


def test_completed_read_preserves_state():
    response = RuntimeExecuteResponse(
        **response_payload(
            state="completed",
            mode="read",
            result_state=3,
            retained=False,
        )
    )
    assert response.state == OperationState.COMPLETED


def test_committed_response_must_advance_state_once():
    response = RuntimeExecuteResponse(**response_payload())
    assert response.result_state == 4

    with pytest.raises(ValidationError, match="advance runtime state"):
        RuntimeExecuteResponse(**response_payload(result_state=5))


def test_preview_response_cannot_retain_changes():
    with pytest.raises(ValidationError, match="preview responses"):
        RuntimeExecuteResponse(
            **response_payload(
                state="rolled_back",
                mode="preview",
                base_state=0,
                result_state=0,
                retained=True,
            )
        )


def test_operation_state_machine_rejects_invalid_transition():
    validate_operation_transition(OperationState.RECEIVED, OperationState.QUEUED)
    validate_operation_transition(OperationState.QUEUED, OperationState.RUNNING)
    validate_operation_transition(OperationState.VALIDATING, OperationState.COMPLETED)
    with pytest.raises(ValueError, match="invalid operation state transition"):
        validate_operation_transition(OperationState.RECEIVED, OperationState.COMMITTED)


def descriptor_payload(created):
    return {
        "bridge_instance_id": "bridge-1",
        "port": 54321,
        "token": "a" * 64,
        "pid": 123,
        "process_start_time_utc": created,
        "nx_version": "NX 2406",
        "bridge_build": "dev",
        "executor_build": "dev",
        "created_at": created,
        "expires_at": created + timedelta(hours=1),
        "capabilities": ["dynamic-nxopen-v1"],
        "workspace_digest": "b" * 64,
        "high_privilege_enabled": True,
    }


def test_bridge_descriptor_is_loopback_and_has_valid_expiration():
    created = datetime.now(timezone.utc)
    descriptor = BridgeDescriptor.model_validate(descriptor_payload(created))
    assert descriptor.host == "127.0.0.1"

    payload = descriptor_payload(created)
    payload["expires_at"] = created
    with pytest.raises(ValidationError, match="later than"):
        BridgeDescriptor.model_validate(payload)
