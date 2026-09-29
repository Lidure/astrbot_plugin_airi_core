import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LlmMentionToolTests(unittest.TestCase):
    def test_mention_tool_is_configurable_and_registered_only_when_enabled(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('self.config.get("mention_tool_enabled", True)', source)
        self.assertIn("class MentionTool(FunctionTool)", source)
        self.assertIn('name: str = "mention_user"', source)
        self.assertIn("if self.mention_tool_enabled:", source)
        self.assertIn("self.context.add_llm_tools(MentionTool(plugin=self))", source)

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("mention_tool_enabled", schema)
        self.assertEqual(schema["mention_tool_enabled"]["type"], "bool")
        self.assertIs(schema["mention_tool_enabled"]["default"], True)

    def test_mention_tool_records_target_and_validates_onebot_group_member(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("class MentionTool(FunctionTool)")
        end = source.index("class Main(Star)", start)
        tool_source = source[start:end]

        self.assertIn('platform_name != "aiocqhttp"', tool_source)
        self.assertIn("group_id", tool_source)
        self.assertIn("user_id.isdigit()", tool_source)
        self.assertIn("event.get_self_id()", tool_source)
        self.assertIn('"get_group_member_info"', tool_source)
        self.assertIn('event.set_extra("airi_mention_target", user_id)', tool_source)

    def test_final_reply_is_decorated_with_at_without_dynamic_prompt_injection(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("@filter.on_decorating_result()", source)
        self.assertIn("async def decorate_mention_result", source)

        start = source.index("async def decorate_mention_result")
        end = source.index("async def on_notice_event", start)
        handler_source = source[start:end]

        self.assertIn('event.get_extra("airi_mention_target")', handler_source)
        self.assertIn("event.get_result()", handler_source)
        self.assertIn("Comp.At(qq=target_qq)", handler_source)
        self.assertIn("result.chain.insert(0", handler_source)
        self.assertNotIn("system_prompt", handler_source)
        self.assertNotIn("extra_user_content_parts", handler_source)


if __name__ == "__main__":
    unittest.main()
