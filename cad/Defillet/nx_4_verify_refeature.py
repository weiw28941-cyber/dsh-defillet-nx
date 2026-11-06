# -*- coding: utf-8 -*-
r"""Quantitative acceptance check for the defillet -> autofillet round trip.

This is deliberately a STANDALONE OCC script (same environment as
nx_0_detect_fillet_by_rule.py / nx_2_occ_detect_sharp_edge_group_*.py), NOT
an NXOpen script, so it can run right after nx_3_autofillet.py's NX process
exits, without needing pythonocc-core inside NX's own Python.

Problem this closes: nx_1_defillet.py / nx_3_autofillet.py only guard
against gross failures (bbox blew up, body count changed).  They cannot
detect "successfully created a plausible-looking fillet that is nonetheless
geometrically different from the original" -- which is exactly the failure
mode described as "defillet没问题，addFillet跟原始不一致".  This script
replaces "look at three renders side by side" with a single number: the
Boolean-intersection IoU between the ORIGINAL (pre-defillet) STEP and the
final re-featured STEP, plus volume/area/face-count deltas, so any STEP file
run through the pipeline gets an objective pass/fail instead of only a
visual spot check.

Usage:
    python cad/Defillet/nx_4_verify_refeature.py \
        --original  D:\...\original.step \
        --refeatured D:\...\test_after_fillet.step

    # or, let it resolve --original from the defillet_history.json that
    # nx_1_defillet.py already wrote next to its output:
    python cad/Defillet/nx_4_verify_refeature.py \
        --defillet-history D:\...\test_after_defillet.defillet_history.json \
        --refeatured D:\...\test_after_fillet.step

Exit code is 0 on pass, 1 on fail, 2 on error, so it can be wired straight
into a batch/regression harness.
"""

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from OCC.Extend.TopologyUtils import TopologyExplorer

from cad.OccUtils.compute_iou_occ_robust import (
    compute_iou,
    read_step,
    fix_shape,
    check_shape,
)

DEFAULT_IOU_THRESHOLD = 0.995
DEFAULT_VOLUME_RATIO_TOLERANCE = 0.01
DEFAULT_FUZZY_FACTOR = 1.0e-4


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--original", default="", help="Original STEP file, before defillet.")
    parser.add_argument(
        "--defillet-history",
        default="",
        help="Path to a defillet_history.json (as written by nx_1_defillet.py). "
             "Used to auto-resolve --original from its 'input' field when --original is not given.",
    )
    parser.add_argument("--refeatured", required=True, help="Final STEP produced by nx_3_autofillet.py.")
    parser.add_argument("--iou-threshold", type=float, default=DEFAULT_IOU_THRESHOLD)
    parser.add_argument("--volume-ratio-tolerance", type=float, default=DEFAULT_VOLUME_RATIO_TOLERANCE,
                         help="Allowed |volume_refeatured/volume_original - 1| before failing, "
                              "independent of the IoU check.")
    parser.add_argument("--fuzzy-factor", type=float, default=DEFAULT_FUZZY_FACTOR)
    parser.add_argument("--no-glue", dest="use_glue", action="store_false", default=True,
                         help="Disable BOPAlgo_GlueFull for the boolean common op.")
    parser.add_argument("--report", default="", help="Path to write a JSON verification report "
                                                       "(default: <refeatured>_verify_report.json).")
    return parser.parse_args(argv)


def resolve_original_path(args):
    if args.original:
        return args.original
    if args.defillet_history:
        with open(args.defillet_history, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
        original = payload.get("input")
        if original:
            return original
        raise RuntimeError(
            "defillet_history.json at {!r} has no 'input' field.".format(args.defillet_history)
        )
    raise RuntimeError("Give --original, or --defillet-history to resolve it automatically.")


def face_count(shape):
    return len(list(TopologyExplorer(shape).faces()))


def solid_count(shape):
    return len(list(TopologyExplorer(shape).solids()))


def run_verification(original_path, refeatured_path, iou_threshold, volume_ratio_tolerance,
                      fuzzy_factor, use_glue):
    if not os.path.isfile(original_path):
        raise RuntimeError("Original STEP not found: {}".format(original_path))
    if not os.path.isfile(refeatured_path):
        raise RuntimeError("Re-featured STEP not found: {}".format(refeatured_path))

    iou_result = compute_iou(
        original_path,
        refeatured_path,
        fuzzy_factor=fuzzy_factor,
        use_glue=use_glue,
    )

    original_shape = fix_shape(read_step(original_path))
    refeatured_shape = fix_shape(read_step(refeatured_path))
    check_shape(original_shape, "original")
    check_shape(refeatured_shape, "refeatured")

    original_faces = face_count(original_shape)
    refeatured_faces = face_count(refeatured_shape)
    original_solids = solid_count(original_shape)
    refeatured_solids = solid_count(refeatured_shape)

    volume_ratio = None
    volume_ratio_ok = True
    if iou_result["volume_a"] > 1.0e-9:
        volume_ratio = iou_result["volume_b"] / iou_result["volume_a"]
        volume_ratio_ok = abs(volume_ratio - 1.0) <= volume_ratio_tolerance

    iou_ok = iou_result["iou"] >= iou_threshold
    solid_count_ok = original_solids == refeatured_solids
    passed = iou_ok and volume_ratio_ok and solid_count_ok

    reasons = []
    if not iou_ok:
        reasons.append(
            "iou {:.6f} < threshold {:.6f}".format(iou_result["iou"], iou_threshold)
        )
    if not volume_ratio_ok:
        reasons.append(
            "volume ratio {} outside 1.0 +/- {}".format(volume_ratio, volume_ratio_tolerance)
        )
    if not solid_count_ok:
        reasons.append(
            "solid count changed: original={} refeatured={}".format(original_solids, refeatured_solids)
        )

    return {
        "original": os.path.abspath(original_path),
        "refeatured": os.path.abspath(refeatured_path),
        "passed": passed,
        "fail_reasons": reasons,
        "iou": iou_result["iou"],
        "iou_threshold": iou_threshold,
        "volume_original": iou_result["volume_a"],
        "volume_refeatured": iou_result["volume_b"],
        "volume_ratio": volume_ratio,
        "volume_ratio_tolerance": volume_ratio_tolerance,
        "intersection_volume": iou_result["intersection_volume"],
        "union_volume": iou_result["union_volume"],
        "fuzzy_used": iou_result["fuzzy_used"],
        "face_count_original": original_faces,
        "face_count_refeatured": refeatured_faces,
        "face_count_delta": refeatured_faces - original_faces,
        "solid_count_original": original_solids,
        "solid_count_refeatured": refeatured_solids,
    }


def main(argv=None):
    args = parse_args(argv)

    try:
        original_path = resolve_original_path(args)
        report = run_verification(
            original_path,
            args.refeatured,
            args.iou_threshold,
            args.volume_ratio_tolerance,
            args.fuzzy_factor,
            args.use_glue,
        )
    except Exception as exc:
        print("ERROR: verification could not run: {}".format(exc))
        return 2

    print("Verifying re-featured result against original")
    print("  original  :", report["original"])
    print("  refeatured:", report["refeatured"])
    print(json.dumps(report, indent=2, ensure_ascii=False))

    report_path = args.report or (os.path.splitext(args.refeatured)[0] + "_verify_report.json")
    os.makedirs(os.path.dirname(os.path.abspath(report_path)) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fp:
        json.dump(report, fp, indent=2, ensure_ascii=False)
    print("Report written:", report_path)

    if report["passed"]:
        print("PASS: iou={:.6f} volume_ratio={} solids match".format(
            report["iou"], report["volume_ratio"]
        ))
        return 0

    print("FAIL: {}".format("; ".join(report["fail_reasons"])))
    return 1


if __name__ == "__main__":
    sys.exit(main())
