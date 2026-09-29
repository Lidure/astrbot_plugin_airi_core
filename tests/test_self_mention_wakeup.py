import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SelfMentionWakeupTests(unittest.TestCase):
    def test_self_mention_uses_explicit_llm_request_with_current_conversation(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn("async def on_self_mention", source)
        self.assertIn("isinstance(component, Comp.At)", source)
        self.assertIn("str(component.qq) == self_id", source)
        self.assertIn("get_curr_conversation_id", source)
        self.assertIn("new_conversation", source)
        self.assertIn("get_conversation", source)
        self.assertIn("yield event.request_llm(", source)
        self.assertIn("conversation=conversation", source)
        self.assertIn("event.should_call_llm(True)", source)

    def test_self_mention_does_not_inject_an_extra_system_prompt(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("async def on_self_mention")
        end = source.index("async def on_notice_event", start)
        handler_source = source[start:end]

        self.assertNotIn("system_prompt=", handler_source)
        self.assertNotIn("contexts=", handler_source)

    def test_self_mention_wakeup_can_be_disabled_and_defaults_to_enabled(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn(
            'self.config.get("self_mention_wakeup_enabled", True)',
            source,
        )

        start = source.index("async def on_self_mention")
        end = source.index("async def on_notice_event", start)
        handler_source = source[start:end]
        self.assertIn("if not self.self_mention_wakeup_enabled:", handler_source)

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("self_mention_wakeup_enabled", schema)
        self.assertEqual(schema["self_mention_wakeup_enabled"]["type"], "bool")
        self.assertIs(schema["self_mention_wakeup_enabled"]["default"], True)


if __name__ == "__main__":
    unittest.main()
