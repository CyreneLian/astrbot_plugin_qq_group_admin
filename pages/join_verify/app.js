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

  function renderGroups(groups) {
    var box = document.getElementById("groups-list");
    if (!groups || !groups.length) {
      box.innerHTML = '<div class="empty">未获取到 Bot 所在群（请确认 Bot 已加入群聊且平台正常）</div>';
      return;
    }
    // 排序：Bot 有管理员/群主权限的群排上面，普通成员/未知权限排下面
    groups = groups.slice().sort(function (a, b) {
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
      var dd = window._defaultsData || {};
      var permTip = canPerm ? "" : '<div class="perm-tip">Bot 无管理权限：自动同意/拒绝、人机验证等权限项不可调节（欢迎词仍可设置）</div>';
      // 每个设置项所属的总开关（插件配置面板的全局默认）；欢迎词无总开关、永可调
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
        join_verify_welcome_msg: ""
      };
      // 禁用原因：无权限或总开关未开 → 返回原因文案（空=不禁用）；欢迎词永不禁用
      function disabledReason(key) {
        var m = MASTER[key];
        if (m === "") return "";
        if (!canPerm) return "Bot 在群内无管理权限，无法调节";
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
      return (
        '<div class="group-card" data-gid="' + g.group_id + '">' +
        '<div class="group-card-header">' +
        '<span class="group-id">群号 ' + g.group_id + '</span>' +
        '<span class="group-name">' + (g.group_name ? g.group_name : "") + '</span>' +
        '<span class="role-badge ' + role.cls + '">' + role.text + '</span>' +
        '<span class="override-badge ' + (g.overridden ? "on" : "") + '">' + (g.overridden ? "已覆盖" : "跟随默认") + "</span>" +
        "</div>" +
        '<div class="cfg-grid">' +
        sw("auto_accept_group_request", "等级达标自动同意") +
        sw("auto_reject_below_level", "等级未达自动拒绝") +
        sw("auto_accept_whitelist", "白词命中自动同意") +
        sw("auto_reject_whitelist_miss", "未命中白词自动拒绝") +
        sw("auto_accept_dual_verify", "入群双重审核") +
        sw("auto_reject_dual_verify", "拒绝入群双重验证") +
        num("auto_accept_group_level", "QQ 等级门槛（0=不限）") +
        txt("auto_accept_group_whitelist", "白词列表（逗号分隔）") +
        "</div>" +
        '<div class="cfg-grid">' +
        sw("enable_join_verify", "开启人机验证") +
        num("join_verify_timeout", "验证超时（秒）") +
        num("join_verify_max_attempts", "最大错误次数") +
        num("join_verify_max_failures", "拉黑阈值（0=不限）") +
        "</div>" +
        '<div class="cfg-section-title">入群欢迎词</div>' +
        '<div class="cfg-grid">' +
        txt("join_verify_welcome_msg", "自定义欢迎词") +
        "</div>" + permTip +
        '<div class="group-card-actions">' +
        '<button class="btn btn-sm" onclick="resetGroupConfig(\'' + g.group_id + '\')">重置本群为默认</button>' +
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
    box.innerHTML =
      '<div class="defs-group"><div class="defs-title">🚪 自动同意 / 拒绝入群默认值</div>' +
      row("等级达标自动同意", onoff(d.auto_accept_group_request)) +
      row("等级未达自动拒绝", onoff(d.auto_reject_below_level)) +
      row("白词命中自动同意", onoff(d.auto_accept_whitelist)) +
      row("未命中白词自动拒绝", onoff(d.auto_reject_whitelist_miss)) +
      row("入群双重审核", onoff(d.auto_accept_dual_verify)) +
      row("拒绝入群双重验证", onoff(d.auto_reject_dual_verify)) +
      row("QQ 等级门槛", (d.auto_accept_group_level || 0) > 0 ? d.auto_accept_group_level + " 级" : "0（不限）") +
      row("白词列表", wl ? wl : "未设置") +
      '</div><div class="defs-group"><div class="defs-title">🛡️ 入群人机验证默认值</div>' +
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
    if (!off.length) { tip.style.display = "none"; tip.innerHTML = ""; return; }
    tip.style.display = "";
    tip.innerHTML = '⚠️ 总开关未开启：' + off.join("、") + '——对应设置不可调节，请前往 AstrBot 插件配置面板「自动同意入群工具 / 入群人机验证工具」分组开启';
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

  // 保存单键覆盖（change 即保存；空值 = 取消覆盖跟随默认）
  window.saveGroupConfig = function (gid, key, value) {
    apiPost("group-config", { group_id: gid, key: key, value: value })
      .then(function (resp) {
        showToast("已保存");
        // 局部更新「跟随默认 / 已覆盖」徽章（不重建列表，保留编辑状态）
        var card = document.querySelector('.group-card[data-gid="' + gid + '"]');
        if (card) {
          var badge = card.querySelector(".override-badge");
          if (badge) {
            var on = !!(resp && resp.overridden);
            badge.textContent = on ? "已覆盖" : "跟随默认";
            badge.classList.toggle("on", on);
          }
        }
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
        }
      })
      .catch(function (e) { showToast("重置失败：" + (e && e.message ? e.message : e)); });
  };

  // 事件委托：开关/输入的变更即保存
  document.addEventListener("change", function (e) {
    var el = e.target;
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
