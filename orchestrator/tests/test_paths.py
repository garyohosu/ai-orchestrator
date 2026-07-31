import tempfile
import unittest
from pathlib import Path

from paths import PathResolver, ProjectPathOutOfRangeError


class PathResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_root = Path(tempfile.mkdtemp())
        self.orchestrator_dir = self.project_root / "orchestrator"
        self.orchestrator_dir.mkdir()
        self.script_path = self.orchestrator_dir / "orchestrator.py"
        self.script_path.write_text("", encoding="utf-8")
        self.resolver = PathResolver.from_script(self.script_path)

    def test_locations_derived_from_script_path(self) -> None:
        self.assertEqual(self.resolver.orchestrator_dir, self.orchestrator_dir.resolve())
        self.assertEqual(self.resolver.project_root, self.project_root.resolve())
        self.assertEqual(self.resolver.mail_dir, (self.project_root / "mail").resolve())

    def test_empty_project_path_defaults_to_project_root(self) -> None:
        self.assertEqual(self.resolver.resolve_project_path(None), self.project_root.resolve())
        self.assertEqual(self.resolver.resolve_project_path(""), self.project_root.resolve())

    def test_relative_project_path_within_root_is_allowed(self) -> None:
        sub = self.project_root / "work"
        resolved = self.resolver.resolve_project_path("work")
        self.assertEqual(resolved, sub.resolve())

    def test_relative_project_path_escaping_root_is_rejected(self) -> None:
        with self.assertRaises(ProjectPathOutOfRangeError):
            self.resolver.resolve_project_path("../outside")

    def test_absolute_project_path_outside_root_is_rejected(self) -> None:
        outside = Path(tempfile.mkdtemp())
        with self.assertRaises(ProjectPathOutOfRangeError):
            self.resolver.resolve_project_path(str(outside))

    def test_absolute_project_path_inside_root_is_allowed(self) -> None:
        inside = self.project_root / "work2"
        resolved = self.resolver.resolve_project_path(str(inside))
        self.assertEqual(resolved, inside.resolve())

    def test_resolution_is_independent_of_current_working_directory(self) -> None:
        import os

        other_cwd = tempfile.mkdtemp()
        original_cwd = os.getcwd()
        try:
            os.chdir(other_cwd)
            resolver = PathResolver.from_script(self.script_path)
            self.assertEqual(resolver.project_root, self.project_root.resolve())
        finally:
            os.chdir(original_cwd)


if __name__ == "__main__":
    unittest.main()
