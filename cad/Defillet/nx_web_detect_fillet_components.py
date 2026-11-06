# -*- coding: utf-8 -*-
"""Detect NX curved-face candidates and partition them by shared topology edges."""

from __future__ import print_function

import argparse
import json
import os
import sys
import traceback

import NXOpen

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from nx_web_classify_fillet_faces import (  # noqa: E402
    TAGGED_FACE_NAME,
    classify_face,
    parse_step_faces,
)
from nx_1_defillet import (  # noqa: E402
    activate_defillet_context,
    close_all_parts,
    close_part,
    cleanup_step_import_sidecars,
    collect_defillet_contexts,
    edge_faces,
    open_step,
    safe_body_journal_identifier,
    safe_face_journal_identifier,
    safe_face_name,
    safe_tag,
    safe_text,
)


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


def write_report(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


def connected_components(rows, faces_by_tag):
    row_by_tag = {row["tag"]: row for row in rows}
    candidate_tags = set(row_by_tag)
    adjacency = {tag: set() for tag in candidate_tags}
    for tag, face in faces_by_tag.items():
        for edge in face.GetEdges():
            for neighbor in edge_faces(edge):
                neighbor_tag = safe_tag(neighbor)
                if neighbor_tag in candidate_tags and neighbor_tag != tag:
                    adjacency[tag].add(neighbor_tag)
                    adjacency[neighbor_tag].add(tag)

    remaining = set(candidate_tags)
    groups = []
    while remaining:
        seed = min(remaining, key=lambda tag: row_by_tag[tag]["step_entity_id"])
        remaining.remove(seed)
        stack = [seed]
        tags = []
        while stack:
            tag = stack.pop()
            tags.append(tag)
            neighbors = sorted(
                adjacency[tag] & remaining,
                key=lambda item: row_by_tag[item]["step_entity_id"],
                reverse=True,
            )
            for neighbor in neighbors:
                remaining.remove(neighbor)
                stack.append(neighbor)
        component_rows = sorted(
            (row_by_tag[tag] for tag in tags),
            key=lambda row: row["step_entity_id"],
        )
        groups.append(component_rows)
    groups.sort(key=lambda group: group[0]["step_entity_id"])
    return groups, adjacency


def main(argv=None):
    args = parse_args(argv)
    session = NXOpen.Session.GetSession()
    display_part = None
    try:
        step_faces = parse_step_faces(args.input)
        close_all_parts(session, log=lambda message: print(message, flush=True))
        cleanup_step_import_sidecars(args.input, log=lambda message: print(message, flush=True))
        display_part = open_step(session, args.input)
        contexts = collect_defillet_contexts(display_part)
        all_rows = []
        selected_rows = []
        faces_by_tag = {}
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
                    all_rows.append(row)
                    if row["selectable"] and exact_step_id is not None:
                        selected_rows.append(row)
                        faces_by_tag[row["tag"]] = face
        if not all_rows:
            raise RuntimeError("NX imported STEP but found no faces")
        expected_ids = set(row["step_entity_id"] for row in step_faces)
        actual_ids = [row["step_entity_id"] for row in all_rows if row["step_entity_id"] is not None]
        actual_id_set = set(actual_ids)
        mapping_errors = {
            "untagged_nx_face_count": sum(1 for row in all_rows if row["step_entity_id"] is None),
            "duplicate_step_entity_ids": sorted(set(value for value in actual_ids if actual_ids.count(value) > 1)),
            "missing_step_entity_ids": sorted(expected_ids - actual_id_set),
            "unexpected_step_entity_ids": sorted(actual_id_set - expected_ids),
        }
        mapping_complete = (
            len(step_faces) == len(all_rows)
            and not mapping_errors["untagged_nx_face_count"]
            and not mapping_errors["duplicate_step_entity_ids"]
            and not mapping_errors["missing_step_entity_ids"]
            and not mapping_errors["unexpected_step_entity_ids"]
        )
        if not mapping_complete:
            raise RuntimeError("NX did not preserve every injected STEP face name: {}".format(mapping_errors))

        groups, adjacency = connected_components(selected_rows, faces_by_tag)
        components = []
        for index, group in enumerate(groups, start=1):
            counts = {}
            radii = set()
            for row in group:
                face_type = safe_text(row["solid_face_type"])
                counts[face_type] = counts.get(face_type, 0) + 1
                if row.get("blend_radius") is not None:
                    radii.add(float(row["blend_radius"]))
            ids = [row["step_entity_id"] for row in group]
            edge_count = sum(len(adjacency[row["tag"]]) for row in group) // 2
            components.append({
                "component_id": "fc-{:03d}".format(index),
                "face_count": len(group),
                "step_entity_ids": ids,
                "surface_types": counts,
                "blend_face_count": sum(1 for row in group if row.get("is_blend")),
                "non_blend_face_count": sum(1 for row in group if not row.get("is_blend")),
                "blend_radii": sorted(radii),
                "adjacency_edge_count": edge_count,
            })

        report = {
            "schema": "nx_fillet_components_v1",
            "success": True,
            "input": args.input,
            "engine": "NXOpen SolidFaceType + shared NX topology edges",
            "mapping": "exact injected ADVANCED_FACE name to NX face.Name",
            "step_face_count": len(step_faces),
            "nx_face_count": len(all_rows),
            "mapping_complete": mapping_complete,
            "mapping_errors": mapping_errors,
            "candidate_face_count": len(selected_rows),
            "component_count": len(components),
            "components": components,
        }
        write_report(args.report, report)
        print("RESULT candidates={} components={} report={}".format(
            len(selected_rows), len(components), args.report
        ), flush=True)
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
