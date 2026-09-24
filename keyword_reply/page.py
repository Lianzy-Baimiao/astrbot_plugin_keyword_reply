# -*- coding: utf-8 -*-
"""插件 Web 面板的后端接口（AstrBot Plugin Pages）。

路由都注册在 /astrbot_plugin_keyword_reply/page/* 下，前端 pages/keyword-reply/
通过 window.AstrBotPluginPage 的 apiGet / apiPost 调用（bridge 会自动补插件名前缀）。

AstrBot 4.26+ 提供 astrbot.api.web（request/json_response/error_response），
更早的版本只有裸 quart。两套 API 形状不同（request.query vs request.args、
await request.json(...) vs await request.get_json(...)），所以这里统一包一层
_query_* / _read_json，让上层 handler 只写一遍。
"""

from __future__ import annotations

import json
from typing import Any, Callable

try:  # AstrBot >= 4.26
    from astrbot.api.web import error_response, json_response, request

    _HAS_WEB_API = True
except (ImportError, AttributeError):  # 老版本回落到 quart
    _HAS_WEB_API = False
    try:
        from quart import jsonify as _quart_jsonify
        from quart import request  # type: ignore[assignment]
    except ImportError:  # 本地单测环境两个都没有
        request = None  # type: ignore[assignment]
        _quart_jsonify = None  # type: ignore[assignment]

    def json_response(  # type: ignore[misc]
        data: Any = None,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if _quart_jsonify is None:
            return {"status_code": status_code, "data": data}
        resp = _quart_jsonify(data)
        resp.status_code = status_code
        for key, value in (headers or {}).items():
            resp.headers[key] = value
        return resp

    def error_response(  # type: ignore[misc]
        message: str = "",
        *,
        status_code: int = 400,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return json_response(
            {"status": "error", "message": message, "data": data if data is not None else {}},
            status_code=status_code,
            headers=headers,
        )


from .rules import (
    FORMAT_LABELS,
    FORMAT_TYPES,
    MATCH_LABELS,
    MATCH_TYPES,
    MAX_KEYWORD_LEN,
    MAX_REPLY_LEN,
    PLACEHOLDERS,
    decorate_rule,
    find_matches,
    render_reply,
    stats as rule_stats,
)
from .store import SCOPE_GLOBAL, RuleStore

PLUGIN_NAME = "astrbot_plugin_keyword_reply"


class KeywordPageController:
    """把 RuleStore 的能力包成 HTTP 接口。"""

    def __init__(self, context: Any, store: RuleStore, plugin: Any = None) -> None:
        self.context = context
        self.store = store
        self.plugin = plugin

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register_routes(self) -> None:
        routes: list[tuple[str, Callable[..., Any], list[str], str]] = [
            ("/page/meta", self.get_meta, ["GET"], "关键字回复：枚举与字段上限"),
            ("/page/rules", self.get_rules, ["GET"], "关键字回复：规则列表"),
            ("/page/rules", self.create_rule, ["POST"], "关键字回复：新增规则"),
            ("/page/rules", self.update_rule, ["PUT"], "关键字回复：修改规则"),
            ("/page/rules", self.delete_rules, ["DELETE"], "关键字回复：删除规则"),
            ("/page/rules/toggle", self.toggle_rule, ["POST"], "关键字回复：启用/停用规则"),
            ("/page/rules/clear", self.clear_rules, ["POST"], "关键字回复：清空某个范围"),
            ("/page/test", self.test_match, ["POST"], "关键字回复：试一句话看命中哪条"),
            ("/page/export", self.export_rules, ["GET"], "关键字回复：导出规则"),
            ("/page/import", self.import_rules, ["POST"], "关键字回复：导入规则"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(f"/{PLUGIN_NAME}{path}", handler, methods, desc)
            except Exception as exc:  # 注册失败不能拖垮插件本体
                _log_warn(f"注册 Web API {path} 失败: {exc}")

    # ------------------------------------------------------------------
    # 请求 / 响应小工具
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(data: Any = None, message: str = "") -> Any:
        return json_response(
            {"status": "ok", "message": message, "data": {} if data is None else data}
        )

    @staticmethod
    def _err(message: str, status: int = 400) -> Any:
        return error_response(message, status_code=status, data={})

    @staticmethod
    def _query_get(key: str, default: str = "") -> str:
        if request is None:
            return default
        args = getattr(request, "query", None)
        if args is None:
            args = getattr(request, "args", None)
        if args is None:
            return default
        value = args.get(key, default)
        return default if value is None else str(value)

    @staticmethod
    async def _read_json() -> dict[str, Any]:
        """读 JSON body，两套 API 都兼容；body 不是对象就当空字典。"""
        if request is None:
            return {}
        payload: Any = None
        reader = getattr(request, "json", None)
        if callable(reader):
            try:
                payload = await reader(default=None)  # astrbot.api.web
            except TypeError:
                payload = await reader()  # quart: request.json 是 property/协程
            except Exception:
                payload = None
        if payload is None:
            getter = getattr(request, "get_json", None)
            if callable(getter):
                try:
                    payload = await getter(silent=True)
                except Exception:
                    payload = None
        if payload is None:
            body_reader = getattr(request, "body", None) or getattr(request, "get_data", None)
            if callable(body_reader):
                try:
                    raw = await body_reader()
                    payload = json.loads(raw) if raw else None
                except Exception:
                    payload = None
        return payload if isinstance(payload, dict) else {}

    async def _read_json_with_method(self) -> tuple[str, dict[str, Any]]:
        """取出 body 和实际方法。

        面板 bridge 只有 apiGet / apiPost，改删操作靠 body 里的 _method 透传，
        所以 PUT / DELETE 既能走原生方法，也能走 POST + _method。
        """
        payload = await self._read_json()
        override = str(payload.pop("_method", "") or "").strip().upper()
        native = str(getattr(request, "method", "") or "").upper()
        return (override or native or "POST"), payload

    def _scope(self, payload: dict[str, Any] | None = None) -> str:
        if payload and payload.get("scope"):
            return str(payload["scope"]).strip() or SCOPE_GLOBAL
        return self._query_get("scope", SCOPE_GLOBAL).strip() or SCOPE_GLOBAL

    # ------------------------------------------------------------------
    # handlers
    # ------------------------------------------------------------------

    async def get_meta(self) -> Any:
        return self._ok(
            {
                "match_types": [
                    {"value": name, "label": MATCH_LABELS[name]} for name in MATCH_TYPES
                ],
                "formats": [
                    {"value": name, "label": FORMAT_LABELS[name]} for name in FORMAT_TYPES
                ],
                "placeholders": list(PLACEHOLDERS),
                "limits": {
                    "keyword": MAX_KEYWORD_LEN,
                    "reply": MAX_REPLY_LEN,
                },
                "scope_global": SCOPE_GLOBAL,
            }
        )

    async def get_rules(self) -> Any:
        scope = self._scope()
        search = self._query_get("search").strip().lower()
        rules = self.store.list_rules(scope)
        if search:
            rules = [
                rule
                for rule in rules
                if search in str(rule.get("keyword", "")).lower()
                or search in str(rule.get("reply", "")).lower()
            ]
        return self._ok(
            {
                "scope": scope,
                "scopes": self._scope_options(),
                "rules": [decorate_rule(rule) for rule in rules],
                "stats": rule_stats(self.store.list_rules(scope)),
                "enabled": self._plugin_enabled(),
            }
        )

    def _scope_options(self) -> list[dict[str, Any]]:
        """范围下拉：全局 + 已有规则的会话 + 插件见过的活跃会话。"""
        known = {
            name: len(self.store.list_rules(name)) for name in self.store.scope_names()
        }
        seen: dict[str, str] = {}
        if self.plugin is not None:
            seen = dict(getattr(self.plugin, "seen_sessions", {}) or {})
        for umo in seen:
            known.setdefault(umo, 0)
        out: list[dict[str, Any]] = []
        for name, count in known.items():
            label = "全局（所有会话）" if name == SCOPE_GLOBAL else (seen.get(name) or name)
            out.append({"value": name, "label": label, "count": count})
        out.sort(key=lambda item: (item["value"] != SCOPE_GLOBAL, item["label"]))
        return out

    def _plugin_enabled(self) -> bool:
        if self.plugin is None:
            return True
        return bool(getattr(self.plugin, "auto_reply_enabled", True))

    async def create_rule(self) -> Any:
        method, payload = await self._read_json_with_method()
        if method == "PUT":
            return await self.update_rule(payload)
        if method == "DELETE":
            return await self.delete_rules(payload)
        scope = self._scope(payload)
        overwrite = bool(payload.pop("overwrite", False))
        payload.pop("scope", None)
        try:
            rule = await self.store.add(payload, scope, overwrite=overwrite)
        except ValueError as exc:
            return self._err(str(exc))
        return self._ok({"rule": decorate_rule(rule)}, "已保存")

    async def update_rule(self, payload: dict[str, Any] | None = None) -> Any:
        if payload is None:
            _, payload = await self._read_json_with_method()
        scope = self._scope(payload)
        rule_id = str(payload.pop("id", "") or "").strip()
        payload.pop("scope", None)
        if not rule_id:
            return self._err("缺少规则 id")
        try:
            rule = await self.store.update(rule_id, payload, scope)
        except LookupError as exc:
            return self._err(str(exc), status=404)
        except ValueError as exc:
            return self._err(str(exc))
        return self._ok({"rule": decorate_rule(rule)}, "已更新")

    async def delete_rules(self, payload: dict[str, Any] | None = None) -> Any:
        if payload is None:
            _, payload = await self._read_json_with_method()
        scope = self._scope(payload)
        ids = payload.get("ids")
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list):
            single = str(payload.get("id", "") or "").strip()
            ids = [single] if single else []
        try:
            result = await self.store.delete([str(i) for i in ids], scope)
        except ValueError as exc:
            return self._err(str(exc))
        if not result["deleted"]:
            return self._err("没有删掉任何规则（id 不存在？）", status=404)
        return self._ok(result, f"已删除 {len(result['deleted'])} 条")

    async def toggle_rule(self) -> Any:
        _, payload = await self._read_json_with_method()
        scope = self._scope(payload)
        rule_id = str(payload.get("id", "") or "").strip()
        if not rule_id:
            return self._err("缺少规则 id")
        enabled = payload.get("enabled")
        try:
            rule = await self.store.toggle(
                rule_id, scope, None if enabled is None else bool(enabled)
            )
        except LookupError as exc:
            return self._err(str(exc), status=404)
        return self._ok(
            {"rule": decorate_rule(rule)},
            "已启用" if rule.get("enabled") else "已停用",
        )

    async def clear_rules(self) -> Any:
        _, payload = await self._read_json_with_method()
        scope = self._scope(payload)
        if str(payload.get("confirm", "")).strip().lower() not in {"1", "true", "yes", "confirm"}:
            return self._err("清空需要带 confirm 参数")
        count = await self.store.clear_scope(scope)
        return self._ok({"scope": scope, "cleared": count}, f"已清空 {count} 条")

    async def test_match(self) -> Any:
        """面板上「试一句话」：返回这句话在某范围下会命中哪些规则、实际会回什么。"""
        _, payload = await self._read_json_with_method()
        scope = self._scope(payload)
        text = str(payload.get("text", "") or "").strip()
        if not text:
            return self._err("请输入要测试的内容")
        ignore_case = bool(payload.get("ignore_case", self._ignore_case_default()))
        effective = (
            self.store.effective_rules(scope) if scope != SCOPE_GLOBAL
            else self.store.list_rules(SCOPE_GLOBAL)
        )
        hits = find_matches(effective, text, ignore_case=ignore_case, include_disabled=True)
        preview_values = {
            "sender": "张三",
            "group": "测试群",
            "keyword": text,
            "date": "2026-01-01",
            "time": "12:00:00",
            "datetime": "2026-01-01 12:00:00",
        }
        items = []
        for idx, rule in enumerate(hits):
            decorated = decorate_rule(rule)
            decorated["would_reply"] = idx == 0 and rule.get("enabled", True)
            decorated["rendered"] = render_reply(
                rule.get("reply", ""), {**preview_values, "keyword": rule.get("keyword", text)}
            )
            items.append(decorated)
        # 停用的规则排在前面会让「实际会回这条」看起来不对，这里明确标出第一条有效规则
        first_enabled = next(
            (i for i, rule in enumerate(hits) if rule.get("enabled", True)), None
        )
        if first_enabled is not None:
            for idx, item in enumerate(items):
                item["would_reply"] = idx == first_enabled
        return self._ok({"text": text, "scope": scope, "matches": items})

    def _ignore_case_default(self) -> bool:
        if self.plugin is None:
            return False
        return bool(getattr(self.plugin, "ignore_case", False))

    async def export_rules(self) -> Any:
        scope = self._scope()
        return self._ok(self.store.export_rules(scope))

    async def import_rules(self) -> Any:
        _, payload = await self._read_json_with_method()
        scope = self._scope(payload)
        raw = payload.get("rules")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                return self._err("导入内容不是合法 JSON")
            raw = parsed.get("rules") if isinstance(parsed, dict) else parsed
        if not isinstance(raw, list):
            return self._err("导入内容需要是规则数组，或含 rules 数组的对象")
        result = await self.store.import_rules(
            raw, scope, replace=bool(payload.get("replace", False))
        )
        message = f"导入完成：新增 {result['added']} 条，覆盖 {result['updated']} 条"
        if result["failed"]:
            message += f"，{len(result['failed'])} 条被跳过"
        return self._ok(result, message)


def _log_warn(message: str) -> None:
    try:
        from astrbot.api import logger

        logger.warning(f"[keyword_reply] {message}")
    except Exception:
        pass
