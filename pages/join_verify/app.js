/* 入群审核管理面板前端逻辑 */

(function () {
  "use strict";

  var bridge = window.AstrBotPluginPage;
  var toastTimer = null;

  function showToast(message) {
    var toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toast.classList.remove("show");
    }, 2200);
  }

  function unwrap(response) {
    if (
      response &&
      typeof response === "object" &&
      Object.prototype.hasOwnProperty.call(response, "ok")
    ) {
      if (!response.ok) {
        throw new Error(response.message || "请求失败");
      }
      return Object.prototype.hasOwnProperty.call(response, "data")
        ? response.data
        : response;
    }
    return response;
  }

  function apiGet(path, params) {
    return bridge.apiGet(path, params || {}).then(unwrap);
  }

  function apiPost(path, body) {
    return bridge.apiPost(path, body || {}).then(unwrap);
  }

  function renderRow(user) {
    // 生成单行 HTML（供局部更新使用，保持行位置不变）
    var toggleText = user.blacklisted ? "解除" : "拉黑";
    var toggleCls = user.blacklisted ? "btn-success" : "btn-warn";
    var status = user.blacklisted
      ? '<span class="badge badge-danger">已拉黑</span>'
      : '<span class="badge badge-normal">正常</span>';
    var remainingText =
      user.remaining < 0 ? "不限" : user.remaining > 0 ? user.remaining : "已用完";
    return (
      "<td>" +
      user.user_id +
      "</td>" +
      "<td>" +
      user.failures +
      "</td>" +
      "<td>" +
      remainingText +
      "</td>" +
      "<td>" +
      status +
      "</td>" +
      "<td>" +
      '<div class="btn-group">' +
      '<button class="btn btn-sm ' +
      toggleCls +
      '" id="btn-toggle-' +
      user.user_id +
      '" onclick="handleToggle(\'' +
      user.user_id +
      '\')">' +
      toggleText +
      "</button>" +
      '<button class="btn btn-sm btn-danger" id="btn-reset-' +
      user.user_id +
      '" onclick="handleReset(\'' +
      user.user_id +
      '\')">删除</button>' +
      "</div>" +
      "</td>"
    );
  }

  function updateOverview() {
    // 只刷新概览卡片（不重新排序列表）
    apiGet("overview")
      .then(function (data) {
        _maxFailures = data.max_failures != null ? data.max_failures : 0;
        document.getElementById("stat-total").textContent =
          data.total_records != null ? data.total_records : "-";
        document.getElementById("stat-blacklisted").textContent =
          data.blacklisted_count != null ? data.blacklisted_count : "-";
        document.getElementById("stat-failures").textContent =
          data.total_failures != null ? data.total_failures : "-";
        document.getElementById("stat-max").textContent =
          data.max_failures != null
            ? data.max_failures > 0
              ? data.max_failures
              : "不限"
            : "-";
      })
      .catch(function () {});
  }

  function renderUsers(users) {
    _usersCache = users || [];
    var tbody = document.getElementById("user-tbody");
    if (!users || users.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty">暂无记录</td></tr>';
      return;
    }
    tbody.innerHTML = "";
    users.forEach(function (u) {
      var tr = document.createElement("tr");


      tr.setAttribute("data-uid", u.user_id);
      tr.innerHTML = renderRow(u);
      tbody.appendChild(tr);
    });
  }

  // 顶部加载光带控制：body.is-loading 时顶部移动横线动画。
  // 计数版：多个请求并发时各自 +1/-1，全部完成才关闭，避免快的请求提前关掉光带
  var _loadingCount = 0;
  function setLoading(on) {
    if (on) {
      _loadingCount += 1;
    } else {
      _loadingCount = Math.max(0, _loadingCount - 1);
    }
    document.body.classList.toggle("is-loading", _loadingCount > 0);
  }

  function loadAll() {
    resetAllPending();
    var pending = 2;
    function allDone() { if (--pending <= 0) setLoading(false); }
    setLoading(true);
    apiGet("overview")
      .then(function (data) {
        _maxFailures = data.max_failures != null ? data.max_failures : 0;
        document.getElementById("stat-total").textContent =
          data.total_records != null ? data.total_records : "-";
        document.getElementById("stat-blacklisted").textContent =
          data.blacklisted_count != null ? data.blacklisted_count : "-";
        document.getElementById("stat-failures").textContent =
          data.total_failures != null ? data.total_failures : "-";
        document.getElementById("stat-max").textContent =
          data.max_failures != null
            ? data.max_failures > 0
              ? data.max_failures
              : "不限"
            : "-";
      })
      .catch(function (e) {
        showToast("加载概览失败：" + e.message);
      })
      .finally(allDone);

    apiGet("users")
      .then(function (users) {
        renderUsers(users);
      })
      .catch(function (e) {
        var tbody = document.getElementById("user-tbody");
        tbody.innerHTML =
          '<tr><td colspan="5" class="empty">加载失败：' +
          (e && e.message ? e.message : "未知错误") +
          "</td></tr>";
        showToast("加载用户列表失败：" + (e && e.message ? e.message : "未知错误"));
      })
      .finally(allDone);
  }

  // ---- 二次确认机制（iframe 内 confirm 可能被拦截，改用"再次点击确认"） ----
  // （原二次点击确认机制的 pendingMap 已移除，改用弹窗确认）
  var _usersCache = [];
  var _maxFailures = 0;

  function findUser(userId) {
    for (var i = 0; i < _usersCache.length; i++) {
      if (_usersCache[i].user_id === userId) return _usersCache[i];
    }
    return null;
  }

  function resetButtonText(buttonId) {
    var btn = document.getElementById(buttonId);
    if (btn) {
      if (buttonId === "btn-clear") btn.textContent = "清空全部失败记录";
      else if (buttonId.indexOf("btn-toggle-") === 0) {
        // 从按钮 id 反推用户状态需要查询；这里通过数据属性恢复
        var uid = buttonId.replace("btn-toggle-", "");
        var u = findUser(uid);
        btn.textContent = u && u.blacklisted ? "解除" : "拉黑";
      }
      else if (buttonId.indexOf("btn-reset-") === 0) btn.textContent = "删除";
    }
  }

  function resetAllPending() {
    // 弹窗机制下无需清理待确认状态；仅恢复顶部固定按钮文本
    resetButtonText("btn-clear");
  }

  // ===== 弹窗确认机制（10 秒无操作自动关闭）=====
  var _modalCallback = null;
  var _modalTimer = null;

  function showConfirm(text, callback) {
    document.getElementById("modal-text").textContent = text;
    _modalCallback = callback;
    document.getElementById("confirm-modal").style.display = "flex";
    // 10 秒无操作自动关闭弹窗并提示取消
    clearTimeout(_modalTimer);
    _modalTimer = setTimeout(function () {
      hideModal();
      showToast("操作已取消");
    }, 10000);
  }

  function hideModal() {
    clearTimeout(_modalTimer);
    _modalTimer = null;
    document.getElementById("confirm-modal").style.display = "none";
    _modalCallback = null;
  }

  // 绑定弹窗按钮（脚本在 body 末尾加载，DOM 已就绪）
  document.getElementById("modal-cancel").onclick = hideModal;
  document.getElementById("modal-ok").onclick = function () {
    var cb = _modalCallback;
    hideModal();
    if (cb) cb();
  };
  document.getElementById("confirm-modal").onclick = function (e) {
    // 点击遮罩空白区域关闭
    if (e.target === this) hideModal();
  };

  function doReset(userId) {
    apiPost("reset", { user_id: userId })
      .then(function (res) {
        showToast((res && res.message) || "操作成功");
        // 局部删除该行（不整体刷新，保持其他行位置稳定）
        _usersCache = _usersCache.filter(function (u) {
          return u.user_id !== userId;
        });
        var tbody = document.getElementById("user-tbody");
        if (tbody) {
          var trs = tbody.querySelectorAll("tr");
          trs.forEach(function (tr) {
            if (tr.getAttribute("data-uid") === userId) tr.remove();
          });
          if (_usersCache.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty">暂无记录</td></tr>';
          }
        }
        updateOverview();
      })
      .catch(function (e) {
        showToast("操作失败：" + (e && e.message ? e.message : "未知错误"));
      });
  }

  function doClear() {
    apiPost("clear", {})
      .then(function (res) {
        showToast((res && res.message) || "操作成功");
        loadAll();
      })
      .catch(function (e) {
        showToast("操作失败：" + (e && e.message ? e.message : "未知错误"));
      });
  }

  function handleToggle(userId) {
    var u = findUser(userId);
    var isBlacklist = u && u.blacklisted;
    var hint = isBlacklist
      ? "确定要将用户 " + userId + " 移出黑名单吗？"
      : "确定要将用户 " + userId + " 拉入黑名单吗？该用户会在10秒后被移出Bot拥有管理员权限的群聊";
    showConfirm(hint, function () {
      apiPost("toggle-blacklist", { user_id: userId })
      .then(function (res) {
        showToast((res && res.message) || "操作成功");
        // 局部更新该行（不整体刷新，避免排序瞬间跳动让用户误以为操作出错）
        var u = findUser(userId);
        if (u) {
          if (u.blacklisted) {
            // 解除：剩余 1 次（设上限）或不限（未设上限），失败次数保持不动
            u.blacklisted = false;
            u.remaining = _maxFailures > 0 ? 1 : -1;
          } else {
            // 拉黑：剩余次数清零，失败次数保持不动
            u.blacklisted = true;
            u.remaining = 0;
          }
          var tbody = document.getElementById("user-tbody");
          if (tbody) {
            var trs = tbody.querySelectorAll("tr");
            trs.forEach(function (tr) {
              if (tr.getAttribute("data-uid") === userId) {
                tr.innerHTML = renderRow(u);
              }
            });
          }
        }
        updateOverview();
      })
      .catch(function (e) {
        showToast("操作失败：" + (e && e.message ? e.message : "未知错误"));
      });
    });
  }

  function handleReset(userId) {
    showConfirm("确定要删除用户 " + userId + " 的记录吗？", function () {
      doReset(userId);
    });
  }

  function handleClear() {
    showConfirm("确定要清空全部用户的失败记录吗？此操作不可撤销！", function () {
      doClear();
    });
  }

  function handleAddBlacklist() {
    var input = document.getElementById("input-add-user");
    var userId = (input.value || "").trim();
    if (!userId) {
      showToast("请输入要拉黑的 QQ 号");
      return;
    }
    if (!/^\d+$/.test(userId)) {
      showToast("QQ号格式不正确，请输入纯数字");
      return;
    }
    showConfirm("确定要将用户 " + userId + " 拉入黑名单吗？该用户会在10秒后被移出Bot拥有管理员权限的群聊", function () {
      apiPost("add-blacklist", { user_id: userId })
        .then(function (res) {
          showToast((res && res.message) || "操作成功");
          input.value = "";
          loadAll();
        })
        .catch(function (e) {
          showToast("操作失败：" + (e && e.message ? e.message : "未知错误"));
        });
    });
  }

  // ===== 视图切换（黑名单管理 / 入群工具管理）=====
  window.switchView = function (view) {
    document.querySelectorAll(".view").forEach(function (v) { v.style.display = "none"; });
    document.querySelectorAll(".tab-btn").forEach(function (b) { b.classList.remove("active"); });
    var target = document.querySelector(".view-" + view);
    if (target) target.style.display = "";
    var btn = document.querySelector('.tab-btn[data-view="' + view + '"]');
    if (btn) btn.classList.add("active");
    // 页面标题跟随视图：入群工具管理 ↔ 黑名单管理
    var title = document.getElementById("pageTitle");
    if (title) title.textContent = view === "tools" ? "入群工具管理" : "黑名单管理";
    // 切入群工具管理 → 刷新群列表；切黑名单管理 → 刷新黑名单数据（都带顶部加载光带）
    if (view === "tools") { loadDefaults(); loadGroups(); }
    else if (view === "blacklist") loadAll();
  };

  // ===== 入群工具管理：群列表 =====
  function roleLabel(role) {
    if (role === "owner") return { text: "群主", cls: "role-owner" };
    if (role === "admin" || role === "administrator") return { text: "管理员", cls: "role-admin" };
    if (role === "member") return { text: "成员", cls: "role-member" };
    return { text: "未知", cls: "role-unknown" };
  }

  // ===== 群聊筛选 =====
  var _allGroups = [];          // 全量群列表（筛选的基准数据）
  var _filters = { role: "all", state: "all", conf: "all", keyword: "" };

  // 设置某个维度的筛选值
  window.setFilter = function (dim, val, btn) {
    _filters[dim] = val;
    var group = btn && btn.parentNode;
    if (group) {
      Array.prototype.forEach.call(group.children, function (c) { c.classList.remove("active"); });
      if (btn) btn.classList.add("active");
    }
    applyFilters();
  };

  // 重置全部筛选
  window.resetFilters = function () {
    _filters = { role: "all", state: "all", conf: "all", keyword: "" };
    ["filter-role", "filter-state", "filter-conf"].forEach(function (id) {
      var box = document.getElementById(id);
      if (box) Array.prototype.forEach.call(box.children, function (c, i) {
        c.classList.toggle("active", i === 0);
      });
    });
    var kw = document.getElementById("filter-keyword");
    if (kw) kw.value = "";
    applyFilters();
  };

  // 按当前筛选条件过滤
  window.applyFilters = function () {
    var kwEl = document.getElementById("filter-keyword");
    _filters.keyword = (kwEl && kwEl.value ? kwEl.value : "").trim().toLowerCase();
    renderGroups(_allGroups, true);
  };

  // 归一化角色：administrator 归入 admin
  function roleOf(g) {
    var r = (g.bot_role || "").toLowerCase();
    if (r === "administrator") return "admin";
    return r || "unknown";
  }

  // 真正的过滤 + 绘制入口（filterMode=true 时跳过重新排序后的全量覆盖）
  function renderGroups(groups, filterMode) {
    var box = document.getElementById("groups-list");
    // 基准数据更新必须先于空态判断：
    // 否则 Bot 退群后（后端返回空列表）缓存会保留旧数据，筛选时仍显示已退群的群
    if (!filterMode) _allGroups = groups || [];
    var all = _allGroups;
    var cnt0 = document.getElementById("filter-count");
    if (!all.length) {
      if (cnt0) cnt0.textContent = "";
      box.innerHTML = '<div class="empty">未获取到 Bot 所在群（请确认 Bot 已加入群聊且平台正常）</div>';
      return;
    }
    if (filterMode) groups = all;
    // 应用筛选
    var f = _filters;
    var kw = f.keyword;
    groups = groups.filter(function (g) {
      var r = roleOf(g);
      if (f.role === "owner" && r !== "owner") return false;
      if (f.role === "admin" && r !== "admin") return false;
      if (f.role === "member" && r !== "member") return false;
      if (f.state === "normal" && g.blacklisted) return false;
      if (f.state === "black" && !g.blacklisted) return false;
      if (f.conf === "over" && !g.overridden) return false;
      if (f.conf === "follow" && g.overridden) return false;
      if (kw) {
        var hay = (String(g.group_id || "") + " " + String(g.group_name || "")).toLowerCase();
        if (hay.indexOf(kw) < 0) return false;
      }
      return true;
    });
    var cnt = document.getElementById("filter-count");
    if (cnt) {
      var total = _allGroups.length;
      cnt.textContent = groups.length === total
        ? "共 " + total + " 个群聊"
        : "已筛选 " + groups.length + " / " + total + " 个群聊";
    }
    if (!groups.length) {
      box.innerHTML = '<div class="empty">没有符合筛选条件的群聊，试试调整或重置筛选</div>';
      return;
    }
    // 排序：Bot 有管理员/群主权限的群排上面，普通成员/未知权限排下面，黑名单群统一沉到最后
    groups = groups.slice().sort(function (a, b) {
      if (!!a.blacklisted !== !!b.blacklisted) return a.blacklisted ? 1 : -1;
      function rank(r) {
        if (r === "owner") return 0;
        if (r === "admin" || r === "administrator") return 1;
        if (r === "member") return 2;
        return 3;
      }
      return rank(a.bot_role) - rank(b.bot_role);
    });
    box.innerHTML = groups.map(function (g) {
      var role = roleLabel(g.bot_role);
      var canPerm = ["owner", "admin", "administrator"].indexOf(g.bot_role) !== -1;
      var cfg = g.config || {};
      var isBlacklisted = !!g.blacklisted;
      var dd = window._defaultsData || {};
      var permTip = g.blacklisted
        ? '<div class="perm-tip perm-tip-black">🚫 该群聊在黑名单中，插件的全部功能均已禁用，下方配置暂不生效。如需恢复，请在插件配置面板「群聊黑名单」分组中移出该群聊</div>'
        : (canPerm ? "" : '<div class="perm-tip">Bot 无管理权限：自动同意/拒绝入群、入群人机验证等权限项不可调节（「群聊管理与互动工具」分组与欢迎词不受此限制，仍可设置）</div>');
      // 每个设置项所属的总开关（插件配置面板的全局默认）；欢迎词无总开关、永可调
      // 普通群友可用工具清单（来自 _conf_schema.json 的 member_allowed_tools.options）
      var TOOL_OPTIONS = ["读取历史消息", "禁言/解禁", "踢人/拉黑", "清理潜水成员", "撤回消息", "设置/取消精华", "全体禁言", "修改群名片", "设置专属头衔", "设置/取消管理员", "修改群名称", "发布群公告", "获取群信息", "获取全员列表", "查询成员信息", "戳一戳", "@全体成员"];
      var MASTER = {
        auto_accept_group_request: "auto_accept_group_request",
        auto_reject_below_level: "auto_reject_below_level",
        auto_accept_whitelist: "auto_accept_whitelist",
        auto_reject_whitelist_miss: "auto_reject_whitelist_miss",
        auto_accept_dual_verify: "auto_accept_dual_verify",
        auto_reject_dual_verify: "auto_reject_dual_verify",
        auto_accept_group_level: "auto_accept_group_request",
        auto_accept_group_whitelist: "auto_accept_whitelist",
        enable_join_verify: "enable_join_verify",
        join_verify_timeout: "enable_join_verify",
        join_verify_max_attempts: "enable_join_verify",
        join_verify_max_failures: "enable_join_verify",
        join_verify_welcome_msg: "",
        // 群聊管理与互动工具：三个行为开关映射到自己 → 即各自的总开关
        // （插件设置里关闭时，面板对应控件会置灰，与入群审核各开关保持一致）
        allow_bot_admin: "", allow_group_owner: "", allow_group_admin: "",
        enable_at_feature: "enable_at_feature", enable_poke_reply: "enable_poke_reply",
        enable_group_decrease_notice: "enable_group_decrease_notice",
        member_allowed_tools: ""
      };
      // 禁用原因：无权限或总开关未开 → 返回原因文案（空=不禁用）；欢迎词永不禁用
      // 无需 Bot 群权限即可调节的项（纯设置项）：与 web.py 的 _NON_PERM_KEYS 保持一致
      var NO_PERM_KEYS = {
        allow_bot_admin: 1, allow_group_owner: 1, allow_group_admin: 1,
        enable_at_feature: 1, enable_poke_reply: 1,
        enable_group_decrease_notice: 1, member_allowed_tools: 1,
        join_verify_welcome_msg: 1
      };
      function disabledReason(key) {
        // 黑名单群：功能整体已禁用，所有配置项均不可调
        if (isBlacklisted) return "该群聊在黑名单中，插件功能已禁用";
        var m = MASTER[key];
        if (m === "") return "";
        if (!NO_PERM_KEYS[key] && !canPerm) return "Bot 在群内无管理权限，无法调节";
        if (m && !dd[m]) return "总开关未开启，需在插件配置面板开启";
        return "";
      }
      // 禁用时把提示挂到外层 .cfg-item 的 data-tip（自定义 tooltip，hover 立即显示；
      // disabled 元素本身不响应 hover，所以不放在 input 上）
      function sw(key, label) {
        var r = disabledReason(key);
        return '<div class="cfg-item"' + (r ? ' data-tip="' + r + '"' : '') + '><span class="cfg-label">' + label + '</span><label class="cfg-switch"><input type="checkbox" data-gid="' + g.group_id + '" data-key="' + key + '"' +
          (cfg[key] ? " checked" : "") + (r ? " disabled" : "") + "></label></div>";
      }
      function num(key, hint) {
        var r = disabledReason(key);
        return '<div class="cfg-item"' + (r ? ' data-tip="' + r + '"' : '') + '><label>' + hint + '</label><input type="number" data-gid="' + g.group_id + '" data-key="' + key +
          '" value="' + (cfg[key] === null || cfg[key] === undefined ? "" : cfg[key]) + '"' + (r ? " disabled" : "") + '></div>';
      }
      function txt(key, hint) {
        var r = disabledReason(key);
        return '<div class="cfg-item cfg-item-wide"' + (r ? ' data-tip="' + r + '"' : '') + '><label>' + hint + '</label><input type="text" data-gid="' + g.group_id + '" data-key="' + key +
          '" value="' + (cfg[key] || "") + '" placeholder="留空跟随插件默认"' + (r ? " disabled" : "") + '></div>';
      }
      // 普通群友可用工具：多选勾选框（每群可独立设置）
      function toolsMulti(key) {
        var r = disabledReason(key);
        var cur = Array.isArray(cfg[key]) ? cfg[key] : (cfg[key] || "").split(",").map(function (x) { return x.trim(); }).filter(Boolean);
        var opts = (window._defaultsData && window._defaultsData.member_allowed_tools_options) || TOOL_OPTIONS;
        return '<div class="cfg-item cfg-item-wide cfg-item-multi"' + (r ? ' data-tip="' + r + '"' : '') +
          '><label>勾选的工具该群普通群友即可调用</label><div class="cfg-multi-box"' + (r ? ' disabled' : '') + '>' +
          opts.map(function (name) {
            var on = cur.indexOf(name) >= 0;
            return '<label class="cfg-multi-item"><input type="checkbox" data-gid="' + g.group_id + '" data-key="' + key +
              '" data-multi="1" value="' + name + '"' + (on ? " checked" : "") + (r ? " disabled" : "") + "><span>" + name + "</span></label>";
          }).join("") +
          '</div></div>';
      }
      return (
        '<div class="group-card' + (g.blacklisted ? " is-blacklisted" : "") + '" data-gid="' + g.group_id + '">' +
        '<div class="group-card-header">' +
        '<span class="group-id">群号 ' + g.group_id + '</span>' +
        '<span class="group-name">' + (g.group_name ? g.group_name : "") + '</span>' +
        '<span class="role-badge ' + role.cls + '">' + role.text + '</span>' +
        (g.blacklisted ? '<span class="blacklist-badge" title="该群聊在黑名单中，插件的全部功能均已禁用">🚫 黑名单</span>' : "") +
        '<span class="override-badge ' + (g.overridden ? "on" : "") + '">' + (g.overridden ? "已覆盖" : "跟随默认") + "</span>" +
        "</div>" + permTip +
        '<div class="cfg-section-title">群聊管理与互动工具</div>' +
        '<div class="cfg-grid">' +
        sw("allow_bot_admin", "Bot 超级管理员") +
        sw("allow_group_owner", "群主") +
        sw("allow_group_admin", "群管理员") +
        sw("enable_at_feature", "允许 @ 成员") +
        sw("enable_poke_reply", "戳一戳回复") +
        sw("enable_group_decrease_notice", "退群提示") +
        "</div>" +
        '<div class="cfg-section-title">普通群友可用工具</div>' +
        toolsMulti("member_allowed_tools") +
        '<div class="cfg-section-title">自动同意入群工具</div>' +
        '<div class="cfg-grid">' +
        sw("auto_accept_group_request", "等级达标自动同意") +
        sw("auto_reject_below_level", "等级未达自动拒绝") +
        num("auto_accept_group_level", "QQ 等级门槛（0=不限）") +
        sw("auto_accept_whitelist", "白词命中自动同意") +
        sw("auto_reject_whitelist_miss", "未命中白词自动拒绝") +
        txt("auto_accept_group_whitelist", "白词列表（逗号分隔）") +
        sw("auto_accept_dual_verify", "入群双重审核") +
        sw("auto_reject_dual_verify", "拒绝入群双重验证") +
        "</div>" +
        '<div class="cfg-section-title">入群人机验证工具</div>' +
        '<div class="cfg-grid">' +
        sw("enable_join_verify", "开启人机验证") +
        num("join_verify_timeout", "验证超时（秒）") +
        num("join_verify_max_attempts", "最大错误次数") +
        num("join_verify_max_failures", "拉黑阈值（0=不限）") +
        "</div>" +
        '<div class="cfg-section-title cfg-title-tail">入群欢迎词</div>' +
        '<div class="cfg-grid cfg-grid-tail">' +
        txt("join_verify_welcome_msg", "自定义欢迎词") +
        "</div>" +
        '<div class="group-card-actions">' +
        '<button class="btn btn-sm" onclick="resetGroupConfig(\'' + g.group_id + '\')"' + (isBlacklisted ? " disabled" : "") + ">重置本群为默认</button>" +
        "</div>" +
        "</div>"
      );
    }).join("");
  }

  function loadDefaults() {
    var box = document.getElementById("defaultsPanel");
    if (box) box.innerHTML = '<span class="tools-hint">加载默认值…</span>';
    apiGet("defaults")
      .then(function (data) {
        window._defaultsData = (data && data.defaults) || null;
        renderDefaults(window._defaultsData);
      })
      .catch(function () {
        window._defaultsData = null;
        var b = document.getElementById("defaultsPanel"); if (b) b.innerHTML = "";
        renderMasterTip(null);
      });
  }

  function renderDefaults(d) {
    var box = document.getElementById("defaultsPanel");
    if (!box) return;
    if (!d) { box.innerHTML = ""; return; }
    function row(k, v) { return '<div class="defs-row"><span>' + k + '</span><b>' + v + '</b></div>'; }
    function onoff(v) { return v ? "开启" : "关闭"; }
    var wl = Array.isArray(d.auto_accept_group_whitelist)
      ? d.auto_accept_group_whitelist.join("、")
      : (d.auto_accept_group_whitelist || "");
    var tools = Array.isArray(d.member_allowed_tools)
      ? d.member_allowed_tools.join("、")
      : (d.member_allowed_tools || "");
    box.innerHTML =
      '<div class="defs-group"><div class="defs-title">🛠️ 群聊管理与互动工具默认值</div>' +
      row("Bot 超级管理员", onoff(d.allow_bot_admin)) +
      row("群主", onoff(d.allow_group_owner)) +
      row("群管理员", onoff(d.allow_group_admin)) +
      row("允许 @ 成员", onoff(d.enable_at_feature)) +
      row("戳一戳回复", onoff(d.enable_poke_reply)) +
      row("退群提示", onoff(d.enable_group_decrease_notice)) +
      row("普通群友可用工具", tools ? tools : "未设置") +
      '</div><div class="defs-group"><div class="defs-title">🚪 自动同意入群工具默认值</div>' +
      row("等级达标自动同意", onoff(d.auto_accept_group_request)) +
      row("等级未达自动拒绝", onoff(d.auto_reject_below_level)) +
      row("QQ 等级门槛", (d.auto_accept_group_level || 0) > 0 ? d.auto_accept_group_level + " 级" : "0（不限）") +
      row("白词命中自动同意", onoff(d.auto_accept_whitelist)) +
      row("未命中白词自动拒绝", onoff(d.auto_reject_whitelist_miss)) +
      row("白词列表", wl ? wl : "未设置") +
      row("入群双重审核", onoff(d.auto_accept_dual_verify)) +
      row("拒绝入群双重验证", onoff(d.auto_reject_dual_verify)) +
      '</div><div class="defs-group"><div class="defs-title">🛡️ 入群人机验证工具默认值</div>' +
      row("验证", onoff(d.enable_join_verify)) +
      row("验证超时", (d.join_verify_timeout || 0) + " 秒") +
      row("最大错误次数", (d.join_verify_max_attempts || 0) + " 次") +
      row("拉黑阈值", (d.join_verify_max_failures || 0) > 0 ? d.join_verify_max_failures + " 次" : "0（不限制）") +
      row("自定义欢迎词", d.join_verify_welcome_msg ? d.join_verify_welcome_msg : "未设置") +
      '</div>';
    renderMasterTip(d);
  }

  // 底部提示条：总开关未开启的项 + 前往插件配置面板指引
  function renderMasterTip(d) {
    var tip = document.getElementById("masterTip");
    if (!tip) return;
    if (!d) { tip.style.display = "none"; tip.innerHTML = ""; return; }
    var off = [];
    if (!d.auto_accept_group_request) off.push("等级达标自动同意");
    if (!d.auto_reject_below_level) off.push("等级未达自动拒绝");
    if (!d.auto_accept_whitelist) off.push("白词命中自动同意");
    if (!d.auto_reject_whitelist_miss) off.push("未命中白词自动拒绝");
    if (!d.auto_accept_dual_verify) off.push("入群双重审核");
    if (!d.auto_reject_dual_verify) off.push("拒绝入群双重验证");
    if (!d.enable_join_verify) off.push("入群人机验证");
    if (!d.enable_at_feature) off.push("@ 成员功能");
    if (!d.enable_poke_reply) off.push("戳一戳回复");
    if (!d.enable_group_decrease_notice) off.push("退群提示");
    if (!off.length) { tip.style.display = "none"; tip.innerHTML = ""; return; }
    tip.style.display = "";
    tip.innerHTML = '⚠️ 总开关未开启：' + off.join("、") + '——对应设置不可调节，请前往 AstrBot 插件配置面板「群聊管理与互动工具 / 自动同意入群工具 / 入群人机验证工具」分组开启';
  }

  function loadGroups() {
    var box = document.getElementById("groups-list");
    if (box) box.innerHTML = "加载中...";
    setLoading(true);
    apiGet("groups")
      .then(function (data) { renderGroups(data || []); })
      .catch(function (e) { showToast("加载群列表失败：" + (e && e.message ? e.message : e)); })
      .finally(function () { setLoading(false); });
  }

  // 局部更新「跟随默认 / 已覆盖」徽章（不重建列表，保留编辑状态）
  function updateOverrideBadge(gid, resp) {
    // 更新基准数据里的 overridden，供筛选重算使用
    var g = null;
    for (var i = 0; i < _allGroups.length; i++) {
      if (String(_allGroups[i].group_id) === String(gid)) { g = _allGroups[i]; break; }
    }
    var on = !!(resp && resp.overridden);
    if (g) g.overridden = on;

    var card = document.querySelector('.group-card[data-gid="' + gid + '"]');
    if (!card) return;
    var badge = card.querySelector(".override-badge");
    if (!badge) return;
    badge.textContent = on ? "已覆盖" : "跟随默认";
    badge.classList.toggle("on", on);

    // 当前筛选维度涉及 overridden（已覆盖 / 跟随默认）时，归属可能已变化 → 重算列表
    if (_filters.conf !== "all") applyFilters();
  }

  // 保存单键覆盖（change 即保存；空值 = 取消覆盖跟随默认）
  window.saveGroupConfig = function (gid, key, value) {
    // 多选类型（普通群友可用工具）：收集该组全部勾选项一次性提交
    var multiInputs = document.querySelectorAll('.group-card[data-gid="' + gid + '"] input[data-key="' + key + '"][data-multi]');
    if (multiInputs.length) {
      var picked = [];
      multiInputs.forEach(function (el) { if (el.checked) picked.push(el.value); });
      apiPost("group-config", { group_id: gid, key: key, value: picked })
        .then(function (resp) {
          showToast("已保存");
          updateOverrideBadge(gid, resp);   // ← 补上：多选也要刷新「跟随默认/已覆盖」徽章
        })
        .catch(function (e) {
          if (e && e.code === "NO_PERMISSION") showToast(e.message || "无权限修改该项");
          else showToast("保存失败：" + (e && e.message ? e.message : e));
        });
      return;
    }
    apiPost("group-config", { group_id: gid, key: key, value: value })
      .then(function (resp) {
        showToast("已保存");
        updateOverrideBadge(gid, resp);
      })
      .catch(function (e) {
        if (e && e.code === "NO_PERMISSION") showToast(e.message || "无权限修改该项");
        else showToast("保存失败：" + (e && e.message ? e.message : e));
      });
  };

  window.resetGroupConfig = function (gid) {
    apiPost("group-config-reset", { group_id: gid })
      .then(function (resp) {
        showToast("已重置为插件默认值");
        var card = document.querySelector('.group-card[data-gid="' + gid + '"]');
        // 刷新效果：扫光动画（先移除类→强制重排→再添加，保证重复点击也能重新播放）
        if (card) {
          card.classList.remove("refreshing");
          void card.offsetWidth;
          card.classList.add("refreshing");
        }
        // 局部更新该群卡片：控件恢复插件默认值、徽章变「跟随默认」，不刷新整个列表
        if (card && resp && resp.config) {
          card.querySelectorAll("[data-key]").forEach(function (el) {
            var v = resp.config[el.dataset.key];
            // 多选框（普通群友可用工具）：按数组逐项比对勾选状态
            if (el.dataset.multi) {
              var picked = Array.isArray(v) ? v : (v || "").split(",").map(function (x) { return x.trim(); }).filter(Boolean);
              el.checked = picked.indexOf(el.value) >= 0;
              return;
            }
            if (el.type === "checkbox") {
              el.checked = !!v;
            } else {
              el.value = (v === null || v === undefined) ? "" : v;
            }
          });
          var badge = card.querySelector(".override-badge");
          if (badge) {
            badge.textContent = "跟随默认";
            badge.classList.remove("on");
          }
          // 同步基准数据：重置后该群不再是「已覆盖」
          for (var _i = 0; _i < _allGroups.length; _i++) {
            if (String(_allGroups[_i].group_id) === String(gid)) { _allGroups[_i].overridden = false; break; }
          }
          if (_filters.conf !== "all") applyFilters();
        }
      })
      .catch(function (e) { showToast("重置失败：" + (e && e.message ? e.message : e)); });
  };

  // 事件委托：开关/输入的变更即保存
  document.addEventListener("change", function (e) {
    var el = e.target;
    // 多选类型（普通群友可用工具）：勾选变化时收集全部勾选项一次性保存
    if (el && el.dataset && el.dataset.multi) {
      var mgid = el.dataset.gid, mkey = el.dataset.key;
      if (mgid && mkey) saveGroupConfig(mgid, mkey, null);
      return;
    }
        if (!el || !el.dataset || !el.dataset.key) return;
        var gid = el.dataset.gid;
        var key = el.dataset.key;
        if (!gid || !key) return;
        var val = el.type === "checkbox" ? el.checked : el.value;
        saveGroupConfig(gid, key, val);
  });

  // 暴露给全局（供 HTML onclick 使用）
  window.handleReset = handleReset;
  window.handleToggle = handleToggle;
  window.handleAddBlacklist = handleAddBlacklist;
  window.handleClear = handleClear;
  window.loadAll = loadAll;
  window.loadGroups = loadGroups;

  // 等 bridge 就绪后初始化
  function init() {
    loadAll();
    loadDefaults();
    loadGroups();
  }
  if (bridge && typeof bridge.ready === "function") {
    bridge.ready().then(init);
  } else {
    window.addEventListener("DOMContentLoaded", init);
  }
})();
