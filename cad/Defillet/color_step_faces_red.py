# -*- coding: utf-8 -*-
"""Color explicitly requested STEP ADVANCED_FACE entity IDs red through NX."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


SCRIPT_DIR = Path(__file__).resolve().parent
NX_LAUNCHER = SCRIPT_DIR / "color_blend_faces_red.py"
FACE_PATTERN = re.compile(
    rb"(#\s*(\d+)\s*=\s*ADVANCED_FACE\s*\(\s*)'((?:[^']|'')*)'",
    re.IGNORECASE | re.DOTALL,
)


def face_id(value: str) -> int:
    text = value.strip().lstrip("#")
    try:
        result = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("face ID must look like 248 or #248") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("face ID must be positive")
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Color exact original STEP ADVANCED_FACE entity IDs red with NX."
    )
    parser.add_argument("--input", required=True, help="Source STEP file")
    parser.add_argument("--faces", nargs="+", required=True, type=face_id, help="IDs such as 248 #249")
    parser.add_argument("--output", required=True, help="Colored STEP output")
    parser.add_argument("--report", default="", help="Optional NX JSON report")
    parser.add_argument("--part-output", default="", help="Optional colored NX part")
    parser.add_argument("--color", type=int, default=186, help="NX color index; 186 is red")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def inject_names(input_path: Path, tagged_path: Path, requested_ids: list[int]):
    data = input_path.read_bytes()
    requested = set(requested_ids)
    found = set()
    names = []
    chunks = []
    cursor = 0
    for match in FACE_PATTERN.finditer(data):
        step_id = int(match.group(2))
        if step_id not in requested:
            continue
        name = "DSH_TARGET_STEP_FACE_{}".format(step_id)
        chunks.append(data[cursor:match.start(3)])
        chunks.append(name.encode("ascii"))
        cursor = match.end(3)
        found.add(step_id)
        names.append(name)
    chunks.append(data[cursor:])
    missing = sorted(requested - found)
    if missing:
        raise ValueError(
            "these IDs are not ADVANCED_FACE entities in the input STEP: {}".format(
                ", ".join("#{}".format(value) for value in missing)
            )
        )
    tagged_path.write_bytes(b"".join(chunks))
    return names


def main(argv=None):
    args = parse_args(argv)
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError("input STEP does not exist: {}".format(input_path))
    if input_path == output_path:
        raise ValueError("input and output paths must differ")
    if output_path.exists() and not args.overwrite:
        raise FileExistsError("output exists; pass --overwrite: {}".format(output_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    requested_ids = sorted(set(args.faces))

    with tempfile.TemporaryDirectory(prefix="dsh-step-face-color-") as temporary:
        temporary_dir = Path(temporary)
        tagged_path = temporary_dir / "tagged_input.stp"
        names_path = temporary_dir / "target_names.json"
        target_names = inject_names(input_path, tagged_path, requested_ids)
        names_path.write_text(json.dumps(target_names, indent=2) + "\n", encoding="utf-8")

        report_path = (
            Path(args.report).expanduser().resolve()
            if args.report
            else output_path.with_name(output_path.stem + "_report.json")
        )
        command = [
            sys.executable, str(NX_LAUNCHER),
            "--input", str(tagged_path),
            "--output", str(output_path),
            "--report", str(report_path),
            "--target-face-names-json", str(names_path),
            "--color", str(args.color),
        ]
        if args.part_output:
            command.extend(["--part-output", str(Path(args.part_output).expanduser().resolve())])
        if args.overwrite:
            command.append("--overwrite")
        print("STEP: {}".format(input_path), flush=True)
        print("Target faces: {}".format(", ".join("#{}".format(value) for value in requested_ids)), flush=True)
        completed = subprocess.run(command, cwd=str(SCRIPT_DIR.parent.parent))
        if completed.returncode == 0:
            print("Colored STEP: {}".format(output_path), flush=True)
            print("Report: {}".format(report_path), flush=True)
        return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
