import unittest
from pathlib import Path

from poke_rank_options import (
    DEFAULT_RANK_LIMIT,
    HARD_MAX_RANK_LIMIT,
    normalize_rank_max_limit,
    parse_rank_command_args,
)


class RankLimitConfigTests(unittest.TestCase):
    def test_default_display_limit_is_always_ten(self):
        self.assertEqual(DEFAULT_RANK_LIMIT, 10)

    def test_max_limit_defaults_to_one_hundred_and_is_bounded(self):
        self.assertEqual(normalize_rank_max_limit(None), 100)
        self.assertEqual(normalize_rank_max_limit("50"), 50)
        self.assertEqual(normalize_rank_max_limit(5), 10)
        self.assertEqual(normalize_rank_max_limit(9999), HARD_MAX_RANK_LIMIT)


class RankCommandArgTests(unittest.TestCase):
    def test_no_args_uses_default_ten(self):
        limit, qq, error = parse_rank_command_args(
            "", "", max_limit=80, command_name="poke排行"
        )
        self.assertEqual((limit, qq, error), (10, None, None))

    def test_single_small_number_is_rank_limit(self):
        limit, qq, error = parse_rank_command_args(
            "50", "", max_limit=80, command_name="poke排行"
        )
        self.assertEqual((limit, qq, error), (50, None, None))

    def test_requested_limit_is_capped_by_configured_max(self):
        limit, qq, error = parse_rank_command_args(
            "80", "", max_limit=50, command_name="poke排行"
        )
        self.assertEqual((limit, qq, error), (50, None, None))

    def test_legacy_single_qq_query_still_works(self):
        limit, qq, error = parse_rank_command_args(
            "123456789", "", max_limit=80, command_name="poke排行"
        )
        self.assertEqual((limit, qq, error), (10, "123456789", None))

    def test_limit_and_qq_can_be_used_together(self):
        limit, qq, error = parse_rank_command_args(
            "50", "123456789", max_limit=80, command_name="poke排行"
        )
        self.assertEqual((limit, qq, error), (50, "123456789", None))

    def test_zero_limit_is_rejected(self):
        limit, qq, error = parse_rank_command_args(
            "0", "", max_limit=80, command_name="poke排行"
        )
        self.assertIsNone(limit)
        self.assertIsNone(qq)
        self.assertIn("正整数", error)

    def test_invalid_second_arg_is_rejected_as_qq(self):
        limit, qq, error = parse_rank_command_args(
            "50", "abc", max_limit=80, command_name="poke排行"
        )
        self.assertIsNone(limit)
        self.assertIsNone(qq)
        self.assertIn("QQ", error)


class MainWiringContractTests(unittest.TestCase):
    def test_all_four_commands_accept_two_optional_arguments(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        signatures = (
            "async def poke_rank(self, event: AstrMessageEvent, arg1: str = \"\", arg2: str = \"\")",
            "async def poke_total_rank(self, event: AstrMessageEvent, arg1: str = \"\", arg2: str = \"\")",
            "async def poke_history_rank(self, event: AstrMessageEvent, arg1: str = \"\", arg2: str = \"\")",
            "async def poke_history_total_rank(self, event: AstrMessageEvent, arg1: str = \"\", arg2: str = \"\")",
        )
        for signature in signatures:
            self.assertIn(signature, source)

    def test_per_command_limit_flows_to_name_resolution_and_renderer(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("rank_limit=rank_limit", source)
        self.assertIn("def _rank_user_ids(self, summary: dict[str, Any], rank_limit: int)", source)
        self.assertNotIn("[: self.poke_rank_limit]", source)


if __name__ == "__main__":
    unittest.main()
