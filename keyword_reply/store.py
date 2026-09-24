# -*- coding: utf-8 -*-
"""规则存盘：单个 json 文件 + 异步锁，纯本地逻辑，可直接单测。

数据结构（data/plugin_data/astrbot_plugin_keyword_reply/rules.json）：

    {
      "version": 1,
      "scopes": {
        "global": [ {规则}, ... ],
        "napcat:GroupMessage:12345": [ {规则}, ... ]
      }
    }

「global」是全局规则，对所有会话生效；其余 key 是 unified_msg_origin，只对该会话生效。
查询时两者合并，会话规则优先级更高（同一关键字覆盖全局那条）。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .rules import (
    MATCH_EXACT,
    find_matches,
    new_rule_id,
    normalize_rule,
    rule_key,
    sort_key,
)

SCOPE_GLOBAL = "global"
STORE_VERSION = 1

# 单个会话的规则条数上限，防止面板批量导入把文件撑爆
MAX_RULES_PER_SCOPE = 2000


class RuleStore:
    """规则的读写与查询。所有写操作都加锁并原子落盘。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._scopes: dict[str, list[dict[str, Any]]] = {}
        self._loaded = False
        self._dirty = False

    # ---------------- 读写盘 ----------------

    def load(self) -> None:
        """同步读盘。文件不存在或坏了都当空库处理，不抛异常。"""
        self._scopes = {}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict):
                scopes = raw.get("scopes")
                if isinstance(scopes, dict):
                    for scope, items in scopes.items():
                        if not isinstance(items, list):
                            continue
                        self._scopes[str(scope)] = self._sanitize_list(items)
                elif isinstance(raw.get("rules"), list):
                    # 兼容更早的扁平结构（只有一份全局规则）
                    self._scopes[SCOPE_GLOBAL] = self._sanitize_list(raw["rules"])
        self._loaded = True
        self._dirty = False

    @staticmethod
    def _sanitize_list(items: Iterable[Any]) -> list[dict[str, Any]]:
        """逐条过 normalize_rule，坏规则跳过而不是整个文件作废。"""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            try:
                rule = normalize_rule(item, existing=item if isinstance(item, dict) else None)
            except ValueError:
                continue
            key = rule_key(rule["keyword"], rule["match"])
            if key in seen:
                continue
            seen.add(key)
            out.append(rule)
        out.sort(key=sort_key)
        return out

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> None:
        """原子写：先写同目录临时文件再 replace，避免写一半断电留个坏 json。"""
        self._ensure_loaded()
        payload = {
            "version": STORE_VERSION,
            "updated_at": time.time(),
            "scopes": {scope: items for scope, items in self._scopes.items() if items},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".rules-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._dirty = False

    async def flush(self) -> None:
        async with self._lock:
            if self._dirty:
                await asyncio.to_thread(self.save)

    # ---------------- 查询 ----------------

    def scope_names(self) -> list[str]:
        self._ensure_loaded()
        names = [s for s in self._scopes if s != SCOPE_GLOBAL and self._scopes[s]]
        names.sort()
        return [SCOPE_GLOBAL] + names

    def list_rules(self, scope: str = SCOPE_GLOBAL) -> list[dict[str, Any]]:
        """某个 scope 自己的规则（不含全局）。返回副本，调用方改不到内部状态。"""
        self._ensure_loaded()
        return [dict(rule) for rule in self._scopes.get(scope or SCOPE_GLOBAL, [])]

    def effective_rules(self, scope: str | None) -> list[dict[str, Any]]:
        """某会话实际生效的规则：会话规则 + 全局规则，同键会话覆盖全局。"""
        self._ensure_loaded()
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rule in self._scopes.get(scope or "", []):
            seen.add(rule_key(rule["keyword"], rule["match"]))
            merged.append(rule)
        for rule in self._scopes.get(SCOPE_GLOBAL, []):
            if rule_key(rule["keyword"], rule["match"]) in seen:
                continue
            merged.append(rule)
        merged.sort(key=sort_key)
        return merged

    def match(
        self,
        text: str,
        scope: str | None = None,
        *,
        ignore_case: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return find_matches(
            self.effective_rules(scope),
            text,
            ignore_case=ignore_case,
            limit=limit,
        )

    def get(self, rule_id: str, scope: str = SCOPE_GLOBAL) -> dict[str, Any] | None:
        self._ensure_loaded()
        for rule in self._scopes.get(scope or SCOPE_GLOBAL, []):
            if rule.get("id") == rule_id:
                return dict(rule)
        return None

    def find_by_keyword(
        self,
        keyword: str,
        scope: str = SCOPE_GLOBAL,
        *,
        match: str | None = None,
    ) -> list[dict[str, Any]]:
        """按关键字精确找规则（聊天命令删除用）。match 为空则同名的都返回。"""
        self._ensure_loaded()
        needle = str(keyword or "").strip().lower()
        out = []
        for rule in self._scopes.get(scope or SCOPE_GLOBAL, []):
            if str(rule.get("keyword") or "").strip().lower() != needle:
                continue
            if match and rule.get("match") != match:
                continue
            out.append(dict(rule))
        return out

    # ---------------- 写入（同步核心，异步包装在下面） ----------------

    def _bucket(self, scope: str) -> list[dict[str, Any]]:
        self._ensure_loaded()
        scope = scope or SCOPE_GLOBAL
        if scope not in self._scopes:
            self._scopes[scope] = []
        return self._scopes[scope]

    def _add_sync(
        self,
        payload: dict[str, Any],
        scope: str,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        bucket = self._bucket(scope)
        rule = normalize_rule(payload)
        key = rule_key(rule["keyword"], rule["match"])
        for idx, old in enumerate(bucket):
            if rule_key(old["keyword"], old["match"]) != key:
                continue
            if not overwrite:
                raise ValueError(
                    f"已存在同样的规则：{old['keyword']}（{old['match']}），"
                    "改用编辑，或勾选覆盖"
                )
            rule["id"] = old["id"]
            rule["hits"] = old.get("hits", 0)
            rule["created_at"] = old.get("created_at", rule["created_at"])
            bucket[idx] = rule
            bucket.sort(key=sort_key)
            self._dirty = True
            return dict(rule)
        if len(bucket) >= MAX_RULES_PER_SCOPE:
            raise ValueError(f"规则太多了（上限 {MAX_RULES_PER_SCOPE} 条）")
        bucket.append(rule)
        bucket.sort(key=sort_key)
        self._dirty = True
        return dict(rule)

    def _update_sync(self, rule_id: str, payload: dict[str, Any], scope: str) -> dict[str, Any]:
        bucket = self._bucket(scope)
        for idx, old in enumerate(bucket):
            if old.get("id") != rule_id:
                continue
            merged = normalize_rule(payload, existing=old)
            new_key = rule_key(merged["keyword"], merged["match"])
            for other_idx, other in enumerate(bucket):
                if other_idx == idx:
                    continue
                if rule_key(other["keyword"], other["match"]) == new_key:
                    raise ValueError(
                        f"改完会和已有规则重复：{other['keyword']}（{other['match']}）"
                    )
            bucket[idx] = merged
            bucket.sort(key=sort_key)
            self._dirty = True
            return dict(merged)
        raise LookupError(f"找不到规则：{rule_id}")

    def _delete_sync(self, rule_ids: Iterable[str], scope: str) -> dict[str, list[str]]:
        bucket = self._bucket(scope)
        wanted = [str(rid) for rid in rule_ids if str(rid or "").strip()]
        if not wanted:
            raise ValueError("没有要删除的规则")
        keep, deleted = [], []
        for rule in bucket:
            if rule.get("id") in wanted:
                deleted.append(rule["id"])
            else:
                keep.append(rule)
        self._scopes[scope or SCOPE_GLOBAL] = keep
        if deleted:
            self._dirty = True
        return {
            "deleted": deleted,
            "missing": [rid for rid in wanted if rid not in deleted],
        }

    def _toggle_sync(self, rule_id: str, scope: str, enabled: bool | None) -> dict[str, Any]:
        bucket = self._bucket(scope)
        for rule in bucket:
            if rule.get("id") != rule_id:
                continue
            rule["enabled"] = (not rule.get("enabled", True)) if enabled is None else bool(enabled)
            rule["updated_at"] = time.time()
            self._dirty = True
            return dict(rule)
        raise LookupError(f"找不到规则：{rule_id}")

    def _import_sync(
        self,
        items: Iterable[Any],
        scope: str,
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        bucket = self._bucket(scope)
        if replace:
            bucket.clear()
        added, updated, failed = 0, 0, []
        for raw in items:
            try:
                before = len(bucket)
                self._add_sync(raw if isinstance(raw, dict) else {}, scope, overwrite=True)
                if len(bucket) > before:
                    added += 1
                else:
                    updated += 1
            except ValueError as exc:
                failed.append(str(exc))
        return {"added": added, "updated": updated, "failed": failed, "total": len(bucket)}

    def bump_hits(self, rule_id: str, scope: str | None = None) -> None:
        """命中计数 +1。会话规则和全局规则都可能是命中的那条，两边都找。"""
        self._ensure_loaded()
        for name in ({scope, SCOPE_GLOBAL} if scope else {SCOPE_GLOBAL}):
            for rule in self._scopes.get(name or "", []):
                if rule.get("id") == rule_id:
                    rule["hits"] = int(rule.get("hits", 0) or 0) + 1
                    self._dirty = True
                    return

    # ---------------- 异步包装：加锁 + 立即落盘 ----------------

    async def add(
        self,
        payload: dict[str, Any],
        scope: str = SCOPE_GLOBAL,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            rule = self._add_sync(payload, scope, overwrite=overwrite)
            await asyncio.to_thread(self.save)
            return rule

    async def update(
        self,
        rule_id: str,
        payload: dict[str, Any],
        scope: str = SCOPE_GLOBAL,
    ) -> dict[str, Any]:
        async with self._lock:
            rule = self._update_sync(rule_id, payload, scope)
            await asyncio.to_thread(self.save)
            return rule

    async def delete(
        self,
        rule_ids: Iterable[str],
        scope: str = SCOPE_GLOBAL,
    ) -> dict[str, list[str]]:
        async with self._lock:
            result = self._delete_sync(rule_ids, scope)
            await asyncio.to_thread(self.save)
            return result

    async def toggle(
        self,
        rule_id: str,
        scope: str = SCOPE_GLOBAL,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            rule = self._toggle_sync(rule_id, scope, enabled)
            await asyncio.to_thread(self.save)
            return rule

    async def import_rules(
        self,
        items: Iterable[Any],
        scope: str = SCOPE_GLOBAL,
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            result = self._import_sync(items, scope, replace=replace)
            await asyncio.to_thread(self.save)
            return result

    async def clear_scope(self, scope: str) -> int:
        async with self._lock:
            self._ensure_loaded()
            count = len(self._scopes.get(scope, []))
            self._scopes[scope] = []
            self._dirty = True
            await asyncio.to_thread(self.save)
            return count

    def export_rules(self, scope: str = SCOPE_GLOBAL) -> dict[str, Any]:
        """导出成可再导入的结构，去掉 id / 命中数这些运行时字段。"""
        return {
            "version": STORE_VERSION,
            "scope": scope,
            "exported_at": time.time(),
            "rules": [
                {
                    "keyword": rule["keyword"],
                    "match": rule["match"],
                    "reply": rule["reply"],
                    "format": rule["format"],
                    "priority": rule.get("priority", 0),
                    "enabled": rule.get("enabled", True),
                }
                for rule in self.list_rules(scope)
            ],
        }


def default_rules() -> list[dict[str, Any]]:
    """首次启动塞两条示例，让面板不是空的、用户一眼看懂四个字段怎么填。"""
    return [
        {
            "id": new_rule_id(),
            "keyword": "活动地址",
            "match": MATCH_EXACT,
            "reply": "## 本周活动\n\n- 地址：https://example.com/activity\n- 截止：周日 24:00",
            "format": "markdown",
            "priority": 0,
            "enabled": True,
        },
        {
            "id": new_rule_id(),
            "keyword": "在吗",
            "match": "contains",
            "reply": "在的，{sender}，有什么事？",
            "format": "text",
            "priority": 0,
            "enabled": True,
        },
    ]
