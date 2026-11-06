import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from webapp.backend.app import main
from webapp.backend.app.store import JobStore


def create_ready_job(store: JobStore) -> str:
    job_id, revision_root = store.create_job()
    (revision_root / "revisions" / "r0" / "model.stp").write_text("step", encoding="utf-8")
    store.update(job_id, "ready", "r0")
    return job_id


def test_existing_database_migrates_current_revision_to_accepted(tmp_path: Path):
    database = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, current_revision TEXT,
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                created_at TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
            );
        """)
        db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?)",
            ("legacy", "ready", "r2", None, "created", "updated"),
        )
    (tmp_path / "legacy" / "revisions" / "r2").mkdir(parents=True)

    store = JobStore(tmp_path)
    job = store.get("legacy")

    assert job["accepted_revision"] == "r2"
    assert job["current_revision"] == "r2"
    assert job["candidate_revision"] is None
    assert job["session_id"] is None
    assert store.revisions("legacy")[0]["status"] == "accepted"


def test_execute_claim_is_atomic_and_keeps_current_revision(tmp_path: Path):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)

    assert store.begin_execute(job_id, "r0") is True
    assert store.begin_execute(job_id, "r0") is False
    assert store.get(job_id)["current_revision"] == "r0"


def test_candidate_must_be_explicitly_accepted_or_rejected(tmp_path: Path):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)

    candidate, _ = store.next_revision(job_id)
    store.publish_candidate(job_id, candidate)

    pending = store.get(job_id)
    assert pending["current_revision"] == "r0"
    assert pending["accepted_revision"] == "r0"
    assert pending["candidate_revision"] == candidate
    assert {row["id"]: row["status"] for row in store.revisions(job_id)} == {
        "r0": "accepted",
        candidate: "candidate",
    }

    accepted = store.accept_candidate(job_id, candidate)
    assert accepted["current_revision"] == candidate
    assert accepted["accepted_revision"] == candidate
    assert accepted["candidate_revision"] is None
    assert {row["id"]: row["status"] for row in store.revisions(job_id)} == {
        "r0": "superseded",
        candidate: "accepted",
    }

    next_candidate, _ = store.next_revision(job_id)
    store.publish_candidate(job_id, next_candidate)
    rejected = store.reject_candidate(job_id, next_candidate)
    assert rejected["current_revision"] == candidate
    assert rejected["candidate_revision"] is None
    assert store.revisions(job_id)[-1]["status"] == "rejected"


def test_rollback_accepted_to_strict_ancestor(tmp_path: Path):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)
    store.bind_session(job_id, "rollback-session")
    candidate, candidate_dir = store.next_revision(job_id)
    (candidate_dir / "model.stp").write_text("candidate", encoding="utf-8")
    store.publish_candidate(job_id, candidate)
    store.accept_candidate(job_id, candidate)
    store.set_face_selection("rollback-session", job_id, candidate, [{"face_id": "face"}])

    result = store.rollback_accepted(job_id, "r0")

    assert result["accepted_revision"] == "r0"
    assert result["current_revision"] == "r0"
    assert result["candidate_revision"] is None
    assert result["rolled_back_from"] == candidate
    assert result["rolled_back_to"] == "r0"
    statuses = {row["id"]: row["status"] for row in store.revisions(job_id)}
    assert statuses == {"r0": "accepted", candidate: "superseded"}
    assert store.get_face_selection("rollback-session")["selected_face_count"] == 0


def test_rollback_rejects_pending_candidate_and_non_ancestor(tmp_path: Path):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)
    candidate, candidate_dir = store.next_revision(job_id)
    (candidate_dir / "model.stp").write_text("candidate", encoding="utf-8")
    store.publish_candidate(job_id, candidate)
    try:
        store.rollback_accepted(job_id, "r0")
        assert False, "expected pending candidate rejection"
    except ValueError as exc:
        assert "candidate awaits review" in str(exc)
    store.reject_candidate(job_id, candidate)

    orphan, orphan_dir = store.next_revision(job_id, parent_revision="r9")
    (orphan_dir / "model.stp").write_text("orphan", encoding="utf-8")
    store.set_revision_status(job_id, orphan, "superseded")
    try:
        store.rollback_accepted(job_id, orphan)
        assert False, "expected non-ancestor rejection"
    except ValueError as exc:
        assert "not an ancestor" in str(exc)


def test_session_rollback_route_is_bound_and_invalidates_runtime(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)
    store.bind_session(job_id, "rollback-session")
    candidate, candidate_dir = store.next_revision(job_id)
    (candidate_dir / "model.stp").write_text("candidate", encoding="utf-8")
    store.publish_candidate(job_id, candidate)
    store.accept_candidate(job_id, candidate)

    class Runtime:
        invalidated = []

        def invalidate(self, session_id):
            self.invalidated.append(session_id)

    runtime = Runtime()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "runtime", runtime)
    response = TestClient(main.app).post(
        "/api/sessions/rollback-session/revisions/rollback",
        json={"target_revision": "r0"},
    )
    assert response.status_code == 200
    assert response.json()["accepted_revision"] == "r0"
    assert runtime.invalidated == ["rollback-session"]


def test_revision_and_session_apis(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)
    candidate, _ = store.next_revision(job_id)
    store.publish_candidate(job_id, candidate)
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)

    response = client.get(f"/api/jobs/{job_id}/revisions")
    assert response.status_code == 200
    assert response.json()[-1] == {
        "id": candidate,
        "parent_revision": "r0",
        "status": "candidate",
        "created_at": response.json()[-1]["created_at"],
        "updated_at": response.json()[-1]["updated_at"],
        "error": None,
        "is_accepted": False,
        "is_candidate": True,
    }

    response = client.put(f"/api/jobs/{job_id}/session", json={"session_id": " dsh-session-1 "})
    assert response.status_code == 200
    assert response.json()["session_id"] == "dsh-session-1"
    assert client.get(f"/api/jobs/{job_id}").json()["session_id"] == "dsh-session-1"
    assert client.get("/api/sessions/dsh-session-1/job").json()["id"] == job_id

    response = client.post(f"/api/jobs/{job_id}/revisions/{candidate}/accept")
    assert response.status_code == 200
    assert response.json()["accepted_revision"] == candidate

    assert client.delete(f"/api/jobs/{job_id}/session").json()["session_id"] is None


def test_requested_session_upload_and_recovery_contract(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    monkeypatch.setattr(main, "store", store)

    def prepared(job_id, revision_id):
        store.write_json(
            store.revision_dir(job_id, revision_id) / "mesh.json",
            {"revisionId": revision_id, "faceCount": 0, "faces": []},
        )
        store.update(job_id, "ready", revision_id)

    monkeypatch.setattr(main, "prepare_revision", prepared)
    client = TestClient(main.app)

    response = client.post(
        "/api/sessions/dsh-session-2/jobs",
        files={"file": ("part.step", b"ISO-10303-21", "application/step")},
    )
    assert response.status_code == 200

    recovered = client.get("/api/sessions/dsh-session-2/job")
    assert recovered.status_code == 200
    assert recovered.json()["id"] == response.json()["jobId"]
    assert recovered.json()["accepted_revision"] == "r0"
    assert recovered.json()["candidate_revision"] is None
    assert recovered.json()["current_revision"] == "r0"

    replacement = client.post(
        "/api/sessions/dsh-session-2/jobs",
        files={"file": ("replacement.step", b"ISO-10303-21 replacement", "application/step")},
    )
    assert replacement.status_code == 200
    assert replacement.json()["jobId"] != response.json()["jobId"]
    assert client.get("/api/sessions/dsh-session-2/job").json()["id"] == replacement.json()["jobId"]
    assert client.get(f"/api/jobs/{response.json()['jobId']}").json()["session_id"] is None


def test_current_candidate_routes_and_dsh_cors(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)
    candidate, _ = store.next_revision(job_id)
    store.publish_candidate(job_id, candidate)
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)

    response = client.options(
        f"/api/jobs/{job_id}/candidate/accept",
        headers={
            "Origin": "http://127.0.0.1:3080",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3080"

    response = client.post(f"/api/jobs/{job_id}/candidate/reject")
    assert response.status_code == 200
    assert response.json()["accepted_revision"] == "r0"
    assert response.json()["current_revision"] == "r0"
    assert response.json()["candidate_revision"] is None

    next_candidate, _ = store.next_revision(job_id)
    store.publish_candidate(job_id, next_candidate)
    response = client.post(f"/api/jobs/{job_id}/candidate/accept")
    assert response.status_code == 200
    assert response.json()["accepted_revision"] == next_candidate
    assert response.json()["current_revision"] == next_candidate
    assert response.json()["candidate_revision"] is None

    localhost = client.options(
        f"/api/jobs/{job_id}/candidate/accept",
        headers={
            "Origin": "http://localhost:3080",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert localhost.headers["access-control-allow-origin"] == "http://localhost:3080"


def test_candidate_decision_invalidates_bound_runtime(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)
    store.bind_session(job_id, "runtime-session")
    candidate, _ = store.next_revision(job_id)
    store.publish_candidate(job_id, candidate)

    class Runtime:
        invalidated = []

        def invalidate(self, session_id):
            self.invalidated.append(session_id)

    runtime = Runtime()
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "runtime", runtime)
    client = TestClient(main.app)

    response = client.post(f"/api/jobs/{job_id}/candidate/reject")
    assert response.status_code == 200
    assert runtime.invalidated == ["runtime-session"]


def test_runtime_status_route_is_session_bound(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)
    store.bind_session(job_id, "runtime-session")

    class Runtime:
        def status(self, session_id):
            assert session_id == "runtime-session"
            return {"connected": False, "status": "offline", "runtime_state": 0}

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "runtime", Runtime())
    client = TestClient(main.app)
    response = client.get("/api/sessions/runtime-session/nx-runtime/status")
    assert response.status_code == 200
    assert response.json()["runtime_state"] == 0


def test_execute_returns_candidate_without_advancing_current_revision(tmp_path: Path, monkeypatch):
    store = JobStore(tmp_path)
    job_id = create_ready_job(store)
    monkeypatch.setattr(main, "store", store)

    def successful_execute(job_id_arg, request):
        candidate, _ = store.next_revision(job_id_arg)
        store.publish_candidate(job_id_arg, candidate)

    monkeypatch.setattr(main, "execute_request", successful_execute)
    client = TestClient(main.app)
    response = client.post(
        f"/api/jobs/{job_id}/execute",
        json={
            "revision_id": "r0",
            "selected_faces": [{"face_id": "r0:face:1", "face_index": 1, "pick_point": [0, 0, 0]}],
            "prompt": "delete fillet",
        },
    )

    assert response.status_code == 200
    assert response.json()["current_revision"] == "r0"
    assert response.json()["accepted_revision"] == "r0"
    assert response.json()["candidate_revision"] == "r1"
    assert client.post(f"/api/jobs/{job_id}/execute", json={
        "revision_id": "r0",
        "selected_faces": [{"face_id": "r0:face:1", "face_index": 1, "pick_point": [0, 0, 0]}],
        "prompt": "again",
    }).status_code == 409
