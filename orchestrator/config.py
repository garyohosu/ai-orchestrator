"""config.json loading and validation (SPEC.md 10章)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MAIL_CHECK_INTERVAL_SEC = 5
DEFAULT_CLI_TIMEOUT_SEC = 1800
DEFAULT_REPLY_CHECK_TIMEOUT_SEC = 30
DEFAULT_MAX_ROUND_TRIPS = 10
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RUN_DURATION_SEC = 14400
DEFAULT_LOGS_DIR = "logs"
DEFAULT_CHECKPOINTS_DIR = "checkpoints"
DEFAULT_RUNTIME_DIR = "runtime"

# Same contract as mail/SPEC.md's UID format: "UID" + 6 or more ASCII digits.
_UID_PATTERN = re.compile(r"^UID[0-9]{6,}$")
_KNOWN_CLI_TYPES = ("codex", "claude_code")


class ConfigValidationError(Exception):
    """config.json is missing, malformed, or fails a safety check."""


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    uid: str
    cli_type: str
    command: list[str] = field(default_factory=list)
    order_index: int = 0


@dataclass(frozen=True)
class OrchestratorConfig:
    mail_check_interval_sec: int
    cli_timeout_sec: int
    reply_check_timeout_sec: int
    max_round_trips: int
    max_retries: int
    max_run_duration_sec: int
    agents: list[AgentDefinition]
    project_path: str | None
    logs_dir: str
    checkpoints_dir: str
    runtime_dir: str


def _positive_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigValidationError(f"{key} must be an integer")
    if value <= 0:
        raise ConfigValidationError(f"{key} must be a positive integer")
    return value


def _nonnegative_int(data: dict[str, Any], key: str, default: int) -> int:
    """Like _positive_int but allows 0 (used only for max_run_duration_sec)."""
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigValidationError(f"{key} must be an integer")
    if value < 0:
        raise ConfigValidationError(f"{key} must not be negative")
    return value


def _parse_agents(raw_agents: Any) -> list[AgentDefinition]:
    if raw_agents is None:
        return []
    if not isinstance(raw_agents, list):
        raise ConfigValidationError("agents must be a list")
    agents: list[AgentDefinition] = []
    seen_names: set[str] = set()
    seen_uids: set[str] = set()
    for index, entry in enumerate(raw_agents):
        if not isinstance(entry, dict):
            raise ConfigValidationError(f"agents[{index}] must be an object")
        name = entry.get("name")
        uid = entry.get("uid")
        cli_type = entry.get("cli_type")
        command = entry.get("command", [])
        if not isinstance(name, str) or not name:
            raise ConfigValidationError(f"agents[{index}].name must be a non-empty string")
        if not isinstance(uid, str) or not _UID_PATTERN.fullmatch(uid):
            raise ConfigValidationError(
                f"agents[{index}].uid must match ^UID[0-9]{{6,}}$, got {uid!r}"
            )
        if cli_type not in _KNOWN_CLI_TYPES:
            raise ConfigValidationError(
                f"agents[{index}].cli_type must be one of {_KNOWN_CLI_TYPES}, got {cli_type!r}"
            )
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ConfigValidationError(f"agents[{index}].command must be a list of strings")
        if name in seen_names:
            raise ConfigValidationError(f"duplicate agent name: {name!r}")
        if uid in seen_uids:
            raise ConfigValidationError(f"duplicate agent uid: {uid!r}")
        seen_names.add(name)
        seen_uids.add(uid)
        agents.append(
            AgentDefinition(
                name=name, uid=uid, cli_type=cli_type, command=list(command), order_index=index
            )
        )
    return agents


def load(path: Path) -> OrchestratorConfig:
    """Load config.json, applying safe defaults for any missing field."""
    if not path.is_file():
        raise ConfigValidationError(f"config file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise ConfigValidationError(f"could not read config file: {err}") from err
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise ConfigValidationError(f"config file is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise ConfigValidationError("config file must contain a JSON object")

    project_path = data.get("project_path") or None
    if project_path is not None and not isinstance(project_path, str):
        raise ConfigValidationError("project_path must be a string")

    for dir_key in ("logs_dir", "checkpoints_dir", "runtime_dir"):
        if dir_key in data and not isinstance(data[dir_key], str):
            raise ConfigValidationError(f"{dir_key} must be a string")

    config = OrchestratorConfig(
        mail_check_interval_sec=_positive_int(
            data, "mail_check_interval_sec", DEFAULT_MAIL_CHECK_INTERVAL_SEC
        ),
        cli_timeout_sec=_positive_int(data, "cli_timeout_sec", DEFAULT_CLI_TIMEOUT_SEC),
        reply_check_timeout_sec=_positive_int(
            data, "reply_check_timeout_sec", DEFAULT_REPLY_CHECK_TIMEOUT_SEC
        ),
        max_round_trips=_positive_int(data, "max_round_trips", DEFAULT_MAX_ROUND_TRIPS),
        max_retries=_nonnegative_int(data, "max_retries", DEFAULT_MAX_RETRIES),
        max_run_duration_sec=_nonnegative_int(
            data, "max_run_duration_sec", DEFAULT_MAX_RUN_DURATION_SEC
        ),
        agents=_parse_agents(data.get("agents")),
        project_path=project_path,
        logs_dir=data.get("logs_dir", DEFAULT_LOGS_DIR),
        checkpoints_dir=data.get("checkpoints_dir", DEFAULT_CHECKPOINTS_DIR),
        runtime_dir=data.get("runtime_dir", DEFAULT_RUNTIME_DIR),
    )
    return config
