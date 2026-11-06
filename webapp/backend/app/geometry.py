from __future__ import annotations

import math
from pathlib import Path
import sys
from typing import Any

from OCC.Core.BRep import BRep_Tool
from OCC.Core.BRepGProp import brepgprop
from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
from OCC.Core.GProp import GProp_GProps
from OCC.Core.TopAbs import TopAbs_REVERSED
from OCC.Core.TopLoc import TopLoc_Location

from .config import REPO_ROOT, settings

sys.path.insert(0, str(REPO_ROOT))
from cad.OccUtils.map_freecad_face_to_step import (  # noqa: E402
    entity_info,
    enumerate_faces,
    read_step,
    surface_type,
)
def _finite(value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite geometry value")
    return value


def build_face_mesh(step_path: Path, revision_id: str) -> dict[str, Any]:
    reader, shape = read_step(str(step_path))
    BRepMesh_IncrementalMesh(
        shape, settings.mesh_deflection, False, settings.mesh_angle, True
    ).Perform()
    faces = enumerate_faces(shape)
    payload_faces: list[dict[str, Any]] = []

    for face_index, face in enumerate(faces, start=1):
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation(face, location)
        if triangulation is None or triangulation.NbTriangles() <= 0:
            continue
        transform = location.Transformation()
        positions: list[float] = []
        for node_index in range(1, triangulation.NbNodes() + 1):
            point = triangulation.Node(node_index).Transformed(transform)
            positions.extend([_finite(point.X()), _finite(point.Y()), _finite(point.Z())])
        indices: list[int] = []
        reversed_face = face.Orientation() == TopAbs_REVERSED
        for triangle_index in range(1, triangulation.NbTriangles() + 1):
            n1, n2, n3 = triangulation.Triangle(triangle_index).Get()
            if reversed_face:
                n2, n3 = n3, n2
            indices.extend([n1 - 1, n2 - 1, n3 - 1])

        props = GProp_GProps()
        brepgprop.SurfaceProperties(face, props)
        center = props.CentreOfMass()
        identity = entity_info(reader, face)
        entity_id = identity.get("step_entity_id")
        payload_faces.append(
            {
                "faceId": "{}:face:{}".format(revision_id, face_index),
                "faceIndex": face_index,
                "freecadFace": "Face{}".format(face_index),
                "stepEntityId": entity_id,
                "stepEntity": "#{}".format(entity_id) if entity_id else None,
                "entityType": identity.get("entity_type"),
                "surfaceType": surface_type(face),
                "area": _finite(props.Mass()),
                "centroid": [_finite(center.X()), _finite(center.Y()), _finite(center.Z())],
                "positions": positions,
                "indices": indices,
            }
        )
    return {
        "revisionId": revision_id,
        "faceCount": len(payload_faces),
        "stepEntityIdSource": "part21-ident-label/all-transfer-levels/v2",
        "faces": payload_faces,
    }


def local_observation(mesh: dict[str, Any], selected_ids: set[str]) -> dict[str, Any]:
    selected = [face for face in mesh["faces"] if face["faceId"] in selected_ids]
    return {
        "schema": "face_local_observation_v1",
        "revisionId": mesh["revisionId"],
        "selectedFaces": [
            {
                key: face[key]
                for key in (
                    "faceId",
                    "faceIndex",
                    "stepEntityId",
                    "surfaceType",
                    "area",
                    "centroid",
                )
            }
            for face in selected
        ],
    }
