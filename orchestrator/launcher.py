"""CLI detection and process launch (SPEC.md 11章, 12章, 21章)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from adapters.base import CliAdapter, CliEvidence
from config import AgentDefinition
from output_capture import OutputArtifact, StreamCapture, build_capture
from timeutil import now_iso
from winproc import CREATE_NEW_PROCESS_GROUP, get_process_start_time_iso, terminate_process_tree

_SECRET_FLAG_NAMES = {
    "--api-key", "--apikey", "--token", "--auth-token", "--auth",
    "--password", "--secret", "--client-secret",
}
_URL_CREDENTIAL_PATTERN = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")


def redact_command(argv: list[str]) -> list[str]:
    """Best-effort secret redaction for a launch command before it is
    stored in logs or runtime/ (SPEC.md 23章「実行コマンドの秘密情報を除い
    た部分」). Masks known secret flags (both "--flag value" and
    "--flag=value" forms) and basic-auth credentials embedded in URLs.
    This is a defensive net, not the primary control: the instruction
    itself never appears in argv at all (it is sent over stdin).
    """
    redacted: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if arg.lower() in _SECRET_FLAG_NAMES:
            redacted.append(arg)
            redact_next = True
            continue
        if "=" in arg:
            name, _, _value = arg.partition("=")
            if name.lower() in _SECRET_FLAG_NAMES:
                redacted.append(f"{name}=[REDACTED]")
                continue
        redacted.append(_URL_CREDENTIAL_PATTERN.sub(r"\1[REDACTED]@", arg))
    return redacted

FIXED_INSTRUCTION_TEMPLATE = (
    "あなたは{agent_name}です。\n"
    "UIDは{uid}です。\n"
    "\n"
    "プロジェクト内のmailパッケージを使い、自分宛ての未読メールを確認してください。\n"
    "引継ぎ情報がある場合は確認してください。\n"
    "メールの指示を実行し、指定された宛先へ結果をメールしてください。\n"
    "処理対象がなくなったら終了してください。\n"
)


class CliNotFoundError(Exception):
    """No usable CLI command could be resolved for an agent (SPEC.md 11章)."""


class CliPathResolver:
    """config command -> Windows PATH -> per-CLI default name (SPEC.md 11章)."""

    def __init__(self, adapters: dict[str, CliAdapter]) -> None:
        self._adapters = adapters

    def resolve(self, agent: AgentDefinition) -> list[str]:
        if agent.command:
            return list(agent.command)
        adapter = self._adapters.get(agent.cli_type)
        if adapter is None:
            raise CliNotFoundError(f"unknown cli_type: {agent.cli_type!r}")
        default_name = adapter.default_command_name()
        found = shutil.which(default_name)
        if not found:
            raise CliNotFoundError(
                f"CLI not found for agent {agent.name!r}: no config.json command "
                f"and {default_name!r} is not on PATH"
            )
        return [found]


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    timed_out: bool
    duration_sec: float
    # True unless a timeout/force-stop path terminated the process and
    # could not confirm it actually exited. Callers must not release the
    # double-launch guard (runtime_store entry) when this is False -- see
    # DispatchCycle._attempt_launch.
    terminated_confirmed: bool = True
    stdout: OutputArtifact | None = None
    stderr: OutputArtifact | None = None
    cli_evidence: CliEvidence = CliEvidence()


class LaunchedProcess:
    def __init__(
        self,
        popen: subprocess.Popen,
        agent: AgentDefinition,
        job_id: str,
        origin_mail_id: int,
        launch_command: list[str],
        launched_at_iso: str,
        start_time_iso: str,
        adapter: CliAdapter,
        stdout_capture: StreamCapture,
        stderr_capture: StreamCapture,
    ) -> None:
        self._popen = popen
        self.pid = popen.pid
        self.agent = agent
        self.job_id = job_id
        self.origin_mail_id = origin_mail_id
        self.launch_command = launch_command
        self.launched_at_iso = launched_at_iso
        self.start_time_iso = start_time_iso
        self._adapter = adapter
        self._stdout_capture = stdout_capture
        self._stderr_capture = stderr_capture
        self._capture_threads: list[threading.Thread] = []
        self._capture_started = False

    def _start_capture(self) -> None:
        if self._capture_started:
            return
        self._capture_started = True
        self._stdout_capture.start()
        self._stderr_capture.start()
        for stream, capture in (
            (self._popen.stdout, self._stdout_capture),
            (self._popen.stderr, self._stderr_capture),
        ):
            assert stream is not None
            thread = threading.Thread(target=self._drain, args=(stream, capture), daemon=True)
            thread.start()
            self._capture_threads.append(thread)

    @staticmethod
    def _drain(stream: object, capture: StreamCapture) -> None:
        reader = stream  # type: ignore[assignment]
        try:
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                capture.feed(chunk)
        finally:
            try:
                reader.close()
            except (OSError, ValueError):
                pass

    def _finish_capture(self) -> tuple[OutputArtifact, OutputArtifact]:
        for thread in self._capture_threads:
            thread.join(timeout=10)
        # A descendant that inherited a pipe can keep EOF from arriving.
        # Close the parent's handles to unblock the reader without retaining
        # an open file or racing StreamCapture.finish().
        for stream in (self._popen.stdout, self._popen.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        for thread in self._capture_threads:
            thread.join(timeout=1)
        return self._stdout_capture.finish(), self._stderr_capture.finish()

    def finish_capture(self) -> tuple[OutputArtifact, OutputArtifact]:
        """Finalize output after an externally interrupted wait."""
        return self._finish_capture()

    def wait(self, timeout_sec: float) -> ProcessResult:
        started = time.monotonic()
        timed_out = False
        terminated_confirmed = True
        self._start_capture()
        try:
            self._popen.wait(timeout=timeout_sec)
            exit_code: int | None = self._popen.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            self.terminate()
            try:
                self._popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            exit_code = None
            # poll() reflects Python's own child-reaping state, independent
            # of whether the communicate() above raised again: if the
            # process is truly gone, poll() returns its exit code.
            terminated_confirmed = self._popen.poll() is not None
        stdout, stderr = self._finish_capture()
        classify_output = getattr(self._adapter, "classify_output", None)
        evidence = (
            classify_output(exit_code, timed_out, stdout, stderr)
            if classify_output is not None
            else CliEvidence()
        )
        duration_sec = time.monotonic() - started
        return ProcessResult(
            exit_code=exit_code,
            timed_out=timed_out,
            duration_sec=duration_sec,
            terminated_confirmed=terminated_confirmed,
            stdout=stdout,
            stderr=stderr,
            cli_evidence=evidence,
        )

    def terminate(self) -> None:
        terminate_process_tree(self.pid)

    def has_exited(self) -> bool:
        return self._popen.poll() is not None


class CliLauncher:
    def __init__(
        self,
        resolver: CliPathResolver,
        adapters: dict[str, CliAdapter],
        logs_dir: Path | None = None,
        output_max_bytes: int = 1024 * 1024,
        output_ring_bytes: int = 64 * 1024,
    ) -> None:
        self._resolver = resolver
        self._adapters = adapters
        self._logs_dir = logs_dir
        self._output_max_bytes = output_max_bytes
        self._output_ring_bytes = output_ring_bytes

    def launch(
        self, agent: AgentDefinition, job_id: str, origin_mail_id: int, project_path: Path,
        attempt: int = 1,
    ) -> LaunchedProcess:
        command = self._resolver.resolve(agent)
        adapter = self._adapters[agent.cli_type]
        argv = adapter.build_argv(command, project_path)
        instruction = self._build_fixed_instruction(agent)
        env = self._build_subprocess_env()
        launched_at = now_iso()

        creationflags = CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        try:
            popen = subprocess.Popen(
                argv,
                cwd=str(project_path),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as err:
            # Covers a config.json command that does not exist on disk
            # (FileNotFoundError) as well as other launch-time OS failures
            # (e.g. "not a valid Win32 application"). Either way, the
            # process never started, so this is DELIVERY_FAILED territory
            # (SPEC.md 26章), not an unhandled crash.
            raise CliNotFoundError(f"failed to start CLI for {agent.name!r}: {err}") from err
        try:
            popen.stdin.write(instruction.encode("utf-8"))
            popen.stdin.close()
        except (BrokenPipeError, OSError):
            # The process may have exited immediately; wait() will surface
            # the resulting non-zero/failed status.
            pass

        # Never substitute launched_at (our own clock) for a missing OS
        # start time: is_same_running_process() would then compare against
        # a fabricated value and could wrongly treat the still-running
        # process as STALE (or, worse, wrongly match a different process
        # that happens to share this recycled PID). An empty string can
        # never equal a real "%Y-%m-%dT...Z" start time, so it always
        # falls through to the safe "not running" / STALE path (SPEC.md 24章).
        start_time_iso = get_process_start_time_iso(popen.pid) or ""
        stdout_capture, stderr_capture = build_capture(
            logs_dir=self._logs_dir,
            job_id=job_id,
            attempt=attempt,
            agent_name=agent.name,
            max_file_bytes=self._output_max_bytes,
            ring_bytes=self._output_ring_bytes,
        )
        launched = LaunchedProcess(
            popen=popen,
            agent=agent,
            job_id=job_id,
            origin_mail_id=origin_mail_id,
            launch_command=argv,
            launched_at_iso=launched_at,
            start_time_iso=start_time_iso,
            adapter=adapter,
            stdout_capture=stdout_capture,
            stderr_capture=stderr_capture,
        )
        # Start drainers before returning so a fast CLI cannot fill a pipe
        # during the caller's small gap before wait().
        launched._start_capture()
        return launched

    def _build_fixed_instruction(self, agent: AgentDefinition) -> str:
        return FIXED_INSTRUCTION_TEMPLATE.format(agent_name=agent.name, uid=agent.uid)

    def _build_subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env
