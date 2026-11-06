import json
from pathlib import Path

from fastapi.testclient import TestClient

from webapp.backend.app import fillet_components, main
from webapp.backend.app.store import JobStore


def ready_session_job(store: JobStore, session_id: str = "session-components") -> tuple[str, Path]:
    job_id, job_root = store.create_job(session_id=session_id)
    revision_dir = job_root / "revisions" / "r0"
    (revision_dir / "model.stp").write_text("#10=ADVANCED_FACE('',(),$,.T.);", encoding="ascii")
    store.write_json(revision_dir / "mesh.json", {
        "revisionId": "r0",
        "stepEntityIdSource": "part21-ident-label/all-transfer-levels/v2",
        "faces": [
            {"stepEntityId": 10},
            {"stepEntityId": 20},
            {"stepEntityId": 30},
        ],
    })
    store.update(job_id, "ready", "r0")
    store.bind_session(job_id, session_id)
    return job_id, revision_dir


def test_component_service_validates_and_caches_nx_report(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, revision_dir = ready_session_job(store)
    calls = []

    def fake_nx(step_path, report_path, log_path):
        calls.append(step_path)
        report_path.write_text(json.dumps({
            "schema": "nx_fillet_components_v1",
            "success": True,
            "mapping_complete": True,
            "engine": "NXOpen SolidFaceType + shared NX topology edges",
            "candidate_face_count": 3,
            "components": [
                {"component_id": "fc-001", "face_count": 2, "step_entity_ids": [10, 20]},
                {"component_id": "fc-002", "face_count": 1, "step_entity_ids": [30]},
            ],
        }), encoding="utf-8")

    monkeypatch.setattr(fillet_components, "run_nx_component_detection", fake_nx)
    first = fillet_components.detect_fillet_components(store, job_id, "r0")
    second = fillet_components.detect_fillet_components(store, job_id, "r0")

    assert first == second
    assert first["candidate_face_count"] == 3
    assert first["component_count"] == 2
    assert len(calls) == 1
    assert "DSH_STEP_FACE_10" in (revision_dir / "nx_fillet_components_tagged_input.stp").read_text()


def test_session_component_route_uses_bound_accepted_revision(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, _ = ready_session_job(store, "tool-session")
    monkeypatch.setattr(main, "store", store)
    seen = {}

    def fake_detect(store_arg, job_id_arg, revision_id_arg, force=False):
        seen.update(job_id=job_id_arg, revision_id=revision_id_arg, force=force)
        return {
            "schema": "nx_fillet_components_v1",
            "revision_id": revision_id_arg,
            "candidate_face_count": 3,
            "component_count": 2,
            "components": [],
        }

    monkeypatch.setattr(main, "detect_fillet_components", fake_detect)
    response = TestClient(main.app).post(
        "/api/sessions/tool-session/fillet-components/detect?force=true"
    )
    assert response.status_code == 200
    assert seen == {"job_id": job_id, "revision_id": "r0", "force": True}
    assert response.json()["component_count"] == 2


def test_component_analysis_and_delete_session_routes(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, revision_dir = ready_session_job(store, "delete-session")
    monkeypatch.setattr(main, "store", store)
    image_path = revision_dir / "fillet_components_visual_analysis.png"
    image_path.write_bytes(b"png")
    monkeypatch.setattr(main, "analyze_fillet_components", lambda *args: {
        "schema": "fillet_components_visual_analysis_v1",
        "status": "ready_for_visual_analysis",
        "revision_id": "r0",
        "component_count": 2,
        "legend": [
            {"component_id": "fc-001", "color": "#ef4444"},
            {"component_id": "fc-002", "color": "#3b82f6"},
        ],
        "image_name": image_path.name,
    })
    seen = {}

    def fake_delete(store_arg, job_arg, revision_arg, component_id, strategy, preference):
        seen.update(job=job_arg, revision=revision_arg, component=component_id,
                    strategy=strategy, preference=preference)
        return {"status": "succeeded", "candidate_revision": "r1"}

    monkeypatch.setattr(main, "delete_one_fillet_component", fake_delete)
    client = TestClient(main.app)
    analysis = client.post("/api/sessions/delete-session/fillet-components/analyze")
    assert analysis.status_code == 200
    assert analysis.json()["component_count"] == 2
    assert analysis.json()["job_id"] == job_id
    image = client.get("/api/sessions/delete-session/fillet-components/analysis-image/{}/r0".format(job_id))
    assert image.status_code == 200
    assert image.content == b"png"
    deletion = client.post("/api/sessions/delete-session/fillet-components/delete", json={
        "component_id": "fc-001",
        "strategy": "whole-component-face-edge-blend",
        "preference": "cliff",
    })
    assert deletion.status_code == 200
    assert seen == {
        "job": job_id, "revision": "r0", "component": "fc-001",
        "strategy": "whole-component-face-edge-blend", "preference": "cliff",
    }


def test_delete_service_publishes_only_validated_candidate(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, revision_dir = ready_session_job(store, "service-delete")
    store.write_json(revision_dir / "nx_fillet_components.json", {
        "schema": "nx_fillet_components_v1", "revision_id": "r0",
        "candidate_face_count": 1, "component_count": 1,
        "components": [{"component_id": "fc-001", "face_count": 1,
                        "step_entity_ids": [10], "blend_radii": [1.0],
                        "non_blend_face_count": 0}],
    })

    def fake_nx(request_path, report_path, log_path):
        request = json.loads(request_path.read_text(encoding="utf-8"))
        Path(request["output"]).write_text("candidate", encoding="utf-8")
        report_path.write_text(json.dumps({
            "schema": "nx_fillet_components_delete_result_v1",
            "success": True,
            "component_ids": ["fc-001"],
            "strategy": "whole-component-face-edge-blend",
            "deleted_step_entity_ids": [10],
        }), encoding="utf-8")

    monkeypatch.setattr(fillet_components, "run_nx_component_delete", fake_nx)
    monkeypatch.setattr(fillet_components, "build_face_mesh", lambda path, revision: {
        "revisionId": revision, "faceCount": 2,
        "stepEntityIdSource": "part21-ident-label/all-transfer-levels/v2", "faces": [],
    })
    result = fillet_components.delete_one_fillet_component(
        store, job_id, "r0", "fc-001", "whole-component-face-edge-blend", "cliff"
    )
    assert result["status"] == "succeeded"
    assert store.get(job_id)["accepted_revision"] == "r0"
    assert store.get(job_id)["candidate_revision"] == "r1"


def test_batch_delete_unions_exact_component_faces_and_calls_nx_once(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, revision_dir = ready_session_job(store, "batch-service")
    store.write_json(revision_dir / "nx_fillet_components.json", {
        "schema": "nx_fillet_components_v1", "revision_id": "r0",
        "candidate_face_count": 3, "component_count": 2,
        "components": [
            {"component_id": "fc-001", "face_count": 2, "step_entity_ids": [20, 10]},
            {"component_id": "fc-002", "face_count": 1, "step_entity_ids": [30]},
        ],
    })
    calls = []

    def fake_nx(request_path, report_path, log_path):
        request = json.loads(request_path.read_text(encoding="utf-8"))
        calls.append(request)
        Path(request["output"]).write_text("candidate", encoding="utf-8")
        report_path.write_text(json.dumps({
            "schema": "nx_fillet_components_delete_result_v1",
            "success": True,
            "component_ids": request["component_ids"],
            "strategy": request["strategy"],
            "deleted_step_entity_ids": request["step_entity_ids"],
        }), encoding="utf-8")

    monkeypatch.setattr(fillet_components, "run_nx_component_delete", fake_nx)
    monkeypatch.setattr(fillet_components, "build_face_mesh", lambda path, revision: {
        "revisionId": revision, "faceCount": 1, "faces": [],
    })
    result = fillet_components.delete_fillet_components(
        store, job_id, "r0", ["fc-002", "fc-001"],
        "whole-component-face-edge-blend", "cliff",
    )

    assert result["status"] == "succeeded"
    assert result["component_ids"] == ["fc-002", "fc-001"]
    assert result["deleted_step_entity_ids"] == [10, 20, 30]
    assert len(calls) == 1
    assert calls[0]["schema"] == "nx_fillet_components_delete_v1"
    assert calls[0]["component_ids"] == ["fc-002", "fc-001"]
    assert calls[0]["step_entity_ids"] == [10, 20, 30]
    assert "delete_partial_blend" not in calls[0]
    job = store.get(job_id)
    assert job["accepted_revision"] == "r0"
    assert job["candidate_revision"] == "r1"


def test_batch_delete_rejects_unknown_before_revision_or_nx(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, revision_dir = ready_session_job(store, "batch-unknown")
    store.write_json(revision_dir / "nx_fillet_components.json", {
        "schema": "nx_fillet_components_v1", "revision_id": "r0",
        "candidate_face_count": 1, "component_count": 1,
        "components": [{"component_id": "fc-001", "step_entity_ids": [10]}],
    })
    called = False

    def fake_nx(*args):
        nonlocal called
        called = True

    monkeypatch.setattr(fillet_components, "run_nx_component_delete", fake_nx)
    try:
        fillet_components.delete_fillet_components(
            store, job_id, "r0", ["fc-999"],
            "whole-component-face-edge-blend", "cliff",
        )
        assert False, "expected membership validation failure"
    except ValueError as exc:
        assert "current accepted revision" in str(exc)
    assert called is False
    assert [row["id"] for row in store.revisions(job_id)] == ["r0"]
    assert store.get(job_id)["candidate_revision"] is None


def test_batch_validation_failure_preserves_accepted_and_no_candidate(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, revision_dir = ready_session_job(store, "batch-validation")
    store.write_json(revision_dir / "nx_fillet_components.json", {
        "schema": "nx_fillet_components_v1", "revision_id": "r0",
        "candidate_face_count": 2, "component_count": 2,
        "components": [
            {"component_id": "fc-001", "step_entity_ids": [10]},
            {"component_id": "fc-002", "step_entity_ids": [20]},
        ],
    })

    def fake_nx(request_path, report_path, log_path):
        request = json.loads(request_path.read_text(encoding="utf-8"))
        Path(request["output"]).write_text("invalid candidate", encoding="utf-8")
        report_path.write_text(json.dumps({
            "schema": "nx_fillet_components_delete_result_v1",
            "success": True,
            "component_ids": request["component_ids"],
            "strategy": request["strategy"],
            "deleted_step_entity_ids": [999],
        }), encoding="utf-8")

    monkeypatch.setattr(fillet_components, "run_nx_component_delete", fake_nx)
    result = fillet_components.delete_fillet_components(
        store, job_id, "r0", ["fc-001", "fc-002"],
        "whole-component-face-edge-blend", "cliff",
    )
    assert result["status"] == "failed"
    assert result["failure_category"] == "validation"
    assert result["candidate_revision"] is None
    assert store.get(job_id)["accepted_revision"] == "r0"
    assert store.get(job_id)["candidate_revision"] is None
    assert not (store.revision_dir(job_id, "r1") / "model.stp").exists()


def test_batch_route_and_request_validation(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, _ = ready_session_job(store, "batch-route")
    monkeypatch.setattr(main, "store", store)
    seen = {}

    def fake_delete(store_arg, job_arg, revision_arg, component_ids, strategy, preference):
        seen.update(job=job_arg, revision=revision_arg, components=component_ids)
        return {"status": "succeeded", "candidate_revision": "r1"}

    monkeypatch.setattr(main, "delete_fillet_components", fake_delete)
    client = TestClient(main.app)
    duplicate = client.post("/api/sessions/batch-route/fillet-components/delete-batch", json={
        "component_ids": ["fc-001", "fc-001"],
    })
    assert duplicate.status_code == 422
    empty = client.post("/api/sessions/batch-route/fillet-components/delete-batch", json={
        "component_ids": [],
    })
    assert empty.status_code == 422
    response = client.post("/api/sessions/batch-route/fillet-components/delete-batch", json={
        "component_ids": ["fc-002", "fc-001"],
    })
    assert response.status_code == 200
    assert seen == {"job": job_id, "revision": "r0", "components": ["fc-002", "fc-001"]}


def legacy_test_remove_all_fillets_bisects_inside_one_service_call(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, _ = ready_session_job(store, "remove-all")
    def fake_detect(store_arg, job_arg, revision_arg, **kwargs):
        if revision_arg == "r0":
            return {"components": [
                {"component_id": "fc-001"},
                {"component_id": "fc-002"},
                {"component_id": "fc-003"},
                {"component_id": "fc-004"},
            ]}
        return {"components": []}

    monkeypatch.setattr(fillet_components, "detect_fillet_components", fake_detect)
    attempts = []

    def fake_delete(store_arg, job_arg, revision_arg, component_ids, *args, **kwargs):
        attempts.append(list(component_ids))
        store_arg.update(job_arg, "ready", "r0", None)
        if component_ids == ["fc-002"]:
            return {"status": "succeeded", "candidate_revision": "r9"}
        return {"status": "failed", "candidate_revision": None,
                "failure_category": "geometry", "error": "simulated"}

    monkeypatch.setattr(fillet_components, "delete_fillet_components", fake_delete)
    monkeypatch.setattr(store, "publish_candidate", lambda job, revision: None)
    result = fillet_components.remove_all_fillets(store, job_id, "r0", "cliff")
    assert attempts == [
        ["fc-001", "fc-002", "fc-003", "fc-004"],
        ["fc-001", "fc-002"],
        ["fc-001"],
        ["fc-002"],
    ]
    assert result["status"] == "succeeded"
    assert result["successful_component_ids"] == [
        {"source_revision": "r0", "component_id": "fc-002"},
    ]
    assert result["candidate_revision"] == "r9"
    assert len(result["attempts"]) == 4


def legacy_test_remove_all_fillets_continues_after_success_until_exhausted(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, _ = ready_session_job(store, "remove-all-rounds")
    detections = {
        "r0": ["fc-001", "fc-002"],
        "r1": ["fc-001"],
        "r2": ["fc-001"],
    }
    monkeypatch.setattr(fillet_components, "detect_fillet_components", lambda s, j, revision, **kw: {
        "components": [{"component_id": value} for value in detections[revision]],
    })
    calls = []

    def fake_delete(store_arg, job_arg, revision_arg, component_ids, *args, **kwargs):
        calls.append((revision_arg, list(component_ids)))
        if revision_arg == "r0":
            return {"status": "succeeded", "candidate_revision": "r1"}
        if revision_arg == "r1":
            return {"status": "succeeded", "candidate_revision": "r2"}
        return {"status": "failed", "candidate_revision": None,
                "failure_category": "geometry", "error": "cannot delete"}

    monkeypatch.setattr(fillet_components, "delete_fillet_components", fake_delete)
    monkeypatch.setattr(store, "publish_candidate", lambda job, revision: None)
    result = fillet_components.remove_all_fillets(store, job_id, "r0", "cliff")
    assert calls == [
        ("r0", ["fc-001", "fc-002"]),
        ("r1", ["fc-001"]),
        ("r2", ["fc-001"]),
    ]
    assert result["candidate_revision"] == "r2"
    assert [batch["result_revision"] for batch in result["successful_batches"]] == ["r1", "r2"]
    assert result["rounds"][-1]["terminal"] == "no_deletable_components"


def legacy_test_remove_all_fillets_stops_on_non_geometry_failure(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, _ = ready_session_job(store, "remove-all-stop")
    monkeypatch.setattr(fillet_components, "detect_fillet_components", lambda *args, **kwargs: {
        "components": [{"component_id": "fc-001"}, {"component_id": "fc-002"}],
    })
    calls = []

    def fake_delete(store_arg, job_arg, revision_arg, component_ids, *args, **kwargs):
        calls.append(list(component_ids))
        store_arg.update(job_arg, "ready", revision_arg, None)
        return {"status": "failed", "candidate_revision": None,
                "failure_category": "infrastructure", "error": "license"}

    monkeypatch.setattr(fillet_components, "delete_fillet_components", fake_delete)
    result = fillet_components.remove_all_fillets(store, job_id, "r0", "cliff")
    assert calls == [["fc-001", "fc-002"]]
    assert result["status"] == "aborted"
    assert result["failure_category"] == "infrastructure"
    assert result["all_failed"] is False


def test_remove_all_fillets_uses_one_nx_run_and_one_occ_build(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, _ = ready_session_job(store, "remove-all-single-nx")
    nx_calls = []
    nx_requests = []
    occ_calls = []

    def fake_nx(request_path, report_path, log_path):
        nx_calls.append((request_path, report_path, log_path))
        request = json.loads(request_path.read_text(encoding="utf-8"))
        nx_requests.append(request)
        Path(request["output"]).write_text("final step", encoding="ascii")
        Path(request["report"]).write_text(json.dumps({
            "schema": "nx_remove_all_fillets_result_v2",
            "success": True,
            "terminal_reason": "no_deletable_components",
            "rounds": [{"round": 1, "components": [], "attempts": []}],
            "attempts": [
                {"attempt": 1, "round": 1, "component_ids": ["round-001-fc-001", "round-001-fc-002"], "status": "geometry_failure", "error": "failed"},
                {"attempt": 2, "round": 1, "component_ids": ["round-001-fc-001"], "status": "succeeded"},
            ],
            "successful_batches": [{"round": 1, "attempt": 2, "component_ids": ["round-001-fc-001"], "requested_step_entity_ids": [10], "deleted_step_entity_ids": [10]}],
            "deleted_step_entity_ids": [10],
            "roundtrip_normalization": {
                "performed": True,
                "same_process": True,
                "scope": "final-accumulated-result",
                "reimport_count": 1,
                "pre_roundtrip_step_face_count": 3,
                "nx_roundtrip_face_count": 2,
                "normalized_output_step_face_count": 2,
                "faces_merged_or_removed_on_reimport": 1,
            },
            "output": request["output"],
        }), encoding="utf-8")

    def fake_mesh(path, revision):
        occ_calls.append((path, revision))
        return {"revisionId": revision, "faceCount": 2, "faces": []}

    monkeypatch.setattr(fillet_components, "run_nx_remove_all_fillets", fake_nx)
    monkeypatch.setattr(fillet_components, "build_face_mesh", fake_mesh)
    result = fillet_components.remove_all_fillets(store, job_id, "r0", "cliff")
    assert len(nx_calls) == 1
    assert "delete_partial_blend" not in nx_requests[0]
    assert len(occ_calls) == 1
    assert result["status"] == "succeeded"
    assert result["candidate_revision"] == "r1"
    assert result["roundtrip_normalization"]["reimport_count"] == 1
    assert result["roundtrip_normalization"]["scope"] == "final-accumulated-result"
    job = store.get(job_id)
    assert job["accepted_revision"] == "r0"
    assert job["candidate_revision"] == "r1"
    assert [item["id"] for item in store.revisions(job_id)] == ["r0", "r1"]


def test_remove_all_journal_normalizes_only_after_accumulation():
    source = (Path(__file__).resolve().parents[3] / "cad" / "Defillet" / "nx_web_remove_all_fillets.py").read_text(encoding="utf-8")
    assert source.count("final_roundtrip_normalize(") == 2  # definition plus one call
    call_offset = source.index("display_part, normalization = final_roundtrip_normalize(")
    deletion_loop_offset = source.index("while True:")
    assert call_offset > deletion_loop_offset
    assert '"scope": "final-accumulated-result"' in source
    assert '"reimport_count": 1' in source


def test_remove_all_fillets_rejects_missing_final_roundtrip_normalization(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, _ = ready_session_job(store, "remove-all-no-normalization")
    occ_calls = []

    def fake_nx(request_path, report_path, log_path):
        request = json.loads(request_path.read_text(encoding="utf-8"))
        Path(request["output"]).write_text("unnormalized step", encoding="ascii")
        Path(request["report"]).write_text(json.dumps({
            "schema": "nx_remove_all_fillets_result_v2",
            "success": True,
            "terminal_reason": "no_deletable_components",
            "rounds": [],
            "attempts": [{
                "attempt": 1, "round": 1,
                "component_ids": ["round-001-fc-001"], "status": "succeeded",
            }],
            "successful_batches": [{
                "round": 1, "attempt": 1,
                "component_ids": ["round-001-fc-001"],
                "requested_step_entity_ids": [10],
                "deleted_step_entity_ids": [10],
            }],
            "deleted_step_entity_ids": [10],
            "output": request["output"],
        }), encoding="utf-8")

    monkeypatch.setattr(fillet_components, "run_nx_remove_all_fillets", fake_nx)
    monkeypatch.setattr(fillet_components, "build_face_mesh", lambda *args: occ_calls.append(args))
    result = fillet_components.remove_all_fillets(store, job_id, "r0", "cliff")

    assert result["status"] == "aborted"
    assert result["failure_category"] == "validation"
    assert "exactly once" in result["error"]
    assert result["candidate_revision"] is None
    assert occ_calls == []
    assert store.get(job_id)["candidate_revision"] is None


def test_remove_all_fillets_noop_skips_occ_and_revision(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, _ = ready_session_job(store, "remove-all-noop")
    occ_calls = []

    def fake_nx(request_path, report_path, log_path):
        request = json.loads(request_path.read_text(encoding="utf-8"))
        Path(request["report"]).write_text(json.dumps({
            "schema": "nx_remove_all_fillets_result_v2",
            "success": False,
            "terminal_reason": "no_deletable_components",
            "rounds": [{"round": 1, "components": [{"component_id": "round-001-fc-001"}], "attempts": []}],
            "attempts": [{"attempt": 1, "round": 1, "component_ids": ["round-001-fc-001"], "status": "geometry_failure", "error": "failed"}],
            "successful_batches": [],
            "deleted_step_entity_ids": [],
        }), encoding="utf-8")

    monkeypatch.setattr(fillet_components, "run_nx_remove_all_fillets", fake_nx)
    monkeypatch.setattr(fillet_components, "build_face_mesh", lambda *args: occ_calls.append(args))
    result = fillet_components.remove_all_fillets(store, job_id, "r0", "cliff")
    assert result["status"] == "no_deletable_components"
    assert result["all_failed"] is True
    assert occ_calls == []
    assert [item["id"] for item in store.revisions(job_id)] == ["r0"]
    assert store.get(job_id)["candidate_revision"] is None


def test_session_selection_round_trip_is_revision_bound(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, revision_dir = ready_session_job(store, "selection-route")
    monkeypatch.setattr(main, "store", store)
    mesh = json.loads((revision_dir / "mesh.json").read_text(encoding="utf-8"))
    mesh["faces"][0].update(faceId="r0:face:1", faceIndex=1, stepEntityId=10)
    store.write_json(revision_dir / "mesh.json", mesh)
    client = TestClient(main.app)
    response = client.put("/api/sessions/selection-route/selection", json={
        "revision_id": "r0",
        "selected_faces": [{
            "face_id": "r0:face:1",
            "face_index": 1,
            "step_entity_id": 10,
            "pick_point": [1, 2, 3],
        }],
    })
    assert response.status_code == 200
    payload = client.get("/api/sessions/selection-route/selection").json()
    assert payload["job_id"] == job_id
    assert payload["revision_id"] == "r0"
    assert payload["selected_face_count"] == 1
    assert payload["selected_faces"][0]["face_id"] == "r0:face:1"


def test_remove_all_route_is_session_bound(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id, _ = ready_session_job(store, "remove-all-route")
    monkeypatch.setattr(main, "store", store)
    seen = {}

    def fake_remove(store_arg, job_arg, revision_arg, preference):
        seen.update(job=job_arg, revision=revision_arg, preference=preference)
        return {"status": "no_components", "all_failed": False}

    monkeypatch.setattr(main, "remove_all_fillets", fake_remove)
    response = TestClient(main.app).post(
        "/api/sessions/remove-all-route/fillet-components/delete-all",
        json={"preference": "notch"},
    )
    assert response.status_code == 200
    assert seen == {"job": job_id, "revision": "r0", "preference": "notch"}


def test_batch_route_exposes_structured_failure_categories(tmp_path: Path, monkeypatch):
    for category in ("geometry", "infrastructure"):
        store = JobStore(tmp_path / category)
        ready_session_job(store, "batch-{}".format(category))
        monkeypatch.setattr(main, "store", store)
        failure = {
            "schema": "fillet_components_delete_result_v1",
            "status": "failed",
            "source_revision": "r0",
            "candidate_revision": None,
            "component_ids": ["fc-001", "fc-002"],
            "strategy": "whole-component-face-edge-blend",
            "failure_category": category,
            "error": "simulated {} failure".format(category),
            "rollback": True,
            "allowed_next_strategies": [],
        }
        monkeypatch.setattr(main, "delete_fillet_components", lambda *args, result=failure: result)

        response = TestClient(main.app).post(
            "/api/sessions/batch-{}/fillet-components/delete-batch".format(category),
            json={"component_ids": ["fc-001", "fc-002"]},
        )

        assert response.status_code == 422
        assert response.json() == {"detail": failure}
        assert response.json()["detail"]["failure_category"] == category
