import re
import logging
from typing import Any
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent
from astrbot.api.event.filter import llm_tool

logger = logging.getLogger("astrbot")

@register(
    "astrbot_plugin_qq_group_admin",
    "往昔的涟漪",
    "提供注册给大模型调用的 QQ 群禁言、踢人、拉黑、撤回消息与设置精华消息工具，可用自然语言要求bot进行群管理操作，支持灵活配置 Bot 管理员、群主和群管理员权限。",
    "1.3.0",
    ""
)
class QQGroupAdminPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        # 从 context 获取 Bot 全局配置中的 admins_id 列表
        try:
            raw_admins = context.get_config().get("admins_id", [])
            self.admins_id = [str(a).strip() for a in raw_admins if a]
        except Exception as e:
            logger.warning(f"[QQGroupAdmin] 获取 admins_id 失败: {e}")
            self.admins_id = []

    async def _call_onebot_action(self, event: AstrMessageEvent, action: str, **kwargs) -> Any:
        """
        跨版本安全调用 OneBot/aiocqhttp API 的辅助函数
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

    async def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str, str, str]:
        """
        通用权限检查辅助函数
        返回: (是否通过, 授权角色描述, 群号, 错误提示文案)
        """
        group_id = event.get_group_id()
        if not group_id:
            return False, "", "", "操作失败：该工具仅支持在 QQ 群聊中使用，当前并非群聊环境。"

        sender_id = str(event.get_sender_id()).strip()

        allow_bot_admin = self.config.get("allow_bot_admin", True)
        allow_group_owner = self.config.get("allow_group_owner", True)
        allow_group_admin = self.config.get("allow_group_admin", True)

        is_authorized = False
        auth_role = ""

        # A. 检查 Bot 超级管理员
        if allow_bot_admin and sender_id in self.admins_id:
            is_authorized = True
            auth_role = "Bot 超级管理员"

        # B. 检查群主 / 群管理员
        if not is_authorized and (allow_group_owner or allow_group_admin):
            try:
                member_info = await self._call_onebot_action(
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
                logger.error(f"[QQGroupAdmin] 查询发送者 {sender_id} 权限失败: {e}")

        if not is_authorized:
            return False, "", "", f"拒绝执行：发送者 ({sender_id}) 不具备操作权限（未满足 Bot管理员/群主/群管理员 权限要求）。"

        return True, auth_role, str(group_id), ""

    @llm_tool(name="ban_group_member")
    async def ban_group_member(
        self,
        event: AstrMessageEvent,
        target_user: str,
        duration_minutes: int,
        reason: str = ""
    ) -> str:
        """
        在 QQ 群聊中对违规或指定的群成员执行禁言或解除禁言操作。当且仅当具有管理员权限的用户明确提出禁言或解禁要求时调用。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
            duration_minutes (int): 禁言时长（单位：分钟）。如果为 0 则代表解除禁言。
            reason (str, optional): 禁言或解禁的原因或说明。
        """
        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event)
        if not ok:
            return err_msg

        duration_seconds = max(0, duration_minutes * 60)

        try:
            await self._call_onebot_action(
                event,
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                duration=duration_seconds
            )
            
            action_desc = "解除禁言" if duration_seconds == 0 else f"禁言 {duration_minutes} 分钟"
            reason_desc = f"（原因：{reason}）" if reason else ""
            return f"成功：由 [{auth_role}] 发起，已对用户 ({cleaned_target}) 执行 {action_desc}{reason_desc}。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 set_group_ban 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中缺乏管理员或群主权限，无法对用户 ({cleaned_target}) 执行禁言。"
            return f"执行禁言 API 时出错：{err_str}"

    @llm_tool(name="kick_group_member")
    async def kick_group_member(
        self,
        event: AstrMessageEvent,
        target_user: str,
        reject_add_request: bool = False,
        reason: str = ""
    ) -> str:
        """
        在 QQ 群聊中将指定成员移除群聊（踢出群聊）。当且仅当具有管理员权限的用户明确提出踢人要求时调用。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
            reject_add_request (bool, optional): 是否拒绝此人后续的加群申请（黑名单/不再接收此人加群）。默认 false。
            reason (str, optional): 踢出群聊的原因或说明。
        """
        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event)
        if not ok:
            return err_msg

        try:
            await self._call_onebot_action(
                event,
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                reject_add_request=reject_add_request
            )
            
            reject_desc = "（并拒绝后续加群申请）" if reject_add_request else ""
            reason_desc = f"（原因：{reason}）" if reason else ""
            return f"成功：由 [{auth_role}] 发起，已将用户 ({cleaned_target}) 移除群聊{reject_desc}{reason_desc}。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 set_group_kick 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中缺乏管理员或群主权限，无法对用户 ({cleaned_target}) 执行踢人。"
            return f"执行踢人 API 时出错：{err_str}"

    @llm_tool(name="delete_group_message")
    async def delete_group_message(
        self,
        event: AstrMessageEvent,
        message_id: str = "",
        reason: str = ""
    ) -> str:
        """
        在 QQ 群聊中撤回某条消息。支持直接撤回当前回复/引用的消息，或撤回指定 message_id 的消息。当且仅当具有管理员权限的用户明确提出撤回要求时调用。

        Args:
            message_id (str, optional): 要撤回的消息 ID（一串数字）。若为空，工具将自动从用户当前的引用/回复消息中提取消息 ID。
            reason (str, optional): 撤回消息的原因或说明。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event)
        if not ok:
            return err_msg

        target_msg_id = message_id.strip()

        if not target_msg_id and hasattr(event, "get_messages"):
            try:
                from astrbot.api.message_components import Reply
                for component in event.get_messages():
                    if isinstance(component, Reply):
                        target_msg_id = str(component.id)
                        break
            except Exception as e:
                logger.debug(f"[QQGroupAdmin] 提取 Reply 组件时跳过: {e}")

        if not target_msg_id:
            return "操作失败：无法获取要撤回的消息 ID。请回复/引用要撤回的那条消息并让 Bot 撤回，或明确提供消息 ID。"

        try:
            await self._call_onebot_action(
                event,
                "delete_msg",
                message_id=int(target_msg_id) if target_msg_id.isdigit() else target_msg_id
            )
            
            reason_desc = f"（原因：{reason}）" if reason else ""
            return f"成功：由 [{auth_role}] 发起，已成功撤回消息 (ID: {target_msg_id}){reason_desc}。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 delete_msg 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中缺乏管理员权限或消息超出可撤回时限，无法撤回消息 (ID: {target_msg_id})。"
            return f"执行撤回消息 API 时出错：{err_str}"

    @llm_tool(name="set_group_essence_message")
    async def set_group_essence_message(
        self,
        event: AstrMessageEvent,
        message_id: str = "",
        delete_essence: bool = False
    ) -> str:
        """
        在 QQ 群聊中将某条消息设置为群精华消息或移除群精华消息。支持直接设置/移除当前回复引用的消息，或指定 message_id 的消息。当且仅当具有管理员权限的用户明确提出设为精华或取消精华要求时调用。

        Args:
            message_id (str, optional): 要设置或移除精华的消息 ID（一串数字）。若为空，工具将自动从用户当前的引用/回复消息中提取消息 ID。
            delete_essence (bool, optional): 是否移除精华消息（True 代表移除精华，False 代表添加精华）。默认 False。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event)
        if not ok:
            return err_msg

        target_msg_id = message_id.strip()

        if not target_msg_id and hasattr(event, "get_messages"):
            try:
                from astrbot.api.message_components import Reply
                for component in event.get_messages():
                    if isinstance(component, Reply):
                        target_msg_id = str(component.id)
                        break
            except Exception as e:
                logger.debug(f"[QQGroupAdmin] 提取 Reply 组件时跳过: {e}")

        if not target_msg_id:
            return "操作失败：无法获取目标消息 ID。请回复/引用要设置或取消精华的那条消息并对 Bot 说明，或明确提供消息 ID。"

        action_name = "delete_essence_msg" if delete_essence else "set_essence_msg"
        action_cn = "取消群精华消息" if delete_essence else "设置群精华消息"

        try:
            await self._call_onebot_action(
                event,
                action_name,
                message_id=int(target_msg_id) if target_msg_id.isdigit() else target_msg_id
            )
            return f"成功：由 [{auth_role}] 发起，已将消息 (ID: {target_msg_id}) {action_cn}。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 {action_name} 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中缺乏管理员或群主权限，无法对消息 (ID: {target_msg_id}) 执行{action_cn}。"
            return f"执行{action_cn} API 时出错：{err_str}"
