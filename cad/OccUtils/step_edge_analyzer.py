# -*- coding: utf-8 -*-
"""Small STEP topology helpers shared by OCC draft-detection scripts."""

from __future__ import annotations

import math

from OCC.Core.Bnd import Bnd_Box
from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
from OCC.Core.BRepBndLib import brepbndlib
from OCC.Core.GeomAbs import GeomAbs_Plane
from OCC.Core.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED
from OCC.Core.TopExp import topexp
from OCC.Core.TopTools import (
    TopTools_IndexedDataMapOfShapeListOfShape,
    TopTools_ListIteratorOfListOfShape,
)


DEFAULT_NORMAL_DOT_TOL = 1.0e-6
DEFAULT_PLANE_OFFSET_TOL = 1.0e-4
PLANE_OFFSET_RATIO_TOL = 1.0e-7


def build_edge_face_map(shape):
    edge_face_map = TopTools_IndexedDataMapOfShapeListOfShape()
    topexp.MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_face_map)
    return edge_face_map


def owning_faces(edge, edge_face_map):
    if not edge_face_map.Contains(edge):
        return []

    iterator = TopTools_ListIteratorOfListOfShape(edge_face_map.FindFromKey(edge))
    faces = []
    while iterator.More():
        faces.append(iterator.Value())
        iterator.Next()
    return faces


def shape_diagonal(shape):
    try:
        box = Bnd_Box()
        brepbndlib.Add(shape, box)
        xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
        dx = xmax - xmin
        dy = ymax - ymin
        dz = zmax - zmin
        return math.sqrt(dx * dx + dy * dy + dz * dz)
    except Exception:
        return 1.0


def normalize(vector):
    length = math.sqrt(sum(value * value for value in vector))
    if length <= 1.0e-12:
        return None
    return tuple(value / length for value in vector)


def dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def plane_descriptor(face):
    try:
        surface = BRepAdaptor_Surface(face, True)
        if surface.GetType() != GeomAbs_Plane:
            return None

        plane = surface.Plane()
        direction = plane.Axis().Direction()
        normal = normalize((direction.X(), direction.Y(), direction.Z()))
        if normal is None:
            return None

        if face.Orientation() == TopAbs_REVERSED:
            normal = tuple(-value for value in normal)

        point = plane.Location()
        offset = dot(normal, (point.X(), point.Y(), point.Z()))
        return {"normal": normal, "offset": offset}
    except Exception:
        return None


def are_coplanar_planes(left, right, normal_dot_tol=None, plane_offset_tol=None):
    if left is None or right is None:
        return False

    normal_dot_tol = DEFAULT_NORMAL_DOT_TOL if normal_dot_tol is None else normal_dot_tol
    plane_offset_tol = DEFAULT_PLANE_OFFSET_TOL if plane_offset_tol is None else plane_offset_tol

    normal_dot = dot(left["normal"], right["normal"])
    if abs(normal_dot) < 1.0 - normal_dot_tol:
        return False

    right_offset = right["offset"]
    if normal_dot < 0.0:
        right_offset = -right_offset

    return abs(left["offset"] - right_offset) <= plane_offset_tol


def is_flat_partition_edge(edge, shape=None, edge_face_map=None, normal_dot_tol=None, plane_offset_tol=None):
    """Return True when an edge only separates two coplanar planar faces.

    STEP imports can split one logical planar face into adjacent coplanar faces.
    The shared edge between them is a partition artifact, not a real sharp model
    edge, so draft detection should not use it as an opposite boundary.
    """
    if edge_face_map is None:
        if shape is None:
            raise ValueError("shape or edge_face_map is required")
        edge_face_map = build_edge_face_map(shape)

    faces = owning_faces(edge, edge_face_map)
    if len(faces) != 2:
        return False

    if plane_offset_tol is None and shape is not None:
        plane_offset_tol = max(
            DEFAULT_PLANE_OFFSET_TOL,
            shape_diagonal(shape) * PLANE_OFFSET_RATIO_TOL,
        )

    return are_coplanar_planes(
        plane_descriptor(faces[0]),
        plane_descriptor(faces[1]),
        normal_dot_tol=normal_dot_tol,
        plane_offset_tol=plane_offset_tol,
    )
