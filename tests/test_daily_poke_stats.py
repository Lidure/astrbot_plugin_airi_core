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

    def test_group_summary_includes_group_total_rank(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = poke_stats.PokeStatsStore(Path(tmp) / "poke_stats.json")
            now = datetime(2026, 9, 20, 10, 0, 0)
            for _ in range(5):
                store.record("g1", "u1", now=now)
            for _ in range(8):
                store.record("g2", "u2", now=now)
            for _ in range(3):
                store.record("g3", "u3", now=now)

            daily = store.daily_group_summary("g1", now=now)
            self.assertEqual(daily["group_total_rank"], 2)
            self.assertEqual(daily["group_total_groups"], 3)

            history = store.group_summary("g1")
            self.assertEqual(history["group_total_rank"], 2)
            self.assertEqual(history["group_total_groups"], 3)

    def test_group_total_rank_uses_group_id_as_stable_tiebreaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = poke_stats.PokeStatsStore(Path(tmp) / "poke_stats.json")
            now = datetime(2026, 9, 20, 10, 0, 0)
            for _ in range(4):
                store.record("g2", "u2", now=now)
                store.record("g1", "u1", now=now)

            self.assertEqual(store.daily_group_summary("g1", now=now)["group_total_rank"], 1)
            self.assertEqual(store.daily_group_summary("g2", now=now)["group_total_rank"], 2)

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
        renderer_source = (Path(__file__).resolve().parents[1] / "poke_stats.py").read_text(encoding="utf-8")
        self.assertIn("group_total_rank", renderer_source)
        self.assertIn("群排名", renderer_source)
        self.assertNotIn("TOP {self.poke_rank_limit}", source)


class RankingPresentationTests(unittest.TestCase):
    def test_group_subtitle_does_not_repeat_total_or_rank(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('return "当前群 · 今日榜 · 04:00 刷新" if daily else "当前群 · 历史榜"', source)

    def test_group_footer_promotes_total_users_and_group_rank(self):
        summary = {
            "total_pokes": 335,
            "unique_users": 4,
            "entries": [("u1", 128)],
            "group_total_rank": 3,
            "group_total_groups": 27,
            "is_daily": True,
            "is_global_scope": False,
        }
        self.assertEqual(
            poke_stats.build_rank_footer_stats(summary, 10),
            [("今日被戳", "335 次"), ("参与用户", "4 人"), ("群排名", "第 3 / 27")],
        )

    def test_history_group_footer_uses_history_wording(self):
        summary = {
            "total_pokes": 1824,
            "unique_users": 19,
            "entries": [],
            "group_total_rank": 2,
            "group_total_groups": 41,
            "is_daily": False,
            "is_global_scope": False,
        }
        self.assertEqual(
            poke_stats.build_rank_footer_stats(summary, 10),
            [("历史被戳", "1824 次"), ("参与用户", "19 人"), ("群排名", "第 2 / 41")],
        )

    def test_update_label_is_compact_but_keeps_date_for_older_data(self):
        now = datetime(2026, 9, 20, 10, 37, 40)
        self.assertEqual(
            poke_stats.format_rank_updated_label("2026-09-20 10:37:00", now=now),
            "更新 10:37",
        )
        self.assertEqual(
            poke_stats.format_rank_updated_label("2026-09-19 12:00:00", now=now),
            "更新 09-19 12:00",
        )


if __name__ == "__main__":
    unittest.main()
