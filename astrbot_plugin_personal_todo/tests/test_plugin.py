"""Adapter behaviour with fake AstrBot surfaces and the real SQLite store.

These exercise commands and delivery without a QQ session or a paid model call.
The fake API signatures follow the AstrBot methods used by main.py; the real
framework and platform still require the documented deployment smoke test.
"""

import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, url="", file="", path=""):
        self.url, self.file, self.path = url, file, path


class Reply:
    def __init__(self, chain=None, message_str="", time=0):
        self.chain = chain
        self.message_str = message_str
        self.time = time


class MessageChain:
    def __init__(self):
        self.text = ""

    def message(self, text):
        self.text += text
        return self


def unchanged_decorator(*args, **kwargs):
    return lambda function: function


class Star:
    def __init__(self, context):
        self.context = context


def load_adapter():
    modules = {
        name: types.ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.star",
            "astrbot.api.message_components",
        )
    }
    modules["astrbot"].__path__ = []
    modules["astrbot.api"].__path__ = []
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = Mock()
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.event"].filter = types.SimpleNamespace(command=unchanged_decorator)
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].StarTools = types.SimpleNamespace(get_data_dir=Mock())
    modules["astrbot.api.star"].register = unchanged_decorator
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.message_components"].Image = Image
    modules["astrbot.api.message_components"].Reply = Reply
    with patch.dict(sys.modules, modules):
        return importlib.import_module("astrbot_plugin_personal_todo.main")


adapter = load_adapter()


def stamp(value):
    return int(datetime.fromisoformat(value).timestamp())


NOW = stamp("2026-11-05T12:00:00+08:00")


def extraction(**changes):
    task = {
        "title": "提交报名材料",
        "due_at": "2026-11-06T18:00:00+08:00",
        "due_precision": "datetime",
        "importance": "normal",
        "importance_reason": "用户指定事项",
        "source_excerpt": "明天18点前提交报名材料",
        "remind_at": [],
        "needs_confirmation": False,
        "question": "",
    }
    task.update(changes)
    return json.dumps({"tasks": [task], "questions": []}, ensure_ascii=False)


class Event:
    def __init__(self, text, *, sender="123", private=True, message_id="m1", components=None):
        self.text = text
        self.sender = sender
        self.private = private
        self.unified_msg_origin = f"qq:private:{sender}" if private else "qq:group:789"
        self.message_obj = types.SimpleNamespace(message_id=message_id)
        self.components = [Plain(text)] if components is None else components
        self.stopped = False

    def is_private_chat(self):
        return self.private

    def get_platform_name(self):
        return "aiocqhttp"

    def get_sender_id(self):
        return self.sender

    def get_message_str(self):
        return self.text

    def get_messages(self):
        return self.components

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return text


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "todo.sqlite3"
        self.context = types.SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="deepseek-provider"),
            llm_generate=AsyncMock(
                return_value=types.SimpleNamespace(completion_text=extraction())
            ),
            send_message=AsyncMock(return_value=True),
        )
        self.config = {"owner_qq": "123", "daily_summary_enabled": False}
        self.plugin = adapter.PersonalTodoPlugin(self.context, self.config)
        self.plugin.store = adapter.TaskStore(self.path)
        self.plugin.client = types.SimpleNamespace(aclose=AsyncMock())
        self.plugin.poll_seconds = 30
        self.plugin.model_timeout = 1
        self.plugin.lead_minutes = 120
        self.plugin.date_clock = "20:00"
        self.plugin.image_limit = 8 * 1024**2
        self.clock = patch.object(adapter.time, "time", return_value=NOW)
        self.clock.start()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.clock.stop()
        self.temp.cleanup()

    async def capture(self, event):
        results = [result async for result in self.plugin.capture(event)]
        self.assertEqual(1, len(results))
        return results[0]

    async def command(self, text, **kwargs):
        event = Event(text, **kwargs)
        results = [result async for result in self.plugin.todo(event)]
        self.assertTrue(event.stopped)
        self.assertEqual(1, len(results))
        return results[0]

    def add_due_task(
        self, *, title="交材料", owner="123", precision="datetime", times=None, source="s1"
    ):
        rows, _ = self.plugin.store.add_tasks(
            owner,
            f"qq:private:{owner}",
            source,
            [
                {
                    "title": title,
                    "due_at": stamp("2026-11-06T23:59:59+08:00"),
                    "due_precision": precision,
                    "importance": "normal",
                    "importance_reason": "测试",
                    "source_excerpt": "测试来源",
                    "reminder_times": [NOW] if times is None else times,
                }
            ],
            NOW - 60,
        )
        return rows[0]

    async def test_only_configured_owner_private_chat_can_use_plugin(self):
        for event in (
            Event("/记一下 交材料", sender="456"),
            Event("/记一下 交材料", private=False),
        ):
            text = await self.capture(event)
            self.assertTrue(event.stopped)
            self.assertNotIn("已添加", text)
        self.config["owner_qq"] = ""
        self.assertIn("owner_qq", await self.command("/待办"))
        self.context.llm_generate.assert_not_awaited()
        self.assertEqual([], self.plugin.store.list_tasks("123"))

    async def test_create_complete_and_cancel_stop_future_reminders(self):
        result = await self.capture(Event("/记一下 明天18点前提交报名材料"))
        self.assertIn("已添加", result)
        self.assertIn("#001", result)
        self.assertIn("2026-11-06 16:00", result)
        self.assertIn("已完成", await self.command("/待办 完成 1"))
        self.context.llm_generate.assert_awaited_once()
        await self.plugin._tick(stamp("2026-11-06T16:00:00+08:00"))
        self.context.send_message.assert_not_awaited()
        await self.capture(Event("/记一下 再交一份材料", message_id="m2"))
        self.assertIn("已取消", await self.command("/待办 取消 2"))
        await self.plugin._tick(stamp("2026-11-06T18:00:00+08:00"))
        self.context.send_message.assert_not_awaited()

    async def test_timeout_and_invalid_output_leave_no_task(self):
        async def slow_model(**kwargs):
            await asyncio.sleep(10)

        self.plugin.model_timeout = 0.001
        self.context.llm_generate.side_effect = slow_model
        text = await self.capture(Event("/记一下 交材料"))
        self.assertIn("超时", text)
        self.assertEqual([], self.plugin.store.list_tasks("123"))
        self.context.llm_generate.side_effect = None
        self.context.llm_generate.return_value = types.SimpleNamespace(completion_text="不是JSON")
        text = await self.capture(Event("/记一下 交材料", message_id="m2"))
        self.assertIn("没有保存", text)
        self.assertEqual([], self.plugin.store.list_tasks("123"))

    async def test_quote_and_image_evidence_are_sent_to_model_once(self):
        reference = "https://qq.example/image.png"
        event = Event(
            "/记一下",
            components=[
                Plain("/记一下"),
                Image(url=reference),
                Reply(
                    chain=[Plain("明天18点交材料"), Image(url=reference)],
                    time=NOW - 86400,
                ),
            ],
        )
        encoded = "data:image/png;base64,eA=="
        with patch.object(adapter, "image_data_uri", new=AsyncMock(return_value=encoded)) as reader:
            result = await self.capture(event)
        self.assertIn("已添加", result)
        reader.assert_awaited_once_with(reference, self.plugin.client, self.plugin.image_limit)
        args = self.context.llm_generate.await_args.kwargs
        self.assertEqual([encoded], args["image_urls"])
        self.assertEqual([], args["contexts"])
        payload = json.loads(args["prompt"])
        self.assertEqual("明天18点交材料", payload["quoted_messages"][0]["text"])
        self.assertEqual(NOW - 86400, payload["quoted_messages"][0]["sent_at_unix"])

    async def test_unreadable_quote_does_not_call_model(self):
        result = await self.capture(Event("/记一下", components=[Reply()]))
        self.assertIn("无法读取", result)
        self.context.llm_generate.assert_not_awaited()
        self.assertEqual([], self.plugin.store.list_tasks("123"))

    async def test_image_count_and_decoding_fail_before_model_or_save(self):
        too_many = Event(
            "/记一下",
            components=[Image(url=f"https://qq.example/{number}.png") for number in range(4)],
        )
        with patch.object(adapter, "image_data_uri", new=AsyncMock()) as reader:
            result = await self.capture(too_many)
            self.assertIn("最多识别3张", result)
            reader.assert_not_awaited()
        one_image = Event("/记一下", components=[Image(file="base64://not-an-image")])
        with patch.object(
            adapter,
            "image_data_uri",
            new=AsyncMock(side_effect=adapter.MediaError("图片太大或编码无效")),
        ) as reader:
            result = await self.capture(one_image)
            reader.assert_awaited_once_with(
                "base64://not-an-image", self.plugin.client, 8 * 1024**2
            )
        self.assertIn("没有保存", result)
        self.context.llm_generate.assert_not_awaited()
        self.assertEqual([], self.plugin.store.list_tasks("123"))

    async def test_replayed_platform_message_does_not_create_duplicate(self):
        event = Event("/记一下 明天18点交材料", message_id="same-platform-id")
        self.assertIn("已添加", await self.capture(event))
        result = await self.capture(event)
        self.assertIn("未重复添加", result)
        self.assertEqual(1, len(self.plugin.store.list_tasks("123")))

    async def test_clarification_is_saved_only_after_resolving_question(self):
        self.context.llm_generate.return_value = types.SimpleNamespace(
            completion_text=extraction(
                due_at=None,
                due_precision="none",
                needs_confirmation=True,
                question="截图中的明天是几号？",
            )
        )
        original = Event("/记一下 截图说明明天交材料", message_id="original")
        result = await self.capture(original)
        self.assertIn("暂未保存", result)
        self.assertEqual([], self.plugin.store.list_tasks("123"))
        # The draft is durable, not tied to an event object or image bytes.
        self.plugin.store.close()
        self.plugin.store = adapter.TaskStore(self.path)
        self.context.llm_generate.return_value = types.SimpleNamespace(completion_text=extraction())
        result = await self.command("/待办 补充 明天是11月6日，18点截止", message_id="answer")
        self.assertIn("已添加", result)
        self.assertEqual(1, len(self.plugin.store.list_tasks("123")))
        saved = self.plugin.store.list_tasks("123")[0]
        self.assertEqual(self.plugin._source_key(original, ""), saved["source_key"])
        payload = json.loads(self.context.llm_generate.await_args.kwargs["prompt"])
        self.assertIn("previous_submission", payload)
        self.assertEqual(["截图中的明天是几号？"], payload["questions_to_resolve"])

    async def test_due_jobs_group_and_retry_without_any_model_request(self):
        first = self.add_due_task(title="报名材料", precision="date", source="s1")
        second = self.add_due_task(title="会议记录", source="s2")
        self.context.send_message.return_value = False
        await self.plugin._tick(NOW)
        self.assertEqual(1, self.context.send_message.await_count)
        sent = self.context.send_message.await_args.args[1].text
        self.assertIn("报名材料", sent)
        self.assertIn("会议记录", sent)
        self.assertIn("2026-11-06（未指定具体时刻）", sent)
        self.assertEqual(1, self.plugin.store.list_reminders(first["id"], "123")[0]["attempts"])
        await self.plugin._tick(NOW + 29)
        self.assertEqual(1, self.context.send_message.await_count)
        self.context.send_message.return_value = True
        with patch.object(adapter.time, "time", return_value=NOW + 30):
            await self.plugin._tick(NOW + 30)
        self.assertEqual(2, self.context.send_message.await_count)
        self.assertEqual([], self.plugin.store.list_reminders(first["id"], "123"))
        self.assertEqual([], self.plugin.store.list_reminders(second["id"], "123"))
        await self.plugin._tick(NOW + 60)
        self.assertEqual(2, self.context.send_message.await_count)
        self.context.llm_generate.assert_not_awaited()

    async def test_daily_summary_is_once_per_local_day_across_restart(self):
        self.config["daily_summary_enabled"] = True
        self.add_due_task(times=[])
        await self.command("/待办 列表")
        evening = stamp("2026-11-05T21:00:00+08:00")
        await self.plugin._daily_summary(evening - 60)
        self.context.send_message.assert_not_awaited()
        await self.plugin._daily_summary(evening)
        self.assertEqual(1, self.context.send_message.await_count)
        self.plugin.store.close()
        self.plugin.store = adapter.TaskStore(self.path)
        await self.plugin._daily_summary(evening + 60)
        self.assertEqual(1, self.context.send_message.await_count)
        await self.plugin._daily_summary(evening + 86400)
        self.assertEqual(2, self.context.send_message.await_count)
        self.assertIn("未完成待办汇总", self.context.send_message.await_args.args[1].text)

    async def test_changed_owner_does_not_send_old_owner_tasks(self):
        task = self.add_due_task(owner="123")
        self.plugin.store.set_meta("origin:123", "qq:private:123")
        self.config["owner_qq"] = "456"
        self.config["daily_summary_enabled"] = True
        await self.plugin._tick(stamp("2026-11-05T21:00:00+08:00"))
        self.context.send_message.assert_not_awaited()
        reminders = self.plugin.store.list_reminders(task["id"], "123")
        self.assertEqual("failed", reminders[0]["state"])
        self.assertEqual("pending", self.plugin.store.get_task(task["id"], "123")["status"])

    async def test_owner_changed_during_model_request_cannot_reassign_submission(self):
        async def response_after_owner_changed(**kwargs):
            self.config["owner_qq"] = "456"
            return types.SimpleNamespace(completion_text=extraction())

        self.context.llm_generate.side_effect = response_after_owner_changed
        result = await self.capture(Event("/记一下 明天18点交材料"))
        self.assertNotIn("已添加", result)
        self.assertEqual([], self.plugin.store.list_tasks("123"))
        self.assertEqual([], self.plugin.store.list_tasks("456"))


if __name__ == "__main__":
    unittest.main()
