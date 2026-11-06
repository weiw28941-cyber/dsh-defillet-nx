# -*- coding: utf-8 -*-
"""Map a FreeCAD FaceN selection to its original STEP ADVANCED_FACE entity."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any

from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCC.Core.BRepExtrema import BRepExtrema_DistShapeShape
from OCC.Core.GeomAbs import (
    GeomAbs_BSplineSurface,
    GeomAbs_BezierSurface,
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
from OCC.Core.STEPControl import STEPControl_Reader
from OCC.Core.TopAbs import TopAbs_FACE
from OCC.Core.TopExp import TopExp_Explorer
from OCC.Core.TopoDS import topods
from OCC.Core.gp import gp_Pnt


FACE_PATTERN = re.compile(r"^(?:Face)?(\d+)$", re.IGNORECASE)
ADVANCED_FACE_PATTERN = re.compile(
    r"#\s*(\d+)\s*=\s*ADVANCED_FACE\s*\(\s*'((?:[^']|'')*)'",
    re.IGNORECASE | re.DOTALL,
)
SURFACE_NAMES = {
    GeomAbs_Plane: "Plane",
    GeomAbs_Cylinder: "Cylinder",
    GeomAbs_Cone: "Cone",
    GeomAbs_Sphere: "Sphere",
    GeomAbs_Torus: "Torus",
    GeomAbs_BezierSurface: "BezierSurface",
    GeomAbs_BSplineSurface: "BSplineSurface",
    GeomAbs_SurfaceOfRevolution: "SurfaceOfRevolution",
    GeomAbs_SurfaceOfExtrusion: "SurfaceOfExtrusion",
    GeomAbs_OffsetSurface: "OffsetSurface",
    GeomAbs_OtherSurface: "OtherSurface",
}


def parse_face(value: str) -> int:
    match = FACE_PATTERN.fullmatch(value.strip())
    if not match:
        raise argparse.ArgumentTypeError("face must be FaceN or a positive integer")
    index = int(match.group(1))
    if index < 1:
        raise argparse.ArgumentTypeError("face index must be positive")
    return index


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Map FreeCAD FaceN to the originating STEP ADVANCED_FACE entity."
    )
    parser.add_argument("--input", required=True, help="Input STEP file")
    parser.add_argument("--face", required=True, type=parse_face, help="FaceN or N")
    parser.add_argument(
        "--selection-point",
        nargs=3,
        type=float,
        metavar=("X", "Y", "Z"),
        help="Optional FreeCAD selection coordinates",
    )
    parser.add_argument(
        "--point-tolerance",
        type=float,
        default=1.0e-3,
        help="Correct FaceN when its distance exceeds this value (default: 1e-3)",
    )
    parser.add_argument(
        "--ambiguity-tolerance",
        type=float,
        default=1.0e-7,
        help="Distance difference treated as a nearest-face tie",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--report", default="", help="Optional JSON report")
    parser.add_argument("--strict", action="store_true", help="Fail unless OCC entity mapping is verified")
    args = parser.parse_args(argv)

    args.input = str(Path(args.input).expanduser().resolve())
    args.report = str(Path(args.report).expanduser().resolve()) if args.report else ""
    if not os.path.isfile(args.input):
        parser.error("input STEP does not exist: {}".format(args.input))
    if args.point_tolerance < 0 or args.ambiguity_tolerance < 0:
        parser.error("distance tolerances must be non-negative")
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    return args


def parse_text_faces(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        text = stream.read()
    return [
        {
            "text_ordinal": ordinal,
            "step_entity_id": int(match.group(1)),
            "name": match.group(2).replace("''", "'"),
        }
        for ordinal, match in enumerate(ADVANCED_FACE_PATTERN.finditer(text), start=1)
    ]


def read_step(path: str):
    reader = STEPControl_Reader()
    if reader.ReadFile(path) != IFSelect_RetDone:
        raise RuntimeError("STEP read failed: {}".format(path))
    transferred = reader.TransferRoots()
    if transferred <= 0:
        raise RuntimeError("STEP transferred no roots: {}".format(path))
    shape = reader.OneShape()
    if shape.IsNull():
        raise RuntimeError("STEP produced a null shape: {}".format(path))
    return reader, shape


def enumerate_faces(shape) -> list[Any]:
    faces = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        faces.append(topods.Face(explorer.Current()))
        explorer.Next()
    return faces


def entity_info(reader, face) -> dict[str, Any]:
    transfer_reader = reader.WS().TransferReader()
    model = reader.WS().Model()
    # `mode=-1` searches all transfer-result levels. `mode=1` works for simple
    # single-root STEP files but returns None for faces below assembly/component
    # transfer binders, leaving every viewer face without a Part 21 identity.
    entity = transfer_reader.EntityFromShapeResult(face, -1)
    if entity is None:
        return {
            "verified": False,
            "step_entity_id": None,
            "entity_type": "",
            "mapping_error": "EntityFromShapeResult returned None",
        }

    entity_type = ""
    try:
        entity_type = str(entity.DynamicType().Name())
    except Exception:
        pass

    entity_id = None
    errors = []
    # IdentLabel is the literal Part 21 reference printed in the STEP file
    # (for example #1865). model.Number is only OCC's internal model ordinal
    # and can differ when header/auxiliary records are excluded.
    try:
        number = int(reader.StepModel().IdentLabel(entity))
        if number > 0:
            entity_id = number
    except Exception as exc:
        errors.append("IdentLabel: {}".format(exc))
    if entity_id is None:
        try:
            number = int(model.Number(entity))
            if number > 0:
                entity_id = number
        except Exception as exc:
            errors.append("model.Number: {}".format(exc))

    return {
        "verified": entity_id is not None,
        "step_entity_id": entity_id,
        "entity_type": entity_type,
        "mapping_error": "; ".join(errors) if entity_id is None else "",
    }


def surface_type(face) -> str:
    try:
        return SURFACE_NAMES.get(BRepAdaptor_Surface(face).GetType(), "Unknown")
    except Exception:
        return "Unknown"


def point_distance(face, vertex) -> tuple[float, list[float] | None]:
    extrema = BRepExtrema_DistShapeShape(vertex, face)
    extrema.Perform()
    if not extrema.IsDone():
        return float("inf"), None
    nearest = None
    try:
        point = extrema.PointOnShape2(1)
        nearest = [float(point.X()), float(point.Y()), float(point.Z())]
    except Exception:
        pass
    return float(extrema.Value()), nearest


def face_result(reader, faces, ordinal: int, text_faces) -> dict[str, Any]:
    face = faces[ordinal - 1]
    result = {
        "face_index": ordinal,
        "freecad_face": "Face{}".format(ordinal),
        "surface_type": surface_type(face),
    }
    result.update(entity_info(reader, face))
    text_row = text_faces[ordinal - 1] if ordinal <= len(text_faces) else None
    result["text_order_mapping"] = text_row
    result["text_order_agrees"] = bool(
        text_row
        and result["step_entity_id"] is not None
        and text_row["step_entity_id"] == result["step_entity_id"]
    )
    return result


def write_report(path: str, payload: dict[str, Any]) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    text_faces = parse_text_faces(args.input)
    reader, shape = read_step(args.input)
    faces = enumerate_faces(shape)
    if not faces:
        raise RuntimeError("STEP contains no transferred faces")
    if args.face > len(faces):
        raise ValueError(
            "Face{} is out of range; transferred face count={}".format(args.face, len(faces))
        )

    requested = face_result(reader, faces, args.face, text_faces)
    final_index = args.face
    corrected = False
    nearest_rows = []
    ambiguous = False

    if args.selection_point is not None:
        if not all(math.isfinite(value) for value in args.selection_point):
            raise ValueError("selection point must contain finite coordinates")
        vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*args.selection_point)).Vertex()
        distances = []
        for index, face in enumerate(faces, start=1):
            distance, nearest_point = point_distance(face, vertex)
            distances.append((distance, index, nearest_point))
        distances.sort(key=lambda item: (item[0], item[1]))
        requested_distance = next(item[0] for item in distances if item[1] == args.face)
        requested["selection_distance"] = requested_distance
        requested["point_within_tolerance"] = requested_distance <= args.point_tolerance
        if requested_distance > args.point_tolerance:
            final_index = distances[0][1]
            corrected = final_index != args.face
        best_distance = distances[0][0]
        tied = [
            item for item in distances if abs(item[0] - best_distance) <= args.ambiguity_tolerance
        ]
        ambiguous = len(tied) > 1
        for distance, index, nearest_point in distances[: args.top_k]:
            row = face_result(reader, faces, index, text_faces)
            row["selection_distance"] = distance
            row["nearest_point_on_face"] = nearest_point
            nearest_rows.append(row)

    final = face_result(reader, faces, final_index, text_faces)
    if args.selection_point is not None:
        match = next(row for row in nearest_rows if row["face_index"] == final_index) if any(
            row["face_index"] == final_index for row in nearest_rows
        ) else None
        if match:
            final["selection_distance"] = match["selection_distance"]
            final["nearest_point_on_face"] = match["nearest_point_on_face"]

    if ambiguous:
        status = "ambiguous"
    elif corrected:
        status = "corrected_by_point"
    elif args.selection_point is not None and final.get("verified"):
        status = "verified_by_point"
    elif final.get("verified"):
        status = "verified"
    elif final.get("text_order_mapping"):
        status = "text_order_only"
    else:
        status = "unresolved"

    payload = {
        "input": args.input,
        "request": {
            "freecad_face": "Face{}".format(args.face),
            "face_index": args.face,
            "selection_point": args.selection_point,
            "point_tolerance": args.point_tolerance,
            "ambiguity_tolerance": args.ambiguity_tolerance,
        },
        "counts": {
            "transferred_face_count": len(faces),
            "text_advanced_face_count": len(text_faces),
        },
        "requested_mapping": requested,
        "result": dict(final, status=status, corrected_by_point=corrected),
        "nearest_faces": nearest_rows,
    }

    entity_text = (
        "#{}".format(final["step_entity_id"])
        if final.get("step_entity_id") is not None
        else "unresolved"
    )
    print(
        "{} -> {} (OCC Face{}, status={}, corrected={})".format(
            "Face{}".format(args.face), entity_text, final_index, status, corrected
        )
    )
    if args.selection_point is not None:
        print("selection distance: {}".format(final.get("selection_distance")))
    print("entity type: {}".format(final.get("entity_type") or "unknown"))
    print("surface type: {}".format(final.get("surface_type") or "unknown"))
    print("text-order agreement: {}".format(final.get("text_order_agrees")))
    if ambiguous:
        print("warning: nearest selection point is ambiguous", file=sys.stderr)
    if args.report:
        write_report(args.report, payload)
        print("report: {}".format(args.report))

    if args.strict and (not final.get("verified") or ambiguous):
        return 5
    return 0 if final.get("verified") else 5


if __name__ == "__main__":
    raise SystemExit(main())
