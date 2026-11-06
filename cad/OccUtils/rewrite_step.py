# -*- coding: utf-8 -*-
"""Read a STEP file with OCC and write it back as a normalized STEP."""

from __future__ import annotations

import argparse
import sys

from OCC.Core.IFSelect import IFSelect_RetDone
from OCC.Core.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCC.Core.TopAbs import TopAbs_FACE
from OCC.Core.TopExp import TopExp_Explorer


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="STEP file to read")
    parser.add_argument("--output", required=True, help="Normalized STEP file to write")
    return parser.parse_args(argv)


def read_step(path):
    reader = STEPControl_Reader()
    status = reader.ReadFile(path)
    if status != IFSelect_RetDone:
        raise RuntimeError("Failed to read STEP: {}".format(path))
    reader.TransferRoots()
    return reader.OneShape()


def count_faces(shape):
    count = 0
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def write_step(shape, path):
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    status = writer.Write(path)
    if status != IFSelect_RetDone:
        raise RuntimeError("Failed to write STEP: {}".format(path))


def main(argv=None):
    args = parse_args(argv)
    shape = read_step(args.input)
    face_count = count_faces(shape)
    write_step(shape, args.output)
    print("[rewrite step] faces={} input={} output={}".format(face_count, args.input, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
