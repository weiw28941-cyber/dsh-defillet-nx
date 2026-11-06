from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
import uuid
from typing import Any

from .nx_runtime_contracts import PublicRuntimeExecuteRequest, RuntimeMode
from .nx_runtime_coordinator import RuntimeCoordinator, RuntimeCoordinatorError


CURVED_TYPES = {"Cylindrical", "Spherical", "Torus", "BSurface", "Blending"}
TAGGED_FACE_NAME = re.compile(r"^DSH_STEP_FACE_(\d+)$")
METHOD_TO_SELECT_TYPE = {
    "face_edge_blend": "FaceEdgeBlend",
    "blend": "Blend",
    "generic_face": "Face",
}

OBSERVE_CODE = r'''
def face_type_name(face):
    raw = str(face.SolidFaceType).rsplit('.', 1)[-1]
    return {
        '1': 'Planar', '2': 'Cylindrical', '3': 'Conical',
        '4': 'Spherical', '5': 'Torus', '6': 'BSurface',
    }.get(raw, raw)

def face_name(face):
    try:
        return str(face.Name or '')
    except Exception:
        return ''

faces = []
objects = {}
for body in workPart.Bodies:
    for face in body.GetFaces():
        tag = int(face.Tag)
        objects[tag] = face
        faces.append({
            'tag': tag,
            'name': face_name(face),
            'surface_type': face_type_name(face),
            'edge_count': len(face.GetEdges()),
        })
all_adjacency = dict((row['tag'], set()) for row in faces)
for tag in sorted(all_adjacency):
    for edge in objects[tag].GetEdges():
        try:
            edge_faces = list(edge.GetFaces())
        except Exception:
            edge_faces = []
        for neighbor in edge_faces:
            other = int(neighbor.Tag)
            if other in all_adjacency and other != tag:
                all_adjacency[tag].add(other)
curved = set(row['tag'] for row in faces if row['surface_type'] in %s)
adjacency = dict((tag, all_adjacency[tag] & curved) for tag in curved)
components = []
remaining = set(curved)
while remaining:
    seed = min(remaining)
    remaining.remove(seed)
    stack = [seed]
    group = []
    while stack:
        tag = stack.pop()
        group.append(tag)
        for neighbor in sorted(adjacency[tag] & remaining):
            remaining.remove(neighbor)
            stack.append(neighbor)
    components.append(sorted(group))
components.sort(key=lambda group: (group[0], len(group)))
result = {
    'faces': faces,
    'adjacency': dict((str(tag), sorted(neighbors)) for tag, neighbors in all_adjacency.items()),
    'curved_adjacency': dict((str(tag), sorted(neighbors)) for tag, neighbors in adjacency.items()),
    'components': components,
}
''' % (repr(tuple(sorted(CURVED_TYPES))),)


def delete_code(tags: list[int], method: str, preference: str, allow_related_faces: bool = False) -> str:
    select_type = METHOD_TO_SELECT_TYPE[method]
    preference_name = "Cliff" if preference == "cliff" else "Notch"
    return r'''
import NXOpen.Features
import time
def face_name(face):
    try:
        name = str(face.Name or '')
    except Exception:
        name = ''
    if name:
        return name
    try:
        return str(face.GetUserAttribute('Title', NXOpen.NXObject.AttributeType.String, -1) or '')
    except Exception:
        return ''
wanted = set(%s)
faces = []
for body in workPart.Bodies:
    for face in body.GetFaces():
        if int(face.Tag) in wanted:
            faces.append(face)
if len(faces) != len(wanted):
    raise RuntimeError('state-bound face references are no longer current')
faces.sort(key=lambda face: (face_name(face), int(face.Tag)))
builder = workPart.Features.CreateDeleteFaceBuilder(NXOpen.Features.Feature.Null)
try:
    builder.FaceRecognized.RelationScope = 0
    builder.FaceRecognized.CloneScope = 1535
    builder.FaceRecognized.CoplanarEnabled = False
    builder.FaceRecognized.CoplanarAxesEnabled = False
    builder.FaceRecognized.CoaxialEnabled = False
    builder.FaceRecognized.SameOrbitEnabled = False
    builder.FaceRecognized.EqualDiameterEnabled = False
    builder.FaceRecognized.TangentEnabled = False
    builder.FaceRecognized.SymmetricEnabled = False
    builder.FaceRecognized.OffsetEnabled = False
    builder.FaceRecognized.RigidBodyFaceEnabled = False
    builder.FaceRecognized.UseFindClone = True
    builder.FaceRecognized.UseFindRelated = False
    builder.FaceRecognized.UseFaceBrowse = True
    builder.MaxHoleDiameter.SetFormula('5')
    builder.MaxBlendRadius.SetFormula('5')
    builder.DeletePartialBlend = False
    builder.Type = NXOpen.Features.DeleteFaceBuilder.SelectTypes.%s
    builder.FaceEdgeBlendPreference = NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions.%s
    builder.CapPlane = NXOpen.Plane.Null
    options = workPart.ScRuleFactory.CreateRuleOptions()
    try:
        options.SetSelectedFromInactive(False)
        rule = workPart.ScRuleFactory.CreateRuleFaceDumb(faces, options)
    finally:
        options.Dispose()
    builder.FaceCollector.ReplaceRules([rule], False)
    builder.FaceRecognized.SelectEntities(faces)
    pre_named = {}
    for body in workPart.Bodies:
        for face in body.GetFaces():
            name = face_name(face)
            if name:
                pre_named[name] = len(face.GetEdges())
    selected_names = set(face_name(face) for face in faces if face_name(face))
    commit_started = time.perf_counter()
    try:
        committed = builder.Commit()
    except Exception as commit_error:
        class TimedCommitError(RuntimeError):
            pass
        wrapped = TimedCommitError(str(commit_error) or type(commit_error).__name__)
        wrapped.deletion_seconds = time.perf_counter() - commit_started
        wrapped.nx_error_code = getattr(commit_error, 'ErrorCode', None)
        wrapped.deletion_attempt = {
            'selected_named_faces': sorted(selected_names),
            'selected_count': len(faces),
            'method': %r,
            'preference': %r,
            'delete_partial_blend': False,
        }
        raise wrapped
    commit_seconds = time.perf_counter() - commit_started
    if committed is None:
        error = RuntimeError('DeleteFaceBuilder.Commit returned None')
        error.deletion_seconds = commit_seconds
        raise error
    post_named = {}
    for body in workPart.Bodies:
        for face in body.GetFaces():
            name = face_name(face)
            if name:
                post_named[name] = len(face.GetEdges())
    disappeared = sorted(set(pre_named) - set(post_named))
    collateral = sorted(set(disappeared) - selected_names)
    changed_survivors = sorted(
        name for name in set(pre_named) & set(post_named)
        if pre_named[name] != post_named[name]
    )
    survivor_edge_deltas = dict(
        (name, post_named[name] - pre_named[name]) for name in changed_survivors
    )
    severe_survivor_changes = sorted(
        name for name in changed_survivors if abs(survivor_edge_deltas[name]) >= 8
    )
    if (collateral and not %s) or severe_survivor_changes:
        reasons = []
        if collateral and not %s:
            reasons.append('non-target faces disappeared: %%s' %% ', '.join(collateral[:20]))
        if severe_survivor_changes:
            reasons.append('support-face boundaries changed substantially: %%s' %% ', '.join(severe_survivor_changes[:20]))
        error = RuntimeError('Delete exceeded topology-change policy; ' + '; '.join(reasons))
        error.deletion_seconds = commit_seconds
        error.topology_change = {
            'selected_named_faces': sorted(selected_names),
            'disappeared_named_faces': disappeared,
            'collateral_disappeared_faces': collateral,
            'changed_surviving_faces': changed_survivors,
            'survivor_edge_deltas': survivor_edge_deltas,
            'severe_survivor_changes': severe_survivor_changes,
        }
        raise error
    result = {
        'commit_succeeded': True,
        'deletion_seconds': commit_seconds,
        'selected_count': len(faces),
        'method': %r,
        'preference': %r,
        'delete_partial_blend': False,
        'topology_change': {
            'selected_named_faces': sorted(selected_names),
            'disappeared_named_faces': disappeared,
            'collateral_disappeared_faces': collateral,
            'changed_surviving_faces': changed_survivors,
            'survivor_edge_deltas': survivor_edge_deltas,
            'severe_survivor_changes': severe_survivor_changes,
        },
    }
finally:
    builder.Destroy()
''' % (
        repr(sorted(tags)), select_type,
        preference_name, method, preference, bool(allow_related_faces), bool(allow_related_faces),
        method, preference,
    )


@dataclass
class StateCatalog:
    worker_id: str
    runtime_state: int
    faces: dict[str, dict[str, Any]]
    components: dict[str, dict[str, Any]]
    adjacency: dict[str, list[str]]
    segments: dict[str, dict[str, Any]]
    accepted_component_mapping: dict[str, dict[str, Any]]


class RuntimeCadTools:
    """Allowlisted, state-bound CAD operations over RuntimeCoordinator."""

    def __init__(self, runtime: RuntimeCoordinator) -> None:
        self.runtime = runtime
        self._catalogs: dict[str, StateCatalog] = {}
        self._attempts: dict[str, dict[int, set[tuple[tuple[str, ...], str, str]]]] = {}
        self._retained_attempts: dict[str, dict[str, tuple[tuple[str, ...], str, str]]] = {}

    def _accepted_components(self, session_id: str) -> tuple[str | None, dict[str, set[int]]]:
        if not hasattr(self.runtime, "_resolve") or not hasattr(self.runtime, "jobs"):
            return None, {}
        job, revision_id, _step = self.runtime._resolve(session_id)
        report_path = self.runtime.jobs.revision_dir(job["id"], revision_id) / "nx_fillet_components.json"
        if not report_path.is_file():
            return revision_id, {}
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("revision_id") != revision_id:
            return revision_id, {}
        components: dict[str, set[int]] = {}
        for component in report.get("components") or []:
            component_id = str(component.get("component_id") or "")
            step_ids = {int(value) for value in component.get("step_entity_ids") or []}
            if component_id and step_ids:
                components[component_id] = step_ids
        return revision_id, components

    @staticmethod
    def _operation_id(label: str) -> str:
        return "op-cad-{}-{}".format(label, uuid.uuid4().hex)

    def _execute(self, session_id: str, state: int, mode: RuntimeMode, code: str, label: str) -> dict[str, Any]:
        return self.runtime.execute(session_id, PublicRuntimeExecuteRequest(
            operation_id=self._operation_id(label), base_state=state, mode=mode, code=code
        ))

    def reanalyze(self, session_id: str) -> dict[str, Any]:
        status = self.runtime.status(session_id)
        state = int(status["runtime_state"])
        result = self._execute(session_id, state, RuntimeMode.READ, OBSERVE_CODE, "observe")
        if result.get("state") != "completed":
            raise RuntimeCoordinatorError("NX current-state analysis failed: {}".format(result.get("error")))
        raw = result.get("result") or {}
        normalization = status.get("normalization")
        current_status = self.runtime.status(session_id)
        worker_id = str((current_status.get("binding") or {}).get("worker_id", ""))
        current_state = int(current_status["runtime_state"])
        if current_state != state:
            raise RuntimeCoordinatorError("runtime state changed during current-state analysis")
        prefix = "rs{:06d}".format(state)
        faces: dict[str, dict[str, Any]] = {}
        tag_to_ref: dict[int, str] = {}
        step_id_to_ref: dict[int, str] = {}
        duplicate_step_ids: set[int] = set()

        def raw_face_key(row: dict[str, Any]) -> tuple[int, int, int]:
            tagged = TAGGED_FACE_NAME.fullmatch(str(row.get("name") or ""))
            if tagged:
                return (0, int(tagged.group(1)), int(row["tag"]))
            return (1, int(row["tag"]), int(row["tag"]))

        raw_faces = sorted(raw.get("faces") or [], key=raw_face_key)
        for row in raw_faces:
            tag = int(row["tag"])
            face_ref = "{}-face-{}".format(prefix, len(faces) + 1)
            tag_to_ref[tag] = face_ref
            name = str(row.get("name") or "")
            tagged = TAGGED_FACE_NAME.fullmatch(name)
            step_entity_id = int(tagged.group(1)) if tagged else None
            enriched = {**row, "face_ref": face_ref, "step_entity_id": step_entity_id}
            faces[face_ref] = enriched
            if step_entity_id is not None:
                if step_entity_id in step_id_to_ref:
                    duplicate_step_ids.add(step_entity_id)
                step_id_to_ref[step_entity_id] = face_ref
        adjacency = raw.get("adjacency") or {}
        components: dict[str, dict[str, Any]] = {}

        def stable_tag_key(tag: int) -> tuple[int, int, int]:
            row = faces[tag_to_ref[int(tag)]]
            step_id = row.get("step_entity_id")
            return (
                0 if step_id is not None else 1,
                int(step_id) if step_id is not None else int(tag),
                int(tag),
            )

        raw_components = [set(int(tag) for tag in tags) for tags in raw.get("components") or []]
        raw_components.sort(key=lambda tags: tuple(stable_tag_key(tag) for tag in sorted(tags, key=stable_tag_key)))
        for index, tag_set in enumerate(raw_components, start=1):
            component_id = "{}-fc-{:04d}".format(prefix, index)
            degrees = {
                tag: len(tag_set & set(adjacency.get(str(tag), []))) for tag in tag_set
            }
            ordered_tags = sorted(tag_set, key=stable_tag_key)
            refs = [tag_to_ref[tag] for tag in ordered_tags]
            components[component_id] = {
                "component_id": component_id,
                "face_refs": refs,
                "face_count": len(refs),
                "branch_face_refs": [tag_to_ref[tag] for tag, degree in degrees.items() if degree > 2],
                "end_face_refs": [tag_to_ref[tag] for tag, degree in degrees.items() if degree <= 1],
                "closed_loop": bool(tag_set) and all(degree == 2 for degree in degrees.values()),
                "surface_types": sorted({faces[tag_to_ref[tag]]["surface_type"] for tag in tag_set}),
                "stable_face_identities": [
                    str(faces[tag_to_ref[tag]].get("name") or "tag:{}".format(tag))
                    for tag in ordered_tags
                ],
            }
        adjacency_refs = {
            tag_to_ref[int(tag)]: [tag_to_ref[int(neighbor)] for neighbor in neighbors]
            for tag, neighbors in adjacency.items()
            if int(tag) in tag_to_ref
        }
        accepted_revision: str | None = None
        accepted_components: dict[str, set[int]] = {}
        mapping: dict[str, dict[str, Any]] = {}
        if state == 0:
            accepted_revision, accepted_components = self._accepted_components(session_id)
        if state == 0 and accepted_components:
            if duplicate_step_ids:
                raise RuntimeCoordinatorError(
                    "runtime state-0 STEP identity is ambiguous; duplicate face IDs: {}".format(
                        sorted(duplicate_step_ids)[:20]
                    )
                )
            runtime_component_by_step: dict[int, str] = {}
            for component_id, component in components.items():
                step_ids = {
                    int(faces[ref]["step_entity_id"])
                    for ref in component["face_refs"]
                    if faces[ref].get("step_entity_id") is not None
                }
                component["step_entity_ids"] = sorted(step_ids)
                for step_id in step_ids:
                    if step_id in runtime_component_by_step:
                        raise RuntimeCoordinatorError(
                            "runtime state-0 STEP face belongs to multiple components"
                        )
                    runtime_component_by_step[step_id] = component_id
            for accepted_id, accepted_step_ids in accepted_components.items():
                missing = sorted(accepted_step_ids - set(step_id_to_ref))
                runtime_ids = sorted({
                    runtime_component_by_step[step_id]
                    for step_id in accepted_step_ids
                    if step_id in runtime_component_by_step
                })
                exact = (
                    not missing
                    and len(runtime_ids) == 1
                    and set(components[runtime_ids[0]].get("step_entity_ids") or []) == accepted_step_ids
                )
                runtime_component_id = runtime_ids[0] if exact else None
                mapping[accepted_id] = {
                    "accepted_component_id": accepted_id,
                    "runtime_component_id": runtime_component_id,
                    "step_entity_ids": sorted(accepted_step_ids),
                    "mapping_status": "exact" if exact else "unmapped",
                    "missing_step_entity_ids": missing,
                    "candidate_runtime_component_ids": runtime_ids,
                }
                if runtime_component_id:
                    components[runtime_component_id]["accepted_component_id"] = accepted_id
                    components[runtime_component_id]["identity_mapping"] = "exact-step-face-set"
        catalog = StateCatalog(worker_id, state, faces, components, adjacency_refs, {}, mapping)
        self._catalogs[session_id] = catalog
        return {
            "schema": "nx_current_components_v1",
            "runtime_state": state,
            "worker_id": worker_id,
            "normalization": normalization,
            "face_count": len(faces),
            "faces": list(faces.values()),
            "component_count": len(components),
            "components": list(components.values()),
            "adjacency": adjacency_refs,
            "accepted_revision": accepted_revision,
            "accepted_component_mapping": mapping,
            "mapping_scope": "state-0-only",
            "mapping_complete": bool(mapping) and all(
                row["mapping_status"] == "exact" for row in mapping.values()
            ),
            "references_invalidated_on_commit": True,
        }

    def _catalog(self, session_id: str) -> StateCatalog:
        catalog = self._catalogs.get(session_id)
        status = self.runtime.status(session_id)
        worker_id = str((status.get("binding") or {}).get("worker_id", ""))
        if catalog is None:
            raise RuntimeCoordinatorError("reanalyze_current_state is required first")
        if catalog.runtime_state != int(status["runtime_state"]) or catalog.worker_id != worker_id:
            self._catalogs.pop(session_id, None)
            raise RuntimeCoordinatorError("state-bound component references are stale; reanalyze current state")
        return catalog

    def resolve_accepted_component(self, session_id: str, component_id: str) -> dict[str, Any]:
        catalog = self._catalog(session_id)
        if catalog.runtime_state != 0:
            raise RuntimeCoordinatorError(
                "Accepted component mapping is available only at runtime state 0"
            )
        mapping = catalog.accepted_component_mapping.get(component_id)
        if mapping is None:
            raise RuntimeCoordinatorError(
                "Accepted component does not belong to the loaded runtime baseline"
            )
        if mapping["mapping_status"] != "exact" or not mapping["runtime_component_id"]:
            raise RuntimeCoordinatorError(
                "Accepted component has no exact STEP-identity match in runtime state 0"
            )
        return {
            "schema": "nx_accepted_component_mapping_v1",
            "runtime_state": catalog.runtime_state,
            **mapping,
        }

    def component_topology(self, session_id: str, component_id: str) -> dict[str, Any]:
        catalog = self._catalog(session_id)
        component = catalog.components.get(component_id)
        if component is None:
            raise RuntimeCoordinatorError("component does not belong to the current runtime state")
        return {"schema": "nx_component_topology_v1", "runtime_state": catalog.runtime_state, **component}

    def face_properties(self, session_id: str, face_ref: str) -> dict[str, Any]:
        catalog = self._catalog(session_id)
        face = catalog.faces.get(face_ref)
        if face is None:
            raise RuntimeCoordinatorError("face reference does not belong to the current runtime state")
        component_ids = [
            component_id for component_id, component in catalog.components.items()
            if face_ref in component["face_refs"]
        ]
        neighbors = list(catalog.adjacency.get(face_ref, []))
        neighbor_rows = []
        for neighbor_ref in neighbors:
            neighbor = catalog.faces.get(neighbor_ref)
            if neighbor is None:
                continue
            neighbor_rows.append({
                "face_ref": neighbor_ref,
                "name": neighbor.get("name") or None,
                "step_entity_id": neighbor.get("step_entity_id"),
                "surface_type": neighbor.get("surface_type"),
                "edge_count": neighbor.get("edge_count"),
            })
        return {
            "schema": "nx_face_properties_v1",
            "runtime_state": catalog.runtime_state,
            "face": {
                "face_ref": face_ref,
                "name": face.get("name") or None,
                "step_entity_id": face.get("step_entity_id"),
                "surface_type": face.get("surface_type"),
                "edge_count": face.get("edge_count"),
                "is_curved_candidate": any(face_ref in component["face_refs"] for component in catalog.components.values()),
                "component_ids": component_ids,
            },
            "adjacent_faces": neighbor_rows,
        }

    @staticmethod
    def _connected_groups(refs: list[str], adjacency: dict[str, list[str]]) -> list[list[str]]:
        allowed = set(refs)
        remaining = set(refs)
        groups = []
        while remaining:
            seed = min(remaining)
            remaining.remove(seed)
            stack = [seed]
            group = []
            while stack:
                ref = stack.pop()
                group.append(ref)
                for neighbor in sorted(set(adjacency.get(ref, [])) & remaining & allowed):
                    remaining.remove(neighbor)
                    stack.append(neighbor)
            groups.append(sorted(group))
        return groups

    def segment_component(self, session_id: str, component_id: str, method: str) -> dict[str, Any]:
        catalog = self._catalog(session_id)
        component = catalog.components.get(component_id)
        if component is None:
            raise RuntimeCoordinatorError("component does not belong to the current runtime state")
        refs = list(component["face_refs"])
        if method == "surface_type":
            groups = []
            for surface_type in sorted({catalog.faces[ref]["surface_type"] for ref in refs}):
                same_type = [ref for ref in refs if catalog.faces[ref]["surface_type"] == surface_type]
                groups.extend(self._connected_groups(same_type, catalog.adjacency))
        elif method == "bisection":
            midpoint = (len(refs) + 1) // 2
            groups = [refs[:midpoint], refs[midpoint:]]
        elif method in {"topology_branch", "simple_chains"}:
            branch = set(component["branch_face_refs"])
            non_branch = [ref for ref in refs if ref not in branch]
            groups = self._connected_groups(non_branch, catalog.adjacency)
            if method == "topology_branch":
                groups.extend([[ref] for ref in sorted(branch)])
            if not groups:
                groups = [refs]
        else:
            raise RuntimeCoordinatorError("unsupported segmentation method")
        groups = [group for group in groups if group]
        segments = []
        method_key = uuid.uuid5(uuid.NAMESPACE_URL, component_id + ":" + method).hex[:8]
        for index, group in enumerate(groups, start=1):
            segment_id = "{}-seg-{}-{:04d}".format(component_id, method_key, index)
            stable_identities = [
                str(catalog.faces[ref].get("name") or "tag:{}".format(catalog.faces[ref]["tag"]))
                for ref in group
            ]
            record = {
                "segment_id": segment_id, "component_id": component_id,
                "method": method, "face_refs": group, "face_count": len(group),
                "stable_face_identities": stable_identities,
            }
            catalog.segments[segment_id] = record
            segments.append(record)
        return {"schema": "nx_component_segments_v1", "runtime_state": catalog.runtime_state, "segments": segments}

    def _delete_refs(
        self, session_id: str, refs: list[str], method: str, preference: str,
        target: dict[str, Any], allow_related_faces: bool = False,
    ) -> dict[str, Any]:
        catalog = self._catalog(session_id)
        if method not in METHOD_TO_SELECT_TYPE:
            raise RuntimeCoordinatorError("unsupported deletion method")
        if preference not in {"cliff", "notch"}:
            raise RuntimeCoordinatorError("unsupported FaceEdgeBlend preference")
        if not refs or len(refs) > 500:
            raise RuntimeCoordinatorError("deletion target must contain 1 to 500 faces")
        missing = [ref for ref in refs if ref not in catalog.faces]
        if missing:
            raise RuntimeCoordinatorError("face references do not belong to the current runtime state")
        target_identity = tuple(sorted(
            str(catalog.faces[ref].get("name") or "tag:{}".format(catalog.faces[ref]["tag"]))
            for ref in refs
        ))
        attempt_key = (target_identity, method, preference)
        state_attempts = self._attempts.setdefault(session_id, {}).setdefault(
            catalog.runtime_state, set()
        )
        if attempt_key in state_attempts:
            raise RuntimeCoordinatorError(
                "this target, deletion method, and preference already failed in the current runtime state; choose a different strategy"
            )
        tags = [int(catalog.faces[ref]["tag"]) for ref in refs]
        result = self._execute(
            session_id, catalog.runtime_state, RuntimeMode.COMMIT,
            delete_code(tags, method, preference, allow_related_faces), "delete",
        )
        if result.get("state") == "ambiguous":
            raise RuntimeCoordinatorError(
                "delete outcome is ambiguous; reconcile operation {} before any retry".format(result.get("operation_id"))
            )
        committed = result.get("state") == "committed" and result.get("error") is None
        payload = {
            "schema": "nx_try_delete_result_v1",
            **target,
            "method": method,
            "preference": preference,
            "delete_partial_blend": False,
            "commit_succeeded": committed,
            "retained": bool(result.get("retained")),
            "rolled_back": bool(result.get("rolled_back")),
            "operation_id": result.get("operation_id"),
            "runtime_state_before": catalog.runtime_state,
            "runtime_state_after": result.get("result_state", catalog.runtime_state),
            "deletion_seconds": (
                (result.get("result") or {}).get("deletion_seconds")
                if committed
                else ((result.get("error") or {}).get("details") or {}).get("deletion_seconds")
            ),
            "error": result.get("error"),
            "attempted_face_identities": list(target_identity),
        }
        failed_attempt = (((result.get("error") or {}).get("details") or {}).get("deletion_attempt"))
        if failed_attempt:
            payload["worker_deletion_attempt"] = failed_attempt
        if committed:
            operation_id = result.get("operation_id")
            if operation_id:
                self._retained_attempts.setdefault(session_id, {})[operation_id] = attempt_key
            self._catalogs.pop(session_id, None)
            self._attempts.pop(session_id, None)
            payload["next_required_action"] = "reanalyze_current_state"
            payload["references_invalidated"] = True
        elif result.get("state") == "rollback_failed":
            payload["reset_required"] = True
        elif result.get("state") == "rolled_back":
            state_attempts.add(attempt_key)
            details = ((result.get("error") or {}).get("details") or {})
            if details.get("topology_change"):
                payload["topology_policy_rejected"] = True
                payload["topology_change"] = details["topology_change"]
        return payload

    def try_delete_face_group(
        self, session_id: str, refs: list[str], method: str, preference: str,
        allow_related_faces: bool = False,
    ) -> dict[str, Any]:
        catalog = self._catalog(session_id)
        if not refs or len(refs) > 500:
            raise RuntimeCoordinatorError("face group must contain 1 to 500 faces")
        if len(refs) != len(set(refs)):
            raise RuntimeCoordinatorError("face group references must be unique")
        if any(ref not in catalog.faces for ref in refs):
            raise RuntimeCoordinatorError("face references do not belong to the current runtime state")
        return self._delete_refs(
            session_id, refs, method, preference,
            {"face_group": True, "face_refs": list(refs), "allow_related_faces": bool(allow_related_faces)},
            bool(allow_related_faces),
        )

    def rollback_latest_delete(self, session_id: str) -> dict[str, Any]:
        result = self.runtime.rollback_latest_commit(session_id)
        if result.get("state") == "ambiguous":
            raise RuntimeCoordinatorError(
                "rollback outcome is ambiguous; reconcile operation {} before any retry".format(
                    result.get("operation_id")
                )
            )
        if result.get("state") != "committed":
            raise RuntimeCoordinatorError(
                "latest retained deletion could not be rolled back: {}".format(result.get("error"))
            )
        rolled_back_operation_id = (result.get("result") or {}).get("rolled_back_operation_id")
        rejected_attempt = self._retained_attempts.setdefault(session_id, {}).pop(
            rolled_back_operation_id, None
        )
        self._catalogs.pop(session_id, None)
        self._attempts.pop(session_id, None)
        if rejected_attempt is not None:
            self._attempts.setdefault(session_id, {}).setdefault(
                int(result.get("result_state", 0)), set()
            ).add(rejected_attempt)
        return {
            "schema": "nx_rollback_delete_result_v1",
            "rollback_succeeded": True,
            "operation_id": result.get("operation_id"),
            "rolled_back_operation_id": (result.get("result") or {}).get("rolled_back_operation_id"),
            "runtime_state_after": result.get("result_state"),
            "references_invalidated": True,
            "next_required_action": "reanalyze_current_state",
        }

    def try_delete_component(self, session_id: str, component_id: str, method: str, preference: str) -> dict[str, Any]:
        component = self.component_topology(session_id, component_id)
        return self._delete_refs(session_id, component["face_refs"], method, preference, {"component_id": component_id})

    def try_delete_segment(self, session_id: str, segment_id: str, method: str, preference: str) -> dict[str, Any]:
        catalog = self._catalog(session_id)
        segment = catalog.segments.get(segment_id)
        if segment is None:
            raise RuntimeCoordinatorError("segment does not belong to the current runtime state")
        return self._delete_refs(session_id, segment["face_refs"], method, preference, {"segment_id": segment_id, "component_id": segment["component_id"]})
