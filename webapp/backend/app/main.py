from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from .config import settings
from .fillet_components import (
    analyze_fillet_components,
    delete_fillet_components,
    delete_one_fillet_component,
    detect_fillet_components,
    remove_all_fillets,
)
from .fillet_detection import detect_all_fillet_faces
from .geometry import build_face_mesh
from .nx_runtime_contracts import PublicRuntimeExecuteRequest
from .nx_runtime_coordinator import RuntimeCoordinator, RuntimeCoordinatorError
from .nx_cad_tools import RuntimeCadTools
from .nx_persistent_worker import PersistentWorkerError
from .schemas import (
    AcceptedRuntimeComponentRequest,
    DeleteAllFilletsRequest,
    DeleteFilletComponentRequest,
    DeleteFilletComponentsRequest,
    ExecuteRequest,
    FaceSelectionRequest,
    RevisionRollbackRequest,
    SessionBindingRequest,
    RuntimeComponentRequest,
    RuntimeDeleteComponentRequest,
    RuntimeDeleteSegmentRequest,
    RuntimeFaceRequest,
    RuntimeFaceGroupRequest,
    RuntimeSegmentRequest,
)
from .store import store
from .worker import execute_request, prepare_revision


runtime = RuntimeCoordinator(store)
runtime_cad = RuntimeCadTools(runtime)


app = FastAPI(title="STEP Intelligent Editor", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3080",
        "http://127.0.0.1:3080",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"ok": True, "nxRunner": str(settings.run_journal), "llmConfigured": bool(settings.openai_api_key and settings.openai_model)}


@app.post("/api/jobs")
async def upload_step(file: UploadFile = File(...), session_id: str | None = None):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".stp", ".step"}:
        raise HTTPException(400, "Only .stp and .step files are accepted")
    job_id, job_dir = store.create_job(session_id=session_id)
    if session_id:
        store.bind_session(job_id, session_id)
    destination = job_dir / "revisions" / "r0" / "model.stp"
    size = 0
    with destination.open("wb") as output:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                shutil.rmtree(job_dir, ignore_errors=True)
                raise HTTPException(413, "STEP upload exceeds size limit")
            output.write(chunk)
    try:
        prepare_revision(job_id, "r0")
    except Exception as exc:
        store.update(job_id, "failed", "r0", str(exc))
        raise HTTPException(500, "STEP processing failed: {}".format(exc))
    return {"jobId": job_id, "revisionId": "r0", "status": "ready"}


@app.post("/api/sessions/{session_id}/jobs")
async def upload_step_for_session(session_id: str, file: UploadFile = File(...)):
    return await upload_step(file, session_id=session_id)


@app.get("/api/sessions/{session_id}/job")
def recover_session_job(session_id: str):
    try:
        return store.get_by_session(session_id)
    except KeyError:
        raise HTTPException(404, "session has no bound job")


@app.put("/api/sessions/{session_id}/selection")
def set_session_face_selection(session_id: str, request: FaceSelectionRequest):
    try:
        job = store.get_by_session(session_id)
        if request.revision_id != job["accepted_revision"]:
            raise HTTPException(409, "selection does not belong to the current accepted revision")
        revision_dir = store.revision_dir(job["id"], request.revision_id)
        mesh = json.loads((revision_dir / "mesh.json").read_text(encoding="utf-8"))
        faces_by_id = {
            face["faceId"]: face
            for face in mesh.get("faces") or []
            if face.get("faceId")
        }
        for selected in request.selected_faces:
            face = faces_by_id.get(selected.face_id)
            if face is None:
                raise HTTPException(400, "selected face does not belong to the accepted revision")
            if selected.face_index != face.get("faceIndex"):
                raise HTTPException(400, "selected face index does not match the mesh")
            if selected.step_entity_id != face.get("stepEntityId"):
                raise HTTPException(400, "selected face STEP identity does not match the mesh")
        return store.set_face_selection(
            session_id,
            job["id"],
            request.revision_id,
            [face.model_dump() for face in request.selected_faces],
        )
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except HTTPException:
        raise
    except (OSError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/sessions/{session_id}/selection")
def get_session_face_selection(session_id: str):
    try:
        return store.get_face_selection(session_id)
    except KeyError:
        raise HTTPException(404, "session has no bound job")


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    try:
        return store.get(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")


@app.post("/api/sessions/{session_id}/fillet-components/detect")
def detect_session_fillet_components(session_id: str, force: bool = False):
    try:
        job = store.get_by_session(session_id)
        revision_id = job["accepted_revision"]
        if not revision_id:
            raise HTTPException(409, "session job has no accepted revision")
        if job["status"] not in {"ready", "succeeded"} or job["candidate_revision"] is not None:
            raise HTTPException(409, "accepted revision is not ready for component detection")
        store.event(job["id"], "nx_component_detection", "NX 正在识别圆角连通块")
        payload = detect_fillet_components(store, job["id"], revision_id, force=force)
        store.event(
            job["id"],
            "nx_components_detected",
            "NX 已将 {} 个候选面划分为 {} 个连通块".format(
                payload["candidate_face_count"], payload["component_count"]
            ),
        )
        return payload
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "NX fillet component detection failed: {}".format(exc))


@app.post("/api/sessions/{session_id}/fillet-components/analyze")
def analyze_session_fillet_components(session_id: str):
    try:
        job = store.get_by_session(session_id)
        revision_id = job["accepted_revision"]
        if not revision_id:
            raise HTTPException(409, "session job has no accepted revision")
        result = analyze_fillet_components(store, job["id"], revision_id)
        result["job_id"] = job["id"]
        return result
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "fillet component visual analysis failed: {}".format(exc))


@app.get("/api/sessions/{session_id}/fillet-components/analysis-image/{job_id}/{revision_id}")
def get_session_fillet_components_analysis_image(
    session_id: str, job_id: str, revision_id: str
):
    try:
        job = store.get_by_session(session_id)
        if job["id"] != job_id or job["accepted_revision"] != revision_id:
            raise HTTPException(409, "visual analysis no longer belongs to the current accepted revision")
        image_path = store.revision_dir(job_id, revision_id) / "fillet_components_visual_analysis.png"
        if not image_path.is_file():
            raise HTTPException(404, "fillet component analysis image has not been generated")
        return FileResponse(image_path, media_type="image/png", filename=image_path.name)
    except KeyError:
        raise HTTPException(404, "session has no bound job")


@app.post("/api/sessions/{session_id}/fillet-components/delete")
def delete_session_fillet_component(session_id: str, request: DeleteFilletComponentRequest):
    try:
        job = store.get_by_session(session_id)
        revision_id = job["accepted_revision"]
        if not revision_id:
            raise HTTPException(409, "session job has no accepted revision")
        if not store.begin_execute(job["id"], revision_id):
            raise HTTPException(409, "accepted revision is not ready or a candidate awaits review")
        result = delete_one_fillet_component(
            store,
            job["id"],
            revision_id,
            request.component_id,
            request.strategy,
            request.preference,
        )
        if result["status"] != "succeeded":
            raise HTTPException(422, detail=result)
        return result
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except ValueError as exc:
        if "job" in locals() and "revision_id" in locals():
            store.update(job["id"], "ready", revision_id, None)
        raise HTTPException(400, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "fillet component deletion failed: {}".format(exc))


@app.post("/api/sessions/{session_id}/fillet-components/delete-batch")
def delete_session_fillet_components(session_id: str, request: DeleteFilletComponentsRequest):
    try:
        job = store.get_by_session(session_id)
        revision_id = job["accepted_revision"]
        if not revision_id:
            raise HTTPException(409, "session job has no accepted revision")
        if not store.begin_execute(job["id"], revision_id):
            raise HTTPException(409, "accepted revision is not ready or a candidate awaits review")
        result = delete_fillet_components(
            store,
            job["id"],
            revision_id,
            request.component_ids,
            request.strategy,
            request.preference,
        )
        if result["status"] != "succeeded":
            raise HTTPException(422, detail=result)
        return result
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except ValueError as exc:
        if "job" in locals() and "revision_id" in locals():
            store.update(job["id"], "ready", revision_id, None)
        raise HTTPException(400, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        if "job" in locals() and "revision_id" in locals():
            store.update(job["id"], "ready", revision_id, None)
        raise HTTPException(500, "fillet component batch deletion failed: {}".format(exc))


@app.post("/api/sessions/{session_id}/fillet-components/delete-all")
def delete_all_session_fillets(session_id: str, request: DeleteAllFilletsRequest):
    try:
        job = store.get_by_session(session_id)
        revision_id = job["accepted_revision"]
        if not revision_id:
            raise HTTPException(409, "session job has no accepted revision")
        if job.get("candidate_revision") is not None or job.get("status") not in {"ready", "succeeded"}:
            raise HTTPException(409, "accepted revision is not ready or a candidate awaits review")
        return remove_all_fillets(
            store,
            job["id"],
            revision_id,
            request.preference,
        )
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "global fillet deletion failed: {}".format(exc))


@app.get("/api/sessions/{session_id}/nx-runtime/status")
def nx_runtime_status(session_id: str):
    try:
        return runtime.status(session_id)
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except RuntimeCoordinatorError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-runtime/execute")
def nx_runtime_execute(session_id: str, request: PublicRuntimeExecuteRequest):
    try:
        result = runtime.execute(session_id, request)
        if result.get("state") == "ambiguous":
            raise HTTPException(504, detail=result)
        return result
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except RuntimeCoordinatorError as exc:
        raise HTTPException(409, str(exc))
    except PersistentWorkerError as exc:
        raise HTTPException(503, str(exc))


@app.get("/api/sessions/{session_id}/nx-runtime/operations/{operation_id}")
def nx_runtime_operation(session_id: str, operation_id: str):
    try:
        return runtime.get_operation(session_id, operation_id)
    except KeyError:
        raise HTTPException(404, "runtime operation not found")


@app.post("/api/sessions/{session_id}/nx-runtime/reset")
def nx_runtime_reset(session_id: str):
    try:
        return runtime.reset(session_id)
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-cad/reanalyze")
def nx_cad_reanalyze(session_id: str):
    try:
        return runtime_cad.reanalyze(session_id)
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-cad/resolve-accepted-component")
def nx_cad_resolve_accepted_component(
    session_id: str, request: AcceptedRuntimeComponentRequest
):
    try:
        return runtime_cad.resolve_accepted_component(session_id, request.component_id)
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-cad/component-topology")
def nx_cad_component_topology(session_id: str, request: RuntimeComponentRequest):
    try:
        return runtime_cad.component_topology(session_id, request.component_id)
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-cad/face-properties")
def nx_cad_face_properties(session_id: str, request: RuntimeFaceRequest):
    try:
        return runtime_cad.face_properties(session_id, request.face_ref)
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-cad/segment-component")
def nx_cad_segment_component(session_id: str, request: RuntimeSegmentRequest):
    try:
        return runtime_cad.segment_component(session_id, request.component_id, request.method)
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-cad/try-delete-component")
def nx_cad_try_delete_component(session_id: str, request: RuntimeDeleteComponentRequest):
    try:
        return runtime_cad.try_delete_component(
            session_id, request.component_id, request.method, request.preference,
        )
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-cad/try-delete-segment")
def nx_cad_try_delete_segment(session_id: str, request: RuntimeDeleteSegmentRequest):
    try:
        return runtime_cad.try_delete_segment(
            session_id, request.segment_id, request.method, request.preference,
        )
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-cad/try-delete-face-group")
def nx_cad_try_delete_face_group(session_id: str, request: RuntimeFaceGroupRequest):
    try:
        return runtime_cad.try_delete_face_group(
            session_id, request.face_refs, request.method, request.preference,
            request.allow_related_faces,
        )
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-cad/rollback-latest-delete")
def nx_cad_rollback_latest_delete(session_id: str):
    try:
        return runtime_cad.rollback_latest_delete(session_id)
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except (RuntimeCoordinatorError, PersistentWorkerError) as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/sessions/{session_id}/nx-runtime/publish-candidate")
def nx_runtime_publish_candidate(session_id: str):
    try:
        return runtime.publish_candidate(session_id)
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except RuntimeCoordinatorError as exc:
        raise HTTPException(409, str(exc))
    except PersistentWorkerError as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:
        raise HTTPException(422, "runtime Candidate publication failed: {}".format(exc))


@app.get("/api/jobs/{job_id}/revisions")
def list_revisions(job_id: str):
    try:
        return store.revisions(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")


def _invalidate_runtime_for_job(job_id: str) -> None:
    try:
        job = store.get(job_id)
        session_id = job.get("session_id")
        if session_id:
            runtime.invalidate(session_id)
    except Exception:
        # Review decisions remain authoritative even when runtime cleanup fails;
        # the stale runtime cannot be reused because its binding is removed first.
        pass


@app.post("/api/sessions/{session_id}/revisions/rollback")
def rollback_session_revision(session_id: str, request: RevisionRollbackRequest):
    try:
        job = store.get_by_session(session_id)
        result = store.rollback_accepted(job["id"], request.target_revision)
        _invalidate_runtime_for_job(job["id"])
        return result
    except KeyError:
        raise HTTPException(404, "session has no bound job")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/jobs/{job_id}/revisions/{revision_id}/accept")
def accept_revision(job_id: str, revision_id: str):
    try:
        result = store.accept_candidate(job_id, revision_id)
        _invalidate_runtime_for_job(job_id)
        return result
    except KeyError:
        raise HTTPException(404, "job not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/jobs/{job_id}/candidate/accept")
def accept_current_candidate(job_id: str):
    try:
        job = store.get(job_id)
        revision_id = job["candidate_revision"]
        if revision_id is None:
            raise ValueError("job has no candidate revision")
        result = store.accept_candidate(job_id, revision_id)
        _invalidate_runtime_for_job(job_id)
        return result
    except KeyError:
        raise HTTPException(404, "job not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/jobs/{job_id}/revisions/{revision_id}/reject")
def reject_revision(job_id: str, revision_id: str):
    try:
        result = store.reject_candidate(job_id, revision_id)
        _invalidate_runtime_for_job(job_id)
        return result
    except KeyError:
        raise HTTPException(404, "job not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/jobs/{job_id}/candidate/reject")
def reject_current_candidate(job_id: str):
    try:
        job = store.get(job_id)
        revision_id = job["candidate_revision"]
        if revision_id is None:
            raise ValueError("job has no candidate revision")
        result = store.reject_candidate(job_id, revision_id)
        _invalidate_runtime_for_job(job_id)
        return result
    except KeyError:
        raise HTTPException(404, "job not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.put("/api/jobs/{job_id}/session")
def bind_session(job_id: str, request: SessionBindingRequest):
    try:
        return store.bind_session(job_id, request.session_id)
    except KeyError:
        raise HTTPException(404, "job not found")


@app.delete("/api/jobs/{job_id}/session")
def clear_session(job_id: str):
    try:
        return store.bind_session(job_id, None)
    except KeyError:
        raise HTTPException(404, "job not found")


@app.post("/api/jobs/{job_id}/revisions/{revision_id}/detect-fillets")
def detect_fillets(job_id: str, revision_id: str, force: bool = False):
    try:
        job = store.get(job_id)
        if revision_id != job["accepted_revision"]:
            raise HTTPException(409, "fillet detection is allowed only on the accepted revision")
        store.event(job_id, "nx_fillet_detection", "NX 正在识别当前修订版中的所有圆角面")
        payload = detect_all_fillet_faces(store, job_id, revision_id, force=force)
        store.event(job_id, "nx_fillet_detected", "NX 已识别 {} 个圆角面".format(payload["candidate_count"]))
        return payload
    except KeyError:
        raise HTTPException(404, "job not found")
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, "NX fillet detection failed: {}".format(exc))


@app.get("/api/jobs/{job_id}/revisions/{revision_id}/mesh")
def get_mesh(job_id: str, revision_id: str):
    try:
        revision_dir = store.revision_dir(job_id, revision_id)
        path = revision_dir / "mesh.json"
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "revision not found")
    if not path.is_file():
        raise HTTPException(409, "mesh is not ready")
    # Meshes created before literal Part 21 IDs were fixed contain OCC's
    # internal model ordinals. Rebuild once so JSON #face references remain exact.
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if payload.get("stepEntityIdSource") != "part21-ident-label/all-transfer-levels/v2":
        mesh = build_face_mesh(revision_dir / "model.stp", revision_id)
        store.write_json(path, mesh)
    return FileResponse(path, media_type="application/json")


@app.get("/api/jobs/{job_id}/revisions/{revision_id}/step")
def download_step(job_id: str, revision_id: str):
    try:
        path = store.revision_dir(job_id, revision_id) / "model.stp"
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "revision not found")
    return FileResponse(path, media_type="application/step", filename="{}-{}.stp".format(job_id, revision_id))


@app.post("/api/jobs/{job_id}/execute")
def execute(job_id: str, request: ExecuteRequest):
    try:
        job = store.get(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")
    if (
        job["accepted_revision"] != request.revision_id
        or job["candidate_revision"] is not None
        or job["status"] not in {"ready", "succeeded"}
    ):
        raise HTTPException(409, "requested revision is not accepted/ready or a candidate is awaiting review")
    if not store.begin_execute(job_id, request.revision_id):
        raise HTTPException(409, "requested revision is not accepted/ready or a candidate is awaiting review")
    execute_request(job_id, request)
    result = store.get(job_id)
    if result["status"] == "failed":
        raise HTTPException(500, result.get("error") or "NX execution failed")
    return result


@app.get("/api/jobs/{job_id}/event-log")
def event_log(job_id: str, after: int = 0):
    try:
        store.get(job_id)
    except KeyError:
        raise HTTPException(404, "job not found")
    return store.events(job_id, max(0, after))


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str):
    async def stream():
        sequence = 0
        while True:
            rows = store.events(job_id, sequence)
            for row in rows:
                sequence = row["seq"]
                yield "data: {}\n\n".format(json.dumps(row, ensure_ascii=False))
            try:
                status = store.get(job_id)["status"]
            except KeyError:
                yield "event: error\ndata: job not found\n\n"
                return
            if status in {"succeeded", "failed", "ready"} and sequence:
                yield "event: complete\ndata: {}\n\n".format(status)
                return
            await asyncio.sleep(0.5)
    return StreamingResponse(stream(), media_type="text/event-stream")
