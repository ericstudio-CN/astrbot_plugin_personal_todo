"""Native Agent integration: persona, history, tool outcomes and reminders."""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_plugin as fixtures
from test_plugin import NOW, Event, adapter, extraction


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.PluginTests.setUp(self)
        self.config["conversational_mode"] = True

    asyncTearDown = fixtures.PluginTests.asyncTearDown
    command = fixtures.PluginTests.command
    add_due_task = fixtures.PluginTests.add_due_task

    def request(self, *, persona="你是用户的猫娘朋友，称呼用户为主人。", tool=True):
        return types.SimpleNamespace(
            system_prompt=persona,
            contexts=[{"role": "assistant", "content": "作业具体几点截止呀，主人？"}],
            prompt="晚上十一点",
            func_tool=types.SimpleNamespace(
                tools=[types.SimpleNamespace(name="personal_todo")] if tool else []
            ),
        )

    async def test_native_mode_leaves_plain_chat_to_original_persona_pipeline(self):
        self.config.pop("conversational_mode")
        event = Event("有点烦，陪我聊聊天吧")
        self.assertEqual([], [value async for value in self.plugin.auto_capture(event)])
        self.assertFalse(event.stopped)
        self.context.llm_generate.assert_not_awaited()

    async def test_hook_keeps_persona_history_and_exposes_real_task_context(self):
        task = self.add_due_task(times=[])
        req = self.request()
        history = req.contexts
        original = req.system_prompt
        await self.plugin.enrich_chat(Event("我还要做什么？"), req)
        self.assertTrue(req.system_prompt.startswith(original))
        self.assertIs(req.contexts, history)
        self.assertIn(task["title"], req.system_prompt)
        self.assertIn("不要切换成一个插件客服", req.system_prompt)
        req.system_prompt += "\n其他插件保留的系统说明"
        await self.plugin.enrich_chat(Event("我还要做什么？"), req)
        self.assertEqual(1, req.system_prompt.count("<personal_todo_context>"))
        self.assertIn("其他插件保留的系统说明", req.system_prompt)
        self.assertFalse(Event("测试").stopped)
        self.context.llm_generate.assert_not_awaited()

    async def test_owner_only_context_and_tools(self):
        self.add_due_task(times=[])
        req = self.request()
        await self.plugin.enrich_chat(Event("有什么待办", sender="456"), req)
        self.assertNotIn("交材料", req.system_prompt)
        result = await self.plugin.personal_todo(Event("帮我完成", sender="456"), "complete", "", 1)
        self.assertIn("本人账号", result)
        self.assertEqual("pending", self.plugin.store.get_task(1, "123")["status"])

    async def test_missing_persona_tool_is_detected_without_bypassing_selection(self):
        req = self.request(tool=False)
        await self.plugin.enrich_chat(Event("记一下"), req)
        self.assertEqual([], req.func_tool.tools)
        self.assertIn("不得声称已保存", req.system_prompt)
        self.assertIn("personal_todo", req.system_prompt)
        adapter.logger.warning.assert_called()

    async def test_short_answer_resolves_pending_draft_without_command(self):
        first = Event("7号晚上交四级作业", message_id="first")
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text=extraction(
                title="完成四级词汇作业",
                due_at=None,
                due_precision="none",
                needs_confirmation=True,
                question="7号晚上具体几点？",
            )
        )
        result = await self.plugin.personal_todo(first, "add", first.text, 0)
        self.assertIn("暂未保存", result)
        req = self.request()
        answer = Event("晚上十一点", message_id="answer")
        await self.plugin.enrich_chat(answer, req)
        self.assertIn("7号晚上具体几点", req.system_prompt)
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text=extraction(title="完成四级词汇作业", due_at="2026-11-07T23:00:00+08:00")
        )
        result = await self.plugin.personal_todo(answer, "clarify", "晚上十一点", 0)
        self.assertIn("已添加", result)
        self.assertFalse(first.stopped)
        self.assertFalse(answer.stopped)
        self.context.send_message.assert_not_awaited()
        self.assertEqual(1, len(self.plugin.store.list_tasks("123")))
        payload = json.loads(self.context.llm_generate.await_args.kwargs["prompt"])
        self.assertIn("previous_submission", payload)
        self.assertEqual("晚上十一点", payload["user_request"])

    async def test_tool_query_complete_and_postpone_use_real_database(self):
        task = self.add_due_task(title="四级作业", times=[])
        event = Event("四级作业改到后天")
        result = await self.plugin.personal_todo(event, "list", "", 0)
        self.assertIn("四级作业", result)
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text=extraction(due_at="2026-11-08T18:00:00+08:00")
        )
        result = await self.plugin.personal_todo(
            event, "postpone", "改到11月8日18点截止", task["id"]
        )
        self.assertIn("已更新", result)
        self.assertEqual("四级作业", self.plugin.store.get_task(task["id"], "123")["title"])
        result = await self.plugin.personal_todo(Event("那个做完了"), "complete", "", task["id"])
        self.assertIn("已完成", result)
        self.assertEqual([], self.plugin.store.list_reminders(task["id"], "123"))
        self.assertFalse(event.stopped)

    async def test_no_guessing_target_no_creation_from_invalid_ids(self):
        self.add_due_task(times=[])
        for task_id in (0, -1, 1.5, True):
            result = await self.plugin.personal_todo(Event("那个完成了"), "complete", "", task_id)
            self.assertNotIn("已完成", result)
        self.assertEqual("pending", self.plugin.store.get_task(1, "123")["status"])

    async def test_reminder_question_can_be_answered_without_command(self):
        task = self.add_due_task(times=[])
        due = "2026-11-06T23:59:59+08:00"
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text=extraction(
                due_at=due, remind_at=[], needs_confirmation=True, question="想让我过多久提醒？"
            )
        )
        result = await self.plugin.personal_todo(
            Event("晚点提醒"), "remind", "晚点提醒", task["id"]
        )
        self.assertIn("暂未保存", result)
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text=extraction(due_at=due, remind_at=["2026-11-05T12:30:00+08:00"])
        )
        result = await self.plugin.personal_todo(
            Event("半小时后", message_id="answer"), "clarify", "半小时后", 0
        )
        self.assertIn("已更新", result)
        self.assertEqual(task["due_at"], self.plugin.store.get_task(task["id"], "123")["due_at"])
        self.assertEqual(
            NOW + 1800, self.plugin.store.list_reminders(task["id"], "123")[0]["scheduled_at"]
        )

    async def test_discard_draft_does_not_remove_saved_tasks(self):
        self.add_due_task(times=[])
        self.plugin.store.set_meta("draft:123:qq:private:123", "example draft")
        result = await self.plugin.personal_todo(Event("算了不记了"), "discard_draft", "", 0)
        self.assertIn("已放弃", result)
        self.assertEqual(1, len(self.plugin.store.list_tasks("123")))
        self.assertEqual("", self.plugin.store.get_meta("draft:123:qq:private:123"))

    async def test_notification_uses_selected_persona_and_preserves_facts(self):
        self.context.persona_manager = types.SimpleNamespace(
            resolve_selected_persona=AsyncMock(
                return_value=("cat", {"prompt": "你是猫娘，称呼主人。"}, None, False)
            )
        )
        self.context.conversation_manager = types.SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="cid"),
            get_conversation=AsyncMock(return_value=types.SimpleNamespace(persona_id="cat")),
        )
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text="主人，该照顾一下待办啦，喵～"
        )
        task = self.add_due_task(title="四级作业", times=[NOW])
        await self.plugin._tick(NOW)
        text = self.context.send_message.await_args.args[1].text
        self.assertIn("主人，该照顾一下待办啦，喵～", text)
        self.assertIn("#001", text)
        self.assertIn("四级作业", text)
        self.assertIn("2026-11-06", text)
        self.assertNotIn("/待办", text)
        self.assertIn("你是猫娘", self.context.llm_generate.await_args.kwargs["system_prompt"])
        focus = self.plugin.store.get_meta("focus:123:qq:private:123")
        self.assertEqual([task["id"]], focus["ids"])

    async def test_persona_failure_still_delivers_durable_reminder(self):
        self.plugin._persona_prompts["qq:private:123"] = "猫娘人格"
        self.context.llm_generate.side_effect = RuntimeError("model offline")
        self.add_due_task(times=[NOW])
        await self.plugin._tick(NOW)
        self.context.send_message.assert_awaited_once()
        self.assertIn("交材料", self.context.send_message.await_args.args[1].text)
        self.assertEqual([], self.plugin.store.list_reminders(1, "123"))

    async def test_completion_during_persona_generation_stops_stale_reminder(self):
        task = self.add_due_task(times=[NOW])

        async def render_then_complete(origin, facts):
            self.plugin.store.finish_task(task["id"], "123", "done", NOW)
            return "开场\n" + facts

        with patch.object(self.plugin, "_notification_text", side_effect=render_then_complete):
            await self.plugin._tick(NOW)
        self.context.send_message.assert_not_awaited()

    async def test_owner_change_during_persona_generation_prevents_old_account_delivery(self):
        self.add_due_task(times=[NOW])

        async def change_owner(origin, facts):
            self.config["owner_qq"] = "456"
            return facts

        with patch.object(self.plugin, "_notification_text", side_effect=change_owner):
            await self.plugin._tick(NOW)
        self.context.send_message.assert_not_awaited()

    async def test_persona_intro_cannot_replace_immutable_date_facts(self):
        self.plugin._persona_prompts["qq:private:123"] = "猫娘人格"
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text="已完成任务，改到2027年哦"
        )
        facts = "#001 四级作业，2026-11-06截止"
        self.assertEqual(facts, await self.plugin._notification_text("qq:private:123", facts))


if __name__ == "__main__":
    unittest.main()
