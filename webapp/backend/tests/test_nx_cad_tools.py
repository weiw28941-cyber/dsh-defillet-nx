from types import SimpleNamespace

import pytest

from webapp.backend.app.nx_cad_tools import RuntimeCadTools, delete_code
from webapp.backend.app.nx_runtime_coordinator import RuntimeCoordinatorError
from pydantic import ValidationError

from webapp.backend.app.schemas import (
    RuntimeDeleteComponentRequest,
    RuntimeDeleteSegmentRequest,
    RuntimeFaceGroupRequest,
)


class FakeRuntime:
    def __init__(self, observation=None):
        self.state = 0
        self.worker_id = "worker-test"
        self.responses = []
        self.observation = observation or {
            "faces": [
                {"tag": 10, "surface_type": "Cylindrical", "edge_count": 4},
                {"tag": 11, "surface_type": "Torus", "edge_count": 4},
                {"tag": 20, "surface_type": "Planar", "edge_count": 4},
            ],
            "adjacency": {"10": [11], "11": [10]},
            "components": [[10, 11]],
        }

    def rollback_latest_commit(self, _session_id):
        self.state += 1
        return {
            "operation_id": "op-rollback", "state": "committed", "result_state": self.state,
            "result": {"rolled_back_operation_id": "op-delete", "runtime_state": self.state},
        }

    def status(self, _session_id):
        return {
            "runtime_state": self.state,
            "binding": {"worker_id": self.worker_id},
        }

    def execute(self, _session_id, request):
        self.responses.append(request)
        if request.mode.value == "read":
            return {
                "state": "completed",
                "result": self.observation,
            }
        self.state += 1
        return {
            "state": "committed",
            "retained": True,
            "rolled_back": False,
            "result_state": self.state,
            "operation_id": "op-delete",
            "result": {"deletion_seconds": 0.25},
            "timings": {"execute_seconds": 0.3},
            "error": None,
        }


def test_reanalysis_is_stable_across_nx_tag_and_iteration_order():
    first = {
        "faces": [
            {"tag": 900, "name": "DSH_STEP_FACE_20", "surface_type": "Torus", "edge_count": 4},
            {"tag": 100, "name": "DSH_STEP_FACE_10", "surface_type": "Cylindrical", "edge_count": 4},
            {"tag": 800, "name": "DSH_STEP_FACE_40", "surface_type": "Torus", "edge_count": 4},
            {"tag": 200, "name": "DSH_STEP_FACE_30", "surface_type": "Cylindrical", "edge_count": 4},
        ],
        "adjacency": {"900": [100], "100": [900], "800": [200], "200": [800]},
        "components": [[800, 200], [900, 100]],
    }
    second = {
        "faces": [
            {"tag": 71, "name": "DSH_STEP_FACE_30", "surface_type": "Cylindrical", "edge_count": 4},
            {"tag": 42, "name": "DSH_STEP_FACE_10", "surface_type": "Cylindrical", "edge_count": 4},
            {"tag": 19, "name": "DSH_STEP_FACE_40", "surface_type": "Torus", "edge_count": 4},
            {"tag": 88, "name": "DSH_STEP_FACE_20", "surface_type": "Torus", "edge_count": 4},
        ],
        "adjacency": {"71": [19], "19": [71], "42": [88], "88": [42]},
        "components": [[88, 42], [71, 19]],
    }
    first_result = RuntimeCadTools(FakeRuntime(first)).reanalyze("session")
    second_result = RuntimeCadTools(FakeRuntime(second)).reanalyze("session")
    first_components = [row["stable_face_identities"] for row in first_result["components"]]
    second_components = [row["stable_face_identities"] for row in second_result["components"]]
    assert first_components == second_components == [
        ["DSH_STEP_FACE_10", "DSH_STEP_FACE_20"],
        ["DSH_STEP_FACE_30", "DSH_STEP_FACE_40"],
    ]
    first_segments = RuntimeCadTools(FakeRuntime(first))
    first_component = first_segments.reanalyze("session")["components"][0]
    second_segments = RuntimeCadTools(FakeRuntime(second))
    second_component = second_segments.reanalyze("session")["components"][0]
    first_halves = first_segments.segment_component(
        "session", first_component["component_id"], "bisection"
    )["segments"]
    second_halves = second_segments.segment_component(
        "session", second_component["component_id"], "bisection"
    )["segments"]
    assert [segment["face_count"] for segment in first_halves] == [1, 1]
    assert [segment["face_count"] for segment in second_halves] == [1, 1]
    assert [segment["stable_face_identities"] for segment in first_halves] == [
        segment["stable_face_identities"] for segment in second_halves
    ] == [["DSH_STEP_FACE_10"], ["DSH_STEP_FACE_20"]]


def test_reanalysis_returns_all_face_adjacency_for_planar_supports():
    observation = {
        "faces": [
            {"tag": 10, "surface_type": "Cylindrical", "edge_count": 4},
            {"tag": 20, "surface_type": "Planar", "edge_count": 4},
        ],
        "adjacency": {"10": [20], "20": [10]},
        "components": [[10]],
    }
    result = RuntimeCadTools(FakeRuntime(observation)).reanalyze("session")
    refs = {row["surface_type"]: row["face_ref"] for row in result["faces"]}
    assert result["adjacency"][refs["Cylindrical"]] == [refs["Planar"]]
    assert result["adjacency"][refs["Planar"]] == [refs["Cylindrical"]]


def test_reanalysis_issues_state_bound_component_references():
    tools = RuntimeCadTools(FakeRuntime())
    result = tools.reanalyze("session")
    assert result["runtime_state"] == 0
    assert result["components"][0]["component_id"] == "rs000000-fc-0001"
    assert result["components"][0]["face_refs"] == ["rs000000-face-1", "rs000000-face-2"]
    assert result["components"][0]["end_face_refs"] == ["rs000000-face-1", "rs000000-face-2"]


def test_face_properties_returns_current_face_and_adjacent_face_attributes():
    tools = RuntimeCadTools(FakeRuntime())
    result = tools.reanalyze("session")
    face_ref = result["components"][0]["face_refs"][0]
    properties = tools.face_properties("session", face_ref)
    assert properties["schema"] == "nx_face_properties_v1"
    assert properties["face"]["face_ref"] == face_ref
    assert properties["face"]["surface_type"] == "Cylindrical"
    assert properties["face"]["is_curved_candidate"] is True
    assert properties["adjacent_faces"][0]["surface_type"] == "Torus"


def test_face_properties_rejects_unknown_reference():
    tools = RuntimeCadTools(FakeRuntime())
    tools.reanalyze("session")
    with pytest.raises(RuntimeCoordinatorError, match="current runtime state"):
        tools.face_properties("session", "rs000000-face-999")


def test_state_zero_maps_accepted_components_by_exact_step_face_sets_not_ordinal(monkeypatch):
    observation = {
        "faces": [
            {"tag": 9001, "name": "DSH_STEP_FACE_20", "surface_type": "Torus", "edge_count": 4},
            {"tag": 17, "name": "DSH_STEP_FACE_10", "surface_type": "Cylindrical", "edge_count": 4},
            {"tag": 44, "name": "DSH_STEP_FACE_40", "surface_type": "Torus", "edge_count": 4},
            {"tag": 3, "name": "DSH_STEP_FACE_30", "surface_type": "Cylindrical", "edge_count": 4},
        ],
        "adjacency": {"9001": [17], "17": [9001], "44": [3], "3": [44]},
        # Runtime order deliberately reverses the Accepted component order.
        "components": [[3, 44], [17, 9001]],
    }
    tools = RuntimeCadTools(FakeRuntime(observation))
    monkeypatch.setattr(tools, "_accepted_components", lambda _session: (
        "r0", {"fc-001": {10, 20}, "fc-002": {30, 40}}
    ))

    result = tools.reanalyze("session")

    assert result["mapping_complete"] is True
    assert result["accepted_component_mapping"]["fc-001"]["runtime_component_id"] == "rs000000-fc-0001"
    assert result["accepted_component_mapping"]["fc-002"]["runtime_component_id"] == "rs000000-fc-0002"
    runtime_components = {row["component_id"]: row for row in result["components"]}
    assert runtime_components["rs000000-fc-0001"]["accepted_component_id"] == "fc-001"
    assert runtime_components["rs000000-fc-0002"]["accepted_component_id"] == "fc-002"
    resolved = tools.resolve_accepted_component("session", "fc-001")
    assert resolved["runtime_component_id"] == "rs000000-fc-0001"
    assert resolved["mapping_status"] == "exact"


def test_state_zero_refuses_split_accepted_component_mapping(monkeypatch):
    observation = {
        "faces": [
            {"tag": 10, "name": "DSH_STEP_FACE_100", "surface_type": "Cylindrical", "edge_count": 2},
            {"tag": 20, "name": "DSH_STEP_FACE_101", "surface_type": "Torus", "edge_count": 2},
        ],
        "adjacency": {"10": [], "20": []},
        "components": [[10], [20]],
    }
    tools = RuntimeCadTools(FakeRuntime(observation))
    monkeypatch.setattr(tools, "_accepted_components", lambda _session: (
        "r0", {"fc-003": {100, 101}}
    ))

    result = tools.reanalyze("session")
    assert result["mapping_complete"] is False
    assert result["accepted_component_mapping"]["fc-003"]["mapping_status"] == "unmapped"
    with pytest.raises(RuntimeCoordinatorError, match="no exact STEP-identity match"):
        tools.resolve_accepted_component("session", "fc-003")


def test_state_after_commit_does_not_require_accepted_mapping(monkeypatch):
    runtime = FakeRuntime()
    runtime.state = 1
    tools = RuntimeCadTools(runtime)
    monkeypatch.setattr(
        tools, "_accepted_components",
        lambda _session: (_ for _ in ()).throw(AssertionError("must not load Accepted mapping")),
    )

    result = tools.reanalyze("session")
    assert result["runtime_state"] == 1
    assert result["accepted_component_mapping"] == {}
    assert result["mapping_complete"] is False


def test_successful_delete_retains_and_invalidates_catalog():
    runtime = FakeRuntime()
    tools = RuntimeCadTools(runtime)
    component = tools.reanalyze("session")["components"][0]
    result = tools.try_delete_component(
        "session", component["component_id"], "face_edge_blend", "cliff"
    )
    assert result["commit_succeeded"] is True
    assert result["retained"] is True
    assert result["deletion_seconds"] == 0.25
    assert result["runtime_state_after"] == 1
    with pytest.raises(RuntimeCoordinatorError, match="reanalyze_current_state"):
        tools.component_topology("session", component["component_id"])


def test_face_group_delete_accepts_explicit_related_faces():
    tools = RuntimeCadTools(FakeRuntime())
    result = tools.reanalyze("session")
    refs = result["components"][0]["face_refs"]
    deleted = tools.try_delete_face_group("session", refs, "generic_face", "cliff", True)
    assert deleted["face_group"] is True
    assert deleted["allow_related_faces"] is True
    request = tools.runtime.responses[-1]
    assert "True" in request.code


def test_face_group_delete_rejects_unknown_reference():
    tools = RuntimeCadTools(FakeRuntime())
    tools.reanalyze("session")
    with pytest.raises(RuntimeCoordinatorError, match="do not belong"):
        tools.try_delete_face_group("session", ["rs000000-face-999"], "generic_face", "cliff")


def test_face_group_request_rejects_duplicate_references():
    with pytest.raises(ValidationError):
        RuntimeFaceGroupRequest(
            face_refs=["rs000000-face-1", "rs000000-face-1"],
        )


def test_latest_retained_delete_can_be_rolled_back_and_invalidates_catalog():
    runtime = FakeRuntime()
    tools = RuntimeCadTools(runtime)
    component = tools.reanalyze("session")["components"][0]
    tools.try_delete_component("session", component["component_id"], "face_edge_blend", "cliff")
    result = tools.rollback_latest_delete("session")
    assert result["rollback_succeeded"] is True
    assert result["rolled_back_operation_id"] == "op-delete"
    assert result["runtime_state_after"] == 2
    assert result["next_required_action"] == "reanalyze_current_state"
    current = tools.reanalyze("session")["components"][0]
    with pytest.raises(RuntimeCoordinatorError, match="already failed"):
        tools.try_delete_component(
            "session", current["component_id"], "face_edge_blend", "cliff"
        )


def test_failed_delete_uses_commit_timing_and_preserves_catalog():
    runtime = FakeRuntime()
    tools = RuntimeCadTools(runtime)
    component = tools.reanalyze("session")["components"][0]
    runtime.execute = lambda _session, _request: {
        "state": "rolled_back", "retained": False, "rolled_back": True,
        "result_state": 0,
        "error": {"code": "NX_API_ERROR", "message": "cannot close", "details": {"deletion_seconds": 0.42}},
        "timings": {"execute_seconds": 1.2},
    }
    result = tools.try_delete_component(
        "session", component["component_id"], "face_edge_blend", "cliff"
    )
    assert result["commit_succeeded"] is False
    assert result["deletion_seconds"] == 0.42
    assert tools.component_topology("session", component["component_id"])["face_count"] == 2
    with pytest.raises(RuntimeCoordinatorError, match="already failed"):
        tools.try_delete_component(
            "session", component["component_id"], "face_edge_blend", "cliff"
        )


def test_topology_policy_rejection_is_reported_and_undoes():
    runtime = FakeRuntime()
    tools = RuntimeCadTools(runtime)
    component = tools.reanalyze("session")["components"][0]
    runtime.execute = lambda _session, _request: {
        "state": "rolled_back", "retained": False, "rolled_back": True,
        "result_state": 0,
        "error": {
            "code": "NX_API_ERROR", "message": "Delete exceeded topology-change policy",
            "details": {
                "deletion_seconds": 0.1,
                "topology_change": {
                    "selected_named_faces": ["DSH_STEP_FACE_164"],
                    "collateral_disappeared_faces": ["DSH_STEP_FACE_194"],
                    "severe_survivor_changes": ["DSH_STEP_FACE_232"],
                },
            },
        },
    }
    result = tools.try_delete_component(
        "session", component["component_id"], "face_edge_blend", "cliff"
    )
    assert result["commit_succeeded"] is False
    assert result["rolled_back"] is True
    assert result["topology_policy_rejected"] is True
    assert result["topology_change"]["collateral_disappeared_faces"] == ["DSH_STEP_FACE_194"]


def test_delete_partial_blend_is_rejected_by_public_contract():
    with pytest.raises(ValidationError):
        RuntimeDeleteComponentRequest(
            component_id="rs000000-fc-0001", delete_partial_blend=True
        )


def test_segment_ids_are_state_bound_and_delete_code_is_allowlisted():
    tools = RuntimeCadTools(FakeRuntime())
    component = tools.reanalyze("session")["components"][0]
    segments = tools.segment_component("session", component["component_id"], "bisection")
    assert [row["face_count"] for row in segments["segments"]] == [1, 1]
    assert all("-seg-" in row["segment_id"] for row in segments["segments"])
    RuntimeDeleteSegmentRequest(segment_id=segments["segments"][0]["segment_id"])
    code = delete_code([10], "generic_face", "notch")
    assert "SelectTypes.Face" in code
    assert "PreferenceOptions.Notch" in code
    assert "builder.DeletePartialBlend = False" in code
    assert "builder.DeletePartialBlend = True" not in code
