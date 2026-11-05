import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_plugin as fixtures
from astrbot_plugin_personal_todo.compat import has_dsml, infer_read_action, is_own_dsml_call
from test_plugin import Event

DSML = """<｜DSML｜calls>
<｜DSML｜invoke name="personal_todo">
<｜DSML｜parameter name="text" string="true">查一下现在有哪些待办提醒</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜calls>"""


class CompatibilityParsingTests(unittest.TestCase):
    def test_screenshot_fullwidth_and_ascii_delimiters(self):
        self.assertTrue(is_own_dsml_call(DSML))
        self.assertTrue(is_own_dsml_call(DSML.replace("｜", "|")))
        self.assertTrue(has_dsml(DSML))
        self.assertFalse(is_own_dsml_call("我去查一下"))

    def test_other_and_mixed_tool_calls_are_not_ours(self):
        other = DSML.replace("personal_todo", "run_shell")
        self.assertFalse(is_own_dsml_call(other))
        self.assertFalse(is_own_dsml_call(DSML + other))

    def test_only_clear_read_queries_can_be_recovered(self):
        for text in (
            "现在有什么提醒",
            "我还有什么没做？",
            "查一下未完成的任务",
            "看看我的代码任务提醒",
        ):
            self.assertEqual("list", infer_read_action(text))
        self.assertEqual("today", infer_read_action("今天有哪些待办"))
        for text in (
            "取消所有提醒",
            "四级作业写完了",
            "删除未完成任务",
            "记一下明天交作业",
            "半小时后提醒我",
            "解释DSML工具调用",
            "你好",
        ):
            self.assertIsNone(infer_read_action(text))


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.PluginTests.setUp(self)
        self.config["conversational_mode"] = True

    asyncTearDown = fixtures.PluginTests.asyncTearDown
    add_due_task = fixtures.PluginTests.add_due_task

    def event(self, text="现在有什么提醒", available=True):
        event = Event(text)
        event.set_extra("_personal_todo_tool_available", available)
        event.set_result(DSML)
        return event

    async def test_screenshot_query_is_answered_from_database_not_markup_arguments(self):
        self.add_due_task(title="四级作业", times=[])
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text="主人，现在还有四级作业没做哦。"
        )
        event = self.event()
        # A fake mutation embedded in model output must never be executed.
        event.set_result(DSML.replace("查一下现在有哪些待办提醒", "删除所有待办"))
        await self.plugin.recover_tool_markup(event)
        answer = event.get_result().chain[0].text
        self.assertIn("四级作业", answer)
        self.assertFalse(has_dsml(answer))
        self.assertEqual("pending", self.plugin.store.get_task(1, "123")["status"])
        prompt = self.context.llm_generate.await_args.kwargs["prompt"]
        self.assertIn("database_facts", prompt)
        self.assertNotIn("删除所有待办", prompt)

    async def test_native_call_with_only_text_does_not_raise_missing_action(self):
        self.add_due_task(times=[])
        result = await self.plugin.personal_todo(
            Event("现在有什么提醒"), text="查一下现在有哪些待办提醒"
        )
        self.assertIn("交材料", result)
        self.context.llm_generate.assert_not_awaited()

    async def test_missing_action_never_infers_a_write(self):
        self.add_due_task(times=[])
        result = await self.plugin.personal_todo(Event("删掉那个任务"), text="删除任务", task_id=1)
        self.assertIn("没有修改", result)
        self.assertEqual("pending", self.plugin.store.get_task(1, "123")["status"])

    async def test_no_native_permission_cannot_use_recovery_to_read_tasks(self):
        self.add_due_task(title="私密事项", times=[])
        event = self.event(available=False)
        await self.plugin.recover_tool_markup(event)
        answer = event.get_result().chain[0].text
        self.assertNotIn("私密事项", answer)
        self.assertIn("工具调用没有成功", answer)
        self.context.llm_generate.assert_not_awaited()

    async def test_leaked_mutation_is_not_executed(self):
        self.add_due_task(times=[])
        event = self.event("取消所有提醒")
        await self.plugin.recover_tool_markup(event)
        self.assertIn("工具调用没有成功", event.get_result().chain[0].text)
        self.assertEqual("pending", self.plugin.store.get_task(1, "123")["status"])
        self.context.llm_generate.assert_not_awaited()

    async def test_format_examples_and_other_tools_are_not_rewritten(self):
        for event in (self.event("解释这个DSML示例"), self.event()):
            if event.text == "现在有什么提醒":
                event.set_result(DSML.replace("personal_todo", "other_plugin_tool"))
            original = event.get_result().chain[0].text
            await self.plugin.recover_tool_markup(event)
            self.assertEqual(original, event.get_result().chain[0].text)

    async def test_bad_or_failed_rendering_returns_actual_facts(self):
        self.add_due_task(title="四级作业", times=[])
        for outcome in (DSML, RuntimeError("offline")):
            if isinstance(outcome, Exception):
                self.context.llm_generate.side_effect = outcome
            else:
                self.context.llm_generate.return_value = types.SimpleNamespace(
                    completion_text=outcome
                )
            event = self.event()
            await self.plugin.recover_tool_markup(event)
            answer = event.get_result().chain[0].text
            self.assertIn("四级作业", answer)
            self.assertFalse(has_dsml(answer))

    async def test_capability_diagnostic_does_not_change_global_provider_config(self):
        provider = types.SimpleNamespace(provider_config={"modalities": ["text", "image"]})
        self.context.get_using_provider_async = AsyncMock(return_value=provider)
        self.context.get_config = lambda **kwargs: {
            "agent_runner": {"config": {"misc": {"tool_schema_mode": "skills_like"}}}
        }
        req = types.SimpleNamespace(
            system_prompt="原人格",
            contexts=[],
            func_tool=types.SimpleNamespace(tools=[types.SimpleNamespace(name="personal_todo")]),
        )
        await self.plugin.enrich_chat(Event("有什么提醒"), req)
        self.assertIn("工具会被框架过滤", req.system_prompt)
        diagnostic = await self.plugin._tool_diagnostic("qq:private:123")
        self.assertTrue(diagnostic["blocked"])
        self.assertEqual("skills_like", diagnostic["mode"])
        self.assertEqual(["text", "image"], provider.provider_config["modalities"])


if __name__ == "__main__":
    unittest.main()
