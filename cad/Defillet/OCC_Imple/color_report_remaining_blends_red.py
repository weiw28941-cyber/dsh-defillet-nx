# -*- coding: utf-8 -*-
"""Color faces listed in a grow-delete JSON report red and export a colored STEP.

The report's NX journal identifiers contain a point lying on each remaining
blend face. Each point is matched to the nearest OCC face, so OCC does not need
to reproduce NX's blend classification.
"""

from __future__ import print_function

import argparse
import json
import math
import os
import re

from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCC.Core.BRepExtrema import BRepExtrema_DistShapeShape
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.Interface import Interface_Static
from OCC.Core.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCC.Core.STEPCAFControl import STEPCAFControl_Writer
from OCC.Core.STEPControl import STEPControl_AsIs, STEPControl_Reader
from OCC.Core.TDocStd import TDocStd_Document
from OCC.Core.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCC.Core.TopExp import TopExp_Explorer
from OCC.Core.gp import gp_Pnt
from OCC.Core.XCAFApp import XCAFApp_Application
from OCC.Core.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool


POINT_PATTERN = re.compile(
    r"\{\(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)"
)
RED = Quantity_Color(1.0, 0.0, 0.0, Quantity_TOC_RGB)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Color report.remaining_blends red and export a colored AP214 STEP."
    )
    parser.add_argument("--input", required=True, help="Input STEP file")
    parser.add_argument("--report", required=True, help="Grow-delete JSON report")
    parser.add_argument("--output", required=True, help="Colored STEP output")
    parser.add_argument(
        "--max-distance",
        type=float,
        default=1.0e-3,
        help="Maximum allowed report-point to OCC-face distance (default: 1e-3)",
    )
    parser.add_argument(
        "--include-adjacent-faces",
        action="store_true",
        help=(
            "Also color the immediate shared-edge neighbors of report blends, "
            "matching the expanded NX phase-2 candidate rule."
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)

    args.input = os.path.abspath(args.input)
    args.report = os.path.abspath(args.report)
    args.output = os.path.abspath(args.output)
    if not os.path.isfile(args.input):
        parser.error("input STEP does not exist: {}".format(args.input))
    if not os.path.isfile(args.report):
        parser.error("report does not exist: {}".format(args.report))
    if args.max_distance < 0:
        parser.error("--max-distance must be non-negative")
    if os.path.normcase(args.input) == os.path.normcase(args.output):
        parser.error("input and output paths must differ")
    if os.path.exists(args.output) and not args.overwrite:
        parser.error("output exists; pass --overwrite: {}".format(args.output))
    parent = os.path.dirname(args.output)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return args


def report_points(report_path):
    with open(report_path, "r", encoding="utf-8") as stream:
        report = json.load(stream)
    rows = report.get("remaining_blends") or []
    targets = []
    for index, row in enumerate(rows, start=1):
        text = str(row.get("journal_identifier") or "")
        match = POINT_PATTERN.search(text)
        if not match:
            raise RuntimeError(
                "remaining_blends[{}] has no parseable point: {}".format(index - 1, text)
            )
        point = tuple(float(match.group(axis)) for axis in (1, 2, 3))
        if not all(math.isfinite(value) for value in point):
            raise RuntimeError("non-finite target point at remaining_blends[{}]".format(index - 1))
        targets.append({"index": index, "point": point, "report_row": row})
    if not targets:
        raise RuntimeError("report contains no remaining_blends")
    return targets


def open_step_xcaf(path):
    # STEPCAFControl_Reader crashes on some NX-exported AP214 files in the
    # current pythonocc build. Read the B-rep with STEPControl_Reader, then put
    # it into a fresh XCAF document; this is sufficient for persistent colors.
    reader = STEPControl_Reader()
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise RuntimeError("STEP read failed: {}".format(path))
    if reader.TransferRoots() <= 0:
        raise RuntimeError("STEP shape transfer failed: {}".format(path))
    shape = reader.OneShape()
    if shape.IsNull():
        raise RuntimeError("STEP produced a null shape: {}".format(path))

    application = XCAFApp_Application.GetApplication()
    document = TDocStd_Document("")
    application.NewDocument("MDTV-XCAF", document)
    shape_tool = XCAFDoc_DocumentTool.ShapeTool(document.Main())
    root_label = shape_tool.AddShape(shape, False, False)
    if root_label.IsNull():
        raise RuntimeError("could not add STEP shape to XCAF document")
    return document, shape_tool, root_label, shape


def collect_faces(shape_tool, root_label, root_shape):
    records = []
    explorer = TopExp_Explorer(root_shape, TopAbs_FACE)
    while explorer.More():
        records.append(
            {
                "face": explorer.Current(),
                "root_label": root_label,
                "root_index": 1,
                "face_index": len(records) + 1,
            }
        )
        explorer.Next()
    if not records:
        raise RuntimeError("no OCC faces found in STEP")
    return shape_tool, records


def point_face_distance(point, face):
    vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
    extrema = BRepExtrema_DistShapeShape(vertex, face)
    extrema.Perform()
    if not extrema.IsDone():
        return float("inf")
    return float(extrema.Value())


def match_targets(targets, faces, max_distance):
    candidates = []
    for target_index, target in enumerate(targets):
        for face_index, face_record in enumerate(faces):
            candidates.append(
                (
                    point_face_distance(target["point"], face_record["face"]),
                    target_index,
                    face_index,
                )
            )
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))

    assignments = {}
    used_faces = set()
    for distance, target_index, face_index in candidates:
        if target_index in assignments or face_index in used_faces:
            continue
        assignments[target_index] = (face_index, distance)
        used_faces.add(face_index)
        if len(assignments) == len(targets):
            break

    if len(assignments) != len(targets):
        raise RuntimeError(
            "could only assign {} of {} target points uniquely".format(
                len(assignments), len(targets)
            )
        )

    matched = []
    for target_index, target in enumerate(targets):
        face_index, distance = assignments[target_index]
        if distance > max_distance:
            raise RuntimeError(
                "target {} nearest unique face distance {} exceeds {}".format(
                    target["index"], distance, max_distance
                )
            )
        matched.append(
            dict(target, face_record=faces[face_index], distance=distance)
        )
    return matched


def face_edges(face):
    edges = []
    explorer = TopExp_Explorer(face, TopAbs_EDGE)
    while explorer.More():
        edges.append(explorer.Current())
        explorer.Next()
    return edges


def shares_edge(face_a, face_b):
    edges_a = face_edges(face_a)
    edges_b = face_edges(face_b)
    return any(edge_a.IsSame(edge_b) for edge_a in edges_a for edge_b in edges_b)


def expand_with_adjacent_faces(matched, faces):
    selected_indices = {
        match["face_record"]["face_index"] for match in matched
    }
    anchor_faces = [match["face_record"]["face"] for match in matched]
    adjacent = []
    for record in faces:
        if record["face_index"] in selected_indices:
            continue
        shared_anchor_count = sum(
            1 for anchor_face in anchor_faces if shares_edge(record["face"], anchor_face)
        )
        if shared_anchor_count:
            item = dict(record)
            item["shared_anchor_count"] = shared_anchor_count
            adjacent.append(item)
    adjacent.sort(key=lambda record: record["face_index"])
    return adjacent


def color_faces(document, shape_tool, face_records):
    color_tool = XCAFDoc_DocumentTool.ColorTool(document.Main())
    for face_record in face_records:
        face_label = shape_tool.AddSubShape(
            face_record["root_label"], face_record["face"]
        )
        if face_label.IsNull():
            raise RuntimeError(
                "could not create XCAF label for OCC face {}".format(
                    face_record["face_index"]
                )
            )
        color_tool.SetColor(face_label, RED, XCAFDoc_ColorSurf)


def export_step(document, output_path):
    Interface_Static.SetCVal("write.step.schema", "AP214IS")
    writer = STEPCAFControl_Writer()
    writer.SetColorMode(True)
    writer.SetNameMode(True)
    writer.SetLayerMode(True)
    if not writer.Transfer(document, STEPControl_AsIs):
        raise RuntimeError("STEP transfer for export failed")
    temporary = output_path + ".tmp.stp"
    if os.path.exists(temporary):
        os.remove(temporary)
    if writer.Write(temporary) != IFSelect_RetDone:
        raise RuntimeError("STEP write failed: {}".format(temporary))
    if not os.path.isfile(temporary) or os.path.getsize(temporary) <= 0:
        raise RuntimeError("STEP output is empty: {}".format(temporary))
    if os.path.exists(output_path):
        os.remove(output_path)
    os.replace(temporary, output_path)


def main(argv=None):
    args = parse_args(argv)
    targets = report_points(args.report)
    document, shape_tool, root_label, shape = open_step_xcaf(args.input)
    shape_tool, faces = collect_faces(shape_tool, root_label, shape)
    matched = match_targets(targets, faces, args.max_distance)
    confirmed_faces = [match["face_record"] for match in matched]
    adjacent_faces = (
        expand_with_adjacent_faces(matched, faces)
        if args.include_adjacent_faces
        else []
    )
    color_faces(document, shape_tool, confirmed_faces + adjacent_faces)
    export_step(document, args.output)

    print("Input STEP: {}".format(args.input))
    print("Report: {}".format(args.report))
    print("OCC face count: {}".format(len(faces)))
    print("Confirmed NX blend faces: {}".format(len(matched)))
    print("Adjacent GetBlendData-false candidate faces: {}".format(len(adjacent_faces)))
    print("Red face count: {}".format(len(matched) + len(adjacent_faces)))
    for match in matched:
        print(
            "  target={} occ_face={} root={} distance={:.12g}".format(
                match["index"],
                match["face_record"]["face_index"],
                match["face_record"]["root_index"],
                match["distance"],
            )
        )
    print("Output STEP: {} ({} bytes)".format(args.output, os.path.getsize(args.output)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
