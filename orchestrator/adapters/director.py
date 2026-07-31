"""Director process adapter. Director itself is not an AI CLI."""

from __future__ import annotations

from pathlib import Path

from adapters.base import CliEvidence
from output_capture import OutputArtifact


class DirectorCliAdapter:
    cli_type = "director"

    def default_command_name(self) -> str:
        return "py"

    def build_argv(self, command: list[str], project_path: Path) -> list[str]:
        return [*command, "--once"]

    def classify_output(self, exit_code: int | None, timed_out: bool, stdout: OutputArtifact, stderr: OutputArtifact) -> CliEvidence:
        return CliEvidence()
