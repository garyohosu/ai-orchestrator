"""Path resolution anchored to orchestrator.py's own location (SPEC.md 5,6章).

All paths are resolved from ``orchestrator.py``'s file location, never
from the current working directory, so the same project and mail
database are used regardless of where PowerShell was launched from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ProjectPathOutOfRangeError(Exception):
    """A configured project_path normalizes to outside project_root."""


@dataclass(frozen=True)
class PathResolver:
    orchestrator_dir: Path
    project_root: Path
    mail_dir: Path

    @classmethod
    def from_script(cls, script_path: Path) -> "PathResolver":
        orchestrator_dir = Path(script_path).resolve().parent
        project_root = orchestrator_dir.parent
        mail_dir = project_root / "mail"
        return cls(
            orchestrator_dir=orchestrator_dir,
            project_root=project_root,
            mail_dir=mail_dir,
        )

    def resolve_project_path(self, project_path: str | None) -> Path:
        """Resolve config.json's project_path, rejecting anything outside project_root.

        An empty/None value means "use project_root itself" (SPEC.md 5章).
        Both relative and absolute inputs are normalized and must resolve
        to project_root or a location under it; absolute paths are
        accepted syntactically but remain subject to the same containment
        check, since the safety property ("正規化後にプロジェクトルート外
        となる指定は拒否する") is about the resulting location, not the
        input's spelling.
        """
        root = self.project_root.resolve()
        if not project_path:
            return root
        candidate = Path(project_path)
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ProjectPathOutOfRangeError(
                f"project_path {project_path!r} resolves to {resolved}, "
                f"which is outside project_root {root}"
            )
        return resolved
