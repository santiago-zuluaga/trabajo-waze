import json
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PortabilityTests(unittest.TestCase):
    def test_project_supports_python_311_and_312_and_installs_local_modules(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["requires-python"], ">=3.11,<3.13")
        self.assertEqual(
            set(project["tool"]["setuptools"]["py-modules"]),
            {"waze_accidents", "waze_communes"},
        )
        self.assertIn("osmnx", {item.split(">")[0].split("<")[0].split("=")[0] for item in project["project"]["dependencies"]})

    def test_notebook_bootstraps_colab_and_anchors_paths_to_project_root(self):
        notebook = json.loads((ROOT / "WAZE_IA_CORRECCION.ipynb").read_text(encoding="utf-8"))
        source = "".join(notebook["cells"][2]["source"])
        data_source = "".join(notebook["cells"][10]["source"])
        experiment_source = "".join(notebook["cells"][36]["source"])
        self.assertIn("COLAB_RELEASE_TAG", source)
        self.assertIn("git", source)
        self.assertIn("pip", source)
        self.assertIn("os.chdir(PROJECT_ROOT)", source)
        self.assertIn('PROJECT_ROOT / "data" / "raw"', data_source)
        self.assertIn("target = PROJECT_ROOT / target", experiment_source)
        self.assertNotIn("C:\\Users\\", source + data_source + experiment_source)


if __name__ == "__main__":
    unittest.main()
