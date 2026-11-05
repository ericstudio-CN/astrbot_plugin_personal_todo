"""QQ private-chat todo capture and durable, model-independent reminders."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime

import astrbot.api.message_components as Comp
import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register

from .domain import (
    LOCAL_TZ,
    ExtractionError,
    build_extraction_prompt,
    day_bounds,
    format_time,
    parse_extraction,
    validate_clock,
)
from .media import MediaError, image_data_uri
from .storage import TaskStore

HELP = """个人待办（仅本人私聊，北京时间）
直接发通知文字或图片即可自动识别；普通聊天不会记录。
/记一下 明天下午六点交材料，提前两小时提醒
/记一下 + 图片，也可引用消息后发送 /记一下
/待办 列表｜今日｜详情 23
/待办 完成 23｜取消 23
/待办 延期 23 到后天18点
/待办 提醒 23 半小时后提醒我
/待办 重要 23 高（或普通、低）
/待办 补充 这里的明天是11月8日
/待办 汇总 开｜关｜20:30
/待办 测试提醒（约一分钟后发送）
/待办 状态（检查版本、自动识别开关）
/待办 帮助
日期不明确时先补充信息，确认清楚后才建立待办。
没有截止日期也没有提醒时间的事项只进入清单。"""

PRIORITIES = {"high": "🔴高", "normal": "🟡普通", "low": "⚪低"}


def _integer(value, minimum: int, maximum: int, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是整数") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{name}应在 {minimum}～{maximum} 之间")
    return result


VERSION = "0.2.0"


@register("astrbot_plugin_personal_todo", "Personal Todo contributors", "个人待办与提醒", VERSION)
class PersonalTodoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store: TaskStore | None = None
        self.client: httpx.AsyncClient | None = None
        self.worker: asyncio.Task | None = None
        self.model_lock = asyncio.Lock()
        self._closing = False

    @property
    def owner(self) -> str:
        return str(self.config.get("owner_qq", "")).strip()

    async def initialize(self):
        if self.worker is not None:
            return
        self.poll_seconds = _integer(self.config.get("poll_seconds", 30), 10, 300, "检查间隔")
        self.model_timeout = _integer(
            self.config.get("model_timeout_seconds", 90), 10, 300, "模型超时"
        )
        self.lead_minutes = _integer(
            self.config.get("default_lead_minutes", 120), 0, 525600, "提前提醒分钟数"
        )
        self.image_limit = _integer(self.config.get("max_image_mb", 8), 1, 16, "图片大小") * 1024**2
        self.date_clock = validate_clock(self.config.get("date_reminder_time", "20:00"))
        validate_clock(self.config.get("daily_summary_time", "21:00"))
        self.store = TaskStore(
            StarTools.get_data_dir("astrbot_plugin_personal_todo") / "todo.sqlite3"
        )
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20), limits=httpx.Limits(max_connections=2)
        )
        self._closing = False
        self.worker = asyncio.create_task(self._worker(), name="personal-todo-reminders")
        logger.info(
            "个人待办 v%s 已加载：自动识别=%s，主人配置=%s，检查间隔=%ss",
            VERSION,
            self.config.get("auto_capture_enabled", True),
            bool(self.owner),
            self.poll_seconds,
        )
        if not self.owner:
            logger.warning("个人待办：请先在插件配置中填写 owner_qq，再私聊机器人。")

    async def terminate(self):
        self._closing = True
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
            self.worker = None
        # A command currently waiting for the model checks _closing before writing.
        if self.client:
            await self.client.aclose()
            self.client = None
        if self.store:
            self.store.close()
            self.store = None

    def _authorize(self, event: AstrMessageEvent) -> str | None:
        if event.get_platform_name() != "aiocqhttp":
            return "本插件只支持通过 OneBot v11 接入的 QQ 私聊。"
        if not event.is_private_chat():
            return "本插件只在 QQ 私聊中使用，请私聊机器人。"
        if not self.owner:
            return "请先到 AstrBot 插件配置填写 owner_qq（你本人的 QQ 号），保存并重载插件。"
        if str(event.get_sender_id()) != self.owner:
            return "本插件仅供已配置的本人账号使用。"
        if self.store is None or self._closing:
            return "插件正在加载或重载，请稍后重试。"
        self.store.set_meta(f"origin:{self.owner}", event.unified_msg_origin)
        return None

    @staticmethod
    def _body(event: AstrMessageEvent, names: str) -> str:
        text = event.get_message_str().strip()
        match = re.search(rf"(?:{names})(?=\s|$)", text, flags=re.IGNORECASE)
        return text[match.end() :].strip() if match else ""

    def _is_command_event(self, event: AstrMessageEvent) -> bool:
        """Leave registered commands and explicit command prefixes to their handlers.

        AstrBot may already have stripped the wake prefix from message_str, so
        also inspect the original Plain components and matched handler metadata.
        """
        for handler in event.get_extra("activated_handlers", []) or []:
            if any(
                type(item).__name__ in {"CommandFilter", "CommandGroupFilter"}
                for item in getattr(handler, "event_filters", [])
            ):
                return True
        text = event.get_message_str().strip()
        if re.match(r"^(?:待办|todo|记一下|记待办)(?:\s|$)", text, re.IGNORECASE):
            return True
        raw_text = "".join(
            c.text for c in event.get_messages() if isinstance(c, Comp.Plain)
        ).strip()
        prefixes = ["/"]
        get_config = getattr(self.context, "get_config", None)
        if callable(get_config):
            cfg = get_config(umo=event.unified_msg_origin)
            configured = cfg.get("wake_prefix", [])
            if isinstance(configured, list):
                prefixes.extend(p for p in configured if isinstance(p, str) and p)
        return any(raw_text.startswith(prefix) for prefix in prefixes)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=99999)
    async def auto_capture(self, event: AstrMessageEvent):
        """Recognize owner notifications before ordinary chat/merger handlers."""
        if (
            not self.config.get("auto_capture_enabled", True)
            or event.get_platform_name() != "aiocqhttp"
            or not event.is_private_chat()
            or not self.owner
            or str(event.get_sender_id()) != self.owner
            or self._is_command_event(event)
        ):
            return
        text = event.get_message_str().strip()
        if not text and not any(
            isinstance(c, (Comp.Image, Comp.Reply)) for c in event.get_messages()
        ):
            return
        error = self._authorize(event)
        if error:
            event.stop_event()
            yield event.plain_result(error)
            return
        logger.info(
            "个人待办：自动识别开始 message_id=%s text_chars=%s",
            getattr(event.message_obj, "message_id", "unknown"),
            len(text),
        )
        result = await self._safe_capture(event, text, mode="auto")
        if result is not None:
            # Only our saved/clarification/error response may promise task state.
            # Stop the downstream chat model and message-merger for this task.
            event.stop_event()
            yield event.plain_result(result)

    @filter.command("记一下", alias={"记待办"}, priority=100000)
    async def capture(self, event: AstrMessageEvent):
        event.stop_event()
        error = self._authorize(event)
        if error:
            yield event.plain_result(error)
            return
        text = self._body(event, "记一下|记待办")
        logger.info("个人待办：收到手动记录指令 text_chars=%s", len(text))
        yield event.plain_result(await self._safe_capture(event, text))

    @filter.command("待办", alias={"todo"}, priority=100000)
    async def todo(self, event: AstrMessageEvent):
        event.stop_event()
        error = self._authorize(event)
        if error:
            yield event.plain_result(error)
            return
        body = self._body(event, "待办|todo")
        logger.info(
            "个人待办：收到管理指令 action=%s", body.split(maxsplit=1)[0] if body else "列表"
        )
        try:
            result = await self._dispatch(event, body)
        except (ValueError, MediaError) as exc:
            result = str(exc)
        except Exception as exc:
            logger.error("个人待办指令失败：%s", type(exc).__name__)
            result = (
                "操作失败，请查看 AstrBot 日志后重试。未收到建立成功和编号时，请先查询待办列表。"
            )
        yield event.plain_result(result)

    async def _dispatch(self, event: AstrMessageEvent, body: str) -> str:
        assert self.store is not None
        parts = body.split(maxsplit=1)
        verb = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        now = int(time.time())
        if verb in ("帮助", "help"):
            return HELP
        if verb == "状态":
            return (
                f"私人待办提醒 v{VERSION}\n"
                f"私聊自动识别：{'开启' if self.config.get('auto_capture_enabled', True) else '关闭'}\n"
                f"模型提供商：{self.config.get('provider_id') or '跟随当前会话模型'}\n"
                "记录成功会回复‘已添加’和编号；时间不明确会提示补充。"
            )
        if verb in ("", "列表", "list", "今日", "今天"):
            return self._list(now, today=verb in ("今日", "今天"))
        if verb == "汇总":
            return self._summary_settings(rest)
        if verb == "测试提醒":
            rows, _ = self.store.add_tasks(
                self.owner,
                event.unified_msg_origin,
                self._source_key(event, body),
                [
                    {
                        "title": "测试提醒",
                        "due_at": None,
                        "due_precision": "none",
                        "importance": "low",
                        "importance_reason": "用于验证主动消息投递",
                        "source_excerpt": "手动测试",
                        "reminder_times": [now + 60],
                    }
                ],
                now,
            )
            return f"已建立 #{rows[0]['id']:03d} 测试提醒，约一分钟后发送。请保持 AstrBot 和 QQ 连接在线。"
        if verb in ("详情", "完成", "取消", "延期", "提醒", "重要"):
            task_parts = rest.split(maxsplit=1)
            task_token = task_parts[0] if task_parts else ""
            instruction = task_parts[1] if len(task_parts) > 1 else ""
            if not task_token.lstrip("#").isdigit():
                return f"用法：/待办 {verb} 编号" + (
                    " 具体要求" if verb in ("延期", "提醒", "重要") else ""
                )
            task_id = int(task_token.lstrip("#"))
            task = self.store.get_task(task_id, self.owner)
            if task is None:
                return "没有找到该编号的待办。"
            if verb == "详情":
                return self._detail(task)
            if verb in ("完成", "取消"):
                updated = self.store.finish_task(
                    task_id, self.owner, "done" if verb == "完成" else "cancelled", now
                )
                return (
                    f"#{task_id:03d} 已{verb}，后续提醒已停止。"
                    if updated
                    else "任务状态无法修改。"
                )
            if task["status"] != "pending":
                return "该任务已经完成或取消，不能再修改。"
            if verb == "重要":
                priority = {"高": "high", "普通": "normal", "低": "low"}.get(instruction.strip())
                if priority is None:
                    return "用法：/待办 重要 编号 高（或普通、低）"
                self.store.update_importance(task_id, self.owner, priority, now)
                return f"#{task_id:03d} 已标为{PRIORITIES[priority]}。现有提醒时间不变，可用 /待办 提醒 修改。"
            if not instruction.strip():
                return f"用法：/待办 {verb} 编号 " + (
                    "到明天18点" if verb == "延期" else "半小时后提醒我"
                )
            return await self._safe_capture(
                event,
                instruction.strip(),
                mode="postpone" if verb == "延期" else "reminder",
                original_task=task,
            )
        if verb == "补充":
            draft_raw = self.store.get_meta(f"draft:{self.owner}:{event.unified_msg_origin}")
            if not draft_raw:
                return "当前没有待补充的内容。请用 /记一下 重新提交。"
            draft = json.loads(draft_raw)
            if now - draft["created_at"] > 86400:
                return "这条待补充记录已超过一天，请用 /记一下 重新提交，以免相对日期产生歧义。"
            if not rest:
                return "请在 /待办 补充 后写明日期或其他缺失信息。"
            return await self._safe_capture(
                event,
                rest,
                mode=draft["mode"],
                original_task=draft.get("original_task"),
                draft=draft,
            )
        text = rest if verb in ("添加", "add") else body
        return await self._safe_capture(event, text)

    def _source_key(self, event: AstrMessageEvent, text: str) -> str:
        message_id = getattr(event.message_obj, "message_id", None)
        identity = (
            str(message_id)
            if message_id not in (None, "", 0, "0")
            else f"{int(time.time()) // 120}:{text}"
        )
        return hashlib.sha256(f"{event.unified_msg_origin}:{identity}".encode()).hexdigest()

    async def _input(self, event: AstrMessageEvent, text: str) -> tuple[dict, list[str]]:
        if len(text) > 16000:
            raise ValueError("文字过长，请分批提交（每次最多16000字）。")
        images: list[str] = []
        quotes: list[dict] = []
        for component in event.get_messages():
            if isinstance(component, Comp.Image):
                images.append(component.url or component.file or component.path or "")
            elif isinstance(component, Comp.Reply):
                chain = component.chain or []
                quoted_text = "\n".join(c.text for c in chain if isinstance(c, Comp.Plain))
                quoted_text = quoted_text or component.message_str or ""
                quoted_images = [
                    c.url or c.file or c.path or "" for c in chain if isinstance(c, Comp.Image)
                ]
                if not quoted_text and not quoted_images:
                    raise MediaError("无法读取被引用的消息，请直接转发文字或重新发送图片。")
                if len(quoted_text) > 16000:
                    raise ValueError("被引用消息过长，请分批提交（每次最多16000字）。")
                quotes.append({"text": quoted_text, "sent_at_unix": component.time or None})
                images.extend(quoted_images)
        images = list(dict.fromkeys(images))
        if len(images) > 3:
            raise MediaError("每次最多识别3张图片，请分批提交。")
        if not text and not images and not quotes:
            raise ValueError("请在 /记一下 后附文字、图片，或引用一条消息。\n" + HELP)
        assert self.client is not None
        encoded = [
            await asyncio.wait_for(image_data_uri(ref, self.client, self.image_limit), timeout=30)
            for ref in images
        ]
        return {"user_request": text, "quoted_messages": quotes}, encoded

    async def _safe_capture(
        self, event, text, mode="create", original_task=None, draft=None
    ) -> str | None:
        if self.model_lock.locked():
            logger.info("个人待办：识别繁忙，本条尚未保存")
            return "上一条内容还在识别，请等收到结果后再提交。"
        async with self.model_lock:
            try:
                return await self._extract(event, text, mode, original_task, draft)
            except (ExtractionError, MediaError, ValueError) as exc:
                logger.warning(
                    "个人待办：识别校验未通过 type=%s reason=%s", type(exc).__name__, exc
                )
                return f"没有保存本次修改：{exc}"
            except asyncio.TimeoutError:
                logger.warning("个人待办：模型或图片读取超时，本次没有保存")
                return "模型或图片读取超时，本次没有保存。请稍后重试。"
            except Exception as exc:
                logger.error("个人待办识别失败：%s", type(exc).__name__)
                return "处理失败，请先用 /待办 列表 核对是否已建立。请确认模型可用；有图片时需要支持视觉输入。"

    async def _extract(self, event, text, mode, original_task, draft) -> str | None:
        assert self.store is not None
        request_owner = self.owner
        payload, images = await self._input(event, text)
        if draft:
            payload["previous_submission"] = draft["payload"]
            payload["previous_candidates"] = draft["candidates"]
            payload["questions_to_resolve"] = draft["questions"]
            payload["previous_received_at_unix"] = draft["created_at"]
        received_at = int(time.time())
        provider_id = str(self.config.get("provider_id", "")).strip()
        if not provider_id:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        if not provider_id:
            raise ValueError("请先在 AstrBot 配置一个可用的聊天模型。")
        logger.info(
            "个人待办：请求模型 mode=%s provider=%s images=%s", mode, provider_id, len(images)
        )
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=json.dumps(payload, ensure_ascii=False),
                system_prompt=build_extraction_prompt(
                    received_at, mode=mode, original_task=original_task
                ),
                image_urls=images,
                contexts=[],
            ),
            timeout=self.model_timeout,
        )
        if self._closing:
            return "插件正在重载，本次未保存，请稍后重试。"
        if self.owner != request_owner or str(event.get_sender_id()) != request_owner:
            return "主人配置已变更，本次未保存，请使用当前主人账号重新提交。"
        parsed = parse_extraction(
            response.completion_text,
            now=int(time.time()),
            default_lead_minutes=self.lead_minutes,
            date_reminder_time=self.date_clock,
            mode=mode,
        )
        if mode == "auto" and not parsed["is_task"]:
            logger.info("个人待办：自动识别结果=非待办，继续普通聊天")
            return None
        logger.info(
            "个人待办：识别完成 candidates=%s questions=%s",
            len(parsed["tasks"]),
            len(parsed["questions"]),
        )
        draft_key = f"draft:{self.owner}:{event.unified_msg_origin}"
        if parsed["questions"]:
            # Store extracted evidence, not image bytes, so a clarification survives a restart.
            previous_payload = draft["payload"] if draft else payload
            self.store.set_meta(
                draft_key,
                json.dumps(
                    {
                        "mode": "create" if mode == "auto" else mode,
                        "original_task": original_task,
                        "payload": previous_payload,
                        "candidates": parsed["tasks"],
                        "questions": parsed["questions"],
                        "created_at": draft["created_at"] if draft else received_at,
                        "source_key": draft["source_key"]
                        if draft
                        else self._source_key(event, text),
                    },
                    ensure_ascii=False,
                ),
            )
            logger.info("个人待办：需要补充信息，已保存确认草稿，待办尚未入库")
            return (
                "需要补充信息，暂未保存：\n"
                + "\n".join(f"• {q}" for q in parsed["questions"])
                + "\n请回复 /待办 补充 具体信息"
            )
        items = parsed["tasks"]
        if not items:
            return "没有识别到可建立的待办，请补充具体要做的事。"
        now = int(time.time())
        if mode not in ("create", "auto"):
            if len(items) != 1 or original_task is None:
                raise ValueError("修改一次只能对应一个待办，请重新描述。")
            item = items[0]
            if mode == "postpone":
                if item["due_at"] is None:
                    raise ValueError("延期需要明确新的截止日期或时间。")
                updated = self.store.reschedule_task(
                    original_task["id"],
                    self.owner,
                    item["due_at"],
                    item["due_precision"],
                    item["reminder_times"],
                    now,
                )
            else:
                if (item["due_at"], item["due_precision"]) != (
                    original_task["due_at"],
                    original_task["due_precision"],
                ):
                    raise ValueError("模型意外改变了截止时间，本次没有修改，请重新设置提醒。")
                if not item["reminder_times"]:
                    raise ValueError("请给出未来的提醒时间，例如半小时后提醒。")
                updated = self.store.replace_reminders(
                    original_task["id"], self.owner, item["reminder_times"], now
                )
            if not updated:
                return "任务已被完成或取消，本次没有修改。"
            self.store.set_meta(draft_key, "")
            logger.info("个人待办：已更新 id=%s mode=%s", original_task["id"], mode)
            return "已更新：\n" + self._detail(updated)
        rows, created = self.store.add_tasks(
            self.owner,
            event.unified_msg_origin,
            draft["source_key"] if draft else self._source_key(event, text),
            items,
            now,
        )
        self.store.set_meta(draft_key, "")
        logger.info(
            "个人待办：%s count=%s ids=%s",
            "已入库" if created else "重复消息未重复入库",
            len(rows),
            ",".join(str(row["id"]) for row in rows),
        )
        heading = "已添加：" if created else "这条消息已处理，未重复添加："
        return heading + "\n\n" + "\n\n".join(self._detail(row) for row in rows)

    @staticmethod
    def _line(task: dict, now: int) -> str:
        due = task["due_at"]
        overdue = "【已逾期】" if due is not None and due < now else ""
        return f"#{task['id']:03d} {PRIORITIES[task['importance']]} {overdue}{task['title']}\n截止：{format_time(due, task['due_precision'])}"

    def _list(self, now: int, today: bool = False) -> str:
        rows = self.store.list_tasks(self.owner, limit=500)
        if today:
            _, end = day_bounds(now)
            rows = [row for row in rows if row["due_at"] is not None and row["due_at"] < end]
        if not rows:
            return (
                "今天没有待处理的到期事项。"
                if today
                else "当前没有未完成待办。发送 /记一下 开始记录。"
            )
        rows.sort(key=lambda row: (row["due_at"] is None, row["due_at"] or 0, row["id"]))
        title = "今日到期与已逾期待办" if today else "未完成待办"
        return f"{title}（{len(rows)}项，最多显示30项）：\n\n" + "\n\n".join(
            self._line(row, now) for row in rows[:30]
        )

    def _detail(self, task: dict) -> str:
        labels = {"pending": "待完成", "done": "已完成", "cancelled": "已取消"}
        text = self._line(task, int(time.time())) + f"\n状态：{labels[task['status']]}"
        if task.get("importance_reason"):
            text += f"\n标记依据：{task['importance_reason']}"
        reminders = self.store.list_reminders(task["id"], self.owner)
        queued = [r for r in reminders if r["state"] in ("pending", "processing")]
        text += "\n后续提醒：" + (
            "、".join(format_time(r["scheduled_at"]) for r in queued) if queued else "无"
        )
        if any(r["state"] == "failed" for r in reminders):
            text += "\n⚠ 有提醒多次发送失败，请检查 QQ 连接后重新设置提醒。"
        if task.get("source_excerpt"):
            text += f"\n来源摘录：{task['source_excerpt'][:300]}"
        return text

    def _summary_settings(self, value: str) -> str:
        key = f"summary_enabled:{self.owner}"
        clock_key = f"summary_clock:{self.owner}"
        if value == "开":
            self.store.set_meta(key, "true")
        elif value == "关":
            self.store.set_meta(key, "false")
        elif value:
            self.store.set_meta(clock_key, validate_clock(value))
            self.store.set_meta(key, "true")
        enabled, clock = self._summary_config()
        return (
            f"每日汇总：{'开启' if enabled else '关闭'}；北京时间 {clock}。只在有未完成事项时发送。"
        )

    def _summary_config(self) -> tuple[bool, str]:
        enabled = self.store.get_meta(f"summary_enabled:{self.owner}")
        if enabled is None:
            enabled = "true" if self.config.get("daily_summary_enabled", True) else "false"
        clock = self.store.get_meta(f"summary_clock:{self.owner}") or self.config.get(
            "daily_summary_time", "21:00"
        )
        return enabled == "true", validate_clock(clock)

    async def _send(self, origin: str, text: str):
        result = await asyncio.wait_for(
            self.context.send_message(origin, MessageChain().message(text)), timeout=20
        )
        if result is False:
            raise RuntimeError("未找到可用的消息平台")

    async def _tick(self, now: int):
        if not self.store or not self.owner:
            return
        groups: dict[str, list[dict]] = defaultdict(list)
        for job in self.store.claim_due(now, limit=20):
            if job["owner"] != self.owner:
                self.store.fail_reminder(
                    job["reminder_id"], now, "owner configuration changed", max_attempts=1
                )
                continue
            if self.store.is_reminder_active(job["reminder_id"]):
                groups[job["origin"]].append(job)
        for origin, jobs in groups.items():
            # No await between final state check and starting the send operation.
            jobs = [
                job
                for job in jobs
                if job["owner"] == self.owner and self.store.is_reminder_active(job["reminder_id"])
            ]
            if not jobs:
                continue
            lines = ["⏰ 待办提醒（北京时间）"]
            for job in jobs:
                late = "（延迟补发）" if now - job["scheduled_at"] > self.poll_seconds * 2 else ""
                lines.append(
                    f"#{job['task_id']:03d} {PRIORITIES[job['importance']]} {job['title']}{late}\n截止：{format_time(job['due_at'], job.get('due_precision', 'datetime'))}"
                )
            lines.append("完成后回复 /待办 完成 编号；稍后提醒可用 /待办 提醒 编号 半小时后")
            try:
                await self._send(origin, "\n\n".join(lines))
            except Exception as exc:
                logger.warning("个人待办提醒发送失败：%s", type(exc).__name__)
                for job in jobs:
                    self.store.fail_reminder(
                        job["reminder_id"], int(time.time()), type(exc).__name__
                    )
            else:
                for job in jobs:
                    self.store.acknowledge(job["reminder_id"], int(time.time()))
                logger.info("个人待办：提醒投递成功 count=%s", len(jobs))
        await self._daily_summary(now)

    async def _daily_summary(self, now: int):
        enabled, clock = self._summary_config()
        local = datetime.fromtimestamp(now, LOCAL_TZ)
        day = local.date().isoformat()
        marker = f"summary_last:{self.owner}"
        origin = self.store.get_meta(f"origin:{self.owner}")
        if not enabled or not origin or local.strftime("%H:%M") < clock:
            return
        if self.store.get_meta(marker) == day:
            return
        retry_after = int(self.store.get_meta(f"summary_retry:{self.owner}", "0"))
        if now < retry_after:
            return
        rows = self.store.list_tasks(self.owner, limit=500)
        if rows:
            rows.sort(key=lambda row: (row["due_at"] is None, row["due_at"] or 0, row["id"]))
            text = f"📋 {day} 未完成待办汇总（{len(rows)}项，最多显示20项）\n\n"
            text += "\n\n".join(self._line(row, now) for row in rows[:20])
            try:
                await self._send(origin, text)
            except Exception as exc:
                logger.warning("个人待办汇总发送失败：%s", type(exc).__name__)
                self.store.set_meta(f"summary_retry:{self.owner}", str(now + 300))
                return
        self.store.set_meta(marker, day)
        if rows:
            logger.info("个人待办：每日汇总投递成功 date=%s count=%s", day, len(rows))

    async def _worker(self):
        # Yield during startup so platform connections can finish loading.
        await asyncio.sleep(3)
        while not self._closing:
            try:
                await self._tick(int(time.time()))
            except Exception as exc:
                logger.error("个人待办提醒检查失败：%s", type(exc).__name__)
            await asyncio.sleep(self.poll_seconds)
