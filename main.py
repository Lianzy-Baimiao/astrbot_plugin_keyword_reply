# -*- coding: utf-8 -*-
"""关键字回复插件（AstrBot）：关键字 / 匹配类型 / 回复 / 格式 四字段规则，带 Web 面板。

规则存在 data/plugin_data/astrbot_plugin_keyword_reply/rules.json，分「全局」和
「按会话」两级：会话规则优先，同一关键字会盖掉全局那条。

面板在 AstrBot WebUI → 插件 → 关键字回复 → 打开面板（需要 AstrBot >= 4.24.1）。
聊天里也能管：/关键字 帮助。
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

# AstrBot 以 data.plugins.<name> 加载 main.py，keyword_reply 子包要显式进 sys.path。
_PLUGIN_ROOT = Path(__file__).resolve().parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from keyword_reply.page import KeywordPageController
from keyword_reply.rules import (
    FORMAT_MARKDOWN,
    describe_rule,
    format_label,
    match_label,
    parse_rule_line,
    render_reply,
    rule_key,
)
from keyword_reply.store import SCOPE_GLOBAL, RuleStore, default_rules

PLUGIN_NAME = "astrbot_plugin_keyword_reply"

HELP_TEXT = """关键字回复
面板：AstrBot WebUI → 插件 → 关键字回复 → 打开面板（推荐，四个字段下拉可选）

聊天命令（管理员）：
/关键字 列表            看本群规则（带 全局 看全局规则）
/关键字 添加 关键字|匹配类型|回复|格式
/关键字 删除 <id 或 关键字>
/关键字 开关 <id>        启用/停用
/关键字 测试 <文本>      看这句话会命中哪条
加「全局」操作全局规则，如 /关键字 全局添加 ...

例：/关键字 添加 活动地址|精准匹配|一个活动地址|MD格式
匹配类型：精准匹配 / 模糊匹配 / 前缀匹配 / 后缀匹配 / 正则匹配
格式：文本 / MD格式
回复里可用占位符：{sender} {group} {keyword} {date} {time} {datetime}"""


class KeywordReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = RuleStore(data_dir / "rules.json")
        self.store.load()

        # 首次启动塞两条示例，面板不至于空白
        if not self.store.list_rules(SCOPE_GLOBAL) and not self.store.scope_names()[1:]:
            try:
                self.store._import_sync(default_rules(), SCOPE_GLOBAL)
                self.store.save()
            except Exception as exc:
                logger.warning(f"[keyword_reply] 写入示例规则失败: {exc}")

        # 面板里「范围」下拉要能列出机器人见过的会话：umo -> 显示名
        self.seen_sessions: dict[str, str] = {}
        # 冷却：umo -> 上次回复时间
        self._last_reply: dict[str, float] = {}

        self.page = KeywordPageController(context, self.store, plugin=self)
        self.page.register_routes()

    # ------------------------------------------------------------------
    # 配置读取（每次现读，面板改完配置立即生效，不用重启）
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    @property
    def auto_reply_enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    @property
    def ignore_case(self) -> bool:
        return bool(self._cfg("ignore_case", True))

    @property
    def markdown_mode(self) -> str:
        return str(self._cfg("markdown_mode", "image")).strip().lower()

    @property
    def cooldown(self) -> int:
        try:
            return max(0, int(self._cfg("cooldown", 0)))
        except (TypeError, ValueError):
            return 0

    @property
    def stop_propagation(self) -> bool:
        return bool(self._cfg("stop_event", True))

    def _list_cfg(self, key: str) -> set[str]:
        raw = self._cfg(key, [])
        if isinstance(raw, str):
            raw = [raw]
        out = set()
        for item in raw or []:
            text = str(item or "").strip()
            if text:
                out.add(text)
        return out

    # ------------------------------------------------------------------
    # 会话过滤
    # ------------------------------------------------------------------

    def _session_allowed(self, event: AstrMessageEvent) -> bool:
        """白名单非空时只认名单内；黑名单里的直接不回。两者都吃 umo 和裸群号。"""
        umo = event.unified_msg_origin
        gid = str(event.get_group_id() or "")
        tokens = {umo, gid} - {""}
        blacklist = self._list_cfg("blacklist")
        if tokens & blacklist:
            return False
        whitelist = self._list_cfg("whitelist")
        if whitelist and not (tokens & whitelist):
            return False
        if not self._cfg("reply_in_private", True) and not gid:
            return False
        return True

    def _remember_session(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        if not umo or umo in self.seen_sessions:
            return
        gid = str(event.get_group_id() or "")
        self.seen_sessions[umo] = f"群 {gid}" if gid else f"私聊 {event.get_sender_id()}"
        # 只留最近 200 个，免得长期运行越攒越多
        if len(self.seen_sessions) > 200:
            for key in list(self.seen_sessions)[:50]:
                self.seen_sessions.pop(key, None)

    # ------------------------------------------------------------------
    # 自动回复
    # ------------------------------------------------------------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """所有消息过一遍规则。"""
        if not self.auto_reply_enabled:
            return
        text = (event.message_str or "").strip()
        if not text:
            return
        self._remember_session(event)
        if not self._session_allowed(event):
            return
        # 别自己触发自己（get_self_id 不是所有平台适配器都有）
        self_id = getattr(event, "get_self_id", None)
        if callable(self_id):
            try:
                if str(event.get_sender_id() or "") == str(self_id() or ""):
                    return
            except Exception:
                pass
        # 本插件自己的管理命令不参与关键字匹配
        if text.lstrip("/").startswith(("关键字", "关键词回复")):
            return

        hits = self.store.match(
            text,
            event.unified_msg_origin,
            ignore_case=self.ignore_case,
            limit=1,
        )
        if not hits:
            return
        rule = hits[0]

        if self.cooldown:
            now = time.monotonic()
            last = self._last_reply.get(event.unified_msg_origin, 0.0)
            if now - last < self.cooldown:
                return
            self._last_reply[event.unified_msg_origin] = now

        self.store.bump_hits(rule["id"], event.unified_msg_origin)
        async for result in self._emit(event, rule, text):
            yield result
        if self.stop_propagation:
            event.stop_event()

    async def _emit(self, event: AstrMessageEvent, rule: dict[str, Any], text: str):
        """把一条规则的回复发出去。MD 格式默认渲染成图片，失败回落纯文本。"""
        now = time.localtime()
        reply = render_reply(
            rule.get("reply", ""),
            {
                "sender": event.get_sender_name() or event.get_sender_id() or "",
                "group": str(event.get_group_id() or ""),
                "keyword": rule.get("keyword", text),
                "date": time.strftime("%Y-%m-%d", now),
                "time": time.strftime("%H:%M:%S", now),
                "datetime": time.strftime("%Y-%m-%d %H:%M:%S", now),
            },
        )
        if not reply.strip():
            return

        if rule.get("format") == FORMAT_MARKDOWN and self.markdown_mode == "image":
            url = await self._markdown_image(reply)
            if url:
                yield event.image_result(url)
                return
        yield event.plain_result(reply)

    async def _markdown_image(self, markdown: str) -> str:
        """渲染 markdown 成图片。AstrBot 版本不带 t2i 或渲染失败都返回空串。"""
        renderer = getattr(self, "text_to_image", None)
        if not callable(renderer):
            return ""
        try:
            return await renderer(markdown) or ""
        except Exception as exc:
            logger.warning(f"[keyword_reply] markdown 转图片失败，改发纯文本: {exc}")
            return ""

    # ------------------------------------------------------------------
    # 聊天命令
    # ------------------------------------------------------------------

    @filter.command("关键字", alias={"关键词回复", "关键字回复"})
    async def cmd_keyword(self, event: AstrMessageEvent):
        """/关键字 <子命令> ...，详见 /关键字 帮助。

        参数自己从 message_str 里切：子命令后面跟的是「关键字|匹配类型|回复|格式」，
        里面含空格和 | ，交给框架按空格拆参数会把回复内容切碎。
        """
        raw = (event.message_str or "").strip()
        for prefix in ("/关键字", "关键字回复", "关键词回复", "关键字"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        self._remember_session(event)

        if not raw or raw in {"帮助", "help", "?", "？"}:
            yield event.plain_result(HELP_TEXT)
            return

        # 「全局xxx」或「全局 xxx」都算全局操作
        scope = event.unified_msg_origin
        if raw.startswith("全局"):
            scope = SCOPE_GLOBAL
            raw = raw[2:].strip()
        parts = raw.split(maxsplit=1)
        action = parts[0] if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if action in {"列表", "list", "查看"}:
            yield event.plain_result(self._render_list(scope))
            return
        if action in {"测试", "test"}:
            yield event.plain_result(self._render_test(scope, rest))
            return

        if action in {"添加", "新增", "add", "删除", "移除", "del", "delete", "开关", "toggle"}:
            if not self._is_admin(event):
                yield event.plain_result("只有管理员能改关键字规则")
                return
            try:
                if action in {"添加", "新增", "add"}:
                    yield event.plain_result(await self._do_add(scope, rest))
                elif action in {"开关", "toggle"}:
                    yield event.plain_result(await self._do_toggle(scope, rest))
                else:
                    yield event.plain_result(await self._do_delete(scope, rest))
            except ValueError as exc:
                yield event.plain_result(f"没能完成：{exc}")
            except LookupError as exc:
                yield event.plain_result(str(exc))
            return

        yield event.plain_result(f"不认识的子命令「{action}」。\n\n{HELP_TEXT}")

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                pass
        return str(getattr(event, "role", "") or "").lower() == "admin"

    @staticmethod
    def _scope_name(scope: str) -> str:
        return "全局" if scope == SCOPE_GLOBAL else "本会话"

    def _render_list(self, scope: str) -> str:
        own = self.store.list_rules(scope)
        lines = [f"{self._scope_name(scope)}规则（{len(own)} 条）"]
        lines += [f"  {describe_rule(rule)}" for rule in own] or ["  （空）"]
        if scope != SCOPE_GLOBAL:
            inherited = [
                rule
                for rule in self.store.list_rules(SCOPE_GLOBAL)
                if rule not in own
            ]
            if inherited:
                lines.append(f"另继承全局 {len(inherited)} 条（/关键字 全局列表 查看）")
        return "\n".join(lines)

    def _render_test(self, scope: str, text: str) -> str:
        if not text:
            return "用法：/关键字 测试 <要测试的文本>"
        hits = self.store.match(text, scope, ignore_case=self.ignore_case)
        if not hits:
            return f"「{text}」没有命中任何规则"
        lines = [f"「{text}」命中 {len(hits)} 条，实际会回第一条："]
        lines += [f"  {i + 1}. {describe_rule(rule)}" for i, rule in enumerate(hits[:5])]
        return "\n".join(lines)

    async def _do_add(self, scope: str, rest: str) -> str:
        if not rest:
            return (
                "用法：/关键字 添加 关键字|匹配类型|回复|格式\n"
                "例：/关键字 添加 活动地址|精准匹配|一个活动地址|MD格式"
            )
        payload = parse_rule_line(rest)
        rule = await self.store.add(payload, scope, overwrite=True)
        return (
            f"已加到{self._scope_name(scope)}：{rule['keyword']}"
            f"（{match_label(rule['match'])}，{format_label(rule['format'])}）"
            f"\nid={rule['id']}"
        )

    async def _do_delete(self, scope: str, rest: str) -> str:
        if not rest:
            return "用法：/关键字 删除 <id 或 关键字>"
        target = rest.strip()
        if self.store.get(target, scope):
            result = await self.store.delete([target], scope)
            return f"已删除 {len(result['deleted'])} 条"
        found = self.store.find_by_keyword(target, scope)
        if not found:
            return f"{self._scope_name(scope)}里没有「{target}」这条规则"
        result = await self.store.delete([rule["id"] for rule in found], scope)
        return f"已删除 {len(result['deleted'])} 条（关键字「{target}」）"

    async def _do_toggle(self, scope: str, rest: str) -> str:
        if not rest:
            return "用法：/关键字 开关 <id>"
        target = rest.strip()
        if not self.store.get(target, scope):
            found = self.store.find_by_keyword(target, scope)
            if not found:
                return f"{self._scope_name(scope)}里没有「{target}」这条规则"
            target = found[0]["id"]
        rule = await self.store.toggle(target, scope)
        return f"「{rule['keyword']}」已{'启用' if rule.get('enabled') else '停用'}"

    async def terminate(self):
        try:
            await self.store.flush()
        except Exception as exc:
            logger.warning(f"[keyword_reply] 退出前保存失败: {exc}")
