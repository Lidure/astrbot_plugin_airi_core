import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import poke_stats


class StatsDayKeyTests(unittest.TestCase):
    def test_before_4am_belongs_to_previous_day(self):
        self.assertEqual(
            poke_stats.stats_day_key(datetime(2026, 9, 20, 3, 59, 59)),
            "2026-09-19",
        )

    def test_4am_starts_new_stats_day(self):
        self.assertEqual(
            poke_stats.stats_day_key(datetime(2026, 9, 20, 4, 0, 0)),
            "2026-09-20",
        )


class DailyStatsTests(unittest.TestCase):
    def test_daily_resets_at_4am_but_history_keeps_accumulating(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = poke_stats.PokeStatsStore(Path(tmp) / "poke_stats.json")
            store.record("g1", "u1", now=datetime(2026, 9, 20, 3, 59, 0))
            store.record("g1", "u1", now=datetime(2026, 9, 20, 4, 0, 0))
            store.record("g1", "u2", now=datetime(2026, 9, 20, 4, 1, 0))

            daily = store.daily_group_summary("g1", now=datetime(2026, 9, 20, 4, 1, 0))
            self.assertEqual(daily["entries"], [("u1", 1), ("u2", 1)])
            self.assertEqual(daily["total_pokes"], 2)

            history = store.group_summary("g1")
            self.assertEqual(history["entries"], [("u1", 2), ("u2", 1)])
            self.assertEqual(history["total_pokes"], 3)

    def test_stale_daily_bucket_reads_as_empty_after_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = poke_stats.PokeStatsStore(Path(tmp) / "poke_stats.json")
            store.record("g1", "u1", now=datetime(2026, 9, 20, 3, 0, 0))
            summary = store.daily_group_summary("g1", now=datetime(2026, 9, 20, 4, 0, 0))
            self.assertEqual(summary["entries"], [])
            self.assertEqual(summary["total_pokes"], 0)

    def test_daily_global_aggregates_only_current_stats_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = poke_stats.PokeStatsStore(Path(tmp) / "poke_stats.json")
            store.record("g1", "u1", now=datetime(2026, 9, 20, 5, 0, 0))
            store.record("g2", "u1", now=datetime(2026, 9, 20, 6, 0, 0))
            store.record("g2", "u2", now=datetime(2026, 9, 20, 6, 1, 0))
            summary = store.daily_global_summary(now=datetime(2026, 9, 20, 7, 0, 0))
            self.assertEqual(summary["entries"], [("u1", 2), ("u2", 1)])
            self.assertEqual(summary["total_pokes"], 3)

    def test_legacy_json_keeps_history_and_starts_daily_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poke_stats.json"
            path.write_text(json.dumps({
                "groups": {"g1": {"users": {"u1": {"count": 7}}, "total": 7}},
                "meta": {"updated_at": "2026-09-19 12:00:00"},
            }), encoding="utf-8")
            store = poke_stats.PokeStatsStore(path)
            self.assertEqual(store.group_summary("g1")["total_pokes"], 7)
            self.assertEqual(
                store.daily_group_summary("g1", now=datetime(2026, 9, 20, 10, 0, 0))["total_pokes"],
                0,
            )
            store.record("g1", "u1", now=datetime(2026, 9, 20, 10, 1, 0))
            self.assertEqual(store.group_summary("g1")["total_pokes"], 8)
            self.assertEqual(
                store.daily_group_summary("g1", now=datetime(2026, 9, 20, 10, 1, 0))["total_pokes"],
                1,
            )


class MainDailyRankingWiringTests(unittest.TestCase):
    def test_four_rank_commands_are_registered(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        for command in ("poke排行", "poke总排行", "poke历史排行", "poke历史总排行"):
            self.assertIn(f'@filter.command("{command}")', source)

    def test_daily_commands_use_daily_summaries_and_history_keeps_cumulative_summaries(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("self._poke_store.daily_group_summary", source)
        self.assertIn("self._poke_store.daily_global_summary", source)
        self.assertIn("self._poke_store.group_summary", source)
        self.assertIn("self._poke_store.global_summary", source)
        self.assertIn("每日 04:00 刷新", source)


if __name__ == "__main__":
    unittest.main()
