# -*- coding: utf-8 -*-
r"""Detect autofillet sharp edges and assign radii by support-face pairs.

This is a drop-in replacement for nx_2_occ_detect_sharp_edge_group_inference.py.
The old script assigned each new sharp edge to the nearest original fillet face
centroid. That is unstable for nested inner/outer support surfaces: an inner
sharp edge can be spatially closer to the outer fillet face.

This version assigns radius by topology-derived support faces:

    original fillet face -> adjacent non-fillet support face pairs -> radius
    defillet new edge    -> adjacent face pair                    -> radius

Only analytic support faces are matched strongly. If support-pair matching cannot
assign a radius, the script falls back to the original nearest-fillet-face
radius flow so support pairs remain a preferred radius source rather than a
hard filter.
"""

import itertools
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
from OCC.Core.GeomAbs import (
    GeomAbs_Plane,
    GeomAbs_Cylinder,
    GeomAbs_Cone,
    GeomAbs_Sphere,
    GeomAbs_Torus,
)
from OCC.Core.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCC.Core.TopExp import topexp
from OCC.Core.TopTools import (
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_ListIteratorOfListOfShape,
)
from OCC.Extend.TopologyUtils import TopologyExplorer

from cad.Defillet import nx_2_occ_detect_sharp_edge_group_inference as base

# Geometry matching tolerances. They are intentionally tight enough to avoid
# cross-matching nested inner/outer supports, while still allowing STEP transfer
# numeric noise.
SUPPORT_DIST_RATIO_TOL = 2.0e-4
SUPPORT_RADIUS_RATIO_TOL = 2.0e-4
SUPPORT_MIN_DIST_TOL = 1.0e-3
SUPPORT_MIN_RADIUS_TOL = 1.0e-3
SUPPORT_AXIS_DOT_MIN = 0.9999
SUPPORT_AMBIGUOUS_SCORE_EPS = 1.0e-6
SUPPORT_AMBIGUOUS_DIST_EPS = 1.0e-4


def shape_hash(shape):
    try:
        return int(shape.HashCode(2147483647))
    except Exception:
        return id(shape)


def point_tuple(point):
    return (float(point.X()), float(point.Y()), float(point.Z()))


def vector_tuple(direction):
    return (float(direction.X()), float(direction.Y()), float(direction.Z()))


def dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross3(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def sub3(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def norm3(v):
    return math.sqrt(dot3(v, v))


def normalize3(v):
    length = norm3(v)
    if length <= 1.0e-12:
        return None
    return (v[0] / length, v[1] / length, v[2] / length)


def canonical_direction(v):
    result = normalize3(v)
    if result is None:
        return None
    for value in result:
        if abs(value) > 1.0e-10:
            if value < 0.0:
                result = (-result[0], -result[1], -result[2])
            break
    return result


def distance3(a, b):
    return norm3(sub3(a, b))


def point_line_distance(point, line_point, line_direction):
    direction = normalize3(line_direction)
    if direction is None:
        return None
    return norm3(cross3(sub3(point, line_point), direction))


def angle_penalty(direction_a, direction_b):
    dot = abs(dot3(direction_a, direction_b))
    dot = max(-1.0, min(1.0, dot))
    if dot < SUPPORT_AXIS_DOT_MIN:
        return None
    return 1.0 - dot


def dist_tolerance(bbox_diag):
    diag = float(bbox_diag or 1.0)
    return max(SUPPORT_MIN_DIST_TOL, diag * SUPPORT_DIST_RATIO_TOL)


def radius_tolerance(bbox_diag):
    diag = float(bbox_diag or 1.0)
    return max(SUPPORT_MIN_RADIUS_TOL, diag * SUPPORT_RADIUS_RATIO_TOL)


def build_edge_face_map(shape):
    edge_face_map = TopTools_IndexedDataMapOfShapeListOfShape()
    topexp.MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_face_map)
    return edge_face_map


def owning_faces(edge, edge_face_map):
    if not edge_face_map.Contains(edge):
        return []
    iterator = TopTools_ListIteratorOfListOfShape(edge_face_map.FindFromKey(edge))
    result = []
    while iterator.More():
        result.append(iterator.Value())
        iterator.Next()
    return result


def iter_edges(shape):
    return list(TopologyExplorer(shape).edges())


def unique_shapes(shapes):
    result = []
    for shape in shapes:
        duplicate = False
        for existing in result:
            try:
                if shape.IsSame(existing):
                    duplicate = True
                    break
            except Exception:
                pass
        if not duplicate:
            result.append(shape)
    return result


def build_face_index_by_hash(shape):
    face_index_by_hash = {}
    for index, face in enumerate(TopologyExplorer(shape).faces(), start=1):
        face_index_by_hash[shape_hash(face)] = index
    return face_index_by_hash


def face_descriptor(face, face_index=None):
    try:
        adaptor = BRepAdaptor_Surface(face)
        surface_type = adaptor.GetType()
    except Exception:
        return None

    descriptor = {
        "face": face,
        "face_hash": shape_hash(face),
        "face_index": face_index,
        "kind": None,
    }

    try:
        centroid = base.face_centroid(face)
    except Exception:
        centroid = None
    descriptor["centroid"] = centroid

    if surface_type == GeomAbs_Plane:
        try:
            plane = adaptor.Plane()
            normal = canonical_direction(vector_tuple(plane.Axis().Direction()))
            if normal is None:
                return None
            location = point_tuple(plane.Location())
            descriptor.update(
                {
                    "kind": "plane",
                    "normal": normal,
                    "offset": dot3(normal, location),
                }
            )
            return descriptor
        except Exception:
            return None

    if surface_type == GeomAbs_Cylinder:
        try:
            cylinder = adaptor.Cylinder()
            axis = cylinder.Axis()
            direction = canonical_direction(vector_tuple(axis.Direction()))
            if direction is None:
                return None
            descriptor.update(
                {
                    "kind": "cylinder",
                    "axis_point": point_tuple(axis.Location()),
                    "axis_direction": direction,
                    "radius": abs(float(cylinder.Radius())),
                }
            )
            return descriptor
        except Exception:
            return None

    if surface_type == GeomAbs_Cone:
        try:
            cone = adaptor.Cone()
            axis = cone.Axis()
            direction = canonical_direction(vector_tuple(axis.Direction()))
            if direction is None:
                return None
            descriptor.update(
                {
                    "kind": "cone",
                    "axis_point": point_tuple(axis.Location()),
                    "axis_direction": direction,
                    "semi_angle": abs(float(cone.SemiAngle())),
                    "ref_radius": abs(float(cone.RefRadius())),
                }
            )
            return descriptor
        except Exception:
            return None

    if surface_type == GeomAbs_Sphere:
        try:
            sphere = adaptor.Sphere()
            descriptor.update(
                {
                    "kind": "sphere",
                    "center": point_tuple(sphere.Location()),
                    "radius": abs(float(sphere.Radius())),
                }
            )
            return descriptor
        except Exception:
            return None

    if surface_type == GeomAbs_Torus:
        try:
            torus = adaptor.Torus()
            axis = torus.Axis()
            direction = canonical_direction(vector_tuple(axis.Direction()))
            if direction is None:
                return None
            descriptor.update(
                {
                    "kind": "torus",
                    "center": point_tuple(axis.Location()),
                    "axis_direction": direction,
                    "major_radius": abs(float(torus.MajorRadius())),
                    "minor_radius": abs(float(torus.MinorRadius())),
                }
            )
            return descriptor
        except Exception:
            return None

    return None


def surface_match_score(source, target, bbox_diag):
    if source is None or target is None:
        return None
    if source.get("kind") != target.get("kind"):
        return None

    dist_tol = dist_tolerance(bbox_diag)
    rad_tol = radius_tolerance(bbox_diag)
    kind = source["kind"]

    if kind == "plane":
        penalty = angle_penalty(source["normal"], target["normal"])
        if penalty is None:
            return None
        offset_delta = abs(float(source["offset"]) - float(target["offset"]))
        if offset_delta > dist_tol:
            return None
        return penalty + offset_delta / dist_tol

    if kind == "cylinder":
        penalty = angle_penalty(source["axis_direction"], target["axis_direction"])
        if penalty is None:
            return None
        radius_delta = abs(float(source["radius"]) - float(target["radius"]))
        if radius_delta > rad_tol:
            return None
        line_dist = point_line_distance(
            target["axis_point"],
            source["axis_point"],
            source["axis_direction"],
        )
        if line_dist is None or line_dist > dist_tol:
            return None
        return penalty + radius_delta / rad_tol + line_dist / dist_tol

    if kind == "cone":
        penalty = angle_penalty(source["axis_direction"], target["axis_direction"])
        if penalty is None:
            return None
        line_dist = point_line_distance(
            target["axis_point"],
            source["axis_point"],
            source["axis_direction"],
        )
        angle_delta = abs(float(source["semi_angle"]) - float(target["semi_angle"]))
        radius_delta = abs(float(source["ref_radius"]) - float(target["ref_radius"]))
        if line_dist is None or line_dist > dist_tol:
            return None
        if angle_delta > 1.0e-4 or radius_delta > rad_tol:
            return None
        return penalty + line_dist / dist_tol + angle_delta / 1.0e-4 + radius_delta / rad_tol

    if kind == "sphere":
        center_dist = distance3(source["center"], target["center"])
        radius_delta = abs(float(source["radius"]) - float(target["radius"]))
        if center_dist > dist_tol or radius_delta > rad_tol:
            return None
        return center_dist / dist_tol + radius_delta / rad_tol

    if kind == "torus":
        penalty = angle_penalty(source["axis_direction"], target["axis_direction"])
        if penalty is None:
            return None
        center_dist = distance3(source["center"], target["center"])
        major_delta = abs(float(source["major_radius"]) - float(target["major_radius"]))
        minor_delta = abs(float(source["minor_radius"]) - float(target["minor_radius"]))
        if center_dist > dist_tol or major_delta > rad_tol or minor_delta > rad_tol:
            return None
        return penalty + center_dist / dist_tol + major_delta / rad_tol + minor_delta / rad_tol

    return None


def support_pair_match_score(edge_pair, support_pair, bbox_diag):
    if len(edge_pair) != 2 or len(support_pair) != 2:
        return None

    a0 = surface_match_score(edge_pair[0], support_pair[0], bbox_diag)
    a1 = surface_match_score(edge_pair[1], support_pair[1], bbox_diag)
    score_a = None if a0 is None or a1 is None else a0 + a1

    b0 = surface_match_score(edge_pair[0], support_pair[1], bbox_diag)
    b1 = surface_match_score(edge_pair[1], support_pair[0], bbox_diag)
    score_b = None if b0 is None or b1 is None else b0 + b1

    if score_a is None:
        return score_b
    if score_b is None:
        return score_a
    return min(score_a, score_b)


def is_target_fillet_face_hash(face_hash, target_face_hashes):
    return face_hash in target_face_hashes


def collect_support_faces_for_fillet(fillet_face, edge_face_map, target_face_hashes):
    support_faces = []
    for edge in iter_edges(fillet_face):
        for face in owning_faces(edge, edge_face_map):
            face_hash = shape_hash(face)
            try:
                if face.IsSame(fillet_face):
                    continue
            except Exception:
                pass
            if is_target_fillet_face_hash(face_hash, target_face_hashes):
                continue
            support_faces.append(face)
    return unique_shapes(support_faces)


def radius_face_by_hash(radius_faces):
    result = {}
    for item in radius_faces:
        result[shape_hash(item["face"])] = item
    return result


def build_original_support_pair_records(
    original_shape,
    original_face_rows,
    radius_faces,
    target_name,
    bbox_diag,
):
    edge_face_map = build_edge_face_map(original_shape)
    face_index_by_hash = {
        shape_hash(row["face"]): row["face_index"]
        for row in original_face_rows
    }
    target_face_hashes = set(
        shape_hash(row["face"])
        for row in original_face_rows
        if base.is_defillet_target_face_name(row["face_name"], target_name)
    )
    usable_radius_by_hash = radius_face_by_hash(radius_faces)

    records = []
    skipped_not_enough_support = 0
    skipped_unsupported_support = 0

    for face_hash, radius_face in usable_radius_by_hash.items():
        fillet_face = radius_face["face"]
        support_faces = collect_support_faces_for_fillet(
            fillet_face,
            edge_face_map,
            target_face_hashes,
        )
        if len(support_faces) < 2:
            skipped_not_enough_support += 1
            continue

        support_descriptors = []
        for support_face in support_faces:
            descriptor = face_descriptor(
                support_face,
                face_index=face_index_by_hash.get(shape_hash(support_face)),
            )
            if descriptor is None:
                skipped_unsupported_support += 1
                continue
            support_descriptors.append(descriptor)

        if len(support_descriptors) < 2:
            skipped_not_enough_support += 1
            continue

        for pair in itertools.combinations(support_descriptors, 2):
            records.append(
                {
                    "radius_face": radius_face,
                    "support_pair": pair,
                    "radius": radius_face.get("radius"),
                    "radius_bucket": radius_face.get("radius_bucket"),
                    "radius_key": radius_face.get("radius_key"),
                    "fillet_face_index": radius_face.get("face_index"),
                    "fillet_step_entity_id": radius_face.get("step_entity_id"),
                }
            )

    print("\nSupport-pair radius source summary:")
    print("  target fillet faces:", len(target_face_hashes))
    print("  usable radius fillet faces:", len(radius_faces))
    print("  support-pair records:", len(records))
    print("  skipped fillet faces without enough support:", skipped_not_enough_support)
    print("  skipped unsupported support faces:", skipped_unsupported_support)
    print("  support match dist tolerance:", dist_tolerance(bbox_diag))
    print("  support match radius tolerance:", radius_tolerance(bbox_diag))
    return records


def adjacent_descriptors_for_edge(edge, edge_face_map, face_index_by_hash):
    faces = unique_shapes(owning_faces(edge, edge_face_map))
    descriptors = []
    for face in faces:
        descriptor = face_descriptor(
            face,
            face_index=face_index_by_hash.get(shape_hash(face)),
        )
        if descriptor is not None:
            descriptors.append(descriptor)
    return descriptors


def edge_support_pair_candidates(edge_descriptors):
    if len(edge_descriptors) < 2:
        return []
    return list(itertools.combinations(edge_descriptors, 2))


def centroid_distance_to_radius_face(edge_centroid, record):
    radius_face = record.get("radius_face")
    if edge_centroid is None or radius_face is None:
        return None
    face_centroid = radius_face.get("centroid")
    if face_centroid is None:
        return None
    return distance3(edge_centroid, face_centroid)


def find_support_pair_radius_match(edge_item, edge_pair_candidates, support_records, bbox_diag):
    try:
        edge_centroid = base.edge_centroid(edge_item["edge"])
    except Exception:
        edge_centroid = None

    candidates = []
    for edge_pair in edge_pair_candidates:
        for record in support_records:
            pair_score = support_pair_match_score(
                edge_pair,
                record["support_pair"],
                bbox_diag,
            )
            if pair_score is None:
                continue
            centroid_dist = centroid_distance_to_radius_face(edge_centroid, record)
            candidates.append(
                {
                    "record": record,
                    "pair_score": pair_score,
                    "centroid_distance": centroid_dist,
                    "edge_pair": edge_pair,
                }
            )

    if not candidates:
        return None, "no_support_pair_match", edge_centroid, []

    def sort_key(candidate):
        dist = candidate["centroid_distance"]
        if dist is None:
            dist = 1.0e99
        return (candidate["pair_score"], dist)

    candidates.sort(key=sort_key)
    best = candidates[0]
    best_record = best["record"]
    best_dist = best["centroid_distance"]

    ambiguous = []
    for candidate in candidates[1:]:
        record = candidate["record"]
        if record.get("radius_key") == best_record.get("radius_key"):
            continue
        score_close = (
            abs(candidate["pair_score"] - best["pair_score"])
            <= SUPPORT_AMBIGUOUS_SCORE_EPS
        )
        cand_dist = candidate["centroid_distance"]
        dist_close = (
            best_dist is not None
            and cand_dist is not None
            and abs(cand_dist - best_dist) <= SUPPORT_AMBIGUOUS_DIST_EPS
        )
        if score_close and (best_dist is None or cand_dist is None or dist_close):
            ambiguous.append(candidate)

    if ambiguous:
        return None, "ambiguous_support_pair_match", edge_centroid, candidates[:5]

    return best, "support_pair", edge_centroid, candidates[:5]


def make_unknown_radius_item(item, edge_centroid, reason, edge_descriptors):
    result = dict(item)
    result.update(
        {
            "matched": False,
            "edge_centroid": edge_centroid,
            "nearest_face": None,
            "nearest_distance": None,
            "radius": None,
            "radius_bucket": None,
            "radius_key": "UNKNOWN",
            "radius_source": "UNKNOWN",
            "radius_unknown_reason": reason,
            "support_pair_failed_reason": reason,
            "support_adjacent_face_count": len(edge_descriptors),
        }
    )
    return result


def make_nearest_face_fallback_radius_item(
        item,
        radius_faces,
        support_pair_reason,
        edge_descriptors,
):
    if not radius_faces:
        return None

    try:
        match = base.match_nearest_radius_face(item, radius_faces)
    except Exception:
        return None

    if not match.get("matched") or match.get("radius") is None:
        return None

    result = dict(item)
    result.update(
        {
            "matched": True,
            "edge_centroid": match.get("edge_centroid"),
            "nearest_face": match.get("nearest_face"),
            "nearest_distance": match.get("nearest_distance"),
            "radius": match.get("radius"),
            "radius_bucket": match.get("radius_bucket"),
            "radius_key": match.get("radius_key"),
            "radius_source": "nearest_face_fallback",
            "support_pair_failed_reason": support_pair_reason,
            "support_adjacent_face_count": len(edge_descriptors),
        }
    )
    return result


def assign_radius_to_new_edges_by_support_pairs(
        new_edges,
        defillet_shape,
        support_records,
        bbox_diag,
        radius_faces=None,
):
    radius_faces = radius_faces or []
    edge_face_map = build_edge_face_map(defillet_shape)
    face_index_by_hash = build_face_index_by_hash(defillet_shape)
    enriched = []
    stats = defaultdict(int)

    for item in new_edges:
        edge_descriptors = adjacent_descriptors_for_edge(
            item["edge"],
            edge_face_map,
            face_index_by_hash,
        )
        edge_pair_candidates = edge_support_pair_candidates(edge_descriptors)

        if not edge_pair_candidates:
            try:
                edge_centroid = base.edge_centroid(item["edge"])
            except Exception:
                edge_centroid = None
            reason = "not_enough_adjacent_support_faces"
            fallback_item = make_nearest_face_fallback_radius_item(
                item,
                radius_faces,
                reason,
                edge_descriptors,
            )
            if fallback_item is not None:
                enriched.append(fallback_item)
                stats["nearest_face_fallback_assigned"] += 1
                nearest = fallback_item.get("nearest_face") or {}
                print(
                    "[WARN] support-pair radius fallback: edge_index={} reason={} -> radius={} face={} dist={}".format(
                        item["edge_index"],
                        reason,
                        fallback_item.get("radius_key"),
                        nearest.get("face_index"),
                        fallback_item.get("nearest_distance"),
                    )
                )
                continue

            enriched.append(
                make_unknown_radius_item(
                    item,
                    edge_centroid,
                    reason,
                    edge_descriptors,
                )
            )
            stats["unknown_" + reason] += 1
            print(
                "[WARN] support-pair radius UNKNOWN after nearest-face fallback: edge_index={} reason={} adjacent_faces={}".format(
                    item["edge_index"],
                    reason,
                    len(edge_descriptors),
                )
            )
            continue

        match, reason, edge_centroid, top_candidates = find_support_pair_radius_match(
            item,
            edge_pair_candidates,
            support_records,
            bbox_diag,
        )

        if match is None:
            fallback_item = make_nearest_face_fallback_radius_item(
                item,
                radius_faces,
                reason,
                edge_descriptors,
            )
            if fallback_item is not None:
                enriched.append(fallback_item)
                stats["nearest_face_fallback_assigned"] += 1
                nearest = fallback_item.get("nearest_face") or {}
                print(
                    "[WARN] support-pair radius fallback: edge_index={} reason={} -> radius={} face={} dist={}".format(
                        item["edge_index"],
                        reason,
                        fallback_item.get("radius_key"),
                        nearest.get("face_index"),
                        fallback_item.get("nearest_distance"),
                    )
                )
                for candidate in top_candidates:
                    record = candidate["record"]
                    print(
                        "       support candidate radius={} fillet_face={} score={:.9g} centroid_dist={}".format(
                            record.get("radius_key"),
                            record.get("fillet_face_index"),
                            candidate["pair_score"],
                            candidate["centroid_distance"],
                        )
                    )
                continue

            enriched.append(
                make_unknown_radius_item(
                    item,
                    edge_centroid,
                    reason,
                    edge_descriptors,
                )
            )
            stats["unknown_" + reason] += 1
            print(
                "[WARN] support-pair radius UNKNOWN after nearest-face fallback: edge_index={} reason={} adjacent_faces={}".format(
                    item["edge_index"],
                    reason,
                    len(edge_descriptors),
                )
            )
            for candidate in top_candidates:
                record = candidate["record"]
                print(
                    "       candidate radius={} fillet_face={} score={:.9g} centroid_dist={}".format(
                        record.get("radius_key"),
                        record.get("fillet_face_index"),
                        candidate["pair_score"],
                        candidate["centroid_distance"],
                    )
                )
            continue

        record = match["record"]
        radius_face = record["radius_face"]
        enriched_item = dict(item)
        enriched_item.update(
            {
                "matched": True,
                "edge_centroid": edge_centroid,
                "nearest_face": radius_face,
                "nearest_distance": match["centroid_distance"],
                "radius": record.get("radius"),
                "radius_bucket": record.get("radius_bucket"),
                "radius_key": record.get("radius_key"),
                "radius_source": "support_pair",
                "support_pair_score": match["pair_score"],
                "support_adjacent_face_count": len(edge_descriptors),
            }
        )
        enriched.append(enriched_item)
        stats["support_pair_assigned"] += 1

    print("\nSupport-pair radius assignment summary:")
    print("  new edges:", len(new_edges))
    print("  assigned by support pairs:", stats["support_pair_assigned"])
    print("  assigned by nearest-face fallback:", stats["nearest_face_fallback_assigned"])
    unknown_total = sum(value for key, value in stats.items() if key.startswith("unknown_"))
    print("  UNKNOWN:", unknown_total)
    for key in sorted(stats):
        if key.startswith("unknown_"):
            print("  {}: {}".format(key, stats[key]))

    return enriched


def matched_target_edge_groups(matched):
    grouped = defaultdict(list)

    for match_index, match in enumerate(matched, start=1):
        edge_info = {}
        try:
            edge_info = json.loads(match.get("name", "") or "{}")
        except Exception:
            edge_info = {}

        radius = edge_info.get("radius")
        try:
            radius = float(radius) if radius is not None else None
        except Exception:
            radius = None

        radius_key = base.radius_key(radius)
        item = {
            "edge": match["target_edge"],
            "edge_index": match_index,
            "component_id": edge_info.get("component_id"),
            "radius": radius,
            "radius_bucket": radius,
            "radius_key": radius_key,
            "matched_name": match.get("name", ""),
            "normalized_dist": match.get("normalized_dist"),
            "length_ratio": match.get("length_ratio"),
            "tangent_dot": match.get("tangent_dot"),
        }
        grouped[radius_key].append(item)

    def sort_key(key):
        if key == "UNKNOWN":
            return (1, 0.0)
        sample = grouped[key][0]
        radius = sample.get("radius_bucket")
        try:
            radius_value = float(radius)
        except Exception:
            radius_value = 0.0
        return (0, radius_value)

    groups = []
    for group_id, key in enumerate(sorted(grouped.keys(), key=sort_key), start=1):
        items = grouped[key]
        radius_available = key != "UNKNOWN"
        radii = [
            item["radius"]
            for item in items
            if item.get("radius") is not None
        ]
        groups.append(
            {
                "group_id": group_id,
                "radius_key": key,
                "radius_bucket": items[0].get("radius_bucket"),
                "edges": items,
                "edge_count": len(items),
                "radius_available": radius_available,
                "min_radius": min(radii) if radii else None,
                "max_radius": max(radii) if radii else None,
                "average_radius": sum(radii) / len(radii) if radii else None,
                "color": base.group_color(group_id, radius_available),
            }
        )

    return groups


def main(argv=None):
    args = base.parse_args(argv)
    wrote_model_edge_info = False

    print("Reading original STEP with face names:", args.original)
    original_shape, original_face_rows = base.read_step_with_face_names(args.original)

    print("Reading defillet STEP:", args.defillet)
    defillet_shape = base.read_step(args.defillet)

    model_path = args.defillet_from_model
    print("Reading defillet-from-model STEP for radius threshold:", model_path)
    model_shape_for_radius = base.read_step(model_path)
    radius_threshold = base.bbox_half_min_radius_threshold(model_shape_for_radius)
    display_shape = model_shape_for_radius
    display_edge_groups = []
    bbox_diag = base.compute_bbox_diagonal(model_shape_for_radius) if hasattr(base, "compute_bbox_diagonal") else None
    if bbox_diag is None:
        try:
            from cad.OccUtils.edge_retag_utils import compute_bbox_diagonal

            bbox_diag = compute_bbox_diagonal(model_shape_for_radius)
        except Exception:
            bbox_diag = 1.0

    print("Collecting original edges ...")
    original_edges = base.collect_edges(original_shape)

    print("Collecting defillet edges ...")
    defillet_edges = base.collect_edges(defillet_shape)

    print("Original edge count:", len(original_edges))
    print("Defillet edge count:", len(defillet_edges))

    print("\nCollecting usable radius fillet faces from original STEP ...")
    radius_faces = base.collect_radius_fillet_faces(
        face_rows=original_face_rows,
        target_name=args.face_name,
        tolerance=args.radius_tolerance,
        max_radius=radius_threshold,
    )

    if not radius_faces:
        raise RuntimeError(
            "No usable radius fillet faces found. "
            "Please check --face-name or original STEP face names."
        )

    support_records = build_original_support_pair_records(
        original_shape=original_shape,
        original_face_rows=original_face_rows,
        radius_faces=radius_faces,
        target_name=args.face_name,
        bbox_diag=bbox_diag,
    )

    print("\nDetecting new defillet edges ...")
    new_edges = base.collect_new_edges(
        defillet_edges=defillet_edges,
        original_edges=original_edges,
    )

    print("\nAssigning fillet radius to new edges by support-face pairs ...")
    new_edges_with_radius = assign_radius_to_new_edges_by_support_pairs(
        new_edges=new_edges,
        defillet_shape=defillet_shape,
        support_records=support_records,
        bbox_diag=bbox_diag,
        radius_faces=radius_faces,
    )

    histories = getattr(args, "histories", []) or []
    if histories:
        print("\nMatching OCC-approved edges to defillet history order ...")
        new_edges_with_radius = base.attach_restore_order_from_history(
            new_edges_with_radius,
            histories,
            defillet_shape,
        )

    print("\nAssigning connected component ids to new edges ...")
    new_edges_with_radius = base.assign_connected_component_ids(
        new_edges_with_radius,
        tolerance=args.component_endpoint_tolerance,
    )

    edge_groups = base.build_edge_radius_groups(new_edges_with_radius)

    base.print_summary(
        original_path=args.original,
        defillet_path=args.defillet,
        original_edges=original_edges,
        defillet_edges=defillet_edges,
        new_edges=new_edges,
        radius_faces=radius_faces,
        edge_groups=edge_groups,
    )

    base.print_edge_radius_summary(edge_groups)

    model_path = args.defillet_from_model

    print("\n" + "=" * 60)
    print("  source（检测结果来自）:", args.defillet)
    print("  target（模型输出）    :", model_path)

    unknown_radius_edges = [
        item for item in new_edges_with_radius
        if item.get("radius") is None
    ]
    if unknown_radius_edges:
        print(
            "  UNKNOWN radius edges kept for STEP retag:",
            len(unknown_radius_edges),
        )

    named_edges = base.build_named_edges_for_retag(
        new_edges_with_radius,
        edge_groups,
        defillet_shape,
    )
    print("  检测到的有标签新边数:", len(named_edges))

    if not named_edges:
        print("[WARN] 没有可迁移的标签，跳过模型 STEP 输出。")
    else:
        print("\nReading defillet-from-model STEP:", model_path)
        model_shape = base.read_step(model_path)
        display_shape = model_shape

        print("\n执行几何匹配 ...")
        matched, unmatched_indices = base.match_named_edges(
            named_edges=named_edges,
            target_shape=model_shape,
            bbox_diag=None,
            dist_ratio_threshold=args.dist_ratio_threshold,
            length_ratio_threshold=args.length_ratio_threshold,
            tangent_dot_threshold=args.tangent_dot_threshold,
            verbose=True,
        )

        print("\n匹配结果汇总:")
        print("  有标签新边总数: {}".format(len(named_edges)))
        print("  成功匹配:       {}".format(len(matched)))
        print("  未匹配:         {}".format(len(unmatched_indices)))

        if unmatched_indices:
            print("  未匹配的 source 边:")
            for si in unmatched_indices:
                _, name = named_edges[si]
                print("    source #{:3d}  name={}".format(si, name))

        if not matched:
            print("[WARN] 没有任何边匹配成功，不写出模型 STEP。")
        else:
            display_edge_groups = matched_target_edge_groups(matched)
            model_edge_info_output = args.output or base.suffixed_step_path(
                model_path,
                base.DEFAULT_EDGE_INFO_SUFFIX,
            )
            base.write_model_step_with_matched_edge_names(
                output_path=model_edge_info_output,
                model_shape=model_shape,
                matched=matched,
            )
            wrote_model_edge_info = True
            print(
                "\n完成。可将 {} 送入后续 autofillet 流程。".format(
                    os.path.basename(model_edge_info_output)
                )
            )

    if not args.no_display:
        print("\nDisplaying matched target model edges:", model_path)
        base.display_result(
            defillet_shape=display_shape,
            edge_groups=display_edge_groups,
            edge_width=args.edge_width,
            dim_background=not args.no_dim_background,
        )

    return 0 if wrote_model_edge_info else 2


if __name__ == "__main__":
    raise SystemExit(main())
