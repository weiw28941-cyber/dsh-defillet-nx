import json
from pathlib import Path

from PIL import Image
import pytest

from webapp.backend.app.component_rendering import render_fillet_components


def mesh_payload():
    return {
        "revisionId": "r0",
        "faces": [
            {
                "stepEntityId": 10,
                "positions": [0, 0, 0, 1, 0, 0, 0, 1, 0],
                "indices": [0, 1, 2],
            },
            {
                "stepEntityId": 20,
                "positions": [0, 0, 1, 1, 0, 1, 0, 1, 1],
                "indices": [0, 1, 2],
            },
            {
                "stepEntityId": 30,
                "positions": [0, 0, 0, 1, 0, 0, 0, 0, 1],
                "indices": [0, 1, 2],
            },
        ],
    }


def test_render_all_components_with_distinct_colors(tmp_path: Path):
    mesh_path = tmp_path / "mesh.json"
    image_path = tmp_path / "analysis.png"
    mesh_path.write_text(json.dumps(mesh_payload()), encoding="utf-8")
    result = render_fillet_components(mesh_path, {
        "revision_id": "r0",
        "components": [
            {"component_id": "fc-001", "step_entity_ids": [10]},
            {"component_id": "fc-002", "step_entity_ids": [20]},
        ],
    }, image_path)
    assert result["component_count"] == 2
    assert result["views"] == ["Isometric", "Front", "Right", "Top"]
    assert len({entry["color"] for entry in result["legend"]}) == 2
    with Image.open(image_path) as image:
        assert image.format == "PNG"
        assert image.size == (1220, 760)
        pixels = set(image.convert("RGB").get_flattened_data())
        for entry in result["legend"]:
            color = entry["color"].lstrip("#")
            target = tuple(int(color[offset:offset + 2], 16) for offset in (0, 2, 4))
            assert any(sum(abs(a - b) for a, b in zip(pixel, target)) < 100 for pixel in pixels)
        for left, top in ((0, 0), (480, 0), (0, 380), (480, 380)):
            crop = image.crop((left, top, left + 480, top + 380)).convert("RGB")
            assert len(set(crop.get_flattened_data())) > 5


def test_render_rejects_unmapped_component_face(tmp_path: Path):
    mesh_path = tmp_path / "mesh.json"
    mesh_path.write_text(json.dumps(mesh_payload()), encoding="utf-8")
    with pytest.raises(ValueError, match="unmapped STEP face"):
        render_fillet_components(mesh_path, {
            "revision_id": "r0",
            "components": [{"component_id": "fc-001", "step_entity_ids": [999]}],
        }, tmp_path / "analysis.png")
