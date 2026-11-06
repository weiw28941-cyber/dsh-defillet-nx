# -*- coding: utf-8 -*-
"""Open STEP in NX, recognize blend-like faces globally, color them, export STEP.

Every NX Face independently seeds a FaceConnectedBlend rule with blend-like and
unlabelled-blend inclusion enabled. No OCC remapping or adjacency-to-known-blend
eligibility heuristic is involved.
"""

from __future__ import print_function

import argparse
import json
import os
import re
import sys
import traceback

import NXOpen


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from nx_1_defillet import (  # noqa: E402
    activate_defillet_context,
    blend_data,
    close_all_parts,
    close_part,
    collect_defillet_contexts,
    export_step,
    open_step,
    part_display_name,
    safe_body_journal_identifier,
    safe_face_journal_identifier,
    safe_face_name,
    safe_text,
    safe_tag,
    save_part_as,
)


DEFAULT_NX_BASE = os.path.join(os.environ.get("ProgramFiles", "C:/Program Files"), "Siemens", "NX2406")
DEFAULT_STEP214_DEF = os.environ.get("NX_STEP214_EXPORT_DEF") or os.path.join(
    os.environ.get("UGII_BASE_DIR") or DEFAULT_NX_BASE,
    "STEP214UG",
    "ugstep214.def",
)
NX_RED_COLOR = 186
DISPLAY_CHUNK_SIZE = 500
ADVANCED_FACE_PATTERN = re.compile(
    r"#\s*(\d+)\s*=\s*ADVANCED_FACE\s*\(", re.IGNORECASE | re.DOTALL
)


def parse_step_face_ids(path):
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        text = stream.read()
    return [int(match.group(1)) for match in ADVANCED_FACE_PATTERN.finditer(text)]


def load_target_step_ids(path):
    if not path:
        return None, None
    with open(path, "r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    if not isinstance(payload, list):
        raise ValueError("target STEP IDs JSON must be an array")
    if all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in payload):
        return set(payload), None
    targets = {}
    for value in payload:
        if not isinstance(value, dict):
            raise ValueError("target entries must be positive IDs or descriptor objects")
        step_id = value.get("step_entity_id")
        point = value.get("pick_point")
        if (
            isinstance(step_id, bool)
            or not isinstance(step_id, int)
            or step_id <= 0
            or not isinstance(point, list)
            or len(point) != 3
        ):
            raise ValueError("target descriptor requires step_entity_id and 3D pick_point")
        targets[step_id] = [float(component) for component in point]
    return set(targets), targets


def load_target_face_names(path):
    if not path:
        return None
    with open(path, "r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    if not isinstance(payload, list) or any(
        not isinstance(value, str) or not value for value in payload
    ):
        raise ValueError("target face names JSON must be an array of nonempty strings")
    return set(payload)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Probe every NX face with FaceConnectedBlend(includeBlendLike=True, "
            "includeUnlabeledBlend=True), color accepted seeds, and export AP214 STEP."
        )
    )
    parser.add_argument("--input", required=True, help="Input STEP")
    parser.add_argument("--output", required=True, help="Colored STEP output")
    parser.add_argument("--report", default="", help="Optional JSON report")
    parser.add_argument("--part-output", default="", help="Optional colored NX PRT")
    parser.add_argument("--settings-file", default=DEFAULT_STEP214_DEF)
    parser.add_argument(
        "--target-step-ids-json",
        default="",
        help="Legacy geometric target descriptor JSON",
    )
    parser.add_argument(
        "--target-face-names-json",
        default="",
        help="JSON array of exact imported NX face names",
    )
    parser.add_argument("--color", type=int, default=NX_RED_COLOR)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)

    args.input = os.path.abspath(args.input)
    args.output = os.path.abspath(args.output)
    args.settings_file = os.path.abspath(args.settings_file)
    args.report = os.path.abspath(
        args.report or (os.path.splitext(args.output)[0] + "_report.json")
    )
    if args.part_output:
        args.part_output = os.path.abspath(args.part_output)
    if args.target_step_ids_json:
        args.target_step_ids_json = os.path.abspath(args.target_step_ids_json)
    if args.target_face_names_json:
        args.target_face_names_json = os.path.abspath(args.target_face_names_json)

    if not os.path.isfile(args.input):
        parser.error("input STEP does not exist: {}".format(args.input))
    if not os.path.isfile(args.settings_file):
        parser.error("STEP export settings do not exist: {}".format(args.settings_file))
    if args.target_step_ids_json and not os.path.isfile(args.target_step_ids_json):
        parser.error(
            "target STEP IDs JSON does not exist: {}".format(args.target_step_ids_json)
        )
    if args.target_face_names_json and not os.path.isfile(args.target_face_names_json):
        parser.error(
            "target face names JSON does not exist: {}".format(args.target_face_names_json)
        )
    if args.target_step_ids_json and args.target_face_names_json:
        parser.error("choose only one explicit target JSON mode")
    if os.path.normcase(args.input) == os.path.normcase(args.output):
        parser.error("input and output paths must differ")
    if args.color < 1 or args.color > 216:
        parser.error("--color must be an NX color index between 1 and 216")

    outputs = [args.output, args.report]
    if args.part_output:
        outputs.append(args.part_output)
    existing = [path for path in outputs if os.path.exists(path)]
    if existing and not args.overwrite:
        parser.error("outputs exist; pass --overwrite: {}".format(existing))
    for path in outputs:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    return args


def log(message):
    print(str(message), flush=True)


def face_record(
    face, body_index, face_index, body_journal_identifier, radius, is_blend, error
):
    return {
        "tag": safe_tag(face),
        "body_index": body_index,
        "face_index": face_index,
        "body_journal_identifier": body_journal_identifier,
        "journal_identifier": safe_face_journal_identifier(face),
        "name": safe_face_name(face),
        "blend_radius": radius,
        "is_blend": bool(is_blend),
        "blend_error": error,
    }


def collector_objects(collector):
    try:
        return list(collector.GetObjects())
    except Exception:
        return []


def probe_connected_blend_candidate(part, face):
    """Return NX ConnectedBlend recognition evidence for one independent seed."""
    options = None
    collector = None
    rule = None
    evidence = {
        "connected_blend_rule_created": False,
        "connected_blend_seed_retained": False,
        "connected_blend_member_count": 0,
        "connected_blend_member_journal_identifiers": [],
        "connected_blend_error": "",
    }
    try:
        options = part.ScRuleFactory.CreateRuleOptions()
        options.SetSelectedFromInactive(False)
        rule = part.ScRuleFactory.CreateRuleFaceConnectedBlend(
            face,
            True,
            True,
            NXOpen.Features.Feature.Null,
            options,
        )
        evidence["connected_blend_rule_created"] = True
        collector = part.ScCollectors.CreateCollector()
        collector.ReplaceRules([rule], False)
        members = collector_objects(collector)
        seed_tag = safe_tag(face)
        evidence["connected_blend_seed_retained"] = any(
            safe_tag(member) == seed_tag for member in members
        )
        evidence["connected_blend_member_count"] = len(members)
        evidence["connected_blend_member_journal_identifiers"] = [
            safe_face_journal_identifier(member) for member in members
        ]
    except Exception as exc:
        evidence["connected_blend_error"] = safe_text(exc)
    finally:
        # Rule options are explicitly disposable. Collector/rule lifetimes are
        # owned by NX and end with the work part; do not retain either object.
        if options is not None:
            try:
                options.Dispose()
            except Exception:
                pass
    return evidence


def nearest_face_for_point(session, part, point):
    point_object = part.Points.CreatePoint(NXOpen.Point3d(*point))
    best = None
    try:
        for body_index, body in enumerate(list(part.Bodies), start=1):
            body_journal_identifier = safe_body_journal_identifier(body)
            for face_index, face in enumerate(list(body.GetFaces()), start=1):
                try:
                    distance, _input_point, point_on_face, accuracy = (
                        session.Measurement.GetMinimumDistance(point_object, face)
                    )
                except Exception:
                    continue
                row = (
                    float(distance), face, body_index, face_index,
                    body_journal_identifier, point_on_face, float(accuracy),
                )
                if best is None or row[0] < best[0]:
                    best = row
    finally:
        try:
            point_object.Destroy()
        except Exception:
            pass
    return best


def scan_part_by_pick_points(session, part, target_points):
    targets = []
    used_tags = set()
    for step_entity_id in sorted(target_points):
        best = nearest_face_for_point(session, part, target_points[step_entity_id])
        if best is None:
            raise RuntimeError("could not map STEP face #{} by pick point".format(step_entity_id))
        distance, face, body_index, face_index, body_jid, point_on_face, accuracy = best
        tag = safe_tag(face)
        if tag in used_tags:
            raise RuntimeError(
                "multiple STEP face IDs mapped to one NX face: #{}".format(step_entity_id)
            )
        used_tags.add(tag)
        record = {
            "tag": tag,
            "body_index": body_index,
            "face_index": face_index,
            "body_journal_identifier": body_jid,
            "journal_identifier": safe_face_journal_identifier(face),
            "name": safe_face_name(face),
            "step_entity_id": step_entity_id,
            "selected_by_step_id": True,
            "mapping": "OCC interior point to nearest NX face",
            "pick_point": target_points[step_entity_id],
            "distance": distance,
            "accuracy": accuracy,
            "point_on_face": [point_on_face.X, point_on_face.Y, point_on_face.Z],
        }
        targets.append((face, record))
    return [record for _face, record in targets], targets


def scan_part(part, progress_log=None):
    records = []
    targets = []
    all_faces = []
    for body_index, body in enumerate(list(part.Bodies), start=1):
        body_journal_identifier = safe_body_journal_identifier(body)
        for face_index, face in enumerate(list(body.GetFaces()), start=1):
            all_faces.append((body_index, face_index, body_journal_identifier, face))

    for probe_index, item in enumerate(all_faces, start=1):
        body_index, face_index, body_journal_identifier, face = item
        radius, is_blend, error = blend_data(face)
        record = face_record(
            face,
            body_index,
            face_index,
            body_journal_identifier,
            radius,
            is_blend,
            error,
        )
        evidence = probe_connected_blend_candidate(part, face)
        record.update(evidence)
        record["connected_blend_candidate"] = bool(
            evidence["connected_blend_rule_created"]
            and evidence["connected_blend_seed_retained"]
            and evidence["connected_blend_member_count"] > 0
        )
        records.append(record)
        if record["connected_blend_candidate"]:
            targets.append((face, record))
        if progress_log is not None and (
            probe_index == 1 or probe_index % 25 == 0 or probe_index == len(all_faces)
        ):
            progress_log(
                "PROBE faces={}/{} candidates={}".format(
                    probe_index, len(all_faces), len(targets)
                )
            )
    return records, targets


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def apply_face_color(session, faces, color):
    changed = 0
    for face_chunk in chunks(list(faces), DISPLAY_CHUNK_SIZE):
        modification = session.DisplayManager.NewDisplayModification()
        try:
            modification.ApplyToAllFaces = False
            modification.NewColor = int(color)
            modification.NewTranslucency = 0
            modification.Apply(face_chunk)
            changed += len(face_chunk)
        finally:
            try:
                modification.Dispose()
            except Exception:
                pass
    return changed


def write_report(path, report):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


def cleanup_output(path):
    if path and os.path.isfile(path):
        os.remove(path)


def run(args):
    session = NXOpen.Session.GetSession()
    display_part = None
    target_step_ids, target_points = load_target_step_ids(args.target_step_ids_json)
    target_face_names = load_target_face_names(args.target_face_names_json)
    report = {
        "success": False,
        "input": args.input,
        "output": args.output,
        "part_output": args.part_output or None,
        "nx_color_index": args.color,
        "classification": (
            "exact injected ADVANCED_FACE names"
            if target_face_names is not None
            else "explicit ADVANCED_FACE STEP entity IDs by geometric point"
            if target_step_ids is not None
            else "FaceConnectedBlendRule(includeBlendLike=True, includeUnlabeledBlend=True) "
            "with nonempty collector retaining the independently probed seed"
        ),
        "target_step_entity_ids": (
            sorted(target_step_ids) if target_step_ids is not None else None
        ),
        "contexts": [],
        "total_face_count": 0,
        "probed_face_count": 0,
        "get_blend_data_confirmed_count": 0,
        "connected_blend_candidate_count": 0,
        "connected_blend_only_count": 0,
        "colored_blend_face_count": 0,
        "errors": [],
    }

    try:
        if args.overwrite:
            cleanup_output(args.output)
            cleanup_output(args.report)
            cleanup_output(args.part_output)

        close_all_parts(session, log=log)
        log("IMPORT {}".format(args.input))
        display_part = open_step(session, args.input)
        contexts = collect_defillet_contexts(display_part)
        if not contexts:
            raise RuntimeError("no body-owning NX context found")

        matched_step_ids = set()
        for context_index, context in enumerate(contexts, start=1):
            active_part = activate_defillet_context(session, context)
            if active_part is None:
                continue
            if target_face_names is not None:
                records = []
                targets = []
                for body_index, body in enumerate(list(active_part.Bodies), start=1):
                    body_jid = safe_body_journal_identifier(body)
                    for face_index, face in enumerate(list(body.GetFaces()), start=1):
                        name = safe_text(face.Name)
                        record = {
                            "tag": safe_tag(face), "body_index": body_index,
                            "face_index": face_index,
                            "body_journal_identifier": body_jid,
                            "journal_identifier": safe_face_journal_identifier(face),
                            "name": name, "selected_by_exact_name": name in target_face_names,
                        }
                        records.append(record)
                        if name in target_face_names:
                            targets.append((face, record))
                matched_names = set(record["name"] for _face, record in targets)
                missing_names = sorted(target_face_names - matched_names)
                if missing_names:
                    raise RuntimeError("target imported face names not found: {}".format(missing_names))
                log("SELECT context={} matched_named_faces={}".format(context_index, len(targets)))
            elif target_step_ids is None:
                records, targets = scan_part(active_part, progress_log=log)
            else:
                if target_points is None:
                    raise RuntimeError(
                        "explicit STEP face coloring requires geometric pick points; "
                        "regenerate the target JSON with the wrapper script"
                    )
                records, targets = scan_part_by_pick_points(
                    session, active_part, target_points
                )
                matched_step_ids.update(
                    record["step_entity_id"] for _face, record in targets
                )
                log(
                    "SELECT context={} matched_step_faces={}".format(
                        context_index, len(targets)
                    )
                )
            if not records:
                continue
            changed = apply_face_color(
                session, [face for face, _record in targets], args.color
            )
            get_blend_count = sum(1 for record in records if record.get("is_blend"))
            connected_only_count = sum(
                1
                for record in records
                if record.get("connected_blend_candidate") and not record.get("is_blend")
            )
            context_report = {
                "context_index": context_index,
                "label": context.get("label"),
                "part": part_display_name(active_part),
                "face_count": len(records),
                "probed_face_count": len(records),
                "get_blend_data_confirmed_count": get_blend_count,
                "connected_blend_candidate_count": len(targets),
                "connected_blend_only_count": connected_only_count,
                "colored_face_count": changed,
                "candidate_faces": [record for _face, record in targets],
                "face_probe_results": records,
                "selection_mode": (
                    "exact_face_names" if target_face_names is not None
                    else "step_entity_ids" if target_step_ids is not None else "blend_rule"
                ),
            }
            report["contexts"].append(context_report)
            report["total_face_count"] += len(records)
            report["probed_face_count"] += len(records)
            report["get_blend_data_confirmed_count"] += get_blend_count
            report["connected_blend_candidate_count"] += len(targets)
            report["connected_blend_only_count"] += connected_only_count
            report["colored_blend_face_count"] += changed
            log(
                "COLOR context={} part={} faces={} getblend={} connected={} "
                "connected_only={} colored={}".format(
                    context_index,
                    part_display_name(active_part),
                    len(records),
                    get_blend_count,
                    len(targets),
                    connected_only_count,
                    changed,
                )
            )

        if report["total_face_count"] <= 0:
            raise RuntimeError("NX imported STEP but found no faces")
        if target_step_ids is not None:
            missing_step_ids = sorted(target_step_ids - matched_step_ids)
            report["matched_step_entity_ids"] = sorted(matched_step_ids)
            report["missing_step_entity_ids"] = missing_step_ids
            report["colored_target_face_count"] = len(matched_step_ids)
            if missing_step_ids:
                raise RuntimeError(
                    "target ADVANCED_FACE IDs were not mapped to NX faces: {}".format(
                        missing_step_ids
                    )
                )

        try:
            session.Parts.SetWork(display_part)
        except Exception as exc:
            log("WARNING restore display work part: {}".format(safe_text(exc)))

        if args.part_output:
            log("SAVE PRT {}".format(args.part_output))
            save_part_as(display_part, args.part_output)

        temporary_output = args.output + ".tmp.stp"
        cleanup_output(temporary_output)
        log("EXPORT {}".format(temporary_output))
        export_step(session, display_part, temporary_output, args.settings_file)
        if not os.path.isfile(temporary_output) or os.path.getsize(temporary_output) <= 0:
            raise RuntimeError("NX STEP export is missing or empty")
        cleanup_output(args.output)
        os.replace(temporary_output, args.output)

        report["success"] = True
        report["output_size"] = os.path.getsize(args.output)
        log(
            "RESULT colored={} output={}".format(
                report["colored_blend_face_count"], args.output
            )
        )
        return True, report
    except Exception as exc:
        report["errors"].append(safe_text(exc))
        report["traceback"] = traceback.format_exc()
        log("RESULT failed: {}".format(safe_text(exc)))
        cleanup_output(args.output)
        cleanup_output(args.output + ".tmp.stp")
        return False, report
    finally:
        try:
            write_report(args.report, report)
        except Exception as exc:
            log("REPORT failed: {}".format(safe_text(exc)))
        if display_part is not None and not args.keep_open:
            try:
                close_part(session, display_part)
            except Exception as exc:
                log("CLOSE warning: {}".format(safe_text(exc)))


def main(argv=None):
    args = parse_args(argv)
    success, _report = run(args)
    return success


if __name__ == "__main__":
    main()
