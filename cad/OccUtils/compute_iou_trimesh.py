#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STEP IoU —— 体素化方式（快速版）
OCC  : 读 STEP → 修复 → BRepMesh → 临时 STL
trimesh: voxelized().fill() → 公共网格对齐 → 集合交/并 → IoU
"""

import os
import tempfile
import numpy as np
import trimesh
import trimesh.repair

from OCC.Core.STEPControl import STEPControl_Reader
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
from OCC.Core.BRepGProp import brepgprop
from OCC.Core.GProp import GProp_GProps
from OCC.Core.ShapeFix import ShapeFix_Shape
from OCC.Core.StlAPI import StlAPI_Writer


# ── 1. OCC 读取 & 修复 ────────────────────────────────────────────────────────

def read_step(path: str):
    reader = STEPControl_Reader()
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise RuntimeError(f"Failed to read: {path}")
    transferred = reader.TransferRoots()
    if transferred <= 0:
        raise RuntimeError(f"No transferable STEP roots: {path}")
    shape = reader.OneShape()
    if shape is None:
        raise RuntimeError(f"Empty shape: {path}")
    try:
        is_null = shape.IsNull()
    except Exception:
        is_null = False
    if is_null:
        raise RuntimeError(f"Empty shape: {path}")
    return shape


def fix_shape(shape):
    fixer = ShapeFix_Shape(shape)
    fixer.Perform()
    return fixer.Shape()


def occ_volume(shape) -> float:
    props = GProp_GProps()
    brepgprop.VolumeProperties(shape, props)
    return props.Mass()


# ── 2. OCC Shape → trimesh ────────────────────────────────────────────────────

def occ_shape_to_trimesh(shape,
                          linear_deflection: float = 0.01,
                          angular_deflection: float = 0.1) -> trimesh.Trimesh:
    mesher = BRepMesh_IncrementalMesh(shape, linear_deflection, False, angular_deflection)
    mesher.Perform()
    if not mesher.IsDone():
        raise RuntimeError("BRepMesh failed.")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".stl")
    os.close(tmp_fd)
    try:
        StlAPI_Writer().Write(shape, tmp_path)
        if os.path.getsize(tmp_path) == 0:
            raise RuntimeError("Empty STL.")
        # process=True: merge_vertices + remove_degenerate + fix_normals
        mesh = trimesh.load_mesh(tmp_path, process=True, force="mesh")
    finally:
        os.unlink(tmp_path)

    if isinstance(mesh, trimesh.Scene):
        geoms = list(mesh.geometry.values())
        mesh = trimesh.util.concatenate(geoms)

    trimesh.repair.fix_winding(mesh)
    return mesh


# ── 3. 体素化 IoU（核心）─────────────────────────────────────────────────────

def compute_iou_voxel(mesh_a: trimesh.Trimesh,
                       mesh_b: trimesh.Trimesh,
                       n_voxels_along_longest: int = 100) -> float | None:
    """
    1. 按两 mesh 公共包围盒自动确定 pitch
    2. 各自体素化并填充内部
    3. 把体素中心坐标对齐到同一整数网格
    4. IoU = |grid_A ∩ grid_B| / |grid_A ∪ grid_B|

    n_voxels_along_longest: 最长轴上的体素数，越大越精确但越慢。
    100 对应约 1% 精度，对几毫米零件已经足够。
    """
    lo = np.minimum(mesh_a.bounds[0], mesh_b.bounds[0])
    hi = np.maximum(mesh_a.bounds[1], mesh_b.bounds[1])
    extent = float(np.max(hi - lo))
    if extent <= 0:
        return None

    pitch = extent / n_voxels_along_longest
    print(f"  [voxel] pitch={pitch:.4f}  extent={extent:.4f}")

    # 体素化 + 填充实体内部
    vox_a = mesh_a.voxelized(pitch=pitch).fill()
    vox_b = mesh_b.voxelized(pitch=pitch).fill()

    # 各自体素中心（世界坐标）→ 对齐到公共整数网格
    def to_index_set(points: np.ndarray) -> set:
        idx = np.round((points - lo) / pitch).astype(np.int32)
        return set(map(tuple, idx))

    idx_a = to_index_set(vox_a.points)
    idx_b = to_index_set(vox_b.points)

    n_inter = len(idx_a & idx_b)
    n_union = len(idx_a | idx_b)

    print(f"  [voxel] |A|={len(idx_a)}  |B|={len(idx_b)}"
          f"  inter={n_inter}  union={n_union}")

    if n_union == 0:
        return None

    return n_inter / n_union


# ── 4. 主接口 ─────────────────────────────────────────────────────────────────

def compute_iou(step_a: str, step_b: str,
                n_voxels: int = 100) -> dict:

    shape_a = fix_shape(read_step(step_a))
    shape_b = fix_shape(read_step(step_b))

    vol_a_occ = occ_volume(shape_a)
    vol_b_occ = occ_volume(shape_b)
    print(f"[OCC] vol_A={vol_a_occ:.4f}  vol_B={vol_b_occ:.4f}")

    print("[info] Tessellating A ...")
    mesh_a = occ_shape_to_trimesh(shape_a)
    print(f"       faces={len(mesh_a.faces)}, watertight={mesh_a.is_watertight}")

    print("[info] Tessellating B ...")
    mesh_b = occ_shape_to_trimesh(shape_b)
    print(f"       faces={len(mesh_b.faces)}, watertight={mesh_b.is_watertight}")

    print(f"[info] Voxel IoU (n_voxels={n_voxels}) ...")
    iou = compute_iou_voxel(mesh_a, mesh_b, n_voxels_along_longest=n_voxels)

    return {
        "volume_a_occ": vol_a_occ,
        "volume_b_occ": vol_b_occ,
        "iou": iou if iou is not None else float("nan"),
    }


# ── 5. main ───────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Compute voxel STEP IoU")
    parser.add_argument("step_a")
    parser.add_argument("step_b")
    parser.add_argument("--n-voxels", type=int, default=100)
    args = parser.parse_args()

    result = compute_iou(args.step_a, args.step_b, n_voxels=args.n_voxels)

    print("\nSTEP IoU Result")
    print("───────────────")
    print(f"Volume A (OCC) : {result['volume_a_occ']:.4f}")
    print(f"Volume B (OCC) : {result['volume_b_occ']:.4f}")
    print(f"IoU            : {result['iou']:.6f}")


if __name__ == "__main__":
    main()
