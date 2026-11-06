# -*- coding: utf-8 -*-
"""Regular-Python launcher for the NXOpen blend-face coloring journal."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
NX_JOURNAL = SCRIPT_DIR / "nx_color_blend_faces_red.py"
DEFAULT_NX_BASE = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Siemens" / "NX2406"


def resolve_run_journal(nx_base: str = "", run_journal: str = "") -> Path:
    if run_journal:
        return Path(run_journal).expanduser().resolve()
    env_runner = os.environ.get("NX_RUN_JOURNAL", "").strip()
    if env_runner:
        return Path(env_runner).expanduser().resolve()
    base = Path(
        nx_base
        or os.environ.get("UGII_BASE_DIR", "").strip()
        or str(DEFAULT_NX_BASE)
    ).expanduser()
    return (base / "NXBIN" / "run_journal.exe").resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Launch nx_color_blend_faces_red.py through Siemens NX run_journal.exe."
        )
    )
    parser.add_argument("--input", required=True, help="Input STEP")
    parser.add_argument("--output", required=True, help="Colored STEP output")
    parser.add_argument("--report", default="", help="Optional JSON report")
    parser.add_argument("--part-output", default="", help="Optional colored NX PRT")
    parser.add_argument("--settings-file", default="", help="Optional STEP214 .def")
    parser.add_argument(
        "--target-step-ids-json",
        default="",
        help="Legacy geometric target descriptor JSON",
    )
    parser.add_argument(
        "--target-face-names-json",
        default="",
        help="Optional JSON array of exact imported NX face names to color",
    )
    parser.add_argument("--color", type=int, default=186, help="NX color index")
    parser.add_argument("--nx-base", default="", help="NX installation root")
    parser.add_argument("--run-journal", default="", help="Explicit run_journal.exe")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner = resolve_run_journal(args.nx_base, args.run_journal)
    if not runner.is_file():
        raise FileNotFoundError("run_journal.exe does not exist: {}".format(runner))
    if not NX_JOURNAL.is_file():
        raise FileNotFoundError("NX journal does not exist: {}".format(NX_JOURNAL))

    command = [
        str(runner),
        str(NX_JOURNAL),
        "-args",
        "--input",
        str(Path(args.input).expanduser().resolve()),
        "--output",
        str(Path(args.output).expanduser().resolve()),
        "--color",
        str(args.color),
    ]
    for option, value in (
        ("--report", args.report),
        ("--part-output", args.part_output),
        ("--settings-file", args.settings_file),
        ("--target-step-ids-json", args.target_step_ids_json),
        ("--target-face-names-json", args.target_face_names_json),
    ):
        if value:
            command.extend([option, str(Path(value).expanduser().resolve())])
    if args.overwrite:
        command.append("--overwrite")
    if args.keep_open:
        command.append("--keep-open")

    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)

    print("NX runner: {}".format(runner), flush=True)
    print("NX journal: {}".format(NX_JOURNAL), flush=True)
    print("Command: {}".format(subprocess.list2cmdline(command)), flush=True)
    completed = subprocess.run(
        command,
        cwd=str(SCRIPT_DIR.parent.parent),
        env=environment,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
