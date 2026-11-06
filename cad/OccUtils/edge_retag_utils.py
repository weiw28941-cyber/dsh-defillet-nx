# -*- coding: utf-8 -*-
"""
edge_retag_utils.py

公共边标签迁移工具。

通过几何特征（中点坐标、边长、切线方向）将 source shape 中有名称的 edge
与 target shape 中的 edge 做尺度无关匹配，返回匹配结果供调用方注入标签。

所有距离判据以 target shape 的 BBox 对角线归一化，与模型绝对尺度无关。

─── 公共接口 ─────────────────────────────────────────────────────────────────

    match_named_edges(
        named_edges,           # [(TopoDS_Edge, name_str), ...]  source 侧有名边
        target_shape,          # TopoDS_Shape                    待打标签的 shape
        bbox_diag=None,        # float | None  归一化因子，None 时自动计算
        dist_ratio_threshold,  # 归一化中点距离上限（相对 bbox_diag）
        length_ratio_threshold,# 边长相对差上限
        tangent_dot_threshold, # 切向量 |dot| 下限
        verbose,               # 是否打印日志
    ) -> (matched, unmatched_source_indices)

    matched : list[dict]
        每项包含：
            target_edge       TopoDS_Edge  匹配到的 target 边
            name              str          来自 source 的标签
            source_edge       TopoDS_Edge  对应的 source 边
            normalized_dist   float        归一化中点距离
            length_ratio      float        边长相对差
            tangent_dot       float        切向量 |dot|

    unmatched_source_indices : list[int]
        named_edges 中未能匹配的下标（供调用方记录/告警）。

─── 辅助函数（可单独复用）───────────────────────────────────────────────────

    compute_bbox_diagonal(shape)        -> float
    get_edge_descriptor(edge)           -> dict | None
    extract_edge_descriptors(shape)     -> list[dict]
"""

import math

import numpy as np

from OCC.Core.BRepAdaptor import BRepAdaptor_Curve
from OCC.Core.BRepBndLib import brepbndlib_Add
from OCC.Core.Bnd import Bnd_Box
from OCC.Core.GProp import GProp_GProps
from OCC.Core.BRepGProp import brepgprop_LinearProperties
from OCC.Core.gp import gp_Vec
from OCC.Extend.TopologyUtils import TopologyExplorer


# ══════════════════════════════════════════════════════════════════════════════
# 基础几何工具
# ══════════════════════════════════════════════════════════════════════════════

def compute_bbox_diagonal(shape):
    """
    计算 shape 的包围盒对角线长度。

    用作距离归一化因子，使匹配阈值与模型绝对尺度无关。

    返回
    ----
    float  对角线长度（与模型单位相同）
    """
    box = Bnd_Box()
    brepbndlib_Add(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    dx = xmax - xmin
    dy = ymax - ymin
    dz = zmax - zmin
    diag = math.sqrt(dx * dx + dy * dy + dz * dz)
    return diag


def get_edge_descriptor(edge):
    """
    计算单条 edge 的几何描述子。

    返回
    ----
    dict  包含以下键：
        midpoint  np.ndarray (3,)  参数中点处的空间坐标
        length    float            边长（BRepGProp 线积分）
        tangent   np.ndarray (3,)  参数中点处归一化切向量

    若 edge 退化（零长、无法求导），返回 None。
    """
    adaptor = BRepAdaptor_Curve(edge)
    u0 = adaptor.FirstParameter()
    u1 = adaptor.LastParameter()

    if abs(u1 - u0) < 1e-12:
        return None

    # 参数中点
    um = (u0 + u1) * 0.5

    # 中点坐标
    pm = adaptor.Value(um)
    mid = np.array([pm.X(), pm.Y(), pm.Z()])

    # 中点切向量
    dv = gp_Vec()
    adaptor.D1(um, pm, dv)
    tan_arr = np.array([dv.X(), dv.Y(), dv.Z()])
    tan_norm = np.linalg.norm(tan_arr)
    if tan_norm < 1e-12:
        return None
    tan_arr = tan_arr / tan_norm

    # 边长
    props = GProp_GProps()
    brepgprop_LinearProperties(edge, props)
    length = props.Mass()
    if length < 1e-12:
        return None

    return {
        "midpoint": mid,
        "length":   length,
        "tangent":  tan_arr,
    }


def extract_edge_descriptors(shape):
    """
    提取 shape 中全部 edge 的几何描述子。

    退化边（get_edge_descriptor 返回 None）被跳过。

    返回
    ----
    list[dict]  每项在 get_edge_descriptor 结果基础上增加：
        edge  TopoDS_Edge  对应的拓扑对象
    """
    result = []
    for edge in TopologyExplorer(shape).edges():
        desc = get_edge_descriptor(edge)
        if desc is not None:
            desc["edge"] = edge
            result.append(desc)
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 核心匹配接口
# ══════════════════════════════════════════════════════════════════════════════

def match_named_edges(
    named_edges,
    target_shape,
    bbox_diag=None,
    dist_ratio_threshold=0.02,
    length_ratio_threshold=0.20,
    tangent_dot_threshold=0.90,
    verbose=True,
):
    """
    将 source 侧有名称的 edge 与 target_shape 中的 edge 做几何匹配。

    匹配策略
    --------
    1. 对每对 (source_edge_i, target_edge_j) 计算三项指标：
         norm_dist  = |mid_i - mid_j| / bbox_diag
         len_ratio  = |len_i - len_j| / max(len_i, len_j)
         tan_dot    = |dot(tan_i, tan_j)|     （符号不定，取绝对值）

    2. 同时满足以下条件视为候选：
         norm_dist  < dist_ratio_threshold
         len_ratio  < length_ratio_threshold
         tan_dot    > tangent_dot_threshold

    3. 对所有候选按 norm_dist 升序排列，贪婪分配：
         每个 source edge 和每个 target edge 各自最多匹配一次。
         先处理距离最近的候选，确保最优对先被分配。

    参数
    ----
    named_edges : list of (TopoDS_Edge, str)
        source shape 中有名称的 edge 列表，格式为 (edge, name_str)。
    target_shape : TopoDS_Shape
        待打标签的目标 shape。
    bbox_diag : float or None
        归一化因子，None 时自动计算 target_shape 的 BBox 对角线。
    dist_ratio_threshold : float
        归一化中点距离上限，默认 0.02（BBox 对角线的 2%）。
    length_ratio_threshold : float
        边长相对差上限，默认 0.20（20%）。
    tangent_dot_threshold : float
        切向量 |dot| 下限，默认 0.90（约 ±26° 以内）。
    verbose : bool
        是否打印每次匹配结果及汇总信息。

    返回
    ----
    matched : list[dict]
        成功匹配的项，每项包含：
            target_edge     TopoDS_Edge
            name            str
            source_edge     TopoDS_Edge
            normalized_dist float
            length_ratio    float
            tangent_dot     float

    unmatched_source_indices : list[int]
        named_edges 中未能匹配的下标列表。
    """

    # ── 1. 归一化因子 ──────────────────────────────────────────────────────────
    if bbox_diag is None:
        bbox_diag = compute_bbox_diagonal(target_shape)

    if bbox_diag < 1e-12:
        raise ValueError(
            "target_shape 的 BBox 对角线近似为零，无法进行距离归一化。"
        )

    inv_diag = 1.0 / bbox_diag

    if verbose:
        print("[retag] target BBox 对角线 = {:.6f}".format(bbox_diag))
        print(
            "[retag] 阈值: norm_dist < {}  len_ratio < {}  tan_dot > {}".format(
                dist_ratio_threshold, length_ratio_threshold, tangent_dot_threshold
            )
        )
        print(
            "[retag] source 有名 edge: {}".format(len(named_edges))
        )

    # ── 2. 提取 target 描述子 ──────────────────────────────────────────────────
    target_descs = extract_edge_descriptors(target_shape)

    if verbose:
        print("[retag] target 有效 edge: {}".format(len(target_descs)))

    # ── 3. 枚举所有满足三重条件的候选对 ──────────────────────────────────────
    candidates = []  # (norm_dist, si, ti, name, src_edge, info_dict)

    for si, (src_edge, name) in enumerate(named_edges):
        src_desc = get_edge_descriptor(src_edge)
        if src_desc is None:
            if verbose:
                print("[retag] SKIP source #{}: 退化边 name={}".format(si, name))
            continue

        src_mid = src_desc["midpoint"]
        src_len = src_desc["length"]
        src_tan = src_desc["tangent"]

        for ti, tgt_desc in enumerate(target_descs):
            # 归一化中点距离
            norm_dist = float(
                np.linalg.norm(src_mid - tgt_desc["midpoint"]) * inv_diag
            )
            if norm_dist > dist_ratio_threshold:
                continue

            # 边长相对差（已尺度无关）
            max_len = max(src_len, tgt_desc["length"])
            len_ratio = float(abs(src_len - tgt_desc["length"]) / max_len)
            if len_ratio > length_ratio_threshold:
                continue

            # 切向量一致性（符号不定，取绝对值）
            tan_dot = float(abs(np.dot(src_tan, tgt_desc["tangent"])))
            if tan_dot < tangent_dot_threshold:
                continue

            candidates.append((
                norm_dist,
                si,
                ti,
                name,
                src_edge,
                {
                    "normalized_dist": norm_dist,
                    "length_ratio":    len_ratio,
                    "tangent_dot":     tan_dot,
                },
            ))

    # ── 4. 按 norm_dist 升序贪婪分配 ──────────────────────────────────────────
    candidates.sort(key=lambda x: x[0])

    matched_si = set()
    matched_ti = set()
    matched    = []

    for norm_dist, si, ti, name, src_edge, info in candidates:
        if si in matched_si or ti in matched_ti:
            continue

        matched_si.add(si)
        matched_ti.add(ti)

        matched.append({
            "target_edge":     target_descs[ti]["edge"],
            "name":            name,
            "source_edge":     src_edge,
            "normalized_dist": info["normalized_dist"],
            "length_ratio":    info["length_ratio"],
            "tangent_dot":     info["tangent_dot"],
        })

        if verbose:
            print(
                "[retag] MATCH  si={:3d} -> ti={:4d} | "
                "norm_dist={:.5f}  len_ratio={:.3f}  tan_dot={:.4f} | "
                "name={}".format(
                    si, ti,
                    info["normalized_dist"],
                    info["length_ratio"],
                    info["tangent_dot"],
                    name,
                )
            )

    # ── 5. 汇总 ───────────────────────────────────────────────────────────────
    unmatched_source_indices = [
        si for si in range(len(named_edges)) if si not in matched_si
    ]

    if verbose:
        print(
            "[retag] 完成: 匹配 {} / {}  未匹配 {}".format(
                len(matched),
                len(named_edges),
                len(unmatched_source_indices),
            )
        )

    return matched, unmatched_source_indices
