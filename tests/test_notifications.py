import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_plugin as fixtures
from astrbot_plugin_personal_todo.notifications import (
    agrees_to_expand,
    deadline_hint,
    notification_payload,
    parse_notification,
    requests_all,
    select_preview,
)
from test_plugin import Event, adapter, stamp

NOW = stamp("2026-11-07T21:00:00+08:00")


def row(task_id, title, due, **changes):
    result = {
        "id": task_id,
        "title": title,
        "due_at": stamp(due) if due else None,
        "due_precision": "datetime" if due else "none",
        "importance": "normal",
        "status": "pending",
    }
    result.update(changes)
    return result


class NotificationLogicTests(unittest.TestCase):
    def test_deadline_window_excludes_future_old_and_undated(self):
        tasks = [
            row(1, "今天", "2026-11-07T23:00:00+08:00"),
            row(2, "明天", "2026-11-08T23:00:00+08:00"),
            row(3, "下周课程", "2026-11-22T23:00:00+08:00"),
            row(4, "很早逾期", "2026-11-01T12:00:00+08:00"),
            row(5, "没写日期", None),
            row(6, "刚逾期", "2026-11-06T12:00:00+08:00"),
        ]
        result = select_preview(tasks, NOW)
        self.assertEqual([6, 1, 2], [r["id"] for r in result["visible"]])
        self.assertEqual({3, 4, 5}, {r["id"] for r in result["hidden"]})

    def test_boundaries_limit_and_completed_tasks(self):
        tasks = [
            row(1, "恰好48小时", "2026-11-05T21:00:00+08:00"),
            row(2, "早一秒", "2026-11-05T20:59:59+08:00"),
            row(3, "后天零点", "2026-11-09T00:00:00+08:00"),
            row(4, "已做完", "2026-11-08T12:00:00+08:00", status="done"),
        ]
        result = select_preview(tasks, NOW, limit=1)
        self.assertEqual([1], [r["id"] for r in result["visible"]])
        self.assertEqual({2, 3}, {r["id"] for r in result["hidden"]})

    def test_natural_body_is_not_an_opening_plus_a_fixed_table(self):
        tasks = [row(1, "英语作业", "2026-11-08T23:00:00+08:00")]
        raw = json.dumps(
            {
                "opening": "主人，先惦记一下英语那件事呀。",
                "items": [{"id": 1, "text": "英语作业明天 23:00就截止啦，留点时间慢慢收尾。"}],
                "closing": "另外还有7项，想让我也一起告诉你嘛？",
            },
            ensure_ascii=False,
        )
        text = parse_notification(raw, tasks, NOW, 7)
        self.assertIn("主人", text)
        self.assertNotIn("#001", text)
        self.assertNotIn("截止：", text)
        self.assertNotIn("未完成待办汇总", text)
        self.assertIn("7项", text)

    def test_model_cannot_change_task_ids_deadline_or_skip_expansion_question(self):
        tasks = [row(1, "英语作业", "2026-11-08T23:00:00+08:00")]
        for item, closing in (
            ({"id": 9, "text": "明天 23:00"}, "还有7项，要看吗？"),
            ({"id": 1, "text": "明天 22:00"}, "还有7项，要看吗？"),
            ({"id": 1, "text": "明天 23:00，22:00提醒"}, "还有7项，要看吗？"),
            ({"id": 1, "text": "明天 23:00"}, "好了"),
        ):
            with self.subTest(item=item, closing=closing), self.assertRaises(ValueError):
                parse_notification(json.dumps({"items": [item], "closing": closing}), tasks, NOW, 7)

    def test_payload_never_contains_hidden_task_names(self):
        visible = [row(1, "明天要做", "2026-11-08T23:00:00+08:00")]
        self.assertEqual("明天 23:00", deadline_hint(visible[0], NOW))
        payload = notification_payload(visible, NOW, 10, "summary")
        self.assertEqual(10, payload["hidden_count"])
        self.assertEqual(1, len(payload["tasks"]))

    def test_declining_or_negating_is_never_agreement(self):
        for answer in ("好", "好呀", "全部给我看看"):
            self.assertTrue(agrees_to_expand(answer))
        for answer in ("不用", "不需要全部", "不要全都发来", "全部不用发", "先别列完整清单"):
            self.assertFalse(agrees_to_expand(answer), answer)
            self.assertFalse(requests_all(answer), answer)


class NotificationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.PluginTests.setUp(self)
        self.config.update(conversational_mode=True, daily_summary_enabled=True)
        adapter.time.time.return_value = NOW
        self.plugin.store.set_meta("origin:123", "qq:private:123")
        self.plugin._persona_prompts["qq:private:123"] = "你是爱撒娇的猫娘，叫用户主人。"

    asyncTearDown = fixtures.PluginTests.asyncTearDown

    def add(self, title, due, *, times=None):
        item = row(0, title, due)
        item.update(importance_reason="测试", source_excerpt="测试", reminder_times=times or [])
        rows, _ = self.plugin.store.add_tasks("123", "qq:private:123", title, [item], NOW)
        return rows[0]

    def seed(self):
        near = self.add("英语单元测试", "2026-11-08T23:00:00+08:00")
        for number in range(7):
            self.add(f"下周远期课程{number}", "2026-11-22T23:00:00+08:00")
        return near

    def natural_reply(self, task_id=1, hidden=7):
        return types.SimpleNamespace(
            completion_text=json.dumps(
                {
                    "opening": "主人，英语这件事先记着呀。",
                    "items": [
                        {"id": task_id, "text": "英语单元测试明天 23:00截止，别拖到最后才着急嘛。"}
                    ],
                    "closing": f"还有{hidden}项暂时没展开，要我把完整清单也告诉你吗？"
                    if hidden
                    else "",
                },
                ensure_ascii=False,
            )
        )

    async def test_screenshot_summary_filters_then_renders_whole_persona_body(self):
        self.seed()
        self.context.llm_generate.return_value = self.natural_reply()
        await self.plugin._daily_summary(NOW)
        text = self.context.send_message.await_args.args[1].text
        self.assertIn("主人", text)
        self.assertIn("明天 23:00", text)
        self.assertIn("7项", text)
        self.assertNotIn("下周远期课程", text)
        self.assertNotIn("未完成待办汇总", text)
        self.assertNotIn("#001", text)
        payload = json.loads(self.context.llm_generate.await_args.kwargs["prompt"])
        self.assertEqual(1, len(payload["tasks"]))
        self.assertEqual(7, payload["hidden_count"])
        self.assertEqual(8, len(self.plugin.store.list_tasks("123")))

    async def test_switch_can_restore_fixed_format_without_revealing_far_tasks(self):
        self.seed()
        self.config["fixed_notification_format"] = True
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text="主人，先看临近的这一件呀。"
        )
        await self.plugin._daily_summary(NOW)
        text = self.context.send_message.await_args.args[1].text
        self.assertIn("未完成待办汇总", text)
        self.assertIn("#001", text)
        self.assertNotIn("下周远期课程", text)
        self.assertIn("7项", text)
        self.assertIn("人格自然对话", self.plugin._summary_settings("自然"))
        self.assertFalse(self.plugin._fixed_notifications())

    async def test_plain_yes_expands_only_after_a_recent_actual_offer(self):
        self.seed()
        self.context.llm_generate.return_value = self.natural_reply()
        await self.plugin._daily_summary(NOW)
        self.plugin.store.close()
        self.plugin.store = adapter.TaskStore(self.path)
        event = Event("好", message_id="reply")
        result = await self.plugin.personal_todo(event, "list")
        self.assertIn("下周远期课程", result)
        self.assertIsNone(self.plugin.store.get_meta("more_offer:123:qq:private:123"))
        self.assertEqual(8, len(self.plugin.store.list_tasks("123")))

    async def test_unrelated_message_does_not_leave_a_stale_yes_permission(self):
        self.seed()
        self.context.llm_generate.return_value = self.natural_reply()
        await self.plugin._daily_summary(NOW)
        req = types.SimpleNamespace(
            system_prompt="原人格",
            func_tool=types.SimpleNamespace(tools=[types.SimpleNamespace(name="personal_todo")]),
        )
        await self.plugin.enrich_chat(Event("今天天气真好", message_id="chat"), req)
        result = await self.plugin.personal_todo(Event("好", message_id="later"), "list")
        self.assertNotIn("下周远期课程", result)

    async def test_default_query_is_short_but_explicit_all_is_complete(self):
        self.seed()
        brief = await self.plugin.personal_todo(Event("还有什么没做", message_id="q1"), "list")
        self.assertIn("英语单元测试", brief)
        self.assertNotIn("下周远期课程", brief)
        all_tasks = await self.plugin.personal_todo(Event("全部给我看看", message_id="q2"), "list")
        self.assertIn("下周远期课程", all_tasks)

    async def test_no_refuses_expansion_and_stops_the_same_invitation(self):
        self.seed()
        self.context.llm_generate.return_value = self.natural_reply()
        await self.plugin._daily_summary(NOW)
        result = await self.plugin.personal_todo(Event("不需要全部", message_id="no"), "list")
        self.assertNotIn("下周远期课程", result)
        self.assertNotIn("是否想看", result)
        self.assertIsNone(self.plugin.store.get_meta("more_offer:123:qq:private:123"))

    async def test_all_today_keeps_the_requested_date_scope(self):
        self.seed()
        self.add("今天英语复习", "2026-11-07T23:00:00+08:00")
        result = await self.plugin.personal_todo(
            Event("今天所有待办都告诉我", message_id="today-all"), "today"
        )
        self.assertIn("今天英语复习", result)
        self.assertNotIn("英语单元测试", result)
        self.assertNotIn("下周远期课程", result)

    async def test_expired_invitation_does_not_turn_yes_into_all(self):
        self.seed()
        self.context.llm_generate.return_value = self.natural_reply()
        await self.plugin._daily_summary(NOW)
        adapter.time.time.return_value = NOW + 7201
        result = await self.plugin.personal_todo(Event("好", message_id="late"), "list")
        self.assertNotIn("下周远期课程", result)

    async def test_chat_format_change_saves_the_same_setting_as_configuration_ui(self):
        class Config(dict):
            saved = 0

            def save_config(self):
                self.saved += 1

        self.config = self.plugin.config = Config(self.config)
        self.plugin._summary_settings("固定")
        self.assertTrue(self.config["fixed_notification_format"])
        self.assertEqual(1, self.config.saved)
        self.assertTrue(self.plugin._fixed_notifications())
        self.config["fixed_notification_format"] = False
        self.assertFalse(self.plugin._fixed_notifications())

    async def test_model_failure_uses_a_brief_preview_not_the_old_long_table(self):
        self.seed()
        self.context.llm_generate.side_effect = RuntimeError("offline")
        await self.plugin._daily_summary(NOW)
        text = self.context.send_message.await_args.args[1].text
        self.assertIn("英语单元测试", text)
        self.assertIn("7项", text)
        self.assertNotIn("下周远期课程", text)
        self.assertNotIn("未完成待办汇总", text)

    async def test_due_batches_are_collapsed_once_and_can_be_expanded(self):
        self.config["daily_summary_enabled"] = False
        self.config["persona_notifications"] = False
        for n in range(5):
            self.add(f"自定义提醒{n}", "2026-11-22T23:00:00+08:00", times=[NOW])
        await self.plugin._tick(NOW)
        text = self.context.send_message.await_args.args[1].text
        self.assertNotIn("自定义提醒4", text)
        self.assertIn("2项", text)
        self.assertEqual(1, self.context.send_message.await_count)
        await self.plugin._tick(NOW + 180)
        self.assertEqual(1, self.context.send_message.await_count)
        result = await self.plugin.personal_todo(Event("好", message_id="more"), "list")
        self.assertIn("自定义提醒4", result)

    async def test_completion_while_generating_does_not_send_stale_task(self):
        near = self.seed()

        async def generate(origin, facts, **kwargs):
            self.plugin.store.finish_task(near["id"], "123", "done", NOW)
            return "旧内容"

        with patch.object(self.plugin, "_notification_text", side_effect=generate):
            await self.plugin._daily_summary(NOW)
        text = self.context.send_message.await_args.args[1].text
        self.assertNotIn("英语单元测试", text)
        self.assertNotIn("下周远期课程", text)
        self.assertIn("7项", text)


if __name__ == "__main__":
    unittest.main()
