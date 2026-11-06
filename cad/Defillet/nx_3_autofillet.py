# -*- coding: utf-8 -*-
r"""NX command-line journal: STEP -> fillet named STEP edges -> export STEP.

STEP edge name example:
    #593 = EDGE_CURVE('{"component_id":1,"radius":0.5}',#594,#518,#596,.T.);

Semantics:
- Edges with JSON names containing component_id and radius are fillet targets.
- One component_id is committed as one EdgeBlend feature (with bisect retry on overlap).
- Inside the same component_id, edges are grouped by radius and added as multiple chainsets.

Example:
    run_journal.exe cad\Defillet\nx_3_autofillet.py -args --input "D:\in.step" --output "D:\out.step"
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from collections import OrderedDict
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
DEFAULT_FEATURE_NAME = "UNPARAMETERIZED_FEATURE(1)"

BLEND_OVERLAP_KEYWORDS = ("Blend faces overlap",)
EDGE_MATCH_TOLERANCE = 1.0e-3
TRIM_REBIND_TOLERANCE = 5.0e-2
STEP_EDGE_NAME_BY_TAG = {}

_NUMBER_PATTERN = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
_JOURNAL_POINT_RE = re.compile(
    r"\((" + _NUMBER_PATTERN + r"),(" + _NUMBER_PATTERN + r"),(" + _NUMBER_PATTERN + r")\)"
)
_FEATURE_NAME_RE = re.compile(r"([A-Za-z_]+_FEATURE\(\d+\))")


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="STEP file to open")
    parser.add_argument("--output", required=True, help="STEP file to export")
    parser.add_argument("--part-output", default="", help="Optional NX .prt file to save before STEP export")
    parser.add_argument("--settings-file", default=DEFAULT_STEP214_DEF, help="STEP214 .def settings file")
    parser.add_argument("--feature-name", default=DEFAULT_FEATURE_NAME, help="Imported BREP feature name")
    parser.add_argument("--tolerance", type=float, default=0.01)
    parser.add_argument("--angle-tol", type=float, default=0.5)
    parser.add_argument("--keep-open", action="store_true", help="Keep the imported part open after export")
    parser.add_argument("--no-cleanup", action="store_true", help="Do not delete STEP translator log/prt temp files")
    parser.add_argument(
        "--keep-failed-fillet",
        dest="keep_failed_fillet",
        action="store_true",
        default=False,
        help="Keep failed autofillet features and rename them for debugging",
    )
    parser.add_argument(
        "--no-keep-failed-fillet",
        dest="keep_failed_fillet",
        action="store_false",
        help="Undo failed autofillet features instead of keeping them",
    )
    return parser.parse_args(argv)


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
        print(text, flush=True)
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
            log("Warning: CloseAll before autofillet failed: {}".format(safe_text(exc)))
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


def activate_work_part(session, work_part):
    load_status = None
    try:
        _status, load_status = session.Parts.SetActiveDisplay(
            work_part,
            NXOpen.DisplayPartOption.ReplaceExisting,
            NXOpen.PartDisplayPartWorkPartOption.SameAsDisplay,
        )
    except Exception:
        try:
            session.Parts.SetWork(work_part)
        except Exception:
            pass
    finally:
        if load_status is not None:
            try:
                load_status.Dispose()
            except Exception:
                pass

    try:
        session.Parts.SetWork(work_part)
    except Exception:
        pass

    session.ApplicationSwitchImmediate("UG_APP_MODELING")


def close_probe_part(session, probe_part, main_part):
    try:
        probe_part.Close(
            NXOpen.BasePart.CloseWholeTree.FalseValue,
            NXOpen.BasePart.CloseModified.CloseModified,
            None,
        )
    finally:
        if main_part is not None:
            activate_work_part(session, main_part)


def cleanup_temp_files(input_path, output_path):
    candidates = [
        os.path.splitext(input_path)[0] + ".log",
        os.path.splitext(input_path)[0] + "_step.prt",
        os.path.splitext(output_path)[0] + ".log",
        os.path.splitext(output_path)[0] + "_step.prt",
    ]
    candidates.extend(glob.glob(os.path.splitext(input_path)[0] + "_step_*.prt"))
    candidates.extend(glob.glob(os.path.splitext(output_path)[0] + "_step_*.prt"))

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
            or lower_name == input_stem + "_step.prt"
            or (
                lower_name.startswith(input_stem + "_step_")
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


def dispose_status(status):
    if status is None:
        return
    try:
        status.Dispose()
    except Exception:
        pass


def part_body_count(part):
    try:
        return len(list(part.Bodies))
    except Exception:
        return 0


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


def component_position_transform(component):
    if component is None:
        return None
    try:
        origin, matrix = component.GetPosition()
    except Exception:
        return None
    origin_tuple = point_tuple(origin)
    if origin_tuple is None:
        return None
    return {
        "origin": origin_tuple,
        "matrices": (
            (
                (float(matrix.Xx), float(matrix.Yx), float(matrix.Zx)),
                (float(matrix.Xy), float(matrix.Yy), float(matrix.Zy)),
                (float(matrix.Xz), float(matrix.Yz), float(matrix.Zz)),
            ),
            (
                (float(matrix.Xx), float(matrix.Xy), float(matrix.Xz)),
                (float(matrix.Yx), float(matrix.Yy), float(matrix.Yz)),
                (float(matrix.Zx), float(matrix.Zy), float(matrix.Zz)),
            ),
        ),
    }


def transform_point_tuple_candidates(point, transform):
    if point is None or transform is None:
        return []
    origin = transform.get("origin")
    matrices = transform.get("matrices") or ()
    if origin is None or not matrices:
        return []

    candidates = []
    for matrix in matrices:
        candidates.append((
            origin[0] + matrix[0][0] * point[0] + matrix[0][1] * point[1] + matrix[0][2] * point[2],
            origin[1] + matrix[1][0] * point[0] + matrix[1][1] * point[1] + matrix[1][2] * point[2],
            origin[2] + matrix[2][0] * point[0] + matrix[2][1] * point[1] + matrix[2][2] * point[2],
        ))
    return candidates


def iter_component_tree(component):
    if component is None:
        return
    for child in list(safe_call(lambda: component.GetChildren(), []) or []):
        yield child
        for grandchild in iter_component_tree(child):
            yield grandchild


def collect_autofillet_contexts(display_part):
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
                "transform": component_position_transform(component),
            }
        )

    add_context(display_part, component=None)

    root_component = safe_call(lambda: display_part.ComponentAssembly.RootComponent, None)
    for component in iter_component_tree(root_component):
        prototype = safe_call(lambda: component.Prototype, None)
        add_context(prototype, component=component)

    return contexts


def activate_autofillet_context(session, context):
    part = context.get("part")
    if part is not None:
        activate_work_part(session, part)
        return session.Parts.Work

    return None


def parse_step_edge_curve_names(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            text = fp.read()
    except Exception:
        return []

    names = []
    pattern = re.compile(
        r"#\s*\d+\s*=\s*EDGE_CURVE\s*\(\s*'((?:[^']|'')*)'",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        names.append(match.group(1).replace("''", "'"))
    return names


def parse_step_point_tuple(text):
    values = []
    for item in safe_text(text).split(","):
        try:
            values.append(float(item.strip()))
        except Exception:
            return None
    if len(values) != 3:
        return None
    return tuple(values)


def step_entity_map(text, type_name):
    entities = OrderedDict()
    pattern = re.compile(
        r"#\s*(\d+)\s*=\s*" + re.escape(type_name) + r"\s*\(([^;]*)\);",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        entities[match.group(1)] = match.group(2)
    return entities


def step_refs(text):
    return re.findall(r"#\s*(\d+)", safe_text(text))


def parse_step_named_edge_specs(path, brep_index=None):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            text = fp.read()
    except Exception:
        return []

    cartesian_points = {}
    for match in re.finditer(
            r"#\s*(\d+)\s*=\s*CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(([^)]*)\)",
            text,
            re.IGNORECASE | re.DOTALL,
    ):
        point = parse_step_point_tuple(match.group(2))
        if point is not None:
            cartesian_points[match.group(1)] = point

    vertex_points = {}
    for match in re.finditer(
            r"#\s*(\d+)\s*=\s*VERTEX_POINT\s*\(\s*'[^']*'\s*,\s*#\s*(\d+)\s*\)",
            text,
            re.IGNORECASE | re.DOTALL,
    ):
        point = cartesian_points.get(match.group(2))
        if point is not None:
            vertex_points[match.group(1)] = point

    allowed_edge_ids = None
    if brep_index is not None:
        brep_entities = step_entity_map(text, "ADVANCED_BREP_SHAPE_REPRESENTATION")
        manifold_entities = step_entity_map(text, "MANIFOLD_SOLID_BREP")
        shell_entities = step_entity_map(text, "CLOSED_SHELL")
        face_entities = step_entity_map(text, "ADVANCED_FACE")
        bound_entities = step_entity_map(text, "FACE_BOUND")
        outer_bound_entities = step_entity_map(text, "FACE_OUTER_BOUND")
        loop_entities = step_entity_map(text, "EDGE_LOOP")
        oriented_edge_entities = step_entity_map(text, "ORIENTED_EDGE")

        brep_ids = list(brep_entities.keys())
        if 1 <= int(brep_index) <= len(brep_ids):
            brep_body = brep_entities[brep_ids[int(brep_index) - 1]]
            manifold_ids = [ref for ref in step_refs(brep_body) if ref in manifold_entities]
            shell_ids = []
            for manifold_id in manifold_ids:
                shell_ids.extend(ref for ref in step_refs(manifold_entities[manifold_id]) if ref in shell_entities)

            face_ids = []
            for shell_id in shell_ids:
                face_ids.extend(ref for ref in step_refs(shell_entities[shell_id]) if ref in face_entities)

            loop_ids = []
            for face_id in face_ids:
                for ref in step_refs(face_entities[face_id]):
                    if ref in bound_entities:
                        loop_ids.extend(r for r in step_refs(bound_entities[ref]) if r in loop_entities)
                    elif ref in outer_bound_entities:
                        loop_ids.extend(r for r in step_refs(outer_bound_entities[ref]) if r in loop_entities)

            oriented_edge_ids = []
            for loop_id in loop_ids:
                oriented_edge_ids.extend(ref for ref in step_refs(loop_entities[loop_id]) if ref in oriented_edge_entities)

            allowed_edge_ids = set()
            for oriented_edge_id in oriented_edge_ids:
                for ref in step_refs(oriented_edge_entities[oriented_edge_id]):
                    if ref not in oriented_edge_entities:
                        allowed_edge_ids.add(ref)

    specs = []
    for edge_order, match in enumerate(
            re.finditer(
                r"#\s*(\d+)\s*=\s*EDGE_CURVE\s*\(\s*'((?:[^']|'')*)'\s*,\s*#\s*(\d+)\s*,\s*#\s*(\d+)\s*,",
                text,
                re.IGNORECASE | re.DOTALL,
            ),
            start=1,
    ):
        edge_name = match.group(2).replace("''", "'")
        step_edge_id = match.group(1)
        if allowed_edge_ids is not None and step_edge_id not in allowed_edge_ids:
            continue
        edge_label = parse_fillet_edge_name(edge_name)
        if edge_label is None:
            continue
        point_a = vertex_points.get(match.group(3))
        point_b = vertex_points.get(match.group(4))
        if point_a is None or point_b is None:
            continue
        specs.append(
            {
                "step_edge_id": step_edge_id,
                "edge_order": edge_order,
                "name": edge_name,
                "point_a": point_a,
                "point_b": point_b,
                "length": vector_length_between(point_a, point_b),
                "component_id": edge_label["component_id"],
                "radius": edge_label["radius"],
            }
        )

    return specs


def step_edge_geometry_match_score(edge, step_spec, tolerance, transform=None):
    spec_point_a = step_spec.get("point_a")
    spec_point_b = step_spec.get("point_b")

    candidates = []
    point_a, point_b = edge_endpoint_tuples(edge)
    if point_a is not None and point_b is not None:
        candidates.append((point_a, point_b, "vertex"))
        transformed_as = transform_point_tuple_candidates(point_a, transform)
        transformed_bs = transform_point_tuple_candidates(point_b, transform)
        for transformed_index, (transformed_a, transformed_b) in enumerate(zip(transformed_as, transformed_bs), start=1):
            candidates.append((transformed_a, transformed_b, "vertex_transform{}".format(transformed_index)))

    journal_points = journal_identifier_points(safe_edge_journal_identifier(edge))
    jid_point_a, jid_point_b = longest_journal_segment_endpoints(journal_points)
    if jid_point_a is not None and jid_point_b is not None:
        candidates.append((jid_point_a, jid_point_b, "journal"))
        transformed_as = transform_point_tuple_candidates(jid_point_a, transform)
        transformed_bs = transform_point_tuple_candidates(jid_point_b, transform)
        for transformed_index, (transformed_a, transformed_b) in enumerate(zip(transformed_as, transformed_bs), start=1):
            candidates.append((transformed_a, transformed_b, "journal_transform{}".format(transformed_index)))

    if not candidates:
        return None

    scores = []
    for point_a, point_b, source in candidates:
        if not unordered_endpoints_close(point_a, point_b, spec_point_a, spec_point_b, tolerance):
            continue
        direct_error = distance_squared(point_a, spec_point_a) + distance_squared(point_b, spec_point_b)
        reverse_error = distance_squared(point_a, spec_point_b) + distance_squared(point_b, spec_point_a)
        source_rank = 0 if source == "vertex" else 1
        scores.append((min(direct_error, reverse_error), source_rank, safe_edge_tag(edge)))

    if not scores:
        return None

    return sorted(scores)[0]


def context_edge_records(contexts):
    records = []
    for context_index, context in enumerate(contexts, start=1):
        part = context.get("part")
        for body_index, body in enumerate(list(safe_call(lambda: part.Bodies, []) or []), start=1):
            for edge_index, edge in enumerate(list(safe_call(lambda: body.GetEdges(), []) or []), start=1):
                records.append(
                    {
                        "context_index": context_index,
                        "body_index": body_index,
                        "edge_index": edge_index,
                        "edge": edge,
                        "tag": safe_edge_tag(edge),
                        "transform": context.get("transform"),
                    }
                )
    return records


def assign_step_edge_names_to_nx_edges(
        contexts,
        input_path,
        log=None,
        step_edge_offset=0,
        brep_index=None,
):
    global STEP_EDGE_NAME_BY_TAG
    STEP_EDGE_NAME_BY_TAG = {}

    step_edge_names = parse_step_edge_curve_names(input_path)
    step_edge_specs = parse_step_named_edge_specs(input_path, brep_index=brep_index)
    if not step_edge_names:
        if log:
            log("No EDGE_CURVE names found in STEP text fallback.")
        return 0, 0, 0

    edge_records = context_edge_records(contexts)
    used_tags = set()
    assigned_count = 0
    order_assigned_count = 0
    target_count = len(step_edge_specs)
    native_target_count = 0
    for record in edge_records:
        native_name = safe_text(safe_call(lambda edge=record["edge"]: edge.Name, ""))
        if parse_fillet_edge_name(native_name) is not None:
            native_target_count += 1

    for step_spec in step_edge_specs:
        matches = []
        for record in edge_records:
            edge_tag = record["tag"]
            if not edge_tag or edge_tag in used_tags:
                continue
            score = step_edge_geometry_match_score(
                record["edge"],
                step_spec,
                max(EDGE_MATCH_TOLERANCE, TRIM_REBIND_TOLERANCE),
                transform=record.get("transform"),
            )
            if score is None:
                continue
            matches.append((score, record))

        if not matches:
            continue

        matches.sort(key=lambda item: item[0])
        record = matches[0][1]
        STEP_EDGE_NAME_BY_TAG[record["tag"]] = step_spec["name"]
        used_tags.add(record["tag"])
        assigned_count += 1

    if log:
        log(
            "STEP edge-name fallback: step_edges={}, named_step_edges={}, nx_edges_seen={}, native_named={}, geometry_assigned={}, order_assigned={}, target_labels={}, step_offset={}, brep_index={}".format(
                len(step_edge_names),
                len(step_edge_specs),
                len(edge_records),
                native_target_count,
                assigned_count,
                order_assigned_count,
                target_count,
                step_edge_offset,
                safe_text(brep_index),
            )
        )

    return len(edge_records), assigned_count + order_assigned_count, target_count


def silent_cleanup_step_temp_files(step_path):
    candidates = [
        step_path,
        os.path.splitext(step_path)[0] + ".log",
        os.path.splitext(step_path)[0] + "_step.prt",
    ]
    candidates.extend(glob.glob(os.path.splitext(step_path)[0] + "_step_*.prt"))

    for path in candidates:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


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
        active_exception = sys.exc_info()[0] is not None
        try:
            creator.Destroy()
        except Exception:
            if not active_exception:
                raise


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


def safe_edge_name(edge):
    tag = safe_edge_tag(edge)
    if tag in STEP_EDGE_NAME_BY_TAG:
        return STEP_EDGE_NAME_BY_TAG[tag]
    return safe_text(safe_call(lambda: edge.Name, ""))


def safe_edge_journal_identifier(edge):
    return safe_text(safe_call(lambda: edge.JournalIdentifier, ""))


def safe_edge_tag(edge):
    return safe_text(safe_call(lambda: edge.Tag, ""))


def point_tuple(point):
    try:
        return (float(point.X), float(point.Y), float(point.Z))
    except Exception:
        return None


def distance_squared(point_a, point_b):
    return (
        (point_a[0] - point_b[0]) * (point_a[0] - point_b[0])
        + (point_a[1] - point_b[1]) * (point_a[1] - point_b[1])
        + (point_a[2] - point_b[2]) * (point_a[2] - point_b[2])
    )


def points_close(point_a, point_b, tolerance):
    if point_a is None or point_b is None:
        return False
    return distance_squared(point_a, point_b) <= tolerance * tolerance


def vector_length_between(point_a, point_b):
    if point_a is None or point_b is None:
        return 0.0
    return distance_squared(point_a, point_b) ** 0.5


def vector_between(point_a, point_b):
    return (
        point_b[0] - point_a[0],
        point_b[1] - point_a[1],
        point_b[2] - point_a[2],
    )


def vector_dot(vector_a, vector_b):
    return (
        vector_a[0] * vector_b[0]
        + vector_a[1] * vector_b[1]
        + vector_a[2] * vector_b[2]
    )


def vector_cross(vector_a, vector_b):
    return (
        vector_a[1] * vector_b[2] - vector_a[2] * vector_b[1],
        vector_a[2] * vector_b[0] - vector_a[0] * vector_b[2],
        vector_a[0] * vector_b[1] - vector_a[1] * vector_b[0],
    )


def vector_length(vector):
    return (
        vector[0] * vector[0]
        + vector[1] * vector[1]
        + vector[2] * vector[2]
    ) ** 0.5


def normalize_vector(vector):
    length = vector_length(vector)
    if length <= 1.0e-12:
        return None
    return (
        vector[0] / length,
        vector[1] / length,
        vector[2] / length,
    )


def distance_point_to_line(point, line_point, line_dir_unit):
    return vector_length(vector_cross(vector_between(line_point, point), line_dir_unit))


def edge_vertices(edge):
    try:
        vertex_a, vertex_b = edge.GetVertices()
        return vertex_a, vertex_b
    except Exception:
        return None, None


def edge_endpoint_tuples(edge):
    vertex_a, vertex_b = edge_vertices(edge)
    return point_tuple(vertex_a), point_tuple(vertex_b)


def edge_midpoint_from_points(point_a, point_b):
    if point_a is None or point_b is None:
        return None
    return (
        (point_a[0] + point_b[0]) * 0.5,
        (point_a[1] + point_b[1]) * 0.5,
        (point_a[2] + point_b[2]) * 0.5,
    )


def journal_identifier_points(journal_identifier):
    points = []
    for match in _JOURNAL_POINT_RE.finditer(safe_text(journal_identifier)):
        try:
            points.append((
                float(match.group(1)),
                float(match.group(2)),
                float(match.group(3)),
            ))
        except Exception:
            pass
    return points


def journal_identifier_feature_name(journal_identifier):
    matches = _FEATURE_NAME_RE.findall(safe_text(journal_identifier))
    if not matches:
        return ""
    return matches[-1]


def longest_journal_segment_endpoints(points):
    if len(points) < 2:
        return None, None

    best_a = None
    best_b = None
    best_length = -1.0
    for index_a in range(len(points)):
        for index_b in range(index_a + 1, len(points)):
            length = vector_length_between(points[index_a], points[index_b])
            if length > best_length:
                best_length = length
                best_a = points[index_a]
                best_b = points[index_b]
    return best_a, best_b


def edge_type_name(edge):
    text = safe_text(safe_call(lambda: edge.SolidEdgeType, ""))
    if "." in text:
        return text.rsplit(".", 1)[-1]
    return text


def edge_type_is_linear(edge_or_type):
    if hasattr(edge_or_type, "SolidEdgeType"):
        text = edge_type_name(edge_or_type)
    else:
        text = safe_text(edge_or_type)
        if "." in text:
            text = text.rsplit(".", 1)[-1]
    return text.lower() in ("linear", "line", "1")


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
    return vector_length_between(point_a, point_b)


def make_edge_spec(edge):
    point_a, point_b = edge_endpoint_tuples(edge)
    journal_identifier = safe_edge_journal_identifier(edge)
    journal_points = journal_identifier_points(journal_identifier)
    segment_point_a, segment_point_b = point_a, point_b
    jid_point_a, jid_point_b = longest_journal_segment_endpoints(journal_points)
    if jid_point_a is not None and jid_point_b is not None:
        segment_point_a, segment_point_b = jid_point_a, jid_point_b
    return {
        "edge": edge,
        "tag": safe_edge_tag(edge),
        "journal_identifier": journal_identifier,
        "base_feature_name": journal_identifier_feature_name(journal_identifier),
        "name": safe_edge_name(edge),
        "edge_type": edge_type_name(edge),
        "length": safe_edge_curve_length(edge),
        "point_a": point_a,
        "point_b": point_b,
        "midpoint": edge_midpoint_from_points(point_a, point_b),
        "segment_point_a": segment_point_a,
        "segment_point_b": segment_point_b,
        "journal_points": journal_points,
    }


def current_body_edges(work_part):
    edges = []
    for body in work_part.Bodies:
        try:
            edges.extend(list(body.GetEdges()))
        except Exception:
            pass
    return edges


def current_body_edge_index(work_part):
    edges = current_body_edges(work_part)
    edge_by_tag = OrderedDict()
    for edge in edges:
        edge_tag = safe_edge_tag(edge)
        if edge_tag and edge_tag not in edge_by_tag:
            edge_by_tag[edge_tag] = edge
    return edges, edge_by_tag


def nxobject_from_tag(tag):
    if tag is None:
        return None
    return safe_call(lambda: NXOpen.Utilities.NXObjectManager.Get(tag), None)


def tag_from_text(tag_text):
    text = safe_text(tag_text).strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def unordered_endpoints_close(point_a, point_b, other_point_a, other_point_b, tolerance):
    return (
        points_close(point_a, other_point_a, tolerance)
        and points_close(point_b, other_point_b, tolerance)
    ) or (
        points_close(point_a, other_point_b, tolerance)
        and points_close(point_b, other_point_a, tolerance)
    )


def current_edge_matches_spec(edge, spec, tolerance=EDGE_MATCH_TOLERANCE):
    point_a, point_b = edge_endpoint_tuples(edge)
    spec_point_a = spec.get("point_a")
    spec_point_b = spec.get("point_b")
    if not unordered_endpoints_close(point_a, point_b, spec_point_a, spec_point_b, tolerance):
        return False

    spec_type = safe_text(spec.get("edge_type"))
    if spec_type and edge_type_name(edge) != spec_type:
        return False

    spec_length = spec.get("length")
    edge_length = safe_edge_curve_length(edge)
    try:
        if abs(edge_length - float(spec_length)) > max(tolerance, abs(float(spec_length)) * 1.0e-4):
            return False
    except Exception:
        pass

    return True


def usable_edge_object(obj):
    if obj is None:
        return False
    if safe_call(lambda: obj.JournalIdentifier, None) is None:
        return False
    point_a, point_b = edge_endpoint_tuples(obj)
    if point_a is None or point_b is None:
        return False
    return True


def current_edge_from_object(obj, edge_by_tag, used_tags):
    edge_tag = safe_edge_tag(obj)
    if not edge_tag or edge_tag in used_tags:
        return None
    current_edge = edge_by_tag.get(edge_tag)
    if current_edge is not None:
        return current_edge
    if usable_edge_object(obj):
        return obj
    return None


def feature_name_candidates(spec):
    candidates = []
    for value in (
            spec.get("base_feature_name"),
            journal_identifier_feature_name(spec.get("journal_identifier")),
            DEFAULT_FEATURE_NAME,
    ):
        value = safe_text(value)
        if value and value not in candidates:
            candidates.append(value)
    return candidates


def find_edge_by_journal_identifier(work_part, spec, edge_by_tag, used_tags):
    journal_identifier = safe_text(spec.get("journal_identifier"))
    if not journal_identifier:
        return None

    for feature_name in feature_name_candidates(spec):
        feature = safe_call(lambda name=feature_name: work_part.Features.FindObject(name), None)
        if feature is None:
            continue

        obj = safe_call(lambda feature=feature: feature.FindObject(journal_identifier), None)
        edge = current_edge_from_object(obj, edge_by_tag, used_tags)
        if edge is not None:
            return edge

    obj = safe_call(lambda: work_part.FindObject(journal_identifier), None)
    return current_edge_from_object(obj, edge_by_tag, used_tags)


def find_alive_edge_by_tag(spec, edge_by_tag, used_tags):
    old_tag = tag_from_text(spec.get("tag"))
    if old_tag is None:
        return None

    direct_edge = current_edge_from_object(nxobject_from_tag(old_tag), edge_by_tag, used_tags)
    if direct_edge is not None:
        return direct_edge

    uf_session = safe_call(lambda: NXOpen.UF.UFSession.GetUFSession(), None)
    if uf_session is None:
        return None

    alive_tag = safe_call(lambda: uf_session.Modl.AskAliveEdge(old_tag), None)
    if isinstance(alive_tag, tuple) and alive_tag:
        alive_tag = alive_tag[-1]
    if alive_tag is None:
        return None

    return current_edge_from_object(nxobject_from_tag(alive_tag), edge_by_tag, used_tags)


def projection_on_line(point, line_point, line_dir_unit):
    return vector_dot(vector_between(line_point, point), line_dir_unit)


def line_trimmed_edge_match_score(edge, spec, tolerance=TRIM_REBIND_TOLERANCE):
    spec_type = safe_text(spec.get("edge_type"))
    if spec_type and not edge_type_is_linear(spec_type):
        return None
    if not edge_type_is_linear(edge):
        return None

    spec_point_a = spec.get("segment_point_a") or spec.get("point_a")
    spec_point_b = spec.get("segment_point_b") or spec.get("point_b")
    if spec_point_a is None or spec_point_b is None:
        return None

    spec_vector = vector_between(spec_point_a, spec_point_b)
    spec_length = vector_length(spec_vector)
    spec_dir = normalize_vector(spec_vector)
    if spec_dir is None or spec_length <= max(tolerance, 1.0e-6):
        return None

    point_a, point_b = edge_endpoint_tuples(edge)
    if point_a is None or point_b is None:
        return None

    midpoint = edge_midpoint_from_points(point_a, point_b)
    distances = [
        distance_point_to_line(point_a, spec_point_a, spec_dir),
        distance_point_to_line(point_b, spec_point_a, spec_dir),
    ]
    if midpoint is not None:
        distances.append(distance_point_to_line(midpoint, spec_point_a, spec_dir))

    max_distance = max(distances)
    if max_distance > tolerance:
        return None

    param_a = projection_on_line(point_a, spec_point_a, spec_dir)
    param_b = projection_on_line(point_b, spec_point_a, spec_dir)
    param_min = min(param_a, param_b)
    param_max = max(param_a, param_b)

    if param_min < -tolerance or param_max > spec_length + tolerance:
        return None

    overlap = min(param_max, spec_length) - max(param_min, 0.0)
    if overlap <= max(tolerance * 0.25, 1.0e-4):
        return None

    edge_length = safe_edge_curve_length(edge)
    if edge_length <= max(tolerance * 0.25, 1.0e-4):
        return None

    interval_error = abs(edge_length - (param_max - param_min))
    midpoint_param = (param_min + param_max) * 0.5
    spec_midpoint = spec.get("midpoint") or edge_midpoint_from_points(spec_point_a, spec_point_b)
    spec_midpoint_param = (
        projection_on_line(spec_midpoint, spec_point_a, spec_dir)
        if spec_midpoint is not None else spec_length * 0.5
    )
    midpoint_error = abs(midpoint_param - spec_midpoint_param)

    # Prefer a well-aligned longer residual segment; midpoint is only a tie breaker.
    return (
        max_distance,
        interval_error,
        -overlap,
        midpoint_error,
        safe_edge_tag(edge),
    )


def find_trimmed_edge_matches(work_part, spec, current_edges, edge_by_tag, used_tags):
    matches = []
    for edge in current_edges:
        edge_tag = safe_edge_tag(edge)
        if edge_tag and edge_tag in used_tags:
            continue
        score = line_trimmed_edge_match_score(edge, spec)
        if score is None:
            continue
        matches.append((score, edge))

    if not matches:
        return []

    matches.sort(key=lambda item: item[0])
    best_score = matches[0][0]
    best_edges = [matches[0][1]]

    # If NX split the original target into several surviving line fragments,
    # keep all clear fragments on that original interval instead of silently
    # dropping the shorter remainder.
    for score, edge in matches[1:]:
        if score[0] > max(TRIM_REBIND_TOLERANCE, best_score[0] + TRIM_REBIND_TOLERANCE):
            continue
        best_edges.append(edge)

    resolved = []
    for edge in best_edges:
        current_edge = current_edge_from_object(edge, edge_by_tag, used_tags)
        if current_edge is not None:
            resolved.append(current_edge)
    return resolved


def mark_edges_used(edges, used_tags):
    for edge in edges:
        edge_tag = safe_edge_tag(edge)
        if edge_tag:
            used_tags.add(edge_tag)


def resolve_edge_spec(work_part, spec, used_tags=None, current_edges=None, edge_by_tag=None):
    if hasattr(spec, "JournalIdentifier"):
        return [spec]

    if not isinstance(spec, dict):
        return [spec]

    used_tags = used_tags if used_tags is not None else set()
    if current_edges is None or edge_by_tag is None:
        current_edges, edge_by_tag = current_body_edge_index(work_part)

    alive_edge = find_alive_edge_by_tag(spec, edge_by_tag, used_tags)
    if alive_edge is not None:
        mark_edges_used([alive_edge], used_tags)
        return [alive_edge]

    journal_edge = find_edge_by_journal_identifier(work_part, spec, edge_by_tag, used_tags)
    if journal_edge is not None:
        mark_edges_used([journal_edge], used_tags)
        return [journal_edge]

    for edge in current_edges:
        edge_tag = safe_edge_tag(edge)
        if edge_tag and edge_tag in used_tags:
            continue
        if current_edge_matches_spec(edge, spec):
            mark_edges_used([edge], used_tags)
            return [edge]

    trimmed_edges = find_trimmed_edge_matches(work_part, spec, current_edges, edge_by_tag, used_tags)
    if trimmed_edges:
        mark_edges_used(trimmed_edges, used_tags)
        return trimmed_edges

    raise RuntimeError("Cannot resolve autofillet edge: {}".format(
        edge_spec_label(spec),
    ))


def resolve_edge_specs(work_part, specs):
    used_tags = set()
    current_edges, edge_by_tag = current_body_edge_index(work_part)
    resolved = []
    for spec in specs:
        resolved.extend(resolve_edge_spec(
            work_part,
            spec,
            used_tags=used_tags,
            current_edges=current_edges,
            edge_by_tag=edge_by_tag,
        ))
    return resolved


def create_exact_edge_rule(work_part, edges, rule_options):
    errors = []
    for rule_args in (
            (edges, rule_options),
            (tuple(edges), rule_options),
            (edges,),
            (tuple(edges),),
    ):
        try:
            return work_part.ScRuleFactory.CreateRuleEdgeDumb(*rule_args)
        except Exception as exc:
            errors.append(safe_text(exc))
    raise RuntimeError("CreateRuleEdgeDumb failed: {}".format("; ".join(errors)))


def create_tangent_edge_rule(work_part, edges, angle_tol, rule_options):
    errors = []
    for rule_args in (
            (edges, angle_tol, True, rule_options),
            (tuple(edges), angle_tol, True, rule_options),
            (edges, angle_tol, True),
            (tuple(edges), angle_tol, True),
    ):
        try:
            return work_part.ScRuleFactory.CreateRuleEdgeMultipleSeedTangent(*rule_args)
        except Exception as exc:
            errors.append(safe_text(exc))
    raise RuntimeError("CreateRuleEdgeMultipleSeedTangent failed: {}".format("; ".join(errors)))


def validate_exact_edge_collector(sc_collector, seed_edges, log=None):
    try:
        objects = list(sc_collector.GetObjects())
    except Exception as exc:
        if log:
            log("    exact edge collector validation skipped: {}".format(safe_text(exc)))
        return

    seed_tags = [safe_edge_tag(edge) for edge in seed_edges]
    object_tags = [safe_edge_tag(obj) for obj in objects]
    if len(objects) != len(seed_edges) or set(object_tags) != set(seed_tags):
        raise RuntimeError(
            "exact edge collector changed selection: requested tags={} selected tags={}".format(
                seed_tags,
                object_tags,
            )
        )


def log_auto_extend_collector(sc_collector, seed_edges, log=None):
    if not log:
        return
    try:
        objects = list(sc_collector.GetObjects())
    except Exception as exc:
        log("    auto-extend collector inspection skipped: {}".format(safe_text(exc)))
        return
    log("    auto-extend collector seed_edges={} selected_edges={}".format(
        len(seed_edges),
        len(objects),
    ))


def edge_spec_label(spec):
    if isinstance(spec, dict):
        return "tag={} jid={}".format(
            safe_text(spec.get("tag")),
            safe_text(spec.get("journal_identifier")),
        )
    return "tag={} jid={}".format(
        safe_edge_tag(spec),
        safe_edge_journal_identifier(spec),
    )


def safe_tag(obj):
    return safe_text(safe_call(lambda: obj.Tag, ""))


def safe_feature_name(feature):
    if feature is None:
        return ""
    name = safe_text(safe_call(lambda: feature.GetFeatureName(), ""))
    if name:
        return name
    return safe_text(safe_call(lambda: feature.Name, ""))


def suppress_autofillet_feature(session, work_part, feature, log=None):
    if feature is None:
        return False

    try:
        session.SetUndoMark(
            NXOpen.Session.MarkVisibility.Visible,
            "Suppress Failed Autofillet",
        )
    except Exception:
        pass

    try:
        features = [NXOpen.Features.Feature.Null] * 1
        features[0] = feature
        work_part.Features.SuppressFeatures(features)
    except Exception as collection_exc:
        try:
            feature.Suppress()
        except Exception as exc:
            if log:
                log("  failed-autofillet debug: failed to suppress feature: {}".format(
                    safe_text(collection_exc),
                ))
                log("  failed-autofillet debug: feature.Suppress fallback failed: {}".format(
                    safe_text(exc),
                ))
            return False

    suppressed = safe_call(lambda: feature.Suppressed, None)
    if log:
        log("  failed-autofillet debug: suppressed feature name={} tag={} suppressed={}".format(
            safe_feature_name(feature),
            safe_tag(feature),
            safe_text(suppressed),
        ))
    return suppressed is not False


def part_features(work_part):
    return list(safe_call(lambda: list(work_part.Features), []))


def feature_tag_snapshot(work_part):
    tags = set()
    for feature in part_features(work_part):
        tag = safe_tag(feature)
        if tag:
            tags.add(tag)
    return tags


def is_feature_like(obj):
    if obj is None:
        return False
    if safe_call(lambda: obj.GetFeatureName(), None) is not None:
        return True
    if safe_call(lambda: obj.FeatureType, None) is not None:
        return True
    return False


def append_unique_feature(candidates, feature):
    if not is_feature_like(feature):
        return

    feature_tag = safe_tag(feature)
    for existing in candidates:
        existing_tag = safe_tag(existing)
        if feature_tag and existing_tag == feature_tag:
            return
        if not feature_tag and existing is feature:
            return

    candidates.append(feature)


def new_features_since(work_part, before_feature_tags):
    features = []
    for feature in part_features(work_part):
        tag = safe_tag(feature)
        if tag and tag in before_feature_tags:
            continue
        append_unique_feature(features, feature)
    return features


def committed_builder_features(builder):
    features = []
    try:
        committed = list(builder.GetCommittedObjects())
    except Exception:
        committed = []

    for obj in committed:
        append_unique_feature(features, obj)

    return features


def autofillet_feature_sort_key(feature):
    return (
        safe_call(lambda: int(feature.Timestamp), -1),
        safe_tag(feature),
    )


def prefer_autofillet_feature(candidates):
    if not candidates:
        return None

    blend_candidates = []
    for feature in candidates:
        text = "{} {}".format(
            safe_feature_name(feature),
            safe_text(safe_call(lambda: feature.FeatureType, "")),
        ).upper()
        if "BLEND" in text or "FILLET" in text:
            blend_candidates.append(feature)

    usable = blend_candidates if blend_candidates else candidates
    return sorted(usable, key=autofillet_feature_sort_key)[-1]


def mark_autofillet_feature_as_failed_debug(feature, error, log=None):
    if feature is None:
        return False

    old_name = safe_feature_name(feature)
    if "FAILED_DEBUG_KEEP" in old_name:
        new_name = old_name
    else:
        base_name = old_name if old_name else "Edge Blend"
        new_name = "FAILED_DEBUG_KEEP {}".format(base_name)

    try:
        feature.SetName(new_name)
    except Exception as set_name_exc:
        try:
            feature.Name = new_name
        except Exception as name_exc:
            if log:
                log("  failed-autofillet debug: cannot rename feature tag={} old_name={} error={} fallback={}".format(
                    safe_tag(feature),
                    old_name,
                    safe_text(set_name_exc),
                    safe_text(name_exc),
                ))
            return False

    if log:
        log("  failed-autofillet debug: renamed feature tag={} old_name={} new_name={} reason={}".format(
            safe_tag(feature),
            old_name,
            safe_feature_name(feature),
            safe_text(error),
        ))
    return True


def keep_failed_autofillet_feature(work_part, builder, feature, before_feature_tags, error, log=None):
    candidates = []
    append_unique_feature(candidates, feature)
    for candidate in committed_builder_features(builder):
        append_unique_feature(candidates, candidate)
    for candidate in new_features_since(work_part, before_feature_tags):
        append_unique_feature(candidates, candidate)

    failed_feature = prefer_autofillet_feature(candidates)
    if failed_feature is None:
        if log:
            log("  failed-autofillet debug: no failed feature was found to keep")
        return None

    mark_autofillet_feature_as_failed_debug(failed_feature, error, log=log)

    if log:
        log("  failed-autofillet debug: kept feature name={} tag={} suppressed={}".format(
            safe_feature_name(failed_feature),
            safe_tag(failed_feature),
            safe_text(safe_call(lambda: failed_feature.Suppressed, "")),
        ))

    return failed_feature


def parse_fillet_edge_name(edge_name):
    """Return {"component_id": ..., "radius": ...} or None."""
    text = safe_text(edge_name).strip()
    if not text:
        return None

    try:
        data = json.loads(text)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    if "component_id" not in data or "radius" not in data:
        return None

    component_id = data["component_id"]
    radius = data["radius"]

    if component_id is None or radius is None:
        return None

    return {
        "component_id": safe_text(component_id),
        "radius": safe_text(radius),
    }


def collect_named_fillet_edges(work_part, log=None):
    """Collect STEP-imported edges whose NX edge.Name is JSON with component_id/radius."""
    components = OrderedDict()

    for body_index, body in enumerate(work_part.Bodies, start=1):
        for edge_index, edge in enumerate(body.GetEdges(), start=1):
            edge_name = safe_edge_name(edge)
            spec = parse_fillet_edge_name(edge_name)
            if spec is None:
                continue

            component_id = spec["component_id"]
            radius = spec["radius"]

            component = components.setdefault(
                component_id,
                {
                    "component_id": component_id,
                    "radius_groups": OrderedDict(),
                    "edge_count": 0,
                },
            )

            group = component["radius_groups"].setdefault(
                radius,
                {
                    "radius": radius,
                    "edges": [],
                },
            )
            group["edges"].append(make_edge_spec(edge))
            component["edge_count"] += 1

    if log:
        log("Collected {} fillet components.".format(len(components)))
        for component_id, component in components.items():
            summary = [
                "{}:{}".format(radius, len(group["edges"]))
                for radius, group in component["radius_groups"].items()
            ]
            log("  component_id={} edges={} radius_groups={}".format(
                component_id,
                component["edge_count"],
                summary,
            ))

    return components


def radius_sort_value(radius):
    try:
        return float(safe_text(radius))
    except Exception:
        return float("-inf")


def sort_radius_groups_desc(radius_groups):
    """Run larger radius groups before smaller ones; keep original order on ties."""
    indexed = list(enumerate(radius_groups))
    indexed.sort(
        key=lambda item: (radius_sort_value(item[1].get("radius")), -item[0]),
        reverse=True,
    )
    return [group for _index, group in indexed]


def sorted_components_by_max_radius_desc(components):
    """Run components whose largest radius is bigger first; keep original order on ties."""
    indexed = list(enumerate(components.items()))

    def key(item):
        original_index, (_component_id, component) = item
        groups = component["radius_groups"].values()
        max_radius = max([radius_sort_value(g.get("radius")) for g in groups] or [float("-inf")])
        return (max_radius, -original_index)

    indexed.sort(key=key, reverse=True)
    return [entry for _index, entry in indexed]


def create_fillet_from_edge_objects(
        edge_radius_groups,
        tolerance=0.01,
        angle_tol=0.5,
        log=None,
        dry_run=False,
        keep_failed_debug=True,
        auto_extend_tangent=False,
):
    """Create one NX EdgeBlend feature from edge objects grouped by radius."""
    theSession = NXOpen.Session.GetSession()
    workPart = theSession.Parts.Work

    markId = theSession.SetUndoMark(
        NXOpen.Session.MarkVisibility.Invisible if dry_run else NXOpen.Session.MarkVisibility.Visible,
        "边倒圆"
    )

    edgeBlendBuilder = workPart.Features.CreateEdgeBlendBuilder(
        NXOpen.Features.Feature.Null
    )
    feature = None
    before_feature_tags = feature_tag_snapshot(workPart)

    try:
        edgeBlendBuilder.SegmentBlendFaces = True
        edgeBlendBuilder.Tolerance = tolerance
        edgeBlendBuilder.AllInstancesOption = False
        edgeBlendBuilder.RemoveSelfIntersection = True
        edgeBlendBuilder.PatchComplexGeometryAreas = True
        edgeBlendBuilder.LimitFailingAreas = False
        edgeBlendBuilder.ConvexConcaveY = False
        edgeBlendBuilder.RollOverSmoothEdge = True
        edgeBlendBuilder.RollOntoEdge = True
        edgeBlendBuilder.MoveSharpEdge = True
        edgeBlendBuilder.TrimmingOption = False
        edgeBlendBuilder.OverlapOption = (
            NXOpen.Features.EdgeBlendBuilder.Overlap.MaintainAndIntersect
        )
        edgeBlendBuilder.BlendOrder = (
            NXOpen.Features.EdgeBlendBuilder.OrderOfBlending.ConvexFirst
        )
        edgeBlendBuilder.SetbackOption = (
            NXOpen.Features.EdgeBlendBuilder.Setback.SeparateFromCorner
        )
        edgeBlendBuilder.BlendFaceContinuity = (
            NXOpen.Features.EdgeBlendBuilder.FaceContinuity.Tangent
        )

        for group in edge_radius_groups:
            group_radius = safe_text(group["radius"])
            requested_edges = list(group["edges"])
            seed_edges = resolve_edge_specs(workPart, requested_edges)

            if not seed_edges:
                continue

            unique_seed_tags = set(safe_edge_tag(edge) for edge in seed_edges if safe_edge_tag(edge))
            if log:
                log("    resolved radius={} requested_edges={} resolved_edges={} unique_edges={}".format(
                    group_radius,
                    len(requested_edges),
                    len(seed_edges),
                    len(unique_seed_tags),
                ))
            if len(unique_seed_tags) != len(seed_edges):
                raise RuntimeError(
                    "resolved autofillet edges are not unique: radius={} requested={} resolved={} unique={}".format(
                        group_radius,
                        len(requested_edges),
                        len(seed_edges),
                        len(unique_seed_tags),
                    )
                )

            collector = workPart.ScCollectors.CreateCollector()

            rule_options = workPart.ScRuleFactory.CreateRuleOptions()
            try:
                rule_options.SetSelectedFromInactive(False)
                if auto_extend_tangent:
                    edge_rule = create_tangent_edge_rule(
                        workPart,
                        seed_edges,
                        angle_tol,
                        rule_options,
                    )
                else:
                    edge_rule = create_exact_edge_rule(workPart, seed_edges, rule_options)
            finally:
                rule_options.Dispose()

            collector.ReplaceRules([edge_rule], False)
            collector.AddEvaluationFilter(
                NXOpen.ScEvaluationFiltertype.LaminarEdge
            )
            if auto_extend_tangent:
                log_auto_extend_collector(collector, seed_edges, log=log)
            else:
                validate_exact_edge_collector(collector, seed_edges, log=log)

            edgeBlendBuilder.AddChainset(collector, group_radius)

        feature = edgeBlendBuilder.CommitFeature()
        theSession.SetUndoMarkName(markId, "边倒圆")
        if dry_run:
            theSession.UndoToMark(markId, "autofillet dry run")
            return None
        return feature

    except Exception as exc:
        if dry_run:
            try:
                theSession.UndoToMark(markId, "autofillet dry run")
            except Exception:
                pass
            raise

        if not keep_failed_debug:
            try:
                theSession.UndoToMark(markId, "autofillet failed probe")
            except Exception:
                pass
            raise

        kept_feature = keep_failed_autofillet_feature(
            workPart,
            edgeBlendBuilder,
            feature,
            before_feature_tags,
            exc,
            log=log,
        )
        if kept_feature is not None:
            try:
                theSession.SetUndoMarkName(markId, "autofillet failed kept")
            except Exception:
                pass
            raise

        try:
            theSession.UndoToMark(markId, "边倒圆")
        except Exception:
            pass
        raise

    finally:
        active_exception = sys.exc_info()[0] is not None
        try:
            edgeBlendBuilder.Destroy()
        except Exception:
            if not active_exception:
                raise


# ---------------------------------------------------------------------------
# Bisect helpers
# ---------------------------------------------------------------------------

def _is_overlap_error(exc):
    """Return True if the exception message matches a Blend faces overlap error."""
    msg = safe_text(exc)
    return any(kw in msg for kw in BLEND_OVERLAP_KEYWORDS)


def _is_unresolvable_edge_error_text(error_text):
    return "Cannot resolve autofillet edge" in safe_text(error_text)


def _flatten_radius_groups(radius_groups):
    """Convert radius_groups list -> flat list of (radius_str, edge) tuples."""
    result = []
    for group in radius_groups:
        for edge in group["edges"]:
            result.append((group["radius"], edge))
    return result


def _reconstruct_radius_groups(pairs):
    """Convert flat (radius_str, edge) tuples back to radius_groups list."""
    groups = OrderedDict()
    for radius, edge in pairs:
        if radius not in groups:
            groups[radius] = {"radius": radius, "edges": []}
        groups[radius]["edges"].append(edge)
    return list(groups.values())


def _edge_pair_label(pair):
    radius, edge_spec = pair
    return "radius={} {}".format(
        safe_text(radius),
        edge_spec_label(edge_spec),
    )


def _pair_resolvable_on_current_part(work_part, pair):
    _radius, edge_spec = pair
    try:
        return len(resolve_edge_specs(work_part, [edge_spec])) > 0
    except Exception:
        return False


def keep_failed_fillet_debug_attempt(
        pairs,
        tolerance,
        angle_tol,
        log,
        component_id="?",
        reason="",
        auto_extend_tangent=False,
):
    """Try a final failed batch on the main part so NX can leave a debug feature."""
    if not pairs:
        return

    theSession = NXOpen.Session.GetSession()
    mark_id = None
    try:
        mark_id = theSession.SetUndoMark(
            NXOpen.Session.MarkVisibility.Visible,
            "autofillet failed debug attempt",
        )
    except Exception:
        pass

    label = reason if reason else "failed batch"
    if log:
        log("  FAILED DEBUG KEEP TRY component_id={} edges={} reason={}".format(
            component_id,
            len(pairs),
            label,
        ))

    try:
        create_fillet_from_edge_objects(
            _reconstruct_radius_groups(pairs),
            tolerance=tolerance,
            angle_tol=angle_tol,
            log=log,
            keep_failed_debug=True,
            auto_extend_tangent=auto_extend_tangent,
        )
    except Exception as exc:
        if mark_id is not None:
            try:
                theSession.SetUndoMarkName(mark_id, "autofillet failed debug kept")
            except Exception:
                pass
        if log:
            log("  FAILED DEBUG KEEP DONE component_id={} edges={} | {}".format(
                component_id,
                len(pairs),
                safe_text(exc),
            ))
        return

    if log:
        log("  FAILED DEBUG KEEP UNEXPECTED SUCCESS component_id={} edges={} -> undo".format(
            component_id,
            len(pairs),
        ))

    if mark_id is not None:
        try:
            theSession.UndoToMark(mark_id, "autofillet failed debug unexpected success")
        except Exception as exc:
            if log:
                log("  FAILED DEBUG KEEP undo failed: {}".format(safe_text(exc)))


def dispose_save_status(save_status):
    if save_status is None:
        return
    try:
        save_status.Dispose()
    except Exception:
        pass


def save_part_in_place(work_part):
    save_status = work_part.Save(
        NXOpen.BasePart.SaveComponents.FalseValue,
        NXOpen.BasePart.CloseAfterSave.FalseValue,
    )
    dispose_save_status(save_status)


def open_prt_for_probe(session, part_path, main_part):
    probe_part = None
    load_status = None
    try:
        probe_part, load_status = session.Parts.OpenDisplay(part_path)
        activate_work_part(session, probe_part)
        return probe_part
    except Exception:
        if probe_part is not None:
            try:
                close_probe_part(session, probe_part, main_part)
            except Exception:
                pass
        raise
    finally:
        if load_status is not None:
            try:
                load_status.Dispose()
            except Exception:
                pass


class IsolatedFilletProbe(object):
    """Run destructive fillet probes on disposable .prt copies."""

    def __init__(self, session, main_part, log=None):
        self.session = session
        self.main_part = main_part
        self.log = log
        self.temp_dir = tempfile.mkdtemp(prefix="nx_autofillet_probe_")
        self.baseline_path = os.path.join(self.temp_dir, "baseline.prt")
        self.trial_index = 0

        source_path = safe_text(safe_call(lambda: main_part.FullPath, ""))
        if not source_path or not os.path.exists(source_path):
            raise RuntimeError("Cannot create autofillet probe baseline: main part has no saved FullPath")

        save_part_in_place(main_part)
        shutil.copy2(source_path, self.baseline_path)

    def cleanup(self):
        try:
            shutil.rmtree(self.temp_dir)
        except Exception:
            pass
        try:
            activate_work_part(self.session, self.main_part)
        except Exception:
            pass

    def try_pairs(self, pairs, tolerance, angle_tol, auto_extend_tangent=False):
        self.trial_index += 1
        trial_path = os.path.join(self.temp_dir, "trial_{:04d}.prt".format(self.trial_index))
        shutil.copy2(self.baseline_path, trial_path)

        probe_part = None
        try:
            probe_part = open_prt_for_probe(self.session, trial_path, self.main_part)
            create_fillet_from_edge_objects(
                _reconstruct_radius_groups(pairs),
                tolerance=tolerance,
                angle_tol=angle_tol,
                log=self.log,
                dry_run=False,
                keep_failed_debug=False,
                auto_extend_tangent=auto_extend_tangent,
            )
            return True, "", ""
        except Exception as exc:
            return False, safe_text(exc), traceback.format_exc()
        finally:
            if probe_part is not None:
                try:
                    close_probe_part(self.session, probe_part, self.main_part)
                except Exception:
                    pass
            silent_cleanup_step_temp_files(trial_path)


def create_fillet_greedy_probe(
        radius_groups,
        tolerance,
        angle_tol,
        log,
        component_id="?",
        keep_failed_fillet=True,
        auto_extend_tangent=False,
):
    """
    Try the whole batch first. If it fails, repeatedly build an order-stable
    compatible edge batch by probing accepted_edges + candidate_edge on a
    disposable part copy. Only accepted batches are committed on the main part.
    """
    theSession = NXOpen.Session.GetSession()
    main_part = theSession.Parts.Work
    all_pairs = _flatten_radius_groups(radius_groups)
    created_features = []
    failed_batches = []

    if not all_pairs:
        return created_features, failed_batches

    whole_probe = None
    try:
        whole_probe = IsolatedFilletProbe(theSession, main_part, log=log)
        whole_ok, whole_error, _whole_tb = whole_probe.try_pairs(
            all_pairs,
            tolerance=tolerance,
            angle_tol=angle_tol,
            auto_extend_tangent=auto_extend_tangent,
        )
    finally:
        if whole_probe is not None:
            whole_probe.cleanup()

    if whole_ok:
        feature = create_fillet_from_edge_objects(
            radius_groups,
            tolerance=tolerance,
            angle_tol=angle_tol,
            log=log,
            keep_failed_debug=keep_failed_fillet,
            auto_extend_tangent=auto_extend_tangent,
        )
        created_features.append(feature)
        log("  OK component_id={} batch_edges={} mode=whole".format(
            component_id,
            len(all_pairs),
        ))
        return created_features, failed_batches

    log("  WHOLE FAILED component_id={} edges={} -> greedy probe | {}".format(
        component_id,
        len(all_pairs),
        whole_error,
    ))

    pending = list(all_pairs)
    pass_index = 0

    while pending:
        pass_index += 1
        accepted = []
        rejected = []

        pass_probe = None
        try:
            pass_probe = IsolatedFilletProbe(theSession, main_part, log=log)

            for pair in pending:
                trial = accepted + [pair]
                ok, error_text, tb_text = pass_probe.try_pairs(
                    trial,
                    tolerance=tolerance,
                    angle_tol=angle_tol,
                    auto_extend_tangent=auto_extend_tangent,
                )
                if not ok:
                    if (
                            created_features
                            and _is_unresolvable_edge_error_text(error_text)
                            and not _pair_resolvable_on_current_part(main_part, pair)
                    ):
                        log("  GREEDY ABSORBED component_id={} pass={} | {} | target edge no longer has a resolvable residual after prior fillets".format(
                            component_id,
                            pass_index,
                            _edge_pair_label(pair),
                        ))
                        continue

                    rejected.append({
                        "pair": pair,
                        "error": error_text,
                        "traceback": tb_text,
                    })
                    log("  GREEDY SKIP component_id={} pass={} | {} | {}".format(
                        component_id,
                        pass_index,
                        _edge_pair_label(pair),
                        error_text,
                    ))
                    continue

                accepted.append(pair)
                log("  GREEDY KEEP component_id={} pass={} accepted={} | {}".format(
                    component_id,
                    pass_index,
                    len(accepted),
                    _edge_pair_label(pair),
                ))
        finally:
            if pass_probe is not None:
                pass_probe.cleanup()

        if not accepted:
            failed_pairs = [item["pair"] for item in rejected]
            for item in rejected:
                failed_batches.append({
                    "pairs": [item["pair"]],
                    "error": item["error"],
                    "traceback": item["traceback"],
                })
            log("  GREEDY STOP component_id={} remaining_edges={} cannot fillet even singly".format(
                component_id,
                len(pending),
            ))
            if keep_failed_fillet:
                for failed_pair in failed_pairs:
                    keep_failed_fillet_debug_attempt(
                        [failed_pair],
                        tolerance=tolerance,
                        angle_tol=angle_tol,
                        log=log,
                        component_id=component_id,
                        reason="remaining edge cannot fillet singly",
                        auto_extend_tangent=auto_extend_tangent,
                    )
            break

        try:
            feature = create_fillet_from_edge_objects(
                _reconstruct_radius_groups(accepted),
                tolerance=tolerance,
                angle_tol=angle_tol,
                log=log,
                keep_failed_debug=keep_failed_fillet,
                auto_extend_tangent=auto_extend_tangent,
            )
            created_features.append(feature)
            log("  OK component_id={} pass={} committed_edges={} remaining_edges={}".format(
                component_id,
                pass_index,
                len(accepted),
                len(rejected),
            ))
        except Exception as exc:
            error_text = safe_text(exc)
            tb_text = traceback.format_exc()
            failed_batches.append({
                "pairs": accepted,
                "error": error_text,
                "traceback": tb_text,
            })
            log("  GREEDY FINAL COMMIT FAILED component_id={} pass={} edges={} | {}".format(
                component_id,
                pass_index,
                len(accepted),
                error_text,
            ))
            log(tb_text.rstrip())

        pending = [item["pair"] for item in rejected]

    return created_features, failed_batches


def create_fillet_bisect(radius_groups, tolerance, angle_tol, log, component_id="?"):
    """
    Try to create EdgeBlend features for the given radius_groups.

    On "Blend faces overlap" failure, the flat edge list is bisected:
      - The first half is retried immediately (recursing as needed).
      - The second half is queued and processed after the first half finishes.

    Other errors cause the batch to be recorded as permanently failed.

    Returns:
        created_features  – list of NX Feature objects
        failed_batches    – list of dicts {pairs, error, traceback}
    """
    all_pairs = _flatten_radius_groups(radius_groups)

    created_features = []
    failed_batches = []

    # Queue of flat edge-pair lists still to be processed.
    pending = [all_pairs]

    while pending:
        batch = pending.pop(0)

        if not batch:
            continue

        groups = _reconstruct_radius_groups(batch)

        try:
            feature = create_fillet_from_edge_objects(
                groups,
                tolerance=tolerance,
                angle_tol=angle_tol,
                log=log,
            )
            created_features.append(feature)
            log("  OK component_id={} batch_edges={}".format(component_id, len(batch)))

        except Exception as exc:
            error_text = safe_text(exc)
            tb_text = traceback.format_exc()

            if _is_overlap_error(exc) and len(batch) > 1:
                mid = len(batch) // 2
                first_half = batch[:mid]
                second_half = batch[mid:]
                log(
                    "  OVERLAP component_id={} edges={} -> bisect [{}, {}]".format(
                        component_id, len(batch), len(first_half), len(second_half)
                    )
                )
                # Process first half next; second half after that.
                pending.insert(0, second_half)
                pending.insert(0, first_half)

            else:
                # Non-overlap error, or batch is a single edge and still fails.
                failed_batches.append({
                    "pairs": batch,
                    "error": error_text,
                    "traceback": tb_text,
                })
                log("  FAILED component_id={} batch_edges={} | {}".format(
                    component_id, len(batch), error_text
                ))
                log(tb_text.rstrip())

    return created_features, failed_batches


def flatten_failed_batch_pairs(failed_batches):
    pairs = []
    for batch in failed_batches:
        pairs.extend(batch.get("pairs", []))
    return pairs


# ---------------------------------------------------------------------------

def apply_named_edge_fillets(
        session,
        work_part,
        tolerance,
        angle_tol,
        log,
        keep_failed_fillet=True,
):
    components = collect_named_fillet_edges(work_part, log=log)

    created_features = []
    failed_components = []

    for component_id, component in sorted_components_by_max_radius_desc(components):
        radius_groups = sort_radius_groups_desc(
            list(component["radius_groups"].values())
        )
        radius_order = [
            "{}:{}".format(group["radius"], len(group["edges"]))
            for group in radius_groups
        ]

        log("Filleting component_id={} with {} radius group(s), {} edge(s).".format(
            component_id,
            len(radius_groups),
            component["edge_count"],
        ))
        log("  radius order (large -> small): {}".format(radius_order))

        component_failed_batches = []

        feats, failed_batches = create_fillet_greedy_probe(
            radius_groups,
            tolerance=tolerance,
            angle_tol=angle_tol,
            log=log,
            component_id=component_id,
            keep_failed_fillet=keep_failed_fillet,
        )

        created_features.extend(feats)
        component_failed_batches.extend(failed_batches)

        rescue_pairs = flatten_failed_batch_pairs(component_failed_batches)
        if rescue_pairs:
            log("  AUTO-EXTEND RESCUE component_id={} failed_edges={} -> tangent chain probe".format(
                component_id,
                len(rescue_pairs),
            ))
            rescue_feats, rescue_failed_batches = create_fillet_greedy_probe(
                _reconstruct_radius_groups(rescue_pairs),
                tolerance=tolerance,
                angle_tol=angle_tol,
                log=log,
                component_id="{}-autoextend".format(component_id),
                keep_failed_fillet=keep_failed_fillet,
                auto_extend_tangent=True,
            )
            created_features.extend(rescue_feats)
            component_failed_batches = rescue_failed_batches
            log("  AUTO-EXTEND RESCUE DONE component_id={} created_features={} remaining_failed_batches={}".format(
                component_id,
                len(rescue_feats),
                len(rescue_failed_batches),
            ))

        if component_failed_batches:
            # Aggregate all failed edges for reporting.
            total_failed_edges = sum(len(b["pairs"]) for b in component_failed_batches)
            failed_components.append({
                "component_id": component_id,
                "failed_batches": len(component_failed_batches),
                "failed_edges": total_failed_edges,
                "errors": [b["error"] for b in component_failed_batches],
            })

    return created_features, failed_components


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

        contexts = collect_autofillet_contexts(work_part)
        log("Autofillet geometry contexts: {}".format(len(contexts)))

        created_features = []
        failed_components = []

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
            active_part = activate_autofillet_context(session, context)
            if active_part is None or part_body_count(active_part) <= 0:
                log("  Skipped: no active body-owning part for this context.")
                continue

            assign_step_edge_names_to_nx_edges(
                [{"part": active_part, "transform": context.get("transform")}],
                args.input,
                log=log,
                # NX component traversal is reverse of the STEP BREP order.
                brep_index=len(contexts) - context_index + 1,
            )

            context_features, context_failed_components = apply_named_edge_fillets(
                session,
                active_part,
                tolerance=args.tolerance,
                angle_tol=args.angle_tol,
                log=log,
                keep_failed_fillet=args.keep_failed_fillet,
            )
            created_features.extend(context_features)
            for failed_component in context_failed_components:
                failed_component = dict(failed_component)
                failed_component["context"] = context.get("label")
                failed_components.append(failed_component)
            log(
                "  Context result: created_features={}, failed_components={}".format(
                    len(context_features),
                    len(context_failed_components),
                )
            )

        try:
            activate_work_part(session, work_part)
        except Exception as exc:
            log("Warning: could not restore top work part before export: {}".format(safe_text(exc)))

        if args.part_output:
            log("Saving NX part: {}".format(args.part_output))
            save_part_as(work_part, args.part_output)

        log("Exporting STEP: {}".format(args.output))
        export_step(session, work_part, args.output, args.settings_file)

        log("Created fillet features: {}".format(len(created_features)))
        log("Failed components (partial): {}".format(len(failed_components)))

        for fc in failed_components:
            log("  component_id={} failed_batches={} failed_edges={} errors={}".format(
                fc["component_id"],
                fc["failed_batches"],
                fc["failed_edges"],
                fc["errors"],
            ))

    finally:
        if work_part is not None and not args.keep_open:
            close_part(session, work_part)

        # sleep(10)
        if not args.no_cleanup:
            cleanup_temp_files(args.input, args.output)


if __name__ == "__main__":
    main(sys.argv[1:])
