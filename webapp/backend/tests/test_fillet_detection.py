import json
from pathlib import Path

from webapp.backend.app import fillet_detection


def test_inject_step_face_names_preserves_literal_entity_ids(tmp_path: Path):
    source = tmp_path / "input.step"
    tagged = tmp_path / "tagged.step"
    source.write_text("#42=ADVANCED_FACE('',(),$,.T.);\n#105=ADVANCED_FACE('old',(),$,.T.);\n", encoding="ascii")
    assert fillet_detection.inject_step_face_names(source, tagged) == 2
    text = tagged.read_text(encoding="ascii")
    assert "#42=ADVANCED_FACE('DSH_STEP_FACE_42'" in text
    assert "#105=ADVANCED_FACE('DSH_STEP_FACE_105'" in text


def test_production_detector_has_no_recorded_calibration_dependency():
    source = Path(fillet_detection.__file__).read_text(encoding="utf-8")
    assert "CALIBRATION" not in source
    assert "source_sha256" not in source
    assert "nx_gui_fillet_calibration" not in source
