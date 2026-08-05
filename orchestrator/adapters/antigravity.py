"""Antigravity adapter -- placeholder only, no working connection method yet.

Antigravity is the logical successor to Gemini CLI for this project's
individual-tier account (Gemini CLI itself now returns "This client is no
longer supported ... migrate to the Antigravity suite of products" --
IneligibleTierError, observed 2026-08-05). Antigravity is registered here as
a real, addressable AI system boundary in this project's role/candidate
model (see orchestrator/config.json's role_candidates), per the explicit
requirement to record it as a logical AI lineage rather than silently
falling back to Gemini.

No `antigravity` command exists on PATH on this machine, and no API/app
connection method has been identified yet, so this adapter cannot actually
launch anything. It exists so that:
  - availability.py has a real cli_type to report cli.not_installed for
    (not a made-up "always fails" agent hidden behind a working adapter);
  - the day a real Antigravity CLI/API/app integration is identified, only
    default_command_name()/build_argv()/prompt_transport need to change --
    the rest of the system (config, availability checks, fallback
    ordering, mail registration) is already wired.
"""

from __future__ import annotations

from pathlib import Path

from adapters.base import CliEvidence
from output_capture import OutputArtifact


class AntigravityCliAdapter:
    cli_type = "antigravity"
    # Unknown until a real connection method exists. "stdin" is the safer
    # placeholder default (matches the majority of adapters) since
    # build_argv() below refuses to run either way.
    prompt_transport = "stdin"

    def default_command_name(self) -> str:
        # Best-guess executable name for a future real integration; not
        # currently resolvable on any known PATH (see availability.py,
        # which reports this adapter as unavailable unconditionally,
        # independent of whether this name happens to exist on some future
        # machine).
        return "antigravity"

    def build_argv(
        self, command: list[str], project_path: Path, prompt_path: Path | None = None
    ) -> list[str]:
        raise NotImplementedError(
            "Antigravity has no working CLI/API connection in this project yet "
            "(cli_type='antigravity'); availability.py must exclude this agent "
            "from candidate selection before build_argv() is ever reached."
        )

    def classify_output(
        self,
        exit_code: int | None,
        timed_out: bool,
        stdout: OutputArtifact,
        stderr: OutputArtifact,
    ) -> CliEvidence:
        return CliEvidence()
