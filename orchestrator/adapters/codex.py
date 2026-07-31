"""Codex CLI adapter (SPEC.md 9.3, 11章)."""

from __future__ import annotations

from pathlib import Path


class CodexCliAdapter:
    cli_type = "codex"

    def default_command_name(self) -> str:
        return "codex"

    def build_argv(self, command: list[str], project_path: Path) -> list[str]:
        # "-" tells Codex to read the prompt from stdin, which avoids
        # putting the instruction in argv/process listings and sidesteps
        # Windows command-line code-page issues (SPEC.md 21章).
        return [
            *command,
            "exec",
            "--full-auto",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(project_path),
            "-",
        ]
