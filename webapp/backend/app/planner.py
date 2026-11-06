from __future__ import annotations

import gzip
import json
import re
import httpx

from .config import settings
from .schemas import ExecuteRequest, FilletDeletePlan


SYSTEM_PROMPT = """You are a CAD operation planner. Return JSON only. The only allowed operation is fillet.delete. You may target only face IDs provided in selectedFaces. Prefer cliff on the first attempt. Partial-blend deletion is always enabled by the fixed backend and is not a planning parameter. When previousAttempts is non-empty, study each normalized NX failure and choose a materially different safe plan: change preference or try a smaller non-empty subset of selected faces. Never repeat an exact target/preference combination from previousAttempts. Never return code, commands, paths, NXOpen APIs, or extra properties. Schema: {\"operation\":\"fillet.delete\",\"target_face_ids\":[\"...\"],\"preference\":\"cliff|notch\",\"reason\":\"...\"}."""


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
    return json.loads(text)


def _response_json(response: httpx.Response) -> dict:
    content = response.content
    # The local MIP proxy sometimes returns gzip bytes without a
    # Content-Encoding header, so httpx cannot decompress them automatically.
    if len(content) >= 2 and content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return json.loads(content.decode("utf-8"))


def plan_operation(
    request: ExecuteRequest,
    observation: dict,
    previous_attempts: list[dict] | None = None,
) -> FilletDeletePlan:
    if not settings.openai_api_key or not settings.openai_model:
        raise RuntimeError("OPENAI_API_KEY and OPENAI_MODEL must be configured")
    payload = {
        "model": settings.openai_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "userRequest": request.prompt,
                        "observation": observation,
                        "previousAttempts": previous_attempts or [],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    headers = dict(settings.planner_headers)
    headers["Authorization"] = "Bearer {}".format(settings.openai_api_key)
    response = httpx.post(
        settings.openai_base_url + "/chat/completions",
        headers=headers,
        json=payload,
        timeout=60.0,
    )
    try:
        response_payload = _response_json(response)
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError("model service returned an unreadable response: {}".format(exc)) from exc
    if response.is_error:
        detail = response_payload.get("error", {}).get("message", str(response_payload))
        raise RuntimeError("model service returned HTTP {}: {}".format(response.status_code, detail[:1000]))
    content = response_payload["choices"][0]["message"]["content"]
    return FilletDeletePlan.model_validate(_extract_json(content))


def plan_signature(plan: FilletDeletePlan) -> tuple[tuple[str, ...], str]:
    return tuple(sorted(set(plan.target_face_ids))), plan.preference


def validate_policy(
    plan: FilletDeletePlan,
    request: ExecuteRequest,
    previous_attempts: list[dict] | None = None,
) -> None:
    selected = {face.face_id for face in request.selected_faces}
    targets = set(plan.target_face_ids)
    if not targets.issubset(selected):
        raise ValueError("planner returned targets outside the user's selected faces")
    if not targets:
        raise ValueError("planner returned no target faces")
    attempted = {
        (
            tuple(sorted(set(item["plan"]["target_face_ids"]))),
            item["plan"]["preference"],
        )
        for item in (previous_attempts or [])
        if isinstance(item.get("plan"), dict)
    }
    if plan_signature(plan) in attempted:
        raise ValueError("planner repeated an already failed operation")
