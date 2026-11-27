import unittest
from html.parser import HTMLParser
from pathlib import Path


VIEW = Path(__file__).resolve().parents[1] / "views" / "poke-milestones" / "index.html"


class ViewParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = {}
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"input", "textarea"} and "name" in attributes:
            self.controls[attributes["name"]] = attributes
        if tag == "script":
            self.scripts.append(attributes)


class PokeMilestonesViewTests(unittest.TestCase):
    def setUp(self):
        self.source = VIEW.read_text(encoding="utf-8")
        self.page = ViewParser()
        self.page.feed(self.source)

    def test_editor_fields_and_new_rule_defaults(self):
        controls = self.page.controls
        self.assertEqual(set(controls), {
            "id", "name", "target", "tolerance", "mute_minutes", "enabled", "llm_enabled", "prompt"
        })
        self.assertEqual(controls["tolerance"]["value"], "2")
        self.assertEqual(controls["mute_minutes"]["value"], "1")
        self.assertEqual(controls["target"]["min"], "1")
        self.assertNotIn("checked", controls["llm_enabled"])

    def test_bridge_contract_and_self_contained_scripts(self):
        self.assertIn("window.AstrBotPluginView", self.source)
        self.assertIn(".apiGet(", self.source)
        self.assertIn(".apiPost(", self.source)
        for path in ("state", "settings", "rules/save", "rules/toggle", "rules/delete"):
            self.assertIn(f'"poke-milestones/{path}"', self.source)
        self.assertTrue(self.page.scripts)
        self.assertTrue(all("src" not in script for script in self.page.scripts))
        self.assertNotIn(".innerHTML", self.source)
