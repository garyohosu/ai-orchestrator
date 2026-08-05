"""Pre-launch availability checks for agents.

Used two ways:
  1. By a candidate-selection helper (see director's/human_controller's
     role-based dispatch) to pick the first plausibly-usable agent for a
     role instead of hardcoding one.
  2. By dispatch.py's fallback handoff (DispatchCycle._handoff_rate_limited)
     to skip candidates that are obviously not worth trying.

This is explicitly a pre-screen, not a guarantee (SPEC intent, per this
project's own design instructions): "ヘルスチェックだけで完全な利用可否を
断定できない場合は、実行時の失敗を正式な判定としてください" -- a live
launch attempt is always the authoritative result. A candidate this module
reports "available" can still fail at launch time (and will be classified
and possibly failed-over again); a candidate it reports "unavailable" is
only ever *skipped*, never treated as a confirmed permanent failure.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import error_taxonomy as et
from adapters.base import CliAdapter
from config import AgentDefinition
from rate_limit_store import RateLimitStore


@dataclass(frozen=True)
class AvailabilityResult:
    available: bool
    reason_code: str | None = None
    detail: str | None = None


def check_agent_availability(
    agent: AgentDefinition,
    adapters: dict[str, CliAdapter],
    rate_limit_store: RateLimitStore | None = None,
) -> AvailabilityResult:
    # Antigravity has no working CLI/API/app connection method identified
    # in this environment yet (adapters/antigravity.py). This is a fixed
    # "not installed" fact of the current environment, not a probe result --
    # reported unconditionally so it is never silently chosen, and so the
    # day a real connection method exists, only this one check (plus the
    # adapter itself) needs to change.
    if agent.cli_type == "antigravity":
        return AvailabilityResult(
            available=False,
            reason_code=et.CLI_NOT_INSTALLED,
            detail="Antigravity has no CLI/API/app connection configured in this environment yet",
        )

    adapter = adapters.get(agent.cli_type)
    if adapter is None:
        return AvailabilityResult(
            available=False,
            reason_code=et.CLI_NOT_INSTALLED,
            detail=f"no adapter registered for cli_type {agent.cli_type!r}",
        )

    if agent.command:
        exe = agent.command[0]
        exe_path = Path(exe)
        resolvable = shutil.which(exe) is not None or (exe_path.is_absolute() and exe_path.is_file())
        if not resolvable:
            return AvailabilityResult(
                available=False,
                reason_code=et.CLI_COMMAND_NOT_FOUND,
                detail=f"configured command {exe!r} not found",
            )
    else:
        default_name = adapter.default_command_name()
        if shutil.which(default_name) is None:
            return AvailabilityResult(
                available=False,
                reason_code=et.CLI_NOT_INSTALLED,
                detail=f"{default_name!r} not found on PATH",
            )

    if rate_limit_store is not None and rate_limit_store.is_in_cooldown(agent.name):
        record = rate_limit_store.get(agent.name)
        return AvailabilityResult(
            available=False,
            reason_code="rate_limit.cooldown_active",
            detail=(
                f"rule_id={record.rule_id!r} retry_at={record.retry_at!r}"
                if record is not None
                else "cooldown active"
            ),
        )

    return AvailabilityResult(available=True)


def select_available_candidate(
    candidate_names: list[str],
    agents_by_name: dict[str, AgentDefinition],
    adapters: dict[str, CliAdapter],
    rate_limit_store: RateLimitStore | None = None,
) -> tuple[AgentDefinition | None, list[tuple[str, AvailabilityResult]]]:
    """Return (first available agent or None, [(name, result), ...] for all candidates tried).

    The second element records *why* each candidate was skipped (or
    accepted), for HUMAN_REQUIRED notifications and handoff history --
    section 14 of this project's failover requirements: "未試行候補がある
    場合は、その理由" must always be reconstructable.
    """
    attempts: list[tuple[str, AvailabilityResult]] = []
    for name in candidate_names:
        agent = agents_by_name.get(name)
        if agent is None:
            attempts.append((name, AvailabilityResult(False, et.CLI_NOT_INSTALLED, "unknown agent name")))
            continue
        result = check_agent_availability(agent, adapters, rate_limit_store)
        attempts.append((name, result))
        if result.available:
            return agent, attempts
    return None, attempts
