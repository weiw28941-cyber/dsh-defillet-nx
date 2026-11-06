# -*- coding: utf-8 -*-
r"""Grow a face set from a FreeCAD-style FaceN seed and delete it in NX.

The journal imports a temporary copy of the input STEP whose Nth ADVANCED_FACE
has a unique name.  It first tries the seed alone.  If deletion fails, it adds
one NX-recognized blend face from the selected set's shared-edge one-ring and
retries the complete set, up to ``--max-faces`` (maximum 50) and the global
``--max-runtime-seconds`` processing deadline.

Run with NX2406, for example:

    run_journal.exe cad/Defillet/nx_grow_delete_fillet_from_face.py -args \
      --input model.stp --output result.stp --seed-face Face496
"""

from __future__ import print_function

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid

import NXOpen
import NXOpen.Features


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# Reuse low-level, battle-tested NX geometry helpers only.  The existing high
# level delete functions are intentionally not used because they enforce a bbox
# gate and retain Face objects across undo operations.
from nx_1_defillet import (  # noqa: E402
    activate_defillet_context,
    blend_data,
    close_all_parts,
    close_part,
    collect_defillet_contexts,
    configure_delete_face_builder,
    destroy_builder,
    edge_faces,
    export_step,
    face_edge_points,
    face_geometry_key,
    face_stable_sort_key,
    open_step,
    part_display_name,
    safe_body_journal_identifier,
    safe_call,
    safe_face_journal_identifier,
    safe_face_name,
    safe_tag,
    safe_text,
    save_part_as,
)


DEFAULT_NX_BASE = os.path.join(os.environ.get("ProgramFiles", "C:/Program Files"), "Siemens", "NX2406")
DEFAULT_STEP214_DEF = os.environ.get("NX_STEP214_EXPORT_DEF") or os.path.join(
    os.environ.get("UGII_BASE_DIR") or DEFAULT_NX_BASE,
    "STEP214UG",
    "ugstep214.def",
)
MAX_ALLOWED_FACES = 50
SEED_NAME_PREFIX = "__NX_GROW_DELETE_SEED_"
ADVANCED_FACE_PATTERN = re.compile(
    rb"(#\s*(\d+)\s*=\s*ADVANCED_FACE\s*\(\s*')((?:[^']|'')*)(')",
    re.IGNORECASE | re.DOTALL,
)


def parse_seed_face(value):
    text = safe_text(value).strip()
    match = re.fullmatch(r"(?:Face)?([1-9][0-9]*)", text, re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(
            "seed face must be a positive FreeCAD-style index such as Face496 or 496"
        )
    return int(match.group(1))


def default_report_path(output_path):
    stem, _extension = os.path.splitext(output_path)
    return stem + ".grow_delete_report.json"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Grow an NX blend-face one-ring from a FreeCAD FaceN seed and delete it."
    )
    parser.add_argument("--input", required=True, help="Input STEP/STP file")
    parser.add_argument("--output", required=True, help="Successful output STEP/STP file")
    parser.add_argument(
        "--seed-face",
        type=parse_seed_face,
        help="Optional FreeCAD-style 1-based priority seed, for example Face496",
    )
    parser.add_argument(
        "--selection-point",
        nargs=3,
        type=float,
        metavar=("X", "Y", "Z"),
        help=(
            "Optional model-space point from Gui.Selection.addSelection. "
            "When supplied, the nearest NX face is used instead of STEP face order."
        ),
    )
    parser.add_argument("--selection-x", type=float, help="Selection point X; use --selection-x=-1 for negatives")
    parser.add_argument("--selection-y", type=float, help="Selection point Y; use --selection-y=-1 for negatives")
    parser.add_argument("--selection-z", type=float, help="Selection point Z; use --selection-z=-1 for negatives")
    parser.add_argument("--report", default="", help="JSON report path")
    parser.add_argument("--settings-file", default=DEFAULT_STEP214_DEF)
    parser.add_argument("--part-output", default="", help="Optional successful NX PRT output")
    parser.add_argument("--max-faces", type=int, default=MAX_ALLOWED_FACES)
    parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=600.0,
        help=(
            "Global processing time limit in seconds (default: 600). "
            "The journal stops starting new NX operations at the deadline."
        ),
    )
    phase1_group = parser.add_mutually_exclusive_group()
    phase1_group.add_argument(
        "--run-phase1",
        dest="skip_phase1",
        action="store_false",
        help="Run repeated single-face deletion before group growth.",
    )
    phase1_group.add_argument(
        "--skip-phase1",
        dest="skip_phase1",
        action="store_true",
        help="Skip repeated single-face deletion (default).",
    )
    parser.set_defaults(skip_phase1=True)
    parser.add_argument("--max-hole-diameter", default="5")
    parser.add_argument("--max-blend-radius", default="5")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args(argv)

    separate_point_values = (args.selection_x, args.selection_y, args.selection_z)
    has_any_separate_point = any(value is not None for value in separate_point_values)
    if args.selection_point and has_any_separate_point:
        parser.error("use either --selection-point or --selection-x/--selection-y/--selection-z")
    if has_any_separate_point:
        if not all(value is not None for value in separate_point_values):
            parser.error("--selection-x, --selection-y and --selection-z must be provided together")
        args.selection_point = list(separate_point_values)
    if args.selection_point and args.seed_face is None:
        parser.error("selection coordinates require --seed-face as their display label")

    args.input = os.path.abspath(args.input)
    args.output = os.path.abspath(args.output)
    args.settings_file = os.path.abspath(args.settings_file)
    args.report = os.path.abspath(args.report or default_report_path(args.output))
    if args.part_output:
        args.part_output = os.path.abspath(args.part_output)

    if not os.path.isfile(args.input):
        parser.error("input STEP does not exist: {}".format(args.input))
    if not os.path.isfile(args.settings_file):
        parser.error("STEP214 export settings do not exist: {}".format(args.settings_file))
    if os.path.normcase(args.input) == os.path.normcase(args.output):
        parser.error("input and output paths must differ")
    if args.max_faces < 1 or args.max_faces > MAX_ALLOWED_FACES:
        parser.error("--max-faces must be between 1 and {}".format(MAX_ALLOWED_FACES))
    if args.max_runtime_seconds <= 0:
        parser.error("--max-runtime-seconds must be positive")
    for path in (args.output, args.report, args.part_output):
        if not path:
            continue
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    if not args.overwrite:
        existing = [path for path in (args.output, args.report, args.part_output) if path and os.path.exists(path)]
        if existing:
            parser.error("output already exists; use --overwrite: {}".format(existing[0]))
    return args


def escape_step_string_bytes(text):
    return safe_text(text).replace("'", "''").encode("utf-8")


def make_marked_step(input_path, seed_index, temp_dir):
    with open(input_path, "rb") as stream:
        source = stream.read()

    matches = list(ADVANCED_FACE_PATTERN.finditer(source))
    if seed_index > len(matches):
        raise ValueError(
            "{} exceeds ADVANCED_FACE count {}".format(seed_index, len(matches))
        )

    match = matches[seed_index - 1]
    seed_name = "{}{}_{}__".format(SEED_NAME_PREFIX, seed_index, uuid.uuid4().hex)
    replacement = (
        source[: match.start(3)]
        + escape_step_string_bytes(seed_name)
        + source[match.end(3) :]
    )
    extension = os.path.splitext(input_path)[1] or ".step"
    marked_path = os.path.join(temp_dir, "marked_seed{}{}".format(seed_index, extension))
    with open(marked_path, "wb") as stream:
        stream.write(replacement)

    return {
        "path": marked_path,
        "name": seed_name,
        "face_index": seed_index,
        "step_entity_id": int(match.group(2)),
        "advanced_face_count": len(matches),
    }


def make_unmodified_step_copy(input_path, temp_dir):
    extension = os.path.splitext(input_path)[1] or ".step"
    copied_path = os.path.join(temp_dir, "selection_point_input{}".format(extension))
    shutil.copy2(input_path, copied_path)
    return copied_path


def cleanup_temp_point(session, point):
    try:
        mark = session.SetUndoMark(
            NXOpen.Session.MarkVisibility.Invisible,
            "delete grow-delete selection point",
        )
        try:
            session.UpdateManager.AddToDeleteList(point)
            session.UpdateManager.DoUpdate(mark)
        finally:
            try:
                session.DeleteUndoMark(mark, None)
            except Exception:
                pass
    except Exception:
        pass


def find_nearest_face_by_point(session, work_part, records, coordinates):
    point = NXOpen.Point3d(
        float(coordinates[0]),
        float(coordinates[1]),
        float(coordinates[2]),
    )
    point_object = work_part.Points.CreatePoint(point)
    candidates = []
    try:
        for record in records:
            try:
                distance, point_on_input, point_on_face, accuracy = (
                    session.Measurement.GetMinimumDistance(point_object, record["face"])
                )
            except Exception as exc:
                continue
            item = dict(record)
            item["selection_distance"] = float(distance)
            item["selection_accuracy"] = float(accuracy)
            item["point_on_input"] = [
                float(point_on_input.X),
                float(point_on_input.Y),
                float(point_on_input.Z),
            ]
            item["point_on_face"] = [
                float(point_on_face.X),
                float(point_on_face.Y),
                float(point_on_face.Z),
            ]
            candidates.append(item)
    finally:
        cleanup_temp_point(session, point_object)

    if not candidates:
        raise RuntimeError("NX could not measure the selection point against any face")
    candidates.sort(
        key=lambda record: (
            record["selection_distance"],
            face_stable_sort_key(record),
        )
    )
    return candidates[0], candidates[:5]


def json_safe(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {safe_text(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return safe_text(value)


def make_progress_logger(_session):
    """Write progress directly to run_journal stdout for VS Code terminals."""

    def progress(message=""):
        text = "[{}] {}".format(time.strftime("%H:%M:%S"), safe_text(message))
        print(text, flush=True)

    return progress


def candidate_stable_id(record, prefix="blend"):
    return "{}:{}".format(prefix, record["geometry_digest"][:16])


def try_resolve_locator(locator, records):
    try:
        return resolve_locator(locator, records)
    except Exception:
        return None


def write_report(path, report):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as stream:
        json.dump(json_safe(report), stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temp_path, path)


def geometry_digest(value):
    payload = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def collect_face_records(work_part):
    records = []
    for body_index, body in enumerate(list(work_part.Bodies), start=1):
        body_jid = safe_body_journal_identifier(body)
        for face_index, face in enumerate(list(body.GetFaces()), start=1):
            radius, is_blend, blend_error = blend_data(face)
            geom_key = face_geometry_key(face)
            record = {
                "face": face,
                "tag": safe_tag(face),
                "name": safe_face_name(face),
                "body": body,
                "body_index": body_index,
                "body_journal_identifier": body_jid,
                "face_index": face_index,
                "journal_identifier": safe_face_journal_identifier(face),
                "face_geometry_key": geom_key,
                "geometry_digest": geometry_digest(geom_key),
                "blend_radius": radius,
                "is_blend": bool(is_blend),
                "blend_error": blend_error,
            }
            records.append(record)
    return records


def public_face_record(record):
    return {
        "stable_id": record.get("stable_id"),
        "body_index": record.get("body_index"),
        "face_index": record.get("face_index"),
        "journal_identifier": record.get("journal_identifier"),
        "name": record.get("name"),
        "geometry_digest": record.get("geometry_digest"),
        "is_blend": record.get("is_blend"),
        "blend_radius": record.get("blend_radius"),
        "blend_error": record.get("blend_error"),
        "phase2_candidate_kind": record.get("phase2_candidate_kind"),
        "connected_blend_rule_created": record.get("connected_blend_rule_created"),
        "connected_blend_seed_retained": record.get("connected_blend_seed_retained"),
        "connected_blend_member_count": record.get("connected_blend_member_count"),
        "connected_blend_error": record.get("connected_blend_error"),
    }


def sorted_blend_records(records):
    result = [record for record in records if record.get("is_blend")]
    result.sort(key=face_stable_sort_key)
    return result


def connected_blend_evidence(work_part, face):
    """Probe one face with NX's broad connected-blend recognition rule."""
    options = None
    evidence = {
        "connected_blend_rule_created": False,
        "connected_blend_seed_retained": False,
        "connected_blend_member_count": 0,
        "connected_blend_error": "",
    }
    try:
        options = work_part.ScRuleFactory.CreateRuleOptions()
        options.SetSelectedFromInactive(False)
        rule = work_part.ScRuleFactory.CreateRuleFaceConnectedBlend(
            face,
            True,
            True,
            NXOpen.Features.Feature.Null,
            options,
        )
        evidence["connected_blend_rule_created"] = True
        collector = work_part.ScCollectors.CreateCollector()
        collector.ReplaceRules([rule], False)
        members = list(safe_call(lambda: collector.GetObjects(), []) or [])
        seed_tag = safe_tag(face)
        evidence["connected_blend_seed_retained"] = any(
            safe_tag(member) == seed_tag for member in members
        )
        evidence["connected_blend_member_count"] = len(members)
    except Exception as exc:
        evidence["connected_blend_error"] = safe_text(exc)
    finally:
        if options is not None:
            try:
                options.Dispose()
            except Exception:
                pass
    return evidence


def classify_phase2_candidates(work_part, records, user_seed_locator=None):
    """Probe every face independently with NX ConnectedBlend recognition."""
    candidates = []
    user_record = (
        try_resolve_locator(user_seed_locator, records)
        if user_seed_locator is not None
        else None
    )
    user_digest = user_record["geometry_digest"] if user_record is not None else None

    for record in records:
        evidence = connected_blend_evidence(work_part, record["face"])
        accepted = bool(
            evidence["connected_blend_rule_created"]
            and evidence["connected_blend_seed_retained"]
            and evidence["connected_blend_member_count"] > 0
        )
        if not accepted and record["geometry_digest"] != user_digest:
            continue
        item = dict(record)
        item.update(evidence)
        if record["geometry_digest"] == user_digest:
            item["phase2_candidate_kind"] = "user_seed"
        elif record.get("is_blend"):
            item["phase2_candidate_kind"] = "nx_get_blend_data"
        else:
            item["phase2_candidate_kind"] = "connected_blend_like"
        candidates.append(item)
    return candidates


def make_locator(record, stable_id=None):
    return {
        "stable_id": stable_id or record.get("stable_id") or "face:{}".format(record["geometry_digest"]),
        "seed_name": record.get("name") if safe_text(record.get("name")).startswith(SEED_NAME_PREFIX) else "",
        "body_index": record["body_index"],
        "body_journal_identifier": record.get("body_journal_identifier", ""),
        "face_index": record["face_index"],
        "journal_identifier": record.get("journal_identifier", ""),
        "geometry_digest": record["geometry_digest"],
        "phase2_candidate_kind": record.get("phase2_candidate_kind"),
        "get_blend_is_blend": bool(record.get("is_blend")),
        "connected_blend_rule_created": record.get("connected_blend_rule_created"),
        "connected_blend_seed_retained": record.get("connected_blend_seed_retained"),
        "connected_blend_member_count": record.get("connected_blend_member_count"),
    }


def resolve_locator(locator, records):
    seed_name = locator.get("seed_name")
    if seed_name:
        matches = [record for record in records if record.get("name") == seed_name]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError("seed name matched {} NX faces".format(len(matches)))

    matches = [
        record
        for record in records
        if record.get("geometry_digest") == locator.get("geometry_digest")
        and record.get("body_index") == locator.get("body_index")
    ]
    if len(matches) == 1:
        return matches[0]

    journal_identifier = locator.get("journal_identifier")
    jid_matches = [
        record
        for record in matches
        if record.get("journal_identifier") == journal_identifier
    ]
    if len(jid_matches) == 1:
        return jid_matches[0]

    raise RuntimeError(
        "cannot uniquely resolve {} (geometry matches={}, jid matches={})".format(
            locator.get("stable_id"), len(matches), len(jid_matches)
        )
    )


def resolve_locators(locators, records):
    return [resolve_locator(locator, records) for locator in locators]


def find_seed_record(records, seed_name):
    matches = [record for record in records if record.get("name") == seed_name]
    if len(matches) != 1:
        raise RuntimeError(
            "temporary seed name must match exactly one NX face; got {}".format(len(matches))
        )
    return matches[0]


def same_body(record_a, record_b):
    return record_a.get("body_index") == record_b.get("body_index")


def build_one_ring(selected_records, all_records, allowed_nonblend_digests=None):
    selected_tags = set(record["tag"] for record in selected_records)
    info_by_tag = {record["tag"]: record for record in all_records}
    allowed_nonblend_digests = set(allowed_nonblend_digests or [])
    shared_edge_counts = {}

    for selected in selected_records:
        for edge in list(safe_call(lambda face=selected["face"]: face.GetEdges(), []) or []):
            seen_on_edge = set()
            for neighbor_face in edge_faces(edge):
                neighbor_tag = safe_tag(neighbor_face)
                if neighbor_tag in selected_tags or neighbor_tag in seen_on_edge:
                    continue
                seen_on_edge.add(neighbor_tag)
                neighbor = info_by_tag.get(neighbor_tag)
                if neighbor is None or not same_body(selected, neighbor):
                    continue
                shared_edge_counts[neighbor_tag] = shared_edge_counts.get(neighbor_tag, 0) + 1

    candidates = []
    for tag, shared_edge_count in shared_edge_counts.items():
        record = info_by_tag[tag]
        digest = record["geometry_digest"]
        if not record.get("is_blend") and digest not in allowed_nonblend_digests:
            continue
        item = dict(record)
        item["shared_edge_count"] = shared_edge_count
        item["phase2_candidate_kind"] = (
            "nx_get_blend_data"
            if record.get("is_blend")
            else "connected_blend_like"
        )
        candidates.append(item)

    candidates.sort(
        key=lambda record: (
            -int(record.get("shared_edge_count") or 0),
            float(record.get("blend_radius")) if record.get("blend_radius") is not None else float("inf"),
            face_stable_sort_key(record),
        )
    )
    return candidates


def strategy_catalog():
    select_types = NXOpen.Features.DeleteFaceBuilder.SelectTypes
    preferences = NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions
    return [
        (
            "face_edge_blend.cliff.partial_off",
            select_types.FaceEdgeBlend,
            preferences.Cliff,
            False,
        ),
    ]


def undo_to_mark(session, mark, label):
    session.UndoToMark(mark, label)
    try:
        session.DeleteUndoMark(mark, None)
    except Exception:
        pass


def create_delete_builder(work_part, faces, args, strategy):
    _strategy_id, select_type, preference, delete_partial = strategy
    builder = work_part.Features.CreateDeleteFaceBuilder(NXOpen.Features.Feature.Null)
    configure_delete_face_builder(
        builder,
        args.max_hole_diameter,
        args.max_blend_radius,
        delete_partial_blend=delete_partial,
        select_type=select_type,
        face_edge_blend_preference=preference,
    )
    rule_options = work_part.ScRuleFactory.CreateRuleOptions()
    try:
        rule_options.SetSelectedFromInactive(False)
        face_rule = work_part.ScRuleFactory.CreateRuleFaceDumb(faces, rule_options)
    finally:
        rule_options.Dispose()
    builder.FaceCollector.ReplaceRules([face_rule], False)
    builder.FaceRecognized.CloneScope = 1535
    builder.FaceRecognized.SelectEntities(faces)
    builder.Type = select_type
    return builder


def verify_undo(work_part, selected_locators):
    records = collect_face_records(work_part)
    resolve_locators(selected_locators, records)
    return len(records)


def face_representative_point(record):
    journal_identifier = safe_text(record.get("journal_identifier"))
    match = re.search(
        r"\{\(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)",
        journal_identifier,
    )
    if match:
        return [float(match.group(index)) for index in (1, 2, 3)]

    points = face_edge_points(record["face"])
    if not points:
        return None
    return [
        sum(float(point[axis]) for point in points) / float(len(points))
        for axis in range(3)
    ]


def make_face_task(record, prefix="task"):
    return {
        "stable_id": candidate_stable_id(record, prefix),
        "anchor": face_representative_point(record),
        "blend_radius": record.get("blend_radius"),
        "original": public_face_record(record),
    }


def resolve_task_by_nearest_blend(session, work_part, task):
    blends = sorted_blend_records(collect_face_records(work_part))
    if not blends or not task.get("anchor"):
        return None, []
    nearest, nearest_records = find_nearest_face_by_point(
        session,
        work_part,
        blends,
        task["anchor"],
    )
    return nearest, nearest_records


def try_strategy(session, work_part, selected_locators, args, strategy, keep_success=False):
    strategy_id = strategy[0]
    label = "grow-delete {}".format(strategy_id)
    mark = session.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, label)
    builder = None
    committed = None
    error = ""
    undo_ok = False
    try:
        records = collect_face_records(work_part)
        selected_records = resolve_locators(selected_locators, records)
        faces = [record["face"] for record in selected_records]
        builder = create_delete_builder(work_part, faces, args, strategy)
        committed = builder.Commit()
        if committed is None:
            error = "builder.Commit() returned None"
        else:
            session.SetUndoMarkName(mark, label)
            return {
                "ok": True,
                "commit_ok": True,
                "target_removed": None,
                "residuals": [],
                "strategy": strategy_id,
                "error": "",
                "undo_ok": None,
                "committed": True,
            }
    except Exception as exc:
        error = safe_text(exc)
    finally:
        destroy_builder(builder)

    try:
        undo_to_mark(session, mark, label)
        records_after_undo = collect_face_records(work_part)
        resolve_locators(selected_locators, records_after_undo)
        undo_ok = True
    except Exception as exc:
        undo_error = safe_text(exc)
        if error:
            error = "{}; undo failed: {}".format(error, undo_error)
        else:
            error = "undo failed: {}".format(undo_error)

    return {
        "ok": False,
        "commit_ok": False,
        "target_removed": None,
        "residuals": [],
        "strategy": strategy_id,
        "error": error,
        "undo_ok": undo_ok,
        "committed": False,
    }


def probe_face_set(
    session,
    work_part,
    selected_locators,
    args,
    log,
    report_attempts,
    phase="phase2",
    epoch=0,
    seed_id="",
):
    for strategy in strategy_catalog():
        result = try_strategy(
            session,
            work_part,
            selected_locators,
            args,
            strategy,
            keep_success=False,
        )
        report_attempts.append(
            {
                "phase": phase,
                "epoch": epoch,
                "seed_id": seed_id,
                "selected_count": len(selected_locators),
                "selected_ids": [locator["stable_id"] for locator in selected_locators],
                "strategy": result["strategy"],
                "ok": result["ok"],
                "commit_ok": result["commit_ok"],
                "target_removed": result["target_removed"],
                "residuals": result["residuals"],
                "undo_ok": result["undo_ok"],
                "error": result["error"],
            }
        )
        log(
            "{} epoch={} seed={} selected={} commit={} complete={} undo={} error={}".format(
                phase,
                epoch,
                seed_id,
                len(selected_locators),
                result["commit_ok"],
                result["target_removed"],
                result["undo_ok"],
                result["error"],
            )
        )
        if not result["undo_ok"]:
            raise RuntimeError("fatal undo failure: {}".format(result["error"]))
        if result["ok"]:
            return result["strategy"]
    return None


def final_commit(session, work_part, selected_locators, args, preferred_strategy, log, report_attempts, phase="phase2", epoch=0, seed_id=""):
    strategies = strategy_catalog()
    strategies.sort(key=lambda strategy: 0 if strategy[0] == preferred_strategy else 1)
    for strategy in strategies:
        result = try_strategy(
            session,
            work_part,
            selected_locators,
            args,
            strategy,
            keep_success=True,
        )
        report_attempts.append(
            {
                "phase": phase,
                "operation": "final_commit",
                "epoch": epoch,
                "seed_id": seed_id,
                "selected_count": len(selected_locators),
                "selected_ids": [locator["stable_id"] for locator in selected_locators],
                "strategy": result["strategy"],
                "ok": result["ok"],
                "commit_ok": result["commit_ok"],
                "target_removed": result["target_removed"],
                "residuals": result["residuals"],
                "undo_ok": result["undo_ok"],
                "error": result["error"],
            }
        )
        log(
            "{} FINAL epoch={} seed={} selected={} ok={} complete={} error={}".format(
                phase,
                epoch,
                seed_id,
                len(selected_locators),
                result["ok"],
                result["target_removed"],
                result["error"],
            )
        )
        if result["ok"]:
            return result["strategy"]
        if not result["undo_ok"]:
            raise RuntimeError("fatal undo failure during final commit: {}".format(result["error"]))
    return None


def permanently_delete_set(session, work_part, locators, args, log, report, phase, epoch, seed_id):
    strategy = strategy_catalog()[0]
    result = try_strategy(
        session,
        work_part,
        locators,
        args,
        strategy,
        keep_success=True,
    )
    report["attempts"].append(
        {
            "phase": phase,
            "operation": "direct_commit",
            "epoch": epoch,
            "seed_id": seed_id,
            "selected_count": len(locators),
            "selected_ids": [locator["stable_id"] for locator in locators],
            "strategy": result["strategy"],
            "ok": result["ok"],
            "commit_ok": result["commit_ok"],
            "undo_ok": result["undo_ok"],
            "error": result["error"],
            "permanent": result["ok"],
        }
    )
    log(
        "{} DIRECT epoch={} seed={} selected={} commit={} permanent={} undo={} error={}".format(
            phase,
            epoch,
            seed_id,
            len(locators),
            result["commit_ok"],
            result["ok"],
            result["undo_ok"],
            result["error"],
        )
    )
    if not result["ok"] and not result["undo_ok"]:
        raise RuntimeError("fatal undo failure: {}".format(result["error"]))
    return result["strategy"] if result["ok"] else None


def grow_from_seed(
    session,
    work_part,
    seed_locator,
    args,
    log,
    report,
    state,
    epoch,
    allowed_nonblend_digests=None,
    distance_from_last_failed_seed=None,
):
    selected = [seed_locator]
    seed_id = seed_locator["stable_id"]
    seed_kind = seed_locator.get("phase2_candidate_kind") or "nx_get_blend_data"
    run_entry = {
        "epoch": epoch,
        "seed_id": seed_id,
        "candidate_kind": seed_kind,
        "get_blend_is_blend": seed_locator.get("get_blend_is_blend"),
        "distance_from_last_failed_seed": distance_from_last_failed_seed,
        "rounds": [],
        "success": False,
    }
    if seed_kind == "connected_blend_like":
        report["phase2"]["relaxed_candidates_tested"] += 1
    report["phase2"]["seed_runs"].append(run_entry)

    while len(selected) <= args.max_faces:
        if time.monotonic() >= state["deadline"]:
            run_entry["stop_reason"] = "time_limit_reached"
            log("PHASE2 STOP seed={}: global time limit reached".format(seed_id))
            return None, selected
        round_entry = {
            "selected_count": len(selected),
            "selected_ids": [locator["stable_id"] for locator in selected],
        }
        run_entry["rounds"].append(round_entry)
        remaining_seconds = max(0.0, state["deadline"] - time.monotonic())
        log(
            "PHASE2 epoch={} seed={} set={}/{} failed_seeds={} remaining_seconds={:.1f}".format(
                epoch,
                seed_id,
                len(selected),
                args.max_faces,
                report["phase2"]["failed_seed_count"],
                remaining_seconds,
            )
        )
        strategy = permanently_delete_set(
            session,
            work_part,
            selected,
            args,
            log,
            report,
            "phase2",
            epoch,
            seed_id,
        )
        round_entry["strategy"] = strategy
        if strategy:
            run_entry["success"] = True
            run_entry["selected_count"] = len(selected)
            run_entry["selected_faces"] = selected
            run_entry["strategy"] = strategy
            if seed_kind == "connected_blend_like":
                report["phase2"]["relaxed_candidates_deleted"] += 1
            return True, selected
        if len(selected) >= args.max_faces:
            run_entry["stop_reason"] = "max_faces_reached"
            return False, selected

        records = collect_face_records(work_part)
        selected_records = resolve_locators(selected, records)
        candidates = build_one_ring(
            selected_records,
            records,
            allowed_nonblend_digests=allowed_nonblend_digests,
        )
        round_entry["frontier_count"] = len(candidates)
        if not candidates:
            run_entry["stop_reason"] = "frontier_exhausted"
            return False, selected
        candidate = candidates[0]
        locator = make_locator(
            candidate,
            stable_id="{}:neighbor{:02d}:{}".format(
                seed_id, len(selected), candidate["geometry_digest"][:12]
            ),
        )
        selected.append(locator)
        round_entry["added_face"] = dict(
            public_face_record(dict(candidate, stable_id=locator["stable_id"])),
            shared_edge_count=candidate["shared_edge_count"],
        )
        log(
            "PHASE2 add seed={} face={} radius={} shared_edges={} next_set={}".format(
                seed_id,
                locator["stable_id"],
                candidate.get("blend_radius"),
                candidate["shared_edge_count"],
                len(selected),
            )
        )
    return False, selected


def wait_for_nonempty_file(path, timeout=60.0):
    deadline = time.time() + timeout
    last_size = -1
    stable_count = 0
    while time.time() < deadline:
        if os.path.isfile(path):
            size = os.path.getsize(path)
            if size > 0 and size == last_size:
                stable_count += 1
                if stable_count >= 2:
                    return size
            else:
                stable_count = 0
            last_size = size
        time.sleep(0.25)
    raise RuntimeError("output file missing, empty, or unstable: {}".format(path))


def cleanup_path(path, warnings):
    if not path or not os.path.exists(path):
        return
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except Exception as exc:
        warnings.append("cannot remove {}: {}".format(path, safe_text(exc)))


def try_clear_face_name(face):
    try:
        face.SetName("")
        return True
    except Exception:
        return False


def activate_context_records(session, context):
    active_part = activate_defillet_context(session, context)
    if active_part is None:
        return None, []
    return active_part, collect_face_records(active_part)


def scan_body_contexts(session, display_part):
    rows = []
    for index, context in enumerate(collect_defillet_contexts(display_part), start=1):
        active_part, records = activate_context_records(session, context)
        if active_part is None or not records:
            continue
        rows.append(
            {
                "index": index,
                "context": context,
                "part": active_part,
                "records": records,
                "face_count": len(records),
                "blend_count": len(sorted_blend_records(records)),
                "label": context.get("label"),
                "part_name": part_display_name(active_part),
            }
        )
    return rows


def run_single_blend_phase(session, work_part, args, log, report, state):
    phase = report["phase1"]
    while True:
        if time.monotonic() >= state["deadline"]:
            phase["stop_reason"] = "time_limit_reached"
            log("PHASE1 STOP: global time limit reached")
            return
        records = collect_face_records(work_part)
        blends = sorted_blend_records(records)
        tasks = [make_face_task(record, "single") for record in blends]
        phase["round_count"] += 1
        round_number = phase["round_count"]
        round_entry = {
            "round": round_number,
            "initial_candidate_count": len(tasks),
            "processed_count": 0,
            "committed_count": 0,
            "failed_count": 0,
            "tasks": [],
        }
        phase.setdefault("rounds", []).append(round_entry)
        log(
            "PHASE1 round={} epoch={} initial_blends={} permanent_commits={}".format(
                round_number,
                state["epoch"],
                len(tasks),
                state["permanent_commits"],
            )
        )
        if not tasks:
            phase["stop_reason"] = "no_remaining_blends"
            return

        for index, task in enumerate(tasks, start=1):
            if time.monotonic() >= state["deadline"]:
                phase["stop_reason"] = "time_limit_reached"
                log("PHASE1 STOP: global time limit reached")
                return
            current_record, nearest_records = resolve_task_by_nearest_blend(
                session,
                work_part,
                task,
            )
            task_entry = {
                "task_id": task["stable_id"],
                "anchor": task.get("anchor"),
                "original_radius": task.get("blend_radius"),
            }
            round_entry["tasks"].append(task_entry)
            round_entry["processed_count"] += 1
            if current_record is None:
                task_entry["outcome"] = "no_remaining_blend"
                log(
                    "PHASE1 candidate={}/{} id={} locate=missing; continue".format(
                        index, len(tasks), task["stable_id"]
                    )
                )
                continue

            distance = current_record.get("selection_distance")
            current_id = "{}:current:{}".format(
                task["stable_id"], current_record["geometry_digest"][:12]
            )
            locator = make_locator(current_record, stable_id=current_id)
            task_entry["resolved"] = dict(
                public_face_record(dict(current_record, stable_id=current_id)),
                selection_distance=distance,
                nearest_count=len(nearest_records),
            )
            log(
                "PHASE1 candidate={}/{} id={} locate=nearest distance={} radius={}".format(
                    index,
                    len(tasks),
                    task["stable_id"],
                    distance,
                    current_record.get("blend_radius"),
                )
            )
            strategy = permanently_delete_set(
                session,
                work_part,
                [locator],
                args,
                log,
                report,
                "phase1",
                state["epoch"],
                task["stable_id"],
            )
            if not strategy:
                phase["failed_single_attempts"] += 1
                round_entry["failed_count"] += 1
                task_entry["outcome"] = "commit_failed_rolled_back"
                continue

            state["epoch"] += 1
            state["permanent_commits"] += 1
            state["deleted_face_targets"] += 1
            phase["successful_single_deletions"] += 1
            round_entry["committed_count"] += 1
            task_entry["outcome"] = "committed"
            phase["deleted"].append(
                {
                    "epoch": state["epoch"],
                    "task_id": task["stable_id"],
                    "resolved_face": public_face_record(
                        dict(current_record, stable_id=current_id)
                    ),
                    "strategy": strategy,
                }
            )
            log(
                "PHASE1 COMMIT success task={} new_epoch={} total_commits={}; continue same round".format(
                    task["stable_id"],
                    state["epoch"],
                    state["permanent_commits"],
                )
            )

        remaining = len(sorted_blend_records(collect_face_records(work_part)))
        round_entry["remaining_after_round"] = remaining
        log(
            "PHASE1 ROUND END round={} committed={} failed={} remaining={}".format(
                round_number,
                round_entry["committed_count"],
                round_entry["failed_count"],
                remaining,
            )
        )
        if remaining == 0:
            phase["stop_reason"] = "no_remaining_blends"
            return
        if round_entry["committed_count"] == 0:
            phase["stop_reason"] = "full_round_without_success"
            log("PHASE1 STOP: full round completed without a successful commit")
            return


def point_distance(point_a, point_b):
    if point_a is None or point_b is None:
        return None
    return sum(
        (float(point_a[axis]) - float(point_b[axis])) ** 2
        for axis in range(3)
    ) ** 0.5


def ordered_phase2_seeds(
    session,
    work_part,
    records,
    candidates,
    user_seed_locator,
    selection_point,
    last_failed_seed_point=None,
):
    blends = [record for record in candidates if record.get("is_blend")]
    relaxed_candidates = [
        record for record in candidates if not record.get("is_blend")
    ]
    ordered = []
    seen = set()

    if last_failed_seed_point is not None:
        # Keep confirmed blends ahead of relaxed ConnectedBlend candidates,
        # while moving away from the most recently failed seed in each class.
        distance_key = lambda record: (
            -(
                point_distance(
                    face_representative_point(record), last_failed_seed_point
                )
                or 0.0
            ),
            -int(record.get("connected_blend_member_count") or 0),
            face_stable_sort_key(record),
        )
        blends.sort(key=distance_key)
        relaxed_candidates.sort(key=distance_key)
    else:
        if user_seed_locator is not None:
            user_record = try_resolve_locator(user_seed_locator, records)
            if user_record is not None:
                ordered.append(user_record)
                seen.add(user_record["geometry_digest"])

        if selection_point and blends:
            _nearest, nearest = find_nearest_face_by_point(
                session,
                work_part,
                blends,
                selection_point,
            )
            distance_by_digest = {
                item["geometry_digest"]: item.get("selection_distance", float("inf"))
                for item in nearest
            }
            blends.sort(
                key=lambda record: (
                    distance_by_digest.get(record["geometry_digest"], float("inf")),
                    face_stable_sort_key(record),
                )
            )
        relaxed_candidates.sort(
            key=lambda record: (
                -int(record.get("connected_blend_member_count") or 0),
                face_stable_sort_key(record),
            )
        )

    for record in blends + relaxed_candidates:
        if record["geometry_digest"] in seen:
            continue
        ordered.append(record)
        seen.add(record["geometry_digest"])
    return ordered


def run_group_growth_phase(
    session,
    work_part,
    args,
    log,
    report,
    state,
    user_seed_locator=None,
):
    phase = report["phase2"]
    failed_seed_count = int(phase.get("failed_seed_count", 0))
    last_failed_seed_point = state.get("last_failed_seed_point")

    while True:
        if time.monotonic() >= state["deadline"]:
            phase["stop_reason"] = "time_limit_reached"
            phase["failed_seed_count"] = failed_seed_count
            log("PHASE2 STOP: global time limit reached")
            return
        records = collect_face_records(work_part)
        blends = sorted_blend_records(records)
        candidates = classify_phase2_candidates(
            work_part, records, user_seed_locator
        )
        relaxed_candidates = [
            record for record in candidates if not record.get("is_blend")
        ]
        if not candidates:
            phase["stop_reason"] = "no_remaining_candidates"
            phase["failed_seed_count"] = failed_seed_count
            return
        allowed_nonblend_digests = {
            record["geometry_digest"] for record in relaxed_candidates
        }
        phase.setdefault("candidate_scans", []).append(
            {
                "epoch": state["epoch"],
                "probed_face_count": len(records),
                "recognized_blend_count": len(blends),
                "connected_blend_like_count": len(relaxed_candidates),
                "total_candidate_count": len(candidates),
            }
        )
        log(
            "PHASE2 SCAN epoch={} all_faces_probed={} nx_blends={} "
            "connected_blend_like={} candidates={}".format(
                state["epoch"],
                len(records),
                len(blends),
                len(relaxed_candidates),
                len(candidates),
            )
        )

        exhausted = state.setdefault("exhausted_seed_digests", set())
        seeds = [
            record
            for record in ordered_phase2_seeds(
                session,
                work_part,
                records,
                candidates,
                user_seed_locator,
                args.selection_point,
                last_failed_seed_point,
            )
            if record["geometry_digest"] not in exhausted
        ]
        if not seeds:
            phase["stop_reason"] = "all_remaining_seeds_exhausted"
            phase["failed_seed_count"] = failed_seed_count
            return

        seed_record = seeds[0]
        seed_point = face_representative_point(seed_record)
        distance_from_last_failed = point_distance(seed_point, last_failed_seed_point)
        seed_id = candidate_stable_id(seed_record, "group_seed")
        seed_locator = make_locator(seed_record, stable_id=seed_id)
        phase["failed_seed_count"] = failed_seed_count
        log(
            "PHASE2 seed={} kind={} epoch={} remaining_blends={} candidates={} failed_seeds={} "
            "distance_from_last_failed={} remaining_seconds={:.1f}".format(
                seed_id,
                seed_record.get("phase2_candidate_kind"),
                state["epoch"],
                len(blends),
                len(candidates),
                failed_seed_count,
                distance_from_last_failed,
                max(0.0, state["deadline"] - time.monotonic()),
            )
        )
        success, selected = grow_from_seed(
            session,
            work_part,
            seed_locator,
            args,
            log,
            report,
            state,
            state["epoch"],
            allowed_nonblend_digests,
            distance_from_last_failed,
        )
        if success is None:
            phase["stop_reason"] = "time_limit_reached"
            phase["failed_seed_count"] = failed_seed_count
            return
        if success:
            state["epoch"] += 1
            state["permanent_commits"] += 1
            state["deleted_face_targets"] += len(selected)
            phase["successful_group_deletions"] += 1
            phase["failed_seed_count"] = failed_seed_count
            state["exhausted_seed_digests"] = set()
            state["last_failed_seed_point"] = None
            last_failed_seed_point = None
            log(
                "PHASE2 COMMIT success seed={} selected={} new_epoch={} failed_seeds={}; rescanning".format(
                    seed_id,
                    len(selected),
                    state["epoch"],
                    failed_seed_count,
                )
            )
            continue

        exhausted.add(seed_record["geometry_digest"])
        failed_seed_count += 1
        if seed_record.get("phase2_candidate_kind") == "connected_blend_like":
            phase["relaxed_candidates_failed"] += 1
        last_failed_seed_point = seed_point
        state["last_failed_seed_point"] = seed_point
        phase["failed_seed_count"] = failed_seed_count
        log(
            "PHASE2 seed failed id={} attempted_set={} failed_seeds={}; "
            "next seed will maximize distance".format(
                seed_id,
                len(selected),
                failed_seed_count,
            )
        )


def run(args):
    run_started = time.monotonic()
    deadline = run_started + args.max_runtime_seconds
    session = NXOpen.Session.GetSession()
    log = make_progress_logger(session)
    temp_dir = tempfile.mkdtemp(prefix="nx_grow_delete_")
    marked = None
    work_part = None
    user_seed_locator = None
    report = {
        "schema_version": 2,
        "success": False,
        "processing_status": "failed",
        "validation_policy": "commit_only_failure_undo",
        "strategy": "face_edge_blend.cliff.partial_off",
        "input": args.input,
        "output": args.output,
        "report": args.report,
        "limits": {
            "max_faces": args.max_faces,
            "max_runtime_seconds": args.max_runtime_seconds,
        },
        "timing": {
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed_seconds": 0.0,
            "time_limit_reached": False,
        },
        "seed": None,
        "initial_scan": {},
        "phase1": {
            "enabled": not args.skip_phase1,
            "round_count": 0,
            "successful_single_deletions": 0,
            "failed_single_attempts": 0,
            "deleted": [],
            "stop_reason": "",
        },
        "phase2": {
            "seed_runs": [],
            "candidate_scans": [],
            "successful_group_deletions": 0,
            "failed_seed_count": 0,
            "relaxed_candidates_tested": 0,
            "relaxed_candidates_deleted": 0,
            "relaxed_candidates_failed": 0,
            "stop_reason": "",
        },
        "attempts": [],
        "remaining_blends": [],
        "summary": {},
        "cleanup_warnings": [],
        "errors": [],
    }
    state = {
        "epoch": 0,
        "permanent_commits": 0,
        "deleted_face_targets": 0,
        "exhausted_seed_digests": set(),
        "last_failed_seed_point": None,
        "deadline": deadline,
    }

    try:
        if args.overwrite:
            for path in (args.output, args.report, args.part_output):
                if path:
                    cleanup_path(path, report["cleanup_warnings"])

        if args.selection_point:
            input_for_nx = make_unmodified_step_copy(args.input, temp_dir)
            report["mapping"] = {
                "method": "nx_nearest_face_to_selection_point",
                "selection_point": list(args.selection_point),
            }
        elif args.seed_face is not None:
            marked = make_marked_step(args.input, args.seed_face, temp_dir)
            input_for_nx = marked["path"]
            report["mapping"] = {
                "method": "temporary_advanced_face_name",
                "advanced_face_count": marked["advanced_face_count"],
                "step_advanced_face_entity": marked["step_entity_id"],
                "temporary_name": marked["name"],
            }
        else:
            input_for_nx = make_unmodified_step_copy(args.input, temp_dir)
            report["mapping"] = {"method": "no_user_seed"}

        log("START input={}".format(args.input))
        log("START output={}".format(args.output))
        log(
            "START strategy={} phase1={} max_faces={} time_limit_seconds={} optional_seed={}".format(
                report["strategy"],
                "skipped" if args.skip_phase1 else "enabled",
                args.max_faces,
                args.max_runtime_seconds,
                "Face{}".format(args.seed_face) if args.seed_face is not None else "none",
            )
        )
        close_all_parts(session, log=log)
        log("IMPORT opening STEP copy: {}".format(input_for_nx))
        work_part = open_step(session, input_for_nx)
        display_part = work_part
        context_rows = scan_body_contexts(session, display_part)
        if not context_rows:
            raise RuntimeError(
                "NX imported the STEP but no body-owning part/context with faces was found"
            )

        total_initial_faces = sum(row["face_count"] for row in context_rows)
        total_initial_blends = sum(row["blend_count"] for row in context_rows)
        report["initial_scan"] = {
            "context_count": len(context_rows),
            "face_count": total_initial_faces,
            "blend_count": total_initial_blends,
            "contexts": [
                {
                    "index": row["index"],
                    "label": row["label"],
                    "part": row["part_name"],
                    "face_count": row["face_count"],
                    "blend_count": row["blend_count"],
                }
                for row in context_rows
            ],
        }
        log(
            "IMPORT complete contexts={} faces={} nx_blends={}".format(
                len(context_rows), total_initial_faces, total_initial_blends
            )
        )

        seed_context_index = None
        if args.seed_face is not None:
            seed_matches = []
            for row in context_rows:
                active_part, records = activate_context_records(session, row["context"])
                if args.selection_point:
                    seed_record, nearest_records = find_nearest_face_by_point(
                        session, active_part, records, args.selection_point
                    )
                    seed_matches.append(
                        (seed_record.get("selection_distance", float("inf")), row, seed_record, nearest_records)
                    )
                else:
                    matches = [record for record in records if record.get("name") == marked["name"]]
                    if len(matches) == 1:
                        seed_matches.append((0.0, row, matches[0], []))
            if not seed_matches:
                raise RuntimeError("optional user seed could not be resolved in any NX component context")
            seed_matches.sort(key=lambda item: item[0])
            _distance, seed_row, seed_record, nearest_records = seed_matches[0]
            seed_context_index = seed_row["index"]
            if not args.selection_point:
                try_clear_face_name(seed_record["face"])
            user_seed_locator = make_locator(
                seed_record,
                stable_id="user_seed:Face{}".format(args.seed_face),
            )
            user_seed_locator["seed_name"] = ""
            report["seed"] = dict(
                public_face_record(dict(seed_record, stable_id=user_seed_locator["stable_id"])),
                requested="Face{}".format(args.seed_face),
                context_index=seed_context_index,
                selection_point=list(args.selection_point) if args.selection_point else None,
            )
            if nearest_records:
                report["mapping"]["nearest_faces"] = [
                    dict(
                        public_face_record(record),
                        selection_distance=record["selection_distance"],
                        point_on_face=record["point_on_face"],
                    )
                    for record in nearest_records
                ]
            log(
                "SEED resolved context={} id={} blend={} radius={} jid={}".format(
                    seed_context_index,
                    user_seed_locator["stable_id"],
                    seed_record["is_blend"],
                    seed_record["blend_radius"],
                    seed_record["journal_identifier"],
                )
            )

        report["contexts"] = []
        all_remaining_blends = []
        for context_position, row in enumerate(context_rows, start=1):
            active_part, context_records = activate_context_records(session, row["context"])
            if active_part is None or not context_records:
                continue
            context_report = {
                "index": row["index"],
                "label": row["label"],
                "part": row["part_name"],
                "initial_face_count": len(context_records),
                "initial_blend_count": len(sorted_blend_records(context_records)),
            }
            report["contexts"].append(context_report)
            log(
                "CONTEXT {}/{} index={} part={} faces={} blends={}".format(
                    context_position,
                    len(context_rows),
                    row["index"],
                    row["part_name"],
                    context_report["initial_face_count"],
                    context_report["initial_blend_count"],
                )
            )

            if args.skip_phase1:
                report["phase1"]["stop_reason"] = "skipped_by_default"
                log("PHASE1 SKIP context={}: disabled by default".format(row["index"]))
            else:
                run_single_blend_phase(session, active_part, args, log, report, state)

            records_after_phase1 = collect_face_records(active_part)
            context_seed_locator = (
                user_seed_locator if seed_context_index == row["index"] else None
            )
            if context_seed_locator is not None:
                report["seed"]["status_after_phase1"] = (
                    "remaining"
                    if try_resolve_locator(context_seed_locator, records_after_phase1) is not None
                    else "deleted_or_remapped_in_phase1"
                )

            state["exhausted_seed_digests"] = set()
            state["last_failed_seed_point"] = None
            if time.monotonic() < state["deadline"]:
                run_group_growth_phase(
                    session,
                    active_part,
                    args,
                    log,
                    report,
                    state,
                    user_seed_locator=context_seed_locator,
                )
            else:
                report["phase2"]["stop_reason"] = "time_limit_reached"
                log("PHASE2 SKIP context={}: global time limit reached".format(row["index"]))
            final_context_records = collect_face_records(active_part)
            context_remaining = sorted_blend_records(final_context_records)
            context_report["final_face_count"] = len(final_context_records)
            context_report["final_blend_count"] = len(context_remaining)
            all_remaining_blends.extend(
                dict(public_face_record(record), context_index=row["index"])
                for record in context_remaining
            )
            log(
                "CONTEXT RESULT index={} final_faces={} remaining_blends={}".format(
                    row["index"], len(final_context_records), len(context_remaining)
                )
            )

        report["remaining_blends"] = all_remaining_blends
        if not all_remaining_blends:
            status = "complete"
        elif state["permanent_commits"] > 0:
            status = "partial"
        else:
            status = "unchanged"
        report["processing_status"] = status
        report["summary"] = {
            "context_count": len(context_rows),
            "topology_epochs": state["epoch"],
            "permanent_commit_count": state["permanent_commits"],
            "deleted_face_target_count": state["deleted_face_targets"],
            "initial_face_count": total_initial_faces,
            "initial_blend_count": total_initial_blends,
            "final_blend_count": len(all_remaining_blends),
        }

        try:
            session.Parts.SetWork(display_part)
        except Exception as exc:
            log("EXPORT warning: could not restore display work part: {}".format(safe_text(exc)))

        if args.part_output:
            log("EXPORT saving NX part: {}".format(args.part_output))
            save_part_as(display_part, args.part_output)

        temporary_output = args.output + ".tmp.stp"
        cleanup_path(temporary_output, report["cleanup_warnings"])
        log("EXPORT writing temporary STEP: {}".format(temporary_output))
        export_step(session, display_part, temporary_output, args.settings_file)
        output_size = wait_for_nonempty_file(temporary_output)
        cleanup_path(args.output, report["cleanup_warnings"])
        os.replace(temporary_output, args.output)

        report["success"] = True
        report["export"] = {"succeeded": True, "size": output_size}
        log(
            "RESULT status={} commits={} deleted_targets={} remaining_blends={} output={}".format(
                status,
                state["permanent_commits"],
                state["deleted_face_targets"],
                len(all_remaining_blends),
                args.output,
            )
        )
        log("RESULT report={}".format(args.report))
        return True, report
    except Exception as exc:
        report["processing_status"] = "failed"
        report["errors"].append(safe_text(exc))
        report["traceback"] = traceback.format_exc()
        log("RESULT failed error={}".format(safe_text(exc)))
        cleanup_path(args.output, report["cleanup_warnings"])
        cleanup_path(args.output + ".tmp.stp", report["cleanup_warnings"])
        if args.part_output:
            cleanup_path(args.part_output, report["cleanup_warnings"])
        return False, report
    finally:
        elapsed_seconds = time.monotonic() - run_started
        report["timing"]["elapsed_seconds"] = round(elapsed_seconds, 3)
        report["timing"]["time_limit_reached"] = elapsed_seconds >= args.max_runtime_seconds
        report["timing"]["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if report.get("summary"):
            report["summary"]["elapsed_seconds"] = round(elapsed_seconds, 3)
        try:
            write_report(args.report, report)
        except Exception as exc:
            log("RESULT report_write_failed={}".format(safe_text(exc)))
        if work_part is not None and not args.keep_open:
            try:
                close_part(session, work_part)
            except Exception as exc:
                report["cleanup_warnings"].append("close part failed: {}".format(safe_text(exc)))
        if not args.no_cleanup:
            cleanup_path(temp_dir, report["cleanup_warnings"])
        try:
            write_report(args.report, report)
        except Exception:
            pass


def main(argv=None):
    args = parse_args(argv)
    success, _report = run(args)
    if not success:
        # run_journal treats SystemExit as a syntax error, so report failure via
        # output/log rather than raising SystemExit.
        return False
    return True


if __name__ == "__main__":
    main()
