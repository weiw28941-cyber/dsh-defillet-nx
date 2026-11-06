# -*- coding: utf-8 -*-
"""Extract accepted best STEP face matches and color those faces red in NX."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
CASE_DIR = REPO_ROOT / "data" / "CAE"
DEFAULT_MATCHES = CASE_DIR / "step_mesh_quality_feature_matches.json"
DEFAULT_STEP = CASE_DIR / "zhongke123.stp"
DEFAULT_OUTPUT = CASE_DIR / "zhongke123_matched_faces_red.stp"
DEFAULT_REPORT = CASE_DIR / "zhongke123_matched_faces_red_report.json"
DEFAULT_IDS = CASE_DIR / "zhongke123_matched_step_face_ids.json"
COLOR_LAUNCHER = REPO_ROOT / "cad" / "Defillet" / "color_blend_faces_red.py"
FACE_PATTERN = re.compile(
    rb"(#\s*(\d+)\s*=\s*ADVANCED_FACE\s*\(\s*)'((?:[^']|'')*)'",
    re.IGNORECASE | re.DOTALL,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract accepted mapping.best_step_ref values and color those STEP faces red."
    )
    parser.add_argument("--matches", default=str(DEFAULT_MATCHES))
    parser.add_argument("--input", default=str(DEFAULT_STEP))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--ids-output", default=str(DEFAULT_IDS))
    parser.add_argument("--part-output", default="")
    parser.add_argument("--color", type=int, default=186)
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def extract_step_face_ids(payload: dict) -> list[int]:
    ids: set[int] = set()
    for index, match in enumerate(payload.get("matches", []), start=1):
        mapping = match.get("mapping") or {}
        if not mapping.get("accepted"):
            continue
        step_ref = mapping.get("best_step_ref")
        if not isinstance(step_ref, str) or not step_ref.startswith("#"):
            raise ValueError(
                "accepted match {} has invalid best_step_ref: {!r}".format(index, step_ref)
            )
        try:
            step_id = int(step_ref[1:])
        except ValueError as exc:
            raise ValueError("invalid STEP face reference: {!r}".format(step_ref)) from exc
        if step_id <= 0:
            raise ValueError("STEP face ID must be positive: {}".format(step_id))
        ids.add(step_id)
    if not ids:
        raise ValueError("no accepted mapping.best_step_ref values found")
    return sorted(ids)


def inject_target_face_names(
    input_path: Path, output_path: Path, step_ids: list[int]
) -> list[str]:
    data = input_path.read_bytes()
    targets = set(step_ids)
    found: set[int] = set()
    names: list[str] = []
    chunks: list[bytes] = []
    cursor = 0
    for match in FACE_PATTERN.finditer(data):
        step_id = int(match.group(2))
        if step_id not in targets:
            continue
        name = "DSH_TARGET_STEP_FACE_{}".format(step_id)
        chunks.append(data[cursor:match.start(3)])
        chunks.append(name.encode("ascii"))
        cursor = match.end(3)
        found.add(step_id)
        names.append(name)
    chunks.append(data[cursor:])
    missing = sorted(targets - found)
    if missing:
        raise RuntimeError("requested IDs are not ADVANCED_FACE entities: {}".format(missing))
    output_path.write_bytes(b"".join(chunks))
    return names


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    matches_path = Path(args.matches).expanduser().resolve()
    input_path = Path(args.input).expanduser().resolve()
    ids_path = Path(args.ids_output).expanduser().resolve()

    if not matches_path.is_file():
        raise FileNotFoundError("match JSON does not exist: {}".format(matches_path))
    if not input_path.is_file():
        raise FileNotFoundError("actual STEP does not exist: {}".format(input_path))
    if not COLOR_LAUNCHER.is_file():
        raise FileNotFoundError("color launcher does not exist: {}".format(COLOR_LAUNCHER))

    with matches_path.open("r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    step_ids = extract_step_face_ids(payload)
    ids_path.parent.mkdir(parents=True, exist_ok=True)
    tagged_input = ids_path.with_name(input_path.stem + "_tagged_targets.stp")
    target_names = inject_target_face_names(input_path, tagged_input, step_ids)
    ids_path.write_text(json.dumps(target_names, indent=2) + "\n", encoding="utf-8")

    print("Match JSON: {}".format(matches_path), flush=True)
    print("Actual STEP: {}".format(input_path), flush=True)
    print("Accepted unique STEP faces ({}): {}".format(
        len(step_ids), ", ".join("#{}".format(value) for value in step_ids)
    ), flush=True)
    print("Extracted IDs: {}".format(ids_path), flush=True)

    if args.extract_only:
        return 0

    command = [
        sys.executable,
        str(COLOR_LAUNCHER),
        "--input", str(tagged_input),
        "--output", str(Path(args.output).expanduser().resolve()),
        "--report", str(Path(args.report).expanduser().resolve()),
        "--target-face-names-json", str(ids_path),
        "--color", str(args.color),
    ]
    if args.part_output:
        command.extend(["--part-output", str(Path(args.part_output).expanduser().resolve())])
    if args.overwrite:
        command.append("--overwrite")
    return int(subprocess.run(command, cwd=str(REPO_ROOT)).returncode)


if __name__ == "__main__":
    raise SystemExit(main())
