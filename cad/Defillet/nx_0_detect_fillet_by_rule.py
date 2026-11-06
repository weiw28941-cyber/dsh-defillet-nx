# -*- coding: utf-8 -*-

import argparse
import json
import math
import os
import re
import sys
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from OCC.Core.STEPControl import (
    STEPControl_Reader,
)
from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
from OCC.Core.BRep import BRep_Tool
from OCC.Core.BRepLProp import BRepLProp_SLProps
from OCC.Core.BRepTools import breptools
from OCC.Core.GeomAbs import (
    GeomAbs_Cylinder,
    GeomAbs_BSplineSurface,
    GeomAbs_Sphere, GeomAbs_Torus,
)
from OCC.Core.TopAbs import TopAbs_REVERSED
from OCC.Core.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCC.Core.AIS import AIS_Shape
from OCC.Display.SimpleGui import init_display
from OCC.Extend.TopologyUtils import TopologyExplorer
from collections import Counter

# =========================
# 写死配置
# =========================

TARGET_NAME = "1"

TARGET_FACE_COLOR = (1.0, 0.1, 0.0)
BACKGROUND_COLOR = (0.65, 0.65, 0.65)
BACKGROUND_TRANSPARENCY = 0.82
CONVEXITY_CURVATURE_EPS = 1.0e-8
CONVEXITY_MIN_SIGN_RATIO = 0.60
CONVEXITY_SAMPLE_COUNT = 3
FULL_CYLINDER_U_SPAN_RATIO = 0.92
DEFAULT_MAX_FILLET_RADIUS = 12.0
PROFILE_CYLINDER_PROTECT_ABOVE_RADIUS = 5.0
PROFILE_AXIS_MIN_COUNT = 4
PROFILE_AXIS_MIN_RATIO = 0.50
PROFILE_AXIS_PURE_EXTRUSION_RATIO = 0.95
PROFILE_AXIS_PURE_EXTRUSION_MIN_COUNT = 20
PROFILE_AXIS_PURE_EXTRUSION_INTERVAL_RATIO = 0.70
PROFILE_AXIS_INTERVAL_TOLERANCE = 1.0e-3
PROFILE_AXIS_ALIGNMENT_DOT = 0.985
PROFILE_COAXIAL_DISTANCE_TOLERANCE = 1.0e-3
PROFILE_COAXIAL_RADIUS_DELTA_TOLERANCE = 1.0e-3
PROFILE_COAXIAL_MIN_AXIS_LENGTH_RATIO = 2.0
PROFILE_COAXIAL_MAX_PROFILE_RADIUS_RATIO = 2.0
FILLET_RADIUS_TOLERANCE = 1.0e-4
CURVATURE_EPS = 1.0e-8
BSPLINE_CURVATURE_SAMPLE_COUNT = 3
BSPLINE_MIN_FILLET_SAMPLE_RATIO = 0.35
BSPLINE_MIN_FILLET_SAMPLES = 2
MAX_CYLINDER_FILLET_ANGLE_DEGREES = 120.0
FULL_SPHERE_CAP_U_SPAN_RATIO = 0.85
MIN_SPHERE_CAP_V_SPAN_RATIO = 0.40
MAX_FILLET_RADIUS = DEFAULT_MAX_FILLET_RADIUS
MAX_CYLINDER_FILLET_ANGLE_RADIANS = math.radians(MAX_CYLINDER_FILLET_ANGLE_DEGREES)
VERBOSE_FACE_LOGS = False
PROTECT_PROFILE_CYLINDERS = True
PROFILE_CYLINDER_AXES = []
PROFILE_CYLINDER_FACES = []


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target-name", default=TARGET_NAME)
    parser.add_argument(
        "--max-fillet-radius",
        type=float,
        default=DEFAULT_MAX_FILLET_RADIUS,
        help="Largest radius to mark as a removable fillet face.",
    )
    parser.add_argument(
        "--max-cylinder-angle-deg",
        type=float,
        default=MAX_CYLINDER_FILLET_ANGLE_DEGREES,
        help="Skip cylindrical faces whose trimmed angle is wider than this.",
    )
    parser.add_argument(
        "--protect-profile-cylinders",
        dest="protect_profile_cylinders",
        action="store_true",
        default=True,
        help=(
            "Protect large cylindrical faces aligned with the dominant cylinder axis; "
            "these are usually sketch/profile arcs extruded into side walls."
        ),
    )
    parser.add_argument(
        "--no-protect-profile-cylinders",
        dest="protect_profile_cylinders",
        action="store_false",
        help="Disable dominant-axis profile-cylinder protection.",
    )
    parser.add_argument(
        "--profile-cylinder-protect-above-radius",
        type=float,
        default=PROFILE_CYLINDER_PROTECT_ABOVE_RADIUS,
        help="Only protect dominant-axis cylindrical faces above this radius.",
    )
    parser.add_argument("--verbose-faces", action="store_true")
    parser.add_argument("--no-display", action="store_true")
    return parser.parse_args(argv)


def qcolor(rgb):
    return Quantity_Color(
        float(rgb[0]),
        float(rgb[1]),
        float(rgb[2]),
        Quantity_TOC_RGB,
    )


def read_step(filename):
    reader = STEPControl_Reader()
    status = reader.ReadFile(filename)

    if status != IFSelect_RetDone:
        raise RuntimeError("Failed to read STEP file: {}".format(filename))

    transferred = reader.TransferRoots()
    if transferred <= 0:
        print("No transferable STEP roots:", filename)
        return None

    shape = reader.OneShape()
    if is_null_shape(shape):
        print("STEP reader produced a null shape:", filename)
        return None
    return shape


def is_null_shape(shape):
    if shape is None:
        return True
    try:
        return bool(shape.IsNull())
    except Exception:
        return False


def shape_faces(shape):
    if is_null_shape(shape):
        return []
    try:
        return list(TopologyExplorer(shape).faces())
    except Exception as exc:
        print("Cannot iterate STEP faces: {}".format(exc))
        return []


def get_face_type_name(face):
    try:
        surface = BRepAdaptor_Surface(face)
        surface_type = surface.GetType()

        if surface_type == GeomAbs_Cylinder:
            return "Cylinder"

        if surface_type == GeomAbs_BSplineSurface:
            return "BSplineSurface"

        if surface_type == GeomAbs_Sphere:
            return "Sphere"

        if surface_type == GeomAbs_Torus:
            return "Torus"

        return "Other"

    except Exception:
        return "Unknown"


def cylinder_u_span(face):
    try:
        u_min, u_max, _v_min, _v_max = uv_bounds(face)
        return abs(float(u_max) - float(u_min))
    except Exception:
        return None


def uv_bounds(face):
    return breptools.UVBounds(face)


def face_radius(face):
    try:
        surface = BRepAdaptor_Surface(face)
        surface_type = surface.GetType()
        if surface_type == GeomAbs_Cylinder:
            return float(surface.Cylinder().Radius())
        if surface_type == GeomAbs_Sphere:
            return float(surface.Sphere().Radius())
        if surface_type == GeomAbs_Torus:
            return float(surface.Torus().MinorRadius())
    except Exception:
        return None
    return None


def cylinder_axis_vector(face):
    try:
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_Cylinder:
            return None
        direction = surface.Cylinder().Axis().Direction()
        return (
            float(direction.X()),
            float(direction.Y()),
            float(direction.Z()),
        )
    except Exception:
        return None


def cylinder_axis_point(face):
    try:
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_Cylinder:
            return None
        location = surface.Cylinder().Location()
        return (
            float(location.X()),
            float(location.Y()),
            float(location.Z()),
        )
    except Exception:
        return None


def normalize_vector(vector):
    if vector is None:
        return None
    length = math.sqrt(sum(float(value) * float(value) for value in vector))
    if length <= 1.0e-12:
        return None
    return tuple(float(value) / length for value in vector)


def vector_subtract(vector_a, vector_b):
    if vector_a is None or vector_b is None:
        return None
    return tuple(float(a) - float(b) for a, b in zip(vector_a, vector_b))


def vector_cross(vector_a, vector_b):
    if vector_a is None or vector_b is None:
        return None
    ax, ay, az = vector_a
    bx, by, bz = vector_b
    return (
        ay * bz - az * by,
        az * bx - ax * bz,
        ax * by - ay * bx,
    )


def vector_length(vector):
    if vector is None:
        return None
    return math.sqrt(sum(float(value) * float(value) for value in vector))


def vector_dot(vector_a, vector_b):
    if vector_a is None or vector_b is None:
        return None
    return sum(float(a) * float(b) for a, b in zip(vector_a, vector_b))


def axis_alignment(axis_a, axis_b):
    axis_a = normalize_vector(axis_a)
    axis_b = normalize_vector(axis_b)
    if axis_a is None or axis_b is None:
        return 0.0
    return abs(sum(a * b for a, b in zip(axis_a, axis_b)))


def parallel_axis_line_distance(point_a, axis_a, point_b, axis_b):
    axis_a = normalize_vector(axis_a)
    axis_b = normalize_vector(axis_b)
    if axis_a is None or axis_b is None:
        return None
    if axis_alignment(axis_a, axis_b) < PROFILE_AXIS_ALIGNMENT_DOT:
        return None
    delta = vector_subtract(point_b, point_a)
    return vector_length(vector_cross(delta, axis_a))


def vertex_point(vertex):
    try:
        point = BRep_Tool.Pnt(vertex)
        return (
            float(point.X()),
            float(point.Y()),
            float(point.Z()),
        )
    except Exception:
        return None


def face_axis_interval(face, axis):
    axis = normalize_vector(axis)
    if axis is None:
        return None
    projections = []
    try:
        vertices = list(TopologyExplorer(face).vertices())
    except Exception:
        return None
    for vertex in vertices:
        point = vertex_point(vertex)
        projection = vector_dot(point, axis)
        if projection is not None:
            projections.append(float(projection))
    if not projections:
        return None
    return (min(projections), max(projections))


def interval_key(interval, tolerance=PROFILE_AXIS_INTERVAL_TOLERANCE):
    if interval is None or tolerance <= 0.0:
        return None
    return (
        int(round(float(interval[0]) / tolerance)),
        int(round(float(interval[1]) / tolerance)),
    )


def vector_label(vector):
    vector = normalize_vector(vector)
    if vector is None:
        return "(unknown)"
    return "({:.3f},{:.3f},{:.3f})".format(vector[0], vector[1], vector[2])


def detect_dominant_cylinder_axes(shape):
    faces = shape_faces(shape)
    axis_groups = []

    for face in faces:
        if get_face_type_name(face) != "Cylinder":
            continue
        axis = normalize_vector(cylinder_axis_vector(face))
        if axis is None:
            continue

        matched_group = None
        for group in axis_groups:
            if axis_alignment(axis, group["axis"]) >= PROFILE_AXIS_ALIGNMENT_DOT:
                matched_group = group
                break

        if matched_group is None:
            axis_groups.append({"axis": axis, "count": 1})
        else:
            matched_group["count"] += 1

    cylinder_count = sum(group["count"] for group in axis_groups)
    if cylinder_count <= 0:
        return []

    axis_groups.sort(key=lambda group: group["count"], reverse=True)
    dominant = axis_groups[0]
    ratio = dominant["count"] / float(cylinder_count)

    print("\nCylinder axis distribution:")
    for group in axis_groups:
        print(
            "  axis {}: {} face(s)".format(
                vector_label(group["axis"]),
                group["count"],
            )
        )

    if dominant["count"] < PROFILE_AXIS_MIN_COUNT:
        return []
    if ratio < PROFILE_AXIS_MIN_RATIO:
        return []
    return [dominant["axis"]]


def detect_pure_extrusion_profile_cylinder_faces(shape, protected_axes):
    if not protected_axes:
        return []

    cylinder_rows = collect_cylinder_rows(shape)
    if not cylinder_rows:
        return []

    protected_faces = []
    for protected_axis in protected_axes:
        aligned_rows = [
            row
            for row in cylinder_rows
            if axis_alignment(row["axis"], protected_axis) >= PROFILE_AXIS_ALIGNMENT_DOT
        ]
        ratio = len(aligned_rows) / float(len(cylinder_rows))
        if ratio < PROFILE_AXIS_PURE_EXTRUSION_RATIO:
            continue
        if len(aligned_rows) < PROFILE_AXIS_PURE_EXTRUSION_MIN_COUNT:
            continue
        interval_counter = Counter(
            key
            for key in (interval_key(row.get("axis_interval")) for row in aligned_rows)
            if key is not None
        )
        if not interval_counter:
            continue
        dominant_interval_count = interval_counter.most_common(1)[0][1]
        interval_ratio = dominant_interval_count / float(len(aligned_rows))
        if interval_ratio < PROFILE_AXIS_PURE_EXTRUSION_INTERVAL_RATIO:
            continue
        protected_faces.extend(row["face"] for row in aligned_rows)
    return merge_unique_faces(protected_faces)


def detect_profile_cylinder_faces(shape, protected_axes):
    if not protected_axes:
        return []

    cylinder_rows = []
    for face in shape_faces(shape):
        if get_face_type_name(face) != "Cylinder":
            continue
        radius = face_radius(face)
        if radius is None:
            continue
        if radius <= PROFILE_CYLINDER_PROTECT_ABOVE_RADIUS + FILLET_RADIUS_TOLERANCE:
            continue
        axis = cylinder_axis_vector(face)
        if not any(
            axis_alignment(axis, protected_axis) >= PROFILE_AXIS_ALIGNMENT_DOT
            for protected_axis in protected_axes
        ):
            continue
        cylinder_rows.append(
            {
                "face": face,
                "radius": radius,
                "axis": normalize_vector(axis),
            }
        )

    protected_faces = []
    for index, row in enumerate(cylinder_rows):
        for other_index, other_row in enumerate(cylinder_rows):
            if index == other_index:
                continue
            if not faces_share_edge(row["face"], other_row["face"]):
                continue
            if axis_alignment(row["axis"], other_row["axis"]) < PROFILE_AXIS_ALIGNMENT_DOT:
                continue
            protected_faces.append(row["face"])
            break

    unique_faces = []
    for face in protected_faces:
        if any(face.IsSame(existing) for existing in unique_faces):
            continue
        unique_faces.append(face)
    return unique_faces


def collect_cylinder_rows(shape):
    rows = []
    for face in shape_faces(shape):
        if get_face_type_name(face) != "Cylinder":
            continue
        radius = face_radius(face)
        axis = cylinder_axis_vector(face)
        point = cylinder_axis_point(face)
        if radius is None or axis is None or point is None:
            continue
        rows.append(
            {
                "face": face,
                "radius": radius,
                "axis": normalize_vector(axis),
                "point": point,
                "axis_interval": face_axis_interval(face, axis),
            }
        )
    return rows


def detect_coaxial_profile_cylinder_faces(shape):
    cylinder_rows = collect_cylinder_rows(shape)
    protected_faces = []

    for index, row in enumerate(cylinder_rows):
        if not is_long_coaxial_profile_row(row):
            continue
        for other_index, other_row in enumerate(cylinder_rows):
            if index == other_index:
                continue
            if not is_long_coaxial_profile_row(other_row):
                continue
            radius_delta = abs(float(row["radius"]) - float(other_row["radius"]))
            if radius_delta <= PROFILE_COAXIAL_RADIUS_DELTA_TOLERANCE:
                continue
            if not is_profile_like_coaxial_radius_pair(row["radius"], other_row["radius"]):
                continue
            line_distance = parallel_axis_line_distance(
                row["point"],
                row["axis"],
                other_row["point"],
                other_row["axis"],
            )
            if line_distance is None:
                continue
            if line_distance > PROFILE_COAXIAL_DISTANCE_TOLERANCE:
                continue
            protected_faces.append(row["face"])
            break

    unique_faces = []
    for face in protected_faces:
        if any(face.IsSame(existing) for existing in unique_faces):
            continue
        unique_faces.append(face)
    return unique_faces


def is_profile_like_coaxial_radius_pair(radius_a, radius_b):
    try:
        radius_a = abs(float(radius_a))
        radius_b = abs(float(radius_b))
    except Exception:
        return False
    min_radius = min(radius_a, radius_b)
    max_radius = max(radius_a, radius_b)
    if min_radius <= FILLET_RADIUS_TOLERANCE:
        return False
    ratio = max_radius / min_radius
    return ratio <= PROFILE_COAXIAL_MAX_PROFILE_RADIUS_RATIO + FILLET_RADIUS_TOLERANCE


def axis_interval_length(interval):
    if not interval:
        return None
    try:
        length = abs(float(interval[1]) - float(interval[0]))
    except Exception:
        return None
    if not math.isfinite(length):
        return None
    return length


def is_long_coaxial_profile_row(row):
    radius = row.get("radius")
    length = axis_interval_length(row.get("axis_interval"))
    if radius is None or length is None:
        return False
    if radius <= FILLET_RADIUS_TOLERANCE:
        return False
    return length / float(radius) >= PROFILE_COAXIAL_MIN_AXIS_LENGTH_RATIO


def merge_unique_faces(*face_lists):
    merged = []
    for face_list in face_lists:
        for face in face_list or []:
            if any(face.IsSame(existing) for existing in merged):
                continue
            merged.append(face)
    return merged


def filter_supported_torus_target_rows(target_rows):
    """Avoid deleting isolated torus corner patches beside protected bend/profile faces."""
    non_torus_rows = [
        row for row in target_rows if row.get("face_type") != "Torus"
    ]
    if not non_torus_rows:
        return [
            row for row in target_rows if row.get("face_type") != "Torus"
        ]

    filtered = []
    skipped = 0
    for row in target_rows:
        if row.get("face_type") != "Torus":
            filtered.append(row)
            continue
        if any(faces_share_edge(row["face"], other["face"]) for other in non_torus_rows):
            filtered.append(row)
            continue
        skipped += 1

    if skipped:
        print("Skipped isolated torus target faces:", skipped)
    return filtered


def is_profile_cylinder_protected(face, radius):
    if not PROTECT_PROFILE_CYLINDERS:
        return False
    if radius is None:
        return False
    return any(face.IsSame(protected_face) for protected_face in PROFILE_CYLINDER_FACES)


def radius_within_fillet_limit(radius):
    if radius is None:
        return True
    return radius <= MAX_FILLET_RADIUS + FILLET_RADIUS_TOLERANCE


def curvature_radius(curvature):
    curvature = abs(float(curvature))
    if curvature <= CURVATURE_EPS:
        return None
    return 1.0 / curvature


def is_bspline_blend_candidate(face):
    try:
        surface = BRepAdaptor_Surface(face, True)
        u_min, u_max, v_min, v_max = uv_bounds(face)
    except Exception:
        return False

    props = BRepLProp_SLProps(2, 1.0e-6)
    props.SetSurface(surface)

    valid_samples = 0
    fillet_like_samples = 0
    for u in uv_sample_values(u_min, u_max, BSPLINE_CURVATURE_SAMPLE_COUNT):
        for v in uv_sample_values(v_min, v_max, BSPLINE_CURVATURE_SAMPLE_COUNT):
            try:
                props.SetParameters(u, v)
                if not props.IsCurvatureDefined():
                    continue
                radii = [
                    curvature_radius(props.MaxCurvature()),
                    curvature_radius(props.MinCurvature()),
                ]
            except Exception:
                continue

            valid_samples += 1
            if any(radius_within_fillet_limit(radius) for radius in radii if radius is not None):
                fillet_like_samples += 1

    if valid_samples <= 0:
        return False

    if fillet_like_samples < BSPLINE_MIN_FILLET_SAMPLES:
        return False

    return fillet_like_samples / float(valid_samples) >= BSPLINE_MIN_FILLET_SAMPLE_RATIO


def is_cylinder_blend_candidate(face):
    """Keep functional full cylinders, delete partial cylindrical blend strips."""
    return cylinder_blend_decision(face)[0]


def cylinder_blend_decision(face):
    """Return (is_target, reason) for diagnostics without changing policy."""
    radius = face_radius(face)
    if not radius_within_fillet_limit(radius):
        return False, "radius>{:.6g}".format(MAX_FILLET_RADIUS)
    if is_profile_cylinder_protected(face, radius):
        return False, "profile-protected"

    u_span = cylinder_u_span(face)
    if u_span is None:
        return True, "target:no-u-span"
    if u_span >= (2.0 * math.pi * FULL_CYLINDER_U_SPAN_RATIO):
        return False, "full-cylinder"
    if u_span <= MAX_CYLINDER_FILLET_ANGLE_RADIANS:
        return True, "target:partial-cylinder"
    return False, "angle>{:.6g}deg".format(math.degrees(MAX_CYLINDER_FILLET_ANGLE_RADIANS))


def fmt_optional_float(value, digits=4):
    if value is None:
        return "None"
    try:
        if not math.isfinite(float(value)):
            return str(value)
        return ("{:.%df}" % digits).format(float(value))
    except Exception:
        return str(value)


def verbose_cylinder_decision(face_index, face):
    radius = face_radius(face)
    u_span = cylinder_u_span(face)
    u_span_deg = None if u_span is None else math.degrees(u_span)
    axis = normalize_vector(cylinder_axis_vector(face))
    interval = face_axis_interval(face, axis)
    is_target, reason = cylinder_blend_decision(face)
    print(
        "Cylinder decision: face_index={idx}, target={target}, reason={reason}, "
        "radius={radius}, u_span_deg={u_span}, axis=({ax},{ay},{az}), interval=({i0},{i1})".format(
            idx=face_index,
            target=str(bool(is_target)).lower(),
            reason=reason,
            radius=fmt_optional_float(radius, 6),
            u_span=fmt_optional_float(u_span_deg, 2),
            ax=fmt_optional_float(axis[0], 3) if axis else "None",
            ay=fmt_optional_float(axis[1], 3) if axis else "None",
            az=fmt_optional_float(axis[2], 3) if axis else "None",
            i0=fmt_optional_float(interval[0], 4) if interval else "None",
            i1=fmt_optional_float(interval[1], 4) if interval else "None",
        )
    )


def is_spherical_cap_face(face):
    """Detect functional dome/hemisphere caps, not local corner blend patches."""
    try:
        u_min, u_max, v_min, v_max = uv_bounds(face)
    except Exception:
        return False

    u_span = abs(float(u_max) - float(u_min))
    v_span = abs(float(v_max) - float(v_min))
    if not math.isfinite(u_span) or not math.isfinite(v_span):
        return False

    full_u = u_span >= (2.0 * math.pi * FULL_SPHERE_CAP_U_SPAN_RATIO)
    broad_v = v_span >= (math.pi * MIN_SPHERE_CAP_V_SPAN_RATIO)
    return full_u and broad_v


def is_sphere_blend_candidate(face):
    radius = face_radius(face)
    if not radius_within_fillet_limit(radius):
        return False

    # A revolved spherical cap/hemisphere is a terminal design feature.  Corner
    # blends are local patches and do not span a full turn around the sphere.
    if is_spherical_cap_face(face):
        return False

    return True


def is_target_face(face):
    face_type = get_face_type_name(face)
    if face_type == "Cylinder":
        return is_cylinder_blend_candidate(face)
    if face_type == "Sphere":
        return is_sphere_blend_candidate(face)
    if face_type == "Torus":
        return radius_within_fillet_limit(face_radius(face))
    if face_type == "BSplineSurface":
        return is_bspline_blend_candidate(face)
    return False


def uv_sample_values(first, last, sample_count):
    if sample_count <= 1:
        return [(first + last) * 0.5]
    span = last - first
    if not math.isfinite(span) or abs(span) <= 1.0e-12:
        return [(first + last) * 0.5]
    return [first + span * (idx + 1) / (sample_count + 1) for idx in range(sample_count)]


def face_convexity_sign(face):
    try:
        surface = BRepAdaptor_Surface(face, True)
        u_min, u_max, v_min, v_max = uv_bounds(face)
    except Exception:
        return 0

    props = BRepLProp_SLProps(2, 1.0e-6)
    props.SetSurface(surface)

    orientation_factor = -1.0 if face.Orientation() == TopAbs_REVERSED else 1.0
    votes = []

    for u in uv_sample_values(u_min, u_max, CONVEXITY_SAMPLE_COUNT):
        for v in uv_sample_values(v_min, v_max, CONVEXITY_SAMPLE_COUNT):
            try:
                props.SetParameters(u, v)
                if not props.IsCurvatureDefined():
                    continue
                mean_curvature = float(props.MeanCurvature()) * orientation_factor
            except Exception:
                continue

            if abs(mean_curvature) <= CONVEXITY_CURVATURE_EPS:
                continue
            votes.append(mean_curvature)

    if not votes:
        return 0

    positive = sum(1 for value in votes if value > 0.0)
    negative = sum(1 for value in votes if value < 0.0)
    total = positive + negative
    if total == 0:
        return 0

    majority = max(positive, negative) / float(total)
    if majority < CONVEXITY_MIN_SIGN_RATIO:
        return 0

    return 1 if positive > negative else -1


def faces_share_edge(face_a, face_b):
    edges_a = list(TopologyExplorer(face_a).edges())
    edges_b = list(TopologyExplorer(face_b).edges())
    for edge_a in edges_a:
        for edge_b in edges_b:
            if edge_a.IsSame(edge_b):
                return True
    return False


def find_parent(parent, index):
    while parent[index] != index:
        parent[index] = parent[parent[index]]
        index = parent[index]
    return index


def union_parent(parent, index_a, index_b):
    root_a = find_parent(parent, index_a)
    root_b = find_parent(parent, index_b)
    if root_a == root_b:
        return
    if root_b < root_a:
        root_a, root_b = root_b, root_a
    parent[root_b] = root_a


def assign_delete_component_ids(face_rows):
    parent = list(range(len(face_rows)))
    signs = [face_convexity_sign(row["face"]) for row in face_rows]
    split_pairs = []

    for i in range(len(face_rows)):
        for j in range(i + 1, len(face_rows)):
            if not faces_share_edge(face_rows[i]["face"], face_rows[j]["face"]):
                continue

            sign_i = signs[i]
            sign_j = signs[j]
            if sign_i != 0 and sign_j != 0 and sign_i * sign_j < 0:
                split_pairs.append((i, j, sign_i, sign_j))
                continue

            union_parent(parent, i, j)

    root_to_component = {}
    next_component = 1
    for index, row in enumerate(face_rows):
        root = find_parent(parent, index)
        if root not in root_to_component:
            root_to_component[root] = next_component
            next_component += 1
        row["defillet_component_id"] = root_to_component[root]
        row["convexity_sign"] = signs[index]

    print("\nDefillet OCC component split summary")
    print("  target faces:", len(face_rows))
    print("  components:", next_component - 1)
    print("  opposite-convexity adjacent pairs split:", len(split_pairs))
    for pair_index, (i, j, sign_i, sign_j) in enumerate(split_pairs, start=1):
        print(
            "    split pair {}: face_index={} sign={} <-> face_index={} sign={}".format(
                pair_index,
                face_rows[i]["face_index"],
                sign_i,
                face_rows[j]["face_index"],
                sign_j,
            )
        )

    return face_rows


def make_defillet_face_name(target_name, component_id, convexity_sign=None):
    payload = {
        "defillet_target": str(target_name),
        "defillet_component_id": int(component_id),
    }
    if convexity_sign is not None:
        payload["defillet_convexity_sign"] = int(convexity_sign)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def escape_step_string(value):
    return str(value).replace("'", "''")


def escape_step_string_bytes(value):
    return escape_step_string(value).encode("ascii")


def replace_advanced_face_names_in_step(input_filename, output_filename, names_by_face_index):
    with open(input_filename, "rb") as fp:
        text = fp.read()

    pattern = re.compile(
        rb"(ADVANCED_FACE\s*\(\s*)'((?:[^']|'')*)'",
        re.IGNORECASE | re.DOTALL,
    )

    replacements = 0
    chunks = []
    cursor = 0

    for face_index, match in enumerate(pattern.finditer(text), start=1):
        new_name = names_by_face_index.get(face_index)
        if new_name is None:
            continue

        chunks.append(text[cursor:match.start(2)])
        chunks.append(escape_step_string_bytes(new_name))
        cursor = match.end(2)
        replacements += 1

    chunks.append(text[cursor:])

    os.makedirs(os.path.dirname(os.path.abspath(output_filename)), exist_ok=True)
    with open(output_filename, "wb") as fp:
        fp.write(b"".join(chunks))

    return replacements


def write_step_with_target_face_names(output_filename, shape, target_name=TARGET_NAME):
    faces = shape_faces(shape)
    if not faces:
        print("\nWrite STEP skipped: no faces in input shape.")
        return []

    target_rows = []
    skipped_type_counter = Counter()

    for face_index, face in enumerate(faces, start=1):
        face_type = get_face_type_name(face)

        if face_type not in ("Cylinder", "BSplineSurface", "Sphere", "Torus"):
            skipped_type_counter[face_type] += 1
            continue

        if not is_target_face(face):
            skipped_type_counter["{}(kept)".format(face_type)] += 1
            continue

        target_rows.append(
            {
                "face": face,
                "face_index": face_index,
                "face_type": face_type,
            }
        )

    target_rows = filter_supported_torus_target_rows(target_rows)
    target_rows = assign_delete_component_ids(target_rows)
    written_faces = []
    names_by_face_index = {}

    for row in target_rows:
        face = row["face"]
        face_index = row["face_index"]
        face_type = row["face_type"]
        component_id = row["defillet_component_id"]
        convexity_sign = row.get("convexity_sign")

        face_name = make_defillet_face_name(target_name, component_id, convexity_sign)
        names_by_face_index[face_index] = face_name
        written_faces.append(face)

        if VERBOSE_FACE_LOGS:
            print(
                "Write name: face_index={}, type={}, component_id={}, name={}".format(
                    face_index,
                    face_type,
                    component_id,
                    face_name,
                )
            )

    replacement_count = replace_advanced_face_names_in_step(
        INPUT_STEP_PATH,
        output_filename,
        names_by_face_index,
    )

    print("\nWrite STEP finished")
    print("Output STEP:", output_filename)
    print("Written target faces:", len(written_faces))
    print("Replaced ADVANCED_FACE names:", replacement_count)
    if skipped_type_counter:
        print("Skipped face distribution:")
        for geom_type, count in skipped_type_counter.most_common():
            print("  {}: {}".format(geom_type, count))

    return written_faces


def collect_target_faces(shape):
    faces = shape_faces(shape)

    target_rows = []

    target_type_counter = {
        "Cylinder": 0,
        "BSplineSurface": 0,
        "Sphere": 0,
        "Torus": 0,
    }

    all_type_counter = Counter()

    for face_index, face in enumerate(faces, start=1):
        face_type = get_face_type_name(face)

        all_type_counter[face_type] += 1
        if VERBOSE_FACE_LOGS and face_type == "Cylinder":
            verbose_cylinder_decision(face_index, face)

        if face_type in target_type_counter and is_target_face(face):
            target_rows.append(
                {
                    "face": face,
                    "face_index": face_index,
                    "face_type": face_type,
                }
            )

    target_rows = filter_supported_torus_target_rows(target_rows)
    target_faces = [row["face"] for row in target_rows]
    for row in target_rows:
        target_type_counter[row["face_type"]] += 1

    print("\n==============================")
    print("STEP FACE GEOMETRY STATISTICS")
    print("==============================")

    print("Input STEP:", INPUT_STEP_PATH)
    print("Total faces:", len(faces))

    print("\nAll face geometry distribution:")
    for geom_type, count in all_type_counter.most_common():
        print("  {}: {}".format(geom_type, count))

    print("\nDetected target face distribution:")
    for geom_type, count in target_type_counter.items():
        print("  {}: {}".format(geom_type, count))

    print("\nTotal detected target faces:", len(target_faces))

    return target_faces, target_type_counter


def display_target_faces(shape, target_faces):
    display, start_display, _, _ = init_display()
    context = display.Context

    ais_bg = AIS_Shape(shape)
    ais_bg.SetColor(qcolor(BACKGROUND_COLOR))
    ais_bg.SetTransparency(BACKGROUND_TRANSPARENCY)
    context.Display(ais_bg, False)

    face_color = qcolor(TARGET_FACE_COLOR)

    for face in target_faces:
        ais_face = AIS_Shape(face)
        ais_face.SetColor(face_color)
        ais_face.SetTransparency(0.0)
        context.Display(ais_face, False)

    display.FitAll()
    context.UpdateCurrentViewer()
    start_display()


def main(argv=None):
    args = parse_args(argv)
    global INPUT_STEP_PATH, OUTPUT_STEP_PATH, TARGET_NAME, VERBOSE_FACE_LOGS
    global MAX_FILLET_RADIUS, MAX_CYLINDER_FILLET_ANGLE_RADIANS
    global PROTECT_PROFILE_CYLINDERS, PROFILE_CYLINDER_PROTECT_ABOVE_RADIUS
    global PROFILE_CYLINDER_AXES, PROFILE_CYLINDER_FACES
    INPUT_STEP_PATH = args.input
    OUTPUT_STEP_PATH = args.output
    TARGET_NAME = args.target_name
    VERBOSE_FACE_LOGS = args.verbose_faces
    MAX_FILLET_RADIUS = args.max_fillet_radius
    MAX_CYLINDER_FILLET_ANGLE_RADIANS = math.radians(args.max_cylinder_angle_deg)
    PROTECT_PROFILE_CYLINDERS = args.protect_profile_cylinders
    PROFILE_CYLINDER_PROTECT_ABOVE_RADIUS = args.profile_cylinder_protect_above_radius

    shape = read_step(INPUT_STEP_PATH)
    if is_null_shape(shape):
        print("\nNo valid OCC shape found; fillet detection skipped.")
        return 2

    PROFILE_CYLINDER_AXES = (
        detect_dominant_cylinder_axes(shape) if PROTECT_PROFILE_CYLINDERS else []
    )
    axis_chain_profile_faces = (
        detect_profile_cylinder_faces(shape, PROFILE_CYLINDER_AXES)
        if PROTECT_PROFILE_CYLINDERS
        else []
    )
    pure_extrusion_profile_faces = (
        detect_pure_extrusion_profile_cylinder_faces(shape, PROFILE_CYLINDER_AXES)
        if PROTECT_PROFILE_CYLINDERS
        else []
    )
    coaxial_profile_faces = (
        detect_coaxial_profile_cylinder_faces(shape)
        if PROTECT_PROFILE_CYLINDERS
        else []
    )
    PROFILE_CYLINDER_FACES = merge_unique_faces(
        axis_chain_profile_faces,
        pure_extrusion_profile_faces,
        coaxial_profile_faces,
    )
    if PROFILE_CYLINDER_AXES:
        print("\nProtected profile cylinder axes:")
        for axis in PROFILE_CYLINDER_AXES:
            print(
                "  axis {} above radius {:.6g}".format(
                    vector_label(axis),
                    PROFILE_CYLINDER_PROTECT_ABOVE_RADIUS,
                )
            )
        print("Protected chained profile cylinder faces:", len(axis_chain_profile_faces))
        print("Protected pure-extrusion cylinder faces:", len(pure_extrusion_profile_faces))
        print("Protected coaxial profile cylinder faces:", len(coaxial_profile_faces))
        print("Protected profile cylinder faces total:", len(PROFILE_CYLINDER_FACES))
    elif PROTECT_PROFILE_CYLINDERS:
        print("\nProtected profile cylinder axes: none")
        print("Protected pure-extrusion cylinder faces:", len(pure_extrusion_profile_faces))
        print("Protected coaxial profile cylinder faces:", len(coaxial_profile_faces))
        print("Protected profile cylinder faces total:", len(PROFILE_CYLINDER_FACES))
    else:
        print("\nProtected profile cylinder axes: disabled")

    collect_target_faces(shape)

    written_faces = write_step_with_target_face_names(
        output_filename=OUTPUT_STEP_PATH,
        shape=shape,
        target_name=TARGET_NAME,
    )

    if not args.no_display:
        display_target_faces(
            shape=shape,
            target_faces=written_faces,
        )

    return 0 if written_faces else 2


if __name__ == "__main__":
    sys.exit(main())
