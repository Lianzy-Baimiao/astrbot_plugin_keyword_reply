# -*- coding: utf-8 -*-
"""store.py 单测：存盘 / 查重 / 范围合并 / 导入导出（不依赖 astrbot）。

    python tests/test_store.py
全绿打印 OK。
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from keyword_reply.rules import FORMAT_MARKDOWN, MATCH_CONTAINS, MATCH_EXACT  # noqa: E402
from keyword_reply.store import (  # noqa: E402
    MAX_RULES_PER_SCOPE,
    SCOPE_GLOBAL,
    RuleStore,
    default_rules,
)

SESSION = "napcat:GroupMessage:339466990"


def _store(tmpdir, name="rules.json"):
    return RuleStore(os.path.join(tmpdir, name))


def _payload(keyword="活动地址", match="精准匹配", reply="一个活动地址", fmt="MD格式", **kw):
    return {"keyword": keyword, "match": match, "reply": reply, "format": fmt, **kw}


def run(coro):
    return asyncio.run(coro)


# ---------------- 基本增删改 ----------------


def test_add_and_list():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        rule = run(st.add(_payload()))
        assert rule["keyword"] == "活动地址"
        assert rule["match"] == MATCH_EXACT
        assert rule["format"] == FORMAT_MARKDOWN
        assert len(st.list_rules(SCOPE_GLOBAL)) == 1


def test_list_returns_copies():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload()))
        got = st.list_rules(SCOPE_GLOBAL)
        got[0]["keyword"] = "被改了"
        assert st.list_rules(SCOPE_GLOBAL)[0]["keyword"] == "活动地址"


def test_duplicate_rejected_then_overwrite_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        first = run(st.add(_payload()))
        try:
            run(st.add(_payload(reply="另一个回复")))
        except ValueError as e:
            assert "已存在" in str(e)
        else:
            raise AssertionError("同关键字同匹配类型应当查重")
        again = run(st.add(_payload(reply="另一个回复"), overwrite=True))
        assert again["id"] == first["id"]          # 覆盖保留 id
        assert again["reply"] == "另一个回复"
        assert len(st.list_rules(SCOPE_GLOBAL)) == 1


def test_same_keyword_different_match_is_not_duplicate():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload()))
        run(st.add(_payload(match="模糊匹配")))
        assert len(st.list_rules(SCOPE_GLOBAL)) == 2


def test_overwrite_keeps_hits_and_created_at():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        first = run(st.add(_payload()))
        st.bump_hits(first["id"])
        again = run(st.add(_payload(reply="新回复"), overwrite=True))
        assert again["hits"] == 1
        assert again["created_at"] == first["created_at"]


def test_update():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        rule = run(st.add(_payload()))
        updated = run(st.update(rule["id"], {"reply": "改过的回复", "format": "文本"}))
        assert updated["id"] == rule["id"]
        assert updated["reply"] == "改过的回复"
        assert updated["format"] == "text"
        assert updated["keyword"] == "活动地址"     # 没传的字段不动


def test_update_missing_id_raises_lookup():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        try:
            run(st.update("nope", {"reply": "x"}))
        except LookupError:
            pass
        else:
            raise AssertionError("改不存在的 id 应当 LookupError")


def test_update_into_duplicate_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload(keyword="活动地址")))
        second = run(st.add(_payload(keyword="在吗", match="模糊匹配")))
        try:
            run(st.update(second["id"], {"keyword": "活动地址", "match": "精准匹配"}))
        except ValueError as e:
            assert "重复" in str(e)
        else:
            raise AssertionError("改成和别人一样应当被拒")


def test_delete():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        a = run(st.add(_payload(keyword="a")))
        b = run(st.add(_payload(keyword="b")))
        result = run(st.delete([a["id"], "不存在的"]))
        assert result["deleted"] == [a["id"]]
        assert result["missing"] == ["不存在的"]
        assert [r["id"] for r in st.list_rules(SCOPE_GLOBAL)] == [b["id"]]


def test_delete_empty_raises():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        try:
            run(st.delete([]))
        except ValueError:
            pass
        else:
            raise AssertionError("空删除列表应当报错")


def test_toggle():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        rule = run(st.add(_payload()))
        assert run(st.toggle(rule["id"]))["enabled"] is False   # 不传就是反转
        assert run(st.toggle(rule["id"]))["enabled"] is True
        assert run(st.toggle(rule["id"], enabled=False))["enabled"] is False
        try:
            run(st.toggle("nope"))
        except LookupError:
            pass
        else:
            raise AssertionError("开关不存在的 id 应当 LookupError")


def test_get_and_find_by_keyword():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        rule = run(st.add(_payload()))
        run(st.add(_payload(match="模糊匹配")))
        assert st.get(rule["id"])["keyword"] == "活动地址"
        assert st.get("nope") is None
        assert len(st.find_by_keyword("活动地址")) == 2
        assert len(st.find_by_keyword("活动地址", match=MATCH_EXACT)) == 1
        assert len(st.find_by_keyword("ACTIVITY")) == 0
        assert len(st.find_by_keyword("活动地址", match=MATCH_CONTAINS)) == 1


def test_max_rules_guard():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.load()
        # 直接灌满内部桶，省得跑 2000 次落盘
        st._scopes[SCOPE_GLOBAL] = [
            {"id": f"id{i}", "keyword": f"k{i}", "match": MATCH_EXACT, "reply": "r",
             "format": "text", "priority": 0, "enabled": True, "hits": 0,
             "created_at": 1.0, "updated_at": 1.0}
            for i in range(MAX_RULES_PER_SCOPE)
        ]
        try:
            run(st.add(_payload(keyword="再来一条")))
        except ValueError as e:
            assert "太多" in str(e)
        else:
            raise AssertionError("超过上限应当被拒")


# ---------------- 存盘 / 读盘 ----------------


def test_persist_and_reload():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rules.json")
        st = RuleStore(path)
        run(st.add(_payload()))
        run(st.add(_payload(keyword="在吗", match="模糊匹配", reply="在的", fmt="文本"), SESSION))

        again = RuleStore(path)
        again.load()
        assert len(again.list_rules(SCOPE_GLOBAL)) == 1
        assert len(again.list_rules(SESSION)) == 1
        assert again.list_rules(SESSION)[0]["reply"] == "在的"


def test_saved_file_shape():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rules.json")
        st = RuleStore(path)
        run(st.add(_payload()))
        raw = json.loads(open(path, encoding="utf-8").read())
        assert raw["version"] == 1
        assert SCOPE_GLOBAL in raw["scopes"]
        assert raw["scopes"][SCOPE_GLOBAL][0]["keyword"] == "活动地址"


def test_no_temp_files_left_behind():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload()))
        leftovers = [n for n in os.listdir(tmp) if n.startswith(".rules-")]
        assert leftovers == [], f"原子写留了临时文件: {leftovers}"


def test_load_missing_file_is_empty():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp, "nope.json")
        st.load()
        assert st.list_rules(SCOPE_GLOBAL) == []


def test_load_corrupt_file_is_empty_not_raise():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rules.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{这不是 json")
        st = RuleStore(path)
        st.load()
        assert st.list_rules(SCOPE_GLOBAL) == []


def test_load_skips_bad_rules_keeps_good_ones():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rules.json")
        payload = {
            "version": 1,
            "scopes": {
                SCOPE_GLOBAL: [
                    {"keyword": "好的", "match": "exact", "reply": "r", "format": "text"},
                    {"keyword": "", "match": "exact", "reply": "r", "format": "text"},
                    {"keyword": "没回复", "match": "exact", "reply": "", "format": "text"},
                    {"keyword": "([", "match": "regex", "reply": "r", "format": "text"},
                    "我不是字典",
                ]
            },
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        st = RuleStore(path)
        st.load()
        assert [r["keyword"] for r in st.list_rules(SCOPE_GLOBAL)] == ["好的"]


def test_load_legacy_flat_rules():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rules.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"rules": [_payload()]}, fh, ensure_ascii=False)
        st = RuleStore(path)
        st.load()
        assert len(st.list_rules(SCOPE_GLOBAL)) == 1


def test_load_dedupes():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rules.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"scopes": {SCOPE_GLOBAL: [_payload(), _payload(reply="重复的")]}}, fh)
        st = RuleStore(path)
        st.load()
        assert len(st.list_rules(SCOPE_GLOBAL)) == 1


# ---------------- 范围合并 ----------------


def test_effective_rules_merges_session_and_global():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload(keyword="活动地址", reply="全局回复")))
        run(st.add(_payload(keyword="在吗", match="模糊匹配", reply="本群回复"), SESSION))
        merged = st.effective_rules(SESSION)
        assert len(merged) == 2
        assert {r["keyword"] for r in merged} == {"活动地址", "在吗"}


def test_session_rule_shadows_global():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload(reply="全局回复")))
        run(st.add(_payload(reply="本群回复"), SESSION))
        merged = st.effective_rules(SESSION)
        assert len(merged) == 1
        assert merged[0]["reply"] == "本群回复"


def test_effective_rules_global_scope_has_no_duplicates():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload()))
        assert len(st.effective_rules(SCOPE_GLOBAL)) == 1


def test_effective_rules_unknown_scope_sees_global_only():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload()))
        assert len(st.effective_rules("napcat:GroupMessage:000")) == 1


def test_match_respects_scope():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload(keyword="仅本群", reply="本群"), SESSION))
        assert len(st.match("仅本群", SESSION)) == 1
        assert st.match("仅本群", "napcat:GroupMessage:000") == []


def test_match_limit_and_ignore_case():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload(keyword="活动", match="模糊匹配")))
        run(st.add(_payload(keyword="活动地址")))
        assert len(st.match("活动地址", SCOPE_GLOBAL)) == 2
        assert len(st.match("活动地址", SCOPE_GLOBAL, limit=1)) == 1
        run(st.add(_payload(keyword="Hello")))
        assert st.match("hello", SCOPE_GLOBAL) == []
        assert len(st.match("hello", SCOPE_GLOBAL, ignore_case=True)) == 1


def test_scope_names_puts_global_first():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload(), SESSION))
        run(st.add(_payload(), "napcat:GroupMessage:111"))
        names = st.scope_names()
        assert names[0] == SCOPE_GLOBAL
        assert set(names[1:]) == {SESSION, "napcat:GroupMessage:111"}


def test_clear_scope():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload(), SESSION))
        run(st.add(_payload(keyword="b"), SESSION))
        run(st.add(_payload()))
        assert run(st.clear_scope(SESSION)) == 2
        assert st.list_rules(SESSION) == []
        assert len(st.list_rules(SCOPE_GLOBAL)) == 1     # 别的范围不受影响


# ---------------- 命中计数 ----------------


def test_bump_hits_in_global_and_session():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        g = run(st.add(_payload(keyword="全局的")))
        s = run(st.add(_payload(keyword="本群的"), SESSION))
        st.bump_hits(g["id"], SESSION)     # 命中的可能是继承来的全局规则
        st.bump_hits(s["id"], SESSION)
        assert st.get(g["id"], SCOPE_GLOBAL)["hits"] == 1
        assert st.get(s["id"], SESSION)["hits"] == 1
        st.bump_hits("不存在", SESSION)      # 不该抛


def test_flush_persists_bumped_hits():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rules.json")
        st = RuleStore(path)
        rule = run(st.add(_payload()))
        st.bump_hits(rule["id"])
        run(st.flush())
        again = RuleStore(path)
        again.load()
        assert again.get(rule["id"], SCOPE_GLOBAL)["hits"] == 1


# ---------------- 导入 / 导出 ----------------


def test_export_shape_drops_runtime_fields():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        rule = run(st.add(_payload()))
        st.bump_hits(rule["id"])
        dumped = st.export_rules(SCOPE_GLOBAL)
        assert dumped["scope"] == SCOPE_GLOBAL
        assert set(dumped["rules"][0]) == {
            "keyword", "match", "reply", "format", "priority", "enabled",
        }


def test_import_then_export_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        src = _store(tmp, "a.json")
        run(src.add(_payload()))
        run(src.add(_payload(keyword="在吗", match="模糊匹配", reply="在的", fmt="文本")))

        dst = _store(tmp, "b.json")
        result = run(dst.import_rules(src.export_rules(SCOPE_GLOBAL)["rules"]))
        assert result["added"] == 2 and result["failed"] == []
        assert {r["keyword"] for r in dst.list_rules(SCOPE_GLOBAL)} == {"活动地址", "在吗"}


def test_import_counts_updates_and_failures():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload(reply="旧回复")))
        result = run(st.import_rules([
            _payload(reply="新回复"),                  # 覆盖已有
            _payload(keyword="在吗", match="模糊匹配"),   # 新增
            {"keyword": "", "reply": "r"},            # 坏的，跳过
            "我不是字典",                               # 坏的，跳过
        ]))
        assert result["added"] == 1
        assert result["updated"] == 1
        assert len(result["failed"]) == 2
        assert st.find_by_keyword("活动地址")[0]["reply"] == "新回复"


def test_import_replace_clears_first():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.add(_payload(keyword="旧的")))
        run(st.import_rules([_payload(keyword="新的")], replace=True))
        assert [r["keyword"] for r in st.list_rules(SCOPE_GLOBAL)] == ["新的"]


def test_import_into_session_scope():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        run(st.import_rules([_payload()], SESSION))
        assert len(st.list_rules(SESSION)) == 1
        assert st.list_rules(SCOPE_GLOBAL) == []


# ---------------- 示例规则 ----------------


def test_default_rules_are_valid_and_importable():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        result = run(st.import_rules(default_rules()))
        assert result["failed"] == []
        assert result["added"] == len(default_rules())
        # 示例里的 MD 规则应当能正常命中
        assert len(st.match("活动地址", SCOPE_GLOBAL)) == 1


def main():
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    if failed:
        print(f"\n{failed} 个用例失败")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
