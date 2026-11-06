from __future__ import annotations

from datetime import datetime
from enum import Enum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROTOCOL_VERSION = 1
DESCRIPTOR_VERSION = 1
MAX_CODE_CHARS = 200_000
MAX_MESSAGE_BYTES = 1024 * 1024


class RuntimeMode(str, Enum):
    READ = "read"
    PREVIEW = "preview"
    COMMIT = "commit"


class OperationState(str, Enum):
    RECEIVED = "received"
    REJECTED = "rejected"
    QUEUED = "queued"
    EXPIRED_BEFORE_START = "expired_before_start"
    RUNNING = "running"
    VALIDATING = "validating"
    COMPLETED = "completed"
    COMMITTED = "committed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    AMBIGUOUS = "ambiguous"


class RetryClass(str, Enum):
    NEVER = "never"
    SAME_OPERATION = "same_operation"
    STATUS_THEN_DECIDE = "status_then_decide"
    RESET_REQUIRED = "reset_required"


TERMINAL_OPERATION_STATES = frozenset(
    {
        OperationState.REJECTED,
        OperationState.EXPIRED_BEFORE_START,
        OperationState.COMPLETED,
        OperationState.COMMITTED,
        OperationState.ROLLED_BACK,
        OperationState.ROLLBACK_FAILED,
    }
)

BLOCKING_OPERATION_STATES = frozenset(
    {
        OperationState.RECEIVED,
        OperationState.QUEUED,
        OperationState.RUNNING,
        OperationState.VALIDATING,
        OperationState.ROLLING_BACK,
        OperationState.AMBIGUOUS,
        OperationState.ROLLBACK_FAILED,
    }
)

_ALLOWED_TRANSITIONS: dict[OperationState, frozenset[OperationState]] = {
    OperationState.RECEIVED: frozenset(
        {OperationState.REJECTED, OperationState.QUEUED, OperationState.EXPIRED_BEFORE_START}
    ),
    OperationState.QUEUED: frozenset(
        {OperationState.REJECTED, OperationState.EXPIRED_BEFORE_START, OperationState.RUNNING}
    ),
    OperationState.RUNNING: frozenset(
        {
            OperationState.REJECTED,
            OperationState.EXPIRED_BEFORE_START,
            OperationState.VALIDATING,
            OperationState.ROLLING_BACK,
            OperationState.AMBIGUOUS,
        }
    ),
    OperationState.VALIDATING: frozenset(
        {
            OperationState.COMPLETED,
            OperationState.COMMITTED,
            OperationState.ROLLING_BACK,
            OperationState.AMBIGUOUS,
        }
    ),
    OperationState.ROLLING_BACK: frozenset(
        {OperationState.ROLLED_BACK, OperationState.ROLLBACK_FAILED, OperationState.AMBIGUOUS}
    ),
    OperationState.AMBIGUOUS: frozenset(
        {
            OperationState.VALIDATING,
            OperationState.COMPLETED,
            OperationState.COMMITTED,
            OperationState.ROLLING_BACK,
            OperationState.ROLLED_BACK,
            OperationState.ROLLBACK_FAILED,
        }
    ),
    OperationState.REJECTED: frozenset(),
    OperationState.EXPIRED_BEFORE_START: frozenset(),
    OperationState.COMPLETED: frozenset(),
    OperationState.COMMITTED: frozenset(),
    OperationState.ROLLED_BACK: frozenset(),
    OperationState.ROLLBACK_FAILED: frozenset(),
}


def validate_operation_transition(current: OperationState, target: OperationState) -> None:
    if current == target:
        return
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError("invalid operation state transition: {} -> {}".format(current.value, target.value))


def code_digest(code: str) -> str:
    return sha256(code.encode("utf-8")).hexdigest()


class PublicRuntimeExecuteRequest(BaseModel):
    """Session-bound API accepted from an Agent; binding is injected server-side."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(pattern=r"^op-[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
    base_state: int = Field(ge=0)
    mode: RuntimeMode
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    preview_operation_id: str | None = Field(
        default=None, pattern=r"^op-[A-Za-z0-9][A-Za-z0-9._-]{0,126}$"
    )
    deadline_at: datetime | None = None

    @field_validator("code")
    @classmethod
    def code_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("code cannot be blank")
        return value

    @model_validator(mode="after")
    def preview_reference_is_commit_only(self) -> "PublicRuntimeExecuteRequest":
        if self.mode != RuntimeMode.COMMIT and self.preview_operation_id is not None:
            raise ValueError("preview_operation_id is valid only in commit mode")
        return self


class ExecutionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_stdout_chars: int = Field(default=16_000, ge=0, le=1_000_000)
    max_stderr_chars: int = Field(default=16_000, ge=0, le=1_000_000)
    max_result_bytes: int = Field(default=MAX_MESSAGE_BYTES, ge=1024, le=16 * MAX_MESSAGE_BYTES)


class RuntimeExecuteRequest(PublicRuntimeExecuteRequest):
    """Internal request after the backend adds authoritative binding and policy."""

    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(pattern=r"^r\d+$")
    worker_id: str = Field(min_length=1, max_length=128)
    code_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    capability_grant: str = Field(min_length=1, max_length=4096)

    @model_validator(mode="after")
    def digest_matches(self) -> "RuntimeExecuteRequest":
        if code_digest(self.code) != self.code_hash:
            raise ValueError("code_hash does not match code")
        return self


class RuntimeErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)
    phase: str | None = Field(default=None, max_length=100)
    retry_class: RetryClass = RetryClass.NEVER
    nx_code: int | str | None = None
    operation_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RuntimeExecuteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    request_id: str
    operation_id: str
    state: OperationState
    mode: RuntimeMode
    base_state: int = Field(ge=0)
    result_state: int | None = Field(default=None, ge=0)
    code_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    retained: bool
    rolled_back: bool
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    baseline: dict[str, Any] = Field(default_factory=dict)
    post_state: dict[str, Any] = Field(default_factory=dict)
    restore_state: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    invalidated_reference_scopes: list[str] = Field(default_factory=list)
    error: RuntimeErrorPayload | None = None
    received_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def state_invariants(self) -> "RuntimeExecuteResponse":
        if self.state == OperationState.COMPLETED:
            if self.mode != RuntimeMode.READ or self.retained or self.rolled_back:
                raise ValueError("completed responses represent successful read operations")
            if self.result_state != self.base_state:
                raise ValueError("completed read responses must preserve runtime state")
        if self.state == OperationState.COMMITTED:
            if self.mode != RuntimeMode.COMMIT or not self.retained:
                raise ValueError("committed responses must retain a commit operation")
            if self.result_state != self.base_state + 1:
                raise ValueError("committed responses must advance runtime state exactly once")
        if self.mode == RuntimeMode.PREVIEW and self.retained:
            raise ValueError("preview responses cannot retain changes")
        if self.rolled_back and self.retained:
            raise ValueError("a response cannot be both retained and rolled back")
        return self


class RuntimeStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connected: bool
    nx_version: str | None = None
    bridge_build: str | None = None
    executor_build: str | None = None
    bridge_instance_id: str | None = None
    worker_id: str | None = None
    job_id: str | None = None
    revision_id: str | None = None
    runtime_state: int = Field(default=0, ge=0)
    work_part: dict[str, Any] | None = None
    part_fingerprint: str | None = None
    status: Literal["offline", "loading", "ready", "busy", "diverged", "quarantined"]
    modifiable: bool = False
    high_privilege_enabled: bool = False
    blocking_operation_id: str | None = None
    last_operation_id: str | None = None


class DescriptorLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_request_bytes: int = Field(default=MAX_MESSAGE_BYTES, ge=1024)
    max_response_bytes: int = Field(default=MAX_MESSAGE_BYTES, ge=1024)
    max_code_chars: int = Field(default=MAX_CODE_CHARS, ge=1)


class BridgeDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    descriptor_version: Literal[DESCRIPTOR_VERSION] = DESCRIPTOR_VERSION
    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    bridge_instance_id: str = Field(min_length=1, max_length=128)
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(ge=1, le=65535)
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    pid: int = Field(gt=0)
    process_start_time_utc: datetime
    nx_version: str = Field(min_length=1, max_length=100)
    bridge_build: str = Field(min_length=1, max_length=128)
    executor_build: str = Field(min_length=1, max_length=128)
    created_at: datetime
    expires_at: datetime | None = None
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    limits: DescriptorLimits = Field(default_factory=DescriptorLimits)
    workspace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    high_privilege_enabled: bool = False

    @model_validator(mode="after")
    def expiration_is_after_creation(self) -> "BridgeDescriptor":
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class BridgeRequestEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    method: Literal["ping", "execute", "get_operation"]
    params: dict[str, Any] = Field(default_factory=dict)


class BridgeResponseEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal[PROTOCOL_VERSION] = PROTOCOL_VERSION
    request_id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: RuntimeErrorPayload | None = None

    @model_validator(mode="after")
    def exactly_one_payload(self) -> "BridgeResponseEnvelope":
        if self.ok and (self.result is None or self.error is not None):
            raise ValueError("successful bridge response must contain result only")
        if not self.ok and (self.error is None or self.result is not None):
            raise ValueError("failed bridge response must contain error only")
        return self
