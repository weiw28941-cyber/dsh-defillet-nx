from webapp.backend.app.planner import validate_policy
from webapp.backend.app.schemas import ExecuteRequest, FilletDeletePlan, SelectedFace


def request():
    return ExecuteRequest(
        revision_id="r0",
        prompt="delete",
        selected_faces=[
            SelectedFace(face_id="r0:face:1", face_index=1, pick_point=(0, 0, 0)),
            SelectedFace(face_id="r0:face:2", face_index=2, pick_point=(1, 0, 0)),
        ],
    )


def test_policy_rejects_exact_failed_plan():
    failed = FilletDeletePlan(
        operation="fillet.delete",
        target_face_ids=["r0:face:1", "r0:face:2"],
        preference="cliff",
        reason="first attempt",
    )
    previous = [{"plan": failed.model_dump(), "error": "NX commit failed"}]

    try:
        validate_policy(failed, request(), previous)
    except ValueError as exc:
        assert "repeated" in str(exc)
    else:
        raise AssertionError("an exact failed plan must not be repeated")


def test_policy_accepts_materially_different_retry():
    previous = [{
        "plan": {
            "operation": "fillet.delete",
            "target_face_ids": ["r0:face:1", "r0:face:2"],
            "preference": "cliff",
            "reason": "first attempt",
        },
        "error": "NX commit failed",
    }]
    retry = FilletDeletePlan(
        operation="fillet.delete",
        target_face_ids=["r0:face:1"],
        preference="notch",
        reason="use the failure feedback and reduce the target set",
    )

    validate_policy(retry, request(), previous)
