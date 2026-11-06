import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from webapp.backend.app import nx_persistent_worker, nx_worker
from webapp.backend.app.config import REPO_ROOT


def test_nx_journals_use_cad_paths():
    assert REPO_ROOT == Path(__file__).resolve().parents[3]
    journals = {
        nx_worker.NX_JOURNAL: "nx_web_delete_fillet.py",
        nx_worker.NX_CLASSIFY_JOURNAL: "nx_web_classify_fillet_faces.py",
        nx_worker.NX_COMPONENT_JOURNAL: "nx_web_detect_fillet_components.py",
        nx_worker.NX_COMPONENT_DELETE_JOURNAL: "nx_web_delete_fillet_component.py",
        nx_worker.NX_REMOVE_ALL_FILLETS_JOURNAL: "nx_web_remove_all_fillets.py",
    }
    for path, name in journals.items():
        assert path == REPO_ROOT / "cad" / "Defillet" / name
        assert path.is_file()
    assert nx_persistent_worker.WORKER_JOURNAL == REPO_ROOT / "cad" / "nx_persistent_worker.py"
    assert nx_persistent_worker.WORKER_JOURNAL.is_file()


def test_coloring_launcher_uses_cad_path_and_repo_cwd(monkeypatch, tmp_path):
    script = REPO_ROOT / "cad" / "Defillet" / "color_matched_step_faces_red.py"
    spec = importlib.util.spec_from_file_location("color_matched_step_faces_red", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.REPO_ROOT == REPO_ROOT
    assert module.DEFAULT_MATCHES.parent == REPO_ROOT / "data" / "CAE"
    assert module.DEFAULT_STEP.parent == REPO_ROOT / "data" / "CAE"
    assert module.DEFAULT_OUTPUT.parent == REPO_ROOT / "data" / "CAE"
    assert module.DEFAULT_REPORT.parent == REPO_ROOT / "data" / "CAE"
    assert module.DEFAULT_IDS.parent == REPO_ROOT / "data" / "CAE"
    assert module.COLOR_LAUNCHER == REPO_ROOT / "cad" / "Defillet" / "color_blend_faces_red.py"
    assert module.COLOR_LAUNCHER.is_file()

    matches = tmp_path / "matches.json"
    matches.write_text(json.dumps({"matches": [{"mapping": {"accepted": True, "best_step_ref": "#10"}}]}))
    step = tmp_path / "model.step"
    step.write_text("#10=ADVANCED_FACE('',(),$,.T.);", encoding="ascii")
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or SimpleNamespace(returncode=0))
    assert module.main(["--matches", str(matches), "--input", str(step), "--ids-output", str(tmp_path / "ids.json")]) == 0
    command, kwargs = calls[0]
    assert command[1] == str(module.COLOR_LAUNCHER)
    assert kwargs["cwd"] == str(REPO_ROOT)
