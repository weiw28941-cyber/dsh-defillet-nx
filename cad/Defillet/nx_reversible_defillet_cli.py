# -*- coding: utf-8 -*-
r"""NX command-line journal for the reversible defillet workflow.

Phase 1:
Open STEP -> delete faces whose imported NX face name matches a target value
using edge-connected components and a greedy grow-delete strategy -> export STEP.

Example:
    cad\run_nx_py.bat cad\Defillet\nx_reversible_defillet_cli.py -args --input "input.step" --output "output.step" --recipe "recipe.json" --recipe-csv "recipe.csv" --visual-prt "preview.prt"
"""

import argparse
import csv
import json
import os
import sys

import NXOpen
import NXOpen.Features
import NXOpen.Preferences

DEFAULT_STEP214_DEF = os.environ.get("NX_STEP214_EXPORT_DEF") or os.path.join(
    os.environ.get("UGII_BASE_DIR") or os.path.join(os.environ.get("ProgramFiles", "C:/Program Files"), "Siemens", "NX2406"),
    "STEP214UG", "ugstep214.def",
)

FILLET_SEGMENT_COLORS = [
    186, 36, 103, 141, 31, 156, 6, 211, 76, 181, 116, 46,
    201, 91, 126, 16, 171, 61, 206, 111, 151, 26, 196, 86,
]
RESTORE_EDGE_COLOR = FILLET_SEGMENT_COLORS[0]
SUPPORT_FACE_COLOR = 118
BACKGROUND_COLOR = 216
BACKGROUND_TRANSLUCENCY = 85
EDGE_OVERLAP_TOLERANCE = 1.0e-8
VISUAL_CHUNK_SIZE = 500
VISUAL_EDGE_WIDTH = "Nine"

DISPLAY_WIDTHS = {
    "Thin": "Thin",
    "Normal": "Normal",
    "Thick": "Thick",
    "One": "One",
    "Two": "Two",
    "Three": "Three",
    "Four": "Four",
    "Five": "Five",
    "Six": "Six",
    "Seven": "Seven",
    "Eight": "Eight",
    "Nine": "Nine",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="STEP file to open")
    parser.add_argument("--output", required=True, help="STEP file to export")
    parser.add_argument("--face-name", default="1", help="Imported NX face name to delete")
    parser.add_argument("--recipe", required=True, help="JSON restore recipe path")
    parser.add_argument("--recipe-csv", required=True, help="CSV restore recipe summary path")
    parser.add_argument("--visual-prt", default="", help="Post-defillet restore-edge visual .prt")
    parser.add_argument("--no-visual", action="store_true", help="Do not save the post-defillet restore-edge visual .prt")
    parser.add_argument("--settings-file", default=DEFAULT_STEP214_DEF, help="STEP214 .def settings file")
    parser.add_argument("--max-hole-diameter", default="5")
    parser.add_argument("--max-blend-radius", default="5")
    parser.add_argument("--radius-tolerance", type=float, default=1.0e-4, help="Radius grouping tolerance")
    parser.add_argument("--max-passes", type=int, default=0, help="0 means no explicit pass limit")
    parser.add_argument("--keep-open", action="store_true", help="Keep the imported part open after export")
    args = parser.parse_args(argv)
    if not args.no_visual and not args.visual_prt:
        parser.error("--visual-prt is required unless --no-visual is set")
    return args


def safe_text(value):
    if value is None:
        return ""
    try:
        return str(value)
    except Exception as exc:
        return "<str failed: {}>".format(exc)


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
        NXOpen.BasePart.CloseWholeTree.FalseValue,
        NXOpen.BasePart.CloseModified.UseResponses,
        None,
    )
    session.ApplicationSwitchImmediate("UG_APP_NOPART")


def cleanup_temp_files(input_path, output_path):
    candidates = [
        os.path.splitext(input_path)[0] + ".log",
        os.path.splitext(input_path)[0] + "_step.prt",
        os.path.splitext(output_path)[0] + ".log",
    ]

    for path in candidates:
        if os.path.exists(path):
            try:
                os.remove(path)
                print("Deleted:", path)
            except Exception as exc:
                print("Cannot delete:", path, exc)


def safe_face_journal_identifier(face):
    try:
        return safe_text(face.JournalIdentifier)
    except Exception:
        return ""


def safe_face_name(face):
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


def face_ref(face_info):
    if face_info.get("journal_identifier"):
        return face_info["journal_identifier"]
    return "body {body_index} face {face_index}".format(**face_info)


def face_skip_key(face_info):
    if face_info["journal_identifier"]:
        return face_info["journal_identifier"]
    return "{}:{}".format(face_info["body_index"], face_info["face_index"])


def component_skip_key(component):
    return tuple(sorted(face_skip_key(face_info) for face_info in component["faces"]))


def collect_faces_by_name(work_part, target_name):
    return [
        face_info
        for face_info in collect_all_face_infos(work_part)
        if face_info["name"] == target_name
    ]


def collect_all_face_infos(work_part):
    faces = []

    for body_index, body in enumerate(work_part.Bodies, start=1):
        body_name = safe_body_name(body)
        body_jid = safe_body_journal_identifier(body)
        for face_index, face in enumerate(body.GetFaces(), start=1):
            radius, is_blend, blend_error = blend_data(face)
            faces.append(
                {
                    "face": face,
                    "tag": safe_tag(face),
                    "name": safe_face_name(face),
                    "body_index": body_index,
                    "body_name": body_name,
                    "body_journal_identifier": body_jid,
                    "face_index": face_index,
                    "journal_identifier": safe_face_journal_identifier(face),
                    "face_type": face_type_name(face),
                    "blend_radius": radius,
                    "is_blend": is_blend,
                    "blend_error": blend_error,
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


def point_from_json(point):
    if point is None:
        return None
    try:
        return (float(point["x"]), float(point["y"]), float(point["z"]))
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


def edge_data_points(edge_data):
    return point_from_json(edge_data.get("vertex_a")), point_from_json(edge_data.get("vertex_b"))


def edge_data_vector(edge_data):
    point_a, point_b = edge_data_points(edge_data)
    return vector_between_points(point_a, point_b)


def edge_data_length(edge_data):
    return vector_length(edge_data_vector(edge_data))


def edge_data_direction(edge_data):
    return normalize_vector(edge_data_vector(edge_data))


def average_undirected_directions(directions):
    average = None
    count = 0
    for direction in directions:
        if direction is None:
            continue
        if average is None:
            average = direction
        else:
            sign = -1.0 if dot_product(average, direction) < 0.0 else 1.0
            average = (
                average[0] + sign * direction[0],
                average[1] + sign * direction[1],
                average[2] + sign * direction[2],
            )
        count += 1

    if count == 0:
        return None
    return normalize_vector(average)


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


def edge_direction(edge):
    vertex_a, vertex_b = edge_vertices(edge)
    return normalize_vector(vector_between_points(point_tuple(vertex_a), point_tuple(vertex_b)))


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


def snapshot_edges(work_part):
    edges = {}
    for body in work_part.Bodies:
        for edge in body.GetEdges():
            edges[edge_identity(edge)] = edge
    return edges


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
    candidate_interval = projected_interval(candidate_point_a, candidate_point_b, candidate_point_a, candidate_direction)
    overlap = intervals_overlap_length(old_interval, candidate_interval)
    min_required = min(candidate_length, old_length) * min_overlap_ratio
    return overlap >= max(min_required, distance_tolerance)


def edge_matches_any_baseline_edge(candidate_edge, baseline_edge_records):
    for edge_record in baseline_edge_records.values():
        if edge_record_overlaps_candidate(edge_record, candidate_edge):
            return True
    return False


def build_named_face_adjacency(face_infos):
    target_tags = set(face_info["tag"] for face_info in face_infos)
    adjacency = {face_info["tag"]: set() for face_info in face_infos}

    for face_info in face_infos:
        face = face_info["face"]
        tag = face_info["tag"]
        for edge in face.GetEdges():
            for neighbor in edge_faces(edge):
                neighbor_tag = safe_tag(neighbor)
                if neighbor_tag != tag and neighbor_tag in target_tags:
                    adjacency[tag].add(neighbor_tag)

    return adjacency


def connected_components(face_infos, adjacency):
    info_by_tag = {face_info["tag"]: face_info for face_info in face_infos}
    unvisited = set(info_by_tag)
    components = []

    while unvisited:
        seed = min(
            unvisited,
            key=lambda tag: (info_by_tag[tag]["body_index"], info_by_tag[tag]["face_index"]),
        )
        stack = [seed]
        unvisited.remove(seed)
        component_tags = []

        while stack:
            tag = stack.pop()
            component_tags.append(tag)
            for neighbor in sorted(adjacency[tag]):
                if neighbor in unvisited:
                    unvisited.remove(neighbor)
                    stack.append(neighbor)

        component_tags.sort(key=lambda tag: (info_by_tag[tag]["body_index"], info_by_tag[tag]["face_index"]))
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
        key=lambda component: (
            component["faces"][0]["body_index"],
            component["faces"][0]["face_index"],
        )
    )
    return components


def collect_connected_components_by_name(work_part, target_name):
    face_infos = collect_faces_by_name(work_part, target_name)
    adjacency = build_named_face_adjacency(face_infos)
    return connected_components(face_infos, adjacency)


def radius_bucket(radius, tolerance):
    if radius is None:
        return None
    if tolerance <= 0:
        return radius
    return round(radius / tolerance) * tolerance


def radii_compatible(radius_a, radius_b, tolerance):
    if radius_a is None or radius_b is None:
        return radius_a is None and radius_b is None
    return abs(radius_a - radius_b) <= tolerance


def serialize_face_info(face_info):
    return {
        "tag": face_info["tag"],
        "name": face_info["name"],
        "body_index": face_info["body_index"],
        "body_name": face_info["body_name"],
        "body_journal_identifier": face_info["body_journal_identifier"],
        "face_index": face_info["face_index"],
        "journal_identifier": face_info["journal_identifier"],
        "face_type": face_info["face_type"],
        "is_blend": face_info["is_blend"],
        "blend_radius": face_info["blend_radius"],
        "blend_error": face_info["blend_error"],
    }


def serialize_face_ref(face_info):
    return {
        "tag": face_info["tag"],
        "body_index": face_info["body_index"],
        "face_index": face_info["face_index"],
        "journal_identifier": face_info["journal_identifier"],
        "ref": face_ref(face_info),
    }


def sorted_pair(tag_a, tag_b):
    if safe_text(tag_a) <= safe_text(tag_b):
        return tag_a, tag_b
    return tag_b, tag_a


def pairwise_combinations(tags):
    ordered = list(tags)
    pairs = []
    for index, tag_a in enumerate(ordered):
        for tag_b in ordered[index + 1:]:
            pairs.append(sorted_pair(tag_a, tag_b))
    return pairs


def collect_component_support_data(component, all_info_by_tag):
    component_tags = set(component["tags"])
    support_tags_by_face = {}
    boundary_edges_by_face = {}
    support_tags = set()

    for face_info in component["faces"]:
        tag = face_info["tag"]
        support_tags_by_face[tag] = set()
        boundary_edges_by_face[tag] = []

        for edge in face_info["face"].GetEdges():
            edge_support_tags = []
            for neighbor in edge_faces(edge):
                neighbor_tag = safe_tag(neighbor)
                if neighbor_tag == tag or neighbor_tag in component_tags:
                    continue
                if neighbor_tag in all_info_by_tag:
                    support_tags_by_face[tag].add(neighbor_tag)
                    edge_support_tags.append(neighbor_tag)
                    support_tags.add(neighbor_tag)

            if edge_support_tags:
                vertex_a, vertex_b = edge_vertices(edge)
                boundary_edges_by_face[tag].append(
                    {
                        "edge_journal_identifier": safe_text(safe_call(lambda edge=edge: edge.JournalIdentifier)),
                        "support_face_tags": sorted(edge_support_tags),
                        "vertex_a": serialize_point(vertex_a),
                        "vertex_b": serialize_point(vertex_b),
                        "midpoint": serialize_point_from_tuple(edge_midpoint(edge)),
                    }
                )

    return support_tags_by_face, boundary_edges_by_face, support_tags


def segment_support_contact_stats(segment_tags, support_tags_by_face, boundary_edges_by_face):
    segment_support_tags = set()
    stats = {}

    for tag in segment_tags:
        face_support_tags = set(support_tags_by_face.get(tag, set()))
        segment_support_tags.update(face_support_tags)

        for boundary_edge in boundary_edges_by_face.get(tag, []):
            edge_support_tags = [
                support_tag
                for support_tag in boundary_edge.get("support_face_tags", [])
                if support_tag in face_support_tags
            ]
            if not edge_support_tags:
                continue

            length = edge_data_length(boundary_edge)
            direction = edge_data_direction(boundary_edge)
            for support_tag in edge_support_tags:
                stat = stats.setdefault(
                    support_tag,
                    {
                        "edge_count": 0,
                        "total_length": 0.0,
                        "directions": [],
                        "sample_points": [],
                    },
                )
                stat["edge_count"] += 1
                stat["total_length"] += length
                if direction is not None:
                    stat["directions"].append(direction)
                for point_key in ["vertex_a", "vertex_b", "midpoint"]:
                    point = boundary_edge.get(point_key)
                    if point is not None:
                        stat["sample_points"].append(point)

    return segment_support_tags, stats


def primary_support_tags_from_stats(stats, all_info_by_tag):
    ranked = sorted(
        stats,
        key=lambda tag: (
            -stats[tag]["total_length"],
            -stats[tag]["edge_count"],
            all_info_by_tag[tag]["body_index"],
            all_info_by_tag[tag]["face_index"],
        ),
    )
    return ranked[:2]


def serialize_support_contact_stats(stats, all_info_by_tag):
    rows = []
    for tag in sorted_tags(stats, all_info_by_tag):
        rows.append(
            {
                "support_face": serialize_face_ref(all_info_by_tag[tag]),
                "edge_count": stats[tag]["edge_count"],
                "total_length": stats[tag]["total_length"],
            }
        )
    return rows


def segment_spine_direction(primary_support_tags, stats):
    directions = []
    for support_tag in primary_support_tags:
        directions.extend(stats.get(support_tag, {}).get("directions", []))
    return average_undirected_directions(directions)


def split_component_into_radius_segments(component, radius_tolerance):
    info_by_tag = {face_info["tag"]: face_info for face_info in component["faces"]}
    unvisited = set(component["tags"])
    segments = []

    while unvisited:
        seed = min(unvisited, key=lambda tag: (info_by_tag[tag]["body_index"], info_by_tag[tag]["face_index"]))
        seed_radius = info_by_tag[seed]["blend_radius"]
        stack = [seed]
        unvisited.remove(seed)
        segment_tags = []

        while stack:
            tag = stack.pop()
            segment_tags.append(tag)
            for neighbor in sorted(component["adjacency"][tag]):
                if neighbor not in unvisited:
                    continue
                if not radii_compatible(seed_radius, info_by_tag[neighbor]["blend_radius"], radius_tolerance):
                    continue
                unvisited.remove(neighbor)
                stack.append(neighbor)

        segment_tags = sorted_tags(segment_tags, info_by_tag)
        radii = [
            info_by_tag[tag]["blend_radius"]
            for tag in segment_tags
            if info_by_tag[tag]["blend_radius"] is not None
        ]
        if radii:
            min_radius = min(radii)
            max_radius = max(radii)
            average_radius = sum(radii) / float(len(radii))
        else:
            min_radius = None
            max_radius = None
            average_radius = None

        segments.append(
            {
                "tags": segment_tags,
                "faces": [info_by_tag[tag] for tag in segment_tags],
                "radius_bucket": radius_bucket(seed_radius, radius_tolerance),
                "min_radius": min_radius,
                "max_radius": max_radius,
                "average_radius": average_radius,
                "radius_mode": "constant" if min_radius == max_radius else "mixed_within_tolerance",
            }
        )

    segments.sort(key=lambda segment: (segment["faces"][0]["body_index"], segment["faces"][0]["face_index"]))
    return segments


def segment_color(global_segment_index):
    return FILLET_SEGMENT_COLORS[(global_segment_index - 1) % len(FILLET_SEGMENT_COLORS)]


def radius_color_key(radius_bucket):
    if radius_bucket is None:
        return "unknown"
    return "{:.9g}".format(radius_bucket)


def build_restore_recipe(work_part, target_name, radius_tolerance):
    all_face_infos = collect_all_face_infos(work_part)
    all_info_by_tag = {face_info["tag"]: face_info for face_info in all_face_infos}
    target_face_infos = [
        face_info
        for face_info in all_face_infos
        if face_info["name"] == target_name
    ]
    adjacency = build_named_face_adjacency(target_face_infos)
    components = connected_components(target_face_infos, adjacency)

    recipe_components = []
    global_segment_index = 0
    color_by_radius_key = {}

    for component_index, component in enumerate(components, start=1):
        support_tags_by_face, boundary_edges_by_face, support_tags = collect_component_support_data(
            component,
            all_info_by_tag,
        )
        segments = split_component_into_radius_segments(component, radius_tolerance)
        recipe_segments = []

        for segment_index, segment in enumerate(segments, start=1):
            global_segment_index += 1
            radius_key = radius_color_key(segment["radius_bucket"])
            if radius_key not in color_by_radius_key:
                color_by_radius_key[radius_key] = segment_color(len(color_by_radius_key) + 1)
            segment_support_tags, support_stats = segment_support_contact_stats(
                segment["tags"],
                support_tags_by_face,
                boundary_edges_by_face,
            )
            primary_support_tags = primary_support_tags_from_stats(support_stats, all_info_by_tag)
            cap_support_tags = [
                tag
                for tag in sorted_tags(segment_support_tags, all_info_by_tag)
                if tag not in set(primary_support_tags)
            ]
            support_pair_tags = []
            if len(primary_support_tags) == 2:
                support_pair_tags = [sorted_pair(primary_support_tags[0], primary_support_tags[1])]

            boundary_sample_points = []
            for tag in segment["tags"]:
                for boundary_edge in boundary_edges_by_face.get(tag, []):
                    for point_key in ["vertex_a", "vertex_b", "midpoint"]:
                        point = boundary_edge.get(point_key)
                        if point is not None:
                            boundary_sample_points.append(point)

            recipe_segments.append(
                {
                    "segment_id": segment_index,
                    "global_segment_id": global_segment_index,
                    "visual_color": color_by_radius_key[radius_key],
                    "radius_color_key": radius_key,
                    "face_count": len(segment["faces"]),
                    "face_tags": list(segment["tags"]),
                    "faces": [serialize_face_ref(face_info) for face_info in segment["faces"]],
                    "radius_mode": segment["radius_mode"],
                    "radius_bucket": segment["radius_bucket"],
                    "min_radius": segment["min_radius"],
                    "max_radius": segment["max_radius"],
                    "average_radius": segment["average_radius"],
                    "support_face_count": len(segment_support_tags),
                    "support_faces": [
                        serialize_face_ref(all_info_by_tag[tag])
                        for tag in sorted_tags(segment_support_tags, all_info_by_tag)
                    ],
                    "primary_support_faces": [
                        serialize_face_ref(all_info_by_tag[tag])
                        for tag in primary_support_tags
                    ],
                    "cap_or_limit_faces": [
                        serialize_face_ref(all_info_by_tag[tag])
                        for tag in cap_support_tags
                    ],
                    "support_contact_stats": serialize_support_contact_stats(support_stats, all_info_by_tag),
                    "spine_direction": serialize_point_from_tuple(segment_spine_direction(primary_support_tags, support_stats)),
                    "support_face_pairs": [
                        {
                            "support_face_a": serialize_face_ref(all_info_by_tag[tag_a]),
                            "support_face_b": serialize_face_ref(all_info_by_tag[tag_b]),
                        }
                        for tag_a, tag_b in sorted(
                            support_pair_tags,
                            key=lambda pair: (
                                all_info_by_tag[pair[0]]["body_index"],
                                all_info_by_tag[pair[0]]["face_index"],
                                all_info_by_tag[pair[1]]["body_index"],
                                all_info_by_tag[pair[1]]["face_index"],
                            ),
                        )
                    ],
                    "boundary_sample_points": boundary_sample_points,
                }
            )

        recipe_components.append(
            {
                "component_id": component_index,
                "component_key": list(component_skip_key(component)),
                "face_count": len(component["faces"]),
                "segment_count": len(recipe_segments),
                "support_face_count": len(support_tags),
                "face_tags": list(component["tags"]),
                "faces": [
                    dict(
                        serialize_face_info(face_info),
                        neighbor_face_tags=sorted(component["adjacency"][face_info["tag"]]),
                        support_face_tags=sorted(support_tags_by_face.get(face_info["tag"], [])),
                        boundary_edges=boundary_edges_by_face.get(face_info["tag"], []),
                    )
                    for face_info in component["faces"]
                ],
                "support_faces": [
                    serialize_face_info(all_info_by_tag[tag])
                    for tag in sorted_tags(support_tags, all_info_by_tag)
                ],
                "segments": recipe_segments,
            }
        )

    return {
        "schema": "reversible_defillet_recipe_v1",
        "input": safe_text(safe_call(lambda: work_part.FullPath)),
        "target_face_name": target_name,
        "radius_tolerance": radius_tolerance,
        "component_count": len(recipe_components),
        "target_face_count": len(target_face_infos),
        "segment_count": global_segment_index,
        "components": recipe_components,
        "deletion_history": [],
    }


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as json_file:
        json.dump(data, json_file, ensure_ascii=False, indent=2, sort_keys=True)


def write_recipe_csv(path, recipe):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fieldnames = [
        "component_id",
        "segment_id",
        "global_segment_id",
        "visual_color",
        "radius_color_key",
        "face_count",
        "radius_mode",
        "radius_bucket",
        "min_radius",
        "max_radius",
        "average_radius",
        "support_face_count",
        "face_refs",
        "support_face_refs",
        "primary_support_refs",
        "cap_or_limit_refs",
        "support_pair_refs",
    ]

    with open(path, "w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        for component in recipe["components"]:
            for segment in component["segments"]:
                writer.writerow(
                    {
                        "component_id": component["component_id"],
                        "segment_id": segment["segment_id"],
                        "global_segment_id": segment["global_segment_id"],
                        "visual_color": segment["visual_color"],
                        "radius_color_key": segment["radius_color_key"],
                        "face_count": segment["face_count"],
                        "radius_mode": segment["radius_mode"],
                        "radius_bucket": segment["radius_bucket"],
                        "min_radius": segment["min_radius"],
                        "max_radius": segment["max_radius"],
                        "average_radius": segment["average_radius"],
                        "support_face_count": segment["support_face_count"],
                        "face_refs": " | ".join(face["ref"] for face in segment["faces"]),
                        "support_face_refs": " | ".join(face["ref"] for face in segment["support_faces"]),
                        "primary_support_refs": " | ".join(face["ref"] for face in segment["primary_support_faces"]),
                        "cap_or_limit_refs": " | ".join(face["ref"] for face in segment["cap_or_limit_faces"]),
                        "support_pair_refs": " | ".join(
                            "{} <-> {}".format(
                                pair["support_face_a"]["ref"],
                                pair["support_face_b"]["ref"],
                            )
                            for pair in segment["support_face_pairs"]
                        ),
                    }
                )


def radius_color_map_from_recipe(recipe):
    color_map = {}
    for component in recipe["components"]:
        for segment in component["segments"]:
            color_map[segment["radius_color_key"]] = segment["visual_color"]
    return color_map


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def resolve_visual_widths(width_name=VISUAL_EDGE_WIDTH):
    width_name = DISPLAY_WIDTHS[width_name]
    return (
        getattr(NXOpen.DisplayableObject.ObjectWidth, width_name),
        getattr(NXOpen.ViewDependentDisplayManager.Width, width_name),
        getattr(NXOpen.Preferences.ViewVisualizationVisual.SmoothEdgeWidth, width_name),
    )


def apply_display(session, objects, color=None, translucency=None, width=None, apply_to_all_faces=False):
    objects = [obj for obj in objects if obj is not None]
    if not objects:
        return 0

    changed = 0
    for object_chunk in chunks(objects, VISUAL_CHUNK_SIZE):
        display_modification = session.DisplayManager.NewDisplayModification()
        try:
            display_modification.ApplyToAllFaces = bool(apply_to_all_faces)
            if color is not None:
                display_modification.NewColor = int(color)
            if translucency is not None:
                display_modification.NewTranslucency = int(translucency)
            if width is not None:
                display_modification.NewWidth = width
            display_modification.Apply(object_chunk)
            changed += len(object_chunk)
        finally:
            try:
                display_modification.Dispose()
            except Exception:
                pass

    return changed


def apply_edge_display_with_fallback(session, edges, color, width):
    try:
        return apply_display(session, edges, color=color, width=width), 0
    except Exception:
        pass

    changed = 0
    failed = 0
    for edge in edges:
        try:
            edge.Color = int(color)
            edge.LineWidth = width
            edge.RedisplayObject()
            changed += 1
        except Exception:
            failed += 1
    return changed, failed


def configure_visual_edge_view(work_part, smooth_edge_width, log=None):
    try:
        work_view = work_part.ModelingViews.WorkView
    except Exception as exc:
        if log:
            log("WARNING: cannot get work view for edge visual: {}".format(safe_text(exc)))
        return False

    ok = True
    try:
        work_view.RenderingStyle = NXOpen.View.RenderingStyleType.ShadedWithEdges
        work_view.DynamicHiddenEdges = NXOpen.View.HiddenEdgesType.Invisible
        work_view.StaticHiddenEdges = NXOpen.View.HiddenEdgesType.Invisible
    except Exception as exc:
        ok = False
        if log:
            log("WARNING: cannot configure work view edge rendering: {}".format(safe_text(exc)))

    try:
        visual = work_view.VisualizationVisualPreferences
        visual.ShadedEdgeStyle = NXOpen.Preferences.ViewVisualizationVisual.ShadedEdgeStyleType.BodyColor

        appearance = visual.DisplayAppearance
        appearance.Silhouettes = False
        appearance.SmoothEdges = False
        appearance.SmoothEdgeWidth = smooth_edge_width
        visual.DisplayAppearance = appearance
    except Exception as exc:
        ok = False
        if log:
            log("WARNING: cannot configure work view edge appearance: {}".format(safe_text(exc)))

    try:
        work_view.Fit()
        work_view.UpdateDisplay()
    except Exception:
        pass

    return ok


def clear_view_dependent_edge_edits(work_part, edges, log=None):
    try:
        work_view = work_part.ModelingViews.WorkView
        for edge_chunk in chunks(edges, VISUAL_CHUNK_SIZE):
            work_view.DependentDisplay.RemoveEdit(edge_chunk)
        return True
    except Exception as exc:
        if log:
            log("WARNING: failed to clear view-dependent edge edits: {}".format(safe_text(exc)))
        return False


def apply_view_dependent_edge_edit(work_part, edges, color, view_width, log=None):
    edges = [edge for edge in edges if edge is not None]
    if not edges:
        return 0

    try:
        work_view = work_part.ModelingViews.WorkView
        nx_color = work_part.Colors.Find(int(color))
        changed = 0
        for edge_chunk in chunks(edges, VISUAL_CHUNK_SIZE):
            work_view.DependentDisplay.ApplyWireframeEdit(
                nx_color,
                NXOpen.ViewDependentDisplayManager.Font.Solid,
                view_width,
                edge_chunk,
            )
            changed += len(edge_chunk)
        return changed
    except Exception as exc:
        if log:
            log("WARNING: view-dependent edge edit failed: {}".format(safe_text(exc)))
        return 0


def make_point3d(point):
    return NXOpen.Point3d(point[0], point[1], point[2])


def object_debug_id(obj):
    return "{} tag={} jid={}".format(
        obj.__class__.__name__,
        safe_text(safe_tag(obj)),
        safe_text(safe_call(lambda: obj.JournalIdentifier)),
    )


def create_extracted_edge_curve(work_part, edge):
    errors = []
    try:
        xform = work_part.Xforms.CreateXform(NXOpen.SmartObject.UpdateOption.AfterModeling, 1.0)
        curve = work_part.Curves.CreateExtractedCurve(
            edge,
            0,
            0,
            xform,
            0.001,
            NXOpen.SmartObject.UpdateOption.AfterModeling,
        )
        try:
            curve.SetVisibility(NXOpen.SmartObject.VisibilityOption.Visible)
        except Exception as exc:
            errors.append("SetVisibility: {}".format(safe_text(exc)))
        try:
            curve.RemoveParameters()
        except Exception as exc:
            errors.append("RemoveParameters: {}".format(safe_text(exc)))
        try:
            curve.Evaluate()
        except Exception as exc:
            errors.append("Evaluate: {}".format(safe_text(exc)))
        return curve, "extracted", "; ".join(errors)
    except Exception as exc:
        errors.append("CreateExtractedCurve: {}".format(safe_text(exc)))

    if is_linear_edge_type(edge_type_name(edge)):
        point_a, point_b = edge_endpoint_tuples(edge)
        if point_a is None or point_b is None:
            return None, "line-fallback", "missing linear endpoints; {}".format("; ".join(errors))
        try:
            return work_part.Curves.CreateLine(make_point3d(point_a), make_point3d(point_b)), "line-fallback", "; ".join(errors)
        except Exception as exc:
            errors.append("CreateLine: {}".format(safe_text(exc)))
            return None, "line-fallback", "; ".join(errors)

    return None, "none", "; ".join(errors)


def create_extracted_edge_curves(work_part, edges):
    curves = []
    failed = 0
    method_counts = {}
    failure_counts = {}
    for edge in edges:
        curve, method, error = create_extracted_edge_curve(work_part, edge)
        method_counts[method] = method_counts.get(method, 0) + 1
        if curve is None:
            failed += 1
            failure_key = error or "{} failed without an exception".format(method)
            failure_counts[failure_key] = failure_counts.get(failure_key, 0) + 1
        else:
            curves.append(curve)
    return curves, failed, method_counts, failure_counts


def update_and_redisplay_visual_objects(session, objects, log=None):
    objects = [obj for obj in objects if obj is not None]
    if not objects:
        return 0

    try:
        mark_id = session.SetUndoMark(NXOpen.Session.MarkVisibility.Invisible, "update restore-edge visual")
        errors = session.UpdateManager.DoUpdate(mark_id)
        if log and errors:
            log("WARNING: restore-edge visual update reported {} errors".format(errors))
    except Exception as exc:
        if log:
            log("WARNING: restore-edge visual update failed: {}".format(safe_text(exc)))

    redisplayed = 0
    for obj in objects:
        try:
            obj.RedisplayObject()
            redisplayed += 1
        except Exception:
            pass
    return redisplayed


def edge_points_match(edge, edge_data, tolerance=1.0e-4):
    expected_a, expected_b = edge_data_points(edge_data)
    if expected_a is None or expected_b is None:
        return False

    actual_a, actual_b = edge_endpoint_tuples(edge)
    if actual_a is None or actual_b is None:
        return False

    return (
        distance_squared(actual_a, expected_a) <= tolerance * tolerance
        and distance_squared(actual_b, expected_b) <= tolerance * tolerance
    ) or (
        distance_squared(actual_a, expected_b) <= tolerance * tolerance
        and distance_squared(actual_b, expected_a) <= tolerance * tolerance
    )


def edge_geometry_matches_data(edge, edge_data, tolerance=1.0e-4):
    expected_type = safe_text(edge_data.get("edge_type"))
    if expected_type and edge_type_name(edge) != expected_type:
        return False

    expected_length = edge_data.get("edge_length")
    if expected_length is not None:
        try:
            if abs(safe_edge_curve_length(edge) - float(expected_length)) > tolerance * 10.0:
                return False
        except Exception:
            return False

    return edge_points_match(edge, edge_data, tolerance=tolerance)


def resolve_current_edge(edge_data, current_edges, current_edge_list):
    edge_ref = edge_data.get("edge_ref")
    edge = current_edges.get(edge_ref)
    if edge is not None:
        return edge, "ref"

    for candidate in current_edge_list:
        if edge_geometry_matches_data(candidate, edge_data):
            return candidate, "geometry"

    return None, "missing"


def visualize_recipe(session, work_part, recipe):
    face_info_by_ref = {}
    for face_info in collect_all_face_infos(work_part):
        face_info_by_ref[face_ref(face_info)] = face_info
        if face_info["journal_identifier"]:
            face_info_by_ref[face_info["journal_identifier"]] = face_info

    apply_display(
        session,
        list(work_part.Bodies),
        color=BACKGROUND_COLOR,
        translucency=BACKGROUND_TRANSLUCENCY,
        apply_to_all_faces=True,
    )

    support_faces = []
    for component in recipe["components"]:
        for support_face in component["support_faces"]:
            face_info = face_info_by_ref.get(support_face["journal_identifier"]) or face_info_by_ref.get(support_face["ref"])
            if face_info:
                support_faces.append(face_info["face"])

    apply_display(session, support_faces, color=SUPPORT_FACE_COLOR, translucency=55)

    for component in recipe["components"]:
        for segment in component["segments"]:
            faces = []
            for face in segment["faces"]:
                face_info = face_info_by_ref.get(face["journal_identifier"]) or face_info_by_ref.get(face["ref"])
                if face_info:
                    faces.append(face_info["face"])
            apply_display(session, faces, color=segment["visual_color"], translucency=0)
            for face in faces:
                try:
                    face.Highlight()
                except Exception:
                    pass


def current_face_lookup(work_part):
    lookup = {}
    for face_info in collect_all_face_infos(work_part):
        lookup[("tag", safe_text(face_info["tag"]))] = face_info
        lookup[("ref", face_info["ref"] if "ref" in face_info else face_ref(face_info))] = face_info
        if face_info["journal_identifier"]:
            lookup[("journal_identifier", face_info["journal_identifier"])] = face_info
    return lookup


def resolve_current_face(face_ref_data, lookup):
    journal_identifier = face_ref_data.get("journal_identifier")
    if journal_identifier:
        face_info = lookup.get(("journal_identifier", journal_identifier))
        if face_info:
            return face_info

    tag = face_ref_data.get("tag")
    if tag is not None:
        face_info = lookup.get(("tag", safe_text(tag)))
        if face_info:
            return face_info

    ref = face_ref_data.get("ref")
    if ref:
        face_info = lookup.get(("ref", ref))
        if face_info:
            return face_info

    return None


def faces_match(face, expected_face_info):
    expected_tag = safe_text(expected_face_info["tag"])
    if safe_text(safe_tag(face)) == expected_tag:
        return True

    expected_jid = expected_face_info["journal_identifier"]
    if expected_jid and safe_text(safe_call(lambda: face.JournalIdentifier)) == expected_jid:
        return True

    return False


def find_current_edges_between_faces(face_info_a, face_info_b):
    edges = []
    seen = set()

    for edge in face_info_a["face"].GetEdges():
        for neighbor in edge_faces(edge):
            if not faces_match(neighbor, face_info_b):
                continue

            edge_key = edge_identity(edge)
            if edge_key not in seen:
                seen.add(edge_key)
                edges.append(edge)
            break

    return edges


def visualize_restore_edges(session, work_part, recipe, log=None):
    current_edges = snapshot_edges(work_part)
    all_edges = list(current_edges.values())
    object_edge_width, view_edge_width, smooth_edge_width = resolve_visual_widths()
    restore_edge_color = RESTORE_EDGE_COLOR

    clear_view_dependent_edge_edits(work_part, all_edges, log)
    edges_by_radius_key = {}
    missing_edges = []
    matched_by_ref = 0
    matched_by_geometry = 0
    serialized_restore_edges = 0

    for history_entry in recipe.get("deletion_history", []):
        if history_entry.get("state") != "deleted":
            continue

        for restore_group in history_entry.get("restore_edge_groups", []):
            serialized_restore_edges += len(restore_group.get("edges", []))
            radius_key = restore_group["radius_color_key"]
            bucket = edges_by_radius_key.setdefault(
                radius_key,
                {
                    "color": restore_group["visual_color"],
                    "radius_bucket": restore_group["radius_bucket"],
                    "edges": {},
                },
            )

            for edge_data in restore_group.get("edges", []):
                edge_ref = edge_data["edge_ref"]
                edge, match_method = resolve_current_edge(edge_data, current_edges, all_edges)
                if edge is not None:
                    bucket["edges"][edge_ref] = edge
                    if match_method == "ref":
                        matched_by_ref += 1
                    else:
                        matched_by_geometry += 1
                else:
                    missing_edges.append(edge_data)

    apply_display(
        session,
        list(work_part.Bodies),
        color=BACKGROUND_COLOR,
        translucency=BACKGROUND_TRANSLUCENCY,
        apply_to_all_faces=True,
    )

    all_overlay_curves = []
    for radius_key, bucket in sorted(edges_by_radius_key.items()):
        edges = list(bucket["edges"].values())
        changed, failed = apply_edge_display_with_fallback(session, edges, restore_edge_color, object_edge_width)
        view_changed = apply_view_dependent_edge_edit(work_part, edges, restore_edge_color, view_edge_width, log)
        overlay_curves, overlay_failed, overlay_methods, overlay_failures = create_extracted_edge_curves(work_part, edges)
        overlay_changed = apply_display(session, overlay_curves, color=restore_edge_color, width=object_edge_width)
        all_overlay_curves.extend(overlay_curves)
        bucket["overlay_curves"] = overlay_curves
        bucket["overlay_failed"] = overlay_failed
        bucket["overlay_methods"] = overlay_methods
        bucket["overlay_failures"] = overlay_failures
        if log:
            log(
                "Restore-edge visual radius {}: resolved topology {}, display edits {}, view edits {}, display failed {}, overlay curves {}, overlay failed {}, overlay methods {}".format(
                    radius_key,
                    len(edges),
                    changed,
                    view_changed,
                    failed,
                    overlay_changed,
                    overlay_failed,
                    overlay_methods,
                )
            )
            if overlay_failures:
                for failure, count in sorted(overlay_failures.items(), key=lambda item: (-item[1], item[0]))[:5]:
                    log("  Overlay failure x{}: {}".format(count, failure))

    redisplayed = update_and_redisplay_visual_objects(session, all_overlay_curves, log)
    recolored = apply_display(session, all_overlay_curves, color=restore_edge_color, width=object_edge_width)
    if log:
        log("Restore-edge visual serialized restore edges: {}".format(serialized_restore_edges))
        log("Restore-edge visual resolved unique edges: {}".format(sum(len(bucket["edges"]) for bucket in edges_by_radius_key.values())))
        log("Restore-edge visual overlay redisplayed: {}".format(redisplayed))
        log("Restore-edge visual overlay recolored after update: {}".format(recolored))

    configure_visual_edge_view(work_part, smooth_edge_width, log)

    if log:
        log("Restore-edge visual matched by ref: {}".format(matched_by_ref))
        log("Restore-edge visual matched by geometry: {}".format(matched_by_geometry))
        log("Restore-edge visual missing edge refs: {}".format(len(missing_edges)))

    return {
        "radius_edge_groups": [
            {
                "radius_color_key": radius_key,
                "radius_bucket": bucket["radius_bucket"],
                "visual_color": restore_edge_color,
                "edge_count": len(bucket["edges"]),
                "edge_refs": sorted(bucket["edges"]),
            }
            for radius_key, bucket in sorted(edges_by_radius_key.items())
        ],
        "overlay_curve_groups": [
            {
                "radius_color_key": radius_key,
                "visual_color": restore_edge_color,
                "curve_count": len(bucket.get("overlay_curves", [])),
                "failed_count": bucket.get("overlay_failed", 0),
                "methods": bucket.get("overlay_methods", {}),
                "failures": bucket.get("overlay_failures", {}),
            }
            for radius_key, bucket in sorted(edges_by_radius_key.items())
        ],
        "missing_edges": missing_edges,
    }


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


def configure_delete_face_builder(builder, max_hole_diameter, max_blend_radius):
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
    builder.DeletePartialBlend = True
    builder.FaceEdgeBlendPreference = NXOpen.Features.DeleteFaceBuilder.FaceEdgeBlendPreferenceOptions.Notch
    builder.CapPlane = NXOpen.Plane.Null
    builder.Type = NXOpen.Features.DeleteFaceBuilder.SelectTypes.FaceEdgeBlend


def destroy_builder(builder):
    if builder is None:
        return
    try:
        builder.Destroy()
    except Exception:
        pass


def delete_face_set(session, work_part, faces, max_hole_diameter, max_blend_radius, undo_on_success=False):
    if not faces:
        return False, "empty face set"

    mark_id = session.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "delete named face set")
    builder = work_part.Features.CreateDeleteFaceBuilder(NXOpen.Features.Feature.Null)

    try:
        configure_delete_face_builder(builder, max_hole_diameter, max_blend_radius)

        rule_options = work_part.ScRuleFactory.CreateRuleOptions()
        try:
            rule_options.SetSelectedFromInactive(False)
            face_rule = work_part.ScRuleFactory.CreateRuleFaceDumb(faces, rule_options)
        finally:
            rule_options.Dispose()

        builder.FaceCollector.ReplaceRules([face_rule], False)
        builder.FaceRecognized.CloneScope = 1535
        builder.FaceRecognized.SelectEntities(faces)

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


def sorted_tags(tags, info_by_tag):
    return sorted(tags, key=lambda tag: (info_by_tag[tag]["body_index"], info_by_tag[tag]["face_index"]))


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
    log,
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
            log,
        )

        if final_ok:
            remaining_tags = [tag for tag in component["tags"] if tag not in selected_tags]
            log("    Greedy delete committed: {} faces".format(len(final_tags)))
            return remaining_tags, "deleted", "", restore_edge_groups, all_new_edge_groups

        log("    Greedy final commit failed: {}".format(final_error))

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
    log=None,
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
        log,
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
    log=None,
):
    radius_groups = capture_radius_groups_for_faces(
        component["faces"],
        selected_tags,
        radius_tolerance,
        radius_color_map,
    )
    before_edges = snapshot_edge_records(work_part)
    ok, error = delete_face_set(
        session,
        work_part,
        faces,
        max_hole_diameter,
        max_blend_radius,
        undo_on_success=False,
    )
    if not ok:
        return ok, error, [], []

    after_edges = snapshot_edge_records(work_part)
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
        log("    Restore-edge capture all changed by radius: {}".format(edge_group_count_summary(all_changed_edge_groups)))
        log("    Restore-edge capture strict baseline-filtered by radius: {}".format(edge_group_count_summary(strict_edge_groups)))
        log("    Restore-edge capture restore candidates by radius: {}".format(edge_group_count_summary(restore_edge_groups)))
        log("    Restore-edge capture restore edge types: {}".format(count_edge_types([edge for group in restore_edge_groups for edge in group["edges"]])))
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
        "component_key": list(component_skip_key(component)),
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
    log,
    deletion_history=None,
):
    if deletion_history is None:
        deletion_history = []

    total_deleted_faces = 0
    failed_attempts = 0
    pass_index = 0
    final_failed_faces = []
    abandoned_components = set()

    while True:
        pass_index += 1
        if max_passes > 0 and pass_index > max_passes:
            final_failed_faces = collect_faces_by_name(work_part, target_name)
            log("Reached max passes: {}".format(max_passes))
            break

        components = collect_connected_components_by_name(work_part, target_name)
        current_faces_count = sum(len(component["faces"]) for component in components)

        if not components:
            final_failed_faces = []
            log("No faces named {!r} remain.".format(target_name))
            break

        log(
            "Pass {}: {} candidate faces named {!r}, {} connected components".format(
                pass_index,
                current_faces_count,
                target_name,
                len(components),
            )
        )

        made_progress = False

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
                "  Component {}/{}: size={}".format(
                    component_index,
                    len(components),
                    len(component["faces"]),
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
                log,
            )

            if ok:
                deleted_count = len(component["faces"])
                total_deleted_faces += deleted_count
                made_progress = True
                deletion_history.append(
                    deletion_history_entry(
                        pass_index,
                        component_index,
                        "whole_component",
                        "deleted",
                        component,
                        deleted_tags=component["tags"],
                        restore_edge_groups=restore_edge_groups,
                        all_new_edge_groups=all_new_edge_groups,
                    )
                )
                log("    Whole component deleted: {} faces".format(deleted_count))
                break

            failed_attempts += 1
            log("    Whole component failed: {}".format(error))

            remaining_tags, state, process_error, restore_edge_groups, all_new_edge_groups = process_connected_component(
                session,
                work_part,
                component,
                max_hole_diameter,
                max_blend_radius,
                radius_tolerance,
                radius_color_map,
                baseline_edge_records,
                log,
            )

            if state == "deleted":
                remaining_tag_set = set(remaining_tags)
                deleted_tags = [tag for tag in component["tags"] if tag not in remaining_tag_set]
                deleted_count = len(deleted_tags)
                total_deleted_faces += deleted_count
                made_progress = True
                deletion_history.append(
                    deletion_history_entry(
                        pass_index,
                        component_index,
                        "greedy_partial",
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
                break

            failed_attempts += 1
            abandoned_components.add(key)
            deletion_history.append(
                deletion_history_entry(
                    pass_index,
                    component_index,
                    "greedy_partial",
                    "abandoned",
                    component,
                    deleted_tags=[],
                    error=process_error,
                )
            )
            log("    Component abandoned: {}".format(process_error))

        if made_progress:
            continue

        final_failed_faces = collect_faces_by_name(work_part, target_name)
        log("No more deletable components in this pass.")
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


def main(argv=None):
    args = parse_args(argv)
    session = NXOpen.Session.GetSession()
    log = make_logger(session)

    work_part = None
    try:
        log("Opening STEP: {}".format(args.input))
        work_part = open_step(session, args.input)

        log("Capturing restore recipe before defillet.")
        recipe = build_restore_recipe(work_part, args.face_name, args.radius_tolerance)
        write_json(args.recipe, recipe)
        write_recipe_csv(args.recipe_csv, recipe)
        log(
            "Recipe captured: {} components, {} segments, {} target faces".format(
                recipe["component_count"],
                recipe["segment_count"],
                recipe["target_face_count"],
            )
        )
        log("Recipe JSON: {}".format(args.recipe))
        log("Recipe CSV: {}".format(args.recipe_csv))

        deletion_history = []
        radius_color_map = radius_color_map_from_recipe(recipe)
        baseline_edge_records = snapshot_edge_records(work_part)
        deleted_count, failed_faces, failed_attempts = delete_faces_by_name(
            session,
            work_part,
            args.face_name,
            args.max_hole_diameter,
            args.max_blend_radius,
            args.max_passes,
            args.radius_tolerance,
            radius_color_map,
            baseline_edge_records,
            log,
            deletion_history=deletion_history,
        )
        recipe["deletion_history"] = deletion_history
        write_json(args.recipe, recipe)

        log("Exporting STEP: {}".format(args.output))
        export_step(session, work_part, args.output, args.settings_file)

        if not args.no_visual:
            log("Saving post-defillet restore-edge visual: {}".format(args.visual_prt))
            recipe["restore_edge_visual"] = visualize_restore_edges(session, work_part, recipe, log)
            write_json(args.recipe, recipe)
            save_part_as(work_part, args.visual_prt)

        log("Deleted faces: {}".format(deleted_count))
        log("Failed remaining faces: {}".format(len(failed_faces)))
        log("Failed attempts: {}".format(failed_attempts))
        for face_info in failed_faces:
            log(
                "  Remaining: body {body_index} face {face_index} {journal_identifier}".format(
                    **face_info
                )
            )
    finally:
        if work_part is not None and not args.keep_open:
            close_part(session, work_part)
        cleanup_temp_files(args.input, args.output)


if __name__ == "__main__":
    main(sys.argv[1:])
