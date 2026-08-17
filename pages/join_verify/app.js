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

  function loadAll() {
    resetAllPending();
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
      });

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
      });
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

  // 暴露给全局（供 HTML onclick 使用）
  window.handleReset = handleReset;
  window.handleToggle = handleToggle;
  window.handleAddBlacklist = handleAddBlacklist;
  window.handleClear = handleClear;
  window.loadAll = loadAll;

  // 等 bridge 就绪后初始化
  function init() {
    loadAll();
  }
  if (bridge && typeof bridge.ready === "function") {
    bridge.ready().then(init);
  } else {
    window.addEventListener("DOMContentLoaded", init);
  }
})();
