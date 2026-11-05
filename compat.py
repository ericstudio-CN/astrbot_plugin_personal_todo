"""Detect leaked tool syntax; only infer harmless queries from the user's text."""

from __future__ import annotations

import re

_INVOKE = re.compile(
    r"<\s*[|｜]\s*DSML\s*[|｜]\s*invoke\s+name\s*=\s*['\"]([^'\"]+)['\"]\s*>",
    re.IGNORECASE,
)


def has_dsml(value: str) -> bool:
    return bool(re.search(r"<\s*/?\s*[|｜]\s*DSML\s*[|｜]", value, re.IGNORECASE))


def is_own_dsml_call(value: str) -> bool:
    """Do not touch another plugin's tools or execute any markup arguments."""
    names = _INVOKE.findall(value)
    return bool(names) and all(name == "personal_todo" for name in names)


def is_format_discussion(text: str) -> bool:
    return bool(
        re.search(
            r"DSML|XML|工具调用|personal_todo.{0,15}(示例|格式|代码|如何|怎么)|(示例|格式|代码|如何|怎么).{0,15}personal_todo",
            text,
            re.IGNORECASE,
        )
    )


def infer_read_action(user_text: str) -> str | None:
    """A deliberately small read-only fallback, never a mutation classifier."""
    text = re.sub(r"\s+", "", user_text)
    if is_format_discussion(text):
        return None
    if re.search(
        r"删除|取消|完成|写完|做完|改到|改成|修改|延期|推迟|延后|记下|记住|添加|创建|设置|不用提醒|不必提醒|别提醒|分钟后提醒|小时后提醒",
        text,
    ):
        # '没完成' is a query filter, so allow that phrase only.
        remainder = re.sub(r"未完成|没完成|没有完成", "", text)
        if re.search(
            r"删除|取消|完成|写完|做完|改到|改成|修改|延期|推迟|延后|记下|记住|添加|创建|设置|不用提醒|不必提醒|别提醒|分钟后提醒|小时后提醒",
            remainder,
        ):
            return None
    if not re.search(r"待办|提醒|任务|事项|要做|没做|未做|未完成|没完成|还有什么事|有什么事", text):
        return None
    if not re.search(
        r"查|看看|看下|看一下|列|有哪些|有什么|还有什么|有啥|要做什么|该做什么|没做|未做|未完成|没完成",
        text,
    ):
        return None
    return "today" if re.search(r"今天|今日", text) else "list"
