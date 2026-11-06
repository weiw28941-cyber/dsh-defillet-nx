import pytest
from webapp.backend.app.planner import validate_policy
from webapp.backend.app.schemas import ExecuteRequest, FilletDeletePlan, SelectedFace


def request():
    return ExecuteRequest(revision_id="r0", prompt="delete", selected_faces=[SelectedFace(face_id="r0:face:1", face_index=1, pick_point=(0,0,0))])


def test_policy_accepts_selected_target():
    validate_policy(FilletDeletePlan(operation="fillet.delete", target_face_ids=["r0:face:1"], reason="ok"), request())


def test_policy_rejects_unselected_target():
    with pytest.raises(ValueError):
        validate_policy(FilletDeletePlan(operation="fillet.delete", target_face_ids=["r0:face:2"], reason="bad"), request())
