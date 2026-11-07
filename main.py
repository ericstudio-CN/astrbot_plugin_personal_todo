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

from .compat import has_dsml, infer_read_action, is_format_discussion, is_own_dsml_call
from .conversation import CONTEXT_MARKER, TOOL_NAME, build_chat_context
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
from .notifications import (
    NOTIFICATION_INSTRUCTIONS,
    agrees_to_expand,
    declines_to_expand,
    notification_payload,
    parse_notification,
    preview_fallback,
    requests_all,
    select_preview,
)
from .storage import TaskStore

HELP = """个人待办（仅本人私聊，北京时间）
直接发通知文字或图片即可记录；可直接说“晚上十一点”“四级作业做完了”“今天要做什么”。
默认与当前人格一起对话，不需要使用下面的备用指令。
/记一下 明天下午六点交材料，提前两小时提醒
/记一下 + 图片，也可引用消息后发送 /记一下
/待办 列表｜今日｜详情 23
/待办 完成 23｜取消 23
/待办 延期 23 到后天18点
/待办 提醒 23 半小时后提醒我
/待办 重要 23 高（或普通、低）
/待办 补充 这里的明天是11月8日
/待办 汇总 开｜关｜20:30｜自然｜固定
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


VERSION = "0.4.0"


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
        self._persona_prompts: dict[str, str] = {}

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
        self._notification_options()
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
            "个人待办 v%s 已加载：自动识别=%s，自然对话=%s，主人配置=%s，检查间隔=%ss",
            VERSION,
            self.config.get("auto_capture_enabled", True),
            self.config.get("conversational_mode", True),
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
        if self.config.get("conversational_mode", True):
            # Native AstrBot chat owns persona, conversation history and tool
            # execution. Do not race it with a second independent bot response.
            return
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
            diagnostic = await self._tool_diagnostic(event.unified_msg_origin)
            return (
                f"私人待办提醒 v{VERSION}\n"
                f"私聊自动识别：{'开启' if self.config.get('auto_capture_enabled', True) else '关闭'}\n"
                f"自然对话与人格融合：{'开启' if self.config.get('conversational_mode', True) else '关闭'}\n"
                f"模型提供商：{self.config.get('provider_id') or '跟随当前会话模型'}\n"
                f"模型工具调用能力：{diagnostic['capability']}\n"
                f"工具调用模式：{diagnostic['mode']}（推荐 full）\n"
                f"提醒格式：{'固定清单' if self._fixed_notifications() else '人格自然对话'}；近期{self._notification_options()[0]}天，最多{self._notification_options()[1]}项\n"
                "自然对话由当前人格通过 personal_todo 工具操作并回复；备用指令显示完整编号。"
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

    def _owned_chat(self, event: AstrMessageEvent) -> bool:
        return (
            bool(self.owner)
            and event.get_platform_name() == "aiocqhttp"
            and event.is_private_chat()
            and str(event.get_sender_id()) == self.owner
        )

    def _draft(self, origin: str) -> dict | None:
        raw = self.store.get_meta(f"draft:{self.owner}:{origin}")
        if not raw:
            return None
        draft = json.loads(raw)
        return draft if int(time.time()) - draft["created_at"] <= 86400 else None

    def _notification_options(self) -> tuple[int, int]:
        return (
            _integer(self.config.get("summary_window_days", 2), 1, 7, "近期汇总天数"),
            _integer(self.config.get("summary_max_items", 3), 1, 8, "主动展示事项上限"),
        )

    def _fixed_notifications(self) -> bool:
        override = self.store.get_meta(f"notification_fixed:{self.owner}") if self.store else None
        fixed = (
            bool(self.config.get("fixed_notification_format", False))
            if override is None
            else bool(override)
        )
        return fixed or not self.config.get("conversational_mode", True)

    def _more_offer(self, event: AstrMessageEvent, *, bind=False) -> dict | None:
        key = f"more_offer:{self.owner}:{event.unified_msg_origin}"
        offer = self.store.get_meta(key)
        if not isinstance(offer, dict) or int(time.time()) - offer.get("time", 0) > 7200:
            return None
        source = self._source_key(event, event.get_message_str())
        if bind and not offer.get("claimed_by"):
            if declines_to_expand(event.get_message_str()):
                self.store.set_meta(key, None)
                return None
            offer["claimed_by"] = source
            self.store.set_meta(key, offer)
        return offer if offer.get("claimed_by") == source else None

    def _record_more_offer(
        self,
        owner: str,
        origin: str,
        rows: list[dict],
        visible: list[dict],
        now: int,
        *,
        scope="pending",
    ):
        key = f"more_offer:{owner}:{origin}"
        hidden = len(rows) - len(visible)
        self.store.set_meta(
            key,
            {
                "time": now,
                "scope": scope,
                "hidden_count": hidden,
                "task_ids": [row["id"] for row in rows],
                "shown_ids": [row["id"] for row in visible],
            }
            if hidden
            else None,
        )

    def _expands_offer(self, event: AstrMessageEvent) -> bool:
        return bool(
            self._more_offer(event, bind=True) and agrees_to_expand(event.get_message_str())
        )

    def _query_facts(self, event: AstrMessageEvent, *, today=False) -> str:
        now = int(time.time())
        rows = self.store.list_tasks(self.owner, limit=500)
        offer = self._more_offer(event, bind=True)
        expanded = bool(offer and agrees_to_expand(event.get_message_str()))
        show_all = requests_all(event.get_message_str()) or expanded
        if show_all:
            if today:
                rows = select_preview(
                    rows, now, self._notification_options()[0], len(rows), today=True
                )["visible"]
            if (
                expanded
                and offer["scope"] == "reminders"
                and not re.search(r"全部待办|所有任务|所有待办|完整待办", event.get_message_str())
            ):
                ids = set(offer["task_ids"])
                rows = [row for row in rows if row["id"] in ids]
            self.store.set_meta(f"more_offer:{self.owner}:{event.unified_msg_origin}", None)
            return "主人已明确要求展开，以下是真实未完成事项：\n\n" + (
                "\n\n".join(self._detail(row) for row in rows[:30]) or "当前没有未完成事项。"
            )
        days, maximum = self._notification_options()
        preview = select_preview(rows, now, days, maximum, today=today)
        visible, hidden = preview["visible"], preview["hidden"]
        declined = declines_to_expand(event.get_message_str())
        if declined:
            self.store.set_meta(f"more_offer:{self.owner}:{event.unified_msg_origin}", None)
        else:
            self._record_more_offer(self.owner, event.unified_msg_origin, rows, visible, now)
        return (
            f"以下仅为近期重点，最多{maximum}项；不要主动展开其他事项。\n"
            + (
                "\n\n".join(self._detail(row) for row in visible)
                or "这段时间没有临近截止或最近逾期的待办。"
            )
            + (
                f"\n另有{len(hidden)}项未展开，请沿用人格询问主人是否想看完整清单，获得同意前不要列出。"
                if hidden and not declined
                else ""
            )
        )

    async def _tool_diagnostic(self, origin: str) -> dict:
        result = {"capability": "未确认", "mode": "full", "blocked": False}
        get_config = getattr(self.context, "get_config", None)
        if callable(get_config):
            cfg = get_config(umo=origin) or {}
            result["mode"] = (
                cfg.get("agent_runner", {})
                .get("config", {})
                .get("misc", {})
                .get(
                    "tool_schema_mode",
                    cfg.get("provider_settings", {}).get("tool_schema_mode", "full"),
                )
            )
        get_provider = getattr(self.context, "get_using_provider_async", None)
        if callable(get_provider):
            try:
                provider = await get_provider(umo=origin)
                modalities = getattr(provider, "provider_config", {}).get("modalities")
                if isinstance(modalities, list) and modalities:
                    result["blocked"] = "tool_use" not in modalities
                    result["capability"] = (
                        "已开启 tool_use"
                        if not result["blocked"]
                        else "未开启 tool_use，AstrBot 会过滤工具"
                    )
            except Exception as exc:
                logger.warning("个人待办：模型工具能力诊断失败 type=%s", type(exc).__name__)
        return result

    def _chat_task_facts(self) -> list[dict]:
        rows = self.store.list_tasks(self.owner, limit=21)
        for row in rows:
            row["reminders"] = [
                format_time(item["scheduled_at"])
                for item in self.store.list_reminders(row["id"], self.owner)
                if item["state"] in ("pending", "processing")
            ]
        return rows

    @filter.on_llm_request()
    async def enrich_chat(self, event: AstrMessageEvent, req):
        """Append capabilities and live facts without replacing the persona."""
        if (
            not self.config.get("conversational_mode", True)
            or not self.config.get("auto_capture_enabled", True)
            or not self._owned_chat(event)
            or not self.store
            or self._closing
        ):
            return
        self.store.set_meta(f"origin:{self.owner}", event.unified_msg_origin)
        base_prompt = re.sub(
            r"\n<personal_todo_context>\n.*?\n</personal_todo_context>",
            "",
            req.system_prompt or "",
            flags=re.DOTALL,
        )
        persona_match = re.search(
            r"<persona_instructions>\s*(.*?)\s*</persona_instructions>", base_prompt, re.DOTALL
        )
        self._persona_prompts[event.unified_msg_origin] = (
            persona_match.group(1) if persona_match else base_prompt
        )
        tool_set = getattr(req, "func_tool", None)
        available = any(
            getattr(tool, "name", "") == TOOL_NAME
            for tool in getattr(tool_set, "tools", getattr(tool_set, "func_list", []))
        )
        event.set_extra("_personal_todo_tool_available", available)
        if not available:
            logger.warning(
                "个人待办：当前会话未提供 personal_todo 工具，请在当前人格能力中启用本插件工具"
            )
            req.system_prompt = (
                base_prompt
                + CONTEXT_MARKER
                + (
                    "当前待办工具不可用。普通聊天沿用原人格；如果用户要记录/修改待办，"
                    "如实说明暂时无法操作，需在当前人格的工具列表启用 personal_todo。"
                    "不得声称已保存或已修改。\n</personal_todo_context>"
                )
            )
            return
        req.system_prompt = base_prompt + build_chat_context(
            int(time.time()),
            self._chat_task_facts(),
            self._draft(event.unified_msg_origin),
            self.store.get_meta(f"focus:{self.owner}:{event.unified_msg_origin}"),
            offer=self._more_offer(event, bind=True),
            preferences={
                "days": self._notification_options()[0],
                "max_items": self._notification_options()[1],
            },
        )
        diagnostic = await self._tool_diagnostic(event.unified_msg_origin)
        logger.info(
            "个人待办：模型工具诊断 capability=%s mode=%s",
            diagnostic["capability"],
            diagnostic["mode"],
        )
        if diagnostic["blocked"]:
            logger.warning(
                "个人待办：请在当前 DeepSeek 模型能力中勾选工具调用/tool_use，工具模式设为 full"
            )
            req.system_prompt += "\n本次模型能力配置未开启 tool_use，工具会被框架过滤。可以依据上方真实数据库快照回答只读查询；不能执行新增或修改，也不能输出 DSML。若需修改，说明需在模型能力中开启工具调用。"
        if not diagnostic["blocked"]:
            logger.info("个人待办：已接入当前人格对话，personal_todo 工具可用")

    @filter.llm_tool(name="personal_todo")
    async def personal_todo(
        self, event: AstrMessageEvent, action: str = "auto", text: str = "", task_id: int = 0
    ) -> str:
        """在正常对话中管理主人的待办，返回真实数据库结果供当前人格自然回复。

        Args:
            action(string): add添加通知/文字/当前图片，clarify补充上次追问，discard_draft放弃未保存草稿，list查询未完成，today今日到期，details详情，complete完成，cancel取消，postpone改截止，remind改提醒，importance改重要性，summary改汇总。
            text(string): 完整任务或补充内容，结合对话补全指代；postpone给新截止，remind给新提醒，importance用高/普通/低，summary用开/关/HH:MM/自然/固定；纯查询填空字符串。
            task_id(number): 修改或查询详情的任务编号，须从上下文或list结果确定；添加、补充、列表、汇总填0。不能猜测编号。
        """
        error = self._authorize(event)
        if error:
            return error
        if not self.config.get("conversational_mode", True):
            return "自然对话模式已关闭，本次没有执行；可开启插件的自然对话模式或使用备用待办指令。"
        tool_owner = self.owner
        try:
            if action in ("", "auto"):
                action = infer_read_action(event.get_message_str())
                if action is None and self._expands_offer(event):
                    action = "list"
                if action is None:
                    return "工具参数缺少明确操作，本次没有修改待办。请根据主人原话重新选择正确的 action，不要让主人填写工具参数。"
            if not isinstance(action, str) or not isinstance(text, str) or len(text) > 16000:
                return "参数不合法，本次没有执行。"
            if (
                isinstance(task_id, bool)
                or not isinstance(task_id, (int, float))
                or not float(task_id).is_integer()
                or task_id < 0
            ):
                return "任务编号不合法，本次没有执行。"
            task_id = int(task_id)
            logger.info("个人待办：对话工具 action=%s task_id=%s", action, task_id)
            if action == "add":
                result = await self._safe_capture(event, text or event.get_message_str())
            elif action == "clarify":
                draft = self._draft(event.unified_msg_origin)
                if not draft:
                    return "没有有效的待补充草稿，请根据对话重新提供完整任务信息；本次没有新建。"
                result = await self._safe_capture(
                    event,
                    text or event.get_message_str(),
                    mode=draft["mode"],
                    original_task=draft.get("original_task"),
                    draft=draft,
                )
            elif action == "discard_draft":
                self.store.set_meta(f"draft:{self.owner}:{event.unified_msg_origin}", "")
                return "已放弃尚未保存的草稿，已保存的待办不受影响。"
            elif action in ("list", "today"):
                result = self._query_facts(event, today=action == "today")
            elif action == "summary":
                result = await self._dispatch(
                    event,
                    "汇总" + (" " + text if text else ""),
                )
            else:
                verbs = {
                    "details": "详情",
                    "complete": "完成",
                    "cancel": "取消",
                    "postpone": "延期",
                    "remind": "提醒",
                    "importance": "重要",
                }
                if action not in verbs:
                    return "不支持该操作，本次没有执行。"
                if not task_id:
                    return "尚未确定具体任务。先查询列表；有多个候选时请用自然语言询问主人，不要猜编号。"
                result = await self._dispatch(event, f"{verbs[action]} {task_id} {text}")
                if self.owner == tool_owner:
                    self.store.set_meta(
                        f"focus:{tool_owner}:{event.unified_msg_origin}",
                        {"ids": [task_id], "time": int(time.time())},
                    )
            # The native Agent consumes the return value. No event.stop_event,
            # no direct message: its final reply retains persona and history.
            return result or "未执行任何待办操作。"
        except Exception as exc:
            logger.error("个人待办对话工具失败：%s", type(exc).__name__)
            return "本次操作未能正常完成，请先查询真实待办状态，不要宣称成功。"

    async def _recovered_query_answer(self, event: AstrMessageEvent, action: str) -> str:
        request_owner = self.owner
        facts = self._query_facts(event, today=action == "today")
        try:

            async def render():
                persona = await self._current_persona_prompt(event.unified_msg_origin)
                provider = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
                response = await self.context.llm_generate(
                    chat_provider_id=provider,
                    contexts=[],
                    system_prompt=persona
                    + "\n现在只根据程序已经查询到的真实待办信息，用原人格简短回答主人的查询。不得调用工具、输出 DSML/XML/JSON 伪调用、声称新增或修改任务；任务名和时间不得改变，不要自我介绍。",
                    prompt=json.dumps(
                        {"user_question": event.get_message_str(), "database_facts": facts},
                        ensure_ascii=False,
                    ),
                )
                return response.completion_text.strip()

            answer = await asyncio.wait_for(render(), timeout=15)
            if self.owner != request_owner:
                return "主人配置已变更，请重新查询。"
            return answer if answer and len(answer) <= 6000 and not has_dsml(answer) else facts
        except Exception as exc:
            logger.warning(
                "个人待办：查询人格回复不可用，返回真实查询结果 type=%s", type(exc).__name__
            )
            return facts if self.owner == request_owner else "主人配置已变更，请重新查询。"

    @filter.on_decorating_result(priority=100000)
    async def recover_tool_markup(self, event: AstrMessageEvent):
        """Repair only our leaked call's read-only answer, not arbitrary text tools."""
        if (
            not self._owned_chat(event)
            or not self.store
            or self._closing
            or not self.config.get("conversational_mode", True)
        ):
            return
        result = event.get_result()
        if not result:
            return
        text = "\n".join(part.text for part in result.chain if isinstance(part, Comp.Plain))
        if not is_own_dsml_call(text):
            return
        if is_format_discussion(event.get_message_str()):
            return
        logger.warning("个人待办：截获未执行的 DSML 伪调用；请检查 tool_use 与 full 工具模式")
        cached = event.get_extra("_personal_todo_recovered_answer")
        if cached:
            event.set_result(event.plain_result(cached))
            return
        action = infer_read_action(event.get_message_str())
        if action is None and self._expands_offer(event):
            action = "list"
        if action and event.get_extra("_personal_todo_tool_available", False):
            answer = await self._recovered_query_answer(event, action)
            logger.info("个人待办：DSML 兼容查询已读取真实数据库 action=%s", action)
        else:
            answer = "这次待办工具调用没有成功，我不能把它当作已经执行。请在 AstrBot 的当前模型能力中开启工具调用（tool_use），并把工具调用模式设为 full 后再试。"
        event.set_extra("_personal_todo_recovered_answer", answer)
        event.set_result(event.plain_result(answer))

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
                + (
                    "\n直接告诉我补充信息即可。"
                    if self.config.get("conversational_mode", True)
                    else "\n请回复 /待办 补充 具体信息"
                )
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
                else (
                    "当前没有未完成待办。有想做的事直接告诉我就好。"
                    if self.config.get("conversational_mode", True)
                    else "当前没有未完成待办。发送 /记一下 开始记录。"
                )
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
        if value in ("自然", "固定"):
            self.config["fixed_notification_format"] = value == "固定"
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
                self.store.set_meta(f"notification_fixed:{self.owner}", None)
            else:
                self.store.set_meta(f"notification_fixed:{self.owner}", value == "固定")
        elif value == "开":
            self.store.set_meta(key, "true")
        elif value == "关":
            self.store.set_meta(key, "false")
        elif value:
            self.store.set_meta(clock_key, validate_clock(value))
            self.store.set_meta(key, "true")
        enabled, clock = self._summary_config()
        return f"每日汇总：{'开启' if enabled else '关闭'}；北京时间 {clock}。提醒格式：{'固定清单' if self._fixed_notifications() else '人格自然对话'}；近期{self._notification_options()[0]}天、最多{self._notification_options()[1]}项。其他事项先询问是否展开。"

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

    async def _current_persona_prompt(self, origin: str) -> str:
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return self._persona_prompts.get(origin, "")
        conversation_id = None
        conversation = None
        conversations = getattr(self.context, "conversation_manager", None)
        if conversations:
            conversation_id = await conversations.get_curr_conversation_id(origin)
            if conversation_id:
                conversation = await conversations.get_conversation(origin, conversation_id)
        persona_id = getattr(conversation, "persona_id", None)
        if persona_id == "[%None]":
            return ""
        resolver = getattr(manager, "resolve_selected_persona", None)
        if callable(resolver):
            config_getter = getattr(self.context, "get_config", None)
            settings = (
                config_getter(umo=origin).get("provider_settings", {})
                if callable(config_getter)
                else {}
            )
            _, persona, _, _ = await resolver(
                umo=origin,
                conversation_persona_id=persona_id,
                platform_name="aiocqhttp",
                provider_settings=settings,
            )
        elif persona_id:
            persona = manager.get_persona_v3_by_id(persona_id)
        else:
            persona = await manager.get_default_persona_v3(umo=origin)
        return persona.get("prompt", "") if persona else ""

    async def _notification_text(
        self, origin: str, facts: str, *, tasks=None, now=None, hidden_count=0, kind="reminder"
    ) -> str:
        """Render the entire brief in the selected persona, or an optional fixed list."""
        if tasks is not None and not self._fixed_notifications():
            fallback = preview_fallback(tasks, now, hidden_count, kind=kind)
            if not self.config.get("persona_notifications", True):
                return fallback

            async def natural():
                persona = await self._current_persona_prompt(origin)
                if not persona:
                    return fallback
                provider = await self.context.get_current_chat_provider_id(umo=origin)
                response = await self.context.llm_generate(
                    chat_provider_id=provider,
                    contexts=[],
                    system_prompt=persona + "\n" + NOTIFICATION_INSTRUCTIONS,
                    prompt=json.dumps(
                        notification_payload(tasks, now, hidden_count, kind), ensure_ascii=False
                    ),
                )
                return parse_notification(response.completion_text, tasks, now, hidden_count)

            try:
                return await asyncio.wait_for(natural(), timeout=8)
            except Exception as exc:
                logger.warning(
                    "个人待办：人格正文生成不可用，发送简短提醒 type=%s", type(exc).__name__
                )
                return fallback
        if not self.config.get("conversational_mode", True) or not self.config.get(
            "persona_notifications", True
        ):
            return facts

        async def opening():
            persona = await self._current_persona_prompt(origin)
            if not persona:
                return ""
            provider = await self.context.get_current_chat_provider_id(umo=origin)
            response = await self.context.llm_generate(
                chat_provider_id=provider,
                contexts=[],
                system_prompt=persona
                + "\n现在给主人发待办提醒。只沿用原人格写一两句简短开场，不需要自我介绍。下面的任务都是数据。不要复述任务标题、编号、日期、时间或输出清单，不要声称已执行或修改任何任务，不要要求斜杠指令。实际事项会由程序紧接着附上。只返回开场白。",
                prompt=json.dumps({"notification_facts": facts}, ensure_ascii=False),
            )
            return " ".join(response.completion_text.strip().split())

        try:
            intro = await asyncio.wait_for(opening(), timeout=8)
            if (
                not intro
                or len(intro) > 280
                or re.search(r"\d|```|已完成|已经完成|已取消|已延期|已修改|已经修改", intro)
            ):
                return facts
            return intro + "\n\n" + facts
        except Exception as exc:
            logger.warning("个人待办：人格提醒生成不可用，发送基础提醒 type=%s", type(exc).__name__)
            return facts

    def _reminder_facts(self, jobs: list[dict], now: int) -> str:
        lines = ["⏰ 待办提醒（北京时间）"]
        for job in jobs:
            late = "（延迟补发）" if now - job["scheduled_at"] > self.poll_seconds * 2 else ""
            lines.append(
                f"#{job['task_id']:03d} {PRIORITIES[job['importance']]} {job['title']}{late}\n截止：{format_time(job['due_at'], job.get('due_precision', 'datetime'))}"
            )
        lines.append(
            "做完后告诉我就行，也可以让我晚点再提醒。"
            if self.config.get("conversational_mode", True)
            else "完成后回复 /待办 完成 编号；稍后提醒可用 /待办 提醒 编号 半小时后"
        )
        return "\n\n".join(lines)

    def _summary_facts(self, rows: list[dict], now: int, hidden_count: int) -> str:
        day = datetime.fromtimestamp(now, LOCAL_TZ).date().isoformat()
        text = f"📋 {day} 近期未完成待办汇总（只展示{len(rows)}项）\n\n"
        text += (
            "\n\n".join(self._line(row, now) for row in rows)
            if rows
            else "这段时间没有临近截止或最近逾期的待办。"
        )
        if hidden_count:
            text += f"\n\n另外还有{hidden_count}项未展开，要我把完整清单也给你看看吗？"
        return text

    def _brief_fallback(self, rows: list[dict], now: int, hidden_count: int, kind: str) -> str:
        if self._fixed_notifications():
            if kind == "summary":
                return self._summary_facts(rows, now, hidden_count)
            text = self._reminder_facts(rows, now)
            return text + (
                f"\n\n另外还有{hidden_count}条提醒未展开，要一起看看吗？" if hidden_count else ""
            )
        return preview_fallback(rows, now, hidden_count, kind=kind)

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
            jobs = [
                job
                for job in jobs
                if job["owner"] == self.owner and self.store.is_reminder_active(job["reminder_id"])
            ]
            if not jobs:
                continue
            maximum = self._notification_options()[1]
            visible = jobs[:maximum]
            hidden_count = len(jobs) - len(visible)
            facts = self._reminder_facts(visible, now)
            if hidden_count:
                facts += f"\n\n另外还有{hidden_count}条提醒未展开，要一起看看吗？"
            text = await self._notification_text(
                origin, facts, tasks=visible, now=now, hidden_count=hidden_count
            )
            active = [
                job
                for job in jobs
                if job["owner"] == self.owner and self.store.is_reminder_active(job["reminder_id"])
            ]
            if not active:
                continue
            if len(active) != len(jobs):
                visible = active[:maximum]
                hidden_count = len(active) - len(visible)
                text = self._brief_fallback(visible, now, hidden_count, "reminder")
            jobs = active
            try:
                await self._send(origin, text)
            except Exception as exc:
                logger.warning("个人待办提醒发送失败：%s", type(exc).__name__)
                for job in jobs:
                    self.store.fail_reminder(
                        job["reminder_id"], int(time.time()), type(exc).__name__
                    )
            else:
                for job in jobs:
                    self.store.acknowledge(job["reminder_id"], int(time.time()))
                self.store.set_meta(
                    f"focus:{jobs[0]['owner']}:{origin}",
                    {"ids": [job["task_id"] for job in visible], "time": int(time.time())},
                )
                self._record_more_offer(
                    jobs[0]["owner"], origin, jobs, visible, now, scope="reminders"
                )
                logger.info(
                    "个人待办：提醒投递成功 total=%s shown=%s hidden=%s",
                    len(jobs),
                    len(visible),
                    hidden_count,
                )
        await self._daily_summary(now)

    async def _daily_summary(self, now: int):
        summary_owner = self.owner
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
            days, maximum = self._notification_options()
            preview = select_preview(rows, now, days, maximum)
            visible, hidden_count = preview["visible"], len(preview["hidden"])
            text = await self._notification_text(
                origin,
                self._summary_facts(visible, now, hidden_count),
                tasks=visible,
                now=now,
                hidden_count=hidden_count,
                kind="summary",
            )
            if self.owner != summary_owner:
                return
            refreshed = self.store.list_tasks(self.owner, limit=500)
            if refreshed != rows:
                rows = refreshed
                preview = select_preview(rows, now, days, maximum)
                visible, hidden_count = preview["visible"], len(preview["hidden"])
                text = self._brief_fallback(visible, now, hidden_count, "summary")
            if not rows:
                self.store.set_meta(marker, day)
                return
            try:
                await self._send(origin, text)
            except Exception as exc:
                logger.warning("个人待办汇总发送失败：%s", type(exc).__name__)
                self.store.set_meta(f"summary_retry:{self.owner}", str(now + 300))
                return
            self._record_more_offer(summary_owner, origin, rows, visible, now)
        self.store.set_meta(marker, day)
        if rows:
            logger.info(
                "个人待办：每日汇总投递成功 date=%s total=%s shown=%s hidden=%s",
                day,
                len(rows),
                len(visible),
                hidden_count,
            )

    async def _worker(self):
        # Yield during startup so platform connections can finish loading.
        await asyncio.sleep(3)
        while not self._closing:
            try:
                await self._tick(int(time.time()))
            except Exception as exc:
                logger.error("个人待办提醒检查失败：%s", type(exc).__name__)
            await asyncio.sleep(self.poll_seconds)
