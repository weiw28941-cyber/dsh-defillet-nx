import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_closure", PLUGIN / "test" / "runtime-closure.py")
runtime_closure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime_closure)


class RuntimeClosureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.stage = Path(self.temporary.name)

    def put(self, name, text=""):
        path = self.stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return name

    def test_manifest_uses_migrated_paths_and_preserves_skill_reference(self):
        manifest = json.loads((PLUGIN / "release-files.json").read_text(encoding="utf-8"))
        runtime = manifest["runtime"]
        self.assertEqual(len(runtime), len(set(runtime)))
        for directory in ("Defillet", "OccUtils"):
            self.assertTrue(any(path.startswith("cad/" + directory + "/") for path in runtime))
            self.assertFalse(any(path.startswith(directory + "/") for path in runtime))
        self.assertIn("cad/nx_persistent_worker.py", runtime)
        self.assertIn("fillet-removal-strategy/references/strategy-background.md", manifest["skills"])

    def test_new_imports_and_journal_are_checked(self):
        backend = self.put("webapp/backend/app/main.py", """
from . import helper
from cad.OccUtils.map_freecad_face_to_step import read_step
import nx_helper
JOURNAL = REPO_ROOT / "cad" / "Defillet" / "journal.py"
""")
        helper = self.put("webapp/backend/app/helper.py")
        nx_helper = self.put("webapp/backend/app/nx_helper.py")
        occ = self.put("cad/OccUtils/map_freecad_face_to_step.py")
        journal = self.put("cad/Defillet/journal.py", "from nx_1_defillet import run\n")
        shared = self.put("cad/Defillet/nx_1_defillet.py")
        allowed = [backend, helper, nx_helper, occ, journal, shared]
        runtime_closure.check_runtime_closure(self.stage, allowed)
        for dependency in (helper, nx_helper, occ, journal, shared):
            with self.subTest(dependency=dependency), self.assertRaises(AssertionError):
                runtime_closure.check_runtime_closure(self.stage, [item for item in allowed if item != dependency])

    def test_legacy_path_is_not_satisfied_by_new_location(self):
        backend = self.put("webapp/backend/app/main.py", """
from OccUtils.map_freecad_face_to_step import read_step
JOURNAL = REPO_ROOT / "Defillet" / "journal.py"
""")
        occ = self.put("cad/OccUtils/map_freecad_face_to_step.py")
        journal = self.put("cad/Defillet/journal.py")
        with self.assertRaises(AssertionError):
            runtime_closure.check_runtime_closure(self.stage, [backend, occ, journal])

    def test_occutils_import_resolves_explicit_cad_search_path(self):
        backend = self.put("webapp/backend/app/main.py", """
import sys
sys.path.insert(0, str(REPO_ROOT / "cad"))
from OccUtils.map_freecad_face_to_step import read_step
""")
        occ = self.put("cad/OccUtils/map_freecad_face_to_step.py")
        runtime_closure.check_runtime_closure(self.stage, [backend, occ])


if __name__ == "__main__":
    unittest.main()
