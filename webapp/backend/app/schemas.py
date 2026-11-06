from __future__ import annotations

import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SelectedFace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    face_id: str
    face_index: int = Field(gt=0)
    step_entity_id: int | None = Field(default=None, gt=0)
    pick_point: tuple[float, float, float]


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str
    selected_faces: list[SelectedFace] = Field(min_length=1, max_length=50)
    prompt: str = Field(min_length=1, max_length=4000)

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("prompt cannot be blank")
        return value


class FaceSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(pattern=r"^r\d+$")
    selected_faces: list[SelectedFace] = Field(max_length=500)

    @field_validator("selected_faces")
    @classmethod
    def unique_face_ids(cls, values: list[SelectedFace]) -> list[SelectedFace]:
        ids = [value.face_id for value in values]
        if len(ids) != len(set(ids)):
            raise ValueError("selected face IDs must be unique")
        return values


class RevisionRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_revision: str = Field(pattern=r"^r\d+$")


class SessionBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = Field(default=None, max_length=500)

    @field_validator("session_id")
    @classmethod
    def normalize_session_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("session_id cannot be blank")
        return value


class DeleteFilletComponentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_id: str = Field(pattern=r"^fc-\d{3,}$")
    strategy: Literal["whole-component-face-edge-blend"] = "whole-component-face-edge-blend"
    preference: Literal["cliff", "notch"] = "cliff"


class DeleteFilletComponentsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_ids: list[str] = Field(min_length=1, max_length=100)
    strategy: Literal["whole-component-face-edge-blend"] = "whole-component-face-edge-blend"
    preference: Literal["cliff", "notch"] = "cliff"

    @field_validator("component_ids")
    @classmethod
    def validate_component_ids(cls, values: list[str]) -> list[str]:
        pattern = re.compile(r"^fc-\d{3,}$")
        if any(not pattern.fullmatch(value) for value in values):
            raise ValueError("every component_id must match fc- followed by at least three digits")
        if len(values) != len(set(values)):
            raise ValueError("component_ids must be unique")
        return values


class DeleteAllFilletsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preference: Literal["cliff", "notch"] = "cliff"


class AcceptedRuntimeComponentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_id: str = Field(pattern=r"^fc-\d{3,}$")


class RuntimeComponentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component_id: str = Field(pattern=r"^rs\d{6}-fc-\d{4,}$")


class RuntimeFaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    face_ref: str = Field(pattern=r"^rs\d{6}-face-\d+$")


class RuntimeSegmentRequest(RuntimeComponentRequest):
    method: Literal["topology_branch", "simple_chains", "surface_type", "bisection"]


class RuntimeDeleteComponentRequest(RuntimeComponentRequest):
    method: Literal["face_edge_blend", "blend", "generic_face"] = "face_edge_blend"
    preference: Literal["cliff", "notch"] = "cliff"


class RuntimeDeleteSegmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment_id: str = Field(pattern=r"^rs\d{6}-fc-\d{4,}-seg-[0-9a-f]{8}-\d{4,}$")
    method: Literal["face_edge_blend", "blend", "generic_face"] = "face_edge_blend"
    preference: Literal["cliff", "notch"] = "cliff"


class RuntimeFaceGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    face_refs: list[str] = Field(min_length=1, max_length=500)
    method: Literal["face_edge_blend", "blend", "generic_face"] = "generic_face"
    preference: Literal["cliff", "notch"] = "cliff"
    allow_related_faces: bool = False

    @field_validator("face_refs")
    @classmethod
    def unique_face_refs(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("face_refs must be unique")
        return values


class FilletDeletePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["fillet.delete"]
    target_face_ids: list[str] = Field(min_length=1, max_length=50)
    preference: Literal["cliff", "notch"] = "cliff"
    reason: str = Field(max_length=1000)
