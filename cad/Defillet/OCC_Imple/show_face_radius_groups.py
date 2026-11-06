# -*- coding: utf-8 -*-
"""
pythonocc-core 7.9.3

读取 STEP -> 从 STEP entity 获取 face name ->
匹配 --face-name 的 face 全部显示 ->
圆柱 / 圆环 / 球面提取半径并按半径分组着色 ->
提取不到半径但 name 匹配的 face 也单独分组显示。

Example:
    python cad/Defillet/OCC_Imple/show_face_radius_groups.py ^
        --input "input.step" ^
        --face-name 1
"""

import argparse
from collections import defaultdict

from OCC.Core.STEPControl import STEPControl_Reader
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.StepRepr import StepRepr_RepresentationItem

from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
from OCC.Core.GeomAbs import GeomAbs_Cylinder, GeomAbs_Sphere, GeomAbs_Torus

from OCC.Core.AIS import AIS_Shape
from OCC.Core.Quantity import Quantity_Color, Quantity_TOC_RGB

from OCC.Display.SimpleGui import init_display
from OCC.Extend.TopologyUtils import TopologyExplorer



RADIUS_GROUP_COLORS = [
    (0.90, 0.10, 0.10),
    (0.10, 0.55, 0.95),
    (0.10, 0.75, 0.25),
    (0.95, 0.60, 0.10),
    (0.65, 0.20, 0.90),
    (0.00, 0.80, 0.80),
    (0.95, 0.20, 0.65),
    (0.55, 0.55, 0.10),
    (0.20, 0.20, 0.90),
    (0.90, 0.90, 0.15),
]

UNKNOWN_RADIUS_COLOR = (1.00, 1.00, 1.00)
BACKGROUND_COLOR = (0.65, 0.65, 0.65)
BACKGROUND_TRANSPARENCY = 0.85


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="STEP file path")
    parser.add_argument("--face-name", default="1", help="Imported STEP face name to inspect")
    parser.add_argument("--radius-tolerance", type=float, default=1.0e-4)
    parser.add_argument("--no-dim-background", action="store_true")
    return parser.parse_args()


def qcolor(rgb):
    return Quantity_Color(float(rgb[0]), float(rgb[1]), float(rgb[2]), Quantity_TOC_RGB)


def safe_str(value):
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def read_step_with_face_names(path):
    reader = STEPControl_Reader()
    status = reader.ReadFile(path)

    if status != IFSelect_RetDone:
        raise RuntimeError("STEP read failed: {}".format(path))

    reader.TransferRoots()
    shape = reader.OneShape()

    ws = reader.WS()
    transfer_reader = ws.TransferReader()
    model = ws.Model()

    face_rows = []

    topo = TopologyExplorer(shape)
    for face_index, face in enumerate(topo.faces(), start=1):
        face_name = ""
        step_entity_id = None
        step_entity_type = ""

        try:
            item = transfer_reader.EntityFromShapeResult(face, 1)
        except Exception:
            item = None

        if item is not None:
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
                        face_name = safe_str(raw_name.ToCString())
            except Exception:
                pass

        face_rows.append(
            {
                "face": face,
                "face_index": face_index,
                "step_entity_id": step_entity_id,
                "step_entity_type": step_entity_type,
                "face_name": face_name,
            }
        )

    return shape, face_rows


def sanitize_radius(value):
    try:
        radius = abs(float(value))
    except Exception:
        return None
    return radius if radius > 0.0 else None


def fillet_geometry(face):
    try:
        surf = BRepAdaptor_Surface(face)
        surf_type = surf.GetType()
    except Exception:
        return "Unknown/Unsupported", None

    if surf_type == GeomAbs_Cylinder:
        try:
            return "Cylindrical", sanitize_radius(surf.Cylinder().Radius())
        except Exception:
            return "Cylindrical", None

    if surf_type == GeomAbs_Torus:
        try:
            return "Torus", sanitize_radius(surf.Torus().MinorRadius())
        except Exception:
            return "Torus", None

    if surf_type == GeomAbs_Sphere:
        try:
            return "Spherical", sanitize_radius(surf.Sphere().Radius())
        except Exception:
            return "Spherical", None

    return "Unknown/Unsupported", None


def radius_bucket(radius, tolerance):
    if radius is None:
        return None
    if tolerance <= 0:
        return radius
    return round(radius / tolerance) * tolerance


def radius_key(radius):
    if radius is None:
        return "UNKNOWN"
    return "{:.9g}".format(radius)


def group_color(group_index, radius_available=True):
    if not radius_available:
        return UNKNOWN_RADIUS_COLOR
    return RADIUS_GROUP_COLORS[(group_index - 1) % len(RADIUS_GROUP_COLORS)]


def collect_target_faces(face_rows, target_name, tolerance):
    rows = []
    target_name = str(target_name)

    for item in face_rows:
        face_name = item["face_name"]

        if face_name != target_name:
            continue

        surface_class, radius = fillet_geometry(item["face"])
        bucket = radius_bucket(radius, tolerance)

        rows.append(
            {
                "face": item["face"],
                "face_index": item["face_index"],
                "step_entity_id": item["step_entity_id"],
                "step_entity_type": item["step_entity_type"],
                "face_name": face_name,
                "surface_class": surface_class,
                "radius": radius,
                "radius_bucket": bucket,
                "radius_key": radius_key(bucket),
                "radius_available": radius is not None,
            }
        )

    return rows


def build_groups(rows):
    grouped = defaultdict(list)

    for row in rows:
        grouped[row["radius_key"]].append(row)

    def sort_key(key):
        if key == "UNKNOWN":
            return (1, 0.0)
        bucket = grouped[key][0]["radius_bucket"]
        return (0, float(bucket) if bucket is not None else 0.0)

    groups = []

    for group_id, key in enumerate(sorted(grouped.keys(), key=sort_key), start=1):
        group_rows = grouped[key]
        radii = [row["radius"] for row in group_rows if row["radius"] is not None]
        radius_available = bool(radii)

        groups.append(
            {
                "group_id": group_id,
                "radius_key": key,
                "radius_bucket": group_rows[0]["radius_bucket"],
                "faces": group_rows,
                "face_count": len(group_rows),
                "min_radius": min(radii) if radii else None,
                "max_radius": max(radii) if radii else None,
                "average_radius": sum(radii) / len(radii) if radii else None,
                "radius_available": radius_available,
                "color": group_color(group_id, radius_available),
            }
        )

    return groups


def display_result(shape, groups, dim_background=True):
    display, start_display, _, _ = init_display()
    context = display.Context

    if dim_background:
        background = AIS_Shape(shape)
        background.SetColor(qcolor(BACKGROUND_COLOR))
        background.SetTransparency(BACKGROUND_TRANSPARENCY)
        context.Display(background, False)

    for group in groups:
        color = qcolor(group["color"])

        for row in group["faces"]:
            ais_face = AIS_Shape(row["face"])
            ais_face.SetColor(color)
            ais_face.SetTransparency(0.0)
            context.Display(ais_face, False)

    display.FitAll()
    context.UpdateCurrentViewer()
    start_display()


def print_all_named_faces(face_rows):
    print("\nAll STEP faces with non-empty imported face name:")

    count = 0
    for row in face_rows:
        if row["face_name"]:
            count += 1
            print(
                "  face_index={idx}, step_id={sid}, name={name}, entity={etype}".format(
                    idx=row["face_index"],
                    sid=row["step_entity_id"],
                    name=row["face_name"],
                    etype=row["step_entity_type"],
                )
            )

    if count == 0:
        print("  No named faces found.")


def print_summary(input_path, target_name, face_rows, rows, groups):
    radius_count = sum(1 for row in rows if row["radius"] is not None)
    unknown_count = sum(1 for row in rows if row["radius"] is None)

    print("Input:", input_path)
    print("Total faces:", len(face_rows))
    print("Target face name:", target_name)
    print("Matched target faces:", len(rows))
    print("Faces with usable radius:", radius_count)
    print("Faces without usable radius:", unknown_count)
    print("Display groups:", len(groups))

    for group in groups:
        if group["radius_available"]:
            label = "radius={}".format(group["radius_key"])
        else:
            label = "UNKNOWN radius"

        print(
            "Group {gid}: {label}, faces={count}, min={minr}, max={maxr}, avg={avg}, color={color}".format(
                gid=group["group_id"],
                label=label,
                count=group["face_count"],
                minr=group["min_radius"],
                maxr=group["max_radius"],
                avg=group["average_radius"],
                color=group["color"],
            )
        )

        for row in group["faces"]:
            print(
                "    face_index={idx}, step_id={sid}, name={name}, type={stype}, radius={radius}".format(
                    idx=row["face_index"],
                    sid=row["step_entity_id"],
                    name=row["face_name"],
                    stype=row["surface_class"],
                    radius=row["radius"],
                )
            )


def main():
    args = parse_args()

    shape, face_rows = read_step_with_face_names(args.input)

    rows = collect_target_faces(
        face_rows=face_rows,
        target_name=args.face_name,
        tolerance=args.radius_tolerance,
    )

    groups = build_groups(rows)

    print_summary(args.input, args.face_name, face_rows, rows, groups)

    if not rows:
        print_all_named_faces(face_rows)

    display_result(
        shape=shape,
        groups=groups,
        dim_background=not args.no_dim_background,
    )


if __name__ == "__main__":
    main()
