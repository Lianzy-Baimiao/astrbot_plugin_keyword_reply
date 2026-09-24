# -*- coding: utf-8 -*-
"""关键字规则的归一化与匹配：纯逻辑，不依赖 astrbot，可本地直接单测。

一条规则四个必填字段，和面板表单一一对应：
    关键字 keyword / 匹配类型 match / 回复 reply / 格式 format

匹配类型和格式都吃中文别名（面板下拉、聊天命令、别人导出的旧 json 混着用也能认），
统一经 normalize_match / normalize_format 收敛成内部英文枚举再存盘。
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 匹配类型
# ---------------------------------------------------------------------------

MATCH_EXACT = "exact"
MATCH_CONTAINS = "contains"
MATCH_PREFIX = "prefix"
MATCH_SUFFIX = "suffix"
MATCH_REGEX = "regex"

MATCH_TYPES: tuple[str, ...] = (
    MATCH_EXACT,
    MATCH_CONTAINS,
    MATCH_PREFIX,
    MATCH_SUFFIX,
    MATCH_REGEX,
)

MATCH_LABELS: dict[str, str] = {
    MATCH_EXACT: "精准匹配",
    MATCH_CONTAINS: "模糊匹配",
    MATCH_PREFIX: "前缀匹配",
    MATCH_SUFFIX: "后缀匹配",
    MATCH_REGEX: "正则匹配",
}

_MATCH_ALIASES: dict[str, str] = {
    "exact": MATCH_EXACT,
    "full": MATCH_EXACT,
    "equal": MATCH_EXACT,
    "fullmatch": MATCH_EXACT,
    "精准匹配": MATCH_EXACT,
    "精确匹配": MATCH_EXACT,
    "精准": MATCH_EXACT,
    "精确": MATCH_EXACT,
    "全匹配": MATCH_EXACT,
    "完全匹配": MATCH_EXACT,
    "等于": MATCH_EXACT,
    "contains": MATCH_CONTAINS,
    "contain": MATCH_CONTAINS,
    "fuzzy": MATCH_CONTAINS,
    "keyword": MATCH_CONTAINS,
    "模糊匹配": MATCH_CONTAINS,
    "模糊": MATCH_CONTAINS,
    "包含匹配": MATCH_CONTAINS,
    "包含": MATCH_CONTAINS,
    "prefix": MATCH_PREFIX,
    "startswith": MATCH_PREFIX,
    "前缀匹配": MATCH_PREFIX,
    "前缀": MATCH_PREFIX,
    "开头匹配": MATCH_PREFIX,
    "开头": MATCH_PREFIX,
    "suffix": MATCH_SUFFIX,
    "endswith": MATCH_SUFFIX,
    "后缀匹配": MATCH_SUFFIX,
    "后缀": MATCH_SUFFIX,
    "结尾匹配": MATCH_SUFFIX,
    "结尾": MATCH_SUFFIX,
    "regex": MATCH_REGEX,
    "regexp": MATCH_REGEX,
    "re": MATCH_REGEX,
    "正则匹配": MATCH_REGEX,
    "正则": MATCH_REGEX,
    "正则表达式": MATCH_REGEX,
}

# 多条同时命中时谁优先：精准 > 前缀/后缀 > 正则 > 模糊。
# 「活动地址」精准命中时不该被某条 contains=「活动」的规则抢走。
_MATCH_SPECIFICITY: dict[str, int] = {
    MATCH_EXACT: 0,
    MATCH_PREFIX: 1,
    MATCH_SUFFIX: 1,
    MATCH_REGEX: 2,
    MATCH_CONTAINS: 3,
}

# ---------------------------------------------------------------------------
# 回复格式
# ---------------------------------------------------------------------------

FORMAT_TEXT = "text"
FORMAT_MARKDOWN = "markdown"

FORMAT_TYPES: tuple[str, ...] = (FORMAT_TEXT, FORMAT_MARKDOWN)

FORMAT_LABELS: dict[str, str] = {
    FORMAT_TEXT: "文本",
    FORMAT_MARKDOWN: "MD格式",
}

_FORMAT_ALIASES: dict[str, str] = {
    "text": FORMAT_TEXT,
    "plain": FORMAT_TEXT,
    "plaintext": FORMAT_TEXT,
    "txt": FORMAT_TEXT,
    "文本": FORMAT_TEXT,
    "纯文本": FORMAT_TEXT,
    "文本格式": FORMAT_TEXT,
    "markdown": FORMAT_MARKDOWN,
    "md": FORMAT_MARKDOWN,
    "md格式": FORMAT_MARKDOWN,
    "markdown格式": FORMAT_MARKDOWN,
    "富文本": FORMAT_MARKDOWN,
}

# ---------------------------------------------------------------------------
# 字段上限：面板和聊天命令共用，越界直接 ValueError（中文文案直接回给用户）
# ---------------------------------------------------------------------------

MAX_KEYWORD_LEN = 200
MAX_REPLY_LEN = 5000
MAX_PRIORITY = 999


def normalize_match(value: Any, *, default: str = MATCH_EXACT) -> str:
    """匹配类型归一化；认不出来的一律落回 default。"""
    key = str(value or "").strip().lower()
    if not key:
        return default
    if key in MATCH_TYPES:
        return key
    return _MATCH_ALIASES.get(key, default)


def normalize_format(value: Any, *, default: str = FORMAT_TEXT) -> str:
    """回复格式归一化；认不出来的一律落回 default（文本最安全）。"""
    key = str(value or "").strip().lower()
    if not key:
        return default
    if key in FORMAT_TYPES:
        return key
    return _FORMAT_ALIASES.get(key, default)


def match_label(value: Any) -> str:
    return MATCH_LABELS.get(normalize_match(value), MATCH_LABELS[MATCH_EXACT])


def format_label(value: Any) -> str:
    return FORMAT_LABELS.get(normalize_format(value), FORMAT_LABELS[FORMAT_TEXT])


# ---------------------------------------------------------------------------
# 正则编译缓存：每条消息都要过一遍所有规则，不缓存会反复 re.compile
# ---------------------------------------------------------------------------

_REGEX_CACHE: dict[tuple[str, int], re.Pattern[str] | None] = {}
_REGEX_CACHE_MAX = 512


def compile_regex(pattern: str, *, ignore_case: bool = False) -> re.Pattern[str] | None:
    """编译并缓存正则；写错的正则返回 None，不抛异常（免得一条烂规则拖垮整条消息链）。"""
    flags = re.IGNORECASE if ignore_case else 0
    key = (pattern, flags)
    if key in _REGEX_CACHE:
        return _REGEX_CACHE[key]
    try:
        compiled: re.Pattern[str] | None = re.compile(pattern, flags)
    except re.error:
        compiled = None
    if len(_REGEX_CACHE) >= _REGEX_CACHE_MAX:
        _REGEX_CACHE.clear()
    _REGEX_CACHE[key] = compiled
    return compiled


def rule_key(keyword: str, match: str) -> str:
    """查重键：同一「关键字 + 匹配类型」算同一条规则。"""
    return f"{normalize_match(match)}\u0000{str(keyword or '').strip().lower()}"


def new_rule_id() -> str:
    return uuid.uuid4().hex[:12]


def normalize_rule(
    payload: Any,
    *,
    existing: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """把面板/命令/导入文件传来的原始字典校验成一条干净规则。

    existing 非空时是「编辑」：没传的字段沿用旧值，id / 命中数 / 创建时间保留。
    字段不合法直接抛 ValueError，消息是给用户看的中文。
    """
    if not isinstance(payload, dict):
        raise ValueError("规则必须是一个对象")
    base: dict[str, Any] = dict(existing or {})
    now = time.time() if now is None else now

    def pick(field: str, fallback: Any = "") -> Any:
        if field in payload:
            return payload[field]
        return base.get(field, fallback)

    keyword = str(pick("keyword") or "").strip()
    if not keyword:
        raise ValueError("关键字不能为空")
    if len(keyword) > MAX_KEYWORD_LEN:
        raise ValueError(f"关键字太长（最多 {MAX_KEYWORD_LEN} 字）")

    match = normalize_match(pick("match", MATCH_EXACT))
    if match == MATCH_REGEX and compile_regex(keyword) is None:
        raise ValueError(f"正则写错了，无法编译：{keyword}")

    reply = str(pick("reply") or "")
    if not reply.strip():
        raise ValueError("回复内容不能为空")
    if len(reply) > MAX_REPLY_LEN:
        raise ValueError(f"回复太长（最多 {MAX_REPLY_LEN} 字）")

    fmt = normalize_format(pick("format", FORMAT_TEXT))

    raw_priority = pick("priority", 0)
    try:
        priority = int(str(raw_priority).strip() or 0)
    except (TypeError, ValueError):
        raise ValueError("优先级必须是整数") from None
    priority = max(-MAX_PRIORITY, min(MAX_PRIORITY, priority))

    raw_enabled = pick("enabled", True)
    if isinstance(raw_enabled, str):
        enabled = raw_enabled.strip().lower() not in {"0", "false", "no", "off", "否", "关"}
    else:
        enabled = bool(raw_enabled)

    try:
        hits = max(0, int(base.get("hits", 0) or 0))
    except (TypeError, ValueError):
        hits = 0

    return {
        "id": str(base.get("id") or payload.get("id") or new_rule_id()),
        "keyword": keyword,
        "match": match,
        "reply": reply,
        "format": fmt,
        "priority": priority,
        "enabled": enabled,
        "hits": hits,
        "created_at": float(base.get("created_at") or now),
        "updated_at": now,
    }


def decorate_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """给面板补上中文标签，前端不用自己维护一份枚举映射。"""
    out = dict(rule)
    out["match_label"] = match_label(rule.get("match"))
    out["format_label"] = format_label(rule.get("format"))
    return out


def describe_rule(rule: dict[str, Any]) -> str:
    """一行文字概括一条规则，聊天命令 /关键字 列表 用。"""
    reply = str(rule.get("reply") or "").replace("\n", " ")
    if len(reply) > 30:
        reply = reply[:30] + "…"
    flag = "" if rule.get("enabled", True) else "（已停用）"
    return (
        f"[{rule.get('id')}] {rule.get('keyword')} | {match_label(rule.get('match'))}"
        f" | {reply} | {format_label(rule.get('format'))}{flag}"
    )


def rule_matches(rule: dict[str, Any], text: str, *, ignore_case: bool = False) -> bool:
    """单条规则是否命中 text。text 由调用方 strip 过。"""
    keyword = str(rule.get("keyword") or "")
    if not keyword or not text:
        return False
    match = normalize_match(rule.get("match"))
    if match == MATCH_REGEX:
        compiled = compile_regex(keyword, ignore_case=ignore_case)
        return bool(compiled and compiled.search(text))
    haystack, needle = (text.lower(), keyword.lower()) if ignore_case else (text, keyword)
    if match == MATCH_EXACT:
        return haystack == needle
    if match == MATCH_PREFIX:
        return haystack.startswith(needle)
    if match == MATCH_SUFFIX:
        return haystack.endswith(needle)
    return needle in haystack


def sort_key(rule: dict[str, Any]) -> tuple[Any, ...]:
    """优先级高的在前；同级按「精准 > 前缀/后缀 > 正则 > 模糊」，再按关键字长的在前。"""
    return (
        -int(rule.get("priority", 0) or 0),
        _MATCH_SPECIFICITY.get(normalize_match(rule.get("match")), 9),
        -len(str(rule.get("keyword") or "")),
        float(rule.get("created_at") or 0),
    )


def find_matches(
    rules: Sequence[dict[str, Any]],
    text: str,
    *,
    ignore_case: bool = False,
    include_disabled: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """返回命中 text 的规则，已按 sort_key 排好序。limit=1 就是「取最该回的那条」。"""
    text = str(text or "").strip()
    if not text:
        return []
    hit = [
        rule
        for rule in rules
        if (include_disabled or rule.get("enabled", True))
        and rule_matches(rule, text, ignore_case=ignore_case)
    ]
    hit.sort(key=sort_key)
    if limit is not None and limit >= 0:
        return hit[:limit]
    return hit


# ---------------------------------------------------------------------------
# 回复里的占位符
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{(sender|group|keyword|date|time|datetime)\}")

PLACEHOLDERS: tuple[str, ...] = ("sender", "group", "keyword", "date", "time", "datetime")


def render_reply(template: str, values: dict[str, Any] | None = None) -> str:
    """替换回复里的 {sender} {group} {keyword} {date} {time} {datetime}。

    故意不用 str.format —— 回复里写 markdown 表格或 JSON 时大括号很常见，
    format 会直接 KeyError。这里只认白名单里的占位符，其余大括号原样留着。
    """
    values = values or {}
    return _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), "")), str(template or ""))


def parse_rule_line(line: str) -> dict[str, Any]:
    """解析聊天命令里的一条规则：关键字|匹配类型|回复|格式（分隔符 | 或 ｜）。

    后两段可省：匹配类型默认精准匹配，格式默认文本。
    """
    parts = [seg.strip() for seg in re.split(r"[|｜]", str(line or ""))]
    if not parts or not parts[0]:
        raise ValueError("格式：关键字|匹配类型|回复|格式，例：活动地址|精准匹配|一个活动地址|MD格式")
    keyword = parts[0]
    match = parts[1] if len(parts) > 1 and parts[1] else MATCH_EXACT
    reply = parts[2] if len(parts) > 2 else ""
    fmt = parts[3] if len(parts) > 3 and parts[3] else FORMAT_TEXT
    if not reply:
        raise ValueError("少了「回复」这一段：关键字|匹配类型|回复|格式")
    return {"keyword": keyword, "match": match, "reply": reply, "format": fmt}


def stats(rules: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """面板顶部的几个数字。"""
    rules = list(rules)
    by_match: dict[str, int] = {name: 0 for name in MATCH_TYPES}
    by_format: dict[str, int] = {name: 0 for name in FORMAT_TYPES}
    enabled = 0
    hits = 0
    for rule in rules:
        by_match[normalize_match(rule.get("match"))] += 1
        by_format[normalize_format(rule.get("format"))] += 1
        if rule.get("enabled", True):
            enabled += 1
        try:
            hits += int(rule.get("hits", 0) or 0)
        except (TypeError, ValueError):
            pass
    return {
        "total": len(rules),
        "enabled": enabled,
        "disabled": len(rules) - enabled,
        "hits": hits,
        "by_match": by_match,
        "by_format": by_format,
    }
