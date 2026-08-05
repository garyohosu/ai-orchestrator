import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from availability import check_agent_availability, select_available_candidate
from config import AgentDefinition
from rate_limit_store import RateLimitStore
from tests.fakes import FakeCliAdapter


class CheckAgentAvailabilityTests(unittest.TestCase):
    def test_antigravity_is_always_unavailable(self) -> None:
        agent = AgentDefinition(name="antigravity_reviewer", uid="UID000007", cli_type="antigravity")
        result = check_agent_availability(agent, {"antigravity": FakeCliAdapter()})
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "cli.not_installed")

    def test_unregistered_cli_type_is_unavailable(self) -> None:
        agent = AgentDefinition(name="x", uid="UID000009", cli_type="ghost")
        result = check_agent_availability(agent, {})
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "cli.not_installed")

    def test_configured_command_resolving_to_real_file_is_available(self) -> None:
        agent = AgentDefinition(name="x", uid="UID000009", cli_type="fake", command=[sys.executable])
        result = check_agent_availability(agent, {"fake": FakeCliAdapter()})
        self.assertTrue(result.available)

    def test_configured_command_not_found_is_unavailable(self) -> None:
        agent = AgentDefinition(
            name="x", uid="UID000009", cli_type="fake", command=["definitely-nonexistent-cli-xyz-12345"]
        )
        result = check_agent_availability(agent, {"fake": FakeCliAdapter()})
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "cli.command_not_found")

    def test_default_name_not_on_path_is_unavailable(self) -> None:
        agent = AgentDefinition(name="x", uid="UID000009", cli_type="fake")
        result = check_agent_availability(
            agent, {"fake": FakeCliAdapter("definitely-not-a-real-executable-xyz")}
        )
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "cli.not_installed")

    def test_active_cooldown_makes_agent_unavailable(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp())
        store = RateLimitStore(runtime_dir)
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        store.record("codex_reviewer", rule_id="codex.rate_limit.usage_limit", retry_at=future, evidence="e")
        agent = AgentDefinition(name="codex_reviewer", uid="UID000003", cli_type="fake", command=[sys.executable])
        result = check_agent_availability(agent, {"fake": FakeCliAdapter()}, store)
        self.assertFalse(result.available)
        self.assertEqual(result.reason_code, "rate_limit.cooldown_active")

    def test_expired_cooldown_does_not_block_availability(self) -> None:
        runtime_dir = Path(tempfile.mkdtemp())
        store = RateLimitStore(runtime_dir)
        past = (datetime.utcnow() - timedelta(days=1)).isoformat()
        store.record("codex_reviewer", rule_id="codex.rate_limit.usage_limit", retry_at=past, evidence="e")
        agent = AgentDefinition(name="codex_reviewer", uid="UID000003", cli_type="fake", command=[sys.executable])
        result = check_agent_availability(agent, {"fake": FakeCliAdapter()}, store)
        self.assertTrue(result.available)


class SelectAvailableCandidateTests(unittest.TestCase):
    def test_returns_first_available_and_records_all_attempts(self) -> None:
        agents = {
            "codex_reviewer": AgentDefinition(
                name="codex_reviewer", uid="UID000003", cli_type="antigravity"
            ),
            "grok_reviewer": AgentDefinition(
                name="grok_reviewer", uid="UID000006", cli_type="fake", command=[sys.executable]
            ),
        }
        chosen, attempts = select_available_candidate(
            ["codex_reviewer", "grok_reviewer"], agents, {"antigravity": FakeCliAdapter(), "fake": FakeCliAdapter()}
        )
        self.assertEqual(chosen.name, "grok_reviewer")
        self.assertEqual([name for name, _ in attempts], ["codex_reviewer", "grok_reviewer"])
        self.assertFalse(attempts[0][1].available)
        self.assertTrue(attempts[1][1].available)

    def test_no_available_candidate_returns_none_with_reasons(self) -> None:
        agents = {
            "codex_reviewer": AgentDefinition(name="codex_reviewer", uid="UID000003", cli_type="antigravity"),
        }
        chosen, attempts = select_available_candidate(
            ["codex_reviewer"], agents, {"antigravity": FakeCliAdapter()}
        )
        self.assertIsNone(chosen)
        self.assertEqual(len(attempts), 1)
        self.assertFalse(attempts[0][1].available)


if __name__ == "__main__":
    unittest.main()
