from __future__ import annotations

import colorsys
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

PALETTE = [
    "#ef4444", "#3b82f6", "#22c55e", "#f97316", "#a855f7",
    "#06b6d4", "#eab308", "#ec4899", "#14b8a6", "#6366f1",
    "#84cc16", "#f43f5e", "#0ea5e9", "#d946ef", "#10b981",
    "#f59e0b", "#8b5cf6", "#0891b2", "#65a30d", "#e11d48",
]
VIEWS = (
    ("Isometric", (1.0, 1.0, 0.8), (0.0, 0.0, 1.0)),
    ("Front", (0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    ("Right", (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    ("Top", (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
)


def _unit(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(value * value for value in vector))
    return tuple(value / length for value in vector)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _color(index: int) -> str:
    if index < len(PALETTE):
        return PALETTE[index]
    hue = (index * 0.618033988749895) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.72, 0.92)
    return "#{:02x}{:02x}{:02x}".format(int(red * 255), int(green * 255), int(blue * 255))


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[offset:offset + 2], 16) for offset in (0, 2, 4))


def _shade(color, points):
    a, b, c = points
    normal = _cross(
        (b[0] - a[0], b[1] - a[1], b[2] - a[2]),
        (c[0] - a[0], c[1] - a[1], c[2] - a[2]),
    )
    length = math.sqrt(max(_dot(normal, normal), 1e-18))
    light = _unit((0.35, -0.45, 1.0))
    intensity = 0.58 + 0.42 * abs(_dot(tuple(value / length for value in normal), light))
    return tuple(max(0, min(255, int(channel * intensity))) for channel in color)


def _boundary_edges(indices):
    counts = {}
    for offset in range(0, len(indices), 3):
        triangle = [int(indices[offset + index]) for index in range(3)]
        for a, b in ((triangle[0], triangle[1]), (triangle[1], triangle[2]), (triangle[2], triangle[0])):
            edge = (min(a, b), max(a, b))
            counts[edge] = counts.get(edge, 0) + 1
    return [edge for edge, count in counts.items() if count == 1]


def _projector(direction, up_hint, points, left, top, width, height):
    forward = _unit(direction)
    right = _unit(_cross(up_hint, forward))
    up = _unit(_cross(forward, right))
    projected = [(_dot(point, right), _dot(point, up), _dot(point, forward)) for point in points]
    min_x = min(point[0] for point in projected)
    max_x = max(point[0] for point in projected)
    min_y = min(point[1] for point in projected)
    max_y = max(point[1] for point in projected)
    span_x = max(max_x - min_x, 1e-9)
    span_y = max(max_y - min_y, 1e-9)
    scale = min((width - 36) / span_x, (height - 54) / span_y)
    offset_x = left + width / 2 - scale * (min_x + max_x) / 2
    offset_y = top + height / 2 + scale * (min_y + max_y) / 2

    def project(point):
        x, y, depth = _dot(point, right), _dot(point, up), _dot(point, forward)
        return (offset_x + x * scale, offset_y - y * scale, depth)

    return project


def render_fillet_components(
    mesh_path: Path,
    component_report: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    mesh = json.loads(mesh_path.read_text(encoding="utf-8"))
    components = component_report.get("components") or []
    if not components:
        raise ValueError("no fillet components are available for visual analysis")

    faces_by_step: dict[int, dict] = {}
    for face in mesh.get("faces") or []:
        step_id = face.get("stepEntityId")
        if step_id is not None:
            step_id = int(step_id)
            if step_id in faces_by_step:
                raise ValueError("mesh contains duplicate STEP face identities")
            faces_by_step[step_id] = face

    component_by_step: dict[int, str] = {}
    legend: list[dict[str, str]] = []
    component_ids = [str(component.get("component_id") or "") for component in components]
    if any(not component_id for component_id in component_ids) or len(component_ids) != len(set(component_ids)):
        raise ValueError("component report contains missing or duplicate component IDs")
    for index, component in enumerate(components):
        component_id = component_ids[index]
        color = _color(index)
        legend.append({"component_id": component_id, "color": color})
        for raw_step_id in component.get("step_entity_ids") or []:
            step_id = int(raw_step_id)
            if step_id not in faces_by_step:
                raise ValueError("component {} contains an unmapped STEP face {}".format(component_id, step_id))
            if step_id in component_by_step:
                raise ValueError("one STEP face belongs to multiple components")
            component_by_step[step_id] = component_id

    color_by_component = {item["component_id"]: item["color"] for item in legend}
    all_points = []
    face_rows = []
    for face in mesh.get("faces") or []:
        values = face.get("positions") or []
        points = [tuple(float(values[i + axis]) for axis in range(3)) for i in range(0, len(values), 3)]
        if not points:
            continue
        all_points.extend(points)
        face_rows.append((face, points))
    if not all_points:
        raise ValueError("accepted revision mesh contains no renderable triangles")

    panel_width, panel_height = 480, 380
    legend_width = 260
    image = Image.new("RGB", (panel_width * 2 + legend_width, panel_height * 2), "#f5f7fa")
    draw = ImageDraw.Draw(image)
    for view_index, (title, direction, up_hint) in enumerate(VIEWS):
        left = (view_index % 2) * panel_width
        top = (view_index // 2) * panel_height
        draw.rectangle((left, top, left + panel_width - 1, top + panel_height - 1), outline="#cbd5e1")
        draw.text((left + 10, top + 8), title, fill="#172033")
        project = _projector(direction, up_hint, all_points, left, top + 18, panel_width, panel_height - 18)
        triangles = []
        face_boundaries = []
        for face, points in face_rows:
            step_id = face.get("stepEntityId")
            component_id = component_by_step.get(int(step_id)) if step_id is not None else None
            base_fill = _rgb(color_by_component[component_id]) if component_id else (205, 211, 220)
            indices = face.get("indices") or []
            for i in range(0, len(indices), 3):
                source = [points[int(indices[i + offset])] for offset in range(3)]
                triangle = [project(point) for point in source]
                triangles.append((
                    sum(point[2] for point in triangle) / 3.0,
                    triangle,
                    _shade(base_fill, source),
                ))
            face_boundaries.append((
                points,
                _boundary_edges(indices),
                "#5b2b23" if component_id else "#778394",
            ))
        triangles.sort(key=lambda item: item[0])
        for _depth, triangle, fill in triangles:
            draw.polygon([(point[0], point[1]) for point in triangle], fill=fill)
        for points, edges, edge_color in face_boundaries:
            for a, b in edges:
                start, end = project(points[a]), project(points[b])
                draw.line(((start[0], start[1]), (end[0], end[1])), fill=edge_color, width=1)

    legend_left = panel_width * 2
    draw.rectangle((legend_left, 0, image.width - 1, image.height - 1), fill="#ffffff", outline="#cbd5e1")
    draw.text((legend_left + 18, 18), "Fillet components", fill="#172033")
    rows_per_column = max(1, (image.height - 58) // 23)
    columns = max(1, math.ceil(len(legend) / rows_per_column))
    column_width = max(95, (legend_width - 28) // columns)
    for index, item in enumerate(legend):
        column = index // rows_per_column
        row = index % rows_per_column
        x = legend_left + 14 + column * column_width
        y = 48 + row * 23
        draw.rectangle((x, y, x + 16, y + 14), fill=item["color"], outline="#334155")
        draw.text((x + 21, y + 1), item["component_id"], fill="#172033")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    return {
        "schema": "fillet_components_visual_analysis_v1",
        "revision_id": component_report.get("revision_id"),
        "component_count": len(components),
        "legend": legend,
        "views": [item[0] for item in VIEWS],
        "image_width": image.width,
        "image_height": image.height,
    }
