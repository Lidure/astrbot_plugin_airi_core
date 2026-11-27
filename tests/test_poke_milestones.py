import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from poke_milestones import PokeMilestoneStore, effective_threshold


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "poke_milestones.json"
        self.store = PokeMilestoneStore(self.path)
        self.day = "2026-11-27"

    def rule(self, **changes):
        return self.store.save_rule(dict({"name": "午休", "target": 10}, **changes))

    def claim(self, rule):
        return self.store.claim(rule, "123", "456", self.day, 10)

    def test_defaults_and_validation(self):
        self.assertEqual(self.store.snapshot(), {"enabled": False, "rules": [], "history": []})
        rule = self.rule()
        self.assertEqual((rule["tolerance"], rule["mute_minutes"], rule["llm_enabled"]), (2, 1, False))
        for change in ({"target": True}, {"target": 0}, {"tolerance": -1},
                       {"mute_minutes": 0}, {"mute_minutes": 43201}, {"llm_enabled": "off"},
                       {"enabled": 1}, {"name": "  "}, {"prompt": []}, {"command": "/ban"},
                       {"name": "x" * 41}, {"prompt": "x" * 1001}, {"target": 1000001},
                       {"tolerance": 10001}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.rule(**change)
        self.assertEqual(len(self.store.snapshot()["rules"]), 1)

    def test_threshold_stable_across_restart_and_not_always_lower_bound(self):
        rule = self.rule(target=1, tolerance=2)
        values = {effective_threshold(rule, "g", str(u), self.day) for u in range(150)}
        self.assertEqual(values, {1, 2, 3})
        saved = PokeMilestoneStore(self.path).snapshot()["rules"][0]
        self.assertEqual(effective_threshold(rule, "g", "u", self.day),
                         effective_threshold(saved, "g", "u", self.day))

    def test_concurrent_claim_confirmed_failure_retry_and_terminal_success(self):
        rule = self.rule(tolerance=0)
        self.assertIsNone(self.claim(rule))
        self.store.set_enabled(True)
        self.assertEqual(self.store.matching_rules("123", "456", self.day, 9), [])
        with ThreadPoolExecutor(6) as pool:
            ids = list(pool.map(lambda _: self.claim(rule), range(6)))
        first, = [key for key in ids if key]
        self.store.finish(first, "failed", "rejected")
        second = self.claim(rule)
        self.assertTrue(second)
        self.assertNotEqual(first, second)
        self.store.finish(second, "success")
        self.store = PokeMilestoneStore(self.path)
        self.assertIsNone(self.claim(rule))
        self.assertEqual([h["status"] for h in self.store.snapshot()["history"]], ["success", "failed"])

    def test_batch_reservation_is_atomic_and_disable_blocks_new_claims(self):
        rules = [self.rule(tolerance=0), self.rule(tolerance=0, mute_minutes=3)]
        self.store.set_enabled(True)
        with patch("poke_milestones.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.claim_many(rules, "123", "456", self.day, 10)
        self.assertEqual(self.store.snapshot()["history"], [])
        claims = self.store.claim_many(rules, "123", "456", self.day, 10)
        self.assertEqual(len(claims), 2)
        self.assertEqual(len(self.store.snapshot()["history"]), 2)
        self.store.set_enabled(False)
        self.assertEqual(self.store.claim_many(rules, "123", "999", self.day, 10), [])
        reloaded = PokeMilestoneStore(self.path)
        self.assertTrue(all(row["status"] == "unknown" for row in reloaded.snapshot()["history"]))

    def test_crash_after_claim_prevents_replay(self):
        rule = self.rule(tolerance=0)
        self.store.set_enabled(True)
        self.assertTrue(self.claim(rule))
        self.store = PokeMilestoneStore(self.path)
        self.assertIsNone(self.claim(rule))
        self.assertEqual(self.store.snapshot()["history"][0]["status"], "unknown")

    def test_stale_rules_and_write_failure_do_not_claim(self):
        rule = self.rule(tolerance=0)
        self.store.set_enabled(True)
        self.store.set_rule_enabled(rule["id"], False)
        self.assertIsNone(self.claim(rule))
        self.store.set_rule_enabled(rule["id"], True)
        with patch("poke_milestones.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.claim(rule)
        self.assertEqual(self.store.snapshot()["history"], [])

    def test_recent_history_cap_does_not_expire_current_day_dedup(self):
        rule = self.rule(tolerance=0)
        self.store.set_enabled(True)
        for user in range(101):
            key = self.store.claim(rule, "1", str(user), self.day, 10)
            self.store.finish(key, "success")
        self.store = PokeMilestoneStore(self.path)
        self.assertEqual(len(self.store.snapshot()["history"]), 100)
        self.assertIsNone(self.store.claim(rule, "1", "0", self.day, 10))
        self.assertIsNotNone(self.store.claim(rule, "1", "0", "2026-11-28", 10))

    def test_corrupt_nested_rule_is_rejected_without_rewriting(self):
        rule = self.rule()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["rules"][rule["id"]]["tolerance"] = -1
        before = json.dumps(data)
        self.path.write_text(before, encoding="utf-8")
        with self.assertRaises(ValueError):
            PokeMilestoneStore(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_edit_toggle_delete_and_corrupt_file(self):
        rule = self.rule()
        changed = self.store.save_rule({**rule, "name": "安静", "prompt": "轻声"})
        self.assertEqual(changed["id"], rule["id"])
        self.store.delete_rule(rule["id"])
        with self.assertRaises(KeyError):
            self.store.set_rule_enabled(rule["id"], True)
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            PokeMilestoneStore(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{")
