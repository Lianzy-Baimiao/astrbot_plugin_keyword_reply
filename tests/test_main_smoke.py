# -*- coding: utf-8 -*-
"""main.py / page.py 冒烟测试：桩掉 astrbot 把插件类拉起来跑。

    python tests/test_main_smoke.py
全绿打印 OK。

本机没装 astrbot 本体，所以先把 `astrbot.*` 塞进 sys.modules 再 import main ——
main.py 的语法错误、名字写错、装饰器用法错、handler 纯逻辑回归都能在本地拦下来。
覆盖：import 健全性 / 自动回复命中与不命中 / 会话规则盖全局 / 黑白名单 /
冷却 / MD 渲染失败回落纯文本 / 自己的命令不参与匹配 / 聊天命令增删查改 /
Web 接口的增删改查与 _method 透传 / 面板 test 接口。
"""
import asyncio
import json
import os
import sys
import tempfile
import types

# ---------------------------------------------------------------------------
# astrbot 桩：必须在 import main 之前塞好
# ---------------------------------------------------------------------------

_DATA_DIR = tempfile.mkdtemp(prefix="kwreply-smoke-")


class _Logger:
    def info(self, *a, **k):
        pass

    warning = error = debug = info


class _Star:
    def __init__(self, context=None):
        self.context = context


class _StarTools:
    @staticmethod
    def get_data_dir(name=""):
        return _DATA_DIR


class _EventMessageType:
    ALL = "all"


def _passthrough_decorator(*d_args, **d_kwargs):
    """filter.command / filter.event_message_type 的桩：原样返回被装饰函数。"""

    def deco(func):
        return func

    return deco


_filter = types.SimpleNamespace(
    command=_passthrough_decorator,
    event_message_type=_passthrough_decorator,
    EventMessageType=_EventMessageType,
)


def _install_astrbot_stub():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = _filter
    event_mod.AstrMessageEvent = object

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = _Star
    star_mod.StarTools = _StarTools

    # astrbot.api.web 故意不提供 —— 逼 page.py 走「老版本回落」那条分支，
    # 顺带证明没有 quart 时也 import 得动。
    api.star = star_mod
    api.event = event_mod
    astrbot.api = api

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stub()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as plugin_main  # noqa: E402
from keyword_reply import page as page_mod  # noqa: E402
from keyword_reply.rules import FORMAT_MARKDOWN  # noqa: E402
from keyword_reply.store import SCOPE_GLOBAL  # noqa: E402


# ---------------------------------------------------------------------------
# 假事件 / 假 context
# ---------------------------------------------------------------------------


class FakeEvent:
    def __init__(
        self,
        text,
        umo="napcat:GroupMessage:1001",
        group_id="1001",
        sender_id="777",
        sender_name="张三",
        admin=True,
    ):
        self.message_str = text
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._admin = admin
        self.stopped = False

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return "10000"

    def is_admin(self):
        return self._admin

    def stop_event(self):
        self.stopped = True

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, url):
        return ("image", url)


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, tuple(methods), desc))


def make_plugin(config=None, fresh=True):
    """建一个插件实例，数据目录每次换新的，避免用例互相污染。"""
    data_dir = tempfile.mkdtemp(prefix="kwreply-case-")
    _StarTools.get_data_dir = staticmethod(lambda name="": data_dir)
    ctx = FakeContext()
    plugin = plugin_main.KeywordReplyPlugin(ctx, config if config is not None else {})
    if fresh:
        # 去掉 __init__ 塞的示例规则，用例自己造数据
        plugin.store._scopes = {}
        plugin.store.save()
    return plugin, ctx


def run(coro):
    return asyncio.run(coro)


async def collect(agen):
    """把 handler 的 async generator 收成列表。"""
    out = []
    async for item in agen:
        out.append(item)
    return out


def texts(results):
    return [payload for kind, payload in results if kind == "plain"]


# ---------------------------------------------------------------------------
# import 健全性
# ---------------------------------------------------------------------------


def test_import_and_routes():
    plugin, ctx = make_plugin()
    assert plugin_main.PLUGIN_NAME == "astrbot_plugin_keyword_reply"
    paths = [path for path, *_ in ctx.routes]
    # 面板需要的接口一个都不能少
    for suffix in (
        "/page/meta",
        "/page/rules",
        "/page/rules/toggle",
        "/page/rules/clear",
        "/page/test",
        "/page/export",
        "/page/import",
    ):
        assert f"/astrbot_plugin_keyword_reply{suffix}" in paths, suffix
    # 所有路由都挂在本插件名下，不会踩到别的插件
    assert all(p.startswith("/astrbot_plugin_keyword_reply/") for p in paths)


def test_page_module_without_astrbot_web():
    # 桩里没给 astrbot.api.web，page.py 必须走回落分支且仍可用
    assert page_mod._HAS_WEB_API is False
    resp = page_mod.KeywordPageController._ok({"a": 1}, "hi")
    assert resp["data"]["status"] == "ok"
    assert resp["data"]["data"] == {"a": 1}
    err = page_mod.KeywordPageController._err("boom", 404)
    assert err["status_code"] == 404


def test_default_rules_seeded_on_first_start():
    data_dir = tempfile.mkdtemp(prefix="kwreply-seed-")
    _StarTools.get_data_dir = staticmethod(lambda name="": data_dir)
    plugin = plugin_main.KeywordReplyPlugin(FakeContext(), {})
    seeded = plugin.store.list_rules(SCOPE_GLOBAL)
    assert len(seeded) == 2
    assert any(r["keyword"] == "活动地址" and r["format"] == FORMAT_MARKDOWN for r in seeded)
    # 再拉一次同目录不该重复塞
    plugin2 = plugin_main.KeywordReplyPlugin(FakeContext(), {})
    assert len(plugin2.store.list_rules(SCOPE_GLOBAL)) == 2


# ---------------------------------------------------------------------------
# 自动回复
# ---------------------------------------------------------------------------


def test_auto_reply_hit_and_miss():
    plugin, _ = make_plugin()
    run(
        plugin.store.add(
            {"keyword": "活动地址", "match": "exact", "reply": "一个活动地址", "format": "text"},
            SCOPE_GLOBAL,
        )
    )
    ev = FakeEvent("活动地址")
    assert texts(run(collect(plugin.on_message(ev)))) == ["一个活动地址"]
    assert ev.stopped is True  # 默认阻断后续插件

    ev2 = FakeEvent("今天天气不错")
    assert run(collect(plugin.on_message(ev2))) == []
    assert ev2.stopped is False


def test_placeholders_rendered():
    plugin, _ = make_plugin()
    run(
        plugin.store.add(
            {"keyword": "在吗", "match": "contains", "reply": "在的，{sender}（{keyword}）", "format": "text"},
            SCOPE_GLOBAL,
        )
    )
    out = texts(run(collect(plugin.on_message(FakeEvent("你在吗？")))))
    assert out == ["在的，张三（在吗）"]


def test_session_rule_beats_global():
    plugin, _ = make_plugin()
    umo = "napcat:GroupMessage:1001"
    run(plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "全局的", "format": "text"}, SCOPE_GLOBAL))
    run(plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "本群的", "format": "text"}, umo))
    assert texts(run(collect(plugin.on_message(FakeEvent("活动地址", umo=umo))))) == ["本群的"]
    # 别的群仍吃全局那条
    other = "napcat:GroupMessage:2002"
    assert texts(run(collect(plugin.on_message(FakeEvent("活动地址", umo=other, group_id="2002"))))) == ["全局的"]


def test_exact_wins_over_contains():
    plugin, _ = make_plugin()
    run(plugin.store.add({"keyword": "活动", "match": "contains", "reply": "模糊的", "format": "text"}, SCOPE_GLOBAL))
    run(plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "精准的", "format": "text"}, SCOPE_GLOBAL))
    assert texts(run(collect(plugin.on_message(FakeEvent("活动地址"))))) == ["精准的"]


def test_disabled_rule_not_replied():
    plugin, _ = make_plugin()
    rule = run(
        plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "x", "format": "text"}, SCOPE_GLOBAL)
    )
    run(plugin.store.toggle(rule["id"], SCOPE_GLOBAL, False))
    assert run(collect(plugin.on_message(FakeEvent("活动地址")))) == []


def test_global_switch_off():
    plugin, _ = make_plugin(config={"enabled": False})
    run(plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "x", "format": "text"}, SCOPE_GLOBAL))
    assert run(collect(plugin.on_message(FakeEvent("活动地址")))) == []


def test_whitelist_blacklist():
    plugin, _ = make_plugin(config={"whitelist": ["1001"]})
    run(plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "x", "format": "text"}, SCOPE_GLOBAL))
    # 名单内
    assert texts(run(collect(plugin.on_message(FakeEvent("活动地址", group_id="1001"))))) == ["x"]
    # 名单外
    ev = FakeEvent("活动地址", umo="napcat:GroupMessage:9999", group_id="9999")
    assert run(collect(plugin.on_message(ev))) == []

    plugin2, _ = make_plugin(config={"blacklist": ["napcat:GroupMessage:1001"]})
    run(plugin2.store.add({"keyword": "活动地址", "match": "exact", "reply": "x", "format": "text"}, SCOPE_GLOBAL))
    assert run(collect(plugin2.on_message(FakeEvent("活动地址")))) == []


def test_private_chat_toggle():
    plugin, _ = make_plugin(config={"reply_in_private": False})
    run(plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "x", "format": "text"}, SCOPE_GLOBAL))
    ev = FakeEvent("活动地址", umo="napcat:FriendMessage:777", group_id="")
    assert run(collect(plugin.on_message(ev))) == []


def test_cooldown():
    plugin, _ = make_plugin(config={"cooldown": 60})
    run(plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "x", "format": "text"}, SCOPE_GLOBAL))
    assert texts(run(collect(plugin.on_message(FakeEvent("活动地址"))))) == ["x"]
    # 同会话紧接着再发不回
    assert run(collect(plugin.on_message(FakeEvent("活动地址")))) == []
    # 换个会话不受影响
    other = FakeEvent("活动地址", umo="napcat:GroupMessage:2002", group_id="2002")
    assert texts(run(collect(plugin.on_message(other)))) == ["x"]


def test_stop_event_can_be_disabled():
    plugin, _ = make_plugin(config={"stop_event": False})
    run(plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "x", "format": "text"}, SCOPE_GLOBAL))
    ev = FakeEvent("活动地址")
    run(collect(plugin.on_message(ev)))
    assert ev.stopped is False


def test_own_command_not_matched():
    """/关键字 开头的消息不能被规则吃掉，否则管理命令会被自己拦住。"""
    plugin, _ = make_plugin()
    run(plugin.store.add({"keyword": "关键字", "match": "contains", "reply": "不该出现", "format": "text"}, SCOPE_GLOBAL))
    assert run(collect(plugin.on_message(FakeEvent("/关键字 列表")))) == []
    assert run(collect(plugin.on_message(FakeEvent("关键字 列表")))) == []


def test_hits_counted():
    plugin, _ = make_plugin()
    rule = run(
        plugin.store.add({"keyword": "活动地址", "match": "exact", "reply": "x", "format": "text"}, SCOPE_GLOBAL)
    )
    run(collect(plugin.on_message(FakeEvent("活动地址"))))
    run(collect(plugin.on_message(FakeEvent("活动地址", umo="napcat:GroupMessage:2002", group_id="2002"))))
    assert plugin.store.get(rule["id"], SCOPE_GLOBAL)["hits"] == 2


# ---------------------------------------------------------------------------
# MD 格式
# ---------------------------------------------------------------------------


def test_markdown_image_mode_and_fallback():
    plugin, _ = make_plugin(config={"markdown_mode": "image"})
    run(
        plugin.store.add(
            {"keyword": "活动地址", "match": "exact", "reply": "## 标题\n- 一行", "format": "markdown"},
            SCOPE_GLOBAL,
        )
    )
    # 渲染成功 → 发图
    plugin.text_to_image = lambda md: _as_coro("http://img/a.png")
    assert run(collect(plugin.on_message(FakeEvent("活动地址")))) == [("image", "http://img/a.png")]

    # 渲染抛异常 → 回落纯文本，不能把消息吞掉
    def boom(md):
        raise RuntimeError("playwright 没装")

    plugin.text_to_image = boom
    plugin._last_reply.clear()
    out = run(collect(plugin.on_message(FakeEvent("活动地址"))))
    assert out == [("plain", "## 标题\n- 一行")]


def test_markdown_text_mode():
    plugin, _ = make_plugin(config={"markdown_mode": "text"})
    run(
        plugin.store.add(
            {"keyword": "活动地址", "match": "exact", "reply": "## 标题", "format": "markdown"},
            SCOPE_GLOBAL,
        )
    )
    plugin.text_to_image = lambda md: _as_coro("http://img/a.png")
    # text 模式下不该去渲染图片
    assert run(collect(plugin.on_message(FakeEvent("活动地址")))) == [("plain", "## 标题")]


def _as_coro(value):
    async def _inner():
        return value

    return _inner()


# ---------------------------------------------------------------------------
# 聊天命令
# ---------------------------------------------------------------------------


def test_cmd_help():
    plugin, _ = make_plugin()
    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字")))))
    assert len(out) == 1 and "关键字|匹配类型|回复|格式" in out[0]


def test_cmd_add_list_delete():
    plugin, _ = make_plugin()
    ev = FakeEvent("/关键字 添加 活动地址|精准匹配|一个活动地址|MD格式")
    out = texts(run(collect(plugin.cmd_keyword(ev))))[0]
    assert "已加到本会话" in out and "精准匹配" in out and "MD格式" in out

    saved = plugin.store.list_rules("napcat:GroupMessage:1001")
    assert len(saved) == 1
    assert saved[0]["keyword"] == "活动地址"
    assert saved[0]["match"] == "exact"
    assert saved[0]["format"] == "markdown"
    assert saved[0]["reply"] == "一个活动地址"

    listed = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 列表")))))[0]
    assert "活动地址" in listed

    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 删除 活动地址")))))[0]
    assert "已删除 1 条" in out
    assert plugin.store.list_rules("napcat:GroupMessage:1001") == []


def test_cmd_global_prefix():
    plugin, _ = make_plugin()
    run(collect(plugin.cmd_keyword(FakeEvent("/关键字 全局添加 活动地址|精准匹配|全局回复|文本"))))
    assert len(plugin.store.list_rules(SCOPE_GLOBAL)) == 1
    assert plugin.store.list_rules("napcat:GroupMessage:1001") == []
    # 「全局 添加」带空格也要认
    run(collect(plugin.cmd_keyword(FakeEvent("/关键字 全局 添加 在吗|模糊匹配|在的|文本"))))
    assert len(plugin.store.list_rules(SCOPE_GLOBAL)) == 2


def test_cmd_add_defaults_and_bad_input():
    plugin, _ = make_plugin()
    # 省略后两段 → 精准 + 文本
    run(collect(plugin.cmd_keyword(FakeEvent("/关键字 添加 你好|精准匹配|哈喽"))))
    rule = plugin.store.list_rules("napcat:GroupMessage:1001")[0]
    assert rule["match"] == "exact" and rule["format"] == "text"

    # 缺回复段 → 友好报错，不抛异常
    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 添加 只有关键字")))))[0]
    assert "没能完成" in out or "少了" in out

    # 正则写错 → 报错
    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 添加 [不闭合|正则匹配|x|文本")))))[0]
    assert "没能完成" in out


def test_cmd_toggle_and_test():
    plugin, _ = make_plugin()
    run(collect(plugin.cmd_keyword(FakeEvent("/关键字 添加 活动地址|精准匹配|一个活动地址|文本"))))
    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 开关 活动地址")))))[0]
    assert "已停用" in out
    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 开关 活动地址")))))[0]
    assert "已启用" in out

    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 测试 活动地址")))))[0]
    assert "命中 1 条" in out
    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 测试 无关内容")))))[0]
    assert "没有命中" in out


def test_cmd_requires_admin_for_writes():
    plugin, _ = make_plugin()
    ev = FakeEvent("/关键字 添加 活动地址|精准匹配|x|文本", admin=False)
    out = texts(run(collect(plugin.cmd_keyword(ev))))[0]
    assert "只有管理员" in out
    assert plugin.store.list_rules("napcat:GroupMessage:1001") == []
    # 只读子命令不要求管理员
    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 列表", admin=False)))))[0]
    assert "规则" in out


def test_cmd_unknown_action():
    plugin, _ = make_plugin()
    out = texts(run(collect(plugin.cmd_keyword(FakeEvent("/关键字 瞎写")))))[0]
    assert "不认识的子命令" in out


# ---------------------------------------------------------------------------
# Web 接口：用假 request 驱动 page.py
# ---------------------------------------------------------------------------


class FakeQuery:
    def __init__(self, data):
        self._data = {k: str(v) for k, v in (data or {}).items()}

    def get(self, key, default=""):
        return self._data.get(key, default)


class FakeRequest:
    """够用的 request 桩：page.py 只用 query / method / json()。"""

    def __init__(self, query=None, body=None, method="GET"):
        self.query = FakeQuery(query)
        self.args = self.query
        self.method = method
        self._body = body

    async def json(self, default=None):
        return self._body if self._body is not None else default


def with_request(req):
    page_mod.request = req


def unwrap(resp):
    """把桩化的 json_response 拆成 (ok?, data, message)。"""
    payload = resp["data"] if "data" in resp and isinstance(resp.get("data"), dict) else resp
    if "status" in payload:
        return payload["status"] == "ok", payload.get("data") or {}, payload.get("message", "")
    return False, {}, payload.get("message", "")


def test_page_meta():
    plugin, _ = make_plugin()
    with_request(FakeRequest())
    ok, data, _ = unwrap(run(plugin.page.get_meta()))
    assert ok
    labels = {item["value"]: item["label"] for item in data["match_types"]}
    assert labels["exact"] == "精准匹配"
    assert labels["contains"] == "模糊匹配"
    fmts = {item["value"]: item["label"] for item in data["formats"]}
    assert fmts["markdown"] == "MD格式" and fmts["text"] == "文本"
    assert "sender" in data["placeholders"]


def test_page_crud_roundtrip():
    plugin, _ = make_plugin()

    # 新增
    with_request(
        FakeRequest(
            body={
                "scope": SCOPE_GLOBAL,
                "keyword": "活动地址",
                "match": "精准匹配",
                "reply": "一个活动地址",
                "format": "MD格式",
            },
            method="POST",
        )
    )
    ok, data, msg = unwrap(run(plugin.page.create_rule()))
    assert ok and msg == "已保存"
    rule = data["rule"]
    assert rule["match"] == "exact" and rule["format"] == "markdown"
    assert rule["match_label"] == "精准匹配" and rule["format_label"] == "MD格式"
    rid = rule["id"]

    # 列表
    with_request(FakeRequest(query={"scope": SCOPE_GLOBAL}))
    ok, data, _ = unwrap(run(plugin.page.get_rules()))
    assert ok and len(data["rules"]) == 1
    assert data["stats"]["total"] == 1 and data["stats"]["enabled"] == 1
    assert any(opt["value"] == SCOPE_GLOBAL for opt in data["scopes"])

    # 重复新增被拦
    with_request(
        FakeRequest(
            body={"scope": SCOPE_GLOBAL, "keyword": "活动地址", "match": "exact", "reply": "又一个", "format": "text"},
            method="POST",
        )
    )
    ok, _, msg = unwrap(run(plugin.page.create_rule()))
    assert not ok and "已存在" in msg

    # 带 overwrite 就允许
    with_request(
        FakeRequest(
            body={
                "scope": SCOPE_GLOBAL,
                "keyword": "活动地址",
                "match": "exact",
                "reply": "覆盖后的",
                "format": "text",
                "overwrite": True,
            },
            method="POST",
        )
    )
    ok, data, _ = unwrap(run(plugin.page.create_rule()))
    assert ok and data["rule"]["reply"] == "覆盖后的"
    assert data["rule"]["id"] == rid  # 覆盖保留原 id

    # 修改（POST + _method=PUT，走面板 bridge 的路径）
    with_request(
        FakeRequest(
            body={"_method": "PUT", "scope": SCOPE_GLOBAL, "id": rid, "reply": "改过的"},
            method="POST",
        )
    )
    ok, data, msg = unwrap(run(plugin.page.create_rule()))
    assert ok and msg == "已更新" and data["rule"]["reply"] == "改过的"
    # 没传的字段沿用旧值
    assert data["rule"]["keyword"] == "活动地址"

    # 开关
    with_request(FakeRequest(body={"scope": SCOPE_GLOBAL, "id": rid}, method="POST"))
    ok, data, msg = unwrap(run(plugin.page.toggle_rule()))
    assert ok and msg == "已停用" and data["rule"]["enabled"] is False

    # 删除（POST + _method=DELETE）
    with_request(
        FakeRequest(body={"_method": "DELETE", "scope": SCOPE_GLOBAL, "ids": [rid]}, method="POST")
    )
    ok, data, _ = unwrap(run(plugin.page.create_rule()))
    assert ok and data["deleted"] == [rid]
    assert plugin.store.list_rules(SCOPE_GLOBAL) == []


def test_page_validation_errors():
    plugin, _ = make_plugin()
    # 关键字为空
    with_request(FakeRequest(body={"keyword": "", "reply": "x"}, method="POST"))
    ok, _, msg = unwrap(run(plugin.page.create_rule()))
    assert not ok and "关键字不能为空" in msg

    # 回复为空
    with_request(FakeRequest(body={"keyword": "a", "reply": "   "}, method="POST"))
    ok, _, msg = unwrap(run(plugin.page.create_rule()))
    assert not ok and "回复内容不能为空" in msg

    # 正则写错
    with_request(
        FakeRequest(body={"keyword": "[不闭合", "match": "正则匹配", "reply": "x"}, method="POST")
    )
    ok, _, msg = unwrap(run(plugin.page.create_rule()))
    assert not ok and "正则" in msg

    # 改不存在的 id
    with_request(FakeRequest(body={"id": "nope", "reply": "x"}, method="POST"))
    ok, _, msg = unwrap(run(plugin.page.update_rule()))
    assert not ok and "找不到规则" in msg


def test_page_test_endpoint():
    plugin, _ = make_plugin()
    run(plugin.store.add({"keyword": "活动", "match": "contains", "reply": "模糊的", "format": "text"}, SCOPE_GLOBAL))
    run(
        plugin.store.add(
            {"keyword": "活动地址", "match": "exact", "reply": "你好 {sender}", "format": "text"}, SCOPE_GLOBAL
        )
    )
    with_request(FakeRequest(body={"scope": SCOPE_GLOBAL, "text": "活动地址"}, method="POST"))
    ok, data, _ = unwrap(run(plugin.page.test_match()))
    assert ok and len(data["matches"]) == 2
    # 精准那条排第一且标为实际会回
    assert data["matches"][0]["keyword"] == "活动地址"
    assert data["matches"][0]["would_reply"] is True
    assert data["matches"][1]["would_reply"] is False
    # 占位符在预览里被替换成示例名
    assert data["matches"][0]["rendered"] == "你好 张三"

    # 空文本报错
    with_request(FakeRequest(body={"scope": SCOPE_GLOBAL, "text": "  "}, method="POST"))
    ok, _, msg = unwrap(run(plugin.page.test_match()))
    assert not ok and "请输入" in msg


def test_page_export_import_clear():
    plugin, _ = make_plugin()
    run(plugin.store.add({"keyword": "a", "match": "exact", "reply": "1", "format": "text"}, SCOPE_GLOBAL))
    run(plugin.store.add({"keyword": "b", "match": "contains", "reply": "2", "format": "markdown"}, SCOPE_GLOBAL))

    with_request(FakeRequest(query={"scope": SCOPE_GLOBAL}))
    ok, data, _ = unwrap(run(plugin.page.export_rules()))
    assert ok and len(data["rules"]) == 2
    dumped = json.dumps(data)

    # 导入到另一个 scope（字符串形式的整个导出文件也要吃得下）
    with_request(FakeRequest(body={"scope": "napcat:GroupMessage:1001", "rules": dumped}, method="POST"))
    ok, result, msg = unwrap(run(plugin.page.import_rules()))
    assert ok and result["added"] == 2 and "新增 2 条" in msg

    # 清空要 confirm
    with_request(FakeRequest(body={"scope": SCOPE_GLOBAL}, method="POST"))
    ok, _, msg = unwrap(run(plugin.page.clear_rules()))
    assert not ok and "confirm" in msg

    with_request(FakeRequest(body={"scope": SCOPE_GLOBAL, "confirm": "1"}, method="POST"))
    ok, data, _ = unwrap(run(plugin.page.clear_rules()))
    assert ok and data["cleared"] == 2
    assert plugin.store.list_rules(SCOPE_GLOBAL) == []
    # 另一个 scope 不受影响
    assert len(plugin.store.list_rules("napcat:GroupMessage:1001")) == 2


def test_page_scope_options_include_seen_sessions():
    plugin, _ = make_plugin()
    run(collect(plugin.on_message(FakeEvent("随便一句", umo="napcat:GroupMessage:1001", group_id="1001"))))
    with_request(FakeRequest(query={"scope": SCOPE_GLOBAL}))
    ok, data, _ = unwrap(run(plugin.page.get_rules()))
    assert ok
    values = [opt["value"] for opt in data["scopes"]]
    assert SCOPE_GLOBAL in values
    assert "napcat:GroupMessage:1001" in values
    assert values[0] == SCOPE_GLOBAL  # 全局排最前


def test_terminate_flushes():
    plugin, _ = make_plugin()
    run(plugin.store.add({"keyword": "a", "match": "exact", "reply": "1", "format": "text"}, SCOPE_GLOBAL))
    plugin.store.bump_hits(plugin.store.list_rules(SCOPE_GLOBAL)[0]["id"], SCOPE_GLOBAL)
    run(plugin.terminate())
    # flush 后重新读盘，命中数应已落盘
    from keyword_reply.store import RuleStore

    reloaded = RuleStore(plugin.store.path)
    reloaded.load()
    assert reloaded.list_rules(SCOPE_GLOBAL)[0]["hits"] == 1


def main():
    tests = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    for name, fn in tests:
        fn()
        print(f"  {name} ok")
    print(f"OK ({len(tests)} tests)")


if __name__ == "__main__":
    main()
