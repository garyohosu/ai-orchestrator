"""Pick the first available agent for a role, instead of a human/upstream
agent habitually hardcoding one implementation (e.g. always "codex_reviewer").

Usage: py -3 orchestrator/select_candidate.py <role>

This is intentionally a small standalone script, not new dispatch.py
plumbing: it reuses the exact same config (role_candidates), adapters, and
availability.check_agent_availability() that dispatch.py's own fallback
handoff consults, so "who would be picked first" here matches "who
dispatch.py would fail over to" during a live run.

Scope note: this covers the *human/initial-dispatch* side of role-based
selection only. director.py's own state machine still delegates to a fixed
UID from the request mail (see docs/ai-director-report.md's role-separation
discussion) -- teaching director.py itself to resolve a role to a UID
before delegating is a larger, separate change to its spec'd state machine
and is intentionally out of scope here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import config as config_module
from adapters import build_adapters
from availability import select_available_candidate
from rate_limit_store import RateLimitStore


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: py -3 orchestrator/select_candidate.py <role>", file=sys.stderr)
        return 2
    role = sys.argv[1]

    cfg = config_module.load(_HERE / "config.json")
    candidate_names = cfg.role_candidates.get(role)
    if not candidate_names:
        print(json.dumps({"error": f"no role_candidates configured for role {role!r}"}))
        return 1

    adapters = build_adapters()
    agents_by_name = {a.name: a for a in cfg.agents}
    rate_limit_store = RateLimitStore(_HERE / cfg.runtime_dir)

    chosen, attempts = select_available_candidate(candidate_names, agents_by_name, adapters, rate_limit_store)

    result = {
        "role": role,
        "chosen": {"name": chosen.name, "uid": chosen.uid} if chosen is not None else None,
        "attempts": [
            {
                "agent": name,
                "available": availability.available,
                "reason_code": availability.reason_code,
                "detail": availability.detail,
            }
            for name, availability in attempts
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if chosen is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
