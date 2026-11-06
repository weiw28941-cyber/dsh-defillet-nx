# -*- coding: utf-8 -*-
"""Fixed NX journal for the local web editor's allowlisted fillet deletion."""

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

from nx_1_defillet import (  # noqa: E402
    activate_defillet_context,
    close_all_parts,
    close_part,
    collect_defillet_contexts,
    configure_delete_face_builder,
    destroy_builder,
    export_step,
    open_step,
    safe_call,
    safe_text,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    args.request = os.path.abspath(args.request)
    return args


def nearest_face(session, part, point):
    point_object = part.Points.CreatePoint(NXOpen.Point3d(*point))
    best = None
    try:
        for body in list(part.Bodies):
            for face in list(body.GetFaces()):
                try:
                    distance, _input_point, point_on_face, accuracy = session.Measurement.GetMinimumDistance(point_object, face)
                except Exception:
                    continue
                row = (float(distance), face, point_on_face, float(accuracy))
                if best is None or row[0] < best[0]:
                    best = row
    finally:
        try:
            point_object.Destroy()
        except Exception:
            pass
    return best


def delete_faces(session, part, faces, preference):
    mark = session.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "web fillet delete")
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
        session.SetUndoMarkName(mark, "web fillet delete committed")
    except Exception:
        try:
            session.UndoToMark(mark, "web fillet delete")
        except Exception:
            pass
        raise
    finally:
        destroy_builder(builder)


def main(argv=None):
    args = parse_args(argv)
    with open(args.request, "r", encoding="utf-8") as stream:
        request = json.load(stream)
    if request.get("schema") != "nx_web_fillet_delete_v1":
        raise RuntimeError("unsupported request schema")
    input_path = os.path.abspath(request["input"])
    output_path = os.path.abspath(request["output"])
    settings_file = os.path.abspath(request["settings_file"])
    if os.path.normcase(input_path) == os.path.normcase(output_path):
        raise RuntimeError("input and output must differ")

    session = NXOpen.Session.GetSession()
    display_part = None
    try:
        close_all_parts(session, log=lambda message: print(message, flush=True))
        display_part = open_step(session, input_path)
        contexts = collect_defillet_contexts(display_part)
        resolved = []
        used_tags = set()
        for target in request["targets"]:
            best_global = None
            for context in contexts:
                part = activate_defillet_context(session, context)
                if part is None:
                    continue
                best = nearest_face(session, part, target["pick_point"])
                if best is not None and (best_global is None or best[0] < best_global[0]):
                    best_global = (best[0], part, best[1], best[2], best[3])
            if best_global is None:
                raise RuntimeError("could not resolve target {}".format(target["face_id"]))
            distance, part, face, point_on_face, accuracy = best_global
            print("TARGET {} distance={} accuracy={} point={}".format(target["face_id"], distance, accuracy, point_on_face), flush=True)
            tag = int(face.Tag)
            if tag not in used_tags:
                resolved.append((part, face))
                used_tags.add(tag)

        parts = {int(part.Tag): part for part, _face in resolved}
        if len(parts) != 1:
            raise RuntimeError("all selected faces must resolve to one NX work part")
        work_part = next(iter(parts.values()))
        session.Parts.SetWork(work_part)
        delete_faces(
            session,
            work_part,
            [face for _part, face in resolved],
            request.get("preference", "cliff"),
        )
        session.Parts.SetWork(display_part)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        export_step(session, display_part, output_path, settings_file)
        if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            raise RuntimeError("STEP export failed")
        print("RESULT {}".format(output_path), flush=True)
        return True
    except Exception as exc:
        print("ERROR {}".format(safe_text(exc)), flush=True)
        print(traceback.format_exc(), flush=True)
        if os.path.isfile(output_path):
            os.remove(output_path)
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
