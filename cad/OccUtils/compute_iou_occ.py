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


def read_step(path):
    """
    Read a STEP file and return a TopoDS_Shape.
    """
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
    """
    Try to repair minor topology issues.
    """
    fixer = ShapeFix_Shape(shape)
    fixer.Perform()
    return fixer.Shape()


def check_shape(shape, name="shape"):
    """
    Check whether shape is topologically valid.
    """
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
    """
    Compute volume of a shape.
    Works for closed solids / compsolids.
    """
    props = GProp_GProps()
    brepgprop.VolumeProperties(shape, props)
    return props.Mass()


def boolean_common(shape_a, shape_b):
    """
    Compute intersection shape A ∩ B.
    """
    common_op = BRepAlgoAPI_Common(shape_a, shape_b)
    common_op.Build()

    if not common_op.IsDone():
        raise RuntimeError("Boolean common operation failed.")

    result = common_op.Shape()

    if result.IsNull():
        return None

    return result


def compute_iou(step_a, step_b):
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

    inter_shape = boolean_common(shape_a, shape_b)

    if inter_shape is None:
        inter_vol = 0.0
    else:
        inter_vol = volume(inter_shape)

    union_vol = vol_a + vol_b - inter_vol

    if union_vol <= 0:
        raise RuntimeError(f"Union volume is non-positive: {union_vol}")

    iou = inter_vol / union_vol

    return {
        "volume_a": vol_a,
        "volume_b": vol_b,
        "intersection_volume": inter_vol,
        "union_volume": union_vol,
        "iou": iou,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Compute STEP volume IoU")
    parser.add_argument("step_a")
    parser.add_argument("step_b")
    args = parser.parse_args()

    result = compute_iou(args.step_a, args.step_b)

    print("STEP IoU Result")
    print("----------------")
    print(f"Volume A             : {result['volume_a']}")
    print(f"Volume B             : {result['volume_b']}")
    print(f"Intersection Volume  : {result['intersection_volume']}")
    print(f"Union Volume         : {result['union_volume']}")
    print(f"IoU                  : {result['iou']}")


if __name__ == "__main__":
    main()
