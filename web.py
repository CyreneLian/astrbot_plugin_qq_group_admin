"""入群审核管理面板 Web API 控制器。

提供入群人机验证数据（失败次数、黑名单状态）的查看与管理接口，
供插件页面（pages/join_verify）调用。
"""

import asyncio
import json
import os
from typing import Any, Callable, Awaitable

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

try:
    from quart import jsonify as quart_jsonify
    from quart import request as quart_request_obj
except ImportError:
    quart_jsonify = None
    quart_request_obj = None

PLUGIN_NAME = "astrbot_plugin_qq_group_admin"
PLUGIN_DATA_DIR = "astrbot_plugin_qq_group_admin"
BLACKLIST_FILENAME = "join_verify_blacklist.json"

# 面板可覆盖的每群配置键（与 main.py _effective_group_config 一致）
PER_GROUP_KEYS = (
    "auto_accept_group_request", "auto_reject_below_level",
    "auto_accept_group_whitelist", "auto_reject_whitelist_miss",
    "auto_accept_group_level", "auto_accept_whitelist",
    "auto_accept_dual_verify", "auto_reject_dual_verify",
    "enable_join_verify", "join_verify_timeout",
    "join_verify_max_attempts", "join_verify_max_failures",
    "join_verify_welcome_msg",
)
# 需要 Bot 群管理权限才能生效的键（欢迎词仅是发消息，无需权限）
PER_GROUP_PERM_KEYS = set(PER_GROUP_KEYS) - {"join_verify_welcome_msg"}


class JoinVerifyWebController:
    """入群审核管理面板控制器。"""

    def __init__(self, context: Context, config: dict):
        self.context = context
        self.config = config or {}
        # 后台任务集合：拉黑即踢等异步任务引用（防止被 GC 回收，完成自动移除）
        self._bg_tasks = set()

    @property
    def data_file(self) -> str:
        return os.path.join(
            get_astrbot_plugin_data_path(), PLUGIN_DATA_DIR, BLACKLIST_FILENAME
        )

    def _load_blacklist(self) -> dict:
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 加载入群黑名单失败: {e}")
        return {}

    def _save_blacklist(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 保存入群黑名单失败: {e}")

    def _max_failures(self) -> int:
        try:
            return int(self.config.get("join_verify_max_failures", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _per_group_config_file(self) -> str:
        return os.path.join(
            get_astrbot_plugin_data_path(), PLUGIN_DATA_DIR, "per_group_config.json"
        )

    def _load_per_group_config(self) -> dict:
        try:
            f = self._per_group_config_file()
            if os.path.exists(f):
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.loads(fh.read())
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 读取每群覆盖配置失败: {e}")
        return {}

    def _save_per_group_config(self, data: dict) -> None:
        try:
            f = self._per_group_config_file()
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 保存每群覆盖配置失败: {e}")

    def _effective_group_config(self, group_id: Any) -> dict:
        """合并全局默认 + 每群覆盖（插件配置面板 = 默认值层）"""
        override = self._load_per_group_config().get(str(group_id), {}) or {}
        eff = {}
        for key in PER_GROUP_KEYS:
            eff[key] = override.get(key) if key in override else self.config.get(key, None)
        return eff

    @staticmethod
    def _value_same(a, b) -> bool:
        """宽容比较两个配置值是否相同（处理 checkbox bool / 数字字符串 / list 白词等类型差异）"""
        if isinstance(b, bool):
            try:
                return bool(a) == b
            except Exception:
                return False
        if isinstance(b, (int, float)) and not isinstance(b, bool):
            try:
                return float(a) == float(b)
            except (TypeError, ValueError):
                return False
        if isinstance(b, list):
            if isinstance(a, list):
                return a == b
            return str(a or "").strip() == ",".join(str(x).strip() for x in b).strip()
        return str(a or "").strip() == str(b or "").strip()

    async def _resolve_bot_role(self, group_id: Any) -> str:
        """查询 Bot 在指定群的角色（owner/admin/member）；失败返回空串"""
        try:
            pm = getattr(self.context, "platform_manager", None)
            platforms = getattr(pm, "platform_insts", None) or []
            for platform in platforms:
                try:
                    client = platform.get_client() if hasattr(platform, "get_client") else None
                    if not client:
                        continue
                    login = await self._client_call_action(client, "get_login_info")
                    bot_self_id = login.get("user_id") if isinstance(login, dict) else None
                    if not bot_self_id:
                        continue
                    info = await self._client_call_action(
                        client, "get_group_member_info",
                        group_id=int(group_id), user_id=int(bot_self_id), no_cache=True,
                    )
                    if isinstance(info, dict):
                        return str(info.get("role", "member")).lower()
                except Exception as e:
                    logger.debug(f"[{PLUGIN_NAME}] 查询角色失败 group={group_id}: {e}")
                    continue
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 查询 Bot 群角色失败: {e}")
        return ""

    @staticmethod
    def _check_quart_available() -> None:
        if quart_jsonify is None or quart_request_obj is None:
            raise RuntimeError("Web framework is unavailable")

    @staticmethod
    def _jsonify(payload: dict[str, Any]):
        JoinVerifyWebController._check_quart_available()
        return quart_jsonify(payload)

    # ---------- 页面接口 ----------

    async def page_overview(self):
        """概览：记录总数、黑名单数量、累计失败次数、配置上限"""
        data = self._load_blacklist()
        total = len(data)
        blacklisted = sum(1 for r in data.values() if r.get("blacklisted"))
        total_failures = sum(int(r.get("failures", 0)) for r in data.values())
        return self._jsonify(
            {
                "ok": True,
                "data": {
                    "total_records": total,
                    "blacklisted_count": blacklisted,
                    "total_failures": total_failures,
                    "max_failures": self._max_failures(),
                },
            }
        )

    async def page_users(self):
        """用户列表：QQ、失败次数、剩余机会、黑名单状态"""
        data = self._load_blacklist()
        max_failures = self._max_failures()
        users = []
        # 排序：已拉黑用户优先置顶，组内再按失败次数从多到少
        for user_id, rec in sorted(
            data.items(),
            key=lambda item: (
                not bool(item[1].get("blacklisted", False)),  # 已拉黑（False）排前
                -int(item[1].get("failures", 0)),             # 组内失败次数降序
            ),
        ):
            failures = int(rec.get("failures", 0))
            blacklisted = bool(rec.get("blacklisted", False))
            if blacklisted:
                # 已拉黑：剩余次数恒为 0
                remaining = 0
            elif max_failures > 0:
                # 优先用独立存储的剩余次数（操作过的用户）；兜底动态计算（兼容旧数据）
                remaining = int(rec.get("remaining", max(1, max_failures - failures)))
            else:
                # 未设上限：剩余不限
                remaining = -1
            users.append(
                {
                    "user_id": user_id,
                    "failures": failures,
                    "remaining": remaining,
                    "blacklisted": blacklisted,
                }
            )
        return self._jsonify({"ok": True, "data": users})

    async def page_remove(self):
        """解除指定用户的入群黑名单"""
        payload = await quart_request_obj.get_json(force=True, silent=True) or {}
        user_id = str(payload.get("user_id", "")).strip()
        if not user_id:
            return self._jsonify({"ok": False, "message": "缺少 user_id 参数"})
        data = self._load_blacklist()
        if user_id in data:
            data[user_id]["blacklisted"] = False
            self._save_blacklist(data)
            return self._jsonify(
                {"ok": True, "message": f"已解除用户 {user_id} 的入群黑名单"}
            )
        return self._jsonify({"ok": False, "message": "该用户不在黑名单记录中"})

    async def page_reset(self):
        """重置指定用户的入群验证记录（清零失败次数并解除黑名单）"""
        payload = await quart_request_obj.get_json(force=True, silent=True) or {}
        user_id = str(payload.get("user_id", "")).strip()
        if not user_id:
            return self._jsonify({"ok": False, "message": "缺少 user_id 参数"})
        data = self._load_blacklist()
        if user_id in data:
            del data[user_id]
            self._save_blacklist(data)
            return self._jsonify(
                {"ok": True, "message": f"已重置用户 {user_id} 的入群验证记录"}
            )
        return self._jsonify({"ok": False, "message": "该用户没有记录"})

    async def page_toggle_blacklist(self):
        """拉黑/解除切换：
        - 未拉黑用户：拉入黑名单并将失败次数清零
        - 已拉黑用户：解除黑名单并将失败次数 +1
        """
        payload = await quart_request_obj.get_json(force=True, silent=True) or {}
        user_id = str(payload.get("user_id", "")).strip()
        if not user_id:
            return self._jsonify({"ok": False, "message": "缺少 user_id 参数"})
        data = self._load_blacklist()
        rec = data.setdefault(user_id, {"failures": 0, "blacklisted": False})
        if rec.get("blacklisted"):
            # 已拉黑 → 解除：剩余 1 次（设上限）或不限（未设上限）；失败次数保持不动
            rec["blacklisted"] = False
            if self._max_failures() > 0:
                rec["remaining"] = 1
                message = f"已解除用户 {user_id} 的入群黑名单（剩余 1 次机会）"
            else:
                rec["remaining"] = -1
                message = f"已解除用户 {user_id} 的入群黑名单（不限次数）"
        else:
            # 未拉黑 → 拉黑：剩余次数清零；失败次数保持不动
            rec["blacklisted"] = True
            rec["remaining"] = 0
            message = f"已将用户 {user_id} 拉入入群黑名单（剩余次数已清零）"
            # 拉黑即踢：后台异步执行（不阻塞接口返回，避免面板长时间等待不刷新）
            self._spawn_background(self._kick_user_from_all_groups(user_id))
        self._save_blacklist(data)
        return self._jsonify({"ok": True, "message": message})

    async def _client_call_action(self, client, action: str, **kwargs):
        """跨平台客户端 API 调用（兼容 call_action / api.call_action / call_api）"""
        if hasattr(client, "call_action") and callable(client.call_action):
            return await client.call_action(action, **kwargs)
        api = getattr(client, "api", None)
        if api and hasattr(api, "call_action") and callable(api.call_action):
            return await api.call_action(action, **kwargs)
        if hasattr(client, "call_api") and callable(client.call_api):
            try:
                return await client.call_api(action, kwargs)
            except Exception:
                return await client.call_api(action, **kwargs)
        raise RuntimeError("当前 Bot 客户端不支持 OneBot call_action API")

    async def _kick_one_group(self, client, gid, user_id: str, bot_self_id) -> str:
        """对单个群执行拉黑即踢：权限验证 → 探测目标 → @提示 → 10秒后移出。
        返回状态：kicked / absent（不在群） / skipped（无权限）/ failed（踢出失败）
        """
        # 群管理权限验证：Bot 非群主/管理员 → 跳过（权限查询失败时仍尝试）
        if bot_self_id:
            try:
                bot_info = await self._client_call_action(
                    client, "get_group_member_info",
                    group_id=int(gid), user_id=int(bot_self_id),
                )
                role = bot_info.get("role") if isinstance(bot_info, dict) else None
                if role not in ("owner", "admin"):
                    logger.info(f"[{PLUGIN_NAME}] 拉黑即踢：群 {gid} 无管理权限，跳过")
                    return "skipped"
            except Exception:
                # 权限查询失败：不跳过，仍继续尝试（尽力而为）
                logger.info(f"[{PLUGIN_NAME}] 拉黑即踢：群 {gid} 权限查询失败，仍尝试处理")
        # 探测用户是否在该群（能查到成员信息 = 在群内）
        try:
            await self._client_call_action(
                client, "get_group_member_info",
                group_id=int(gid), user_id=int(user_id),
            )
        except Exception:
            return "absent"  # 不在该群
        # 在群内 → 先 @提示，10 秒后移出并拉入该群QQ侧黑名单
        try:
            await self._client_call_action(
                client, "send_group_msg",
                group_id=int(gid),
                message=[
                    {"type": "at", "data": {"qq": int(user_id)}},
                    {"type": "text", "data": {"text": " 很抱歉，你已被拉入入群黑名单，你将在10秒后被移除群聊！"}},
                ],
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 拉黑即踢：向群 {gid} 发送提示失败: {e}")
        await asyncio.sleep(10)
        try:
            await self._client_call_action(
                client, "set_group_kick",
                group_id=int(gid), user_id=int(user_id),
                reject_add_request=True,
            )
            logger.info(f"[{PLUGIN_NAME}] 拉黑即踢：用户 {user_id} 已从群 {gid} 移出")
            return "kicked"
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 踢出用户 {user_id} 失败（群 {gid}）: {e}")
            return "failed"

    def _spawn_background(self, coro):
        """将协程转为后台任务，不阻塞当前接口；任务完成自动清理引用。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def _kick_user_from_all_groups(self, user_id: str) -> dict:
        """拉黑即踢：尝试将该用户从所有平台的所有群中移出（reject_add_request=True 顺带拉入该群QQ黑名单）。
        每个群并发处理：权限验证 → 探测 → @提示 → 10秒后移出。
        仅尽力而为：任何平台/群/API 异常都不影响拉黑结果本身。

        Returns:
            {"kicked": [已成功移出的群id], "failed": [踢出失败的群id], "skipped": 无权限跳过的群数, "checked": 检查过的群数}
        """
        result = {"kicked": [], "failed": [], "skipped": 0, "checked": 0}
        try:
            platform_manager = getattr(self.context, "platform_manager", None)
            if not platform_manager:
                return result
            platforms = getattr(platform_manager, "platform_insts", None) or []
            tasks = []
            task_meta = []  # 与 tasks 对应的 (platform, gid)
            for platform in platforms:
                try:
                    client = platform.get_client() if hasattr(platform, "get_client") else None
                    if not client:
                        continue
                    # 获取 Bot 自身 QQ 号（用于群管理权限验证）
                    bot_self_id = None
                    try:
                        login_info = await self._client_call_action(client, "get_login_info")
                        bot_self_id = login_info.get("user_id") if isinstance(login_info, dict) else None
                    except Exception:
                        bot_self_id = getattr(platform, "client_self_id", None)
                    group_list = await self._client_call_action(client, "get_group_list")
                    if not isinstance(group_list, list):
                        continue
                    for g in group_list:
                        gid = g.get("group_id") if isinstance(g, dict) else None
                        if not gid:
                            continue
                        result["checked"] += 1
                        tasks.append(self._kick_one_group(client, gid, user_id, bot_self_id))
                        task_meta.append(gid)
                except Exception as e:
                    logger.error(f"[{PLUGIN_NAME}] 拉黑即踢平台处理失败: {e}")
            # 并发执行所有群的提示+10秒延迟+踢出（避免 N 个群串行等待）
            if tasks:
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                for i, outcome in enumerate(outcomes):
                    gid = task_meta[i] if i < len(task_meta) else None
                    if isinstance(outcome, Exception):
                        result["failed"].append(gid)
                    elif outcome == "kicked":
                        result["kicked"].append(gid)
                    elif outcome == "failed":
                        result["failed"].append(gid)
                    elif outcome == "skipped":
                        result["skipped"] += 1
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 拉黑即踢失败: {e}")
        return result

    async def page_add_blacklist(self):
        """手动将指定 QQ 号拉入入群黑名单"""
        payload = await quart_request_obj.get_json(force=True, silent=True) or {}
        user_id = str(payload.get("user_id", "")).strip()
        if not user_id:
            return self._jsonify({"ok": False, "message": "缺少 user_id 参数"})
        if not user_id.isdigit():
            return self._jsonify({"ok": False, "message": "QQ号格式不正确，请输入纯数字"})
        data = self._load_blacklist()
        rec = data.setdefault(user_id, {"failures": 0, "blacklisted": False})
        rec["blacklisted"] = True
        rec["remaining"] = 0
        self._save_blacklist(data)
        # 拉黑即踢：后台异步执行（不阻塞接口返回，避免面板长时间等待不刷新）
        self._spawn_background(self._kick_user_from_all_groups(user_id))
        return self._jsonify({"ok": True, "message": f"已将用户 {user_id} 拉入入群黑名单"})

    async def page_clear(self):
        """清空全部入群黑名单记录"""
        self._save_blacklist({})
        return self._jsonify({"ok": True, "message": "已清空全部入群黑名单记录"})

    # ---------- 路由注册 ----------

    async def page_defaults(self):
        """插件默认值（入群工具管理面板顶部展示）"""
        cfg = self.config
        def g(k, d=None):
            try:
                return cfg.get(k, d)
            except Exception:
                return d
        return self._jsonify({"ok": True, "defaults": {
            "auto_accept_group_request": g("auto_accept_group_request", False),
            "auto_reject_below_level": g("auto_reject_below_level", False),
            "auto_accept_whitelist": g("auto_accept_whitelist", False),
            "auto_reject_whitelist_miss": g("auto_reject_whitelist_miss", False),
            "auto_accept_group_level": g("auto_accept_group_level", 0),
            "auto_accept_group_whitelist": g("auto_accept_group_whitelist", ""),
            "auto_accept_dual_verify": g("auto_accept_dual_verify", False),
            "auto_reject_dual_verify": g("auto_reject_dual_verify", False),
            "enable_join_verify": g("enable_join_verify", False),
            "join_verify_timeout": g("join_verify_timeout", 180),
            "join_verify_max_attempts": g("join_verify_max_attempts", 3),
            "join_verify_max_failures": g("join_verify_max_failures", 0),
            "join_verify_welcome_msg": g("join_verify_welcome_msg", ""),
        }})

    async def page_groups(self):
        """群列表：群号、群名、Bot 角色、当前生效配置与覆盖标记"""
        groups = []
        try:
            pm = getattr(self.context, "platform_manager", None)
            platforms = getattr(pm, "platform_insts", None) or []
            per_group = self._load_per_group_config()
            for platform in platforms:
                try:
                    client = platform.get_client() if hasattr(platform, "get_client") else None
                    if not client:
                        continue
                    login = await self._client_call_action(client, "get_login_info")
                    bot_self_id = login.get("user_id") if isinstance(login, dict) else None
                    group_list = await self._client_call_action(client, "get_group_list")
                    if not isinstance(group_list, list):
                        continue
                    for g in group_list:
                        gid = str(g.get("group_id", "")) if isinstance(g, dict) else ""
                        if not gid:
                            continue
                        role = ""
                        if bot_self_id:
                            try:
                                info = await self._client_call_action(
                                    client, "get_group_member_info",
                                    group_id=int(gid), user_id=int(bot_self_id), no_cache=True,
                                )
                                if isinstance(info, dict):
                                    role = str(info.get("role", "member")).lower()
                            except Exception:
                                role = ""
                        eff = self._effective_group_config(gid)
                        groups.append({
                            "group_id": gid,
                            "group_name": g.get("group_name", "") or "",
                            "bot_role": role,
                            "config": eff,
                            "overridden": gid in per_group,
                        })
                except Exception as e:
                    logger.warning(f"[{PLUGIN_NAME}] 获取群列表失败: {e}")
                    continue
        except Exception as e:
            return self._jsonify({"ok": False, "message": f"获取群列表失败: {e}"})
        return self._jsonify({"ok": True, "data": groups})

    async def page_group_config_set(self):
        """保存某群覆盖配置：{group_id, key, value}。需权限的键在 Bot 无管理权限时拒绝。"""
        payload = await quart_request_obj.get_json(force=True, silent=True) or {}
        group_id = str(payload.get("group_id", "")).strip()
        key = str(payload.get("key", "")).strip()
        if not group_id:
            return self._jsonify({"ok": False, "message": "缺少 group_id 参数"})
        if key not in PER_GROUP_KEYS:
            return self._jsonify({"ok": False, "message": f"不允许覆盖的配置项: {key}"})
        # 权限校验：需要管理权限的键，Bot 非群主/管理员时拒绝
        if key in PER_GROUP_PERM_KEYS:
            role = await self._resolve_bot_role(group_id)
            if role not in ("owner", "admin", "administrator"):
                return self._jsonify({
                    "ok": False,
                    "code": "NO_PERMISSION",
                    "message": "Bot 在该群无管理员/群主权限，无法修改该配置项",
                })
        data = self._load_per_group_config()
        record = data.setdefault(group_id, {})
        value = payload.get("value")
        default_val = self.config.get(key) if hasattr(self.config, "get") else None
        # 空值或与插件默认一致 → 取消该键覆盖（跟随全局默认，不残留「已覆盖」）
        if value is None or value == "" or (default_val is not None and self._value_same(value, default_val)):
            record.pop(key, None)
        else:
            record[key] = value
        if not record:
            data.pop(group_id, None)
        self._save_per_group_config(data)
        still_overridden = group_id in data and bool(data.get(group_id))
        return self._jsonify({"ok": True, "message": "已保存", "overridden": still_overridden})

    async def page_group_config_reset(self):
        """重置某群覆盖（删除全部或指定键）：{group_id, key?}"""
        payload = await quart_request_obj.get_json(force=True, silent=True) or {}
        group_id = str(payload.get("group_id", "")).strip()
        key = str(payload.get("key", "")).strip()
        if not group_id:
            return self._jsonify({"ok": False, "message": "缺少 group_id 参数"})
        data = self._load_per_group_config()
        if group_id not in data:
            return self._jsonify({"ok": True, "message": "该群无覆盖配置"})
        if key:
            data[group_id].pop(key, None)
            if not data[group_id]:
                data.pop(group_id, None)
        else:
            data.pop(group_id, None)
        self._save_per_group_config(data)
        # 返回重置后的生效配置（= 全局默认值），供前端局部更新卡片
        return self._jsonify({
            "ok": True,
            "message": "已重置为插件默认值",
            "overridden": False,
            "config": self._effective_group_config(group_id),
        })

    def register_routes(self) -> None:
        routes = [
            ("/overview", self.page_overview, ["GET"], "入群审核概览"),
            ("/users", self.page_users, ["GET"], "入群审核用户列表"),
            ("/remove", self.page_remove, ["POST"], "解除用户入群黑名单"),
            ("/reset", self.page_reset, ["POST"], "重置用户入群验证记录"),
            ("/toggle-blacklist", self.page_toggle_blacklist, ["POST"], "拉黑/解除用户入群黑名单"),
            ("/add-blacklist", self.page_add_blacklist, ["POST"], "手动拉黑用户"),
            ("/clear", self.page_clear, ["POST"], "清空入群黑名单"),
        ]
        routes += [
            ("/defaults", self.page_defaults, ["GET"], "入群工具管理·插件默认值"),
            ("/groups", self.page_groups, ["GET"], "入群工具管理·群列表"),
            ("/group-config", self.page_group_config_set, ["POST"], "入群工具管理·保存每群覆盖配置"),
            ("/group-config-reset", self.page_group_config_reset, ["POST"], "入群工具管理·重置每群覆盖配置"),
        ]
        for path, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}{path}",
                self._wrap_handler(handler),
                methods,
                desc,
            )

    def _wrap_handler(
        self, handler: Callable[[], Awaitable]
    ) -> Callable[[], Awaitable]:
        async def wrapped():
            self._check_quart_available()
            try:
                return await handler()
            except ValueError as exc:
                return self._jsonify({"ok": False, "message": str(exc)}), 400
            except Exception as exc:
                logger.exception(f"[{PLUGIN_NAME}] 入群审核面板请求失败")
                return self._jsonify({"ok": False, "message": str(exc)}), 500

        return wrapped
