import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

from poke_stats import PokeStatsStore
from poke_rank_options import parse_group_rank_limit
from group_rank_renderer import (
    extract_onebot_group_names,
    render_group_rank_image,
    resolve_group_display_names,
)


class GroupStatsTests(unittest.TestCase):
    def test_daily_group_ranking_counts_groups_not_users(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokeStatsStore(Path(tmp) / "poke_stats.json")
            now = datetime(2026, 10, 28, 12, 0)
            for _ in range(3):
                store.record("100", "u1", now=now)
            for _ in range(2):
                store.record("200", "u1", now=now)
            for _ in range(2):
                store.record("200", "u2", now=now)
            daily = store.daily_group_leaderboard(now=now)
            self.assertEqual(daily["entries"], [("200", 4), ("100", 3)])
            self.assertEqual(daily["total_pokes"], 7)
            self.assertEqual(daily["total_groups"], 2)

    def test_daily_cutoff_does_not_erase_historical_group_rank(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokeStatsStore(Path(tmp) / "poke_stats.json")
            before = datetime(2026, 10, 28, 3, 59)
            after = datetime(2026, 10, 28, 4, 0)
            store.record("100", "u1", now=before)
            store.record("200", "u2", now=after)
            self.assertEqual(
                store.daily_group_leaderboard(now=after)["entries"], [("200", 1)]
            )
            self.assertEqual(
                store.historical_group_leaderboard()["entries"],
                [("100", 1), ("200", 1)],
            )

    def test_group_ranking_uses_numeric_group_id_tiebreaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokeStatsStore(Path(tmp) / "poke_stats.json")
            now = datetime(2026, 10, 28, 12)
            store.record("2", "u1", now=now)
            store.record("10", "u2", now=now)
            self.assertEqual(
                store.daily_group_leaderboard(now=now)["entries"],
                [("2", 1), ("10", 1)],
            )

    def test_no_records_is_empty_not_a_phantom_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PokeStatsStore(Path(tmp) / "poke_stats.json")
            self.assertEqual(store.historical_group_leaderboard()["entries"], [])
            self.assertEqual(store.historical_group_leaderboard()["total_groups"], 0)


class GroupRankParamTests(unittest.TestCase):
    def test_group_rank_defaults_to_ten(self):
        self.assertEqual(
            parse_group_rank_limit("", max_limit=50, command_name="poke群排行"),
            (10, None),
        )

    def test_custom_limit_and_config_cap(self):
        self.assertEqual(
            parse_group_rank_limit("50", max_limit=100, command_name="poke群排行"),
            (50, None),
        )
        self.assertEqual(
            parse_group_rank_limit("90", max_limit=30, command_name="poke群排行"),
            (30, None),
        )

    def test_nonpositive_and_nonnumeric_are_rejected(self):
        for value in ("0", "-5", "hello"):
            limit, error = parse_group_rank_limit(
                value, max_limit=100, command_name="poke群排行"
            )
            self.assertIsNone(limit)
            self.assertIn("正整数", error)


class GroupNameTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_common_onebot_group_list_shapes(self):
        payload = {"data": [
            {"group_id": 11, "group_name": "一群"},
            {"group_id": 22, "group_name": "二群"},
        ]}
        self.assertEqual(extract_onebot_group_names(payload), {"11": "一群", "22": "二群"})
        self.assertEqual(
            extract_onebot_group_names({"data": {"groups": [{"group_id": 33, "group_name": "三群"}]}}),
            {"33": "三群"},
        )

    async def test_group_list_names_and_fallback_for_unknown_group(self):
        calls = []
        async def action(method, **kwargs):
            calls.append((method, kwargs))
            if method == "get_group_list":
                return {"data": [{"group_id": 11, "group_name": "可爱一群"}]}
            if method == "get_group_info":
                if kwargs["group_id"] == 22:
                    return {"data": {"group_id": 22, "group_name": "可爱二群"}}
                raise RuntimeError("left group")
            raise AssertionError(method)

        result = await resolve_group_display_names(action, ["11", "22", "33"])
        self.assertEqual(result, {"11": "可爱一群", "22": "可爱二群"})
        self.assertEqual(len([method for method, _ in calls if method == "get_group_list"]), 1)


class GroupRankRendererTests(unittest.TestCase):
    def test_group_rank_renders_fifty_rows_without_qq_avatar_lookup(self):
        entries = [(str(i), 101-i) for i in range(1, 61)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rank.png"
            render_group_rank_image(
                path, title="测试群榜", subtitle="今日 · 04:00 切日",
                summary={
                    "entries": entries,
                    "total_groups": 60,
                    "total_pokes": sum(c for _, c in entries),
                    "display_names": {gid: f"群名称{gid}" for gid, _ in entries},
                    "updated_at": "2026-10-28 12:00:00",
                    "is_daily": True,
                }, rank_limit=50,
            )
            with Image.open(path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertGreater(image.height, 3000)


class MainGroupRankWiringTests(unittest.TestCase):
    def test_both_group_rank_commands_registered_and_use_rank_limit(self):
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command("poke群排行")', src)
        self.assertIn('@filter.command("poke历史群排行")', src)
        self.assertIn("self._poke_store.daily_group_leaderboard()", src)
        self.assertIn("self._poke_store.historical_group_leaderboard()", src)
        self.assertIn("render_group_rank_image", src)
        self.assertIn("rank_limit=rank_limit", src)


if __name__ == "__main__":
    unittest.main()
