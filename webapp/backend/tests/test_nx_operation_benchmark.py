from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from benchmark_nx_persistent_operations import (
    DISCOVER_CODE,
    build_plans,
    deletion_code,
    percentile,
    summarize,
)


def test_deletion_code_encodes_face_and_fillet_modes():
    face = deletion_code([10, 20], "Face", "Cliff")
    fillet = deletion_code([30], "FaceEdgeBlend", "Notch")
    assert "set([10, 20])" in face
    assert "SelectTypes.Face" in face
    assert "SelectTypes.FaceEdgeBlend" in fillet
    assert "PreferenceOptions.Notch" in fillet
    assert "DeletePartialBlend = False" in fillet


def test_discovery_normalizes_nx_numeric_face_types():
    assert "'1': 'Planar'" in DISCOVER_CODE
    assert "'2': 'Cylindrical'" in DISCOVER_CODE
    assert "curved_components" in DISCOVER_CODE


def test_build_plans_samples_distinct_groups_reproducibly():
    faces = (
        [{"tag": tag, "face_type": "Cylindrical"} for tag in range(1, 21)]
        + [{"tag": tag, "face_type": "Planar"} for tag in range(101, 121)]
    )
    components = [[tag] for tag in range(1, 21)]
    plans = build_plans(faces, components, 3, sample_count=10, seed=7)
    assert plans == build_plans(faces, components, 3, sample_count=10, seed=7)
    grouped = {}
    for name, tags, _select, _preference, sample_index in plans:
        grouped.setdefault(name, []).append((sample_index, tuple(tags)))
    assert len(grouped["fillet_three_plus_faces"]) == 10
    assert len(set(tags for _index, tags in grouped["fillet_three_plus_faces"])) == 10
    assert all(len(tags) == 3 for _index, tags in grouped["generic_three_plus_faces"])
    assert len(set(tags for _index, tags in grouped["fillet_single_component"])) == 10


def test_summary_counts_success_failure_and_latency():
    rows = [
        {"case": "one", "state": "rolled_back", "success": True, "deletion_seconds": 0.1, "end_to_end_seconds": 1.0},
        {"case": "one", "state": "rolled_back", "success": False, "deletion_seconds": 0.3, "end_to_end_seconds": 3.0},
        {"case": "two", "state": "rolled_back", "success": True, "deletion_seconds": 0.2, "end_to_end_seconds": 2.0},
    ]
    summary = {row["case"]: row for row in summarize(rows)}
    assert summary["one"]["attempts"] == 2
    assert summary["one"]["successes"] == 1
    assert summary["one"]["failures"] == 1
    assert summary["one"]["mean_deletion_seconds"] == 0.2
    assert summary["one"]["mean_end_to_end_seconds"] == 2.0
    assert percentile([1.0, 2.0, 3.0], 0.95) == 3.0
