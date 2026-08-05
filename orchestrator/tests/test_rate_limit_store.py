import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from rate_limit_store import RateLimitStore


class RateLimitStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = Path(tempfile.mkdtemp())
        self.store = RateLimitStore(self.runtime_dir)

    def test_no_record_means_not_in_cooldown(self) -> None:
        self.assertIsNone(self.store.get("codex_reviewer"))
        self.assertFalse(self.store.is_in_cooldown("codex_reviewer"))

    def test_future_retry_at_is_in_cooldown(self) -> None:
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        self.store.record("codex_reviewer", rule_id="codex.rate_limit.usage_limit", retry_at=future, evidence="e")
        self.assertTrue(self.store.is_in_cooldown("codex_reviewer"))
        record = self.store.get("codex_reviewer")
        self.assertEqual(record.rule_id, "codex.rate_limit.usage_limit")

    def test_past_retry_at_is_not_in_cooldown(self) -> None:
        past = (datetime.utcnow() - timedelta(days=1)).isoformat()
        self.store.record("codex_reviewer", rule_id="codex.rate_limit.usage_limit", retry_at=past, evidence="e")
        self.assertFalse(self.store.is_in_cooldown("codex_reviewer"))

    def test_missing_retry_at_is_treated_as_indefinite_cooldown(self) -> None:
        self.store.record("grok_reviewer", rule_id="grok.rate_limit.usage_limit", retry_at=None, evidence="e")
        self.assertTrue(self.store.is_in_cooldown("grok_reviewer"))

    def test_clear_removes_the_record(self) -> None:
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        self.store.record("codex_reviewer", rule_id="x", retry_at=future, evidence="e")
        self.store.clear("codex_reviewer")
        self.assertIsNone(self.store.get("codex_reviewer"))
        self.assertFalse(self.store.is_in_cooldown("codex_reviewer"))

    def test_persists_across_store_instances(self) -> None:
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        self.store.record("codex_reviewer", rule_id="x", retry_at=future, evidence="e")
        reloaded = RateLimitStore(self.runtime_dir)
        self.assertTrue(reloaded.is_in_cooldown("codex_reviewer"))

    def test_agents_are_independent(self) -> None:
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        self.store.record("codex_reviewer", rule_id="x", retry_at=future, evidence="e")
        self.assertFalse(self.store.is_in_cooldown("grok_reviewer"))


if __name__ == "__main__":
    unittest.main()
