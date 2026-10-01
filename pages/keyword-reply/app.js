(function () {
  "use strict";

  // 桥接 SDK 已在 <head> 里显式引入（见页面顶部），执行到这里时 window.AstrBotPluginPage
  // 通常已就位；页面跑在 iframe 里，还要等它和父窗口 ready() 握手完才算真正可用。
  // 保险起见下面仍用 waitForBridge 轮询，不在脚本顶部就把它抓进变量。

  var state = {
    scope: "global",
    rules: [],
    meta: { match_types: [], formats: [] },
    editing: null,
    // 「选择群」面板：机器人所在群 + 见过的会话 + 已有规则的会话
    groups: [],
    groupsLoaded: false,
    // 从「选择群」里挑的、后端 scopes 列表里还没有的会话（挑完要留在下拉里）
    extraScopes: {}
  };

  // ---------- 小工具 ----------

  function byId(id) { return document.getElementById(id); }

  var toastTimer = null;
  function toast(msg, isErr) {
    var el = byId("toast");
    el.textContent = String(msg || "");
    el.className = "toast show" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = "toast"; }, 2600);
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 面板跑在 AstrBot 仪表盘的 iframe 里，浏览器会屏蔽 window.prompt/confirm，
  // 所以用页内弹窗替代（否则确认框点了没反应）。
  function _modal(opts) {
    return new Promise(function (resolve) {
      var mask = byId("modalMask");
      var input = byId("modalInput");
      byId("modalTitle").textContent = opts.title || "";
      byId("modalMsg").textContent = opts.message || "";
      input.hidden = !opts.prompt;
      if (opts.prompt) { input.value = opts.value || ""; }
      mask.hidden = false;
      var previousFocus = document.activeElement;
      byId("modalOk").focus();
      function cancelOnEscape(e) {
        if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); done(opts.prompt ? null : false); }
      }
      mask.addEventListener("keydown", cancelOnEscape);
      if (opts.prompt) { input.focus(); input.select(); }
      function done(val) {
        mask.hidden = true;
        mask.removeEventListener("keydown", cancelOnEscape);
        if (previousFocus && previousFocus.focus) previousFocus.focus();
        byId("modalOk").onclick = null;
        byId("modalCancel").onclick = null;
        input.onkeydown = null;
        resolve(val);
      }
      byId("modalOk").onclick = function () { done(opts.prompt ? input.value : true); };
      byId("modalCancel").onclick = function () { done(opts.prompt ? null : false); };
      input.onkeydown = function (e) {
        if (e.key === "Enter") { e.preventDefault(); done(input.value); }
        else if (e.key === "Escape") { e.preventDefault(); done(null); }
      };
    });
  }
  function promptModal(title, message, value) {
    return _modal({ prompt: true, title: title, message: message, value: value || "" });
  }
  function confirmModal(message, title) {
    return _modal({ prompt: false, title: title || "确认", message: message });
  }

  // 后端统一返回 {status, message, data}；status 不是 ok 就抛出 message。
  function unwrap(resp) {
    if (resp && typeof resp === "object" && "status" in resp) {
      if (String(resp.status).toLowerCase() !== "ok") {
        throw new Error(resp.message || "请求失败");
      }
      return resp.data || {};
    }
    return resp || {};
  }

  // 轮询等待桥接脚本加载并完成 ready 握手；超时就抛出可读错误。
  var _bridgeReady = null;
  function waitForBridge(timeoutMs) {
    if (_bridgeReady) return _bridgeReady;
    _bridgeReady = new Promise(function (resolve, reject) {
      var pollTimer;
      var expired = false;
      var timeout = setTimeout(function () {
        expired = true;
        clearTimeout(pollTimer);
        reject(new Error("面板桥接不可用，请在 AstrBot WebUI 里打开本页面"));
      }, timeoutMs || 8000);
      function finish(error, bridge) {
        if (expired) return;
        clearTimeout(timeout);
        if (error) reject(error);
        else resolve(bridge);
      }
      (function poll() {
        var bridge = window.AstrBotPluginPage;
        if (!bridge) { pollTimer = setTimeout(poll, 32); return; }
        Promise.resolve().then(function () {
          if (typeof bridge.ready === "function") return bridge.ready();
        }).then(function () { finish(null, bridge); }, function (err) { finish(err); });
      })();
    }).catch(function (err) {
      _bridgeReady = null;
      throw err;
    });
    return _bridgeReady;
  }

  function apiGet(path, params) {
    return window.PanelUI.request(waitForBridge().then(function (bridge) {
      return bridge.apiGet(path, params || {}).then(unwrap);
    }));
  }

  // 桥接只有 apiGet / apiPost，PUT / DELETE 靠 body 里的 _method 透传。
  function apiPost(path, body) {
    return window.PanelUI.request(waitForBridge().then(function (bridge) {
      return bridge.apiPost(path, body || {}).then(unwrap);
    }));
  }

  function fail(err) {
    toast((err && err.message) || "操作失败", true);
  }

  // 跟随 WebUI 的明暗主题
  function initTheme() { /* Theme is owned by shell.js. */ }

  // ---------- 渲染 ----------

  function fillSelect(sel, items, value) {
    sel.innerHTML = items.map(function (it) {
      return '<option value="' + esc(it.value) + '">' + esc(it.label) + "</option>";
    }).join("");
    if (value != null) sel.value = value;
  }

  function renderStats(st, enabled) {
    var pill = byId("statusPill");
    pill.textContent = enabled ? "自动回复已开启" : "自动回复已关闭";
    pill.className = "pill" + (enabled ? "" : " off");
    byId("stats").innerHTML = [
      ['<div class="stat"><div class="n">', st.total || 0, '</div><div class="l">本范围规则</div></div>'],
      ['<div class="stat"><div class="n">', st.enabled || 0, '</div><div class="l">启用中</div></div>'],
      ['<div class="stat"><div class="n">', st.disabled || 0, '</div><div class="l">已停用</div></div>'],
      ['<div class="stat"><div class="n">', st.hits || 0, '</div><div class="l">累计命中</div></div>']
    ].map(function (p) { return p.join(""); }).join("");
  }

  function renderRows() {
    var kw = byId("searchBox").value.trim().toLowerCase();
    var rows = state.rules.filter(function (r) {
      if (!kw) return true;
      return String(r.keyword).toLowerCase().indexOf(kw) >= 0 ||
        String(r.reply).toLowerCase().indexOf(kw) >= 0;
    });
    var tb = byId("tbody");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="empty">' +
        (state.rules.length ? "没有匹配的规则" : "还没有规则，点右上角「新增规则」") +
        "</td></tr>";
      return;
    }
    tb.innerHTML = rows.map(function (r) {
      var isMd = r.format === "markdown";
      return '<tr class="' + (r.enabled ? "" : "off") + '">' +
        '<td class="kw">' + esc(r.keyword) + "</td>" +
        '<td><span class="tag">' + esc(r.match_label) + "</span></td>" +
        '<td class="reply-cell"><div class="reply-text" data-act="expand">' + esc(r.reply) + "</div></td>" +
        '<td><span class="tag' + (isMd ? " md" : "") + '">' + esc(r.format_label) + "</span></td>" +
        '<td class="hide-sm">' + esc(r.priority || 0) + "</td>" +
        '<td class="hide-sm">' + esc(r.hits || 0) + "</td>" +
        '<td class="acts">' +
          '<button class="link" data-act="edit" data-id="' + esc(r.id) + '">编辑</button>' +
          '<button class="link" data-act="toggle" data-id="' + esc(r.id) + '">' +
            (r.enabled ? "停用" : "启用") + "</button>" +
          '<button class="link danger" data-act="del" data-id="' + esc(r.id) + '">删除</button>' +
        "</td></tr>";
    }).join("");
  }

  // ---------- 数据加载 ----------

  function loadMeta() {
    return apiGet("page/meta").then(function (d) {
      state.meta = d;
      fillSelect(byId("fMatch"), d.match_types || []);
      fillSelect(byId("fFormat"), d.formats || []);
    });
  }

  function loadRules() {
    return apiGet("page/rules", { scope: state.scope }).then(function (d) {
      state.rules = d.rules || [];
      state.scope = d.scope || state.scope;
      renderScopes(d.scopes || []);
      renderStats(d.stats || {}, d.enabled !== false);
      renderRows();
    });
  }

  // 下拉选项 = 后端给的 scopes（有规则 / 见过话的会话，带群名）+ 手动挑过但还没有规则的
  function renderScopes(scopes) {
    var items = scopes.map(function (s) {
      return { value: s.value, label: s.label, count: s.count || 0 };
    });
    Object.keys(state.extraScopes).forEach(function (umo) {
      var exists = items.some(function (it) { return it.value === umo; });
      if (!exists) {
        items.push({ value: umo, label: state.extraScopes[umo], count: 0 });
      }
    });
    fillSelect(byId("scopeSel"), items.map(function (it) {
      return { value: it.value, label: it.label + "（" + it.count + "）" };
    }), state.scope);
  }

  // ---------- 选择群 ----------

  function openPick() {
    byId("pickSearch").value = "";
    byId("pickMask").hidden = false;
    renderPickList();
    byId("pickSearch").focus();
    if (!state.groupsLoaded) loadGroups(false).catch(fail);
  }

  function loadGroups(refresh) {
    return apiGet("page/groups", refresh ? { refresh: "1" } : {}).then(function (d) {
      state.groups = d.groups || [];
      state.groupsLoaded = true;
      renderPickList();
      setPickHint(d);
      if (refresh) {
        toast("已刷新：" + (d.named || 0) + " 个群有名称，共 " + (d.total || 0) + " 个会话");
      }
      return state.groups;
    });
  }

  function setPickHint(d) {
    var hint = byId("pickHint");
    if (!hint) return;
    var named = d.named || 0;
    var total = d.total || 0;
    if (!total) {
      hint.textContent = "还没有可选的群：群里说句话，或点「刷新群列表」。";
      return;
    }
    hint.textContent = "共 " + total + " 个会话，其中 " + named + " 个已取到群名（取不到时显示群号）";
  }

  function pickSourceLabel(source) {
    if (source === "rule") return "有规则";
    if (source === "platform") return "机器人所在";
    return "见过";
  }

  function renderPickList() {
    var box = byId("pickList");
    if (!state.groupsLoaded) {
      box.innerHTML = '<div class="empty">正在加载群列表…</div>';
      return;
    }
    var kw = (byId("pickSearch").value || "").trim().toLowerCase();
    var rows = state.groups.filter(function (g) {
      if (!kw) return true;
      return (String(g.label) + " " + (g.group_name || "") + " " + (g.group_id || ""))
        .toLowerCase().indexOf(kw) >= 0;
    });
    if (!rows.length) {
      box.innerHTML = '<div class="empty">没有匹配的会话。换个词，或点「刷新群列表」重新取一遍。</div>';
      return;
    }
    box.innerHTML = rows.map(function (g) {
      var sub = [];
      if (g.group_id) sub.push("群号 " + g.group_id);
      if (g.member_count) sub.push(g.member_count + " 人");
      sub.push(g.count ? "规则 " + g.count + " 条" : "暂无规则");
      return '<div class="pick-item' + (g.value === state.scope ? " on" : "") +
        '" data-umo="' + esc(g.value) + '">' +
        '<span class="nm">' + esc(g.group_name || g.label) + "</span>" +
        '<span class="sub">' + esc(sub.join(" · ")) + "</span>" +
        '<span class="tag">' + esc(pickSourceLabel(g.source)) + "</span>" +
        "</div>";
    }).join("");
  }

  function pickGroup(umo) {
    var row = state.groups.filter(function (g) { return g.value === umo; })[0];
    if (!row) return;
    state.extraScopes[umo] = row.label;  // 后端 scopes 还没这条时，也留在下拉里
    state.scope = umo;
    byId("pickMask").hidden = true;
    loadRules().then(function () {
      toast("已切到 " + row.label);
    }).catch(fail);
  }

  // ---------- 编辑弹窗 ----------

  function openEdit(rule) {
    state.editing = rule || null;
    byId("editTitle").textContent = rule ? "编辑规则" : "新增规则";
    byId("fKeyword").value = rule ? rule.keyword : "";
    byId("fMatch").value = rule ? rule.match : "exact";
    byId("fFormat").value = rule ? rule.format : "text";
    byId("fReply").value = rule ? rule.reply : "";
    byId("fPriority").value = rule ? (rule.priority || 0) : 0;
    byId("fEnabled").value = rule && !rule.enabled ? "0" : "1";
    byId("editMask").hidden = false;
    byId("fKeyword").focus();
  }

  function saveEdit() {
    var payload = {
      scope: state.scope,
      keyword: byId("fKeyword").value.trim(),
      match: byId("fMatch").value,
      reply: byId("fReply").value,
      format: byId("fFormat").value,
      priority: byId("fPriority").value.trim() || "0",
      enabled: byId("fEnabled").value === "1"
    };
    if (!payload.keyword) { toast("关键字不能为空", true); return; }
    if (!payload.reply.trim()) { toast("回复内容不能为空", true); return; }

    var btn = byId("editSave");
    btn.disabled = true;
    var req;
    if (state.editing) {
      payload.id = state.editing.id;
      payload._method = "PUT";
      req = apiPost("page/rules", payload);
    } else {
      req = apiPost("page/rules", payload).catch(function (err) {
        // 后端把「同关键字 + 同匹配类型」判重了，问一下要不要覆盖
        if (!/已存在同样的规则/.test(err.message || "")) throw err;
        return confirmModal(err.message + "\n\n覆盖掉原来那条？", "覆盖确认").then(function (ok) {
          if (!ok) throw new Error("已取消");
          payload.overwrite = true;
          return apiPost("page/rules", payload);
        });
      });
    }
    req.then(function () {
      byId("editMask").hidden = true;
      toast(state.editing ? "已更新" : "已保存");
      return loadRules();
    }).catch(function (err) {
      if ((err && err.message) !== "已取消") fail(err);
    }).then(function () { btn.disabled = false; });
  }

  // ---------- 行内操作 ----------

  function onTableClick(ev) {
    var target = ev.target;
    var act = target.getAttribute && target.getAttribute("data-act");
    if (!act) return;
    if (act === "expand") { target.classList.toggle("expanded"); return; }

    var id = target.getAttribute("data-id");
    var rule = state.rules.filter(function (r) { return r.id === id; })[0];
    if (!rule) return;

    if (act === "edit") { openEdit(rule); return; }
    if (act === "toggle") {
      apiPost("page/rules/toggle", { scope: state.scope, id: id })
        .then(function () { return loadRules(); })
        .catch(fail);
      return;
    }
    if (act === "del") {
      confirmModal("删除规则「" + rule.keyword + "」？", "删除规则").then(function (ok) {
        if (!ok) return;
        apiPost("page/rules", { scope: state.scope, ids: [id], _method: "DELETE" })
          .then(function () { toast("已删除"); return loadRules(); })
          .catch(fail);
      });
    }
  }

  // ---------- 测试 ----------

  function runTest() {
    var text = byId("tText").value.trim();
    if (!text) { toast("请输入要测试的内容", true); return; }
    byId("tResult").innerHTML = '<p class="hint">测试中…</p>';
    apiPost("page/test", { scope: state.scope, text: text }).then(function (d) {
      var hits = d.matches || [];
      if (!hits.length) {
        byId("tResult").innerHTML = '<p class="hint">没有命中任何规则</p>';
        return;
      }
      byId("tResult").innerHTML = hits.map(function (h) {
        return '<div class="test-hit' + (h.would_reply ? " win" : "") + '">' +
          "<b>" + esc(h.keyword) + "</b> " +
          '<span class="tag">' + esc(h.match_label) + "</span> " +
          '<span class="tag' + (h.format === "markdown" ? " md" : "") + '">' +
            esc(h.format_label) + "</span> " +
          (h.would_reply ? '<span class="pill">实际会回这条</span>' : "") +
          (h.enabled ? "" : ' <span class="pill off">已停用</span>') +
          "<pre>" + esc(h.rendered) + "</pre></div>";
      }).join("");
    }).catch(function (err) {
      byId("tResult").innerHTML = "";
      fail(err);
    });
  }

  // ---------- 导入导出 ----------

  function doExport() {
    apiGet("page/export", { scope: state.scope }).then(function (d) {
      var text = JSON.stringify(d, null, 2);
      var blob = new Blob([text], { type: "application/json" });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "keyword_rules_" + state.scope.replace(/[^\w.-]+/g, "_") + ".json";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(a.href);
      toast("已导出 " + ((d.rules || []).length) + " 条");
    }).catch(fail);
  }

  function doImport() {
    var raw = byId("iText").value.trim();
    if (!raw) { toast("请先粘贴 JSON", true); return; }
    var btn = byId("importRun");
    btn.disabled = true;
    apiPost("page/import", {
      scope: state.scope,
      rules: raw,
      replace: byId("iReplace").value === "1"
    }).then(function (d) {
      byId("importMask").hidden = true;
      byId("iText").value = "";
      toast("新增 " + d.added + " 条，覆盖 " + d.updated + " 条" +
        (d.failed && d.failed.length ? "，跳过 " + d.failed.length + " 条" : ""));
      return loadRules();
    }).catch(fail).then(function () { btn.disabled = false; });
  }

  // ---------- 绑定 ----------

  function bind() {
    byId("tbody").addEventListener("click", onTableClick);
    byId("searchBox").addEventListener("input", renderRows);
    byId("scopeSel").addEventListener("change", function () {
      state.scope = this.value;
      loadRules().catch(fail);
    });
    byId("btnReload").addEventListener("click", function () {
      loadRules().then(function () { toast("已刷新"); }).catch(fail);
    });
    byId("btnPick").addEventListener("click", openPick);
    byId("pickClose").addEventListener("click", function () { byId("pickMask").hidden = true; });
    byId("pickSearch").addEventListener("input", renderPickList);
    byId("btnPickRefresh").addEventListener("click", function () {
      loadGroups(true).catch(fail);
    });
    byId("pickList").addEventListener("click", function (e) {
      var item = e.target && e.target.closest ? e.target.closest(".pick-item") : null;
      if (item) pickGroup(item.getAttribute("data-umo"));
    });
    byId("btnNew").addEventListener("click", function () { openEdit(null); });
    byId("editCancel").addEventListener("click", function () { byId("editMask").hidden = true; });
    byId("editSave").addEventListener("click", saveEdit);

    byId("btnTest").addEventListener("click", function () {
      byId("tResult").innerHTML = "";
      byId("testMask").hidden = false;
      byId("tText").focus();
    });
    byId("tRun").addEventListener("click", runTest);
    byId("tText").addEventListener("keydown", function (e) {
      if (e.key === "Enter") runTest();
    });
    byId("testClose").addEventListener("click", function () { byId("testMask").hidden = true; });

    byId("btnExport").addEventListener("click", doExport);
    byId("btnImport").addEventListener("click", function () { byId("importMask").hidden = false; });
    byId("importCancel").addEventListener("click", function () { byId("importMask").hidden = true; });
    byId("importRun").addEventListener("click", doImport);

    byId("btnClear").addEventListener("click", function () {
      var label = byId("scopeSel").selectedOptions[0];
      confirmModal("清空「" + (label ? label.textContent : state.scope) +
        "」下的所有规则？此操作不可撤销。", "清空规则").then(function (ok) {
        if (!ok) return;
        apiPost("page/rules/clear", { scope: state.scope, confirm: "1" })
          .then(function (d) { toast("已清空 " + d.cleared + " 条"); return loadRules(); })
          .catch(fail);
      });
    });

    // 点遮罩空白处关闭弹窗
    ["editMask", "testMask", "importMask", "pickMask"].forEach(function (id) {
      byId(id).addEventListener("click", function (e) {
        if (e.target === this) this.hidden = true;
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      ["editMask", "testMask", "importMask", "pickMask"].forEach(function (id) { byId(id).hidden = true; });
    });
  }

  initTheme();
  bind();
  byId("tbody").innerHTML = '<tr><td colspan="7" class="empty">正在连接面板…</td></tr>';
  waitForBridge()
    .then(loadMeta)
    .then(loadRules)
    .catch(function (err) {
      byId("tbody").innerHTML = '<tr><td colspan="7" class="empty">' +
        esc((err && err.message) || "加载失败") + "</td></tr>";
      fail(err);
    });
})();
