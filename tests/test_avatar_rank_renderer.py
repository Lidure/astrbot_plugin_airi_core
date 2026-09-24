import tempfile
import unittest
from pathlib import Path

from PIL import Image

from avatar_rank_renderer import render_poke_rank_image


class FakeCache:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, user_id):
        self.calls.append(str(user_id))
        return self.mapping.get(str(user_id))


class AvatarRankRendererTests(unittest.TestCase):
    def test_avatar_is_pasted_into_first_rank_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            avatar = tmp / "red.png"
            Image.new("RGB", (60, 60), (250, 10, 20)).save(avatar)
            out = tmp / "rank.png"
            cache = FakeCache({"123": str(avatar)})
            summary = {
                "entries": [("123", 5)],
                "display_names": {"123": "Alice"},
                "total_pokes": 5,
                "unique_users": 1,
                "updated_at": None,
                "target_user_id": None,
                "is_daily": True,
                "is_global_scope": True,
            }
            render_poke_rank_image(
                out,
                title="T",
                subtitle="S",
                summary=summary,
                avatar_cache=cache,
            )
            with Image.open(out) as image:
                self.assertEqual(image.getpixel((150, 250)), (250, 10, 20))
            self.assertEqual(cache.calls, ["123"])

    def test_missing_avatar_still_draws_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rank.png"
            cache = FakeCache({})
            summary = {
                "entries": [("123", 5)],
                "display_names": {"123": "Alice"},
                "total_pokes": 5,
                "unique_users": 1,
                "updated_at": None,
                "target_user_id": None,
                "is_daily": True,
                "is_global_scope": True,
            }
            render_poke_rank_image(
                out,
                title="T",
                subtitle="S",
                summary=summary,
                avatar_cache=cache,
            )
            with Image.open(out) as image:
                self.assertNotEqual(image.getpixel((150, 250)), (255, 244, 248))

    def test_only_visible_rows_are_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "rank.png"
            cache = FakeCache({})
            summary = {
                "entries": [("1", 5), ("2", 4), ("3", 3)],
                "display_names": {},
                "total_pokes": 12,
                "unique_users": 3,
                "updated_at": None,
                "target_user_id": None,
                "is_daily": True,
                "is_global_scope": True,
            }
            render_poke_rank_image(
                out,
                title="T",
                subtitle="S",
                summary=summary,
                rank_limit=2,
                avatar_cache=cache,
            )
            self.assertCountEqual(cache.calls, ["1", "2"])

    def test_target_query_card_uses_target_avatar_even_outside_visible_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            avatar = tmp / "blue.png"
            Image.new("RGB", (60, 60), (10, 30, 240)).save(avatar)
            out = tmp / "rank.png"
            cache = FakeCache({"999": str(avatar)})
            summary = {
                "entries": [("123", 5)],
                "display_names": {"123": "Alice", "999": "Bob"},
                "total_pokes": 5,
                "unique_users": 1,
                "updated_at": None,
                "target_user_id": "999",
                "target_user_count": 2,
                "is_daily": True,
                "is_global_scope": False,
                "group_total_rank": 1,
                "group_total_groups": 1,
            }
            render_poke_rank_image(
                out,
                title="T",
                subtitle="S",
                summary=summary,
                avatar_cache=cache,
            )
            # list_top=202, list_h=160, query_y=382; target avatar center=(200,425).
            with Image.open(out) as image:
                self.assertEqual(image.getpixel((200, 425)), (10, 30, 240))
            self.assertCountEqual(cache.calls, ["123", "999"])


if __name__ == "__main__":
    unittest.main()
