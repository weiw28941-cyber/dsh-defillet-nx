"""
fillet_radius_estimator.py
输入一个 TopoDS_Face，输出圆角半径（float）。
失败时返回 None。
"""

import numpy as np
from OCC.Core.BRep import BRep_Tool
from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
from OCC.Core.GeomAbs import GeomAbs_Cylinder, GeomAbs_Torus
from OCC.Core.GeomLProp import GeomLProp_SLProps
from OCC.Core.TopoDS import TopoDS_Face


def estimate_fillet_radius(face: TopoDS_Face):
    """
    估计圆角 B-spline 曲面的圆角半径。

    Parameters
    ----------
    face : TopoDS_Face

    Returns
    -------
    float  — 估计的圆角半径（与模型单位一致）
    None   — 估计失败（采样点不足或曲率未定义）
    """
    adaptor = BRepAdaptor_Surface(face)
    surf_type = adaptor.GetType()

    # 解析曲面直接读取，精度最高
    if surf_type == GeomAbs_Cylinder:
        return adaptor.Cylinder().Radius()
    if surf_type == GeomAbs_Torus:
        return adaptor.Torus().MinorRadius()

    # B-spline / 自由曲面：主曲率采样法
    surface = BRep_Tool.Surface(face)

    u1, u2 = adaptor.FirstUParameter(), adaptor.LastUParameter()
    v1, v2 = adaptor.FirstVParameter(), adaptor.LastVParameter()

    # 边界缩进 12%，避免边界曲率失真
    m = 0.12
    u_samples = np.linspace(u1 + m * (u2 - u1), u2 - m * (u2 - u1), 20)
    v_samples = np.linspace(v1 + m * (v2 - v1), v2 - m * (v2 - v1), 20)

    radii = []
    for u in u_samples:
        for v in v_samples:
            try:
                p = GeomLProp_SLProps(surface, u, v, 2, 1e-7)
            except Exception:
                continue
            if not p.IsCurvatureDefined():
                continue
            kmax = max(abs(p.MinCurvature()), abs(p.MaxCurvature()))
            if kmax > 1e-6:
                radii.append(1.0 / kmax)

    if len(radii) < 3:
        return None

    radii = np.array(radii)

    # MAD 剔除异常值
    median = np.median(radii)
    mad = np.median(np.abs(radii - median))
    if mad > 1e-12:
        radii = radii[np.abs(radii - median) <= 2.5 * mad]

    return float(np.median(radii)) if len(radii) > 0 else None
