#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

from OCC.Core.STEPControl import STEPControl_Reader
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.BRepAlgoAPI import BRepAlgoAPI_Common
from OCC.Core.BRepGProp import brepgprop
from OCC.Core.GProp import GProp_GProps
from OCC.Core.ShapeFix import ShapeFix_Shape
from OCC.Core.BRepCheck import BRepCheck_Analyzer

# --- 新增 import ---
from OCC.Core.TopTools import TopTools_ListOfShape
from OCC.Core.Bnd import Bnd_Box
from OCC.Core.BRepBndLib import brepbndlib


def read_step(path):
    """Read a STEP file and return a TopoDS_Shape."""
    reader = STEPControl_Reader()
    status = reader.ReadFile(path)

    if status != IFSelect_RetDone:
        raise RuntimeError(f"Failed to read STEP file: {path}")

    reader.TransferRoots()
    shape = reader.OneShape()

    if shape.IsNull():
        raise RuntimeError(f"Empty shape in STEP file: {path}")

    return shape


def fix_shape(shape):
    """Try to repair minor topology issues."""
    fixer = ShapeFix_Shape(shape)
    fixer.Perform()
    return fixer.Shape()


def check_shape(shape, name="shape"):
    """Check whether shape is topologically valid."""
    analyzer = BRepCheck_Analyzer(shape)
    if not analyzer.IsValid():
        print(f"Warning: {name} may be invalid.")


def display_shape(shape):
    from OCC.Display.SimpleGui import init_display

    display, start_display, add_menu, add_function_to_menu = init_display()
    if isinstance(shape, list):
        for s in shape:
            display.DisplayShape(s)
    else:
        display.DisplayShape(shape)
    display.FitAll()
    start_display()
    return display


def volume(shape):
    """Compute volume of a shape. Works for closed solids / compsolids."""
    props = GProp_GProps()
    brepgprop.VolumeProperties(shape, props)
    return props.Mass()


def _bbox_diagonal(shape):
    """包围盒对角线长度，用来给 fuzzy 一个与模型尺度相关的量级。"""
    box = Bnd_Box()
    brepbndlib.Add(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return ((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2) ** 0.5


def boolean_common(shape_a, shape_b, fuzzy=0.0, use_glue=False):
    """
    Compute intersection shape A ∩ B.
    建议 3：用 SetArguments/SetTools/Build 而不是构造函数，保证后设的选项生效。
    建议 1：Fuzzy 容差，处理 STEP 导入后的近重合/微小错配。
    建议 4：NonDestructive，运算不修改输入 shape。
    建议 5：UseOBB，加速并减少无谓的 面/面 干涉计算。
    use_glue：建议 2（你跳过了）。两个完全一样的实体若仍算空，把它设为 True 最对症。
    """
    common_op = BRepAlgoAPI_Common()

    args = TopTools_ListOfShape()
    args.Append(shape_a)
    tools = TopTools_ListOfShape()
    tools.Append(shape_b)
    common_op.SetArguments(args)
    common_op.SetTools(tools)

    # 建议 1：Fuzzy
    if fuzzy and fuzzy > 0.0:
        common_op.SetFuzzyValue(fuzzy)

    # 建议 4：安全模式（hasattr 守卫，兼容不同 OCC 版本）
    if hasattr(common_op, "SetNonDestructive"):
        common_op.SetNonDestructive(True)

    # 建议 5：OBB
    if hasattr(common_op, "SetUseOBB"):
        common_op.SetUseOBB(True)

    # 建议 2（可选）：完全重合实体的粘合
    if use_glue and hasattr(common_op, "SetGlue"):
        from OCC.Core.BOPAlgo import BOPAlgo_GlueFull
        common_op.SetGlue(BOPAlgo_GlueFull)

    common_op.Build()

    if common_op.HasErrors():
        raise RuntimeError("Boolean common operation reported errors (HasErrors=True).")
    if hasattr(common_op, "HasWarnings") and common_op.HasWarnings():
        print("Warning: boolean common produced warnings.")

    result = common_op.Shape()
    if result.IsNull():
        return None
    return result


def compute_iou(step_a, step_b, fuzzy_factor=1e-6, use_glue=False):
    shape_a = fix_shape(read_step(step_a))
    shape_b = fix_shape(read_step(step_b))

    check_shape(shape_a, "shape A")
    check_shape(shape_b, "shape B")

    vol_a = volume(shape_a)
    vol_b = volume(shape_b)

    if vol_a <= 0:
        raise RuntimeError(f"Shape A has non-positive volume: {vol_a}")
    if vol_b <= 0:
        raise RuntimeError(f"Shape B has non-positive volume: {vol_b}")

    # 建议 1：按模型尺度自适应取 fuzzy，而不是写死一个绝对值
    diag = max(_bbox_diagonal(shape_a), _bbox_diagonal(shape_b))
    fuzzy = diag * fuzzy_factor

    inter_shape = boolean_common(shape_a, shape_b, fuzzy=fuzzy, use_glue=use_glue)

    inter_vol = 0.0 if inter_shape is None else volume(inter_shape)

    union_vol = vol_a + vol_b - inter_vol

    if union_vol <= 0:
        raise RuntimeError(f"Union volume is non-positive: {union_vol}")

    iou = inter_vol / union_vol

    return {
        "volume_a": vol_a,
        "volume_b": vol_b,
        "intersection_volume": inter_vol,
        "union_volume": union_vol,
        "fuzzy_used": fuzzy,
        "iou": iou,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Compute robust STEP volume IoU")
    parser.add_argument("step_a")
    parser.add_argument("step_b")
    args = parser.parse_args()

    # 若 fuzzy 仍算出 0，把 use_glue 改成 True 再试
    result = compute_iou(args.step_a, args.step_b, fuzzy_factor=1e-3, use_glue=True)

    print("STEP IoU Result")
    print("----------------")
    print(f"Volume A             : {result['volume_a']}")
    print(f"Volume B             : {result['volume_b']}")
    print(f"Intersection Volume  : {result['intersection_volume']}")
    print(f"Union Volume         : {result['union_volume']}")
    print(f"Fuzzy Used           : {result['fuzzy_used']}")
    print(f"IoU                  : {result['iou']}")


if __name__ == "__main__":
    main()
