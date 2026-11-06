# -*- coding: utf-8 -*-
"""
pythonocc-core 7.9.x

给定 STEP 文件和 name 前缀：
  1. 读取 STEP
  2. 从 STEP entity 读取 face / edge 的 Name
  3. 输出匹配 face / edge 的几何类型分布，例如 Plane / Cylinder / Cone
  4. display 完整模型
  5. 高亮 name.startswith(prefix) 的 face 和 edge
"""

import argparse
from collections import Counter

from OCC.Core.STEPControl import STEPControl_Reader
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.StepRepr import StepRepr_RepresentationItem

from OCC.Core.AIS import AIS_Shape
from OCC.Core.Quantity import Quantity_Color, Quantity_TOC_RGB

from OCC.Core.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
from OCC.Core.GeomAbs import (
    GeomAbs_Plane,
    GeomAbs_Cylinder,
    GeomAbs_Cone,
    GeomAbs_Sphere,
    GeomAbs_Torus,
    GeomAbs_BezierSurface,
    GeomAbs_BSplineSurface,
    GeomAbs_SurfaceOfRevolution,
    GeomAbs_SurfaceOfExtrusion,
    GeomAbs_Line,
    GeomAbs_Circle,
    GeomAbs_Ellipse,
    GeomAbs_Hyperbola,
    GeomAbs_Parabola,
    GeomAbs_BezierCurve,
    GeomAbs_BSplineCurve,
)

from OCC.Display.SimpleGui import init_display
from OCC.Extend.TopologyUtils import TopologyExplorer


FACE_COLOR = (1.0, 0.15, 0.05)
EDGE_COLOR = (0.05, 0.35, 1.0)

BACKGROUND_COLOR = (0.65, 0.65, 0.65)
BACKGROUND_TRANSPARENCY = 0.82

MATCHED_FACE_TRANSPARENCY = 0.0
MATCHED_EDGE_WIDTH = 5.0

FACE_GEOM_TYPE_MAP = {
    GeomAbs_Plane: "Plane",
    GeomAbs_Cylinder: "Cylinder",
    GeomAbs_Cone: "Cone",
    GeomAbs_Sphere: "Sphere",
    GeomAbs_Torus: "Torus",
    GeomAbs_BezierSurface: "BezierSurface",
    GeomAbs_BSplineSurface: "BSplineSurface",
    GeomAbs_SurfaceOfRevolution: "SurfaceOfRevolution",
    GeomAbs_SurfaceOfExtrusion: "SurfaceOfExtrusion",
}

EDGE_GEOM_TYPE_MAP = {
    GeomAbs_Line: "Line",
    GeomAbs_Circle: "Circle",
    GeomAbs_Ellipse: "Ellipse",
    GeomAbs_Hyperbola: "Hyperbola",
    GeomAbs_Parabola: "Parabola",
    GeomAbs_BezierCurve: "BezierCurve",
    GeomAbs_BSplineCurve: "BSplineCurve",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="STEP file path")
    parser.add_argument("--name-prefix", default="", help="STEP entity name prefix")
    parser.add_argument("--case-insensitive", action="store_true")
    parser.add_argument("--no-dim-background", action="store_true")
    return parser.parse_args()


def qcolor(rgb):
    return Quantity_Color(
        float(rgb[0]),
        float(rgb[1]),
        float(rgb[2]),
        Quantity_TOC_RGB,
    )


def safe_str(value):
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def face_geom_type(face):
    try:
        surf = BRepAdaptor_Surface(face)
        geom_type = surf.GetType()
        return FACE_GEOM_TYPE_MAP.get(geom_type, "OtherSurface({})".format(int(geom_type)))
    except Exception:
        return "<unknown>"


def edge_geom_type(edge):
    try:
        curve = BRepAdaptor_Curve(edge)
        geom_type = curve.GetType()
        return EDGE_GEOM_TYPE_MAP.get(geom_type, "OtherCurve({})".format(int(geom_type)))
    except Exception:
        return "<unknown>"


def get_shape_step_name(shape_item, transfer_reader, model):
    step_entity_id = None
    step_entity_type = ""
    step_name = ""

    try:
        item = transfer_reader.EntityFromShapeResult(shape_item, 1)
    except Exception:
        item = None

    if item is None:
        return step_entity_id, step_entity_type, step_name

    try:
        step_entity_id = model.Number(item)
    except Exception:
        pass

    try:
        step_entity_type = item.DynamicType().Name()
    except Exception:
        pass

    try:
        rep_item = StepRepr_RepresentationItem.DownCast(item)
        if rep_item is not None:
            raw_name = rep_item.Name()
            if raw_name is not None:
                step_name = safe_str(raw_name.ToCString())
    except Exception:
        pass

    return step_entity_id, step_entity_type, step_name


def name_matches(name, prefix, case_insensitive=False):
    if not name:
        return False

    if name.strip() == "":
        return False

    return True


def read_step_with_named_faces_edges(path):
    reader = STEPControl_Reader()
    status = reader.ReadFile(path)

    if status != IFSelect_RetDone:
        raise RuntimeError("STEP read failed: {}".format(path))

    reader.TransferRoots()
    shape = reader.OneShape()

    ws = reader.WS()
    transfer_reader = ws.TransferReader()
    model = ws.Model()

    topo = TopologyExplorer(shape)

    face_rows = []
    for face_index, face in enumerate(topo.faces(), start=1):
        step_id, step_type, name = get_shape_step_name(
            face,
            transfer_reader,
            model,
        )

        face_rows.append(
            {
                "kind": "face",
                "shape": face,
                "index": face_index,
                "step_entity_id": step_id,
                "step_entity_type": step_type,
                "geom_type": face_geom_type(face),
                "name": name,
            }
        )

    edge_rows = []
    for edge_index, edge in enumerate(topo.edges(), start=1):
        step_id, step_type, name = get_shape_step_name(
            edge,
            transfer_reader,
            model,
        )

        edge_rows.append(
            {
                "kind": "edge",
                "shape": edge,
                "index": edge_index,
                "step_entity_id": step_id,
                "step_entity_type": step_type,
                "geom_type": edge_geom_type(edge),
                "name": name,
            }
        )

    return shape, face_rows, edge_rows


def collect_matches(rows, prefix, case_insensitive=False):
    return [
        row
        for row in rows
        if name_matches(
            row["name"],
            prefix,
            case_insensitive=case_insensitive,
        )
    ]


def print_geom_distribution(title, rows):
    print("\n{}".format(title))

    if not rows:
        print("  No matched item found.")
        return

    counter = Counter(row["geom_type"] for row in rows)

    for geom_type, count in counter.most_common():
        print("  {}: {}".format(geom_type, count))


def print_named_rows(title, rows):
    print("\n{}".format(title))

    count = 0
    for row in rows:
        if not row["name"]:
            continue

        count += 1
        print(
            "  {kind}_index={idx}, step_id={sid}, name={name}, geom={geom}, entity={etype}".format(
                kind=row["kind"],
                idx=row["index"],
                sid=row["step_entity_id"],
                name=row["name"],
                geom=row["geom_type"],
                etype=row["step_entity_type"],
            )
        )

    if count == 0:
        print("  No named item found.")


def print_summary(input_path, prefix, face_rows, edge_rows, matched_faces, matched_edges):
    print("Input:", input_path)
    print("Name prefix:", prefix)
    print("Total faces:", len(face_rows))
    print("Total edges:", len(edge_rows))
    print("Matched faces:", len(matched_faces))
    print("Matched edges:", len(matched_edges))

    print_geom_distribution(
        "Matched face geometry distribution:",
        matched_faces,
    )

    print_geom_distribution(
        "Matched edge geometry distribution:",
        matched_edges,
    )

    if matched_faces:
        print("\nMatched faces:")
        for row in matched_faces:
            print(
                "  face_index={idx}, step_id={sid}, name={name}, geom={geom}, entity={etype}".format(
                    idx=row["index"],
                    sid=row["step_entity_id"],
                    name=row["name"],
                    geom=row["geom_type"],
                    etype=row["step_entity_type"],
                )
            )

    if matched_edges:
        print("\nMatched edges:")
        for row in matched_edges:
            print(
                "  edge_index={idx}, step_id={sid}, name={name}, geom={geom}, entity={etype}".format(
                    idx=row["index"],
                    sid=row["step_entity_id"],
                    name=row["name"],
                    geom=row["geom_type"],
                    etype=row["step_entity_type"],
                )
            )


def display_result(shape, matched_faces, matched_edges, dim_background=True):
    display, start_display, _, _ = init_display()
    context = display.Context

    if dim_background:
        ais_bg = AIS_Shape(shape)
        ais_bg.SetColor(qcolor(BACKGROUND_COLOR))
        ais_bg.SetTransparency(BACKGROUND_TRANSPARENCY)
        context.Display(ais_bg, False)
    else:
        display.DisplayShape(shape, update=False)

    face_color = qcolor(FACE_COLOR)
    edge_color = qcolor(EDGE_COLOR)

    for row in matched_faces:
        ais_face = AIS_Shape(row["shape"])
        ais_face.SetColor(face_color)
        ais_face.SetTransparency(MATCHED_FACE_TRANSPARENCY)
        context.Display(ais_face, False)

    for row in matched_edges:
        ais_edge = AIS_Shape(row["shape"])
        ais_edge.SetColor(edge_color)

        try:
            ais_edge.SetWidth(MATCHED_EDGE_WIDTH)
        except Exception:
            pass

        context.Display(ais_edge, False)

    display.FitAll()
    context.UpdateCurrentViewer()
    start_display()


def main():
    args = parse_args()

    shape, face_rows, edge_rows = read_step_with_named_faces_edges(args.input)

    matched_faces = collect_matches(
        face_rows,
        args.name_prefix,
        case_insensitive=args.case_insensitive,
    )

    matched_edges = collect_matches(
        edge_rows,
        args.name_prefix,
        case_insensitive=args.case_insensitive,
    )

    print_summary(
        input_path=args.input,
        prefix=args.name_prefix,
        face_rows=face_rows,
        edge_rows=edge_rows,
        matched_faces=matched_faces,
        matched_edges=matched_edges,
    )

    if not matched_faces and not matched_edges:
        print_named_rows("All named faces:", face_rows)
        print_named_rows("All named edges:", edge_rows)

    display_result(
        shape=shape,
        matched_faces=matched_faces,
        matched_edges=matched_edges,
        dim_background=not args.no_dim_background,
    )


if __name__ == "__main__":
    main()
