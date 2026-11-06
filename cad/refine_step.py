# -*- coding: utf-8 -*-
"""NX journal: optimize all faces in a STEP file and export a cleaned STEP."""

import argparse
import os
import sys
from time import sleep

import NXOpen


DEFAULT_NX_BASE = os.path.join(os.environ.get("ProgramFiles", "C:/Program Files"), "Siemens", "NX2406")
STEP_DEF = os.environ.get("NX_STEP214_EXPORT_DEF") or os.path.join(
    os.environ.get("UGII_BASE_DIR") or DEFAULT_NX_BASE,
    "STEP214UG",
    "ugstep214.def",
)
DIST_TOL = 0.01


def safe_text(value):
    try:
        return str(value)
    except Exception:
        return "<unprintable>"


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="STEP file to open")
    parser.add_argument("--output", required=True, help="STEP file to export")
    parser.add_argument("--part-output", default="", help="Optional NX .prt file to save before STEP export")
    parser.add_argument("--settings-file", default=STEP_DEF, help="STEP214 .def settings file")
    parser.add_argument("--distance-tolerance", type=float, default=DIST_TOL)
    parser.add_argument("--keep-open", action="store_true", help="Keep the imported part open after export")
    parser.add_argument("--no-cleanup", action="store_true", help="Do not delete STEP translator log/prt temp files")
    return parser.parse_args(argv)


def open_step(session, path):
    _, load_status = session.Parts.OpenActiveDisplay(
        path,
        NXOpen.DisplayPartOption.AllowAdditional,
    )
    load_status.Dispose()
    session.ApplicationSwitchImmediate("UG_APP_MODELING")
    return session.Parts.Work


def close_all_parts(session):
    try:
        responses = session.Parts.NewPartCloseResponses()
    except Exception:
        responses = None
    try:
        session.Parts.CloseAll(NXOpen.BasePart.CloseModified.CloseModified, responses)
    except Exception as exc:
        print("[warning] CloseAll failed: {}".format(safe_text(exc)))
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


def cleanup_step_import_sidecars(input_path):
    directory = os.path.dirname(os.path.abspath(input_path))
    input_stem = os.path.splitext(os.path.basename(input_path))[0].lower()
    deleted_count = 0

    try:
        filenames = os.listdir(directory)
    except Exception as exc:
        print("[warning] cannot list STEP import sidecars: {}".format(safe_text(exc)))
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
            print("[warning] cannot delete STEP import sidecar {}: {}".format(path, safe_text(exc)))

    if deleted_count:
        print("[cleanup] deleted stale STEP import sidecars: {}".format(deleted_count))
    return deleted_count


def collect_all_faces(work_part):
    faces = []
    for body in work_part.Bodies:
        for face in body.GetFaces():
            faces.append(face)
    return faces


def optimize_all_faces(work_part, faces, tolerance):
    if not faces:
        print("[optimize] faces=0, skipped")
        return

    builder = work_part.Features.CreateOptimizeFaceBuilder()
    builder.DistanceTolerance = tolerance
    builder.Report = False

    opts = work_part.ScRuleFactory.CreateRuleOptions()
    opts.SetSelectedFromInactive(False)
    rule = work_part.ScRuleFactory.CreateRuleFaceDumb(faces, opts)
    opts.Dispose()

    builder.FacesToOptimize.ReplaceRules([rule], False)
    builder.Commit()
    builder.Destroy()
    print("[optimize] faces={}, tolerance={} mm".format(len(faces), tolerance))


def export_step(session, work_part, output_path, def_file):
    creator = session.DexManager.CreateStepCreator()
    creator.ExportAs = NXOpen.StepCreator.ExportAsOption.Ap214
    creator.SettingsFile = def_file
    creator.ObjectTypes.Solids = True
    creator.InputFile = work_part.FullPath
    creator.OutputFile = output_path
    creator.FileSaveFlag = False
    creator.LayerMask = "1-256"
    creator.ProcessHoldFlag = True
    creator.Commit()
    creator.Destroy()
    print("[export] {}".format(output_path))


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


def close_part(session, work_part):
    work_part.Close(
        NXOpen.BasePart.CloseWholeTree.FalseValue,
        NXOpen.BasePart.CloseModified.UseResponses,
        None,
    )
    session.ApplicationSwitchImmediate("UG_APP_NOPART")


def main(argv=None):
    args = parse_args(argv)
    session = NXOpen.Session.GetSession()

    work_part = None
    try:
        close_all_parts(session)
        cleanup_step_import_sidecars(args.input)
        print("[open] {}".format(args.input))
        work_part = open_step(session, args.input)

        faces = collect_all_faces(work_part)
        optimize_all_faces(work_part, faces, args.distance_tolerance)

        if args.part_output:
            save_part_as(work_part, args.part_output)
            print("[save part] {}".format(args.part_output))

        export_step(session, work_part, args.output, args.settings_file)
        print("[done]")
    finally:
        if work_part is not None and not args.keep_open:
            close_part(session, work_part)

        # sleep(10)
        if not args.no_cleanup:
            cleanup_temp_files(args.input, args.output)


if __name__ == "__main__":
    main(sys.argv[1:])
