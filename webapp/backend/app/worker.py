from __future__ import annotations

import json

from .config import settings
from .geometry import build_face_mesh, local_observation
from .nx_worker import NxInfrastructureError, execute_fillet
from .planner import plan_operation, validate_policy
from .schemas import ExecuteRequest
from .store import store


def prepare_revision(job_id: str, revision_id: str) -> None:
    revision_dir = store.revision_dir(job_id, revision_id)
    step_path = revision_dir / "model.stp"
    store.event(job_id, "stage", "Triangulating STEP with OCC")
    mesh = build_face_mesh(step_path, revision_id)
    store.write_json(revision_dir / "mesh.json", mesh)
    store.update(job_id, "ready", revision_id)


def _failure_text(exc: Exception) -> str:
    text = "{}: {}".format(type(exc).__name__, exc).strip()
    return text[-4000:]


def validate_selected_faces(
    faces_by_id: dict[str, dict], selected_ids: set[str]
) -> None:
    if not selected_ids.issubset(faces_by_id):
        raise ValueError("selected faces do not belong to the current revision")


def describe_plan(plan, attempt_number: int, max_attempts: int) -> str:
    targets = ", ".join(plan.target_face_ids)
    return (
        "第 {}/{} 次尝试策略：目标 {} 个面 [{}]；Preference={}；"
        "DeletePartialBlend=关闭（固定）；规划理由：{}"
    ).format(
        attempt_number,
        max_attempts,
        len(plan.target_face_ids),
        targets,
        plan.preference,
        plan.reason,
    )


def execute_request(job_id: str, request: ExecuteRequest) -> None:
    try:
        source_dir = store.revision_dir(job_id, request.revision_id)
        mesh = json.loads((source_dir / "mesh.json").read_text(encoding="utf-8"))
        faces_by_id = {face["faceId"]: face for face in mesh["faces"]}
        selected_ids = {face.face_id for face in request.selected_faces}
        validate_selected_faces(faces_by_id, selected_ids)

        observation = local_observation(mesh, selected_ids)
        selected_by_id = {face.face_id: face for face in request.selected_faces}
        store.event(
            job_id,
            "request",
            "开始去圆角任务：源修订版 {}，用户选择 {} 个面 [{}]；最多尝试 {} 次。".format(
                request.revision_id,
                len(selected_ids),
                ", ".join(sorted(selected_ids)),
                settings.max_delete_attempts,
            ),
        )
        previous_attempts: list[dict] = []
        for attempt_number in range(1, settings.max_delete_attempts + 1):
            store.event(
                job_id,
                "stage",
                "Planning constrained NX operation (attempt {}/{})".format(
                    attempt_number, settings.max_delete_attempts
                ),
            )
            revision_id, revision_dir = store.next_revision(job_id)
            try:
                plan = plan_operation(request, observation, previous_attempts)
                validate_policy(plan, request, previous_attempts)
                store.event(
                    job_id,
                    "strategy",
                    describe_plan(plan, attempt_number, settings.max_delete_attempts),
                )
                output_step = revision_dir / "model.stp"
                canonical = {
                    "schema": "nx_web_fillet_delete_v1",
                    "input": str((source_dir / "model.stp").resolve()),
                    "output": str(output_step.resolve()),
                    "settings_file": str(
                        (settings.nx_base / "STEP214UG" / "ugstep214.def").resolve()
                    ),
                    "preference": plan.preference,
                    "targets": [
                        selected_by_id[target].model_dump()
                        for target in plan.target_face_ids
                    ],
                }
                store.write_json(revision_dir / "plan.json", plan.model_dump())
                request_file = revision_dir / "nx_request.json"
                store.write_json(request_file, canonical)
                store.update(job_id, "running_nx", request.revision_id)
                store.event(
                    job_id,
                    "stage",
                    "Executing fixed NX journal (attempt {}/{})".format(
                        attempt_number, settings.max_delete_attempts
                    ),
                )
                execute_fillet(request_file, revision_dir / "nx.log")
                if not output_step.is_file() or output_step.stat().st_size <= 0:
                    raise RuntimeError("NX did not publish a valid STEP result")
                store.event(
                    job_id,
                    "nx_succeeded",
                    "第 {}/{} 次 NX 删除成功，开始验证输出 STEP 并生成显示网格。".format(
                        attempt_number, settings.max_delete_attempts
                    ),
                )
                result_mesh = build_face_mesh(output_step, revision_id)
                store.write_json(revision_dir / "mesh.json", result_mesh)
                store.write_json(
                    revision_dir / "attempt.json",
                    {"attempt": attempt_number, "status": "succeeded", "plan": plan.model_dump()},
                )
                store.event(
                    job_id,
                    "completed",
                    "任务完成：第 {} 次尝试成功，生成候选修订版 {} 等待确认；结果包含 {} 个 BREP 面。".format(
                        attempt_number, revision_id, result_mesh["faceCount"]
                    ),
                )
                store.publish_candidate(job_id, revision_id)
                return
            except Exception as attempt_exc:
                failure = _failure_text(attempt_exc)
                infrastructure_failure = isinstance(attempt_exc, NxInfrastructureError)
                store.set_revision_status(job_id, revision_id, "failed", failure)
                attempt_record = {
                    "attempt": attempt_number,
                    "status": "failed",
                    "error": failure,
                    "failure_category": (
                        "infrastructure" if infrastructure_failure else "operation"
                    ),
                    "retryable": not infrastructure_failure,
                }
                if "plan" in locals():
                    attempt_record["plan"] = plan.model_dump()
                    del plan
                previous_attempts.append(attempt_record)
                store.write_json(revision_dir / "attempt.json", attempt_record)
                if infrastructure_failure:
                    store.event(
                        job_id,
                        "infrastructure_failed",
                        "第 {}/{} 次尝试遇到 NX 系统错误，立即停止且不进行几何重试：{}".format(
                            attempt_number, settings.max_delete_attempts, failure
                        ),
                    )
                    raise
                store.event(
                    job_id,
                    "attempt_failed",
                    "第 {}/{} 次操作失败：{}。下一次会把该错误和已失败策略反馈给大模型，要求更换 Preference、切换部分圆角选项或缩小目标面集合。".format(
                        attempt_number, settings.max_delete_attempts, failure
                    ),
                )

        errors = "; ".join(item["error"] for item in previous_attempts)
        raise RuntimeError(
            "fillet deletion exhausted {} attempts: {}".format(
                settings.max_delete_attempts, errors[-8000:]
            )
        )
    except Exception as exc:
        store.update(job_id, "failed", request.revision_id, str(exc))
