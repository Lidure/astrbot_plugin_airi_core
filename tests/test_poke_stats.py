import tempfile
import unittest
from pathlib import Path

import poke_stats


class PokeStatsFeatureContractTests(unittest.TestCase):
    def test_poke_stats_module_exists(self):
        module_path = Path(__file__).resolve().parents[1] / "poke_stats.py"
        self.assertTrue(module_path.exists())

    def test_public_api_is_available(self):
        self.assertTrue(hasattr(poke_stats, "parse_bot_poke_notice"))
        self.assertTrue(hasattr(poke_stats, "PokeStatsStore"))
        self.assertTrue(hasattr(poke_stats, "render_poke_rank_image"))
        self.assertTrue(hasattr(poke_stats, "extract_onebot_profile_name"))


class ParsePokeNoticeTests(unittest.TestCase):
    def test_accepts_only_pokes_targeting_bot(self):
        raw = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": 10001,
            "user_id": 20002,
            "target_id": 30003,
            "self_id": 30003,
        }
        self.assertEqual(poke_stats.parse_bot_poke_notice(raw), ("10001", "20002"))

    def test_rejects_member_to_member_poke_and_self_poke(self):
        member_poke = {
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "group_id": 1, "user_id": 2, "target_id": 3, "self_id": 9,
        }
        self_poke = {
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "group_id": 1, "user_id": 9, "target_id": 9, "self_id": 9,
        }
        self.assertIsNone(poke_stats.parse_bot_poke_notice(member_poke))
        self.assertIsNone(poke_stats.parse_bot_poke_notice(self_poke))


class PokeStatsStoreTests(unittest.TestCase):
    def test_records_group_and_global_rankings_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poke_stats.json"
            store = poke_stats.PokeStatsStore(path)
            store.record("g1", "u1")
            store.record("g1", "u2")
            store.record("g1", "u1")
            store.record("g2", "u2")
            store.record("g2", "u2")
            store.record("g2", "u1")

            group = store.group_summary("g1", target_user_id="u2")
            self.assertEqual(group["entries"], [("u1", 2), ("u2", 1)])
            self.assertEqual(group["total_pokes"], 3)
            self.assertEqual(group["unique_users"], 2)
            self.assertEqual(group["target_user_count"], 1)

            total = store.global_summary(target_user_id="u2")
            self.assertEqual(total["entries"], [("u1", 3), ("u2", 3)])
            self.assertEqual(total["total_pokes"], 6)
            self.assertEqual(total["unique_users"], 2)
            self.assertEqual(total["target_user_count"], 3)

            reloaded = poke_stats.PokeStatsStore(path)
            self.assertEqual(reloaded.global_summary()["total_pokes"], 6)

    def test_corrupt_file_is_recovered_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "poke_stats.json"
            path.write_text("{broken", encoding="utf-8")
            store = poke_stats.PokeStatsStore(path)
            self.assertEqual(store.global_summary()["total_pokes"], 0)
            store.record("g1", "u1")
            self.assertNotEqual(path.read_text(encoding="utf-8"), "{broken")


class OneBotProfileNameTests(unittest.TestCase):
    def test_group_card_has_priority_over_nickname(self):
        payload = {"data": {"card": "群昵称", "nickname": "QQ昵称"}}
        self.assertEqual(poke_stats.extract_onebot_profile_name(payload, prefer_card=True), "群昵称")

    def test_empty_card_falls_back_to_nickname(self):
        payload = {"card": "", "nickname": "QQ昵称"}
        self.assertEqual(poke_stats.extract_onebot_profile_name(payload, prefer_card=True), "QQ昵称")

    def test_global_profile_uses_nickname(self):
        payload = {"data": {"card": "群昵称", "nickname": "QQ昵称"}}
        self.assertEqual(poke_stats.extract_onebot_profile_name(payload, prefer_card=False), "QQ昵称")

    def test_invalid_profile_returns_empty_string(self):
        self.assertEqual(poke_stats.extract_onebot_profile_name(None, prefer_card=True), "")


class PrivacyLabelTests(unittest.TestCase):
    def test_prefers_visible_name_over_user_id(self):
        self.assertEqual(poke_stats.privacy_safe_label("123456789", "爱莉"), "爱莉")

    def test_fallback_never_contains_full_user_id(self):
        label = poke_stats.privacy_safe_label("123456789", "")
        self.assertTrue(label.startswith("匿名用户 "))
        self.assertNotIn("123456789", label)


class CenteredTextPositionTests(unittest.TestCase):
    def test_centering_accounts_for_bbox_origin_offset(self):
        class FakeDraw:
            def textbbox(self, xy, text, font=None):
                return (2, 5, 12, 21)

        x, y = poke_stats.center_text_xy(FakeDraw(), "1", object(), (20, 30))
        self.assertEqual(x, 13.0)
        self.assertEqual(y, 17.0)


class PokeRankRendererTests(unittest.TestCase):
    def test_renders_png_rank_card(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "rank.png"
            summary = {
                "entries": [("10001", 12), ("10002", 8), ("10003", 3)],
                "display_names": {
                    "10001": "桃井爱莉",
                    "10002": "群昵称小明",
                    "10003": "",
                },
                "total_pokes": 23,
                "unique_users": 3,
                "target_user_id": "10002",
                "target_user_count": 8,
                "updated_at": "2026-09-20 09:30:00",
            }
            result = poke_stats.render_poke_rank_image(
                output,
                title="Airi Poke 排行榜",
                subtitle="当前群 · Top 10",
                summary=summary,
                rank_limit=10,
            )
            self.assertEqual(Path(result), output)
            self.assertTrue(output.is_file())
            with Image.open(output) as image:
                self.assertEqual(image.format, "PNG")
                self.assertGreaterEqual(image.width, 800)
                self.assertGreaterEqual(image.height, 500)

    def test_renderer_source_does_not_draw_raw_qq_number(self):
        source = (Path(__file__).resolve().parents[1] / "poke_stats.py").read_text(encoding="utf-8")
        self.assertNotIn('f"QQ {user_id}"', source)
        self.assertNotIn('f"QQ {target_user_id}', source)


class MainPrivacyWiringTests(unittest.TestCase):
    def test_main_resolves_names_and_does_not_put_group_id_in_card_subtitle(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('"get_group_member_info"', source)
        self.assertIn('"get_stranger_info"', source)
        self.assertIn('summary["display_names"]', source)
        self.assertNotIn('subtitle=f"当前群 {group_id}', source)


if __name__ == "__main__":
    unittest.main()
