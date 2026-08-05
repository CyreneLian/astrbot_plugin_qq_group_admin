from astrbot.api.message_components import At, Plain
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
    "提供注册给大模型调用的全套 QQ 群管理与互动工具（含禁言、踢人/拉黑、清理潜水人员、撤回、精华、头衔、公告、查询成员资料、戳一戳等 16 大功能），可用自然语言指挥 Bot 进行群管理操作，支持灵活配置管理员权限与普通群友授权功能。",
    "2.1.0",
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

    async def _check_permission(self, event: AstrMessageEvent, tool_name: str = "") -> tuple[bool, str, str, str]:
        """
        通用权限检查辅助函数
        返回: (是否通过, 授权角色描述, 群号, 错误提示文案)
        """
        group_id = event.get_group_id()
        if not group_id:
            return False, "", "", "操作失败：该工具仅支持在 QQ 群聊中使用，当前并非群聊环境。"

        sender_id = str(event.get_sender_id()).strip()

        # 检查是否在允许普通群友调用的工具勾选列表中
        member_allowed_tools = self.config.get("member_allowed_tools", [
            "戳一戳",
            "获取群信息"
        ])
        
        tool_cn_map = {
            "ban_group_member": "禁言/解禁",
            "kick_group_member": "踢人/拉黑",
            "delete_group_message": "撤回消息",
            "set_group_essence_message": "设置/取消精华",
            "get_group_msg_history": "读取历史消息",
            "set_group_whole_ban": "全体禁言",
            "set_group_card": "修改群名片",
            "set_group_special_title": "设置专属头衔",
            "set_group_admin": "设置/取消管理员",
            "set_group_name": "修改群名称",
            "send_group_notice": "发布群公告",
            "get_group_info": "获取群信息",
            "get_group_member_list": "获取全员列表",
            "get_group_member_info": "查询成员信息",
            "kick_inactive_members": "清理潜水成员",
            "group_poke": "戳一戳"
        }
        
        # 兼容匹配：匹配工具中文名或工具英文名
        is_tool_allowed_for_member = False
        if tool_name:
            cn_name = tool_cn_map.get(tool_name, "")
            for allowed in member_allowed_tools:
                if allowed == cn_name or allowed == tool_name or allowed.startswith(cn_name) or allowed.startswith(tool_name):
                    is_tool_allowed_for_member = True
                    break

        if is_tool_allowed_for_member:
            return True, "普通群友(配置已授权工具)", str(group_id), ""

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
            return False, "", "", f"拒绝执行：发送者 ({sender_id}) 不具备操作权限（该功能未开放给普通群友，且未满足管理员权限要求）。"

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

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="ban_group_member")
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

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="kick_group_member")
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
        在 QQ 群聊中撤回单条或多条指定消息。支持直接撤回当前回复/引用的消息，或指定单个/多个 message_id（以英文逗号或空格分隔，如 '12345,67890'）。当且仅当具有管理员权限的用户明确提出撤回要求时调用。

        Args:
            message_id (str, optional): 要撤回的消息 ID。可以为单个 ID，或多个用逗号/空格分隔的 ID（如 '12345,67890'）。若为空，工具将自动从用户当前的引用/回复消息中提取消息 ID。
            reason (str, optional): 撤回消息的原因或说明。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="delete_group_message")
        if not ok:
            return err_msg

        target_msg_id_input = message_id.strip()

        # 尝试从 Reply 提取
        if not target_msg_id_input and hasattr(event, "get_messages"):
            try:
                from astrbot.api.message_components import Reply
                for component in event.get_messages():
                    if isinstance(component, Reply):
                        target_msg_id_input = str(component.id)
                        break
            except Exception as e:
                logger.debug(f"[QQGroupAdmin] 提取 Reply 组件时跳过: {e}")

        if not target_msg_id_input:
            return "操作失败：无法获取要撤回的消息 ID。请回复/引用要撤回的那条消息并让 Bot 撤回，或明确提供消息 ID。"

        # 解析可能存在的多个 ID (支持逗号、空格、分号分隔)
        raw_ids = [i.strip() for i in re.split(r"[,;\s]+", target_msg_id_input) if i.strip()]
        if not raw_ids:
            return "操作失败：未提供有效的消息 ID。"

        success_ids = []
        failed_ids = []
        reason_desc = f"（原因：{reason}）" if reason else ""

        for m_id in raw_ids:
            try:
                await self._call_onebot_action(
                    event,
                    "delete_msg",
                    message_id=int(m_id) if m_id.isdigit() else m_id
                )
                success_ids.append(m_id)
            except Exception as e:
                err_str = str(e)
                logger.error(f"[QQGroupAdmin] 执行 delete_msg (ID: {m_id}) 失败: {err_str}")
                failed_ids.append(m_id)

        if success_ids and not failed_ids:
            if len(success_ids) == 1:
                return f"成功：由 [{auth_role}] 发起，已成功撤回消息 (ID: {success_ids[0]}){reason_desc}。"
            else:
                return f"成功：由 [{auth_role}] 发起，已批量撤回 {len(success_ids)} 条消息 (IDs: {', '.join(success_ids)}){reason_desc}。"
        elif success_ids and failed_ids:
            return f"部分成功：由 [{auth_role}] 发起，已成功撤回 {len(success_ids)} 条消息 ({', '.join(success_ids)})，但有 {len(failed_ids)} 条撤回失败 ({', '.join(failed_ids)}){reason_desc}。"
        else:
            return f"执行失败：Bot 自身在群 {group_id} 中缺乏管理员权限或消息超出 2 分钟撤回时限，无法撤回消息 (IDs: {', '.join(failed_ids)})。"

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
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_essence_message")
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

    @llm_tool(name="get_group_msg_history")
    async def get_group_msg_history(
        self,
        event: AstrMessageEvent,
        message_seq: str = "",
        count: int = 20
    ) -> str:
        """
        在 QQ 群聊中获取历史消息记录列表。当需要查看群内近期发言、寻找特定成员发出的消息 ID（以协助撤回或设为精华）时调用。

        Args:
            message_seq (str, optional): 起始消息序号/消息 ID（一串数字）。若为空，则默认获取最新的群历史消息。
            count (int, optional): 获取的消息数量，范围 1~100，默认 20 条。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="get_group_msg_history")
        if not ok:
            return err_msg

        count = max(1, min(count, 100))
        kwargs = {
            "group_id": int(group_id),
            "count": count
        }
        if message_seq.strip().isdigit():
            kwargs["message_seq"] = int(message_seq.strip())

        try:
            res = await self._call_onebot_action(event, "get_group_msg_history", **kwargs)
            if isinstance(res, dict) and "messages" in res:
                messages = res.get("messages", [])
            elif isinstance(res, list):
                messages = res
            else:
                messages = []

            if not messages:
                return f"成功查询群 {group_id} 的历史记录，但未获取到任何消息。"

            formatted_msgs = []
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("message_id", "")
                sender = msg.get("sender", {})
                user_id = sender.get("user_id", "")
                nickname = sender.get("card") or sender.get("nickname") or str(user_id)
                raw_message = msg.get("raw_message") or str(msg.get("message", ""))
                formatted_msgs.append(f"• [ID: {msg_id}] {nickname}({user_id}): {raw_message}")

            result_text = f"成功获取群 {group_id} 近期 {len(formatted_msgs)} 条历史消息：\n" + "\n".join(formatted_msgs)
            return result_text

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 get_group_msg_history 失败: {err_str}")
            return f"获取群历史消息 API 出错：{err_str}"

    @llm_tool(name="set_group_whole_ban")
    async def set_group_whole_ban(
        self,
        event: AstrMessageEvent,
        enable: bool = True
    ) -> str:
        """
        在 QQ 群聊中开启或关闭全员禁言（全体禁言）。当且仅当具有管理员权限的用户明确提出开启/关闭全体禁言要求时调用。

        Args:
            enable (bool): 是否开启全体禁言。True 代表开启全体禁言，False 代表解除全体禁言。默认 True。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_whole_ban")
        if not ok:
            return err_msg

        action_desc = "开启全体禁言" if enable else "解除全体禁言"

        try:
            await self._call_onebot_action(
                event,
                "set_group_whole_ban",
                group_id=int(group_id),
                enable=enable
            )
            return f"成功：由 [{auth_role}] 发起，已在群 ({group_id}) 中 {action_desc}。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 set_group_whole_ban 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中缺乏管理员或群主权限，无法执行{action_desc}。"
            return f"执行{action_cn if 'action_cn' in locals() else action_desc} API 时出错：{err_str}"

    @llm_tool(name="set_group_card")
    async def set_group_card(
        self,
        event: AstrMessageEvent,
        target_user: str,
        card: str = ""
    ) -> str:
        """
        在 QQ 群聊中设置或修改指定成员的群名片（群昵称）。当且仅当具有管理员权限的用户明确提出修改群名片要求时调用。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
            card (str, optional): 新的群名片文本。若为空字符串则代表清空群名片（恢复原昵称）。
        """
        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_card")
        if not ok:
            return err_msg

        try:
            await self._call_onebot_action(
                event,
                "set_group_card",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                card=card.strip()
            )
            card_desc = f"为 '{card.strip()}'" if card.strip() else "为空（重置群名片）"
            return f"成功：由 [{auth_role}] 发起，已将用户 ({cleaned_target}) 的群名片设置为 {card_desc}。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 set_group_card 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中缺乏管理员权限或目标权限高于 Bot，无法修改用户 ({cleaned_target}) 的群名片。"
            return f"执行设置群名片 API 时出错：{err_str}"

    @llm_tool(name="set_group_special_title")
    async def set_group_special_title(
        self,
        event: AstrMessageEvent,
        target_user: str,
        special_title: str = "",
        duration_days: int = -1
    ) -> str:
        """
        在 QQ 群聊中设置指定成员的头衔（专属头衔）。只有群主具备此权限，或当且仅当具有管理员权限的用户提出设置头衔要求时调用。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
            special_title (str, optional): 专属头衔文本。若为空则代表撤销头衔。
            duration_days (int, optional): 头衔有效期（单位：天）。如果为 -1 代表永久有效。默认 -1。
        """
        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_special_title")
        if not ok:
            return err_msg

        duration_seconds = -1 if duration_days <= 0 else duration_days * 86400

        try:
            await self._call_onebot_action(
                event,
                "set_group_special_title",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                special_title=special_title.strip(),
                duration=duration_seconds
            )
            title_desc = f"'{special_title.strip()}'" if special_title.strip() else "撤销头衔"
            return f"成功：由 [{auth_role}] 发起，已为用户 ({cleaned_target}) 设置专属头衔 {title_desc}。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 set_group_special_title 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中必须为群主身份才可设置专属头衔。"
            return f"执行设置专属头衔 API 时出错：{err_str}"

    @llm_tool(name="set_group_admin")
    async def set_group_admin(
        self,
        event: AstrMessageEvent,
        target_user: str,
        enable: bool = True
    ) -> str:
        """
        在 QQ 群聊中设置或取消某位成员的管理员身份。只有群主具备此权限。当且仅当具有管理员权限的用户明确提出提拔或撤销管理员要求时调用。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
            enable (bool, optional): 是否设置为管理员。True 代表设置为管理员，False 代表取消管理员。默认 True。
        """
        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_admin")
        if not ok:
            return err_msg

        action_desc = "设置为群管理员" if enable else "取消群管理员身份"

        try:
            await self._call_onebot_action(
                event,
                "set_group_admin",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                enable=enable
            )
            return f"成功：由 [{auth_role}] 发起，已将用户 ({cleaned_target}) {action_desc}。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 set_group_admin 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中必须为群主身份才可设置/取消管理员。"
            return f"执行设置管理员 API 时出错：{err_str}"

    @llm_tool(name="set_group_name")
    async def set_group_name(
        self,
        event: AstrMessageEvent,
        group_name: str
    ) -> str:
        """
        在 QQ 群聊中修改群聊名称。当且仅当具有管理员权限的用户明确提出修改群名称要求时调用。

        Args:
            group_name (str): 新的群名称。
        """
        clean_name = group_name.strip()
        if not clean_name:
            return "操作失败：新的群名称不能为空。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_name")
        if not ok:
            return err_msg

        try:
            await self._call_onebot_action(
                event,
                "set_group_name",
                group_id=int(group_id),
                group_name=clean_name
            )
            return f"成功：由 [{auth_role}] 发起，已将当前群聊名称修改为 '{clean_name}'。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 set_group_name 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中缺乏管理员或群主权限，无法修改群名称。"
            return f"执行修改群名称 API 时出错：{err_str}"

    @llm_tool(name="send_group_notice")
    async def send_group_notice(
        self,
        event: AstrMessageEvent,
        content: str
    ) -> str:
        """
        在 QQ 群聊中发布群公告/群通知。当且仅当具有管理员权限的用户明确提出发布群公告要求时调用。

        Args:
            content (str): 要发布的群公告文本内容。
        """
        clean_content = content.strip()
        if not clean_content:
            return "操作失败：群公告内容不能为空。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="send_group_notice")
        if not ok:
            return err_msg

        try:
            # 尝试调用 OneBot v11 拓展接口 _send_group_notice 或 send_group_notice
            try:
                await self._call_onebot_action(
                    event,
                    "_send_group_notice",
                    group_id=int(group_id),
                    content=clean_content
                )
            except Exception:
                await self._call_onebot_action(
                    event,
                    "send_group_notice",
                    group_id=int(group_id),
                    content=clean_content
                )
            return f"成功：由 [{auth_role}] 发起，已在群 ({group_id}) 中发布群公告：'{clean_content}'。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 send_group_notice 失败: {err_str}")
            if "102" in err_str or "权限" in err_str or "Permission" in err_str:
                return f"执行失败：Bot 自身在群 {group_id} 中缺乏管理员或群主权限，无法发布群公告。"
            return f"执行发布群公告 API 时出错：{err_str}"

    @llm_tool(name="get_group_info")
    async def get_group_info(
        self,
        event: AstrMessageEvent
    ) -> str:
        """
        在 QQ 群聊中获取当前群聊的详细信息（群名称、群主 QQ、成员数、最大容量等）。当需要了解群基础信息时调用。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="get_group_info")
        if not ok:
            return err_msg
        try:
            res = await self._call_onebot_action(
                event,
                "get_group_info",
                group_id=int(group_id),
                no_cache=True
            )
            if isinstance(res, dict):
                group_name = res.get("group_name", "未知")
                member_count = res.get("member_count", 0)
                max_member_count = res.get("max_member_count", 0)
                owner_id = res.get("owner_id") or res.get("owner_uin") or "未知"
                return f"群 ({group_id}) 详细信息：\n• 群名称：{group_name}\n• 群主QQ：{owner_id}\n• 当前成员数：{member_count}/{max_member_count}"
            return f"获取群信息成功，但返回数据格式异常：{res}"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 get_group_info 失败: {err_str}")
            return f"获取群详细信息 API 出错：{err_str}"

    @llm_tool(name="get_group_member_list")
    async def get_group_member_list(
        self,
        event: AstrMessageEvent,
        keyword: str = ""
    ) -> str:
        """
        在 QQ 群聊中获取全员列表或根据关键词（昵称/名片/QQ号）搜索群成员。当需要查找某群员信息或统计全员时调用。

        Args:
            keyword (str, optional): 搜索关键词，支持匹配昵称、群名片或QQ号。若为空则默认列出前 30 名成员。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="get_group_member_list")
        if not ok:
            return err_msg
        try:
            members = await self._call_onebot_action(
                event,
                "get_group_member_list",
                group_id=int(group_id),
                no_cache=True
            )
            if not isinstance(members, list):
                return "获取群成员列表失败：返回数据并非有效列表。"

            clean_kw = keyword.strip().lower()
            matched = []

            import datetime
            for m in members:
                if not isinstance(m, dict):
                    continue
                user_id = str(m.get("user_id", ""))
                card = str(m.get("card", ""))
                nickname = str(m.get("nickname", ""))
                role = m.get("role", "member")
                role_cn = "群主" if role == "owner" else ("管理员" if role == "admin" else "成员")
                last_sent_ts = m.get("last_sent_time", 0)
                last_sent_str = datetime.datetime.fromtimestamp(last_sent_ts).strftime("%m-%d %H:%M") if last_sent_ts else "从未/未知"

                item_str = f"• [{role_cn}] {card or nickname}({user_id}) | 最近发言: {last_sent_str}"

                if clean_kw:
                    if clean_kw in user_id.lower() or clean_kw in card.lower() or clean_kw in nickname.lower():
                        matched.append(item_str)
                else:
                    matched.append(item_str)

            total_found = len(matched)
            if not matched:
                return f"在群 ({group_id}) 中未找到匹配 '{keyword}' 的群成员。"

            # 限制返回文本避免超出上下文
            display_list = matched[:30]
            suffix = f"\n... 等共 {total_found} 人" if total_found > 30 else ""
            kw_desc = f"（关键词：'{keyword}'）" if keyword else "（展示前30人）"

            return f"群 ({group_id}) 成员列表{kw_desc}：\n" + "\n".join(display_list) + suffix

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 get_group_member_list 失败: {err_str}")
            return f"获取群成员列表 API 出错：{err_str}"

    @llm_tool(name="group_poke")
    async def group_poke(
        self,
        event: AstrMessageEvent,
        target_user: str
    ) -> str:
        """
        在 QQ 群聊中对指定成员执行“戳一戳”（拍一拍/戳一下）操作。当用户要求“戳一下某某”或“拍一拍某某”时调用。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="group_poke")
        if not ok:
            return f"操作失败：{err_msg}"

        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        try:
            try:
                await self._call_onebot_action(
                    event,
                    "group_poke",
                    group_id=int(group_id),
                    user_id=int(cleaned_target)
                )
            except Exception:
                await self._call_onebot_action(
                    event,
                    "send_group_poke",
                    group_id=int(group_id),
                    user_id=int(cleaned_target)
                )
            return f"成功：已在群 ({group_id}) 中对用户 ({cleaned_target}) 执行戳一戳魔法！"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 group_poke 失败: {err_str}")
            return f"执行戳一戳失败：{err_str}"


    @llm_tool(name="get_group_member_info")
    async def get_group_member_info(
        self,
        event: AstrMessageEvent,
        target_user: str
    ) -> str:
        """
        在 QQ 群聊中获取指定群成员的详细个人资料（包含最后发言时间、入群时间、群名片、群头衔、角色身份、禁言状态等）。当需要精准了解或查询某位群成员的详细状态与最近发言时间时调用。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
        """
        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="get_group_member_info")
        if not ok:
            return err_msg

        try:
            res = await self._call_onebot_action(
                event,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                no_cache=True
            )
            if isinstance(res, dict):
                nickname = res.get("nickname", "")
                card = res.get("card", "")
                role = res.get("role", "member")
                role_cn = "群主" if role == "owner" else ("管理员" if role == "admin" else "成员")
                title = res.get("title", "") or "无"
                
                import datetime
                import time
                join_time_ts = res.get("join_time", 0)
                last_sent_ts = res.get("last_sent_time", 0)
                shut_up_ts = res.get("shut_up_timestamp", 0)
                
                join_time_str = datetime.datetime.fromtimestamp(join_time_ts).strftime("%Y-%m-%d %H:%M:%S") if join_time_ts else "未知"
                last_sent_str = datetime.datetime.fromtimestamp(last_sent_ts).strftime("%Y-%m-%d %H:%M:%S") if last_sent_ts else "从未发言或未知"
                
                ban_until_str = "未禁言"
                if shut_up_ts and shut_up_ts > time.time():
                    ban_until_str = datetime.datetime.fromtimestamp(shut_up_ts).strftime("%Y-%m-%d %H:%M:%S")

                info_lines = [
                    f"用户 ({cleaned_target}) 详细群资料：",
                    f"• 昵称/名片：{card or nickname} ({nickname})",
                    f"• 群身份：{role_cn}",
                    f"• 专属头衔：{title}",
                    f"• 入群时间：{join_time_str}",
                    f"• 最近发言时间：{last_sent_str}",
                    f"• 禁言状态：{ban_until_str}"
                ]
                return "\n".join(info_lines)

            return f"获取用户 ({cleaned_target}) 信息成功，但返回数据格式异常。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 get_group_member_info 失败: {err_str}")
            return f"获取群成员详细信息 API 出错：{err_str}"


    @llm_tool(name="kick_inactive_members")
    async def kick_inactive_members(
        self,
        event: AstrMessageEvent,
        days: int = 60,
        confirm: bool = False
    ) -> str:
        """
        在 QQ 群聊中查找或清理长时间未发言的潜水成员。
        当 confirm=False 时为安全预检模式（仅查询并列出名单），当 confirm=True 时执行真正的移出群聊操作。

        Args:
            days (int, optional): 判断潜水的天数阈值，默认 60 天。
            confirm (bool, optional): 是否确认执行真正的清理踢人操作。默认 False（仅查询预览）。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="kick_inactive_members")
        if not ok:
            return err_msg

        days = max(1, days)
        threshold_seconds = days * 86400
        import time
        import datetime
        now_ts = time.time()

        try:
            members = await self._call_onebot_action(
                event,
                "get_group_member_list",
                group_id=int(group_id),
                no_cache=True
            )
            if not isinstance(members, list):
                return "操作失败：无法获取群成员列表数据。"

            inactive_members = []
            for m in members:
                if not isinstance(m, dict):
                    continue
                user_id = str(m.get("user_id", ""))
                role = m.get("role", "member")
                # 跳过群主和管理员，防止误操作
                if role in ["owner", "admin"]:
                    continue

                last_sent_ts = m.get("last_sent_time", 0)
                join_time_ts = m.get("join_time", 0)

                # 考虑未发言或者最后发言时间超过指定天数（且入群时间也已满对应天数）
                time_to_check = last_sent_ts if last_sent_ts > 0 else join_time_ts
                if time_to_check > 0 and (now_ts - time_to_check >= threshold_seconds):
                    card = m.get("card") or m.get("nickname") or user_id
                    last_str = datetime.datetime.fromtimestamp(last_sent_ts).strftime("%Y-%m-%d") if last_sent_ts else "从未发言"
                    inactive_members.append({
                        "user_id": user_id,
                        "name": card,
                        "last_sent_str": last_str
                    })

            if not inactive_members:
                return f"在群 ({group_id}) 中未找到超过 {days} 天未发言的潜水普通成员。"

            total_cnt = len(inactive_members)

            # 如果未确认执行，仅返回预览报告
            if not confirm:
                lines = [f"🔍 潜水成员扫描预览（共找到 {total_cnt} 人超过 {days} 天未发言）："]
                for item in inactive_members[:20]:
                    lines.append(f"• {item['name']}({item['user_id']}) - 最近发言: {item['last_sent_str']}")
                if total_cnt > 20:
                    lines.append(f"... 等共 {total_cnt} 人")
                lines.append(f"\n提示：请确认无误后对 Bot 说明 '确认清理超过 {days} 天未发言的潜水成员' 来执行清理。")
                return "\n".join(lines)

            # 执行实际清理
            success_cnt = 0
            fail_cnt = 0

            for item in inactive_members:
                try:
                    await self._call_onebot_action(
                        event,
                        "set_group_kick",
                        group_id=int(group_id),
                        user_id=int(item["user_id"]),
                        reject_add_request=False
                    )
                    success_cnt += 1
                except Exception as e:
                    logger.error(f"[QQGroupAdmin] 清理潜水成员 {item['user_id']} 失败: {e}")
                    fail_cnt += 1

            return f"🧹 潜水成员清理完成：由 [{auth_role}] 发起，成功移出 {success_cnt} 人，失败 {fail_cnt} 人。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 kick_inactive_members 失败: {err_str}")
            return f"执行潜水清理 API 出错：{err_str}"
