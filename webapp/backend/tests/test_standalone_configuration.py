import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("script", [
    "nx_2_occ_detect_sharp_edge_group_inference.py",
    "nx_2_occ_detect_sharp_edge_group_inference_support_pairs.py",
])
def test_inference_scripts_start_outside_repository(script, tmp_path):
    root = Path(__file__).resolve().parents[3]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(root / "cad" / "Defillet" / script), "--help"],
        cwd=tmp_path, env=env, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert b"--original" in result.stdout

@pytest.mark.parametrize("option", ["--model", "--defillet-from-model"])
def test_inference_model_aliases(option):
    from cad.Defillet.nx_2_occ_detect_sharp_edge_group_inference import parse_args

    args = parse_args(["--original", "original.step", "--defillet", "defillet.step",
                       option, "model.step", "--no-display"])
    assert args.defillet_from_model == "model.step"


def test_inference_requires_model():
    from cad.Defillet.nx_2_occ_detect_sharp_edge_group_inference import parse_args

    with pytest.raises(SystemExit) as error:
        parse_args(["--original", "original.step", "--defillet", "defillet.step"])
    assert error.value.code == 2
