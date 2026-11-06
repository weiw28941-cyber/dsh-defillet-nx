# -*- coding: utf-8 -*-
r"""
OCC / pythonocc command-line probe:
Open STEP -> optionally find faces by STEP/XCAF face name -> classify faces ->
identify blend-like faces by G1 tangency to adjacent faces along shared edges.

Example:
    python cad/Defillet/OCC_Imple/detect_fillet_by_rule_and_blend_occ.py --input "input.step" --face-name "1"
    python cad/Defillet/OCC_Imple/detect_fillet_by_rule_and_blend_occ.py --input test.step --all-faces

Notes:
- Requires pythonocc-core, or an environment exposing the same OCC.Core modules.
- Face names in STEP are only available when they are present in STEP/XCAF metadata.
  If no names are imported, use --face-index or --all-faces.
- Blend criterion used here:
    a face is considered blend-like when it has at least --min-g1-edges shared
    boundary edges whose adjacent face is G1 tangent to it.
"""

from __future__ import print_function

import argparse
import math
import os
import sys
from collections import defaultdict

from OCC.Core.BRep import BRep_Tool
from OCC.Core.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCC.Core.BRepLProp import BRepLProp_SLProps
from OCC.Core.BRepTools import BRepTools_WireExplorer
from OCC.Core.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCC.Core.GeomAbs import (
    GeomAbs_BezierSurface,
    GeomAbs_BSplineSurface,
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_OffsetSurface,
    GeomAbs_OtherSurface,
    GeomAbs_Plane,
    GeomAbs_Sphere,
    GeomAbs_SurfaceOfExtrusion,
    GeomAbs_SurfaceOfRevolution,
    GeomAbs_Torus,
)
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.STEPCAFControl import STEPCAFControl_Reader
from OCC.Core.TCollection import TCollection_ExtendedString
from OCC.Core.TDataStd import TDataStd_Name
from OCC.Core.TDF import TDF_Label, TDF_LabelSequence
from OCC.Core.TDocStd import TDocStd_Document
from OCC.Core.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCC.Core.TopExp import TopExp_Explorer
from OCC.Core.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
from OCC.Core.XCAFDoc import XCAFDoc_DocumentTool


SURFACE_TYPE_NAMES = {
    GeomAbs_Plane: "Planar",
    GeomAbs_Cylinder: "Cylindrical",
    GeomAbs_Cone: "Conical",
    GeomAbs_Sphere: "Spherical",
    GeomAbs_Torus: "Toroidal",
    GeomAbs_BezierSurface: "BezierSurface",
    GeomAbs_BSplineSurface: "BSplineSurface",
    GeomAbs_SurfaceOfRevolution: "SurfaceOfRevolution",
    GeomAbs_SurfaceOfExtrusion: "SurfaceOfExtrusion",
    GeomAbs_OffsetSurface: "OffsetSurface",
    GeomAbs_OtherSurface: "OtherSurface",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="STEP file to open")
    parser.add_argument("--face-name", default="1", help="STEP/XCAF face name to inspect")
    parser.add_argument("--face-index", type=int, default=None, help="1-based face index to inspect")
    parser.add_argument("--all-faces", action="store_true", help="Inspect every face")
    parser.add_argument("--angle-tol-deg", type=float, default=2.0, help="G1 tangent angle tolerance in degrees")
    parser.add_argument("--min-g1-edges", type=int, default=2, help="Minimum G1 adjacent boundary edges to call a face blend-like")
    parser.add_argument("--sample-count", type=int, default=3, help="Samples per shared edge for G1 check")
    return parser.parse_args(argv)


def safe_text(value):
    if value is None:
        return ""
    try:
        return str(value)
    except Exception as exc:
        return "<str failed: {}>".format(exc)


def open_step_xcaf(input_path):
    doc = TDocStd_Document(TCollection_ExtendedString("occ-doc"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    reader.SetLayerMode(True)

    status = reader.ReadFile(input_path)
    if status != IFSelect_RetDone:
        raise RuntimeError("STEP read failed: {}".format(input_path))

    if not reader.Transfer(doc):
        raise RuntimeError("STEP transfer to XCAF document failed: {}".format(input_path))

    shape_tool = XCAFDoc_DocumentTool.ShapeTool(doc.Main())
    roots = TDF_LabelSequence()
    shape_tool.GetFreeShapes(roots)

    shapes = []
    for i in range(1, roots.Length() + 1):
        label = roots.Value(i)
        shape = shape_tool.GetShape(label)
        if not shape.IsNull():
            shapes.append(shape)

    return doc, shape_tool, shapes


def iter_faces(shape):
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        yield exp.Current()
        exp.Next()


def iter_edges(face):
    exp = TopExp_Explorer(face, TopAbs_EDGE)
    while exp.More():
        yield exp.Current()
        exp.Next()


def shape_hash(shape):
    return int(shape.HashCode(2147483647))


def label_name(label):
    name_attr = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID(), name_attr):
        try:
            return safe_text(name_attr.Get().ToExtString())
        except Exception:
            return safe_text(name_attr.Get())
    return ""


def face_name(shape_tool, face):
    # STEP subshape names are not guaranteed. Search tries to find matching XCAF label.
    label = TDF_Label()
    for args in ((True, True, True), (False, True, True), (False, False, True)):
        try:
            if shape_tool.Search(face, label, *args):
                name = label_name(label)
                if name:
                    return name
        except TypeError:
            pass
        except Exception:
            pass
    return ""


def surface_type_and_radius(face):
    surf = BRepAdaptor_Surface(face, True)
    stype = surf.GetType()
    type_name = SURFACE_TYPE_NAMES.get(stype, safe_text(stype))

    radius = None
    try:
        if stype == GeomAbs_Cylinder:
            radius = float(surf.Cylinder().Radius())
        elif stype == GeomAbs_Sphere:
            radius = float(surf.Sphere().Radius())
        elif stype == GeomAbs_Torus:
            radius = float(surf.Torus().MinorRadius())
        elif stype == GeomAbs_Cone:
            radius = float(surf.Cone().RefRadius())
    except Exception:
        radius = None

    return type_name, radius


def build_edge_to_faces(shapes):
    edge_to_faces = defaultdict(list)
    for face in [f for shape in shapes for f in iter_faces(shape)]:
        for edge in iter_edges(face):
            edge_to_faces[shape_hash(edge)].append((edge, face))
    return edge_to_faces


def edge_parameter_samples(edge, count):
    curve = BRepAdaptor_Curve(edge)
    first = curve.FirstParameter()
    last = curve.LastParameter()
    if not math.isfinite(first) or not math.isfinite(last) or first == last:
        return []
    count = max(1, int(count))
    if count == 1:
        return [(first + last) * 0.5]
    # Avoid exact vertices because normals/projections are more fragile there.
    margin = 0.10
    return [first + (last - first) * (margin + (1.0 - 2.0 * margin) * i / (count - 1)) for i in range(count)]


def point_on_edge(edge, param):
    curve = BRepAdaptor_Curve(edge)
    return curve.Value(param)


def face_normal_at_point(face, point, resolution=1.0e-7):
    surface_handle = BRep_Tool.Surface(face)
    projector = GeomAPI_ProjectPointOnSurf(point, surface_handle)
    if not projector.IsDone() or projector.NbPoints() < 1:
        return None

    u, v = projector.LowerDistanceParameters()
    props = BRepLProp_SLProps(surface_handle, u, v, 1, resolution)
    if not props.IsNormalDefined():
        return None

    normal = props.Normal()
    vec = normal.XYZ()

    # Reverse by topological orientation so two adjacent tangent faces tend to have
    # opposite material-side normals. G1 check below uses abs(dot), so this is not required
    # for tangent detection, but it makes diagnostics more consistent.
    if face.Orientation() == TopAbs_REVERSED:
        vec.Multiply(-1.0)

    return vec


def normals_are_g1(face_a, face_b, edge, angle_tol_rad, sample_count):
    min_abs_dot = 1.0
    max_angle = 0.0
    valid = 0

    for param in edge_parameter_samples(edge, sample_count):
        pnt = point_on_edge(edge, param)
        na = face_normal_at_point(face_a, pnt)
        nb = face_normal_at_point(face_b, pnt)
        if na is None or nb is None:
            continue

        if na.Modulus() <= 0.0 or nb.Modulus() <= 0.0:
            continue
        na.Normalize()
        nb.Normalize()
        dot = max(-1.0, min(1.0, na.Dot(nb)))
        abs_dot = abs(dot)
        angle = math.acos(max(-1.0, min(1.0, abs_dot)))
        min_abs_dot = min(min_abs_dot, abs_dot)
        max_angle = max(max_angle, angle)
        valid += 1

    if valid == 0:
        return False, None

    return max_angle <= angle_tol_rad, math.degrees(max_angle)


def adjacent_faces_for_edge(edge_to_faces, edge, this_face):
    result = []
    this_hash = shape_hash(this_face)
    for stored_edge, face in edge_to_faces.get(shape_hash(edge), []):
        if shape_hash(face) != this_hash:
            result.append((stored_edge, face))
    return result


def analyze_g1_blend(face, edge_to_faces, angle_tol_rad, sample_count, min_g1_edges):
    tangent_edges = []
    non_tangent_edges = []
    adjacent_count = 0

    for edge in iter_edges(face):
        for adj_edge, adj_face in adjacent_faces_for_edge(edge_to_faces, edge, face):
            adjacent_count += 1
            is_g1, max_angle_deg = normals_are_g1(face, adj_face, edge, angle_tol_rad, sample_count)
            item = {
                "edge_hash": shape_hash(edge),
                "adjacent_face_hash": shape_hash(adj_face),
                "max_angle_deg": max_angle_deg,
            }
            if is_g1:
                tangent_edges.append(item)
            else:
                non_tangent_edges.append(item)

    return {
        "is_blend_like": len(tangent_edges) >= int(min_g1_edges),
        "adjacent_count": adjacent_count,
        "g1_edge_count": len(tangent_edges),
        "non_g1_edge_count": len(non_tangent_edges),
        "g1_edges": tangent_edges,
        "non_g1_edges": non_tangent_edges,
    }


def collect_faces(shape_tool, shapes, args):
    rows = []
    face_index = 0
    for shape_index, shape in enumerate(shapes, start=1):
        for face in iter_faces(shape):
            face_index += 1
            name = face_name(shape_tool, face)

            if not args.all_faces:
                if args.face_index is not None:
                    if face_index != args.face_index:
                        continue
                elif name != args.face_name:
                    continue

            surface_type, radius = surface_type_and_radius(face)
            rows.append(
                {
                    "face": face,
                    "shape_index": shape_index,
                    "face_index": face_index,
                    "face_name": name,
                    "face_hash": shape_hash(face),
                    "surface_type": surface_type,
                    "radius": radius,
                }
            )
    return rows


def print_report(rows, target_name):
    type_counts = defaultdict(int)
    radius_counts = defaultdict(int)
    blend_counts = defaultdict(int)

    for row in rows:
        type_counts[row["surface_type"] or "<unknown>"] += 1
        if row["radius"] is not None:
            radius_counts["{:.9g}".format(row["radius"])] += 1
        blend_counts["blend-like" if row["g1"]["is_blend_like"] else "not blend-like"] += 1

    print("")
    print("Target face name: {!r}".format(target_name))
    print("Matched faces: {}".format(len(rows)))

    print("")
    print("Face type distribution:")
    if not type_counts:
        print("  <none>")
    else:
        for key in sorted(type_counts):
            print("  {}: {}".format(key, type_counts[key]))

    print("")
    print("Radius distribution:")
    if not radius_counts:
        print("  <none>")
    else:
        for key in sorted(radius_counts, key=lambda x: float(x)):
            print("  {}: {}".format(key, radius_counts[key]))

    print("")
    print("G1 blend-like distribution:")
    if not blend_counts:
        print("  <none>")
    else:
        for key in sorted(blend_counts):
            print("  {}: {}".format(key, blend_counts[key]))

    print("")
    print("Matched face details:")
    if not rows:
        print("  <none>")
    for row in rows:
        radius_text = "" if row["radius"] is None else ", radius={:.9g}".format(row["radius"])
        g1 = row["g1"]
        print(
            "  face_index={face_index}, name={name!r}, hash={face_hash}, type={surface_type}{radius}, "
            "blend_like={blend}, adjacent={adjacent}, g1_edges={g1_edges}, non_g1_edges={non_g1_edges}".format(
                face_index=row["face_index"],
                name=row["face_name"],
                face_hash=row["face_hash"],
                surface_type=row["surface_type"],
                radius=radius_text,
                blend=g1["is_blend_like"],
                adjacent=g1["adjacent_count"],
                g1_edges=g1["g1_edge_count"],
                non_g1_edges=g1["non_g1_edge_count"],
            )
        )
        for item in g1["g1_edges"]:
            angle = "?" if item["max_angle_deg"] is None else "{:.6g}".format(item["max_angle_deg"])
            print("    G1 edge_hash={}, adjacent_face_hash={}, max_angle_deg={}".format(item["edge_hash"], item["adjacent_face_hash"], angle))


def main(argv=None):
    args = parse_args(argv)
    if not os.path.exists(args.input):
        raise SystemExit("Input STEP does not exist: {}".format(args.input))

    print("Opening STEP: {}".format(args.input))
    doc, shape_tool, shapes = open_step_xcaf(args.input)
    print("Free root shapes: {}".format(len(shapes)))

    edge_to_faces = build_edge_to_faces(shapes)
    rows = collect_faces(shape_tool, shapes, args)

    angle_tol_rad = math.radians(args.angle_tol_deg)
    for row in rows:
        row["g1"] = analyze_g1_blend(
            row["face"],
            edge_to_faces,
            angle_tol_rad=angle_tol_rad,
            sample_count=args.sample_count,
            min_g1_edges=args.min_g1_edges,
        )

    print_report(rows, args.face_name)


if __name__ == "__main__":
    main(sys.argv[1:])
