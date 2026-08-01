"""Bounded, redacted subprocess output capture (FAILOVER_DESIGN.md)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


_SECRET_PATTERNS = (
    ("authorization", re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")),
    (
        "credential_assignment",
        re.compile(
            r"(?i)([\"'][A-Za-z0-9_.-]{0,64}"
            r"(?:api[_-]?key|token|secret|password|passwd|cookie)"
            r"[A-Za-z0-9_.-]{0,64}[\"']\s*:\s*)"
            r"(?:\"(?:\\[^\r\n]|`[^\r\n]|\"\"|[^\"\\`\r\n])*\"|"
            r"'(?:\\[^\r\n]|`[^\r\n]|''|[^'\\`\r\n])*'|[^\s,;]+)"
        ),
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]{0,64}"
            r"(?:api[_-]?key|token|secret|password|passwd|cookie)"
            r"[A-Za-z0-9_.-]{0,64}\s*[:=]\s*)"
            r"(?:\"(?:\\[^\r\n]|`[^\r\n]|\"\"|[^\"\\`\r\n])*\"|"
            r"'(?:\\[^\r\n]|`[^\r\n]|''|[^'\\`\r\n])*'|[^\s,;]+)"
        ),
    ),
    ("api_key", re.compile(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s,;]+")),
    ("token", re.compile(r"(?i)((?:access|refresh|auth)[_ -]?token\s*[:=]\s*)[^\s,;]+")),
    ("password", re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+")),
    ("secret", re.compile(r"(?i)(secret\s*[:=]\s*)[^\s,;]+")),
    ("cookie", re.compile(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+")),
    ("url_credentials", re.compile(r"(?i)(https?://)[^\s/:@]+:[^\s/@]+@")),
)


def mask_text(text: str) -> str:
    for _name, pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]" if m.lastindex else "[REDACTED]", text)
    return text


@dataclass(frozen=True)
class OutputArtifact:
    relative_path: str | None
    sha256: str | None
    saved_bytes: int
    total_read_bytes: int
    truncated: bool
    tail: str
    redaction_rule_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "saved_bytes": self.saved_bytes,
            "total_read_bytes": self.total_read_bytes,
            "truncated": self.truncated,
            "redaction_rule_ids": list(self.redaction_rule_ids),
        }


class StreamCapture:
    """One stream: bounded evidence file plus bounded latest-output ring."""

    def __init__(
        self,
        *,
        stream_name: str,
        output_path: Path | None,
        max_file_bytes: int,
        ring_bytes: int,
        logs_root: Path | None,
    ) -> None:
        self.stream_name = stream_name
        self.output_path = output_path
        self.max_file_bytes = max_file_bytes
        self.ring_bytes = ring_bytes
        self.logs_root = logs_root.resolve() if logs_root is not None else None
        self.total_read_bytes = 0
        self.saved_bytes = 0
        self.truncated = False
        self._ring = bytearray()
        self._digest = hashlib.sha256()
        self._file = None
        self._pending = ""
        self._rule_ids: set[str] = set()

    def start(self) -> None:
        if self.output_path is not None:
            if self.logs_root is None:
                raise ValueError("logs_root is required when output_path is set")
            self.output_path = self.output_path.resolve()
            self.output_path.relative_to(self.logs_root)
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.output_path.open("wb")

    def feed(self, raw: bytes) -> None:
        self.total_read_bytes += len(raw)
        text = self._pending + raw.decode("utf-8", errors="replace")
        # Keep a carry so a secret split over two OS chunks is still masked.
        # Keep a bounded but generous carry for credentials split across
        # chunks. This is separate from the 64 KiB evidence ring and never
        # grows with total CLI output.
        carry_len = 8192
        process_text = text[:-carry_len] if len(text) > carry_len else ""
        self._pending = text[len(process_text):]
        self._write_masked(process_text)

    def finish(self) -> OutputArtifact:
        self._write_masked(self._pending)
        self._pending = ""
        if self._file is not None:
            self._file.flush()
            self._file.close()
            self._file = None
        relative = None
        if self.output_path is not None and self.logs_root is not None:
            relative = self.output_path.relative_to(self.logs_root).as_posix()
        return OutputArtifact(
            relative_path=relative,
            sha256=self._digest.hexdigest() if self.output_path is not None else None,
            saved_bytes=self.saved_bytes,
            total_read_bytes=self.total_read_bytes,
            truncated=self.truncated,
            tail=bytes(self._ring).decode("utf-8", errors="replace"),
            redaction_rule_ids=tuple(sorted(self._rule_ids)),
        )

    def _write_masked(self, text: str) -> None:
        masked = mask_text(text)
        if masked != text:
            for name, pattern in _SECRET_PATTERNS:
                if pattern.search(text):
                    self._rule_ids.add(name)
        data = masked.encode("utf-8")
        self._ring.extend(data)
        if len(self._ring) > self.ring_bytes:
            del self._ring[: len(self._ring) - self.ring_bytes]
        if self._file is None:
            return
        remaining = self.max_file_bytes - self.saved_bytes
        if remaining <= 0:
            if data:
                self.truncated = True
            return
        to_write = data[:remaining]
        self._file.write(to_write)
        self._digest.update(to_write)
        self.saved_bytes += len(to_write)
        if len(data) > len(to_write):
            self.truncated = True


def build_capture(
    *,
    logs_dir: Path | None,
    job_id: str,
    attempt: int,
    agent_name: str,
    max_file_bytes: int,
    ring_bytes: int,
) -> tuple[StreamCapture, StreamCapture]:
    safe_job = re.sub(r"[^A-Za-z0-9._-]", "_", job_id)
    safe_agent = re.sub(r"[^A-Za-z0-9._-]", "_", agent_name)
    root = logs_dir / "cli-output" / safe_job if logs_dir is not None else None
    captures = []
    for stream in ("stdout", "stderr"):
        path = root / f"{attempt}-{safe_agent}-{stream}.log" if root is not None else None
        captures.append(StreamCapture(
            stream_name=stream,
            output_path=path,
            max_file_bytes=max_file_bytes,
            ring_bytes=ring_bytes,
            logs_root=logs_dir,
        ))
    return captures[0], captures[1]
