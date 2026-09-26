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
        group_name=None,
        bot=None,
    ):
        self.message_str = text
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._admin = admin
        self.stopped = False
        # AstrBot 的群名挂在 message_obj.group.group_name 上（有的平台事件带，OneBot 不带）
        self.message_obj = types.SimpleNamespace(
            group=types.SimpleNamespace(
                group_id=group_id, group_name=group_name
            )
        )
        if bot is not None:
            self.bot = bot

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


class FakeClient:
    """平台客户端桩：只实现 call_action，够 main.py 取群列表 / 群信息用。"""

    def __init__(self, group_list=None, group_info=None, fail=False):
        self._group_list = group_list if group_list is not None else []
        self._group_info = group_info or {}
        self._fail = fail
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if self._fail:
            raise RuntimeError("平台接口炸了")
        if action == "get_group_list":
            return self._group_list
        if action == "get_group_info":
            return self._group_info
        raise AssertionError(f"没桩的接口: {action}")


class FakePlatformInst:
    def __init__(self, platform_id, client):
        self._platform_id = platform_id
        self._client = client

    def meta(self):
        return types.SimpleNamespace(id=self._platform_id, name="aiocqhttp")

    def get_client(self):
        return self._client


class FakeContext:
    def __init__(self, platform_insts=None):
        self.routes = []
        self.platform_manager = types.SimpleNamespace(
            get_insts=lambda: list(platform_insts or [])
        )

    def register_web_api(self, path, handler, methods, desc):
        self.routes.append((path, handler, tuple(methods), desc))


def make_plugin(config=None, fresh=True, context=None):
    """建一个插件实例，数据目录每次换新的，避免用例互相污染。"""
    data_dir = tempfile.mkdtemp(prefix="kwreply-case-")
    _StarTools.get_data_dir = staticmethod(lambda name="": data_dir)
    ctx = context if context is not None else FakeContext()
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
        "/page/groups",
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


# ---------------------------------------------------------------------------
# 群名（面板范围下拉显示「群名（群号）」）
# ---------------------------------------------------------------------------


def test_scope_label_uses_group_name_from_event():
    plugin, _ = make_plugin()
    ev = FakeEvent(
        "你好",
        umo="napcat:GroupMessage:1001",
        group_id="1001",
        group_name="魔兽世界交流群",
    )
    run(collect(plugin.on_message(ev)))
    assert plugin.groups.name_of("napcat:GroupMessage:1001") == "魔兽世界交流群"

    with_request(FakeRequest(query={"scope": SCOPE_GLOBAL}))
    ok, data, _ = unwrap(run(plugin.page.get_rules()))
    assert ok
    labels = {opt["value"]: opt["label"] for opt in data["scopes"]}
    assert labels["napcat:GroupMessage:1001"] == "魔兽世界交流群（1001）"

    # 事件不带群名时退回「群 群号」，不会留个空 label
    plugin2, _ = make_plugin()
    run(collect(plugin2.on_message(FakeEvent("你好", umo="napcat:GroupMessage:2002", group_id="2002"))))
    with_request(FakeRequest(query={"scope": SCOPE_GLOBAL}))
    ok, data, _ = unwrap(run(plugin2.page.get_rules()))
    labels = {opt["value"]: opt["label"] for opt in data["scopes"]}
    assert labels["napcat:GroupMessage:2002"] == "群 2002"


def test_learn_group_name_via_platform_api():
    plugin, _ = make_plugin()
    client = FakeClient(group_info={"group_id": 1001, "group_name": "活动通知群", "member_count": 300})
    run(plugin._learn_group_name(client, "napcat:GroupMessage:1001", "1001", "napcat"))
    assert plugin.groups.name_of("napcat:GroupMessage:1001") == "活动通知群"
    assert client.calls[0][0] == "get_group_info"
    assert client.calls[0][1]["group_id"] == 1001  # 数字群号（OneBot 要 int）

    # 平台查不到名字：静默，不写空名
    err = FakeClient(fail=True)
    run(plugin._learn_group_name(err, "napcat:GroupMessage:1002", "1002", "napcat"))
    assert plugin.groups.name_of("napcat:GroupMessage:1002") == ""
    empty = FakeClient(group_info={})
    run(plugin._learn_group_name(empty, "napcat:GroupMessage:1003", "1003", "napcat"))
    assert plugin.groups.name_of("napcat:GroupMessage:1003") == ""


def test_page_groups_endpoint_refreshes_from_platform():
    client = FakeClient(
        group_list=[
            {"group_id": 1001, "group_name": "魔兽世界交流群", "member_count": 486},
            {"group_id": 2002, "group_name": "活动通知群", "member_count": 120},
        ]
    )
    ctx = FakeContext(platform_insts=[FakePlatformInst("napcat", client)])
    plugin, _ = make_plugin(context=ctx)
    run(plugin.store.add({"keyword": "a", "match": "exact", "reply": "1"}, "napcat:GroupMessage:1001"))

    with_request(FakeRequest(query={"refresh": "1"}))
    ok, data, _ = unwrap(run(plugin.page.get_groups()))
    assert ok
    assert data["refreshed"] == 2  # 两个群都是新名字
    assert data["named"] == 2 and data["total"] == 2
    by_value = {item["value"]: item for item in data["groups"]}
    assert by_value["napcat:GroupMessage:1001"]["group_name"] == "魔兽世界交流群"
    assert by_value["napcat:GroupMessage:1001"]["count"] == 1  # 有规则
    assert by_value["napcat:GroupMessage:1001"]["source"] == "rule"
    assert by_value["napcat:GroupMessage:2002"]["member_count"] == 120
    assert by_value["napcat:GroupMessage:2002"]["source"] == "platform"
    # 有名字的排前面（按名字排序）
    assert data["groups"][0]["group_name"] == "活动通知群"
    # 刷新后缓存里就有了：不带 refresh 再拉一次，不打平台接口也没有新变化
    with_request(FakeRequest())
    ok, again, _ = unwrap(run(plugin.page.get_groups()))
    assert ok and again["refreshed"] == 0
    assert {item["value"] for item in again["groups"]} == {
        "napcat:GroupMessage:1001",
        "napcat:GroupMessage:2002",
    }

    # 平台接口炸了也不能让面板开不了
    bad_ctx = FakeContext(platform_insts=[FakePlatformInst("napcat", FakeClient(fail=True))])
    plugin_bad, _ = make_plugin(context=bad_ctx)
    with_request(FakeRequest(query={"refresh": "1"}))
    ok, data, _ = unwrap(run(plugin_bad.page.get_groups()))
    assert ok and data["refreshed"] == 0 and data["groups"] == []

    # 没有平台管理器（老版本 AstrBot / 单测桩）也不报错
    plugin_plain, _ = make_plugin()
    plugin_plain.context.platform_manager = types.SimpleNamespace()
    assert run(plugin_plain.refresh_group_names(force=True)) == 0


def test_page_groups_cached_labels_without_refresh():
    plugin, _ = make_plugin()
    run(collect(plugin.on_message(FakeEvent("你好", umo="napcat:GroupMessage:1001", group_id="1001", group_name="交流群"))))
    run(plugin.store.add({"keyword": "a", "match": "exact", "reply": "1"}, "napcat:GroupMessage:1001"))
    with_request(FakeRequest())
    ok, data, _ = unwrap(run(plugin.page.get_groups()))
    assert ok
    assert data["refreshed"] == 0  # 没带 refresh 就不打平台接口
    assert [item["value"] for item in data["groups"]] == ["napcat:GroupMessage:1001"]
    assert data["groups"][0]["label"] == "交流群（1001）"


def test_group_name_cache_survives_restart():
    plugin, ctx = make_plugin()
    run(collect(plugin.on_message(FakeEvent("你好", umo="napcat:GroupMessage:1001", group_id="1001", group_name="交流群"))))
    assert plugin.groups.path.is_file()
    # 同一数据目录再起一个实例：名字还在（面板不用等新消息）
    again = plugin_main.KeywordReplyPlugin(ctx, {})
    assert again.groups.name_of("napcat:GroupMessage:1001") == "交流群"
    assert again.groups.label("napcat:GroupMessage:1001") == "交流群（1001）"


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
