"""
AstrBot QQ群大模型管理工具 - 权限校验模块

功能描述：
- 通用权限检查：群聊环境校验、黑名单拦截、普通群友工具授权匹配
- 发送者身份校验：Bot 超级管理员 / 群主 / 群管理员
- Bot 账号自身群内身份预检：管理工具要求管理员/群主身份，防止静默假成功

作者: 往昔的涟漪
版本: 2.2.0
日期: 2026-08-10
"""

from typing import Dict, Optional, Tuple

from astrbot.api.event import AstrMessageEvent

from .constants import (
    ADMIN_REQUIRED_TOOLS,
    DEFAULT_MEMBER_ALLOWED_TOOLS,
    LOG_PREFIX,
    OWNER_REQUIRED_TOOLS,
    TOOL_CN_MAP,
)
from .utils import call_onebot_action, is_blacklisted_group

from astrbot.api import logger


async def check_permission(
    event: AstrMessageEvent,
    config: dict,
    admins_id: list,
    tool_name: str = ""
) -> Tuple[bool, str, str, str]:
    """
    通用权限检查辅助函数。

    校验链路：
    1. 必须为 QQ 群聊环境
    2. 群黑名单拦截
    3. 普通群友工具授权匹配（member_allowed_tools 勾选即放行）
    4. 发送者身份校验（Bot 超级管理员 / 群主 / 群管理员）
    5. Bot 账号自身群内身份预检（针对需要群管/群主身份的工具）

    Args:
        event: 当前 AstrMessageEvent 事件对象。
        config: 插件配置字典。
        admins_id: AstrBot 全局配置中的超级管理员 ID 列表（字符串）。
        tool_name: 待校验的工具英文名。

    Returns:
        (是否通过, 授权角色描述, 群号, 错误提示文案) 四元组。
    """
    group_id = event.get_group_id()
    if not group_id:
        return False, "", "", "操作失败：该工具仅支持在 QQ 群聊中使用，当前并非群聊环境。"

    # 0. 检查群黑名单
    if is_blacklisted_group(config, group_id):
        return False, "", str(group_id), f"拒绝执行：当前群聊 ({group_id}) 已被管理员列入黑名单，插件功能已被禁用。"

    sender_id = str(event.get_sender_id()).strip()

    # 检查是否在允许普通群友调用的工具勾选列表中
    member_allowed_tools = config.get("member_allowed_tools", DEFAULT_MEMBER_ALLOWED_TOOLS)

    # 兼容匹配：匹配工具中文名或工具英文名
    is_tool_allowed_for_member = False
    if tool_name:
        cn_name = TOOL_CN_MAP.get(tool_name, "")
        for allowed in member_allowed_tools:
            if (
                allowed == cn_name
                or allowed == tool_name
                or allowed.startswith(cn_name)
                or allowed.startswith(tool_name)
            ):
                is_tool_allowed_for_member = True
                break

    if is_tool_allowed_for_member:
        return True, "普通群友(配置已授权工具)", str(group_id), ""

    allow_bot_admin = config.get("allow_bot_admin", True)
    allow_group_owner = config.get("allow_group_owner", True)
    allow_group_admin = config.get("allow_group_admin", True)

    is_authorized = False
    auth_role = ""

    # A. 检查 Bot 超级管理员
    if allow_bot_admin and sender_id in admins_id:
        is_authorized = True
        auth_role = "Bot 超级管理员"

    # B. 检查群主 / 群管理员
    if not is_authorized and (allow_group_owner or allow_group_admin):
        try:
            member_info = await call_onebot_action(
                event,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(sender_id),
                no_cache=True
            )
            if isinstance(member_info, dict):
                role = member_info.get("role", "")
                if role == "owner" and allow_group_owner:
                    is_authorized = True
                    auth_role = "群主"
                elif role == "admin" and allow_group_admin:
                    is_authorized = True
                    auth_role = "群管理员"
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 查询发送者 {sender_id} 权限失败: {e}")

    if not is_authorized:
        return False, "", "", f"拒绝执行：发送者 ({sender_id}) 不具备操作权限（该功能未开放给普通群友，且未满足管理员权限要求）。"

    # C. 校验 Bot 账号自身在群内的身份权限（针对需要群管/群主身份的工具）
    if tool_name in ADMIN_REQUIRED_TOOLS or tool_name in OWNER_REQUIRED_TOOLS:
        bot_role = await _get_bot_role(event, group_id)
        if bot_role:
            tool_cn = ADMIN_REQUIRED_TOOLS.get(tool_name) or OWNER_REQUIRED_TOOLS.get(tool_name)

            if tool_name in OWNER_REQUIRED_TOOLS:
                if bot_role != "owner":
                    return False, "", str(group_id), (
                        f"操作失败：Bot 账号自身在群 ({group_id}) 内缺乏【群主】身份"
                        f"（当前角色为 {bot_role}）。【{tool_cn}】要求 Bot 账号自身必须具备群主身份。"
                    )
            elif tool_name in ADMIN_REQUIRED_TOOLS:
                if bot_role not in {"owner", "admin"}:
                    return False, "", str(group_id), (
                        f"操作失败：Bot 账号自身在群 ({group_id}) 内缺乏管理员或群主权限"
                        f"（当前角色为普通成员）。【{tool_cn}】要求 Bot 账号必须具备群管理员或群主身份。"
                    )

    return True, auth_role, str(group_id), ""


async def _get_bot_role(event: AstrMessageEvent, group_id: str) -> Optional[str]:
    """
    获取 Bot 账号自身在群内的角色（小写）。

    Args:
        event: 当前 AstrMessageEvent 事件对象。
        group_id: 目标群号。

    Returns:
        群角色字符串；获取失败返回 None。
    """
    from .utils import get_bot_self_id, get_bot_role_in_group

    bot_self_id = getattr(event, "self_id", None)
    if not bot_self_id:
        bot_self_id = await get_bot_self_id(event)
    if not bot_self_id:
        return None
    return await get_bot_role_in_group(event, group_id, bot_self_id=bot_self_id)
