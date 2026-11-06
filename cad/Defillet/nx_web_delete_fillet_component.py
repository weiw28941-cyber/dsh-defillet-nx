# -*- coding: utf-8 -*-
"""Delete one or more validated fillet components using exact STEP face names."""

from __future__ import print_function

import argparse
import json
import os
import re
import sys
import traceback

import NXOpen
import NXOpen.Features

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from nx_1_defillet import (  # noqa: E402
    activate_defillet_context,
    close_all_parts,
    close_part,
    collect_defillet_contexts,
    configure_delete_face_builder,
    destroy_builder,
    export_step,
    open_step,
    safe_face_name,
    safe_text,
)

TAGGED_FACE_NAME = re.compile(r"^DSH_STEP_FACE_(\d+)$")
SUPPORTED_STRATEGIES = {"whole-component-face-edge-blend"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    args.request = os.path.abspath(args.request)
    return args


def write_report(path, payload):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


def collect_named_faces(session, contexts, target_ids):
    found = {}
    owning_parts = {}
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
                if step_id in target_ids:
                    if step_id in found:
                        raise RuntimeError("duplicate NX face for STEP ID {}".format(step_id))
                    found[step_id] = face
                    owning_parts[step_id] = part
    missing = sorted(target_ids - set(found))
    if missing:
        raise RuntimeError("could not resolve STEP face IDs {}".format(missing))
    part_tags = set(int(owning_parts[step_id].Tag) for step_id in target_ids)
    if len(part_tags) != 1:
        raise RuntimeError("one component must belong to exactly one NX work part")
    return owning_parts[next(iter(target_ids))], [found[step_id] for step_id in sorted(target_ids)]


def delete_faces(session, part, faces, preference):
    mark = session.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "component fillet delete")
    builder = part.Features.CreateDeleteFaceBuilder(NXOpen.Features.Feature.Null)
    try:
        preference_enum = (
            NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions.Notch
            if preference == "notch"
            else NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions.Cliff
        )
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
            raise RuntimeError("DeleteFaceBuilder.Commit returned None")
        session.SetUndoMarkName(mark, "component fillet delete committed")
    except Exception:
        try:
            session.UndoToMark(mark, "component fillet delete")
        except Exception:
            pass
        raise
    finally:
        destroy_builder(builder)


def main(argv=None):
    args = parse_args(argv)
    with open(args.request, "r", encoding="utf-8") as stream:
        request = json.load(stream)
    request_schema = request.get("schema")
    if request_schema == "nx_fillet_component_delete_v1":
        component_ids = [request["component_id"]]
        result_schema = "nx_fillet_component_delete_result_v1"
    elif request_schema == "nx_fillet_components_delete_v1":
        component_ids = list(request.get("component_ids") or [])
        result_schema = "nx_fillet_components_delete_result_v1"
    else:
        raise RuntimeError("unsupported request schema")
    if not component_ids or len(component_ids) != len(set(component_ids)):
        raise RuntimeError("component IDs must be nonempty and unique")
    strategy = request.get("strategy")
    if strategy not in SUPPORTED_STRATEGIES:
        raise RuntimeError("unsupported component deletion strategy: {}".format(strategy))
    input_path = os.path.abspath(request["input"])
    output_path = os.path.abspath(request["output"])
    report_path = os.path.abspath(request["report"])
    settings_file = os.path.abspath(request["settings_file"])
    target_ids = set(int(value) for value in request["step_entity_ids"])
    if not target_ids:
        raise RuntimeError("component has no STEP face IDs")
    if os.path.normcase(input_path) == os.path.normcase(output_path):
        raise RuntimeError("input and output must differ")

    session = NXOpen.Session.GetSession()
    display_part = None
    try:
        close_all_parts(session, log=lambda message: print(message, flush=True))
        display_part = open_step(session, input_path)
        work_part, faces = collect_named_faces(
            session, collect_defillet_contexts(display_part), target_ids
        )
        session.Parts.SetWork(work_part)
        delete_faces(
            session,
            work_part,
            faces,
            request.get("preference", "cliff"),
        )
        session.Parts.SetWork(display_part)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        export_step(session, display_part, output_path, settings_file)
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise RuntimeError("STEP export failed")
        report = {
            "schema": result_schema,
            "success": True,
            "component_ids": component_ids,
            "strategy": strategy,
            "deleted_step_entity_ids": sorted(target_ids),
            "output": output_path,
        }
        if request_schema == "nx_fillet_component_delete_v1":
            report["component_id"] = component_ids[0]
        write_report(report_path, report)
        print("RESULT components={} faces={} output={}".format(
            ",".join(component_ids), len(target_ids), output_path
        ), flush=True)
        return True
    except Exception as exc:
        if os.path.isfile(output_path):
            os.remove(output_path)
        report = {
            "schema": result_schema,
            "success": False,
            "component_ids": component_ids,
            "strategy": strategy,
            "error": safe_text(exc),
        }
        if request_schema == "nx_fillet_component_delete_v1":
            report["component_id"] = component_ids[0]
        write_report(report_path, report)
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
