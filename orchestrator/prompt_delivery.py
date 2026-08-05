"""Temp-file prompt delivery for prompt_transport="prompt_file" adapters.

Most adapters (Claude Code, Codex) receive the fixed instruction over
stdin (adapters/base.py). A few CLIs (Grok) require the prompt as a file
path instead. This module owns the write/cleanup lifecycle for that path so
launcher.py doesn't need Grok-specific (or any CLI-specific) branching --
any adapter that declares prompt_transport="prompt_file" gets the same
handling.

Guarantees:
  - UTF-8 content, written once.
  - A unique path per call (tempfile.mkstemp is atomic/collision-safe;
    concurrent jobs/attempts never share a file).
  - Placed in the OS per-user temp directory, which on both Windows
    (per-profile ACLs under %TEMP%) and POSIX (mkstemp's default 0o600
    mode) is already "hard for other users to read" without this module
    doing its own permission management.
  - The prompt body is never logged; only the path may appear in logs
    (already true of build_argv()'s returned argv).
  - Callers MUST call delete_prompt_file() from a finally block covering
    every exit path (success, nonzero exit, timeout, launch failure,
    Python exception) -- see launcher.py's CliLauncher.launch()/wait().
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


class PromptFileError(Exception):
    """A prompt_file transport temp file could not be written."""


def _sanitize(value: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in value)[:60]


def write_prompt_file(instruction: str, *, job_id: str, agent_name: str, attempt: int) -> Path:
    """Write ``instruction`` to a new private UTF-8 temp file and return its path.

    Raises PromptFileError (never a bare OSError) on failure, after best-
    effort cleanup of any partially-created file.
    """
    prefix = f"orchestrator-prompt-{_sanitize(job_id)}-{_sanitize(agent_name)}-a{attempt}-"
    fd = None
    path: Path | None = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".txt")
        path = Path(raw_path)
        with os.fdopen(fd, "wb") as handle:
            fd = None  # os.fdopen took ownership; avoid double-close below.
            handle.write(instruction.encode("utf-8"))
    except OSError as err:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if path is not None:
            _silent_unlink(path)
        raise PromptFileError(f"failed to write prompt file for {agent_name!r}: {err}") from err
    return path


def delete_prompt_file(path: Path | None) -> None:
    """Best-effort cleanup. Never raises. Logs only the path, never content."""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as err:
        print(
            f"[WARN] prompt_fileの削除に失敗しました: {path} ({err.__class__.__name__})",
            file=sys.stderr,
        )


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
