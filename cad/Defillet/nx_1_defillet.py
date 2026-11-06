# -*- coding: utf-8 -*-
r"""NX command-line journal for the reversible defillet workflow.

Phase 1:
Open STEP -> delete faces whose imported NX face name matches a target value
using edge-connected components and a greedy grow-delete strategy -> export STEP.

Example:
    run_journal.exe cad\Defillet\nx_1_defillet.py -args --input "input.step" --output "output.step"
"""

import argparse
import hashlib
import json
import os
import re
import sys
from itertools import combinations
from time import sleep

import NXOpen
import NXOpen.Features
import NXOpen.Preferences
import NXOpen.UF

DEFAULT_NX_BASE = os.path.join(os.environ.get("ProgramFiles", "C:/Program Files"), "Siemens", "NX2406")
DEFAULT_STEP214_DEF = os.environ.get("NX_STEP214_EXPORT_DEF") or os.path.join(
    os.environ.get("UGII_BASE_DIR") or DEFAULT_NX_BASE,
    "STEP214UG",
    "ugstep214.def",
)

FILLET_SEGMENT_COLORS = [
    186, 36, 103, 141, 31, 156, 6, 211, 76, 181, 116, 46,
    201, 91, 126, 16, 171, 61, 206, 111, 151, 26, 196, 86,
]
EDGE_OVERLAP_TOLERANCE = 1.0e-8
GLOBAL_BBOX_CHANGE_RATIO = 0.03
GLOBAL_BBOX_ABSOLUTE_TOLERANCE = 1.0e-6
GEOMETRY_KEY_DECIMALS = 6
RULE_RESIDUAL_PHASE = "rule_residual"
NAMED_PHASE = "named"
LOCAL_RESIDUAL_PHASE = "local_residual"
STEP_FACE_NAME_BY_TAG = {}
LOCAL_RESIDUAL_SEARCH_FACTOR = 1.5
LOCAL_RESIDUAL_SEARCH_MIN_DISTANCE = 0.75
LOCAL_RESIDUAL_SUPPORT_FACE_FACTOR = 1.2
LOCAL_RESIDUAL_SUPPORT_FACE_MIN_DIAGONAL = 2.0
LOCAL_RESIDUAL_SUPPORT_EXPANSION_DEPTH = 2
LOCAL_RESIDUAL_MAX_PASSES = 12
# Recollect imported STEP/NX topology after every named delete.  Deleting many
# pre-collected fillet faces in one pass is fast, but on nested bend blends NX
# may heal the outer side first and leave stale inner face references that cut
# support faces incorrectly.
MAX_DELETIONS_PER_NAMED_PASS = 1
SMALL_COMPONENT_SUBSET_SEARCH_LIMIT = 5
SPHERICAL_CAP_MAX_BOUNDARY_EDGES = 2
SPHERICAL_CAP_MIN_EDGE_LENGTH = 1.0e-6

# Mirrors nx_0_detect_fillet_by_rule.py target categories:
# Cylinder, BSplineSurface, Sphere, Torus.  UF surface type is preferred because
# NX SolidFaceType can use broader display labels on imported STEP geometry.
UF_RULE_FACE_TYPES = {
    16: "Cylinder",
    18: "Sphere",
    19: "TorusOrRevolution",
    43: "BSplineSurface",
}
SOLID_FACE_RULE_TYPE_KEYWORDS = (
    "cylind",
    "sphere",
    "spheric",
    "torus",
    "toroid",
    "bspline",
    "b-spline",
    "b_spline",
    "parametric",
    "surfaceofrevolution",
    "surface_of_revolution",
    "revolution",
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="STEP file to open")
    parser.add_argument("--output", required=True, help="STEP file to export")
    parser.add_argument("--part-output", default="", help="Optional NX .prt file to save before STEP export")
    parser.add_argument("--face-name", default="1", help="Imported NX face name to delete")
    parser.add_argument("--settings-file", default=DEFAULT_STEP214_DEF, help="STEP214 .def settings file")
    parser.add_argument("--max-hole-diameter", default="5")
    parser.add_argument("--max-blend-radius", default="5")
    parser.add_argument("--radius-tolerance", type=float, default=1.0e-4, help="Radius grouping tolerance")
    parser.add_argument(
        "--max-passes",
        type=int,
        default=24,
        help="Maximum defillet passes. Use 0 for no explicit pass limit.",
    )
    parser.add_argument(
        "--residual-detect-rule",
        action="store_true",
        help="After named faces are gone, also try deleting any remaining curved faces by rule.",
    )
    parser.add_argument(
        "--local-residual-cleanup",
        dest="local_residual_cleanup",
        action="store_true",
        default=False,
        help=(
            "After named faces are gone, try deleting nearby residual curved/support faces. "
            "Disabled by default because NX can over-heal and create slots on sheet-like bends."
        ),
    )
    parser.add_argument(
        "--no-local-residual-cleanup",
        dest="local_residual_cleanup",
        action="store_false",
        help="Disable cleanup of residual curved faces near edges created by named defillet deletes.",
    )
    parser.add_argument("--keep-open", action="store_true", help="Keep the imported part open after export")
    parser.add_argument("--no-cleanup", action="store_true", help="Do not delete STEP translator log/prt temp files")
    return parser.parse_args(argv)


def safe_text(value):
    if value is None:
        return ""
    try:
        return str(value)
    except Exception as exc:
        return "<str failed: {}>".format(exc)


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def safe_call(func, default=""):
    try:
        return func()
    except Exception:
        return default


def make_logger(session):
    listing_window = session.ListingWindow
    try:
        listing_window.Open()
    except Exception:
        listing_window = None

    def log(message=""):
        text = safe_text(message)
        print(text)
        if listing_window is not None:
            try:
                listing_window.WriteLine(text)
            except Exception:
                pass

    return log


def open_step(session, input_path):
    base_part, load_status = session.Parts.OpenActiveDisplay(
        input_path,
        NXOpen.DisplayPartOption.AllowAdditional,
    )
    try:
        work_part = session.Parts.Work
        session.ApplicationSwitchImmediate("UG_APP_MODELING")
        return work_part
    finally:
        load_status.Dispose()


def close_part(session, work_part):
    work_part.Close(
        NXOpen.BasePart.CloseWholeTree.TrueValue,
        NXOpen.BasePart.CloseModified.CloseModified,
        None,
    )
    session.ApplicationSwitchImmediate("UG_APP_NOPART")


def close_all_parts(session, log=None):
    try:
        responses = session.Parts.NewPartCloseResponses()
    except Exception:
        responses = None
    try:
        session.Parts.CloseAll(NXOpen.BasePart.CloseModified.CloseModified, responses)
    except Exception as exc:
        if log:
            log("Warning: CloseAll before defillet failed: {}".format(safe_text(exc)))
    finally:
        if responses is not None:
            try:
                responses.Dispose()
            except Exception:
                pass
    try:
        session.ApplicationSwitchImmediate("UG_APP_NOPART")
    except Exception:
        pass


def cleanup_temp_files(input_path, output_path):
    candidates = [
        os.path.splitext(input_path)[0] + ".log",
        os.path.splitext(input_path)[0] + "_step.prt",
        os.path.splitext(output_path)[0] + ".log",
        os.path.splitext(output_path)[0] + "_step.prt",
    ]

    for path in candidates:
        if os.path.exists(path):
            try:
                os.remove(path)
                print("Deleted:", path)
            except Exception as exc:
                print("Cannot delete:", path, exc)


def cleanup_step_import_sidecars(input_path, log=None):
    directory = os.path.dirname(os.path.abspath(input_path))
    input_stem = os.path.splitext(os.path.basename(input_path))[0].lower()
    deleted_count = 0

    try:
        filenames = os.listdir(directory)
    except Exception as exc:
        if log:
            log("Warning: cannot list STEP import sidecars: {}".format(safe_text(exc)))
        return 0

    for filename in filenames:
        lower_name = filename.lower()
        is_translator_part = (
            lower_name.startswith("open cascade step translator")
            and lower_name.endswith(".prt")
        )
        is_input_step_sidecar = (
            lower_name == input_stem + ".log"
            or lower_name == input_stem + "_stp.log"
            or lower_name == input_stem + "_step.prt"
            or lower_name == input_stem + "_stp.prt"
            or (
                lower_name.startswith(input_stem + "_step_")
                and lower_name.endswith(".prt")
            )
            or (
                lower_name.startswith(input_stem + "_stp_")
                and lower_name.endswith(".prt")
            )
        )
        if not (is_translator_part or is_input_step_sidecar):
            continue

        path = os.path.join(directory, filename)
        try:
            os.remove(path)
            deleted_count += 1
        except Exception as exc:
            if log:
                log("Warning: cannot delete STEP import sidecar {}: {}".format(path, safe_text(exc)))

    if log and deleted_count:
        log("Deleted stale STEP import sidecars: {}".format(deleted_count))
    return deleted_count


def safe_face_journal_identifier(face):
    try:
        return safe_text(face.JournalIdentifier)
    except Exception:
        return ""


def safe_face_name(face):
    tag = safe_tag(face)
    if tag in STEP_FACE_NAME_BY_TAG:
        return STEP_FACE_NAME_BY_TAG[tag]
    try:
        return safe_text(face.Name)
    except Exception:
        return ""


def safe_tag(obj):
    try:
        return int(obj.Tag)
    except Exception:
        return safe_text(safe_call(lambda: obj.Tag))


def safe_body_journal_identifier(body):
    try:
        return safe_text(body.JournalIdentifier)
    except Exception:
        return ""


def safe_body_name(body):
    try:
        return safe_text(body.Name)
    except Exception:
        return ""


def part_display_name(part):
    if part is None:
        return "<none>"
    for getter in (
            lambda: part.FullPath,
            lambda: part.Leaf,
            lambda: part.Name,
            lambda: part.JournalIdentifier,
    ):
        text = safe_text(safe_call(getter, ""))
        if text:
            return text
    return "<part {}>".format(safe_tag(part))


def component_display_name(component):
    if component is None:
        return "<root>"
    for getter in (
            lambda: component.DisplayName,
            lambda: component.Name,
            lambda: component.JournalIdentifier,
    ):
        text = safe_text(safe_call(getter, ""))
        if text:
            return text
    return "<component {}>".format(safe_tag(component))


def part_body_count(part):
    try:
        return len(list(part.Bodies))
    except Exception:
        return 0


def iter_component_tree(component):
    if component is None:
        return
    for child in list(safe_call(lambda: component.GetChildren(), []) or []):
        yield child
        for grandchild in iter_component_tree(child):
            yield grandchild


def collect_defillet_contexts(display_part):
    contexts = []
    seen_parts = set()

    def add_context(part, component=None):
        if part is None or part_body_count(part) <= 0:
            return
        key = safe_tag(part)
        if key in seen_parts:
            return
        seen_parts.add(key)
        contexts.append(
            {
                "part": part,
                "component": component,
                "label": component_display_name(component),
                "body_count": part_body_count(part),
            }
        )

    add_context(display_part, component=None)

    root_component = safe_call(lambda: display_part.ComponentAssembly.RootComponent, None)
    for component in iter_component_tree(root_component):
        prototype = safe_call(lambda: component.Prototype, None)
        add_context(prototype, component=component)

    return contexts


def parse_step_advanced_face_names(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            text = fp.read()
    except Exception:
        return []

    names = []
    pattern = re.compile(
        r"#\s*\d+\s*=\s*ADVANCED_FACE\s*\(\s*'((?:[^']|'')*)'",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        names.append(match.group(1).replace("''", "'"))
    return names


def assign_step_face_names_to_nx_faces(contexts, input_path, target_name, log=None):
    global STEP_FACE_NAME_BY_TAG
    STEP_FACE_NAME_BY_TAG = {}

    step_face_names = parse_step_advanced_face_names(input_path)
    if not step_face_names:
        if log:
            log("No ADVANCED_FACE names found in STEP text fallback.")
        return 0, 0, 0

    face_index = 0
    assigned_count = 0
    target_count = 0

    for context in contexts:
        part = context.get("part")
        for body in list(safe_call(lambda: part.Bodies, []) or []):
            for face in list(safe_call(lambda: body.GetFaces(), []) or []):
                if face_index >= len(step_face_names):
                    break
                step_name = step_face_names[face_index]
                face_index += 1
                if not step_name:
                    continue
                STEP_FACE_NAME_BY_TAG[safe_tag(face)] = step_name
                assigned_count += 1
                if parse_defillet_face_label(step_name, target_name) is not None:
                    target_count += 1

    if log:
        log(
            "STEP face-name fallback: step_faces={}, nx_faces_seen={}, assigned={}, target_labels={}".format(
                len(step_face_names),
                face_index,
                assigned_count,
                target_count,
            )
        )

    return face_index, assigned_count, target_count


def dispose_status(status):
    if status is None:
        return
    try:
        status.Dispose()
    except Exception:
        pass


def activate_defillet_context(session, context):
    component = context.get("component")
    if component is not None:
        status = safe_call(lambda: session.Parts.SetWorkComponent(component), None)
        dispose_status(status)
        return session.Parts.Work

    part = context.get("part")
    if part is not None:
        session.Parts.SetWork(part)
        return session.Parts.Work

    return None


def face_type_name(face):
    value = safe_call(lambda: face.SolidFaceType, None)
    text = safe_text(value)
    if "." in text:
        return text.rsplit(".", 1)[-1]
    return text


def blend_data(face):
    try:
        radius, is_blend = face.GetBlendData()
        return float(radius), bool(is_blend), ""
    except Exception as exc:
        return None, False, safe_text(exc)


def uf_face_type(face):
    try:
        uf_session = NXOpen.UF.UFSession.GetUFSession()
        data = uf_session.Modl.AskFaceData(face.Tag)
        if data:
            return int(data[0]), ""
    except Exception as exc:
        return None, safe_text(exc)
    return None, ""


def is_detect_rule_face_type(face_info):
    if face_info.get("is_blend"):
        return True

    uf_type = face_info.get("uf_face_type")
    if uf_type in UF_RULE_FACE_TYPES:
        return True

    # Fallback for NX environments where UF AskFaceData is unavailable.
    text = safe_text(face_info.get("face_type")).replace(" ", "").lower()
    return any(keyword in text for keyword in SOLID_FACE_RULE_TYPE_KEYWORDS)


def detect_rule_face_type_label(face_info):
    if face_info.get("is_blend"):
        radius = face_info.get("blend_radius")
        if radius is not None:
            return "NXBlend(radius={:.9g})".format(radius)
        return "NXBlend"

    uf_type = face_info.get("uf_face_type")
    if uf_type in UF_RULE_FACE_TYPES:
        return "{}(UF={})".format(UF_RULE_FACE_TYPES[uf_type], uf_type)
    return safe_text(face_info.get("face_type"))


def is_sphere_face_info(face_info):
    if face_info.get("uf_face_type") == 18:
        return True
    text = safe_text(face_info.get("face_type")).replace(" ", "").lower()
    return "sphere" in text or "spheric" in text


def is_protected_spherical_cap_face_info(face_info):
    if not is_sphere_face_info(face_info):
        return False

    meaningful_edges = []
    circular_edges = 0
    for edge in list(safe_call(lambda: face_info["face"].GetEdges(), []) or []):
        length = safe_edge_curve_length(edge)
        if length <= SPHERICAL_CAP_MIN_EDGE_LENGTH:
            continue
        meaningful_edges.append(edge)
        if is_circular_edge_type(edge_type_name(edge)):
            circular_edges += 1

    if circular_edges <= 0:
        return False

    # Hemisphere/dome caps are normally bounded by one circular loop.  STEP/NX
    # imports may split that loop into two arcs; local spherical corner blends
    # usually have three or more real boundary edges.
    return len(meaningful_edges) <= SPHERICAL_CAP_MAX_BOUNDARY_EDGES


def face_ref(face_info):
    if face_info.get("journal_identifier"):
        return face_info["journal_identifier"]
    return "body {body_index} face {face_index}".format(**face_info)


def face_skip_key(face_info):
    stable_key = face_info.get("face_stable_key")
    if stable_key is not None:
        return stable_key
    if face_info["journal_identifier"]:
        return face_info["journal_identifier"]
    return "{}:{}".format(face_info["body_index"], face_info["face_index"])


def component_skip_key(component):
    return tuple(sorted(face_stable_sort_key(face_info) for face_info in component["faces"]))


def collect_faces_by_name(work_part, target_name):
    result = []
    for face_info in collect_all_face_infos(work_part):
        label = parse_defillet_face_label(face_info["name"], target_name)
        if label is None:
            continue
        item = dict(face_info)
        item["defillet_component_id"] = label.get("defillet_component_id")
        item["defillet_convexity_sign"] = safe_int(label.get("defillet_convexity_sign"), 0)
        item["defillet_source"] = NAMED_PHASE
        result.append(item)
    result.sort(key=face_stable_sort_key)
    return result


def collect_faces_by_detect_rule(work_part):
    result = []
    for face_info in collect_all_face_infos(work_part):
        if not is_detect_rule_face_type(face_info):
            continue
        if is_protected_spherical_cap_face_info(face_info):
            continue
        item = dict(face_info)
        item["defillet_component_id"] = None
        item["defillet_source"] = RULE_RESIDUAL_PHASE
        item["detect_rule_type"] = detect_rule_face_type_label(face_info)
        result.append(item)
    result.sort(key=face_stable_sort_key)
    return result


def collect_anchor_points_from_deletion_history(deletion_history):
    points = []
    seen = set()
    for entry in deletion_history or []:
        for group_key in ("all_new_edge_groups", "restore_edge_groups"):
            for group in entry.get(group_key, []) or []:
                for edge_payload in group.get("edges", []) or []:
                    for point_key in ("midpoint", "vertex_a", "vertex_b"):
                        point = deserialize_point(edge_payload.get(point_key))
                        if point is None:
                            continue
                        rounded = tuple(round(value, 6) for value in point)
                        if rounded in seen:
                            continue
                        seen.add(rounded)
                        points.append(point)
    return points


def face_edge_points(face):
    points = []
    for edge in list(safe_call(lambda: face.GetEdges(), []) or []):
        point_a, point_b = edge_endpoint_tuples(edge)
        points.append(point_a)
        points.append(point_b)
        points.append(edge_midpoint(edge))
    return [point for point in points if point is not None]


def min_face_distance_squared_to_points(face, anchor_points):
    face_points = face_edge_points(face)
    if not face_points or not anchor_points:
        return None

    best = None
    for face_point in face_points:
        for anchor_point in anchor_points:
            distance = distance_squared(face_point, anchor_point)
            if best is None or distance < best:
                best = distance
    return best


def face_bbox_diagonal(face):
    bbox = bbox_from_points(face_edge_points(face))
    if bbox is None:
        return None
    return vector_length(tuple(bbox["length"]))


def is_small_local_support_face(face, max_blend_radius):
    diagonal = face_bbox_diagonal(face)
    if diagonal is None:
        return False
    limit = max(
        LOCAL_RESIDUAL_SUPPORT_FACE_MIN_DIAGONAL,
        float(max_blend_radius or 0.0) * LOCAL_RESIDUAL_SUPPORT_FACE_FACTOR,
    )
    return diagonal <= limit


def is_local_residual_seed_face(face_info, max_blend_radius):
    blend_radius = face_info.get("blend_radius")
    if face_info.get("is_blend") and blend_radius is not None:
        return float(blend_radius) <= float(max_blend_radius) + 1.0e-4

    # Imported STEP residuals are sometimes no longer reported as NX blends.
    # Allow small curved scraps as seeds, but keep large cylinders/surfaces out.
    return is_small_local_support_face(face_info["face"], max_blend_radius)


def local_face_near_anchor(face, anchor_points, max_distance_squared):
    distance_squared_to_anchor = min_face_distance_squared_to_points(face, anchor_points)
    if distance_squared_to_anchor is None or distance_squared_to_anchor > max_distance_squared:
        return None
    return distance_squared_to_anchor


def collect_faces_by_local_detect_rule(
        work_part,
        anchor_points,
        max_distance,
        target_name,
        max_blend_radius=None,
):
    if not anchor_points:
        return []

    max_distance_squared = max_distance * max_distance
    result_by_tag = {}
    all_face_infos = collect_all_face_infos(work_part)
    face_info_by_tag = {face_info["tag"]: face_info for face_info in all_face_infos}
    seed_faces = []

    for face_info in all_face_infos:
        if not is_detect_rule_face_type(face_info):
            continue
        if max_blend_radius is not None and not is_local_residual_seed_face(face_info, max_blend_radius):
            continue

        distance_squared_to_anchor = local_face_near_anchor(
            face_info["face"],
            anchor_points,
            max_distance_squared,
        )
        if distance_squared_to_anchor is None:
            continue

        item = dict(face_info)
        item["defillet_component_id"] = None
        item["defillet_source"] = LOCAL_RESIDUAL_PHASE
        item["detect_rule_type"] = detect_rule_face_type_label(face_info)
        item["local_residual_distance"] = distance_squared_to_anchor ** 0.5
        result_by_tag[item["tag"]] = item
        seed_faces.append(item)

    frontier = [(seed_info, 0) for seed_info in seed_faces]
    frontier_index = 0
    while frontier_index < len(frontier):
        source_info, depth = frontier[frontier_index]
        frontier_index += 1
        if depth >= LOCAL_RESIDUAL_SUPPORT_EXPANSION_DEPTH:
            continue

        for edge in list(safe_call(lambda: source_info["face"].GetEdges(), []) or []):
            for neighbor_face in edge_faces(edge):
                neighbor_tag = safe_tag(neighbor_face)
                if neighbor_tag in result_by_tag:
                    continue
                neighbor_info = face_info_by_tag.get(neighbor_tag)
                if neighbor_info is None:
                    continue
                if not is_small_local_support_face(neighbor_info["face"], max_blend_radius):
                    continue
                distance_squared_to_anchor = local_face_near_anchor(
                    neighbor_info["face"],
                    anchor_points,
                    max_distance_squared,
                )
                if distance_squared_to_anchor is None:
                    continue
                item = dict(neighbor_info)
                item["defillet_component_id"] = None
                item["defillet_source"] = LOCAL_RESIDUAL_PHASE
                item["detect_rule_type"] = "LocalSupport"
                item["local_residual_distance"] = distance_squared_to_anchor ** 0.5
                result_by_tag[item["tag"]] = item
                frontier.append((item, depth + 1))

    result = list(result_by_tag.values())
    result.sort(key=face_stable_sort_key)
    return result


def parse_defillet_face_label(face_name, target_name):
    text = safe_text(face_name).strip()
    target = safe_text(target_name)

    if text == target:
        return {
            "defillet_target": target,
            "defillet_component_id": None,
            "defillet_convexity_sign": 0,
        }

    try:
        data = json.loads(text)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    if safe_text(data.get("defillet_target")) != target:
        return None

    return {
        "defillet_target": target,
        "defillet_component_id": data.get("defillet_component_id"),
        "defillet_convexity_sign": data.get("defillet_convexity_sign", 0),
    }


def collect_all_face_infos(work_part):
    faces = []

    for body_index, body in enumerate(work_part.Bodies, start=1):
        body_name = safe_body_name(body)
        body_jid = safe_body_journal_identifier(body)
        body_key = body_geometry_key(body)
        for face_index, face in enumerate(body.GetFaces(), start=1):
            radius, is_blend, blend_error = blend_data(face)
            uf_type, uf_error = uf_face_type(face)
            face_points = face_edge_points(face)
            face_bbox = bbox_from_points(face_points)
            faces.append(
                {
                    "face": face,
                    "tag": safe_tag(face),
                    "name": safe_face_name(face),
                    "body_index": body_index,
                    "body_name": body_name,
                    "body_journal_identifier": body_jid,
                    "body_geometry_key": body_key,
                    "face_index": face_index,
                    "journal_identifier": safe_face_journal_identifier(face),
                    "face_type": face_type_name(face),
                    "uf_face_type": uf_type,
                    "uf_face_type_name": UF_RULE_FACE_TYPES.get(uf_type, ""),
                    "uf_face_type_error": uf_error,
                    "blend_radius": radius,
                    "is_blend": is_blend,
                    "blend_error": blend_error,
                    "face_bbox": face_bbox,
                    "face_centroid": point_centroid(face_points),
                    "face_geometry_key": face_geometry_key(face),
                }
            )

    return faces


def edge_faces(edge):
    try:
        return list(edge.GetUnsortedFaces())
    except Exception:
        return list(edge.GetFaces())


def point_tuple(point):
    try:
        return (float(point.X), float(point.Y), float(point.Z))
    except Exception:
        return None


def serialize_point(point):
    values = point_tuple(point)
    if values is None:
        return None
    return {"x": values[0], "y": values[1], "z": values[2]}


def serialize_point_from_tuple(values):
    if values is None:
        return None
    return {"x": values[0], "y": values[1], "z": values[2]}


def deserialize_point(payload):
    if not isinstance(payload, dict):
        return None
    try:
        return (
            float(payload["x"]),
            float(payload["y"]),
            float(payload["z"]),
        )
    except Exception:
        return None


def distance_squared(point_a, point_b):
    return (
            (point_a[0] - point_b[0]) * (point_a[0] - point_b[0])
            + (point_a[1] - point_b[1]) * (point_a[1] - point_b[1])
            + (point_a[2] - point_b[2]) * (point_a[2] - point_b[2])
    )


def vector_between_points(point_a, point_b):
    if point_a is None or point_b is None:
        return None
    return (
        point_b[0] - point_a[0],
        point_b[1] - point_a[1],
        point_b[2] - point_a[2],
    )


def vector_length(vector):
    if vector is None:
        return 0.0
    return (
            vector[0] * vector[0]
            + vector[1] * vector[1]
            + vector[2] * vector[2]
    ) ** 0.5


def normalize_vector(vector):
    length = vector_length(vector)
    if length <= 1.0e-12:
        return None
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def dot_product(vector_a, vector_b):
    return (
            vector_a[0] * vector_b[0]
            + vector_a[1] * vector_b[1]
            + vector_a[2] * vector_b[2]
    )


def cross_product(vector_a, vector_b):
    return (
        vector_a[1] * vector_b[2] - vector_a[2] * vector_b[1],
        vector_a[2] * vector_b[0] - vector_a[0] * vector_b[2],
        vector_a[0] * vector_b[1] - vector_a[1] * vector_b[0],
    )


def edge_vertices(edge):
    try:
        vertex_a, vertex_b = edge.GetVertices()
        return vertex_a, vertex_b
    except Exception:
        return None, None


def edge_midpoint(edge):
    vertex_a, vertex_b = edge_vertices(edge)
    point_a = point_tuple(vertex_a)
    point_b = point_tuple(vertex_b)
    if point_a is None or point_b is None:
        return None
    return (
        (point_a[0] + point_b[0]) * 0.5,
        (point_a[1] + point_b[1]) * 0.5,
        (point_a[2] + point_b[2]) * 0.5,
    )


def edge_endpoint_tuples(edge):
    vertex_a, vertex_b = edge_vertices(edge)
    point_a = point_tuple(vertex_a)
    point_b = point_tuple(vertex_b)
    if point_a is None or point_b is None:
        return None, None
    if distance_squared(point_a, point_b) <= EDGE_OVERLAP_TOLERANCE * EDGE_OVERLAP_TOLERANCE:
        return None, None
    return point_a, point_b


def edge_type_name(edge):
    value = safe_call(lambda: edge.SolidEdgeType, None)
    text = safe_text(value)
    if "." in text:
        return text.rsplit(".", 1)[-1]
    return text


def is_linear_edge_type(edge_type):
    text = safe_text(edge_type).lower()
    return text in ("linear", "1")


def is_circular_edge_type(edge_type):
    text = safe_text(edge_type).lower()
    return text in ("circular", "elliptical", "2", "3")


def safe_edge_curve_length(edge):
    for getter in (
            lambda: edge.GetLength(),
            lambda: edge.Length,
    ):
        try:
            value = getter()
            if value is not None:
                return float(value)
        except Exception:
            pass

    point_a, point_b = edge_endpoint_tuples(edge)
    if point_a is None or point_b is None:
        return 0.0
    return vector_length(vector_between_points(point_a, point_b))


def points_close(point_a, point_b, tolerance):
    if point_a is None or point_b is None:
        return False
    return distance_squared(point_a, point_b) <= tolerance * tolerance


def unordered_endpoints_close(point_a, point_b, other_point_a, other_point_b, tolerance):
    return (
            points_close(point_a, other_point_a, tolerance)
            and points_close(point_b, other_point_b, tolerance)
    ) or (
            points_close(point_a, other_point_b, tolerance)
            and points_close(point_b, other_point_a, tolerance)
    )


def edge_identity(edge):
    edge_jid = safe_text(safe_call(lambda: edge.JournalIdentifier))
    if edge_jid:
        return edge_jid
    vertex_a, vertex_b = edge_vertices(edge)
    point_a = point_tuple(vertex_a)
    point_b = point_tuple(vertex_b)
    if point_a is not None and point_b is not None:
        edge_type = edge_type_name(edge)
        edge_length = safe_edge_curve_length(edge)
        rounded = sorted(
            [
                tuple(round(value, 6) for value in point_a),
                tuple(round(value, 6) for value in point_b),
            ]
        )
        return "edge:{}:{:.6f}:{}:{}".format(edge_type, edge_length, rounded[0], rounded[1])
    return safe_text(safe_tag(edge))


def edge_geometry_signature(edge):
    vertex_a, vertex_b = edge_vertices(edge)
    point_a = point_tuple(vertex_a)
    point_b = point_tuple(vertex_b)
    if point_a is None or point_b is None:
        return ""
    edge_type = edge_type_name(edge)
    edge_length = safe_edge_curve_length(edge)
    rounded = sorted(
        [
            tuple(round(value, 6) for value in point_a),
            tuple(round(value, 6) for value in point_b),
        ]
    )
    return "{}:{:.6f}:{}:{}".format(edge_type, edge_length, rounded[0], rounded[1])


def rounded_geometry_value(value):
    try:
        value = round(float(value), GEOMETRY_KEY_DECIMALS)
    except Exception:
        return None
    if value == 0:
        return 0.0
    return value


def rounded_geometry_point(point):
    if point is None:
        return None
    return tuple(rounded_geometry_value(value) for value in point)


def rounded_bbox_key(bbox):
    if bbox is None:
        return ()
    return (
        tuple(rounded_geometry_value(value) for value in bbox["min"]),
        tuple(rounded_geometry_value(value) for value in bbox["max"]),
        tuple(rounded_geometry_value(value) for value in bbox["length"]),
    )


def point_cloud_geometry_key(points):
    clean_points = [point for point in points if point is not None]
    if not clean_points:
        return ()

    rounded_points = sorted(
        set(
            point
            for point in (rounded_geometry_point(point) for point in clean_points)
            if point is not None
        )
    )
    centroid = tuple(
        rounded_geometry_value(
            sum(point[axis] for point in clean_points) / float(len(clean_points))
        )
        for axis in range(3)
    )
    return (
        rounded_bbox_key(bbox_from_points(clean_points)),
        centroid,
        tuple(rounded_points),
    )


def edge_geometry_signatures(edges):
    signatures = []
    for edge in edges:
        signatures.append(edge_geometry_signature(edge))
    return tuple(sorted(signatures))


def body_geometry_key(body):
    edges = list(safe_call(lambda: body.GetEdges(), []))
    points = []
    for edge in edges:
        point_a, point_b = edge_endpoint_tuples(edge)
        points.append(point_a)
        points.append(point_b)
        points.append(edge_midpoint(edge))

    return (
        point_cloud_geometry_key(points),
        len(edges),
        edge_geometry_signatures(edges),
    )


def face_geometry_key(face):
    edges = list(safe_call(lambda: face.GetEdges(), []))
    points = []
    for edge in edges:
        point_a, point_b = edge_endpoint_tuples(edge)
        points.append(point_a)
        points.append(point_b)
        points.append(edge_midpoint(edge))

    return (
        point_cloud_geometry_key(points),
        len(edges),
        edge_geometry_signatures(edges),
    )


def radius_sort_key(radius):
    if radius is None:
        return (0, 0.0)
    rounded = rounded_geometry_value(radius)
    if rounded is None:
        return (0, 0.0)
    return (1, rounded)


def face_stable_sort_key(face_info):
    return (
        face_info.get("body_geometry_key") or (),
        face_info.get("face_geometry_key") or (),
        safe_text(face_info.get("face_type")),
        radius_sort_key(face_info.get("blend_radius")),
        safe_text(face_info.get("defillet_component_id")),
        safe_text(face_info.get("journal_identifier")),
        int(face_info.get("body_index") or 0),
        int(face_info.get("face_index") or 0),
    )


def component_stable_sort_key(component):
    face_keys = tuple(sorted(face_stable_sort_key(face_info) for face_info in component["faces"]))
    if not face_keys:
        return ((), 0, ())
    return (face_keys[0], len(face_keys), face_keys)


def normalized_boundary_score(face_bbox, global_bbox):
    if face_bbox is None or global_bbox is None:
        return 0.0

    score = 0.0
    for axis in range(3):
        global_length = abs(float(global_bbox["length"][axis]))
        if global_length <= 1.0e-9:
            continue
        min_gap = abs(float(face_bbox["min"][axis]) - float(global_bbox["min"][axis]))
        max_gap = abs(float(global_bbox["max"][axis]) - float(face_bbox["max"][axis]))
        nearest_gap = min(min_gap, max_gap)
        axis_score = max(0.0, 1.0 - nearest_gap / (global_length * 0.25))
        score = max(score, axis_score)
    return score


def normalized_radial_score(face_info, global_bbox):
    if global_bbox is None:
        return 0.0
    centroid = face_info.get("face_centroid") or bbox_center(face_info.get("face_bbox"))
    center = bbox_center(global_bbox)
    diagonal = bbox_diagonal_length(global_bbox)
    if centroid is None or center is None or diagonal <= 1.0e-9:
        return 0.0
    return min(1.0, vector_length(vector_between_points(center, centroid)) / diagonal)


def component_max_radius(component):
    radii = []
    for face_info in component.get("faces", []) or []:
        radius = face_info.get("blend_radius")
        if radius is None:
            continue
        try:
            radii.append(abs(float(radius)))
        except Exception:
            continue
    return max(radii) if radii else 0.0


def component_convexity_priority(component):
    signs = [
        safe_int(face_info.get("defillet_convexity_sign"), 0)
        for face_info in component.get("faces", []) or []
    ]
    if any(sign > 0 for sign in signs):
        return 0
    if any(sign == 0 for sign in signs):
        return 1
    return 2


def component_metadata_summary(component):
    ids = sorted(
        set(component_named_ids(component))
    )
    signs = sorted(
        set(
            safe_int(face_info.get("defillet_convexity_sign"), 0)
            for face_info in component.get("faces", []) or []
        )
    )
    return "ids=[{}], signs=[{}]".format(
        ",".join(ids) if ids else "-",
        ",".join(str(sign) for sign in signs) if signs else "-",
    )


def component_named_ids(component):
    ids = []
    for face_info in component.get("faces", []) or []:
        component_id = face_info.get("defillet_component_id")
        if component_id is None:
            continue
        text = safe_text(component_id)
        if text:
            ids.append(text)
    return sorted(set(ids))


def filter_already_deleted_named_components(components, deleted_component_ids):
    if not deleted_component_ids:
        return components
    filtered = []
    for component in components:
        ids = component_named_ids(component)
        if ids and all(component_id in deleted_component_ids for component_id in ids):
            continue
        filtered.append(component)
    return filtered


def component_outer_delete_score(component, global_bbox):
    boundary_score = 0.0
    radial_score = 0.0
    for face_info in component.get("faces", []) or []:
        boundary_score = max(
            boundary_score,
            normalized_boundary_score(face_info.get("face_bbox"), global_bbox),
        )
        radial_score = max(radial_score, normalized_radial_score(face_info, global_bbox))
    return {
        "boundary": boundary_score,
        "radial": radial_score,
        "radius": component_max_radius(component),
    }


def component_delete_order_key(component, global_bbox):
    score = component_outer_delete_score(component, global_bbox)
    return (
        component_convexity_priority(component),
        -score["boundary"],
        -score["radial"],
        -score["radius"],
        component_stable_sort_key(component),
    )


def order_components_for_defillet_delete(components, phase, global_bbox):
    if phase != NAMED_PHASE or not components or global_bbox is None:
        return components
    return sorted(
        components,
        key=lambda component: component_delete_order_key(component, global_bbox),
    )


def stable_key_digest(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=safe_text)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def component_history_key(component):
    return [
        stable_key_digest(face_stable_sort_key(face_info))
        for face_info in sorted(component["faces"], key=face_stable_sort_key)
    ]


def snapshot_edge_records(work_part):
    records = {}
    for body in work_part.Bodies:
        for edge in body.GetEdges():
            vertex_a, vertex_b = edge_vertices(edge)
            point_a = point_tuple(vertex_a)
            point_b = point_tuple(vertex_b)
            key = edge_identity(edge)
            records[key] = {
                "edge": edge,
                "edge_type": edge_type_name(edge),
                "geometry_signature": edge_geometry_signature(edge),
                "point_a": point_a,
                "point_b": point_b,
                "direction": normalize_vector(vector_between_points(point_a, point_b)),
                "length": safe_edge_curve_length(edge),
                "chord_length": vector_length(vector_between_points(point_a, point_b)),
            }
    return records


def bbox_from_points(points):
    clean_points = [point for point in points if point is not None]
    if not clean_points:
        return None

    min_values = [
        min(point[axis] for point in clean_points)
        for axis in range(3)
    ]
    max_values = [
        max(point[axis] for point in clean_points)
        for axis in range(3)
    ]
    lengths = [
        max_values[axis] - min_values[axis]
        for axis in range(3)
    ]
    return {
        "min": min_values,
        "max": max_values,
        "length": lengths,
    }


def point_centroid(points):
    clean_points = [point for point in points if point is not None]
    if not clean_points:
        return None
    return tuple(
        sum(point[axis] for point in clean_points) / float(len(clean_points))
        for axis in range(3)
    )


def bbox_center(bbox):
    if bbox is None:
        return None
    return tuple(
        (float(bbox["min"][axis]) + float(bbox["max"][axis])) * 0.5
        for axis in range(3)
    )


def bbox_diagonal_length(bbox):
    if bbox is None:
        return 0.0
    return vector_length(tuple(float(value) for value in bbox["length"]))


def global_bbox_from_measurement(work_part):
    bodies = list(work_part.Bodies)
    if not bodies:
        return None

    anchor = NXOpen.Point3d(0.0, 0.0, 0.0)
    box_points, _directions, _edge_lengths, point_origin, point_extreme, _volume = (
        NXOpen.Measurement.GetBoundingBoxProperties(bodies, 0, anchor, False)
    )

    points = []
    for point in list(box_points or []):
        points.append(point_tuple(point))
    points.append(point_tuple(point_origin))
    points.append(point_tuple(point_extreme))
    return bbox_from_points(points)


def global_bbox_from_edge_vertices(work_part):
    points = []
    for body in work_part.Bodies:
        for edge in body.GetEdges():
            vertex_a, vertex_b = edge_vertices(edge)
            points.append(point_tuple(vertex_a))
            points.append(point_tuple(vertex_b))
    return bbox_from_points(points)


def collect_global_bbox(work_part):
    try:
        bbox = global_bbox_from_measurement(work_part)
        if bbox is not None:
            return bbox
    except Exception:
        pass

    try:
        return global_bbox_from_edge_vertices(work_part)
    except Exception:
        return None


def format_bbox_values(values):
    return "({:.6g}, {:.6g}, {:.6g})".format(values[0], values[1], values[2])


def bbox_axis_change_percent(before_bbox, after_bbox, axis):
    denominator = max(
        abs(float(before_bbox["length"][axis])),
        GLOBAL_BBOX_ABSOLUTE_TOLERANCE,
    )
    deltas = [
        abs(after_bbox["min"][axis] - before_bbox["min"][axis]),
        abs(after_bbox["max"][axis] - before_bbox["max"][axis]),
        abs(after_bbox["length"][axis] - before_bbox["length"][axis]),
    ]
    return max(deltas) / denominator * 100.0


def bbox_change_percent_summary(before_bbox, after_bbox):
    if before_bbox is None or after_bbox is None:
        return None

    axis_values = [
        bbox_axis_change_percent(before_bbox, after_bbox, axis)
        for axis in range(3)
    ]
    return {
        "x": axis_values[0],
        "y": axis_values[1],
        "z": axis_values[2],
        "max": max(axis_values),
    }


def format_bbox_change_feature_name(summary):
    if not summary:
        return "DEFILLET bbox unknown"
    return (
        "DEFILLET bbox max={max:.3f}% X={x:.3f}% Y={y:.3f}% Z={z:.3f}%"
    ).format(**summary)


def set_feature_name(feature, name):
    if feature is None:
        return False

    try:
        feature.SetName(name)
        return True
    except Exception:
        pass

    try:
        feature.Name = name
        return True
    except Exception:
        return False


def iter_committed_features(committed):
    if committed is None:
        return []
    if isinstance(committed, (list, tuple)):
        return [item for item in committed if item is not None]
    return [committed]


def validate_global_bbox_change(before_bbox, after_bbox):
    if before_bbox is None:
        return False, "global bbox guard failed: cannot capture bbox before delete"
    if after_bbox is None:
        return False, "global bbox guard failed: cannot capture bbox after delete"

    axis_names = ["X", "Y", "Z"]
    failures = []

    for axis in range(3):
        old_length = abs(before_bbox["length"][axis])
        allowed = max(
            old_length * GLOBAL_BBOX_CHANGE_RATIO,
            GLOBAL_BBOX_ABSOLUTE_TOLERANCE,
        )
        min_delta = abs(after_bbox["min"][axis] - before_bbox["min"][axis])
        max_delta = abs(after_bbox["max"][axis] - before_bbox["max"][axis])
        length_delta = abs(after_bbox["length"][axis] - before_bbox["length"][axis])

        if min_delta > allowed or max_delta > allowed or length_delta > allowed:
            failures.append(
                "{}: allowed={:.6g}, min_delta={:.6g}, max_delta={:.6g}, length_delta={:.6g}".format(
                    axis_names[axis],
                    allowed,
                    min_delta,
                    max_delta,
                    length_delta,
                )
            )

    if not failures:
        return True, ""

    return False, (
        "global bbox guard rejected delete: {}; before_min={}, before_max={}, "
        "after_min={}, after_max={}, threshold={:.0%}"
    ).format(
        "; ".join(failures),
        format_bbox_values(before_bbox["min"]),
        format_bbox_values(before_bbox["max"]),
        format_bbox_values(after_bbox["min"]),
        format_bbox_values(after_bbox["max"]),
        GLOBAL_BBOX_CHANGE_RATIO,
    )


def point_line_distance(point, line_point, line_direction):
    vector = vector_between_points(line_point, point)
    if vector is None or line_direction is None:
        return None
    return vector_length(cross_product(vector, line_direction))


def projected_interval(point_a, point_b, origin, direction):
    if point_a is None or point_b is None or origin is None or direction is None:
        return None
    scalar_a = dot_product(vector_between_points(origin, point_a), direction)
    scalar_b = dot_product(vector_between_points(origin, point_b), direction)
    return min(scalar_a, scalar_b), max(scalar_a, scalar_b)


def intervals_overlap_length(interval_a, interval_b):
    if interval_a is None or interval_b is None:
        return 0.0
    return max(0.0, min(interval_a[1], interval_b[1]) - max(interval_a[0], interval_b[0]))


def edge_record_overlaps_candidate(edge_record, candidate_edge, distance_tolerance=1.0e-4, min_overlap_ratio=0.15):
    candidate_type = edge_type_name(candidate_edge)
    old_type = edge_record.get("edge_type", "")

    # A circular/arc edge must not be filtered out by comparing its chord with
    # an old straight edge.  That was hiding valid restore edges.
    if is_linear_edge_type(candidate_type) != is_linear_edge_type(old_type):
        return False
    if is_circular_edge_type(candidate_type) != is_circular_edge_type(old_type):
        return False

    candidate_vertex_a, candidate_vertex_b = edge_vertices(candidate_edge)
    candidate_point_a = point_tuple(candidate_vertex_a)
    candidate_point_b = point_tuple(candidate_vertex_b)
    candidate_direction = normalize_vector(vector_between_points(candidate_point_a, candidate_point_b))
    candidate_length = safe_edge_curve_length(candidate_edge)

    old_point_a = edge_record.get("point_a")
    old_point_b = edge_record.get("point_b")
    old_direction = edge_record.get("direction")
    old_length = edge_record.get("length", 0.0)

    if (
            candidate_point_a is None
            or candidate_point_b is None
            or old_point_a is None
            or old_point_b is None
    ):
        return False

    if candidate_length <= 1.0e-9 or old_length <= 1.0e-9:
        return False

    if is_circular_edge_type(candidate_type) and is_circular_edge_type(old_type):
        if not unordered_endpoints_close(
                candidate_point_a,
                candidate_point_b,
                old_point_a,
                old_point_b,
                distance_tolerance,
        ):
            return False
        length_tolerance = max(distance_tolerance, min(candidate_length, old_length) * 1.0e-4)
        return abs(candidate_length - old_length) <= length_tolerance

    if candidate_direction is None or old_direction is None:
        return False

    if abs(dot_product(candidate_direction, old_direction)) < 0.999:
        return False

    old_dist_a = point_line_distance(old_point_a, candidate_point_a, candidate_direction)
    old_dist_b = point_line_distance(old_point_b, candidate_point_a, candidate_direction)
    candidate_dist_a = point_line_distance(candidate_point_a, old_point_a, old_direction)
    candidate_dist_b = point_line_distance(candidate_point_b, old_point_a, old_direction)
    max_distance = max(old_dist_a, old_dist_b, candidate_dist_a, candidate_dist_b)
    if max_distance > distance_tolerance:
        return False

    old_interval = projected_interval(old_point_a, old_point_b, candidate_point_a, candidate_direction)
    candidate_interval = projected_interval(candidate_point_a, candidate_point_b, candidate_point_a,
                                            candidate_direction)
    overlap = intervals_overlap_length(old_interval, candidate_interval)
    min_required = min(candidate_length, old_length) * min_overlap_ratio
    return overlap >= max(min_required, distance_tolerance)


def edge_matches_any_baseline_edge(candidate_edge, baseline_edge_records):
    for edge_record in baseline_edge_records.values():
        if edge_record_overlaps_candidate(edge_record, candidate_edge):
            return True
    return False


def build_named_face_adjacency(face_infos, log=None):
    target_tags = set(face_info["tag"] for face_info in face_infos)
    info_by_tag = {face_info["tag"]: face_info for face_info in face_infos}
    adjacency = {face_info["tag"]: set() for face_info in face_infos}
    split_by_occ_component = set()

    for face_info in face_infos:
        face = face_info["face"]
        tag = face_info["tag"]
        for edge in face.GetEdges():
            for neighbor in edge_faces(edge):
                neighbor_tag = safe_tag(neighbor)
                if neighbor_tag != tag and neighbor_tag in target_tags:
                    neighbor_info = info_by_tag[neighbor_tag]
                    component_id = face_info.get("defillet_component_id")
                    neighbor_component_id = neighbor_info.get("defillet_component_id")
                    if (
                            component_id is not None
                            and neighbor_component_id is not None
                            and component_id != neighbor_component_id
                    ):
                        split_by_occ_component.add(tuple(sorted((tag, neighbor_tag))))
                        continue
                    adjacency[tag].add(neighbor_tag)

    if log and split_by_occ_component:
        log(
            "  Split {} adjacent fillet face pair(s) by OCC component metadata.".format(
                len(split_by_occ_component)
            )
        )

    return adjacency


def connected_components(face_infos, adjacency):
    info_by_tag = {face_info["tag"]: face_info for face_info in face_infos}
    unvisited = set(info_by_tag)
    components = []

    while unvisited:
        seed = min(
            unvisited,
            key=lambda tag: face_stable_sort_key(info_by_tag[tag]),
        )
        stack = [seed]
        unvisited.remove(seed)
        component_tags = []

        while stack:
            tag = stack.pop()
            component_tags.append(tag)
            for neighbor in sorted(adjacency[tag], key=lambda item: face_stable_sort_key(info_by_tag[item])):
                if neighbor in unvisited:
                    unvisited.remove(neighbor)
                    stack.append(neighbor)

        component_tags.sort(key=lambda tag: face_stable_sort_key(info_by_tag[tag]))
        components.append(
            {
                "tags": component_tags,
                "faces": [info_by_tag[tag] for tag in component_tags],
                "adjacency": {
                    tag: set(neighbor for neighbor in adjacency[tag] if neighbor in component_tags)
                    for tag in component_tags
                },
            }
        )

    components.sort(
        key=component_stable_sort_key
    )
    return components


def collect_connected_components_by_name(work_part, target_name, log=None):
    face_infos = collect_faces_by_name(work_part, target_name)
    adjacency = build_named_face_adjacency(face_infos, log=log)
    return connected_components(face_infos, adjacency)


def collect_connected_components_by_detect_rule(work_part, log=None):
    face_infos = collect_faces_by_detect_rule(work_part)
    if log and face_infos:
        counts = {}
        for face_info in face_infos:
            label = face_info.get("detect_rule_type") or face_info.get("face_type") or "Unknown"
            counts[label] = counts.get(label, 0) + 1
        log(
            "  Detect-rule residual face types: {}".format(
                ", ".join("{}={}".format(key, counts[key]) for key in sorted(counts))
            )
        )
    adjacency = build_named_face_adjacency(face_infos, log=log)
    return connected_components(face_infos, adjacency)


def collect_connected_components_by_local_detect_rule(
        work_part,
        anchor_points,
        max_distance,
        target_name,
        max_blend_radius=None,
        log=None,
):
    face_infos = collect_faces_by_local_detect_rule(
        work_part,
        anchor_points,
        max_distance,
        target_name,
        max_blend_radius=max_blend_radius,
    )
    if log:
        if face_infos:
            counts = {}
            for face_info in face_infos:
                label = face_info.get("detect_rule_type") or face_info.get("face_type") or "Unknown"
                counts[label] = counts.get(label, 0) + 1
            log(
                "  Local residual face types within {:.6g}: {}".format(
                    max_distance,
                    ", ".join("{}={}".format(key, counts[key]) for key in sorted(counts)),
                )
            )
        else:
            log("  No local residual detect-rule faces within {:.6g}.".format(max_distance))
    adjacency = build_named_face_adjacency(face_infos, log=log)
    return connected_components(face_infos, adjacency)


def radius_bucket(radius, tolerance):
    if radius is None:
        return None
    if tolerance <= 0:
        return radius
    return round(radius / tolerance) * tolerance


def serialize_face_ref(face_info):
    return {
        "tag": face_info["tag"],
        "body_index": face_info["body_index"],
        "face_index": face_info["face_index"],
        "journal_identifier": face_info["journal_identifier"],
        "ref": face_ref(face_info),
    }


def segment_color(global_segment_index):
    return FILLET_SEGMENT_COLORS[(global_segment_index - 1) % len(FILLET_SEGMENT_COLORS)]


def radius_color_key(radius_bucket):
    if radius_bucket is None:
        return "unknown"
    return "{:.9g}".format(radius_bucket)


def configure_delete_face_builder(
        builder,
        max_hole_diameter,
        max_blend_radius,
        delete_partial_blend=True,
        select_type=None,
        face_edge_blend_preference=None,
):
    builder.FaceRecognized.RelationScope = 0
    builder.FaceRecognized.CloneScope = 511

    builder.FaceRecognized.CoplanarEnabled = False
    builder.FaceRecognized.CoplanarAxesEnabled = False
    builder.FaceRecognized.CoaxialEnabled = False
    builder.FaceRecognized.SameOrbitEnabled = False
    builder.FaceRecognized.EqualDiameterEnabled = False
    builder.FaceRecognized.TangentEnabled = False
    builder.FaceRecognized.SymmetricEnabled = False
    builder.FaceRecognized.OffsetEnabled = False
    builder.FaceRecognized.RigidBodyFaceEnabled = False

    builder.FaceRecognized.UseFindClone = True
    builder.FaceRecognized.UseFindRelated = False
    builder.FaceRecognized.UseFaceBrowse = True

    builder.MaxHoleDiameter.SetFormula(str(max_hole_diameter))
    builder.MaxBlendRadius.SetFormula(str(max_blend_radius))
    builder.DeletePartialBlend = delete_partial_blend
    if face_edge_blend_preference is None:
        face_edge_blend_preference = NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions.Notch
    builder.FaceEdgeBlendPreference = face_edge_blend_preference
    builder.CapPlane = NXOpen.Plane.Null
    if select_type is None:
        select_type = NXOpen.Features.DeleteFaceBuilder.SelectTypes.FaceEdgeBlend
    builder.Type = select_type


def delete_strategy_label(select_type, face_edge_blend_preference, delete_partial_blend):
    return "select_type={}, preference={}, DeletePartialBlend={}".format(
        safe_text(select_type).rsplit(".", 1)[-1],
        safe_text(face_edge_blend_preference).rsplit(".", 1)[-1],
        delete_partial_blend,
    )


def destroy_builder(builder):
    if builder is None:
        return
    try:
        builder.Destroy()
    except Exception:
        pass


def try_delete_face_set(session, work_part, faces, max_hole_diameter, max_blend_radius, undo_on_success=False,
                        delete_partial_blend=True, reference_global_bbox=None,
                        select_type=None, face_edge_blend_preference=None):
    if not faces:
        return False, "empty face set"

    before_global_bbox = collect_global_bbox(work_part)
    if before_global_bbox is None:
        return False, "global bbox guard failed: cannot capture bbox before delete"

    reference_bbox = reference_global_bbox or before_global_bbox

    mark_id = session.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "delete named face set")
    builder = work_part.Features.CreateDeleteFaceBuilder(NXOpen.Features.Feature.Null)

    try:
        configure_delete_face_builder(
            builder,
            max_hole_diameter,
            max_blend_radius,
            delete_partial_blend=delete_partial_blend,
            select_type=select_type,
            face_edge_blend_preference=face_edge_blend_preference,
        )

        rule_options = work_part.ScRuleFactory.CreateRuleOptions()
        try:
            rule_options.SetSelectedFromInactive(False)
            face_rule = work_part.ScRuleFactory.CreateRuleFaceDumb(faces, rule_options)
        finally:
            rule_options.Dispose()

        builder.FaceCollector.ReplaceRules([face_rule], False)
        builder.FaceRecognized.CloneScope = 1535
        builder.FaceRecognized.SelectEntities(faces)
        if select_type is not None:
            builder.Type = select_type

        committed = builder.Commit()
        session.SetUndoMarkName(mark_id, "delete named face set")

        if committed is None:
            destroy_builder(builder)
            builder = None
            try:
                session.UndoToMark(mark_id, "delete named face set")
            except Exception:
                pass
            return False, "builder.Commit() returned None"

        destroy_builder(builder)
        builder = None

        after_global_bbox = collect_global_bbox(work_part)
        bbox_ok, bbox_error = validate_global_bbox_change(reference_bbox, after_global_bbox)
        if not bbox_ok:
            try:
                session.UndoToMark(mark_id, "delete named face set")
            except Exception as exc:
                return False, "{}; undo failed: {}".format(bbox_error, safe_text(exc))
            return False, bbox_error

        bbox_change_summary = bbox_change_percent_summary(before_global_bbox, after_global_bbox)
        bbox_feature_name = format_bbox_change_feature_name(bbox_change_summary)
        for feature in iter_committed_features(committed):
            set_feature_name(feature, bbox_feature_name)

        if undo_on_success:
            try:
                session.UndoToMark(mark_id, "delete named face set")
            except Exception as exc:
                return False, "delete succeeded but undo failed: {}".format(safe_text(exc))

        return True, ""
    except Exception as exc:
        error = safe_text(exc)
        destroy_builder(builder)
        builder = None
        try:
            session.UndoToMark(mark_id, "delete named face set")
        except Exception:
            pass
        return False, error
    finally:
        destroy_builder(builder)


def delete_face_set(session, work_part, faces, max_hole_diameter, max_blend_radius, undo_on_success=False,
                    reference_global_bbox=None, prefer_plain_face=False):
    select_types = NXOpen.Features.DeleteFaceBuilder.SelectTypes
    preferences = NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions
    face_edge_blend_strategies = [
        {
            "select_type": select_types.FaceEdgeBlend,
            "preference": preferences.Notch,
            "delete_partial_blend": True,
        },
        {
            "select_type": select_types.FaceEdgeBlend,
            "preference": preferences.Notch,
            "delete_partial_blend": False,
        },
        {
            "select_type": select_types.FaceEdgeBlend,
            "preference": preferences.Cliff,
            "delete_partial_blend": True,
        },
        {
            "select_type": select_types.FaceEdgeBlend,
            "preference": preferences.Cliff,
            "delete_partial_blend": False,
        },
    ]
    plain_face_strategies = [
        # Some residual blend sliver faces are accepted by NX's plain Delete Face
        # path only when the FaceEdgeBlend preference is Cliff. This mirrors the
        # recorded NX command used to manually remove those faces.
        {
            "select_type": select_types.Face,
            "preference": preferences.Cliff,
            "delete_partial_blend": True,
        },
        {
            "select_type": select_types.Face,
            "preference": preferences.Cliff,
            "delete_partial_blend": False,
        },
    ]
    if prefer_plain_face:
        strategies = plain_face_strategies + face_edge_blend_strategies
    else:
        strategies = face_edge_blend_strategies + plain_face_strategies

    errors = []
    for strategy in strategies:
        ok, error = try_delete_face_set(
            session,
            work_part,
            faces,
            max_hole_diameter,
            max_blend_radius,
            undo_on_success=undo_on_success,
            delete_partial_blend=strategy["delete_partial_blend"],
            reference_global_bbox=reference_global_bbox,
            select_type=strategy["select_type"],
            face_edge_blend_preference=strategy["preference"],
        )
        label = delete_strategy_label(
            strategy["select_type"],
            strategy["preference"],
            strategy["delete_partial_blend"],
        )
        if ok:
            if errors:
                return True, "retry succeeded with {}".format(label)
            return True, ""
        errors.append("{} failed: {}".format(label, error))

    return False, "; ".join(errors)


def sorted_tags(tags, info_by_tag):
    return sorted(tags, key=lambda tag: face_stable_sort_key(info_by_tag[tag]))


def ordered_start_tags(component):
    info_by_tag = {face_info["tag"]: face_info for face_info in component["faces"]}
    tags = component["tags"]
    adjacency = component["adjacency"]
    boundary = [tag for tag in tags if len(adjacency[tag]) == 1]
    interior_or_isolated = [tag for tag in tags if tag not in boundary]
    return sorted_tags(boundary, info_by_tag) + sorted_tags(interior_or_isolated, info_by_tag)


def choose_next_frontier_tag(selected_tags, rejected_tags, component, info_by_tag):
    adjacency = component["adjacency"]
    candidates = set()

    for tag in selected_tags:
        candidates.update(adjacency[tag])

    candidates.difference_update(selected_tags)
    candidates.difference_update(rejected_tags)
    if not candidates:
        return None

    return sorted_tags(candidates, info_by_tag)[0]


def process_connected_component(
        session,
        work_part,
        component,
        max_hole_diameter,
        max_blend_radius,
        radius_tolerance,
        radius_color_map,
        baseline_edge_records,
        reference_global_bbox,
        log,
        prefer_plain_face=False,
):
    info_by_tag = {face_info["tag"]: face_info for face_info in component["faces"]}
    tried_start_tags = set()

    for start_tag in ordered_start_tags(component):
        if start_tag in tried_start_tags:
            continue
        tried_start_tags.add(start_tag)

        start_info = info_by_tag[start_tag]
        start_ok, start_error = delete_face_set(
            session,
            work_part,
            [start_info["face"]],
            max_hole_diameter,
            max_blend_radius,
            undo_on_success=True,
            reference_global_bbox=reference_global_bbox,
            prefer_plain_face=prefer_plain_face,
        )

        if not start_ok:
            log(
                "    Start failed: body {body_index} face {face_index} {journal_identifier} | {error}".format(
                    error=start_error,
                    **start_info
                )
            )
            continue

        selected_tags = set([start_tag])
        rejected_tags = set()

        while True:
            next_tag = choose_next_frontier_tag(selected_tags, rejected_tags, component, info_by_tag)
            if next_tag is None:
                break

            trial_tags = sorted_tags(selected_tags | set([next_tag]), info_by_tag)
            trial_faces = [info_by_tag[tag]["face"] for tag in trial_tags]
            ok, error = delete_face_set(
                session,
                work_part,
                trial_faces,
                max_hole_diameter,
                max_blend_radius,
                undo_on_success=True,
                reference_global_bbox=reference_global_bbox,
                prefer_plain_face=prefer_plain_face,
            )

            if ok:
                selected_tags.add(next_tag)
                next_info = info_by_tag[next_tag]
                log(
                    "    Greedy add ok: body {body_index} face {face_index} selected_size={size}".format(
                        size=len(selected_tags),
                        **next_info
                    )
                )
            else:
                rejected_tags.add(next_tag)
                next_info = info_by_tag[next_tag]
                log(
                    "    Greedy add failed: body {body_index} face {face_index} | {error}".format(
                        error=error,
                        **next_info
                    )
                )

        final_tags = sorted_tags(selected_tags, info_by_tag)
        final_faces = [info_by_tag[tag]["face"] for tag in final_tags]
        final_ok, final_error, restore_edge_groups, all_new_edge_groups = delete_face_set_with_restore_edge_capture(
            session,
            work_part,
            component,
            final_tags,
            final_faces,
            max_hole_diameter,
            max_blend_radius,
            radius_tolerance,
            radius_color_map,
            baseline_edge_records,
            reference_global_bbox,
            log,
            prefer_plain_face=prefer_plain_face,
        )

        if final_ok:
            remaining_tags = [tag for tag in component["tags"] if tag not in selected_tags]
            log("    Greedy delete committed: {} faces".format(len(final_tags)))
            return remaining_tags, "deleted", "", restore_edge_groups, all_new_edge_groups

        log("    Greedy final commit failed: {}".format(final_error))

    if len(component["faces"]) <= SMALL_COMPONENT_SUBSET_SEARCH_LIMIT:
        return try_delete_small_component_subsets(
            session,
            work_part,
            component,
            max_hole_diameter,
            max_blend_radius,
            radius_tolerance,
            radius_color_map,
            baseline_edge_records,
            reference_global_bbox,
            log,
            prefer_plain_face=prefer_plain_face,
        )

    return component["tags"], "abandoned", "no usable start face", [], []


def try_delete_small_component_subsets(
        session,
        work_part,
        component,
        max_hole_diameter,
        max_blend_radius,
        radius_tolerance,
        radius_color_map,
        baseline_edge_records,
        reference_global_bbox,
        log,
        prefer_plain_face=False,
):
    if len(component["faces"]) > SMALL_COMPONENT_SUBSET_SEARCH_LIMIT:
        return component["tags"], "abandoned", "no usable start face", [], []

    info_by_tag = {face_info["tag"]: face_info for face_info in component["faces"]}
    ordered_tags = sorted_tags(component["tags"], info_by_tag)

    for subset_size in range(len(ordered_tags), 0, -1):
        for subset_tags in combinations(ordered_tags, subset_size):
            subset_faces = [info_by_tag[tag]["face"] for tag in subset_tags]
            ok, error = delete_face_set(
                session,
                work_part,
                subset_faces,
                max_hole_diameter,
                max_blend_radius,
                undo_on_success=True,
                reference_global_bbox=reference_global_bbox,
                prefer_plain_face=prefer_plain_face,
            )
            if not ok:
                continue

            final_ok, final_error, restore_edge_groups, all_new_edge_groups = delete_face_set_with_restore_edge_capture(
                session,
                work_part,
                component,
                subset_tags,
                subset_faces,
                max_hole_diameter,
                max_blend_radius,
                radius_tolerance,
                radius_color_map,
                baseline_edge_records,
                reference_global_bbox,
                log,
                prefer_plain_face=prefer_plain_face,
            )
            if final_ok:
                remaining_tags = [tag for tag in component["tags"] if tag not in subset_tags]
                log("    Small-component subset delete committed: {} faces".format(len(subset_tags)))
                return remaining_tags, "deleted", "", restore_edge_groups, all_new_edge_groups

            log("    Small-component subset commit failed: {}".format(final_error))

    return component["tags"], "abandoned", "no usable start face", [], []


def delete_component_whole(
        session,
        work_part,
        component,
        max_hole_diameter,
        max_blend_radius,
        radius_tolerance,
        radius_color_map,
        baseline_edge_records,
        reference_global_bbox,
        log=None,
        prefer_plain_face=False,
):
    faces = [face_info["face"] for face_info in component["faces"]]
    return delete_face_set_with_restore_edge_capture(
        session,
        work_part,
        component,
        component["tags"],
        faces,
        max_hole_diameter,
        max_blend_radius,
        radius_tolerance,
        radius_color_map,
        baseline_edge_records,
        reference_global_bbox,
        log,
        prefer_plain_face=prefer_plain_face,
    )


def capture_radius_groups_for_faces(face_infos, selected_tags, radius_tolerance, radius_color_map=None):
    selected_tags = set(selected_tags)
    component_tags = set(face_info["tag"] for face_info in face_infos)
    groups = {}

    for face_info in face_infos:
        if face_info["tag"] not in selected_tags:
            continue

        bucket = radius_bucket(face_info["blend_radius"], radius_tolerance)
        radius_key = radius_color_key(bucket)
        group = groups.setdefault(
            radius_key,
            {
                "radius_color_key": radius_key,
                "radius_bucket": bucket,
                "visual_color": None,
                "boundary_sample_points": [],
                "face_tags": [],
                "support_tags": set(),
            },
        )
        group["face_tags"].append(face_info["tag"])

        for edge in face_info["face"].GetEdges():
            edge_support_tags = []
            for neighbor in edge_faces(edge):
                neighbor_tag = safe_tag(neighbor)
                if neighbor_tag != face_info["tag"] and neighbor_tag not in component_tags:
                    edge_support_tags.append(neighbor_tag)

            if not edge_support_tags:
                continue

            group["support_tags"].update(edge_support_tags)
            vertex_a, vertex_b = edge_vertices(edge)
            for point in [point_tuple(vertex_a), point_tuple(vertex_b), edge_midpoint(edge)]:
                if point is not None:
                    group["boundary_sample_points"].append(point)

    radius_color_map = radius_color_map or {}
    for index, radius_key in enumerate(sorted(groups), start=1):
        groups[radius_key]["visual_color"] = radius_color_map.get(radius_key, segment_color(index))

    for group in groups.values():
        group["support_tags"] = sorted(group["support_tags"], key=safe_text)

    return groups


def edge_distance_to_group(edge, group):
    sample = edge_midpoint(edge)
    if sample is None:
        vertex_a, _vertex_b = edge_vertices(edge)
        sample = point_tuple(vertex_a)
    if sample is None or not group["boundary_sample_points"]:
        return None
    return min(distance_squared(sample, point) for point in group["boundary_sample_points"])


def radius_group_distance_limit(group):
    radius = group.get("radius_bucket")
    try:
        radius = abs(float(radius))
    except Exception:
        radius = 0.0

    # The restored sharp edge can move away from the original fillet/support
    # boundary by roughly the fillet radius.  Keep a little room for imported
    # STEP tolerance, but do not let distant cap/limit edges join the group.
    return max(0.25, radius * 1.5)


def edge_matches_radius_group(edge, group):
    distance = edge_distance_to_group(edge, group)
    if distance is None:
        return False

    distance_limit = radius_group_distance_limit(group)
    if distance > distance_limit * distance_limit:
        return False

    return True


def assign_created_edges_to_radius_groups(created_edges, radius_groups, filter_to_restore_candidates=False):
    if not created_edges or not radius_groups:
        return []

    if len(radius_groups) == 1:
        radius_key = next(iter(radius_groups))
        group = radius_groups[radius_key]
        if filter_to_restore_candidates:
            matched_edges = [
                edge
                for edge in created_edges
                if edge_matches_radius_group(edge, group)
            ]
        else:
            matched_edges = list(created_edges)
        return [
            {
                "radius_color_key": radius_key,
                "radius_bucket": group["radius_bucket"],
                "visual_color": group["visual_color"],
                "edges": matched_edges,
            }
        ] if matched_edges else []

    assigned = {
        radius_key: {
            "radius_color_key": radius_key,
            "radius_bucket": group["radius_bucket"],
            "visual_color": group["visual_color"],
            "edges": [],
        }
        for radius_key, group in radius_groups.items()
    }

    sorted_groups = sorted(radius_groups.items())
    for edge in created_edges:
        best_radius_key = None
        best_distance = None
        for radius_key, group in sorted_groups:
            if filter_to_restore_candidates and not edge_matches_radius_group(edge, group):
                continue
            distance = edge_distance_to_group(edge, group)
            if distance is None:
                continue
            if best_distance is None or distance < best_distance:
                best_radius_key = radius_key
                best_distance = distance
        if best_radius_key is not None:
            assigned[best_radius_key]["edges"].append(edge)

    return [group for group in assigned.values() if group["edges"]]


def serialize_restore_edge(edge):
    vertex_a, vertex_b = edge_vertices(edge)
    return {
        "edge_ref": edge_identity(edge),
        "edge_type": edge_type_name(edge),
        "edge_length": safe_edge_curve_length(edge),
        "vertex_a": serialize_point(vertex_a),
        "vertex_b": serialize_point(vertex_b),
        "midpoint": serialize_point_from_tuple(edge_midpoint(edge)),
    }


def serialize_restore_edge_groups(edge_groups):
    return [
        {
            "radius_color_key": group["radius_color_key"],
            "radius_bucket": group["radius_bucket"],
            "visual_color": group["visual_color"],
            "edge_count": len(group["edges"]),
            "edges": [serialize_restore_edge(edge) for edge in group["edges"]],
        }
        for group in edge_groups
    ]


def edge_group_count_summary(edge_groups):
    return [
        "{}:{}".format(group["radius_color_key"], len(group["edges"]))
        for group in sorted(edge_groups, key=lambda item: item["radius_color_key"])
    ]


def count_edge_types(edges):
    counts = {}
    for edge in edges:
        edge_type = edge_type_name(edge)
        counts[edge_type] = counts.get(edge_type, 0) + 1
    return counts


def delete_face_set_with_restore_edge_capture(
        session,
        work_part,
        component,
        selected_tags,
        faces,
        max_hole_diameter,
        max_blend_radius,
        radius_tolerance,
        radius_color_map,
        baseline_edge_records,
        reference_global_bbox,
        log=None,
        prefer_plain_face=False,
):
    try:
        radius_groups = capture_radius_groups_for_faces(
            component["faces"],
            selected_tags,
            radius_tolerance,
            radius_color_map,
        )
        before_edges = snapshot_edge_records(work_part)
    except Exception as exc:
        return False, "pre-delete capture failed: {}".format(safe_text(exc)), [], []

    ok, error = delete_face_set(
        session,
        work_part,
        faces,
        max_hole_diameter,
        max_blend_radius,
        undo_on_success=False,
        reference_global_bbox=reference_global_bbox,
        prefer_plain_face=prefer_plain_face,
    )
    if not ok:
        return ok, error, [], []

    try:
        after_edges = snapshot_edge_records(work_part)
    except Exception as exc:
        return False, "post-delete edge capture failed: {}".format(safe_text(exc)), [], []
    created_edge_keys = []
    for key in sorted(after_edges):
        if key not in before_edges:
            created_edge_keys.append(key)
        elif after_edges[key]["geometry_signature"] != before_edges[key]["geometry_signature"]:
            created_edge_keys.append(key)
    changed_edge_candidates = [after_edges[key]["edge"] for key in created_edge_keys]
    baseline_filtered_edges = [
        edge
        for edge in changed_edge_candidates
        if not edge_matches_any_baseline_edge(edge, baseline_edge_records)
    ]
    all_changed_edge_groups = assign_created_edges_to_radius_groups(
        changed_edge_candidates,
        radius_groups,
        filter_to_restore_candidates=False,
    )
    strict_edge_groups = assign_created_edges_to_radius_groups(
        baseline_filtered_edges,
        radius_groups,
        filter_to_restore_candidates=True,
    )
    restore_edge_groups = assign_created_edges_to_radius_groups(
        changed_edge_candidates,
        radius_groups,
        filter_to_restore_candidates=True,
    )
    if log:
        log(
            "    Restore-edge capture: before_edges={}, after_edges={}, changed_or_new={}, after_baseline_filter={}, radius_groups={}".format(
                len(before_edges),
                len(after_edges),
                len(changed_edge_candidates),
                len(baseline_filtered_edges),
                len(radius_groups),
            )
        )
        log("    Restore-edge capture all changed by radius: {}".format(
            edge_group_count_summary(all_changed_edge_groups)))
        log("    Restore-edge capture strict baseline-filtered by radius: {}".format(
            edge_group_count_summary(strict_edge_groups)))
        log("    Restore-edge capture restore candidates by radius: {}".format(
            edge_group_count_summary(restore_edge_groups)))
        log("    Restore-edge capture restore edge types: {}".format(
            count_edge_types([edge for group in restore_edge_groups for edge in group["edges"]])))
    return (
        ok,
        error,
        serialize_restore_edge_groups(restore_edge_groups),
        serialize_restore_edge_groups(all_changed_edge_groups),
    )


def deletion_history_entry(
        pass_index,
        component_index,
        mode,
        state,
        component,
        deleted_tags=None,
        error="",
        restore_edge_groups=None,
        all_new_edge_groups=None,
):
    deleted_tags = set(deleted_tags or [])
    deleted_faces = [
        serialize_face_ref(face_info)
        for face_info in component["faces"]
        if face_info["tag"] in deleted_tags
    ]
    remaining_faces = [
        serialize_face_ref(face_info)
        for face_info in component["faces"]
        if face_info["tag"] not in deleted_tags
    ]
    return {
        "pass_index": pass_index,
        "component_index_in_pass": component_index,
        "mode": mode,
        "state": state,
        "component_key": component_history_key(component),
        "component_face_count": len(component["faces"]),
        "deleted_face_count": len(deleted_faces),
        "remaining_face_count": len(remaining_faces),
        "deleted_faces": deleted_faces,
        "remaining_faces": remaining_faces,
        "restore_edge_groups": restore_edge_groups or [],
        # Kept for old recipe compatibility; the payload is all changed/new edges.
        "all_new_edge_groups": all_new_edge_groups or [],
        "error": error,
    }


def defillet_phase_label(phase):
    if phase == RULE_RESIDUAL_PHASE:
        return "rule-residual"
    if phase == LOCAL_RESIDUAL_PHASE:
        return "local-residual"
    return "named"


def collect_components_for_defillet_phase(
        work_part,
        target_name,
        phase,
        log=None,
        local_anchor_points=None,
        local_max_distance=None,
        max_blend_radius=None,
):
    if phase == RULE_RESIDUAL_PHASE:
        return collect_connected_components_by_detect_rule(work_part, log=log)
    if phase == LOCAL_RESIDUAL_PHASE:
        return collect_connected_components_by_local_detect_rule(
            work_part,
            local_anchor_points or [],
            local_max_distance or LOCAL_RESIDUAL_SEARCH_MIN_DISTANCE,
            target_name,
            max_blend_radius=max_blend_radius,
            log=log,
        )
    return collect_connected_components_by_name(work_part, target_name, log=log)


def collect_failed_faces_for_defillet_phase(
        work_part,
        target_name,
        phase,
        local_anchor_points=None,
        local_max_distance=None,
        max_blend_radius=None,
):
    if phase == RULE_RESIDUAL_PHASE:
        return collect_faces_by_detect_rule(work_part)
    if phase == LOCAL_RESIDUAL_PHASE:
        return collect_faces_by_local_detect_rule(
            work_part,
            local_anchor_points or [],
            local_max_distance or LOCAL_RESIDUAL_SEARCH_MIN_DISTANCE,
            target_name,
            max_blend_radius=max_blend_radius,
        )
    return collect_faces_by_name(work_part, target_name)


def history_mode_for_phase(phase, mode):
    if phase == RULE_RESIDUAL_PHASE:
        return "rule_residual_{}".format(mode)
    if phase == LOCAL_RESIDUAL_PHASE:
        return "local_residual_{}".format(mode)
    return mode


def delete_faces_by_name(
        session,
        work_part,
        target_name,
        max_hole_diameter,
        max_blend_radius,
        max_passes,
        radius_tolerance,
        radius_color_map,
        baseline_edge_records,
        reference_global_bbox,
        log,
        deletion_history=None,
        enable_residual_detect_rule=False,
        enable_local_residual_cleanup=True,
):
    if deletion_history is None:
        deletion_history = []

    total_deleted_faces = 0
    failed_attempts = 0
    pass_index = 0
    final_failed_faces = []
    abandoned_components = set()
    phase = NAMED_PHASE
    named_deleted_faces = 0
    deleted_named_component_ids = set()
    local_anchor_points = []
    local_pass_count = 0
    local_max_distance = max(
        LOCAL_RESIDUAL_SEARCH_MIN_DISTANCE,
        float(max_blend_radius) * LOCAL_RESIDUAL_SEARCH_FACTOR,
    )

    while True:
        pass_index += 1
        if max_passes > 0 and pass_index > max_passes:
            final_failed_faces = collect_failed_faces_for_defillet_phase(
                work_part,
                target_name,
                phase,
                local_anchor_points=local_anchor_points,
                local_max_distance=local_max_distance,
                max_blend_radius=max_blend_radius,
            )
            log("Reached max passes: {} during {} phase".format(max_passes, defillet_phase_label(phase)))
            break
        if phase == LOCAL_RESIDUAL_PHASE:
            local_pass_count += 1
            if local_pass_count > LOCAL_RESIDUAL_MAX_PASSES:
                final_failed_faces = collect_failed_faces_for_defillet_phase(
                    work_part,
                    target_name,
                    phase,
                    local_anchor_points=local_anchor_points,
                    local_max_distance=local_max_distance,
                    max_blend_radius=max_blend_radius,
                )
                log(
                    "Reached local residual pass limit: {}. Remaining local residual candidates: {}".format(
                        LOCAL_RESIDUAL_MAX_PASSES,
                        len(final_failed_faces),
                    )
                )
                break

        components = collect_components_for_defillet_phase(
            work_part,
            target_name,
            phase,
            log=log,
            local_anchor_points=local_anchor_points,
            local_max_distance=local_max_distance,
            max_blend_radius=max_blend_radius,
        )
        if phase == NAMED_PHASE:
            components = filter_already_deleted_named_components(
                components,
                deleted_named_component_ids,
            )
        components = order_components_for_defillet_delete(
            components,
            phase,
            reference_global_bbox,
        )
        current_faces_count = sum(len(component["faces"]) for component in components)

        if not components:
            if phase == NAMED_PHASE:
                log("No faces named {!r} remain.".format(target_name))
                if enable_local_residual_cleanup and named_deleted_faces > 0:
                    local_anchor_points = collect_anchor_points_from_deletion_history(deletion_history)
                    if local_anchor_points:
                        log(
                            "Starting local residual defillet near {} changed/new edge anchor points, max_distance={:.6g}.".format(
                                len(local_anchor_points),
                                local_max_distance,
                            )
                        )
                        phase = LOCAL_RESIDUAL_PHASE
                        local_pass_count = 0
                        abandoned_components.clear()
                        continue
                    log("Local residual defillet skipped: no changed/new edge anchors captured.")
                if not enable_residual_detect_rule:
                    final_failed_faces = []
                    log("Residual detect-rule defillet is disabled.")
                    break
                log("Starting residual defillet by nx_0_detect_fillet_by_rule face categories.")
                phase = RULE_RESIDUAL_PHASE
                abandoned_components.clear()
                continue

            final_failed_faces = []
            if phase == RULE_RESIDUAL_PHASE:
                log("No residual detect-rule fillet faces remain.")
            elif phase == LOCAL_RESIDUAL_PHASE:
                log("No local residual fillet faces remain.")
            else:
                log("No faces named {!r} remain.".format(target_name))
            break

        log(
            "Pass {} [{}]: {} candidate faces, {} connected components".format(
                pass_index,
                defillet_phase_label(phase),
                current_faces_count,
                len(components),
            )
        )

        made_progress = False
        pass_deleted_count = 0

        for component_index, component in enumerate(components, start=1):
            key = component_skip_key(component)
            if key in abandoned_components:
                log(
                    "  Component {}/{} skipped: already abandoned, size={}".format(
                        component_index,
                        len(components),
                        len(component["faces"]),
                    )
                )
                continue

            log(
                "  Component {}/{}: size={}, {}".format(
                    component_index,
                    len(components),
                    len(component["faces"]),
                    component_metadata_summary(component),
                )
            )

            ok, error, restore_edge_groups, all_new_edge_groups = delete_component_whole(
                session,
                work_part,
                component,
                max_hole_diameter,
                max_blend_radius,
                radius_tolerance,
                radius_color_map,
                baseline_edge_records,
                reference_global_bbox,
                log,
                prefer_plain_face=(phase == LOCAL_RESIDUAL_PHASE),
            )

            if ok:
                deleted_count = len(component["faces"])
                total_deleted_faces += deleted_count
                if phase == NAMED_PHASE:
                    named_deleted_faces += deleted_count
                    deleted_named_component_ids.update(component_named_ids(component))
                made_progress = True
                deletion_history.append(
                    deletion_history_entry(
                        pass_index,
                        component_index,
                        history_mode_for_phase(phase, "whole_component"),
                        "deleted",
                        component,
                        deleted_tags=component["tags"],
                        restore_edge_groups=restore_edge_groups,
                        all_new_edge_groups=all_new_edge_groups,
                    )
                )
                log("    Whole component deleted: {} faces".format(deleted_count))
                pass_deleted_count += 1
                if phase == NAMED_PHASE and pass_deleted_count >= MAX_DELETIONS_PER_NAMED_PASS:
                    log("    Named pass delete batch limit reached: {}".format(MAX_DELETIONS_PER_NAMED_PASS))
                    break
                continue

            failed_attempts += 1
            log("    Whole component failed: {}".format(error))

            if phase == LOCAL_RESIDUAL_PHASE:
                abandoned_components.add(key)
                deletion_history.append(
                    deletion_history_entry(
                        pass_index,
                        component_index,
                        history_mode_for_phase(phase, "whole_component"),
                        "abandoned",
                        component,
                        deleted_tags=[],
                        error=error,
                    )
                )
                log("    Local residual component abandoned after whole-delete failure.")
                continue

            if pass_deleted_count > 0:
                log("    Component deferred: prior deletes in this pass may have changed topology.")
                continue

            remaining_tags, state, process_error, restore_edge_groups, all_new_edge_groups = process_connected_component(
                session,
                work_part,
                component,
                max_hole_diameter,
                max_blend_radius,
                radius_tolerance,
                radius_color_map,
                baseline_edge_records,
                reference_global_bbox,
                log,
                prefer_plain_face=(phase == LOCAL_RESIDUAL_PHASE),
            )

            if state == "deleted":
                remaining_tag_set = set(remaining_tags)
                deleted_tags = [tag for tag in component["tags"] if tag not in remaining_tag_set]
                deleted_count = len(deleted_tags)
                total_deleted_faces += deleted_count
                if phase == NAMED_PHASE:
                    named_deleted_faces += deleted_count
                    if deleted_count == len(component["faces"]):
                        deleted_named_component_ids.update(component_named_ids(component))
                made_progress = True
                deletion_history.append(
                    deletion_history_entry(
                        pass_index,
                        component_index,
                        history_mode_for_phase(phase, "greedy_partial"),
                        "deleted",
                        component,
                        deleted_tags=deleted_tags,
                        restore_edge_groups=restore_edge_groups,
                        all_new_edge_groups=all_new_edge_groups,
                    )
                )
                log(
                    "    Component partially deleted: deleted={}, remaining={}".format(
                        deleted_count,
                        len(remaining_tags),
                    )
                )
                pass_deleted_count += 1
                if phase == NAMED_PHASE and pass_deleted_count >= MAX_DELETIONS_PER_NAMED_PASS:
                    log("    Named pass delete batch limit reached: {}".format(MAX_DELETIONS_PER_NAMED_PASS))
                    break
                continue

            failed_attempts += 1
            abandoned_components.add(key)
            deletion_history.append(
                deletion_history_entry(
                    pass_index,
                    component_index,
                    history_mode_for_phase(phase, "greedy_partial"),
                    "abandoned",
                    component,
                    deleted_tags=[],
                    error=process_error,
                )
            )
            log("    Component abandoned: {}".format(process_error))

        if made_progress:
            # Topology changed in this pass, so a component that failed earlier
            # may become deletable on the next pass. Retry abandoned components
            # instead of skipping them forever.
            abandoned_components.clear()
            continue

        if phase == NAMED_PHASE and enable_local_residual_cleanup and named_deleted_faces > 0:
            local_anchor_points = collect_anchor_points_from_deletion_history(deletion_history)
            if local_anchor_points:
                log(
                    "No more deletable named components; starting local residual defillet near {} changed/new edge anchor points, max_distance={:.6g}.".format(
                        len(local_anchor_points),
                        local_max_distance,
                    )
                )
                phase = LOCAL_RESIDUAL_PHASE
                local_pass_count = 0
                abandoned_components.clear()
                continue

        final_failed_faces = collect_failed_faces_for_defillet_phase(
            work_part,
            target_name,
            phase,
            local_anchor_points=local_anchor_points,
            local_max_distance=local_max_distance,
            max_blend_radius=max_blend_radius,
        )
        log("No more deletable components in {} phase.".format(defillet_phase_label(phase)))
        break

    return total_deleted_faces, final_failed_faces, failed_attempts


def export_step(session, work_part, output_path, settings_file):
    creator = session.DexManager.CreateStepCreator()
    try:
        creator.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap214
        creator.SettingsFile = settings_file
        creator.ObjectTypes.Solids = True
        creator.InputFile = work_part.FullPath
        creator.OutputFile = output_path
        creator.FileSaveFlag = False
        creator.LayerMask = "1-256"
        creator.ProcessHoldFlag = True
        return creator.Commit()
    finally:
        creator.Destroy()


def save_part_as(work_part, output_prt):
    os.makedirs(os.path.dirname(os.path.abspath(output_prt)), exist_ok=True)
    if os.path.exists(output_prt):
        os.remove(output_prt)

    save_status = work_part.SaveAs(output_prt)
    try:
        return save_status
    finally:
        try:
            save_status.Dispose()
        except Exception:
            pass


def main(argv=None):
    args = parse_args(argv)
    session = NXOpen.Session.GetSession()
    log = make_logger(session)

    work_part = None
    try:
        close_all_parts(session, log=log)
        cleanup_step_import_sidecars(args.input, log=log)
        log("Opening STEP: {}".format(args.input))
        work_part = open_step(session, args.input)

        deletion_history = []
        radius_color_map = {}

        contexts = collect_defillet_contexts(work_part)
        log("Defillet geometry contexts: {}".format(len(contexts)))

        deleted_count = 0
        failed_faces = []
        failed_attempts = 0

        for context_index, context in enumerate(contexts, start=1):
            log(
                "Processing context {}/{}: component={}, part={}, bodies={}".format(
                    context_index,
                    len(contexts),
                    context.get("label"),
                    part_display_name(context.get("part")),
                    context.get("body_count"),
                )
            )
            active_part = activate_defillet_context(session, context)
            if active_part is None or part_body_count(active_part) <= 0:
                log("  Skipped: no active body-owning part for this context.")
                continue

            log("  Capturing baseline edge records before defillet.")
            baseline_edge_records = snapshot_edge_records(active_part)
            baseline_global_bbox = collect_global_bbox(active_part)
            if baseline_global_bbox is None:
                log("  Warning: cannot capture original global bbox; delete guard will use per-operation bbox.")
            else:
                log(
                    "  Original global bbox: min={}, max={}".format(
                        format_bbox_values(baseline_global_bbox["min"]),
                        format_bbox_values(baseline_global_bbox["max"]),
                    )
                )

            context_deleted_count, context_failed_faces, context_failed_attempts = delete_faces_by_name(
                session,
                active_part,
                args.face_name,
                args.max_hole_diameter,
                args.max_blend_radius,
                args.max_passes,
                args.radius_tolerance,
                radius_color_map,
                baseline_edge_records,
                baseline_global_bbox,
                log,
                deletion_history=deletion_history,
                enable_residual_detect_rule=args.residual_detect_rule,
                enable_local_residual_cleanup=args.local_residual_cleanup,
            )
            log(
                "  Context result: deleted={}, failed_remaining={}, failed_attempts={}".format(
                    context_deleted_count,
                    len(context_failed_faces),
                    context_failed_attempts,
                )
            )
            deleted_count += context_deleted_count
            failed_faces.extend(context_failed_faces)
            failed_attempts += context_failed_attempts

        try:
            session.Parts.SetWork(work_part)
        except Exception as exc:
            log("Warning: could not restore top work part before export: {}".format(safe_text(exc)))

        if args.part_output:
            log("Saving NX part: {}".format(args.part_output))
            save_part_as(work_part, args.part_output)

        log("Exporting STEP: {}".format(args.output))
        export_step(session, work_part, args.output, args.settings_file)

        log("Deleted faces: {}".format(deleted_count))
        log("Failed remaining faces: {}".format(len(failed_faces)))
        log("Failed attempts: {}".format(failed_attempts))
        max_failed_faces_to_log = 30
        for face_info in failed_faces[:max_failed_faces_to_log]:
            log(
                "  Remaining: body {body_index} face {face_index} {journal_identifier}".format(
                    **face_info
                )
            )
        if len(failed_faces) > max_failed_faces_to_log:
            log("  ... {} more remaining face(s) omitted from log.".format(
                len(failed_faces) - max_failed_faces_to_log
            ))
    finally:
        if work_part is not None and not args.keep_open:
            close_part(session, work_part)

        # sleep(10)
        if not args.no_cleanup:
            cleanup_temp_files(args.input, args.output)


if __name__ == "__main__":
    main(sys.argv[1:])
