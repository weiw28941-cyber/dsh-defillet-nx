import importlib.util
import sys
import types
from pathlib import Path


def load_worker_module(monkeypatch):
    nxopen = types.ModuleType("NXOpen")
    nxopen_uf = types.ModuleType("NXOpen.UF")
    nxopen.UF = nxopen_uf
    monkeypatch.setitem(sys.modules, "NXOpen", nxopen)
    monkeypatch.setitem(sys.modules, "NXOpen.UF", nxopen_uf)

    worker_path = Path(__file__).resolve().parents[3] / "cad" / "nx_persistent_worker.py"
    spec = importlib.util.spec_from_file_location("test_nx_persistent_worker_journal", worker_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def signature(*, face_count=497, edge_count=1235, volume=100.0, area=200.0, checksum=(1.0, 2.0, 3.0)):
    body = {
        "tag": 17,
        "solid": True,
        "face_count": face_count,
        "edge_count": edge_count,
        "mass_properties": {
            "area": area,
            "volume": volume,
            "centroid": [10.0, 20.0, 30.0],
        },
        "bounding_box": {
            "minimum": [0.0, 0.0, 0.0],
            "maximum": [100.0, 100.0, 100.0],
            "lengths": [100.0, 100.0, 100.0],
            "vertex_checksum": list(checksum),
            "vertex_sample_count": 80,
        },
    }
    return {
        "part_identity": "17:model",
        "display_part_identity": "17:model",
        "body_count": 1,
        "feature_count": 1,
        "bodies": [body],
    }


def test_undo_signature_accepts_measured_mass_property_drift(monkeypatch):
    worker = load_worker_module(monkeypatch)
    baseline = signature()
    restored = signature(
        volume=100.000099357,
        area=199.999991965,
        checksum=(1.0, 2.0, 3.0),
    )

    assert worker.undo_signatures_match(baseline, restored)


def test_undo_signature_rejects_topology_or_boundary_changes(monkeypatch):
    worker = load_worker_module(monkeypatch)
    baseline = signature()

    assert not worker.undo_signatures_match(baseline, signature(face_count=496))
    assert not worker.undo_signatures_match(baseline, signature(checksum=(1.0, 2.0, 3.1)))
    assert not worker.undo_signatures_match(baseline, signature(volume=100.001))


def test_undo_signature_rejects_non_finite_mass_properties(monkeypatch):
    worker = load_worker_module(monkeypatch)
    baseline = signature()
    restored = signature()
    restored["bodies"][0]["mass_properties"]["volume"] = float("nan")

    assert not worker.undo_signatures_match(baseline, restored)


def test_normalization_uses_reimported_signature_without_source_comparison(monkeypatch, tmp_path):
    worker = load_worker_module(monkeypatch)
    source = signature(face_count=312, volume=37540.003, area=35505.470)
    normalized = signature(face_count=312, volume=37551.538, area=35507.107)
    calls = []
    session = type("Session", (), {"Parts": type("Parts", (), {"Display": object()})()})()
    normalized_part = object()

    monkeypatch.setattr(worker, "model_signature", lambda part, display: normalized)
    monkeypatch.setattr(
        worker,
        "export_normalized_step",
        lambda session, part, output_path, settings_file: calls.append(
            ("export", part, output_path, settings_file)
        ) or output_path,
    )
    monkeypatch.setattr(worker, "close_all_parts", lambda session: calls.append(("close", session)))
    monkeypatch.setattr(
        worker,
        "load_step",
        lambda session, input_path: calls.append(("load", session, input_path)) or normalized_part,
    )

    part, path, metadata = worker.normalize_loaded_step(session, source, str(tmp_path))

    assert part is normalized_part
    assert path.endswith("input\\normalized.stp") or path.endswith("input/normalized.stp")
    assert metadata == {
        "normalized_signature": normalized,
        "normalized_path": path,
        "reimported": True,
    }
    assert [call[0] for call in calls] == ["export", "close", "load"]
    assert worker.normalization_summary(normalized) == {
        "normalized_face_count": 312,
        "normalized_body_count": 1,
        "reimported": True,
    }
