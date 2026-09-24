# -*- coding: utf-8 -*-
"""rules.py 纯函数单测（不依赖 astrbot，可本地直接跑）。

    python tests/test_rules.py
全绿打印 OK。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from keyword_reply.rules import (  # noqa: E402
    FORMAT_MARKDOWN,
    FORMAT_TEXT,
    MATCH_CONTAINS,
    MATCH_EXACT,
    MATCH_PREFIX,
    MATCH_REGEX,
    MATCH_SUFFIX,
    MAX_KEYWORD_LEN,
    MAX_REPLY_LEN,
    compile_regex,
    describe_rule,
    find_matches,
    format_label,
    match_label,
    normalize_format,
    normalize_match,
    normalize_rule,
    parse_rule_line,
    render_reply,
    rule_key,
    rule_matches,
    stats,
)

# ---------------- 枚举归一化 ----------------


def test_normalize_match_chinese_aliases():
    assert normalize_match("精准匹配") == MATCH_EXACT
    assert normalize_match("精确") == MATCH_EXACT
    assert normalize_match("模糊匹配") == MATCH_CONTAINS
    assert normalize_match("包含") == MATCH_CONTAINS
    assert normalize_match("前缀匹配") == MATCH_PREFIX
    assert normalize_match("后缀") == MATCH_SUFFIX
    assert normalize_match("正则表达式") == MATCH_REGEX


def test_normalize_match_english_and_fallback():
    assert normalize_match("EXACT") == MATCH_EXACT       # 大小写无关
    assert normalize_match("startswith") == MATCH_PREFIX
    assert normalize_match("") == MATCH_EXACT            # 空 → 默认
    assert normalize_match("瞎写的") == MATCH_EXACT       # 认不出 → 默认
    assert normalize_match(None, default=MATCH_CONTAINS) == MATCH_CONTAINS


def test_normalize_format_aliases():
    assert normalize_format("MD格式") == FORMAT_MARKDOWN
    assert normalize_format("md") == FORMAT_MARKDOWN
    assert normalize_format("markdown") == FORMAT_MARKDOWN
    assert normalize_format("文本") == FORMAT_TEXT
    assert normalize_format("纯文本") == FORMAT_TEXT
    assert normalize_format("") == FORMAT_TEXT
    assert normalize_format("不认识") == FORMAT_TEXT      # 落回最安全的纯文本


def test_labels_roundtrip():
    assert match_label("exact") == "精准匹配"
    assert match_label("瞎写") == "精准匹配"
    assert format_label("markdown") == "MD格式"
    assert format_label("text") == "文本"


# ---------------- 正则编译缓存 ----------------


def test_compile_regex_caches_and_tolerates_bad_pattern():
    p1 = compile_regex(r"^\d+$")
    p2 = compile_regex(r"^\d+$")
    assert p1 is p2                      # 同一对象 = 命中缓存
    assert compile_regex("([") is None   # 写错的正则不抛异常


def test_compile_regex_ignore_case_is_separate_entry():
    assert compile_regex("abc").search("ABC") is None
    assert compile_regex("abc", ignore_case=True).search("ABC") is not None


# ---------------- normalize_rule ----------------


def _payload(**kw):
    base = {"keyword": "活动地址", "match": "精准匹配", "reply": "一个活动地址", "format": "MD格式"}
    base.update(kw)
    return base


def test_normalize_rule_happy_path():
    rule = normalize_rule(_payload())
    assert rule["keyword"] == "活动地址"
    assert rule["match"] == MATCH_EXACT
    assert rule["reply"] == "一个活动地址"
    assert rule["format"] == FORMAT_MARKDOWN
    assert rule["enabled"] is True
    assert rule["hits"] == 0
    assert rule["priority"] == 0
    assert len(rule["id"]) == 12
    assert rule["created_at"] > 0 and rule["updated_at"] > 0


def test_normalize_rule_rejects_empty_fields():
    for bad in ({"keyword": ""}, {"keyword": "   "}):
        try:
            normalize_rule(_payload(**bad))
        except ValueError as e:
            assert "关键字" in str(e)
        else:
            raise AssertionError("空关键字应当被拒")
    for bad in ({"reply": ""}, {"reply": "  \n "}):
        try:
            normalize_rule(_payload(**bad))
        except ValueError as e:
            assert "回复" in str(e)
        else:
            raise AssertionError("空回复应当被拒")


def test_normalize_rule_rejects_oversize():
    try:
        normalize_rule(_payload(keyword="x" * (MAX_KEYWORD_LEN + 1)))
    except ValueError as e:
        assert "太长" in str(e)
    else:
        raise AssertionError("超长关键字应当被拒")
    try:
        normalize_rule(_payload(reply="x" * (MAX_REPLY_LEN + 1)))
    except ValueError as e:
        assert "太长" in str(e)
    else:
        raise AssertionError("超长回复应当被拒")


def test_normalize_rule_rejects_bad_regex():
    try:
        normalize_rule(_payload(keyword="([", match="正则匹配"))
    except ValueError as e:
        assert "正则" in str(e)
    else:
        raise AssertionError("写错的正则应当在存盘前就被拦下")


def test_normalize_rule_non_dict():
    try:
        normalize_rule("我不是字典")
    except ValueError:
        pass
    else:
        raise AssertionError("非字典应当被拒")


def test_normalize_rule_priority_clamped_and_validated():
    assert normalize_rule(_payload(priority="5"))["priority"] == 5
    assert normalize_rule(_payload(priority=99999))["priority"] == 999
    assert normalize_rule(_payload(priority=-99999))["priority"] == -999
    try:
        normalize_rule(_payload(priority="高"))
    except ValueError as e:
        assert "整数" in str(e)
    else:
        raise AssertionError("非整数优先级应当被拒")


def test_normalize_rule_enabled_accepts_strings():
    assert normalize_rule(_payload(enabled="false"))["enabled"] is False
    assert normalize_rule(_payload(enabled="关"))["enabled"] is False
    assert normalize_rule(_payload(enabled="0"))["enabled"] is False
    assert normalize_rule(_payload(enabled="true"))["enabled"] is True
    assert normalize_rule(_payload(enabled=1))["enabled"] is True


def test_normalize_rule_edit_keeps_identity_and_hits():
    old = normalize_rule(_payload())
    old["hits"] = 7
    new = normalize_rule({"reply": "换了个回复"}, existing=old)
    assert new["id"] == old["id"]                  # id 不变
    assert new["hits"] == 7                        # 命中数不清零
    assert new["created_at"] == old["created_at"]  # 创建时间保留
    assert new["keyword"] == old["keyword"]        # 没传的字段沿用旧值
    assert new["reply"] == "换了个回复"
    assert new["updated_at"] >= old["updated_at"]


# ---------------- 匹配 ----------------


def _rule(keyword, match, **kw):
    return normalize_rule({"keyword": keyword, "match": match, "reply": "r", **kw})


def test_rule_matches_each_type():
    assert rule_matches(_rule("活动地址", "精准匹配"), "活动地址")
    assert not rule_matches(_rule("活动地址", "精准匹配"), "问下活动地址")

    assert rule_matches(_rule("活动", "模糊匹配"), "问下活动地址在哪")
    assert not rule_matches(_rule("活动", "模糊匹配"), "今天天气不错")

    assert rule_matches(_rule("查询", "前缀匹配"), "查询角色卡")
    assert not rule_matches(_rule("查询", "前缀匹配"), "我要查询角色卡")

    assert rule_matches(_rule("怎么办", "后缀匹配"), "这个副本打不过怎么办")
    assert not rule_matches(_rule("怎么办", "后缀匹配"), "怎么办才好呢")

    assert rule_matches(_rule(r"^\d{5,}$", "正则匹配"), "123456")
    assert not rule_matches(_rule(r"^\d{5,}$", "正则匹配"), "123")


def test_rule_matches_ignore_case():
    rule = _rule("Hello", "精准匹配")
    assert not rule_matches(rule, "hello")
    assert rule_matches(rule, "hello", ignore_case=True)
    rx = _rule("abc", "正则匹配")
    assert not rule_matches(rx, "ABC")
    assert rule_matches(rx, "ABC", ignore_case=True)


def test_rule_matches_empty_inputs():
    assert not rule_matches(_rule("x", "精准匹配"), "")
    assert not rule_matches({"keyword": "", "match": "exact"}, "abc")


def test_rule_matches_broken_regex_is_false_not_raise():
    # 正常路径下 normalize_rule 会拦住烂正则，但存量文件里可能混进来
    assert not rule_matches({"keyword": "([", "match": "regex", "enabled": True}, "abc")


# ---------------- 命中排序 ----------------


def test_find_matches_exact_beats_contains():
    rules = [_rule("活动", "模糊匹配"), _rule("活动地址", "精准匹配")]
    hit = find_matches(rules, "活动地址")
    assert len(hit) == 2
    assert hit[0]["match"] == MATCH_EXACT      # 精准优先，不被 contains 抢走


def test_find_matches_specificity_order():
    rules = [
        _rule("活动", "模糊匹配"),
        _rule("活动.*", "正则匹配"),
        _rule("活动", "前缀匹配"),
        _rule("活动地址", "精准匹配"),
    ]
    order = [r["match"] for r in find_matches(rules, "活动地址")]
    assert order == [MATCH_EXACT, MATCH_PREFIX, MATCH_REGEX, MATCH_CONTAINS]


def test_find_matches_priority_wins_over_specificity():
    rules = [
        _rule("活动地址", "精准匹配"),
        _rule("活动", "模糊匹配", priority=10),
    ]
    assert find_matches(rules, "活动地址")[0]["match"] == MATCH_CONTAINS


def test_find_matches_longer_keyword_first_within_same_type():
    rules = [_rule("活动", "模糊匹配"), _rule("活动地址", "模糊匹配")]
    assert find_matches(rules, "问下活动地址")[0]["keyword"] == "活动地址"


def test_find_matches_skips_disabled_unless_asked():
    rules = [_rule("活动地址", "精准匹配", enabled=False)]
    assert find_matches(rules, "活动地址") == []
    assert len(find_matches(rules, "活动地址", include_disabled=True)) == 1


def test_find_matches_limit_and_blank_text():
    rules = [_rule("活动", "模糊匹配"), _rule("活动地址", "精准匹配")]
    assert len(find_matches(rules, "活动地址", limit=1)) == 1
    assert find_matches(rules, "   ") == []


def test_find_matches_strips_text():
    assert len(find_matches([_rule("活动地址", "精准匹配")], "  活动地址  ")) == 1


# ---------------- 占位符渲染 ----------------


def test_render_reply_replaces_known_placeholders():
    out = render_reply("你好 {sender}，群 {group}，关键字 {keyword}", {
        "sender": "张三", "group": "123", "keyword": "活动地址",
    })
    assert out == "你好 张三，群 123，关键字 活动地址"


def test_render_reply_keeps_other_braces():
    # markdown 表格 / JSON 里的大括号不能炸（这正是不用 str.format 的原因）
    tpl = '{"a": 1, "b": {"c": 2}} 与 {unknown} 保持原样，{sender} 替换'
    out = render_reply(tpl, {"sender": "张三"})
    assert '{"a": 1, "b": {"c": 2}}' in out
    assert "{unknown}" in out
    assert "张三" in out


def test_render_reply_missing_value_becomes_empty():
    assert render_reply("hi {sender}", {}) == "hi "
    assert render_reply("hi {sender}", None) == "hi "


def test_render_reply_none_template():
    assert render_reply(None) == ""


# ---------------- 聊天命令解析 ----------------


def test_parse_rule_line_full():
    got = parse_rule_line("活动地址|精准匹配|一个活动地址|MD格式")
    assert got == {
        "keyword": "活动地址",
        "match": "精准匹配",
        "reply": "一个活动地址",
        "format": "MD格式",
    }


def test_parse_rule_line_fullwidth_separator():
    got = parse_rule_line("活动地址｜精准匹配｜一个活动地址｜MD格式")
    assert got["keyword"] == "活动地址" and got["format"] == "MD格式"


def test_parse_rule_line_defaults():
    got = parse_rule_line("在吗|模糊匹配|在的")
    assert got["format"] == FORMAT_TEXT      # 格式可省 → 文本
    got2 = parse_rule_line("在吗||在的")
    assert got2["match"] == MATCH_EXACT      # 匹配类型留空 → 精准


def test_parse_rule_line_errors():
    for bad in ("", "   ", "只有关键字", "关键字|精准匹配"):
        try:
            parse_rule_line(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应当报错: {bad!r}")


# ---------------- 其它 ----------------


def test_rule_key_is_case_insensitive_per_match_type():
    assert rule_key("Hello", "exact") == rule_key("hello", "精准匹配")
    assert rule_key("hello", "exact") != rule_key("hello", "contains")


def test_describe_rule_truncates_long_reply():
    line = describe_rule(normalize_rule(_payload(reply="很长的回复" * 20)))
    assert "…" in line
    assert "精准匹配" in line and "MD格式" in line


def test_describe_rule_marks_disabled():
    assert "已停用" in describe_rule(normalize_rule(_payload(enabled=False)))


def test_stats_counts():
    rules = [
        normalize_rule(_payload(keyword="a")),
        normalize_rule(_payload(keyword="b", match="模糊匹配", format="文本")),
        normalize_rule(_payload(keyword="c", enabled=False)),
    ]
    rules[0]["hits"] = 3
    rules[1]["hits"] = 4
    got = stats(rules)
    assert got["total"] == 3
    assert got["enabled"] == 2
    assert got["disabled"] == 1
    assert got["hits"] == 7
    assert got["by_match"][MATCH_EXACT] == 2
    assert got["by_match"][MATCH_CONTAINS] == 1
    assert got["by_format"][FORMAT_MARKDOWN] == 2
    assert got["by_format"][FORMAT_TEXT] == 1


def test_stats_tolerates_bad_hits():
    assert stats([{"keyword": "a", "match": "exact", "format": "text", "hits": "坏值"}])["hits"] == 0


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
