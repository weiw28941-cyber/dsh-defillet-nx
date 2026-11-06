from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid

from .component_rendering import render_fillet_components
from .fillet_detection import inject_step_face_names
from .geometry import build_face_mesh
from .config import settings
from .nx_worker import (
    NxInfrastructureError,
    delete_fillet_component as run_nx_component_delete,
    detect_fillet_components as run_nx_component_detection,
    remove_all_fillets as run_nx_remove_all_fillets,
)
from .store import JobStore

SCHEMA = "nx_fillet_components_v1"


def detect_fillet_components(
    store: JobStore,
    job_id: str,
    revision_id: str,
    force: bool = False,
) -> dict:
    revision_dir = store.revision_dir(job_id, revision_id)
    cache_path = revision_dir / "nx_fillet_components.json"
    if cache_path.is_file() and not force:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("schema") == SCHEMA and payload.get("revision_id") == revision_id:
            return payload

    step_path = revision_dir / "model.stp"
    tagged_step = revision_dir / "nx_fillet_components_tagged_input.stp"
    report_path = revision_dir / "nx_fillet_components_raw.json"
    log_path = revision_dir / "nx_fillet_components.log"
    inject_step_face_names(step_path, tagged_step)
    run_nx_component_detection(tagged_step, report_path, log_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema") != SCHEMA or not report.get("success"):
        raise RuntimeError("NX returned an invalid fillet component report")
    if not report.get("mapping_complete"):
        raise RuntimeError("NX did not preserve every injected STEP face name")

    components = report.get("components") or []
    component_ids = [component.get("component_id") for component in components]
    if len(component_ids) != len(set(component_ids)) or any(not value for value in component_ids):
        raise RuntimeError("NX returned duplicate or missing component IDs")
    grouped_ids = [
        int(step_id)
        for component in components
        for step_id in component.get("step_entity_ids") or []
    ]
    if len(grouped_ids) != len(set(grouped_ids)):
        raise RuntimeError("NX assigned one STEP face to multiple components")
    if len(grouped_ids) != int(report.get("candidate_face_count", -1)):
        raise RuntimeError("NX component face total does not match candidate count")

    mesh_path = revision_dir / "mesh.json"
    mesh = json.loads(mesh_path.read_text(encoding="utf-8"))
    if mesh.get("stepEntityIdSource") != "part21-ident-label/all-transfer-levels/v2":
        mesh = build_face_mesh(step_path, revision_id)
        store.write_json(mesh_path, mesh)
    available = {
        int(face["stepEntityId"])
        for face in mesh["faces"]
        if face.get("stepEntityId")
    }
    missing = sorted(set(grouped_ids) - available)
    if missing:
        raise RuntimeError("NX component report contains unknown STEP face IDs: {}".format(missing[:20]))

    payload = {
        "schema": SCHEMA,
        "revision_id": revision_id,
        "engine": report.get("engine"),
        "candidate_face_count": len(grouped_ids),
        "component_count": len(components),
        "components": components,
    }
    store.write_json(cache_path, payload)
    return payload


def component_by_id(payload: dict, component_id: str) -> dict:
    for component in payload.get("components") or []:
        if component.get("component_id") == component_id:
            return component
    raise ValueError("component does not belong to the current accepted revision")


def analyze_fillet_components(
    store: JobStore,
    job_id: str,
    revision_id: str,
) -> dict:
    """Render every current fillet component in a deterministic color-coded view sheet."""
    payload = detect_fillet_components(store, job_id, revision_id)
    revision_dir = store.revision_dir(job_id, revision_id)
    image_path = revision_dir / "fillet_components_visual_analysis.png"
    result_path = revision_dir / "fillet_components_visual_analysis.json"
    result = render_fillet_components(revision_dir / "mesh.json", payload, image_path)
    result.update(
        status="ready_for_visual_analysis",
        image_name=image_path.name,
        analysis_instruction=(
            "Use the four rendered views and color legend to infer each component's shape, "
            "topology, likely support-face behavior, and plausible removal method. Distinguish "
            "currently executable whole-component deletion from speculative methods."
        ),
        currently_executable_strategy="whole-component-face-edge-blend",
    )
    store.write_json(result_path, result)
    return result


def delete_fillet_components(
    store: JobStore,
    job_id: str,
    revision_id: str,
    component_ids: list[str],
    strategy: str,
    preference: str,
    *,
    single_compat: bool = False,
    publish_candidate: bool = True,
    manage_job_state: bool = True,
) -> dict:
    """Delete complete current-revision components with one NX commit."""
    if strategy != "whole-component-face-edge-blend":
        raise ValueError("unsupported component deletion strategy")
    if not component_ids:
        raise ValueError("component_ids must not be empty")
    if len(component_ids) != len(set(component_ids)):
        raise ValueError("component_ids must be unique")

    payload = detect_fillet_components(store, job_id, revision_id)
    by_id = {component.get("component_id"): component for component in payload.get("components") or []}
    missing = [component_id for component_id in component_ids if component_id not in by_id]
    if missing:
        raise ValueError(
            "components do not belong to the current accepted revision: {}".format(", ".join(missing))
        )
    selected = [by_id[component_id] for component_id in component_ids]
    face_ids = [
        int(step_id)
        for component in selected
        for step_id in component.get("step_entity_ids") or []
    ]
    if not face_ids or any(step_id <= 0 for step_id in face_ids):
        raise ValueError("selected components contain invalid STEP face IDs")
    if len(face_ids) != len(set(face_ids)):
        raise ValueError("selected components contain overlapping STEP face IDs")
    face_ids = sorted(face_ids)

    revision_id_out, revision_dir = store.next_revision(job_id, parent_revision=revision_id)
    output_step = revision_dir / "model.stp"
    report_path = revision_dir / "nx_component_delete_result.json"
    request_path = revision_dir / "nx_component_delete_request.json"
    tagged_step = revision_dir / "nx_component_delete_tagged_input.stp"
    result_schema = "fillet_component_delete_result_v1" if single_compat else "fillet_components_delete_result_v1"
    try:
        source_dir = store.revision_dir(job_id, revision_id)
        inject_step_face_names(source_dir / "model.stp", tagged_step)
        canonical = {
            "schema": "nx_fillet_components_delete_v1",
            "input": str(tagged_step.resolve()),
            "output": str(output_step.resolve()),
            "report": str(report_path.resolve()),
            "settings_file": str((settings.nx_base / "STEP214UG" / "ugstep214.def").resolve()),
            "component_ids": component_ids,
            "step_entity_ids": face_ids,
            "strategy": strategy,
            "preference": preference,
        }
        store.write_json(request_path, canonical)
        store.event(job_id, "component_delete", "NX 正在删除连通块 {}".format(", ".join(component_ids)))
        run_nx_component_delete(request_path, report_path, revision_dir / "nx_component_delete.log")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not report.get("success") or not output_step.is_file() or output_step.stat().st_size <= 0:
            raise RuntimeError(report.get("error") or "NX did not publish a valid component deletion result")
        if report.get("schema") != "nx_fillet_components_delete_result_v1":
            raise ValueError("NX returned an invalid component deletion report schema")
        if report.get("component_ids") != component_ids or report.get("strategy") != strategy:
            raise ValueError("NX deletion report does not match the requested components or strategy")
        if sorted(int(value) for value in report.get("deleted_step_entity_ids") or []) != face_ids:
            raise ValueError("NX deletion report does not match the requested STEP face IDs")
        try:
            result_mesh = build_face_mesh(output_step, revision_id_out)
        except Exception as exc:
            raise ValueError("candidate STEP failed OCC re-import: {}".format(exc)) from exc
        if int(result_mesh.get("faceCount", 0)) <= 0:
            raise ValueError("candidate STEP contains no BREP faces")
        store.write_json(revision_dir / "mesh.json", result_mesh)
        result = {
            "schema": result_schema,
            "status": "succeeded",
            "source_revision": revision_id,
            "candidate_revision": revision_id_out,
            "component_ids": component_ids,
            "strategy": strategy,
            "deleted_step_entity_ids": face_ids,
            "validation": {
                "step_exported": True,
                "step_reimported": True,
                "result_face_count": result_mesh["faceCount"],
            },
        }
        if single_compat:
            result["component_id"] = component_ids[0]
        store.write_json(revision_dir / "component_delete.json", result)
        if publish_candidate:
            store.publish_candidate(job_id, revision_id_out)
        else:
            store.set_revision_status(job_id, revision_id_out, "intermediate")
            if manage_job_state:
                store.update(job_id, "ready", revision_id, None)
        return result
    except Exception as exc:
        if isinstance(exc, NxInfrastructureError):
            failure_category = "infrastructure"
        elif isinstance(exc, ValueError):
            failure_category = "validation"
        else:
            failure_category = "geometry"
        if output_step.is_file():
            output_step.unlink()
        store.set_revision_status(job_id, revision_id_out, "failed", str(exc))
        if manage_job_state:
            store.update(job_id, "ready", revision_id, None)
        result = {
            "schema": result_schema,
            "status": "failed",
            "source_revision": revision_id,
            "candidate_revision": None,
            "component_ids": component_ids,
            "strategy": strategy,
            "failure_category": failure_category,
            "error": str(exc),
            "rollback": True,
            "allowed_next_strategies": [],
        }
        if single_compat:
            result["component_id"] = component_ids[0]
        return result


def remove_all_fillets(
    store: JobStore,
    job_id: str,
    revision_id: str,
    preference: str,
) -> dict:
    """Run the complete detect/delete/bisect loop in one NX process."""
    summary = {
        "schema": "remove_all_fillets_result_v3",
        "source_revision": revision_id,
        "status": "aborted",
        "candidate_revision": None,
        "rounds": [],
        "attempts": [],
        "successful_batches": [],
        "successful_component_ids": [],
        "all_failed": False,
    }
    if not store.begin_execute(job_id, revision_id):
        summary.update(
            failure_category="state",
            error="accepted revision is not ready or a candidate awaits review",
        )
        return summary

    revision_id_out = None
    staging_dir = store.root / job_id / "operations" / ("remove-all-" + uuid.uuid4().hex)
    try:
        source_dir = store.revision_dir(job_id, revision_id)
        staging_dir.mkdir(parents=True)
        input_step = staging_dir / "nx_remove_all_tagged_input.stp"
        output_step = staging_dir / "model.stp"
        report_path = staging_dir / "nx_remove_all_result.json"
        request_path = staging_dir / "nx_remove_all_request.json"
        inject_step_face_names(source_dir / "model.stp", input_step)
        request = {
            "schema": "nx_remove_all_fillets_v1",
            "input": str(input_step.resolve()),
            "output": str(output_step.resolve()),
            "report": str(report_path.resolve()),
            "settings_file": str((settings.nx_base / "STEP214UG" / "ugstep214.def").resolve()),
            "preference": preference,
        }
        store.write_json(request_path, request)
        store.event(job_id, "component_delete", "NX 正在单会话内删除全部可删除圆角")
        run_nx_remove_all_fillets(request_path, report_path, staging_dir / "nx_remove_all.log")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("schema") != "nx_remove_all_fillets_result_v2":
            raise ValueError("NX returned an invalid global fillet deletion report schema")
        summary.update(
            rounds=report.get("rounds") or [],
            attempts=report.get("attempts") or [],
            successful_batches=report.get("successful_batches") or [],
            terminal_reason=report.get("terminal_reason"),
            roundtrip_normalization=report.get("roundtrip_normalization"),
        )
        successful = summary["successful_batches"]
        terminal_reason = report.get("terminal_reason")
        if terminal_reason not in {"no_components", "no_deletable_components", "process_failure"}:
            raise ValueError("NX returned an invalid terminal reason")
        if terminal_reason == "process_failure":
            store.update(job_id, "ready", revision_id, None)
            summary.update(
                status="aborted",
                failure_category=report.get("failure_category") or "infrastructure",
                error=report.get("error") or "NX global deletion failed",
                all_failed=False,
            )
            return summary
        if bool(report.get("success")) != bool(successful):
            raise ValueError("NX success flag does not match successful batches")
        if not successful:
            if output_step.is_file():
                output_step.unlink()
            store.update(job_id, "ready", revision_id, None)
            summary.update(
                status=terminal_reason,
                all_failed=terminal_reason == "no_deletable_components",
            )
            return summary
        if not report.get("success") or not output_step.is_file() or output_step.stat().st_size <= 0:
            raise ValueError(report.get("error") or "NX did not export the accumulated STEP result")
        normalization = report.get("roundtrip_normalization") or {}
        if (
            normalization.get("performed") is not True
            or normalization.get("same_process") is not True
            or normalization.get("scope") != "final-accumulated-result"
            or normalization.get("reimport_count") != 1
        ):
            raise ValueError("NX did not normalize the final accumulated STEP result exactly once")
        normalized_face_count = int(normalization.get("normalized_output_step_face_count") or 0)
        nx_roundtrip_face_count = int(normalization.get("nx_roundtrip_face_count") or 0)
        if normalized_face_count <= 0 or normalized_face_count != nx_roundtrip_face_count:
            raise ValueError("NX roundtrip normalization reported inconsistent face counts")
        if Path(str(report.get("output", ""))).resolve() != output_step.resolve():
            raise ValueError("NX report output does not match the requested output")
        deleted_ids = sorted(int(value) for value in report.get("deleted_step_entity_ids") or [])
        if not deleted_ids or len(deleted_ids) != len(set(deleted_ids)) or any(value <= 0 for value in deleted_ids):
            raise ValueError("NX reported invalid deleted STEP face IDs")
        batch_deleted = sorted({
            int(value)
            for batch in successful
            for value in batch.get("deleted_step_entity_ids") or []
        })
        if batch_deleted != deleted_ids:
            raise ValueError("NX successful batches do not match deleted STEP face IDs")
        source_mesh = json.loads((source_dir / "mesh.json").read_text(encoding="utf-8"))
        available_ids = {
            int(face["stepEntityId"])
            for face in source_mesh.get("faces") or []
            if face.get("stepEntityId") is not None
        }
        if not set(deleted_ids).issubset(available_ids):
            raise ValueError("NX reported deleted STEP face IDs outside the source model")
        successful_attempts = {
            (int(attempt.get("round", 0)), int(attempt.get("attempt", 0))): attempt
            for attempt in summary["attempts"]
            if attempt.get("status") == "succeeded"
        }
        for batch in successful:
            key = (int(batch.get("round", 0)), int(batch.get("attempt", 0)))
            attempt = successful_attempts.get(key)
            if attempt is None or attempt.get("component_ids") != batch.get("component_ids"):
                raise ValueError("NX successful batch does not match a successful attempt")

        revision_id_out, revision_dir = store.next_revision(job_id, parent_revision=revision_id)
        final_step = revision_dir / "model.stp"
        shutil.move(str(output_step), str(final_step))
        for name in ("nx_remove_all_result.json", "nx_remove_all_request.json", "nx_remove_all.log"):
            source = staging_dir / name
            if source.is_file():
                shutil.copy2(source, revision_dir / name)

        # Expensive OCC validation and tessellation happen once, on the final export.
        mesh = build_face_mesh(final_step, revision_id_out)
        if int(mesh.get("faceCount", 0)) <= 0:
            raise ValueError("candidate STEP contains no BREP faces")
        store.write_json(revision_dir / "mesh.json", mesh)
        summary.update(
            status="succeeded",
            candidate_revision=revision_id_out,
            deleted_step_entity_ids=deleted_ids,
            successful_component_ids=[
                {"round": batch.get("round"), "component_id": component_id}
                for batch in successful
                for component_id in batch.get("component_ids") or []
            ],
            validation={
                "step_exported": True,
                "step_reimported": True,
                "result_face_count": mesh["faceCount"],
            },
        )
        store.write_json(revision_dir / "remove_all_fillets.json", summary)
        store.publish_candidate(job_id, revision_id_out)
        return summary
    except Exception as exc:
        category = "infrastructure" if isinstance(exc, NxInfrastructureError) else "validation"
        if revision_id_out is not None:
            try:
                store.set_revision_status(job_id, revision_id_out, "failed", str(exc))
            except Exception:
                pass
        store.update(job_id, "ready", revision_id, None)
        summary.update(status="aborted", failure_category=category, error=str(exc))
        return summary
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def delete_one_fillet_component(
    store: JobStore,
    job_id: str,
    revision_id: str,
    component_id: str,
    strategy: str,
    preference: str,
) -> dict:
    return delete_fillet_components(
        store, job_id, revision_id, [component_id], strategy, preference,
        single_compat=True,
    )
