import pytest

from webapp.backend.app.worker import validate_selected_faces


def test_selection_policy_accepts_any_current_revision_face():
    faces = {
        "r0:face:1": {"surfaceType": "Plane"},
        "r0:face:2": {"surfaceType": "Cylinder"},
    }
    validate_selected_faces(faces, {"r0:face:1", "r0:face:2"})


def test_selection_policy_rejects_unknown_face():
    with pytest.raises(ValueError, match="current revision"):
        validate_selected_faces({}, {"r0:face:99"})
