"""
AstrBot QQ群大模型管理工具 - 通用工具函数模块

功能描述：
- 跨版本安全调用 OneBot/aiocqhttp API
- 提供 QQ 号清洗、Bot 身份查询、引用消息 ID 提取、时间格式化等通用辅助函数
- 提供群历史消息、全员列表、潜水成员扫描等格式化与筛选逻辑

作者: 往昔的涟漪
版本: 2.2.0
日期: 2026-08-10
"""

import datetime
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api.event import AstrMessageEvent

from .constants import LOG_PREFIX

logger = logging.getLogger("astrbot")


async def call_onebot_action(
    event: AstrMessageEvent,
    action: str,
    **kwargs: Any
) -> Any:
    """
    跨版本安全调用 OneBot/aiocqhttp API 的辅助函数。

    支持以下调用优先级：
    1. event.bot.call_action(action, **kwargs)
    2. event.bot.api.call_action(action, **kwargs)
    3. event.bot.call_api(action, kwargs) / event.bot.call_api(action, **kwargs)

    Args:
        event: 当前 AstrMessageEvent 事件对象。
        action: OneBot 动作名（如 set_group_ban）。
        **kwargs: 动作参数。

    Returns:
        API 调用返回结果。

    Raises:
        RuntimeError: 当事件未关联 bot 平台实例或不支持 OneBot call_action API 时。
    """
    bot = getattr(event, "bot", None)
    if not bot:
        raise RuntimeError("当前事件未关联 bot 平台实例。")

    # 优先级 1: event.bot.call_action(action, **kwargs)
    if hasattr(bot, "call_action") and callable(bot.call_action):
        return await bot.call_action(action, **kwargs)

    # 优先级 2: event.bot.api.call_action(action, **kwargs)
    api = getattr(bot, "api", None)
    if api and hasattr(api, "call_action") and callable(api.call_action):
        return await api.call_action(action, **kwargs)

    # 优先级 3: event.bot.call_api(action, kwargs)
    if hasattr(bot, "call_api") and callable(bot.call_api):
        try:
            return await bot.call_api(action, kwargs)
        except Exception:
            return await bot.call_api(action, **kwargs)

    raise RuntimeError("当前 Bot 客户端不支持 OneBot call_action API。")


def clean_qq_number(text: Any) -> str:
    """
    从任意输入中提取纯数字 QQ 号。

    Args:
        text: 原始输入（QQ 号、@文本、昵称+QQ号混合等）。

    Returns:
        提取出的纯数字字符串；若无法提取则返回空字符串。
    """
    cleaned = re.sub(r"\D", "", str(text))
    return cleaned


def extract_reply_message_id(event: AstrMessageEvent) -> str:
    """
    从当前事件中提取回复/引用消息的 ID。

    Args:
        event: 当前 AstrMessageEvent 事件对象。

    Returns:
        引用消息的纯数字 ID；若不存在则返回空字符串。
    """
    message_obj = getattr(event, "message_obj", None)
    if not message_obj:
        return ""
    reply = getattr(message_obj, "reply", None)
    if not reply:
        return ""
    reply_id = getattr(reply, "id", None) or getattr(reply, "message_id", None)
    if not reply_id:
        return ""
    return clean_qq_number(reply_id)


async def get_bot_self_id(event: AstrMessageEvent) -> Optional[str]:
    """
    获取 Bot 账号自身的 QQ 号。

    优先读取 event.self_id，缺失时调用 get_login_info 兜底。

    Args:
        event: 当前 AstrMessageEvent 事件对象。

    Returns:
        Bot 自身 QQ 号字符串；获取失败返回 None。
    """
    bot_self_id = getattr(event, "self_id", None)
    if not bot_self_id:
        try:
            login_info = await call_onebot_action(event, "get_login_info")
            if isinstance(login_info, dict):
                bot_self_id = login_info.get("user_id")
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 获取 Bot 自身 QQ 失败: {e}")
    return str(bot_self_id) if bot_self_id else None


async def get_bot_role_in_group(
    event: AstrMessageEvent,
    group_id: str,
    bot_self_id: Optional[str] = None
) -> Optional[str]:
    """
    获取 Bot 账号自身在指定群内的角色（owner/admin/member）。

    Args:
        event: 当前 AstrMessageEvent 事件对象。
        group_id: 目标群号。
        bot_self_id: Bot 自身 QQ 号；为空时自动获取。

    Returns:
        群角色字符串（小写）；获取失败返回 None。
    """
    if not bot_self_id:
        bot_self_id = await get_bot_self_id(event)
    if not bot_self_id:
        return None

    try:
        bot_member_info = await call_onebot_action(
            event,
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(bot_self_id),
            no_cache=True
        )
        if isinstance(bot_member_info, dict):
            return str(bot_member_info.get("role", "member")).lower()
    except Exception as e:
        logger.warning(f"{LOG_PREFIX} 预检 Bot 账号群权限失败: {e}")
    return None


def is_blacklisted_group(config: dict, group_id: Any) -> bool:
    """
    判断指定群是否在插件黑名单列表中。

    Args:
        config: 插件配置字典。
        group_id: 群号。

    Returns:
        在名单中返回 True，否则返回 False。
    """
    if not group_id:
        return False
    blacklisted_groups = [str(g).strip() for g in config.get("blacklisted_groups", []) if g]
    return str(group_id) in blacklisted_groups


def format_timestamp(ts: Any, fmt: str = "%H:%M:%S") -> str:
    """
    将 Unix 时间戳格式化为可读字符串。

    Args:
        ts: Unix 时间戳（0 或空值视为无时间）。
        fmt: 输出格式，默认 "%H:%M:%S"。

    Returns:
        格式化后的时间字符串；无时间时返回 ""。
    """
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime(fmt)
    except (ValueError, OSError, TypeError):
        return ""


def format_group_msg_history(res: Any) -> str:
    """
    将 get_group_msg_history 的 API 返回结果格式化为文本列表。

    Args:
        res: API 返回结果（dict 或 list）。

    Returns:
        格式化后的历史消息文本；无消息时返回 ""。
    """
    messages = []
    if isinstance(res, dict) and "messages" in res:
        messages = res["messages"]
    elif isinstance(res, list):
        messages = res

    if not messages:
        return ""

    formatted_lines = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        mid = m.get("message_id") or m.get("id") or "未知ID"
        sender = m.get("sender", {})
        sender_name = (
            sender.get("card")
            or sender.get("nickname")
            or sender.get("user_id")
            or "未知发送者"
        )
        time_str = format_timestamp(m.get("time", 0), "%H:%M:%S")

        raw_content = m.get("raw_message") or m.get("message") or ""
        if isinstance(raw_content, list):
            raw_content = "".join(
                str(item.get("data", {}).get("text", ""))
                for item in raw_content
                if isinstance(item, dict)
            )

        formatted_lines.append(f"• [{time_str}] [ID: {mid}] {sender_name}: {str(raw_content)[:80]}")

    return "\n".join(formatted_lines)


def format_member_list(
    res: list,
    keyword: str = "",
    sort_by_join_time: bool = False,
    sort_by_last_sent_time: bool = False,
    sort_by_group_level: bool = False,
    sort_oldest_first: bool = False
) -> Tuple[str, int]:
    """
    将 get_group_member_list 的返回结果进行多维排序、关键词筛选与文本格式化。

    Args:
        res: 群成员原始列表。
        keyword: 搜索关键词（昵称/名片/QQ号/身份）。
        sort_by_join_time: 是否按进群时间排序。
        sort_by_last_sent_time: 是否按最近发言时间排序。
        sort_by_group_level: 是否按群等级排序。
        sort_oldest_first: 是否升序排序（从旧到新 / 从低到高）。

    Returns:
        (格式化文本, 匹配总数) 二元组；无匹配时文本为空字符串。
    """
    reverse_order = not sort_oldest_first
    if sort_by_last_sent_time:
        res = sorted(res, key=lambda x: x.get("last_sent_time", 0), reverse=reverse_order)
    elif sort_by_group_level:
        res = sorted(res, key=lambda x: int(x.get("level", 0)), reverse=reverse_order)
    elif sort_by_join_time:
        res = sorted(res, key=lambda x: x.get("join_time", 0), reverse=reverse_order)

    matched = []
    clean_kw = keyword.strip().lower()

    for m in res:
        if not isinstance(m, dict):
            continue
        user_id = str(m.get("user_id", ""))
        nickname = str(m.get("nickname", ""))
        card = str(m.get("card", ""))
        role = str(m.get("role", "member"))
        role_cn = "群主" if role == "owner" else ("管理员" if role == "admin" else "成员")

        last_sent_str = format_timestamp(m.get("last_sent_time", 0), "%m-%d %H:%M") or "从未发言"
        join_time_str = format_timestamp(m.get("join_time", 0), "%Y-%m-%d %H:%M") or "未知"

        display_name = card if card else nickname
        level = m.get("level", 0)
        item_str = (
            f"• {display_name} ({user_id}) [{role_cn}] LV.{level}"
            f" - 最近发言: {last_sent_str} | 入群: {join_time_str}"
        )

        if clean_kw:
            if (
                clean_kw in user_id.lower()
                or clean_kw in card.lower()
                or clean_kw in nickname.lower()
                or clean_kw in role_cn.lower()
                or clean_kw in role.lower()
            ):
                matched.append(item_str)
        else:
            matched.append(item_str)

    return "\n".join(matched[:30]), len(matched)


def scan_inactive_members(
    members: list,
    days: int,
    max_group_level: int = 0
) -> List[Dict[str, Any]]:
    """
    扫描并筛选长时间未发言的潜水普通成员。

    自动跳过群主与管理员；支持按群等级上限门槛二次过滤。

    Args:
        members: 群成员原始列表。
        days: 潜水天数阈值。
        max_group_level: 群等级上限门槛（0 代表不限制）。

    Returns:
        潜水成员信息字典列表，每项包含 user_id/name/level/last_sent_str。
    """
    threshold_seconds = days * 86400
    now_ts = time.time()
    inactive_members = []

    for m in members:
        if not isinstance(m, dict):
            continue
        user_id = str(m.get("user_id", ""))
        role = m.get("role", "member")
        if role in ["owner", "admin"]:
            continue

        level = int(m.get("level", 0))
        if max_group_level > 0 and level >= max_group_level:
            continue

        last_sent_ts = m.get("last_sent_time", 0)
        join_time_ts = m.get("join_time", 0)

        time_to_check = last_sent_ts if last_sent_ts > 0 else join_time_ts
        if time_to_check > 0 and (now_ts - time_to_check >= threshold_seconds):
            card = m.get("card") or m.get("nickname") or user_id
            last_str = (
                datetime.datetime.fromtimestamp(last_sent_ts).strftime("%Y-%m-%d")
                if last_sent_ts
                else "从未发言"
            )
            inactive_members.append({
                "user_id": user_id,
                "name": card,
                "level": level,
                "last_sent_str": last_str
            })

    return inactive_members
