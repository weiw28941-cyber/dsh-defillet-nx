# -*- coding: utf-8 -*-
"""Remove every deletable NX curved-face component in one journal process."""

from __future__ import print_function

import argparse
import json
import os
import sys
import traceback

import NXOpen
import NXOpen.Features

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from nx_web_classify_fillet_faces import TAGGED_FACE_NAME, classify_face, parse_step_faces  # noqa: E402
from nx_1_defillet import (  # noqa: E402
    activate_defillet_context,
    close_all_parts,
    close_part,
    cleanup_step_import_sidecars,
    collect_defillet_contexts,
    configure_delete_face_builder,
    destroy_builder,
    edge_faces,
    export_step,
    open_step,
    safe_body_journal_identifier,
    safe_face_name,
    safe_tag,
    safe_text,
)

REQUEST_SCHEMA = "nx_remove_all_fillets_v1"
RESULT_SCHEMA = "nx_remove_all_fillets_result_v2"
PREFERENCES = {"cliff", "notch"}
INFRASTRUCTURE_MARKERS = (
    "unable to reserve license", "license server machine is down", "license checkout",
    "flexnet licensing error", "failed to initialize ufun", "general fault exception",
    "fatal error detected",
)


class InfrastructureError(RuntimeError):
    """An error after which the NX process must fail rather than continue."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    args.request = os.path.abspath(args.request)
    return args


def write_report(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


def absolute_request_path(request, key):
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("request field {!r} must be a nonempty path".format(key))
    return os.path.abspath(value)


def load_request(path):
    with open(path, "r", encoding="utf-8") as stream:
        request = json.load(stream)
    if request.get("schema") != REQUEST_SCHEMA:
        raise RuntimeError("unsupported request schema")

    input_path = absolute_request_path(request, "input")
    output_path = absolute_request_path(request, "output")
    report_path = absolute_request_path(request, "report")
    settings_file = absolute_request_path(request, "settings_file")
    if not os.path.isfile(input_path):
        raise RuntimeError("input STEP does not exist: {}".format(input_path))
    if not os.path.isfile(settings_file):
        raise RuntimeError("STEP settings file does not exist: {}".format(settings_file))
    if os.path.normcase(input_path) == os.path.normcase(output_path):
        raise RuntimeError("input and output must differ")
    if os.path.normcase(output_path) == os.path.normcase(report_path):
        raise RuntimeError("output and report must differ")

    preference = request.get("preference", "cliff")
    if preference not in PREFERENCES:
        raise RuntimeError("preference must be 'cliff' or 'notch'")
    return request, input_path, output_path, report_path, settings_file, preference


def remove_if_present(path):
    if os.path.isfile(path):
        os.remove(path)


def model_snapshot(session, contexts):
    """Capture an inexpensive invariant used to verify every failed rollback."""
    parts = []
    for context_index, context in enumerate(contexts, start=1):
        part = activate_defillet_context(session, context)
        if part is None:
            continue
        bodies = []
        for body_index, body in enumerate(list(part.Bodies), start=1):
            faces = []
            for face in list(body.GetFaces()):
                try:
                    edge_count = len(list(face.GetEdges()))
                except Exception:
                    edge_count = -1
                faces.append((safe_face_name(face), safe_text(face.SolidFaceType), edge_count))
            bodies.append((body_index, safe_body_journal_identifier(body), tuple(sorted(faces))))
        parts.append((context_index, tuple(bodies)))
    return tuple(parts)


def _current_components(rows, faces_by_tag):
    row_by_tag = {row["tag"]: row for row in rows}
    remaining = set(row_by_tag)
    adjacency = {tag: set() for tag in remaining}
    for tag, face in faces_by_tag.items():
        for edge in face.GetEdges():
            for neighbor in edge_faces(edge):
                neighbor_tag = safe_tag(neighbor)
                if neighbor_tag in adjacency and neighbor_tag != tag:
                    adjacency[tag].add(neighbor_tag)
                    adjacency[neighbor_tag].add(tag)
    groups = []
    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        stack, tags = [seed], []
        while stack:
            tag = stack.pop()
            tags.append(tag)
            for neighbor in sorted(adjacency[tag] & remaining, reverse=True):
                remaining.remove(neighbor)
                stack.append(neighbor)
        groups.append([row_by_tag[tag] for tag in sorted(tags)])
    groups.sort(key=lambda group: min(row["tag"] for row in group))
    return groups, adjacency


def detect_components(session, contexts, round_number):
    rows = []
    faces_by_tag = {}
    owner_by_tag = {}
    seen_step_ids = set()

    for context_index, context in enumerate(contexts, start=1):
        part = activate_defillet_context(session, context)
        if part is None:
            continue
        for body_index, body in enumerate(list(part.Bodies), start=1):
            body_jid = safe_body_journal_identifier(body)
            for face_index, face in enumerate(list(body.GetFaces()), start=1):
                match = TAGGED_FACE_NAME.match(safe_face_name(face))
                step_id = int(match.group(1)) if match else None
                if step_id is not None and step_id in seen_step_ids:
                    raise InfrastructureError("duplicate NX face for original STEP ID {}".format(step_id))
                if step_id is not None:
                    seen_step_ids.add(step_id)
                classified = classify_face(face)
                if not classified["selectable"]:
                    continue
                row = {
                    "context_index": context_index,
                    "body_index": body_index,
                    "body_journal_identifier": body_jid,
                    "face_index": face_index,
                    "tag": safe_tag(face),
                    "name": safe_face_name(face),
                    "step_entity_id": step_id,
                    "face": face,
                    "part": part,
                }
                row.update(classified)
                rows.append(row)
                faces_by_tag[row["tag"]] = face
                owner_by_tag[row["tag"]] = part

    groups, adjacency = _current_components(rows, faces_by_tag)
    components = []
    for index, group in enumerate(groups, start=1):
        tags = [row["tag"] for row in group]
        part_tags = set(safe_tag(owner_by_tag[tag]) for tag in tags)
        if len(part_tags) != 1:
            raise InfrastructureError("one connected component spans multiple NX work parts")
        components.append({
            "component_id": "round-{:03d}-fc-{:03d}".format(round_number, index),
            "step_entity_ids": sorted(row["step_entity_id"] for row in group if row["step_entity_id"] is not None),
            "faces": [row["face"] for row in group],
            "part": group[0]["part"],
            "face_count": len(group),
            "adjacency_edge_count": sum(len(adjacency[tag]) for tag in tags) // 2,
        })
    return components


def validate_initial_mapping(session, contexts, expected_ids):
    actual_ids = []
    untagged_count = 0
    for context in contexts:
        part = activate_defillet_context(session, context)
        if part is None:
            continue
        for body in list(part.Bodies):
            for face in list(body.GetFaces()):
                match = TAGGED_FACE_NAME.match(safe_face_name(face))
                if match:
                    actual_ids.append(int(match.group(1)))
                else:
                    untagged_count += 1
    duplicates = sorted(set(value for value in actual_ids if actual_ids.count(value) > 1))
    actual_set = set(actual_ids)
    errors = {
        "untagged_nx_face_count": untagged_count,
        "duplicate_step_entity_ids": duplicates,
        "missing_step_entity_ids": sorted(expected_ids - actual_set),
        "unexpected_step_entity_ids": sorted(actual_set - expected_ids),
    }
    if untagged_count or duplicates or actual_set != expected_ids or len(actual_ids) != len(expected_ids):
        raise RuntimeError("NX did not preserve every injected STEP face name: {}".format(errors))


def resolve_faces(session, contexts, step_ids):
    wanted = set(step_ids)
    found = {}
    owners = {}
    for context in contexts:
        part = activate_defillet_context(session, context)
        if part is None:
            continue
        for body in list(part.Bodies):
            for face in list(body.GetFaces()):
                match = TAGGED_FACE_NAME.match(safe_face_name(face))
                if not match:
                    continue
                step_id = int(match.group(1))
                if step_id not in wanted:
                    continue
                if step_id in found:
                    raise InfrastructureError("duplicate NX face for original STEP ID {}".format(step_id))
                found[step_id] = face
                owners[step_id] = part
    missing = sorted(wanted - set(found))
    if missing:
        raise InfrastructureError("could not resolve current faces for original STEP IDs {}".format(missing))
    part_tags = set(safe_tag(owners[step_id]) for step_id in wanted)
    if len(part_tags) != 1:
        raise ValueError("selected components belong to different NX work parts")
    first_id = min(wanted)
    return owners[first_id], [found[step_id] for step_id in sorted(wanted)]


def current_original_ids(session, contexts):
    result = set()
    for context in contexts:
        part = activate_defillet_context(session, context)
        if part is None:
            continue
        for body in list(part.Bodies):
            for face in list(body.GetFaces()):
                match = TAGGED_FACE_NAME.match(safe_face_name(face))
                if match:
                    result.add(int(match.group(1)))
    return result


def undo_failed_attempt(session, mark, label, contexts, before_snapshot):
    try:
        session.UndoToMark(mark, label)
    except Exception as exc:
        raise InfrastructureError("failed to roll back geometry attempt: {}".format(safe_text(exc)))
    try:
        session.DeleteUndoMark(mark, None)
    except Exception:
        pass
    after_snapshot = model_snapshot(session, contexts)
    if after_snapshot != before_snapshot:
        raise InfrastructureError("failed geometry attempt changed the in-memory model after rollback")


def attempt_delete(session, contexts, component_batch, preference):
    component_ids = [component["component_id"] for component in component_batch]
    step_ids = sorted(set(
        step_id for component in component_batch for step_id in component["step_entity_ids"]
    ))
    part_tags = set(safe_tag(component["part"]) for component in component_batch)
    if len(part_tags) != 1:
        return False, "selected components belong to different NX work parts", step_ids
    part = component_batch[0]["part"]
    faces = [face for component in component_batch for face in component["faces"]]
    before_snapshot = model_snapshot(session, contexts)
    # NX imposes a short limit on Undo Mark names. Component details belong in
    # the JSON attempt report, never in this fixed-size transaction label.
    label = "fillet delete"
    mark = session.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, label)
    builder = None
    try:
        try:
            session.Parts.SetWork(part)
            preference_enum = (
                NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions.Notch
                if preference == "notch"
                else NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions.Cliff
            )
            builder = part.Features.CreateDeleteFaceBuilder(NXOpen.Features.Feature.Null)
            configure_delete_face_builder(
                builder,
                "5",
                "5",
                delete_partial_blend=False,
                select_type=NXOpen.Features.DeleteFaceBuilder.SelectTypes.FaceEdgeBlend,
                face_edge_blend_preference=preference_enum,
            )
            options = part.ScRuleFactory.CreateRuleOptions()
            try:
                options.SetSelectedFromInactive(False)
                rule = part.ScRuleFactory.CreateRuleFaceDumb(faces, options)
            finally:
                options.Dispose()
            builder.FaceCollector.ReplaceRules([rule], False)
            builder.FaceRecognized.CloneScope = 1535
            builder.FaceRecognized.SelectEntities(faces)
            committed = builder.Commit()
            if committed is None:
                raise ValueError("DeleteFaceBuilder.Commit returned None")
        except InfrastructureError:
            destroy_builder(builder)
            builder = None
            undo_failed_attempt(session, mark, label, contexts, before_snapshot)
            raise
        except Exception as exc:
            destroy_builder(builder)
            builder = None
            undo_failed_attempt(session, mark, label, contexts, before_snapshot)
            message = safe_text(exc)
            if any(marker in message.lower() for marker in INFRASTRUCTURE_MARKERS):
                raise InfrastructureError(message)
            return False, message, step_ids

        after_snapshot = model_snapshot(session, contexts)
        if after_snapshot == before_snapshot:
            destroy_builder(builder)
            builder = None
            undo_failed_attempt(session, mark, label, contexts, before_snapshot)
            return False, "DeleteFaceBuilder committed without changing the model", step_ids
        session.SetUndoMarkName(mark, "fillet done")
        try:
            session.DeleteUndoMark(mark, None)
        except Exception:
            pass
        return True, "", step_ids
    finally:
        destroy_builder(builder)


def component_public(component):
    return {
        "component_id": component["component_id"],
        "face_count": component["face_count"],
        "step_entity_ids": list(component["step_entity_ids"]),
        "adjacency_edge_count": component["adjacency_edge_count"],
    }


def count_current_faces(session, contexts):
    count = 0
    for context in contexts:
        part = activate_defillet_context(session, context)
        if part is None:
            continue
        for body in list(part.Bodies):
            count += len(list(body.GetFaces()))
    return count


def final_roundtrip_normalize(session, display_part, output_path, settings_file):
    """Normalize the accumulated result once, in this NX process, before publish."""
    raw_output_path = output_path + ".pre_roundtrip.stp"
    normalized_part = None
    remove_if_present(raw_output_path)
    session.Parts.SetWork(display_part)
    export_step(session, display_part, raw_output_path, settings_file)
    if not os.path.isfile(raw_output_path) or os.path.getsize(raw_output_path) <= 0:
        raise InfrastructureError("pre-roundtrip STEP export failed")

    raw_step_faces = parse_step_faces(raw_output_path)
    raw_face_count = len(raw_step_faces)
    if raw_face_count <= 0:
        raise RuntimeError("pre-roundtrip STEP contains no ADVANCED_FACE entities")

    try:
        close_part(session, display_part)
        cleanup_step_import_sidecars(
            raw_output_path, log=lambda message: print(message, flush=True)
        )
        normalized_part = open_step(session, raw_output_path)
        normalized_contexts = collect_defillet_contexts(normalized_part)
        if not normalized_contexts:
            raise InfrastructureError(
                "NX roundtrip import found no body-owning context"
            )
        nx_face_count = count_current_faces(session, normalized_contexts)
        if nx_face_count <= 0:
            raise RuntimeError("NX roundtrip import contains no faces")

        session.Parts.SetWork(normalized_part)
        remove_if_present(output_path)
        export_step(session, normalized_part, output_path, settings_file)
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise InfrastructureError("normalized STEP export failed")
        output_face_count = len(parse_step_faces(output_path))
        if output_face_count != nx_face_count:
            raise RuntimeError(
                "normalized STEP face count {} does not match NX roundtrip face count {}".format(
                    output_face_count, nx_face_count
                )
            )
        return normalized_part, {
            "performed": True,
            "same_process": True,
            "scope": "final-accumulated-result",
            "reimport_count": 1,
            "pre_roundtrip_step_face_count": raw_face_count,
            "nx_roundtrip_face_count": nx_face_count,
            "normalized_output_step_face_count": output_face_count,
            "face_count_delta": nx_face_count - raw_face_count,
        }
    except Exception:
        if normalized_part is not None:
            try:
                close_part(session, normalized_part)
            except Exception:
                pass
        raise


def run(request_path):
    (_request, input_path, output_path, report_path, settings_file,
     preference) = load_request(request_path)
    remove_if_present(output_path)

    report = {
        "schema": RESULT_SCHEMA,
        "success": False,
        "input": input_path,
        "rounds": [],
        "attempts": [],
        "successful_batches": [],
        "terminal_reason": None,
        "deleted_step_entity_ids": [],
        "roundtrip_normalization": None,
    }
    session = NXOpen.Session.GetSession()
    display_part = None
    try:
        close_all_parts(session, log=lambda message: print(message, flush=True))
        cleanup_step_import_sidecars(input_path, log=lambda message: print(message, flush=True))
        display_part = open_step(session, input_path)
        contexts = collect_defillet_contexts(display_part)
        if not contexts:
            raise InfrastructureError("NX imported STEP but found no body-owning context")

        step_faces = parse_step_faces(input_path)
        expected_ids = set(row["step_entity_id"] for row in step_faces)
        if not expected_ids or len(expected_ids) != len(step_faces):
            raise RuntimeError("input STEP has missing or duplicate ADVANCED_FACE entity IDs")
        validate_initial_mapping(session, contexts, expected_ids)
        original_remaining = current_original_ids(session, contexts)

        round_number = 0
        while True:
            round_number += 1
            components = detect_components(session, contexts, round_number)
            round_row = {
                "round": round_number,
                "components": [component_public(component) for component in components],
                "attempts": [],
            }
            report["rounds"].append(round_row)
            if not components:
                report["terminal_reason"] = "no_components"
                round_row["terminal_reason"] = "no_components"
                break

            pending = [components]
            round_succeeded = False
            while pending:
                batch = pending.pop()
                attempt_number = len(report["attempts"]) + 1
                attempt = {
                    "attempt": attempt_number,
                    "round": round_number,
                    "component_ids": [component["component_id"] for component in batch],
                    "step_entity_ids": sorted(set(
                        step_id for component in batch for step_id in component["step_entity_ids"]
                    )),
                }
                ok, error, requested_ids = attempt_delete(
                    session, contexts, batch, preference
                )
                if ok:
                    remaining = current_original_ids(session, contexts)
                    actually_deleted = sorted(original_remaining - remaining)
                    original_remaining = remaining
                    attempt.update(status="succeeded", deleted_step_entity_ids=actually_deleted)
                    successful = {
                        "round": round_number,
                        "attempt": attempt_number,
                        "component_ids": list(attempt["component_ids"]),
                        "requested_step_entity_ids": requested_ids,
                        "deleted_step_entity_ids": actually_deleted,
                    }
                    report["successful_batches"].append(successful)
                    report["deleted_step_entity_ids"] = sorted(
                        set(report["deleted_step_entity_ids"]) | set(actually_deleted)
                    )
                    round_succeeded = True
                else:
                    attempt.update(status="geometry_failure", error=error)
                round_row["attempts"].append(attempt)
                report["attempts"].append(attempt)

                if ok:
                    break
                if len(batch) > 1:
                    middle = len(batch) // 2
                    # LIFO stack: append right first so the left half is attempted first.
                    pending.append(batch[middle:])
                    pending.append(batch[:middle])

            if not round_succeeded:
                report["terminal_reason"] = "no_deletable_components"
                round_row["terminal_reason"] = "no_deletable_components"
                break

        if report["successful_batches"]:
            output_directory = os.path.dirname(output_path)
            if output_directory:
                os.makedirs(output_directory, exist_ok=True)
            display_part, normalization = final_roundtrip_normalize(
                session, display_part, output_path, settings_file
            )
            report["roundtrip_normalization"] = normalization
            report["success"] = True
            report["output"] = output_path
        else:
            remove_if_present(output_path)
            report["success"] = False

        write_report(report_path, report)
        print("RESULT rounds={} attempts={} batches={} deleted={} terminal={} report={}".format(
            len(report["rounds"]), len(report["attempts"]),
            len(report["successful_batches"]), len(report["deleted_step_entity_ids"]),
            report["terminal_reason"], report_path,
        ), flush=True)
        return True
    except Exception as exc:
        remove_if_present(output_path)
        report.update(
            success=False,
            terminal_reason="process_failure",
            failure_category="infrastructure" if isinstance(exc, InfrastructureError) else "validation",
            error=safe_text(exc),
        )
        try:
            write_report(report_path, report)
        except Exception:
            pass
        print("ERROR {}".format(safe_text(exc)), flush=True)
        print(traceback.format_exc(), flush=True)
        # The structured report is the protocol result; reserve a nonzero exit
        # for failures where no report can be published.
        return os.path.isfile(report_path) and os.path.getsize(report_path) > 0
    finally:
        if display_part is not None:
            try:
                close_part(session, display_part)
            except Exception:
                pass
        remove_if_present(output_path + ".pre_roundtrip.stp")


def main(argv=None):
    args = parse_args(argv)
    return run(args.request)


if __name__ == "__main__":
    if not main():
        raise SystemExit(1)
