from __future__ import annotations

import json
from pathlib import Path
import re

from .geometry import build_face_mesh
from .nx_worker import classify_fillet_faces
from .store import JobStore

SCHEMA = "nx_web_all_fillet_faces_v3"
FACE_PATTERN = re.compile(rb"(#\s*(\d+)\s*=\s*ADVANCED_FACE\s*\(\s*)'((?:[^']|'')*)'", re.IGNORECASE | re.DOTALL)


def inject_step_face_names(source: Path, destination: Path) -> int:
    data = source.read_bytes()
    chunks: list[bytes] = []
    cursor = 0
    count = 0
    for match in FACE_PATTERN.finditer(data):
        step_id = int(match.group(2))
        chunks.append(data[cursor:match.start(3)])
        chunks.append("DSH_STEP_FACE_{}".format(step_id).encode("ascii"))
        cursor = match.end(3)
        count += 1
    chunks.append(data[cursor:])
    if count == 0:
        raise ValueError("STEP contains no ADVANCED_FACE entities")
    destination.write_bytes(b"".join(chunks))
    return count


def detect_all_fillet_faces(store: JobStore, job_id: str, revision_id: str, force: bool = False) -> dict:
    revision_dir = store.revision_dir(job_id, revision_id)
    cache = revision_dir / "nx_all_fillet_faces.json"
    if cache.is_file() and not force:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if payload.get("schema") == SCHEMA:
            return payload

    step_path = revision_dir / "model.stp"
    report_path = revision_dir / "nx_fillet_candidates_raw.json"
    log_path = revision_dir / "nx_fillet_detection.log"
    tagged_step = revision_dir / "nx_fillet_tagged_input.stp"
    inject_step_face_names(step_path, tagged_step)
    classify_fillet_faces(tagged_step, report_path, log_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("mapping_complete"):
        raise RuntimeError("NX did not preserve every injected STEP face name")
    rows = report.get("faces") or []
    selected = [row for row in rows if row.get("selectable")]
    ids = sorted({int(row["step_entity_id"]) for row in selected if row.get("step_entity_id")})
    engine = "NXOpen SolidFaceType curved-face rule (Cylinder/Sphere/Torus/B-Surface)"

    mesh_path = revision_dir / "mesh.json"
    mesh = json.loads(mesh_path.read_text(encoding="utf-8"))
    if mesh.get("stepEntityIdSource") != "part21-ident-label/all-transfer-levels/v2":
        mesh = build_face_mesh(step_path, revision_id)
        store.write_json(mesh_path, mesh)
    available = {int(face["stepEntityId"]) for face in mesh["faces"] if face.get("stepEntityId")}
    missing = sorted(set(ids) - available)
    if missing:
        raise RuntimeError("NX fillet report contains unknown STEP face IDs: {}".format(missing[:20]))

    payload = {
        "schema": SCHEMA,
        "revision_id": revision_id,
        "engine": engine,
        "candidate_count": len(ids),
        "step_entity_ids": ids,
        "faces": selected,
    }
    store.write_json(cache, payload)
    return payload
