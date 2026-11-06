from webapp.backend.app.schemas import FilletDeletePlan
from webapp.backend.app.worker import describe_plan


def test_describe_plan_contains_attempt_strategy_and_reason():
    plan = FilletDeletePlan(
        operation="fillet.delete",
        target_face_ids=["r0:face:1", "r0:face:2"],
        preference="notch",
        reason="缩小目标集合后重试",
    )

    message = describe_plan(plan, 2, 6)

    assert "第 2/6 次尝试" in message
    assert "目标 2 个面" in message
    assert "r0:face:1" in message
    assert "Preference=notch" in message
    assert "DeletePartialBlend=关闭" in message
    assert "缩小目标集合后重试" in message
