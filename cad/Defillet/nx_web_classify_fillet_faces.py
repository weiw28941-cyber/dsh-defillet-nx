# -*- coding: utf-8 -*-
"""Classify Web-selectable fillet candidates with NX APIs only."""

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
    cleanup_step_import_sidecars,
    collect_defillet_contexts,
    face_type_name,
    safe_body_journal_identifier,
    safe_face_journal_identifier,
    safe_face_name,
    safe_text,
    safe_tag,
)

ADVANCED_FACE_PATTERN = re.compile(
    r"#\s*(\d+)\s*=\s*ADVANCED_FACE\s*\(\s*'((?:[^']|'')*)'",
    re.IGNORECASE | re.DOTALL,
)
# NX2406 SolidFaceType values observed for imported STEP geometry:
# 1 Plane, 2 Cylinder, 3 Cone, 4 Sphere, 5 Torus, 6 B-Surface.
# The requested broad fillet-marking rule deliberately selects all curved
# categories except Cone; GetBlendData remains diagnostic only.
SELECTED_SOLID_FACE_TYPES = {"2", "4", "5", "6"}
SELECTED_SOLID_FACE_KEYWORDS = ("cylind", "sphere", "spheric", "torus", "toroid", "bsurface", "bspline", "b-spline", "b_spline")
TAGGED_FACE_NAME = re.compile(r"^DSH_STEP_FACE_(\d+)$")


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    args.input = os.path.abspath(args.input)
    args.report = os.path.abspath(args.report)
    if not os.path.isfile(args.input):
        parser.error("input STEP does not exist: {}".format(args.input))
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    return args


def parse_step_faces(path):
    """Count ADVANCED_FACE entities in the STEP file for mapping validation."""
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        text = stream.read()
    return [
        {"step_entity_id": int(match.group(1))}
        for match in ADVANCED_FACE_PATTERN.finditer(text)
    ]


def classify_face(face):
    radius, is_blend, blend_error = blend_data(face)
    solid_type = face_type_name(face)
    normalized = safe_text(solid_type).replace(" ", "").lower()
    selected_type = normalized in SELECTED_SOLID_FACE_TYPES or any(
        keyword in normalized for keyword in SELECTED_SOLID_FACE_KEYWORDS
    )
    selectable = bool(selected_type)
    if selected_type:
        reason = "nx-curved-face-rule:{}".format(solid_type)
    else:
        reason = "nx-excluded-face-type:{}".format(solid_type)
    return {
        "selectable": selectable,
        "reason": reason,
        "blend_radius": radius,
        "is_blend": bool(is_blend),
        "blend_error": blend_error,
        "solid_face_type": solid_type,
    }


def write_report(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


def main(argv=None):
    args = parse_args(argv)
    session = NXOpen.Session.GetSession()
    display_part = None
    try:
        step_faces = parse_step_faces(args.input)
        close_all_parts(session, log=lambda message: print(message, flush=True))
        cleanup_step_import_sidecars(args.input, log=lambda message: print(message, flush=True))
        from nx_1_defillet import open_step
        display_part = open_step(session, args.input)
        contexts = collect_defillet_contexts(display_part)
        rows = []
        ordinal = 0
        for context_index, context in enumerate(contexts, start=1):
            part = activate_defillet_context(session, context)
            if part is None:
                continue
            for body_index, body in enumerate(list(part.Bodies), start=1):
                body_jid = safe_body_journal_identifier(body)
                for face_index, face in enumerate(list(body.GetFaces()), start=1):
                    ordinal += 1
                    nx_name = safe_face_name(face)
                    tagged = TAGGED_FACE_NAME.match(nx_name)
                    exact_step_id = int(tagged.group(1)) if tagged else None
                    row = {
                        "nx_ordinal": ordinal,
                        "context_index": context_index,
                        "body_index": body_index,
                        "body_journal_identifier": body_jid,
                        "face_index": face_index,
                        "tag": safe_tag(face),
                        "journal_identifier": safe_face_journal_identifier(face),
                        "name": nx_name,
                        "step_entity_id": exact_step_id,
                    }
                    row.update(classify_face(face))
                    rows.append(row)
        if not rows:
            raise RuntimeError("NX imported STEP but found no faces")
        report = {
            "schema": "nx_web_fillet_candidates_v1",
            "success": True,
            "input": args.input,
            "mapping": "exact injected ADVANCED_FACE name to NX face.Name",
            "step_face_count": len(step_faces),
            "nx_face_count": len(rows),
            "mapping_complete": len(step_faces) == len(rows) and all(row["step_entity_id"] for row in rows),
            "candidate_count": sum(1 for row in rows if row["selectable"]),
            "faces": rows,
        }
        write_report(args.report, report)
        print("RESULT candidates={} faces={} report={}".format(report["candidate_count"], len(rows), args.report), flush=True)
        return True
    except Exception as exc:
        print("ERROR {}".format(safe_text(exc)), flush=True)
        print(traceback.format_exc(), flush=True)
        return False
    finally:
        if display_part is not None:
            try:
                close_part(session, display_part)
            except Exception:
                pass


if __name__ == "__main__":
    if not main():
        raise SystemExit(1)
