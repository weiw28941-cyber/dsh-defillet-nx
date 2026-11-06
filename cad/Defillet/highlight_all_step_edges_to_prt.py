# -*- coding: utf-8 -*-
r"""NX command-line journal: color every topology edge in a STEP and save as PRT.

Goal:
    Open a STEP file, collect every B-Rep topology edge from all imported bodies,
    color/thicken those NXOpen.Edge objects directly, make bodies translucent for
    contrast, set the work view to show shaded edges, and save the result as .prt.

Usage:
    cad\run_nx_py.bat cad\Defillet\highlight_all_step_edges_to_prt.py -args --input "D:\xxx\test.step" --output "D:\xxx\all_edges_colored.prt"

Notes:
    NXOpen.Edge inherits NXOpen.DisplayableObject. The reliable primary path is
    DisplayManager.NewDisplayModification() -> NewColor/NewWidth -> Apply(edges).
    This script intentionally avoids extracting edges into curves; it colors the
    topology edges themselves.
"""

import argparse
import os
import sys

import NXOpen
import NXOpen.Preferences



EDGE_COLOR = 186
BODY_COLOR = 216
BODY_TRANSLUCENCY = 85
CHUNK_SIZE = 500
EDGE_WIDTH = "Nine"

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
    parser.add_argument("--output", required=True, help="Output PRT file")
    parser.add_argument("--edge-color", type=int, default=EDGE_COLOR, help="NX color index for topology edges, 1..216")
    parser.add_argument("--edge-width", default=EDGE_WIDTH, choices=sorted(DISPLAY_WIDTHS), help="NX line width for topology edges")
    parser.add_argument("--body-color", type=int, default=BODY_COLOR, help="NX color index for bodies, 1..216")
    parser.add_argument("--body-translucency", type=int, default=BODY_TRANSLUCENCY, help="Body translucency, 0..90")
    parser.add_argument("--keep-open", action="store_true", help="Keep part open after save")
    parser.add_argument("--hide-body", action="store_true", help="Blank/hide solid bodies after coloring edges")
    parser.add_argument("--highlight", action="store_true", help="Also call Highlight() on colored edges in the active NX session")
    parser.add_argument("--no-view-edit", action="store_true", help="Do not add view-dependent wireframe edits")
    return parser.parse_args(argv)


def safe_text(value):
    try:
        return str(value)
    except Exception as exc:
        return "<str failed: {}>".format(exc)


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


def validate_color_index(color, label):
    color = int(color)
    if color < 1 or color > 216:
        raise ValueError("{} must be an NX color index in 1..216, got {}".format(label, color))
    return color


def clamp_translucency(value):
    value = int(value)
    if value < 0:
        return 0
    if value > 90:
        return 90
    return value


def resolve_widths(width_name):
    width_name = DISPLAY_WIDTHS[width_name]
    return (
        getattr(NXOpen.DisplayableObject.ObjectWidth, width_name),
        getattr(NXOpen.ViewDependentDisplayManager.Width, width_name),
        getattr(NXOpen.Preferences.ViewVisualizationVisual.SmoothEdgeWidth, width_name),
    )


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def open_step(session, input_path):
    _base_part, load_status = session.Parts.OpenActiveDisplay(
        input_path,
        NXOpen.DisplayPartOption.AllowAdditional,
    )
    try:
        work_part = session.Parts.Work
        session.ApplicationSwitchImmediate("UG_APP_MODELING")
        return work_part
    finally:
        try:
            load_status.Dispose()
        except Exception:
            pass


def close_part(session, work_part):
    try:
        work_part.Close(
            NXOpen.BasePart.CloseWholeTree.FalseValue,
            NXOpen.BasePart.CloseModified.UseResponses,
            None,
        )
    finally:
        try:
            session.ApplicationSwitchImmediate("UG_APP_NOPART")
        except Exception:
            pass


def save_part_as(work_part, output_prt):
    output_dir = os.path.dirname(os.path.abspath(output_prt))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
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


def object_tag(obj):
    try:
        return int(obj.Tag)
    except Exception:
        return safe_text(getattr(obj, "Tag", ""))


def collect_bodies(work_part):
    bodies = []
    try:
        for body in work_part.Bodies:
            bodies.append(body)
    except Exception:
        pass
    return bodies


def collect_all_edges(work_part, log=None):
    edges = []
    seen_tags = set()

    for body_index, body in enumerate(collect_bodies(work_part), start=1):
        try:
            body_edges = list(body.GetEdges())
        except Exception as exc:
            if log is not None:
                log("WARNING: failed to read edges from body {} tag {}: {}".format(body_index, object_tag(body), safe_text(exc)))
            continue

        for edge in body_edges:
            tag = object_tag(edge)
            if tag in seen_tags:
                continue
            seen_tags.add(tag)
            edges.append(edge)

    return edges


def apply_display(session, objects, color=None, translucency=None, width=None, apply_to_all_faces=False, log=None):
    objects = [obj for obj in objects if obj is not None]
    if not objects:
        return 0

    changed = 0
    for object_chunk in chunks(objects, CHUNK_SIZE):
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
        except Exception as exc:
            if log is not None:
                log("WARNING: DisplayModification.Apply failed for {} objects: {}".format(len(object_chunk), safe_text(exc)))
            raise
        finally:
            try:
                display_modification.Dispose()
            except Exception:
                pass

    return changed


def apply_edge_display_with_fallback(session, edges, color, width, log):
    try:
        changed = apply_display(session, edges, color=color, width=width, log=log)
        return changed, 0
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


def count_edges_with_color(edges, color):
    matched = 0
    readable = 0
    for edge in edges:
        try:
            readable += 1
            if int(edge.Color) == int(color):
                matched += 1
        except Exception:
            pass
    return matched, readable


def set_work_view_for_colored_edges(work_part, edge_color, smooth_edge_width, log):
    try:
        work_view = work_part.ModelingViews.WorkView
    except Exception as exc:
        log("WARNING: cannot get work view: {}".format(safe_text(exc)))
        return False

    ok = True
    try:
        work_view.RenderingStyle = NXOpen.View.RenderingStyleType.ShadedWithEdges
    except Exception as exc:
        ok = False
        log("WARNING: cannot set work view rendering style to ShadedWithEdges: {}".format(safe_text(exc)))

    try:
        work_view.DynamicHiddenEdges = NXOpen.View.HiddenEdgesType.Invisible
        work_view.StaticHiddenEdges = NXOpen.View.HiddenEdgesType.Invisible
    except Exception as exc:
        log("WARNING: cannot set hidden-edge visibility: {}".format(safe_text(exc)))

    try:
        visual = work_view.VisualizationVisualPreferences
        visual.ShadedEdgeColor = int(edge_color)
        visual.ShadedEdgeStyle = NXOpen.Preferences.ViewVisualizationVisual.ShadedEdgeStyleType.ShadedEdgeColor

        appearance = visual.DisplayAppearance
        appearance.Silhouettes = False
        appearance.SmoothEdges = False
        appearance.SmoothEdgeColor = int(edge_color)
        appearance.SmoothEdgeWidth = smooth_edge_width
        visual.DisplayAppearance = appearance
    except Exception as exc:
        ok = False
        log("WARNING: cannot update shaded edge visualization preferences: {}".format(safe_text(exc)))

    try:
        work_view.Fit()
        work_view.UpdateDisplay()
    except Exception:
        pass

    return ok


def apply_view_dependent_edge_edit(work_part, edges, edge_color, view_width, log):
    try:
        work_view = work_part.ModelingViews.WorkView
        nx_color = work_part.Colors.Find(int(edge_color))
        for edge_chunk in chunks(edges, CHUNK_SIZE):
            work_view.DependentDisplay.ApplyWireframeEdit(
                nx_color,
                NXOpen.ViewDependentDisplayManager.Font.Solid,
                view_width,
                edge_chunk,
            )
        return True
    except Exception as exc:
        log("WARNING: view-dependent wireframe edit failed: {}".format(safe_text(exc)))
        return False


def hide_bodies(session, bodies):
    try:
        session.DisplayManager.BlankObjects(bodies)
        return True
    except Exception:
        return False


def highlight_edges(edges, log):
    highlighted = 0
    for edge in edges:
        try:
            edge.Highlight()
            highlighted += 1
        except Exception:
            pass
    if highlighted != len(edges):
        log("WARNING: highlighted {}/{} edges".format(highlighted, len(edges)))
    return highlighted


def main(argv=None):
    args = parse_args(argv)
    edge_color = validate_color_index(args.edge_color, "edge color")
    body_color = validate_color_index(args.body_color, "body color")
    body_translucency = clamp_translucency(args.body_translucency)
    object_edge_width, view_edge_width, smooth_edge_width = resolve_widths(args.edge_width)

    session = NXOpen.Session.GetSession()
    log = make_logger(session)
    work_part = None

    try:
        log("Opening STEP: {}".format(args.input))
        work_part = open_step(session, args.input)

        bodies = collect_bodies(work_part)
        edges = collect_all_edges(work_part, log=log)
        log("Bodies: {}".format(len(bodies)))
        log("Topology edges found: {}".format(len(edges)))

        if not edges:
            log("No topology edges found. Saving original part only.")
            save_part_as(work_part, args.output)
            return

        mark_id = session.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "color all topology edges")

        if args.hide_body:
            if hide_bodies(session, bodies):
                log("Bodies hidden/blanked by request.")
            else:
                log("WARNING: failed to hide bodies.")
        else:
            apply_display(
                session,
                bodies,
                color=body_color,
                translucency=body_translucency,
                apply_to_all_faces=True,
                log=log,
            )
            log("Bodies made translucent before edge coloring.")

        changed, failed = apply_edge_display_with_fallback(session, edges, edge_color, object_edge_width, log)
        log("Topology edge display changed: {}".format(changed))
        if failed:
            log("WARNING: direct edge fallback failed on {} edges".format(failed))

        matched, readable = count_edges_with_color(edges, edge_color)
        log("Edge color readback: {}/{} readable edges report color {}".format(matched, readable, edge_color))

        if not args.no_view_edit:
            if apply_view_dependent_edge_edit(work_part, edges, edge_color, view_edge_width, log):
                log("View-dependent wireframe edge edit applied.")

        if set_work_view_for_colored_edges(work_part, edge_color, smooth_edge_width, log):
            log("Work view configured to show shaded colored edges.")

        if args.highlight:
            highlight_edges(edges, log)

        session.SetUndoMarkName(mark_id, "color all topology edges")

        log("Saving PRT: {}".format(args.output))
        save_part_as(work_part, args.output)
        log("Done.")

    finally:
        if work_part is not None and not args.keep_open:
            close_part(session, work_part)


if __name__ == "__main__":
    main(sys.argv[1:])
