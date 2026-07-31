"""Common interface for per-product CLI adapters (SPEC.md 11章, 12章)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from output_capture import OutputArtifact


@dataclass(frozen=True)
class CliEvidence:
    rate_limited: bool = False
    rule_id: str | None = None
    stream: str | None = None
    evidence: str | None = None


class CliAdapter(Protocol):
    cli_type: str

    def default_command_name(self) -> str:
        """The PATH-resolvable executable name to fall back to (SPEC.md 11章)."""
        ...

    def build_argv(self, command: list[str], project_path: Path) -> list[str]:
        """Build the full argv from the resolved command prefix.

        The fixed instruction (SPEC.md 12章) is passed via stdin by the
        launcher, not appended here, so it never appears in the process's
        argv (visible in task lists / process logs) or in redacted
        command logging.
        """
        ...

    def classify_output(
        self,
        exit_code: int | None,
        timed_out: bool,
        stdout: OutputArtifact,
        stderr: OutputArtifact,
    ) -> CliEvidence:
        ...
