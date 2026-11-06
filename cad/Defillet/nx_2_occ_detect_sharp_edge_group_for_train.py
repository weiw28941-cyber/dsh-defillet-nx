# -*- coding: utf-8 -*-
"""
pythonocc-core 7.9.3

对比原始 STEP 和 defillet 后 STEP：
1. 找出 defillet 后新生成的边；
2. 从原始 STEP 中识别 fillet face 半径；
3. 对每条新边，根据重心最近的"有半径的 fillet face"匹配半径；
4. 按半径分组，同一半径的新边使用同一颜色高亮。

Example:
    python cad/Defillet/nx_2_occ_detect_sharp_edge_group_for_train.py ^
        --original "original.step" ^
        --defillet "defillet.step" ^
        --face-name 1
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from math import sqrt
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from OCC.Core.STEPControl import STEPControl_Reader, STEPControl_Writer, STEPControl_AsIs
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.StepRepr import StepRepr_RepresentationItem
from OCC.Core.STEPConstruct import stepconstruct_FindEntity
from OCC.Core.TopLoc import TopLoc_Location
from OCC.Core.TCollection import TCollection_HAsciiString

from OCC.Core.BRepAlgoAPI import BRepAlgoAPI_Section
from OCC.Core.TopExp import TopExp_Explorer
from OCC.Core.TopAbs import TopAbs_EDGE

from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
from OCC.Core.GeomAbs import (
    GeomAbs_Cylinder,
    GeomAbs_Sphere,
    GeomAbs_Torus,
    GeomAbs_BSplineSurface,          # ← 新增
)

from OCC.Core.GProp import GProp_GProps

try:
    from OCC.Core.BRepGProp import brepgprop
except Exception:
    brepgprop = None

try:
    from OCC.Core.BRepGProp import BRepGProp
except Exception:
    BRepGProp = None

from OCC.Core.AIS import AIS_Shape
from OCC.Core.Quantity import Quantity_Color, Quantity_TOC_RGB

from OCC.Display.SimpleGui import init_display
from OCC.Extend.TopologyUtils import TopologyExplorer

# ← 新增：bspline 半径估计接口
from cad.OccUtils.fillet_radius_estimator import estimate_fillet_radius

# TODO: 优化算法，匹配过程优化
DEFAULT_EDGE_INFO_SUFFIX = "_edge_info"

BACKGROUND_COLOR = (0.65, 0.65, 0.65)
BACKGROUND_TRANSPARENCY = 0.85

NEW_EDGE_WIDTH = 5.0

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


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--original",
        required=True,
        help="原始 STEP 文件路径",
    )

    parser.add_argument(
        "--defillet",
        required=True,
        help="去除 fillet 后的 STEP 文件路径",
    )

    parser.add_argument(
        "--edge-info-output",
        default=None,
        help=(
            "写入 edge JSON name 的 STEP 输出路径。"
            "默认使用 --defillet 文件名增加后缀 {}".format(DEFAULT_EDGE_INFO_SUFFIX)
        ),
    )

    parser.add_argument(
        "--face-name",
        default="1",
        help="原始 STEP 中 fillet face 的 imported STEP face name",
    )

    parser.add_argument(
        "--radius-tolerance",
        type=float,
        default=1.0e-4,
        help="半径分组 tolerance",
    )

    parser.add_argument(
        "--edge-width",
        type=float,
        default=NEW_EDGE_WIDTH,
        help="新边高亮线宽",
    )

    parser.add_argument(
        "--no-dim-background",
        action="store_true",
        help="不显示半透明背景模型",
    )

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


def suffixed_step_path(path, suffix):
    root, ext = os.path.splitext(path)

    if not ext:
        ext = ".step"

    return root + suffix + ext


def json_number_or_none(value):
    if value is None:
        return None

    try:
        return float(value)
    except Exception:
        return None


# ============================================================
# STEP 读取
# ============================================================

def read_step(path):
    reader = STEPControl_Reader()
    status = reader.ReadFile(path)

    if status != IFSelect_RetDone:
        raise RuntimeError("STEP read failed: {}".format(path))

    reader.TransferRoots()
    return reader.OneShape()


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


def collect_edges(shape):
    topo = TopologyExplorer(shape)
    return list(topo.edges())


# ============================================================
# 原有新边判断逻辑：不要动
# ============================================================

def is_edges_intersected(edge1, edge2):
    """
    按你给出的判断方式：
    只检查 BRepAlgoAPI_Section 结果里是否存在 TopAbs_EDGE。
    """
    try:
        sec = BRepAlgoAPI_Section(edge1, edge2)
        sec.Build()

        if not sec.IsDone():
            return False

        comm_shape = sec.Shape()
        explorer = TopExp_Explorer(comm_shape, TopAbs_EDGE)

        if explorer.More():
            return True

        return False

    except Exception:
        return False


def is_new_edge(defillet_edge, original_edges):
    """
    defillet_edge 与原始模型中任意 edge 都不 overlap，
    才认为是新边。
    """
    for original_edge in original_edges:
        if is_edges_intersected(defillet_edge, original_edge):
            return False

    return True


def collect_new_edges(defillet_edges, original_edges):
    new_edges = []

    total = len(defillet_edges)

    for index, edge in enumerate(defillet_edges, start=1):
        print("Checking defillet edge {}/{} ...".format(index, total))

        if is_new_edge(edge, original_edges):
            new_edges.append(
                {
                    "edge": edge,
                    "edge_index": index,
                }
            )
            print("  -> NEW edge: defillet_edge_index={}".format(index))

    return new_edges


# ============================================================
# fillet face 半径识别逻辑
# ============================================================

def sanitize_radius(value):
    try:
        radius = abs(float(value))
    except Exception:
        return None

    return radius if radius > 0.0 else None


def fillet_geometry(face):
    """
    解析面的几何类型与半径。
    - 确定性类型（Cylinder / Torus / Sphere）直接读取半径，返回 (surface_class, radius)。
    - BSpline 类型返回 ("BSpline", None)，由调用方按需估计。
    - 其他类型返回 ("Unknown/Unsupported", None)。
    """
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

    # ← 新增：BSpline 面标记，后续两阶段处理
    if surf_type == GeomAbs_BSplineSurface:
        return "BSpline", None

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


# ============================================================
# 重心计算
# ============================================================

def _linear_properties(shape, props):
    """
    兼容不同 pythonocc 版本。
    """
    if brepgprop is not None and hasattr(brepgprop, "LinearProperties"):
        brepgprop.LinearProperties(shape, props)
        return

    if BRepGProp is not None and hasattr(BRepGProp, "LinearProperties_s"):
        BRepGProp.LinearProperties_s(shape, props)
        return

    raise RuntimeError("No available BRepGProp LinearProperties API.")


def _surface_properties(shape, props):
    """
    兼容不同 pythonocc 版本。
    """
    if brepgprop is not None and hasattr(brepgprop, "SurfaceProperties"):
        brepgprop.SurfaceProperties(shape, props)
        return

    if BRepGProp is not None and hasattr(BRepGProp, "SurfaceProperties_s"):
        BRepGProp.SurfaceProperties_s(shape, props)
        return

    raise RuntimeError("No available BRepGProp SurfaceProperties API.")


def point_to_tuple(pnt):
    return (float(pnt.X()), float(pnt.Y()), float(pnt.Z()))


def distance3(p1, p2):
    dx = p1[0] - p2[0]
    dy = p1[1] - p2[1]
    dz = p1[2] - p2[2]
    return sqrt(dx * dx + dy * dy + dz * dz)


def edge_centroid(edge):
    props = GProp_GProps()
    _linear_properties(edge, props)
    return point_to_tuple(props.CentreOfMass())


def face_centroid(face):
    props = GProp_GProps()
    _surface_properties(face, props)
    return point_to_tuple(props.CentreOfMass())


# ============================================================
# 收集有半径的 fillet face（两阶段：确定半径 → BSpline snap）
# ============================================================

def _snap_to_nearest_bucket(estimated_radius, confirmed_buckets):
    """
    将 BSpline 估计半径 snap 到最近的已确定半径 bucket。
    confirmed_buckets 为已排序的 float 列表。
    """
    if not confirmed_buckets or estimated_radius is None:
        return None
    return min(confirmed_buckets, key=lambda b: abs(b - estimated_radius))


def collect_radius_fillet_faces(face_rows, target_name, tolerance):
    """
    两阶段收集有半径的 fillet face：

    阶段 1：收集几何上可直接解析半径的面（Cylinder / Torus / Sphere）。
    阶段 2：对 BSpline 面用 estimate_fillet_radius 估计粗半径，
            再 snap 到阶段 1 得出的确定半径集合，以消除估计噪声。

    最终返回两类面合并后的列表，供后续新边匹配使用。
    """
    target_name = str(target_name)

    confirmed_faces = []   # 阶段 1 结果
    bspline_rows = []      # 待阶段 2 处理的 BSpline 面
    matched_face_count = 0
    unknown_radius_count = 0

    # ----------------------------------------------------------
    # 阶段 1：确定性几何类型
    # ----------------------------------------------------------
    for row in face_rows:
        if row["face_name"] != target_name:
            continue

        matched_face_count += 1
        surface_class, radius = fillet_geometry(row["face"])

        if surface_class == "BSpline":
            # 留到阶段 2 处理
            bspline_rows.append(row)
            continue

        if radius is None:
            # Unknown/Unsupported 或解析失败
            unknown_radius_count += 1
            continue

        bucket = radius_bucket(radius, tolerance)

        try:
            centroid = face_centroid(row["face"])
        except Exception:
            unknown_radius_count += 1
            continue

        confirmed_faces.append(
            {
                "face": row["face"],
                "face_index": row["face_index"],
                "step_entity_id": row["step_entity_id"],
                "step_entity_type": row["step_entity_type"],
                "face_name": row["face_name"],
                "surface_class": surface_class,
                "radius": radius,
                "radius_bucket": bucket,
                "radius_key": radius_key(bucket),
                "centroid": centroid,
            }
        )

    # 阶段 1 结束后，收集所有确定 bucket 的有序列表，供 snap 使用
    confirmed_buckets = sorted(
        set(
            f["radius_bucket"]
            for f in confirmed_faces
            if f["radius_bucket"] is not None
        )
    )

    print("\nConfirmed radius buckets from stage-1 faces:", confirmed_buckets)

    # ----------------------------------------------------------
    # 阶段 2：BSpline 估计 + snap
    # ----------------------------------------------------------
    bspline_added = 0
    bspline_skipped = 0

    for row in bspline_rows:
        try:
            estimated = estimate_fillet_radius(row["face"])
        except Exception:
            estimated = None

        if estimated is None:
            bspline_skipped += 1
            unknown_radius_count += 1
            continue

        snapped_bucket = _snap_to_nearest_bucket(estimated, confirmed_buckets)

        if snapped_bucket is None:
            # 没有任何确定半径可参考，无法 snap
            bspline_skipped += 1
            unknown_radius_count += 1
            continue

        try:
            centroid = face_centroid(row["face"])
        except Exception:
            bspline_skipped += 1
            unknown_radius_count += 1
            continue

        snapped_radius = float(snapped_bucket)

        confirmed_faces.append(
            {
                "face": row["face"],
                "face_index": row["face_index"],
                "step_entity_id": row["step_entity_id"],
                "step_entity_type": row["step_entity_type"],
                "face_name": row["face_name"],
                "surface_class": "BSpline",
                "radius": snapped_radius,          # snap 后的确定半径
                "radius_bucket": snapped_bucket,
                "radius_key": radius_key(snapped_bucket),
                "centroid": centroid,
            }
        )
        bspline_added += 1
        print(
            "  BSpline face_index={}: estimated={:.6g} -> snapped={}".format(
                row["face_index"], estimated, snapped_bucket
            )
        )

    # ----------------------------------------------------------
    # 汇总日志
    # ----------------------------------------------------------
    print("\nFillet face radius source summary:")
    print("  target face name:", target_name)
    print("  matched fillet faces:", matched_face_count)
    print("  stage-1 confirmed radius faces:", len(confirmed_faces) - bspline_added)
    print("  stage-2 BSpline faces added (snapped):", bspline_added)
    print("  stage-2 BSpline faces skipped:", bspline_skipped)
    print("  total usable radius fillet faces:", len(confirmed_faces))
    print("  ignored faces without usable radius:", unknown_radius_count)

    return confirmed_faces


# ============================================================
# 给新边匹配最近 fillet face 半径
# ============================================================

def match_nearest_radius_face(edge_item, radius_faces):
    edge = edge_item["edge"]

    try:
        ec = edge_centroid(edge)
    except Exception:
        return {
            "matched": False,
            "edge_centroid": None,
            "nearest_face": None,
            "nearest_distance": None,
            "radius": None,
            "radius_bucket": None,
            "radius_key": "UNKNOWN",
        }

    best_face = None
    best_dist = None

    for face_item in radius_faces:
        dist = distance3(ec, face_item["centroid"])

        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_face = face_item

    if best_face is None:
        return {
            "matched": False,
            "edge_centroid": ec,
            "nearest_face": None,
            "nearest_distance": None,
            "radius": None,
            "radius_bucket": None,
            "radius_key": "UNKNOWN",
        }

    return {
        "matched": True,
        "edge_centroid": ec,
        "nearest_face": best_face,
        "nearest_distance": best_dist,
        "radius": best_face["radius"],
        "radius_bucket": best_face["radius_bucket"],
        "radius_key": best_face["radius_key"],
    }


def assign_radius_to_new_edges(new_edges, radius_faces):
    enriched = []

    for item in new_edges:
        match = match_nearest_radius_face(item, radius_faces)

        enriched_item = dict(item)
        enriched_item.update(
            {
                "matched": match["matched"],
                "edge_centroid": match["edge_centroid"],
                "nearest_face": match["nearest_face"],
                "nearest_distance": match["nearest_distance"],
                "radius": match["radius"],
                "radius_bucket": match["radius_bucket"],
                "radius_key": match["radius_key"],
            }
        )

        enriched.append(enriched_item)

    return enriched


def build_edge_radius_groups(new_edges_with_radius):
    grouped = defaultdict(list)

    for item in new_edges_with_radius:
        grouped[item["radius_key"]].append(item)

    def sort_key(key):
        if key == "UNKNOWN":
            return (1, 0.0)

        sample = grouped[key][0]
        bucket = sample["radius_bucket"]

        return (0, float(bucket) if bucket is not None else 0.0)

    groups = []

    for group_id, key in enumerate(sorted(grouped.keys(), key=sort_key), start=1):
        items = grouped[key]
        radius_available = key != "UNKNOWN"

        radii = [
            item["radius"]
            for item in items
            if item["radius"] is not None
        ]

        groups.append(
            {
                "group_id": group_id,
                "radius_key": key,
                "radius_bucket": items[0]["radius_bucket"],
                "edges": items,
                "edge_count": len(items),
                "radius_available": radius_available,
                "min_radius": min(radii) if radii else None,
                "max_radius": max(radii) if radii else None,
                "average_radius": sum(radii) / len(radii) if radii else None,
                "color": group_color(group_id, radius_available),
            }
        )

    return groups


# ============================================================
# 显示
# ============================================================

def display_result(defillet_shape, edge_groups, edge_width=NEW_EDGE_WIDTH, dim_background=True):
    if not edge_groups:
        display, start_display, _, _ = init_display()
        context = display.Context

        if dim_background:
            background = AIS_Shape(defillet_shape)
            background.SetColor(qcolor(BACKGROUND_COLOR))
            background.SetTransparency(BACKGROUND_TRANSPARENCY)
            context.Display(background, False)

        display.FitAll()
        context.UpdateCurrentViewer()
        start_display()
        return

    total_groups = len(edge_groups)

    for group_index, group in enumerate(edge_groups, start=1):
        if group["radius_available"]:
            label = "radius={}".format(group["radius_key"])
        else:
            label = "UNKNOWN radius"

        print(
            "\nRendering radius group {}/{}: {}, edges={}".format(
                group_index,
                total_groups,
                label,
                group["edge_count"],
            )
        )
        print("Close the display window to render the next radius group.")

        display, start_display, _, _ = init_display()
        context = display.Context

        if dim_background:
            background = AIS_Shape(defillet_shape)
            background.SetColor(qcolor(BACKGROUND_COLOR))
            background.SetTransparency(BACKGROUND_TRANSPARENCY)
            context.Display(background, False)

        color = qcolor(group["color"])

        for item in group["edges"]:
            ais_edge = AIS_Shape(item["edge"])
            ais_edge.SetColor(color)

            try:
                ais_edge.SetWidth(float(edge_width))
            except Exception:
                pass

            context.Display(ais_edge, False)

        display.FitAll()
        context.UpdateCurrentViewer()
        start_display()


# ============================================================
# 写出带 edge JSON name 的 defillet STEP
# ============================================================

def build_edge_index_to_component_id(edge_groups):
    """
    当前默认用 edge_groups 的 group_id 作为 component_id。

    如果你后续有自己的连通分量判断，只需要在 new_edges_with_radius
    的每个 item 里提前写入 item["component_id"]，本函数不会覆盖它。
    """
    edge_index_to_component_id = {}

    for group in edge_groups:
        component_id = group["group_id"]

        for item in group["edges"]:
            if "component_id" in item and item["component_id"] is not None:
                edge_index_to_component_id[item["edge_index"]] = item["component_id"]
            else:
                edge_index_to_component_id[item["edge_index"]] = component_id

    return edge_index_to_component_id


# def make_edge_info_for_step_name(item, component_id):
#     nearest_face = item.get("nearest_face")
#
#     if nearest_face is None:
#         nearest_face_info = None
#     else:
#         nearest_face_info = {
#             "face_index": nearest_face.get("face_index"),
#             "step_entity_id": nearest_face.get("step_entity_id"),
#             "step_entity_type": nearest_face.get("step_entity_type"),
#             "face_name": nearest_face.get("face_name"),
#             "surface_class": nearest_face.get("surface_class"),
#             "radius": json_number_or_none(nearest_face.get("radius")),
#             "radius_bucket": json_number_or_none(nearest_face.get("radius_bucket")),
#         }
#
#     return {
#         "edge_index": item.get("edge_index"),
#         "is_new_edge": True,
#         "component_id": component_id,
#         "radius": json_number_or_none(item.get("radius")),
#         "radius_bucket": json_number_or_none(item.get("radius_bucket")),
#         "radius_key": item.get("radius_key"),
#         "matched": bool(item.get("matched")),
#         "edge_centroid": item.get("edge_centroid"),
#         "nearest_distance": json_number_or_none(item.get("nearest_distance")),
#         "nearest_face": nearest_face_info,
#     }

def make_edge_info_for_step_name(item, component_id):
    return {
        "component_id": component_id,
        "radius": json_number_or_none(item.get("radius")),
    }


def write_defillet_step_with_edge_json_names(
        output_path,
        defillet_shape,
        new_edges_with_radius,
        edge_groups,
):
    """
    将 new_edges_with_radius 中每条新 edge 的信息写入 STEP 对应 edge entity 的 Name。

    Name 内容为 json.dumps(edge_info) 的结果。
    注意是 json.dumps，不是 json.dump；json.dump 是写文件句柄用的。
    """
    writer = STEPControl_Writer()
    writer.Transfer(defillet_shape, STEPControl_AsIs)

    finderp = writer.WS().TransferWriter().FinderProcess()
    loc = TopLoc_Location()

    component_map = build_edge_index_to_component_id(edge_groups)

    info_by_edge_index = {}
    for item in new_edges_with_radius:
        edge_index = item["edge_index"]
        component_id = component_map.get(edge_index)
        edge_info = make_edge_info_for_step_name(item, component_id)
        info_by_edge_index[edge_index] = edge_info

    edges = list(TopologyExplorer(defillet_shape).edges())

    written_count = 0
    missing_count = 0

    for edge_index, edge in enumerate(edges, start=1):
        edge_info = info_by_edge_index.get(edge_index)

        if edge_info is None:
            continue

        item = stepconstruct_FindEntity(finderp, edge, loc)

        if item is None:
            missing_count += 1
            print(
                "Cannot find STEP entity for edge: defillet_edge_index={}".format(
                    edge_index
                )
            )
            continue

        name = json.dumps(
            edge_info,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        item.SetName(TCollection_HAsciiString(name))
        written_count += 1

    status = writer.Write(output_path)

    print("\nWrote edge-info STEP:", output_path)
    print("  edge JSON names written:", written_count)
    print("  edge STEP entity missing:", missing_count)
    print("  writer status:", status)


# ============================================================
# 输出
# ============================================================

def print_edge_radius_summary(edge_groups):
    print("\n========== NEW EDGE RADIUS GROUPS ==========")

    if not edge_groups:
        print("No new edge groups.")
        print("===========================================\n")
        return

    for group in edge_groups:
        if group["radius_available"]:
            label = "radius={}".format(group["radius_key"])
        else:
            label = "UNKNOWN radius"

        print(
            "Group {gid}: {label}, edges={count}, min={minr}, max={maxr}, avg={avg}, color={color}".format(
                gid=group["group_id"],
                label=label,
                count=group["edge_count"],
                minr=group["min_radius"],
                maxr=group["max_radius"],
                avg=group["average_radius"],
                color=group["color"],
            )
        )

        for item in group["edges"]:
            nearest_face = item["nearest_face"]

            if nearest_face is None:
                print(
                    "    edge_index={edge_idx}, radius=UNKNOWN, nearest_face=None".format(
                        edge_idx=item["edge_index"],
                    )
                )
            else:
                print(
                    "    edge_index={edge_idx}, radius={radius}, nearest_distance={dist}, "
                    "matched_face_index={face_idx}, matched_step_id={step_id}, "
                    "matched_surface={surface}".format(
                        edge_idx=item["edge_index"],
                        radius=item["radius"],
                        dist=item["nearest_distance"],
                        face_idx=nearest_face["face_index"],
                        step_id=nearest_face["step_entity_id"],
                        surface=nearest_face["surface_class"],
                    )
                )

    print("===========================================\n")


def print_summary(
        original_path,
        defillet_path,
        original_edges,
        defillet_edges,
        new_edges,
        radius_faces,
        edge_groups,
):
    print("\n========== SUMMARY ==========")
    print("Original STEP:", original_path)
    print("Defillet STEP:", defillet_path)
    print("Original edge count:", len(original_edges))
    print("Defillet edge count:", len(defillet_edges))
    print("New edge count:", len(new_edges))
    print("Usable radius fillet face count:", len(radius_faces))
    print("New edge radius group count:", len(edge_groups))
    print("=============================\n")


# ============================================================
# main
# ============================================================

def main():
    args = parse_args()

    print("Reading original STEP with face names:", args.original)
    original_shape, original_face_rows = read_step_with_face_names(args.original)

    print("Reading defillet STEP:", args.defillet)
    defillet_shape = read_step(args.defillet)

    print("Collecting original edges ...")
    original_edges = collect_edges(original_shape)

    print("Collecting defillet edges ...")
    defillet_edges = collect_edges(defillet_shape)

    print("Original edge count:", len(original_edges))
    print("Defillet edge count:", len(defillet_edges))

    print("\nCollecting usable radius fillet faces from original STEP ...")
    radius_faces = collect_radius_fillet_faces(
        face_rows=original_face_rows,
        target_name=args.face_name,
        tolerance=args.radius_tolerance,
    )

    if not radius_faces:
        raise RuntimeError(
            "No usable radius fillet faces found. "
            "Please check --face-name or original STEP face names."
        )

    print("\nDetecting new defillet edges ...")
    new_edges = collect_new_edges(
        defillet_edges=defillet_edges,
        original_edges=original_edges,
    )

    print("\nAssigning nearest fillet radius to new edges ...")
    new_edges_with_radius = assign_radius_to_new_edges(
        new_edges=new_edges,
        radius_faces=radius_faces,
    )

    edge_groups = build_edge_radius_groups(new_edges_with_radius)

    print_summary(
        original_path=args.original,
        defillet_path=args.defillet,
        original_edges=original_edges,
        defillet_edges=defillet_edges,
        new_edges=new_edges,
        radius_faces=radius_faces,
        edge_groups=edge_groups,
    )

    print_edge_radius_summary(edge_groups)

    edge_info_output = args.edge_info_output
    if edge_info_output is None:
        edge_info_output = suffixed_step_path(args.defillet, DEFAULT_EDGE_INFO_SUFFIX)

    write_defillet_step_with_edge_json_names(
        output_path=edge_info_output,
        defillet_shape=defillet_shape,
        new_edges_with_radius=new_edges_with_radius,
        edge_groups=edge_groups,
    )

    display_result(
        defillet_shape=defillet_shape,
        edge_groups=edge_groups,
        edge_width=args.edge_width,
        dim_background=not args.no_dim_background,
    )


if __name__ == "__main__":
    main()
