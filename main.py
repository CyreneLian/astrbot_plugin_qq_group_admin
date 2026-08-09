"""
AstrBot QQ群大模型管理工具 v2.2.0

功能描述：
- 提供注册给大模型调用的全套 QQ 群管理与互动工具（含禁言、踢人/拉黑、清理潜水人员、@、撤回、精华、头衔、公告、戳一戳等 17 大功能）
- 可用自然语言指挥 Bot 进行群管理操作，支持灵活配置管理员权限与普通群友授权功能

作者: 往昔的涟漪
版本: 2.2.0
日期: 2026-08-08
"""

from astrbot.api.message_components import At, Plain, BaseMessageComponent
from astrbot.api.provider import ProviderRequest
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
import re
import logging
from typing import Any, List, Tuple
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import llm_tool

logger = logging.getLogger("astrbot")

@register(
    "astrbot_plugin_qq_group_admin",
    "往昔的涟漪",
    "提供注册给大模型调用的全套 QQ 群管理与互动工具（含禁言、踢人/拉黑、清理潜水人员、@、撤回、精华、头衔、公告、戳一戳等 17 大功能），可用自然语言指挥 Bot 进行群管理操作，支持灵活配置管理员权限与普通群友授权功能。",
    "2.2.0",
    "https://github.com/CyreneLian/astrbot_plugin_qq_group_admin"
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

        # 正则表达式：用于匹配符合规范的艾特标签，例如 [at:123456] 或 [at:all]
        self.valid_at_pattern = re.compile(r"\[at:(\d+|all)\]")

    @filter.on_llm_request()
    async def inject_at_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在 LLM 发出请求前注入艾特功能提示词，告知大模型如何格式化输出 [at:QQ号]。
        当配置中关闭「允许普通 @成员 功能」时，不注入该提示词。
        """
        enable_at_feature = self.config.get("enable_at_feature", True)
        if not enable_at_feature:
            return

        group_id = event.get_group_id()
        if group_id:
            blacklisted_groups = [str(g).strip() for g in self.config.get("blacklisted_groups", []) if g]
            if str(group_id) in blacklisted_groups:
                return

        at_instruction = (
            "\n\n【艾特成员提示】\n"
            "当你想在回复中艾特（提及）某个群成员时，请在回复文本中插入格式为 [at:用户ID] 的标签。\n"
            "例如：你好[at:123456789]，关于你的问题...\n"
            "其中用户ID必须是纯数字，可以先调用 get_group_member_list 工具查询成员 QQ 号。"
        )
        req.system_prompt = (req.system_prompt or "") + at_instruction

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截器：在消息发送给用户前，将 [at:数字] 和 [at:all] 解析为平台原生的 At 组件，并补充防连连看字符。
        """
        if not self.config.get("enable_at_feature", True):
            return

        group_id = event.get_group_id()
        if group_id:
            blacklisted_groups = [str(g).strip() for g in self.config.get("blacklisted_groups", []) if g]
            if str(group_id) in blacklisted_groups:
                return

        result = event.get_result()
        if not result or not result.chain:
            return

        has_tag = False
        for comp in result.chain:
            if isinstance(comp, Plain) and "[at:" in comp.text:
                has_tag = True
                break

        if not has_tag:
            return

        new_chain: List[BaseMessageComponent] = []

        # 第一阶段：正则解析并替换为组件
        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                last_idx = 0

                for match in self.valid_at_pattern.finditer(text):
                    start, end = match.span()
                    if start > last_idx:
                        new_chain.append(Plain(text[last_idx:start]))

                    target_id = match.group(1)
                    if target_id.isdigit():
                        new_chain.append(At(qq=target_id))
                        new_chain.append(Plain(" "))
                    else:
                        new_chain.append(At(qq="all"))
                        new_chain.append(Plain(" "))

                    last_idx = end

                if last_idx < len(text):
                    new_chain.append(Plain(text[last_idx:]))
            else:
                new_chain.append(comp)

        # 第二阶段：空格清理
        idx = 0
        while idx < len(new_chain):
            if isinstance(new_chain[idx], At):
                for prev_idx in range(idx - 1, -1, -1):
                    if isinstance(new_chain[prev_idx], Plain):
                        new_chain[prev_idx].text = new_chain[prev_idx].text.rstrip(" \t")
                        break
                    elif not isinstance(new_chain[prev_idx], At):
                        break

                for next_idx in range(idx + 1, len(new_chain)):
                    if isinstance(new_chain[next_idx], Plain):
                        new_chain[next_idx].text = new_chain[next_idx].text.lstrip(" \t")
                        break
                    elif not isinstance(new_chain[next_idx], At):
                        break
            idx += 1

        # 第三阶段：注入零宽字符与防连防错
        idx = 0
        while idx < len(new_chain):
            if isinstance(new_chain[idx], At):
                found_plain = False
                for next_idx in range(idx + 1, len(new_chain)):
                    if isinstance(new_chain[next_idx], Plain):
                        new_chain[next_idx].text = "\u200b \u200b" + new_chain[next_idx].text
                        found_plain = True
                        break
                if not found_plain:
                    new_chain.insert(idx + 1, Plain("\u200b \u200b"))
            idx += 1

        result.chain = new_chain

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

        # 0. 检查群黑名单
        blacklisted_groups = [str(g).strip() for g in self.config.get("blacklisted_groups", []) if g]
        if str(group_id) in blacklisted_groups:
            return False, "", str(group_id), f"拒绝执行：当前群聊 ({group_id}) 已被管理员列入黑名单，插件功能已被禁用。"

        sender_id = str(event.get_sender_id()).strip()

        # 检查是否在允许普通群友调用的工具勾选列表中
        member_allowed_tools = self.config.get("member_allowed_tools", [
            "读取历史消息",
            "获取群信息",
            "获取全员列表",
            "查询成员信息",
            "戳一戳"
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
            "group_poke": "戳一戳",
            "at_all_members": "@全体成员"
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

        # C. 校验 Bot 账号自身在群内的身份权限（针对需要群管/群主身份的工具）
        admin_required_tools = {
            "ban_group_member": "禁言/解禁",
            "kick_group_member": "踢人/拉黑",
            "delete_group_message": "撤回消息",
            "set_group_essence_message": "设置/取消精华",
            "set_group_whole_ban": "全体禁言",
            "set_group_card": "修改群名片",
            "set_group_name": "修改群名称",
            "kick_inactive_members": "清理潜水成员",
        }
        owner_required_tools = {
            "set_group_special_title": "设置专属头衔",
            "set_group_admin": "设置/取消管理员"
        }

        if tool_name in admin_required_tools or tool_name in owner_required_tools:
            try:
                bot_self_id = getattr(event, "self_id", None)
                if not bot_self_id:
                    login_info = await self._call_onebot_action(event, "get_login_info")
                    if isinstance(login_info, dict):
                        bot_self_id = login_info.get("user_id")

                if bot_self_id:
                    bot_member_info = await self._call_onebot_action(
                        event,
                        "get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(bot_self_id),
                        no_cache=True
                    )
                    if isinstance(bot_member_info, dict):
                        bot_role = str(bot_member_info.get("role", "member")).lower()
                        tool_cn = admin_required_tools.get(tool_name) or owner_required_tools.get(tool_name)
                        
                        if tool_name in owner_required_tools:
                            if bot_role != "owner":
                                return False, "", str(group_id), f"操作失败：Bot 账号自身在群 ({group_id}) 内缺乏【群主】身份（当前角色为 {bot_role}）。【{tool_cn}】要求 Bot 账号自身必须具备群主身份。"
                        elif tool_name in admin_required_tools:
                            if bot_role not in {"owner", "admin"}:
                                return False, "", str(group_id), f"操作失败：Bot 账号自身在群 ({group_id}) 内缺乏管理员或群主权限（当前角色为普通成员）。【{tool_cn}】要求 Bot 账号必须具备群管理员或群主身份。"
            except Exception as e:
                logger.warning(f"[QQGroupAdmin] 预检 Bot 账号群权限失败: {e}")

        return True, auth_role, str(group_id), ""


    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        """
        监听并响应戳一戳事件：当有人戳 Bot 时，触发 LLM 根据角色性格回应戳一戳互动。
        """
        if not self.config.get("enable_poke_reply", True):
            return

        raw_msg = getattr(event.message_obj, "raw_message", {})
        raw_dict = raw_msg if isinstance(raw_msg, dict) else {}
        group_id = event.get_group_id() or raw_dict.get("group_id")
        if group_id:
            blacklisted_groups = [str(g).strip() for g in self.config.get("blacklisted_groups", []) if g]
            if str(group_id) in blacklisted_groups:
                return

        if event.get_platform_name() != "aiocqhttp":
            return

        if not raw_dict:
            return

        # 检查是否为戳一戳事件
        if not (raw_dict.get("post_type") == "notice"
                and raw_dict.get("notice_type") == "notify"
                and raw_dict.get("sub_type") == "poke"):
            return

        bot_id = event.get_self_id() or raw_dict.get("self_id")
        target_id = raw_dict.get("target_id")
        sender_id = event.get_sender_id() or raw_dict.get("user_id")

        if not bot_id or not target_id or not sender_id:
            return

        # 必须是戳 Bot 自己
        if str(target_id) != str(bot_id):
            return

        username = event.get_sender_name() or str(sender_id)
        prompt = (
            f"【系统提示：{username} 刚在聊天中戳了戳你】\n"
            "请完全契合你的性格角色与灵魂，自然地回应这次“戳一戳”互动。"
        )

        # 获取 conversation 对象保证上下文连续
        umo = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager
        conversation = None
        try:
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                cid = await conv_mgr.new_conversation(umo, event.get_platform_id())
            conversation = await conv_mgr.get_conversation(umo, cid)
        except Exception as e:
            logger.warning(f"[QQGroupAdmin] 戳一戳获取 conversation 失败: {e}")

        yield event.request_llm(prompt=prompt, conversation=conversation)
    @llm_tool(name="at_all_members")
    async def at_all_members(
        self,
        event: AstrMessageEvent
    ) -> str:
        """
        在 QQ 群聊中请求 @全体成员 的操作授权与权限检测。当用户要求 @全体成员 或发布全群通知时调用此工具检测权限并获取授权（工具内部会自动校验调用者权限）。
        """
        # 0. 检查 @ 功能全局开关
        if not self.config.get("enable_at_feature", True):
            return "操作失败：管理员已在插件配置中关闭了 @ 成员功能（含 @全体成员）。"

        # 1. 检查发送者/操作者的权限
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="at_all_members")
        if not ok:
            return err_msg

        # 2. 预检 Bot 账号自身在群内是否具备管理员/群主权限
        try:
            bot_self_id = getattr(event, "self_id", None)
            if not bot_self_id:
                login_info = await self._call_onebot_action(event, "get_login_info")
                if isinstance(login_info, dict):
                    bot_self_id = login_info.get("user_id")

            if bot_self_id:
                bot_member_info = await self._call_onebot_action(
                    event,
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(bot_self_id),
                    no_cache=True
                )
                if isinstance(bot_member_info, dict):
                    bot_role = str(bot_member_info.get("role", "member")).lower()
                    if bot_role not in {"owner", "admin"}:
                        return f"操作失败：Bot 账号自身在群 ({group_id}) 内缺乏管理员或群主权限（当前角色为普通成员）。请先将 Bot 设为群管理员后再试。"
        except Exception as e:
            logger.warning(f"[QQGroupAdmin] 预检 Bot 自身 @全体 权限失败: {e}")

        return "授权成功！你与 Bot 均已具备 @全体成员 权限。请在你的最终回复开头写上 [at:all]，并附带具体的通知或提醒文本，合成在同一条消息中回复给用户。"

    @llm_tool(name="ban_group_member")
    async def ban_group_member(
        self,
        event: AstrMessageEvent,
        target_user: str,
        duration_minutes: int,
        reason: str = ""
    ) -> str:
        """
        在 QQ 群聊中对违规或指定的群成员执行禁言或解除禁言操作。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

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
        action_name = "禁言" if duration_seconds > 0 else "解除禁言"

        try:
            res = await self._call_onebot_action(
                event,
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                duration=duration_seconds
            )
            reason_info = f"，原因：{reason}" if reason else ""
            if duration_seconds > 0:
                return f"成功：以 [{auth_role}] 身份将成员 ({cleaned_target}) 禁言 {duration_minutes} 分钟{reason_info}。"
            else:
                return f"成功：以 [{auth_role}] 身份为成员 ({cleaned_target}) 解除了禁言{reason_info}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 {action_name} 失败: {err_str}")
            if "permission" in err_str.lower() or "100" in err_str or "403" in err_str:
                return f"操作失败：Bot 账号自身在群 ({group_id}) 内缺乏管理员权限，或目标成员角色（如群主/管理员）高于 Bot 账号。"
            return f"执行 {action_name} 失败，API 错误：{err_str}"

    @llm_tool(name="kick_group_member")
    async def kick_group_member(
        self,
        event: AstrMessageEvent,
        target_user: str,
        reject_add_request: bool = False,
        reason: str = ""
    ) -> str:
        """
        在 QQ 群聊中将指定成员移除群聊（踢出群聊）。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
            reject_add_request (bool, optional): 是否同时拒绝该用户后续的加群申请（拉黑/黑名单）。默认 False。
            reason (str, optional): 移除群聊的原因或说明。
        """
        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="kick_group_member")
        if not ok:
            return err_msg

        try:
            res = await self._call_onebot_action(
                event,
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                reject_add_request=reject_add_request
            )
            block_info = "（已同步拒绝后续加群申请）" if reject_add_request else ""
            reason_info = f"，原因：{reason}" if reason else ""
            return f"成功：以 [{auth_role}] 身份已将成员 ({cleaned_target}) 移除群聊{block_info}{reason_info}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行踢人失败: {err_str}")
            return f"执行踢人失败，API 错误：{err_str}"

    @llm_tool(name="delete_group_message")
    async def delete_group_message(
        self,
        event: AstrMessageEvent,
        message_id: str = ""
    ) -> str:
        """
        在 QQ 群聊中撤回单条或多条指定消息（单次最多支持批量撤回 10 条）。支持直接撤回当前回复/引用的消息，或指定单个/多个 message_id（以英文逗号或空格分隔，如 '12345,67890'）。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

        Args:
            message_id (str, optional): 需撤回的消息 ID（单条或以逗号/空格分隔的多条 ID）。若为空则优先提取当前回复引用的消息 ID。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="delete_group_message")
        if not ok:
            return err_msg

        target_ids: List[int] = []

        if message_id.strip():
            raw_tokens = re.split(r"[,;\s]+", message_id.strip())
            for tok in raw_tokens:
                cleaned = re.sub(r"\D", "", tok)
                if cleaned:
                    target_ids.append(int(cleaned))

        if not target_ids and hasattr(event, "message_obj"):
            reply = getattr(event.message_obj, "reply", None)
            if reply:
                reply_id = getattr(reply, "id", None) or getattr(reply, "message_id", None)
                if reply_id:
                    cleaned_reply = re.sub(r"\D", "", str(reply_id))
                    if cleaned_reply:
                        target_ids.append(int(cleaned_reply))

        if not target_ids:
            return "操作失败：未提供要撤回的 message_id，且当前消息未回复/引用任何特定消息。"

        # 硬性截断：单次最多批量撤回 10 条
        target_ids = target_ids[:10]

        success_count = 0
        fail_errors = []

        for mid in target_ids:
            try:
                await self._call_onebot_action(
                    event,
                    "delete_msg",
                    message_id=int(mid)
                )
                success_count += 1
            except Exception as e:
                fail_errors.append(f"ID {mid}: {str(e)}")

        if success_count == len(target_ids):
            if len(target_ids) == 1:
                return f"成功：以 [{auth_role}] 身份成功撤回消息 (ID: {target_ids[0]})。"
            else:
                return f"成功：以 [{auth_role}] 身份并发批量撤回了全部 {success_count} 条消息 (IDs: {target_ids})。"
        elif success_count > 0:
            return f"部分成功：成功撤回 {success_count}/{len(target_ids)} 条消息。失败列表: {'; '.join(fail_errors)}"
        else:
            return f"撤回消息失败：API 报错 - {'; '.join(fail_errors)}"

    @llm_tool(name="set_group_essence_message")
    async def set_group_essence_message(
        self,
        event: AstrMessageEvent,
        message_id: str = "",
        enable: bool = True
    ) -> str:
        """
        在 QQ 群聊中将某条消息设置为群精华消息或移除群精华消息。支持直接设置/移除当前回复引用的消息，或指定 message_id 的消息。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

        Args:
            message_id (str, optional): 目标消息 ID。若为空则自动识别当前回复引用的消息 ID。
            enable (bool, optional): 是否设为精华。True 代表设为精华，False 代表移除精华。默认 True。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_essence_message")
        if not ok:
            return err_msg

        target_id = ""
        if message_id.strip():
            target_id = re.sub(r"\D", "", message_id)

        if not target_id and hasattr(event, "message_obj"):
            reply = getattr(event.message_obj, "reply", None)
            if reply:
                reply_id = getattr(reply, "id", None) or getattr(reply, "message_id", None)
                if reply_id:
                    target_id = re.sub(r"\D", "", str(reply_id))

        if not target_id:
            return "操作失败：未指定 message_id，且当前消息未回复/引用任何目标消息。"

        action_name = "设置群精华" if enable else "移除群精华"
        api_action = "set_essence_msg" if enable else "delete_essence_msg"

        try:
            await self._call_onebot_action(
                event,
                api_action,
                message_id=int(target_id)
            )
            return f"成功：以 [{auth_role}] 身份成功将消息 (ID: {target_id}) {action_name}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 {action_name} 失败: {err_str}")
            return f"执行 {action_name} 失败，API 错误：{err_str}"

    @llm_tool(name="get_group_msg_history")
    async def get_group_msg_history(
        self,
        event: AstrMessageEvent,
        message_seq: str = "",
        count: int = 20
    ) -> str:
        """
        在 QQ 群聊中获取历史消息记录列表。当需要查看群内近期发言、寻找特定成员发出的消息 ID（以协助撤回或设为精华）时调用（工具内部会自动校验调用者权限）。

        Args:
            message_seq (str, optional): 起始消息序号/ID。若为空则调取最新发送的历史消息。
            count (int, optional): 获取条数，范围 1~100。默认 20 条。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="get_group_msg_history")
        if not ok:
            return err_msg

        count = max(1, min(100, count))

        try:
            kwargs = {
                "group_id": int(group_id),
                "count": count
            }
            if message_seq.strip():
                cleaned_seq = re.sub(r"\D", "", message_seq)
                if cleaned_seq:
                    kwargs["message_seq"] = int(cleaned_seq)

            res = await self._call_onebot_action(event, "get_group_msg_history", **kwargs)
            
            messages = []
            if isinstance(res, dict) and "messages" in res:
                messages = res["messages"]
            elif isinstance(res, list):
                messages = res

            if not messages:
                return f"未获取到群 ({group_id}) 的历史消息记录。"

            formatted_lines = []
            import datetime
            for m in messages:
                if not isinstance(m, dict):
                    continue
                mid = m.get("message_id") or m.get("id") or "未知ID"
                sender = m.get("sender", {})
                sender_name = sender.get("card") or sender.get("nickname") or sender.get("user_id") or "未知发送者"
                time_ts = m.get("time", 0)
                time_str = datetime.datetime.fromtimestamp(time_ts).strftime("%H:%M:%S") if time_ts else ""
                
                raw_content = m.get("raw_message") or m.get("message") or ""
                if isinstance(raw_content, list):
                    raw_content = "".join([str(item.get("data", {}).get("text", "")) for item in raw_content if isinstance(item, dict)])
                
                formatted_lines.append(f"• [{time_str}] [ID: {mid}] {sender_name}: {raw_content[:80]}")

            return f"获取群 ({group_id}) 最近 {len(formatted_lines)} 条历史消息成功：\n" + "\n".join(formatted_lines)

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
        在 QQ 群聊中开启或关闭全员禁言（全体禁言）。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

        Args:
            enable (bool, optional): 是否开启全员禁言。True 代表开启全员禁言，False 代表解除全员禁言。默认 True。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_whole_ban")
        if not ok:
            return err_msg

        action_name = "开启全员禁言" if enable else "解除全员禁言"

        try:
            await self._call_onebot_action(
                event,
                "set_group_whole_ban",
                group_id=int(group_id),
                enable=enable
            )
            return f"成功：以 [{auth_role}] 身份成功为群 ({group_id}) {action_name}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 {action_name} 失败: {err_str}")
            return f"执行 {action_name} 失败，API 错误：{err_str}"

    @llm_tool(name="set_group_card")
    async def set_group_card(
        self,
        event: AstrMessageEvent,
        target_user: str,
        card: str = ""
    ) -> str:
        """
        在 QQ 群聊中设置或修改指定成员的群名片（群昵称）。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
            card (str, optional): 新的群名片文本。若为空则代表清空/重置名片。
        """
        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_card")
        if not ok:
            return err_msg

        action_desc = f"修改群名片为 '{card}'" if card else "清空群名片"

        try:
            await self._call_onebot_action(
                event,
                "set_group_card",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                card=card
            )
            return f"成功：以 [{auth_role}] 身份成功为成员 ({cleaned_target}) {action_desc}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 修改群名片失败: {err_str}")
            return f"修改群名片失败，API 错误：{err_str}"

    @llm_tool(name="set_group_special_title")
    async def set_group_special_title(
        self,
        event: AstrMessageEvent,
        target_user: str,
        special_title: str = "",
        duration_days: int = -1
    ) -> str:
        """
        在 QQ 群聊中设置指定成员的头衔（专属头衔）。该操作要求执行调用的 Bot 账号自身在群内具备【群主】身份，否则 QQ 服务端将拒绝执行。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

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
                special_title=special_title,
                duration=duration_seconds
            )
            title_desc = f"颁发头衔 '{special_title}'" if special_title else "撤销头衔"
            return f"成功：以 [{auth_role}] 身份为成员 ({cleaned_target}) {title_desc}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 设置专属头衔失败: {err_str}")
            return f"设置专属头衔失败（要求 Bot 账号自身具备群主身份），API 错误：{err_str}"

    @llm_tool(name="set_group_admin")
    async def set_group_admin(
        self,
        event: AstrMessageEvent,
        target_user: str,
        enable: bool = True
    ) -> str:
        """
        在 QQ 群聊中设置或取消某位成员的管理员身份。该操作要求执行调用的 Bot 账号自身在群内具备【群主】身份，否则 QQ 服务端将拒绝执行。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

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

        action_name = "设置群管理员" if enable else "取消群管理员"

        try:
            await self._call_onebot_action(
                event,
                "set_group_admin",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                enable=enable
            )
            return f"成功：以 [{auth_role}] 身份为成员 ({cleaned_target}) {action_name}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 {action_name} 失败: {err_str}")
            return f"执行 {action_name} 失败（要求 Bot 账号自身具备群主身份），API 错误：{err_str}"

    @llm_tool(name="set_group_name")
    async def set_group_name(
        self,
        event: AstrMessageEvent,
        group_name: str
    ) -> str:
        """
        在 QQ 群聊中修改群聊名称。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

        Args:
            group_name (str): 新的群名称。
        """
        if not group_name.strip():
            return "操作失败：新的群名称不能为空。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="set_group_name")
        if not ok:
            return err_msg

        try:
            await self._call_onebot_action(
                event,
                "set_group_name",
                group_id=int(group_id),
                group_name=group_name.strip()
            )
            return f"成功：以 [{auth_role}] 身份将群聊名称修改为 '{group_name.strip()}'。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 修改群名称失败: {err_str}")
            return f"修改群名称失败，API 错误：{err_str}"

    @llm_tool(name="send_group_notice")
    async def send_group_notice(
        self,
        event: AstrMessageEvent,
        action: str = "publish",
        content: str = "",
        notice_id: str = ""
    ) -> str:
        """
        群公告全能工具：支持发布、读取、删除群公告三种操作。当用户提出明确要求时调用（工具内部会自动校验调用者权限）。

        三种操作模式（通过 action 参数区分，大模型需根据用户意图自动选择）：
        - action="publish"（发布群公告）：传入 content 作为公告文案，调用 _send_group_notice 发布新公告。
        - action="get"（读取群公告）：无需 content，调用 _get_group_notice 获取当前群公告列表。
        - action="delete"（删除群公告）：传入 notice_id 作为要删除的公告 ID，调用 _del_group_notice 删除指定公告。

        Args:
            action (str): 操作类型，可选 "publish"（发布）、"get"（读取）、"delete"（删除），默认 "publish"。
            content (str): 发布公告时的公告文案内容（仅 action="publish" 时需要）。
            notice_id (str): 删除公告时的公告 ID（仅 action="delete" 时需要）。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="send_group_notice")
        if not ok:
            return err_msg

        action = action.strip().lower() if action else "publish"

        # 检查 Bot 自身是否具备群管理员/群主身份（发布与删除公告需要权限）
        if action in {"publish", "delete"}:
            try:
                bot_self_id = getattr(event, "self_id", None)
                if not bot_self_id:
                    login_info = await self._call_onebot_action(event, "get_login_info")
                    if isinstance(login_info, dict):
                        bot_self_id = login_info.get("user_id")

                if bot_self_id:
                    bot_member_info = await self._call_onebot_action(
                        event,
                        "get_group_member_info",
                        group_id=int(group_id),
                        user_id=int(bot_self_id),
                        no_cache=True
                    )
                    if isinstance(bot_member_info, dict):
                        bot_role = str(bot_member_info.get("role", "member")).lower()
                        if bot_role not in {"owner", "admin"}:
                            action_cn = "发布" if action == "publish" else "删除"
                            return f"操作失败：Bot 账号自身在群 ({group_id}) 内缺乏管理员或群主权限（当前角色为普通成员）。{action_cn}群公告要求 Bot 账号必须具备群管理员或群主身份。"
            except Exception as e:
                logger.warning(f"[QQGroupAdmin] 预检 Bot 账号群权限失败: {e}")

        # 读取群公告
        if action == "get":
            try:
                res = await self._call_onebot_action(
                    event,
                    "_get_group_notice",
                    group_id=int(group_id)
                )
                return f"成功：以 [{auth_role}] 身份查询到群 ({group_id}) 的公告列表：\n{res}"
            except Exception as e:
                err_str = str(e)
                logger.error(f"[QQGroupAdmin] 读取群公告失败: {err_str}")
                return f"读取群公告失败，API 错误：{err_str}"

        # 删除群公告
        if action == "delete":
            if not notice_id.strip():
                return "操作失败：删除群公告需要提供要删除的公告 ID（notice_id）。"
            try:
                await self._call_onebot_action(
                    event,
                    "_del_group_notice",
                    group_id=int(group_id),
                    notice_id=notice_id.strip()
                )
                return f"成功：以 [{auth_role}] 身份删除了群 ({group_id}) 中 ID 为 '{notice_id.strip()}' 的群公告。"
            except Exception as e:
                err_str = str(e)
                logger.error(f"[QQGroupAdmin] 删除群公告失败: {err_str}")
                return f"删除群公告失败，API 错误：{err_str}"

        # 默认：发布群公告
        if not content.strip():
            return "操作失败：群公告文案内容不能为空。"

        try:
            await self._call_onebot_action(
                event,
                "_send_group_notice",
                group_id=int(group_id),
                content=content.strip()
            )
            return f"成功：以 [{auth_role}] 身份在群 ({group_id}) 中发布了全新群公告。"
        except Exception:
            try:
                await self._call_onebot_action(
                    event,
                    "send_group_notice",
                    group_id=int(group_id),
                    content=content.strip()
                )
                return f"成功：以 [{auth_role}] 身份在群 ({group_id}) 中发布了全新群公告。"
            except Exception as e2:
                err_str = str(e2)
                logger.error(f"[QQGroupAdmin] 发布群公告失败: {err_str}")
                return f"发布群公告失败，API 错误：{err_str}"

    @llm_tool(name="get_group_info")
    async def get_group_info(
        self,
        event: AstrMessageEvent
    ) -> str:
        """
        在 QQ 群聊中获取当前群聊的详细信息（群名称、群主 QQ、成员数、最大容量等）。当需要了解群基础信息时调用（工具内部会自动校验调用者权限）。
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
                gname = res.get("group_name", "未命名群聊")
                owner = res.get("owner_id") or res.get("owner", "未知")
                member_cnt = res.get("member_count", "未知")
                max_cnt = res.get("max_member_count", "未知")
                info_str = (
                    f"群 ({group_id}) 详细信息：\n"
                    f"• 群名称：{gname}\n"
                    f"• 群主 QQ：{owner}\n"
                    f"• 成员数量：{member_cnt} / {max_cnt}"
                )
                return info_str

            return f"获取群 ({group_id}) 信息成功，但返回数据格式异常。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 get_group_info 失败: {err_str}")
            return f"获取群信息 API 出错：{err_str}"

    @llm_tool(name="get_group_member_list")
    async def get_group_member_list(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        sort_by_join_time: bool = False,
        sort_by_last_sent_time: bool = False,
        sort_by_group_level: bool = False,
        sort_oldest_first: bool = False
    ) -> str:
        """
        在 QQ 群聊中获取全员列表或根据关键词（昵称/名片/QQ号）搜索群成员，或按进群时间、最近发言时间、群等级进行多维度排序。当需要查找某群员信息、统计全员、查看最新进群新人、高/低群等级成员或按时间/等级升降序排列时调用（工具内部会自动校验调用者权限）。

        Args:
            keyword (str, optional): 搜索关键词，支持匹配昵称、群名片或QQ号。若为空则默认列出前 30 名成员。
            sort_by_join_time (bool, optional): 是否按进群时间排序。当用户询问“进群的人”、“加群的新人/老人”或按入群时间查看时设置为 True。默认 False。
            sort_by_last_sent_time (bool, optional): 是否按最近发言时间排序。当用户询问“最近谁发言了”、“最久没发言的人”、“按发言时间查看”时设置为 True。默认 False。
            sort_by_group_level (bool, optional): 是否按群等级排序。当用户询问“群等级最高/最低的人”、“按群等级排序”时设置为 True。默认 False。
            sort_oldest_first (bool, optional): 是否升序排序（从旧到新 / 从低到高）。当用户询问“最久没发言”、“最低群等级”、“最早进群”、“从小到大/升序”时设置为 True。默认 False（即默认降序：最新/最高）。
        """
        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="get_group_member_list")
        if not ok:
            return err_msg

        try:
            res = await self._call_onebot_action(
                event,
                "get_group_member_list",
                group_id=int(group_id),
                no_cache=True
            )
            if not isinstance(res, list):
                return f"获取群 ({group_id}) 成员列表数据失败。"

            # 排序逻辑：优先响应发言时间排序，其次群等级排序，最后响应进群时间排序
            reverse_order = not sort_oldest_first
            if sort_by_last_sent_time:
                res = sorted(res, key=lambda x: x.get("last_sent_time", 0), reverse=reverse_order)
            elif sort_by_group_level:
                res = sorted(res, key=lambda x: int(x.get("level", 0)), reverse=reverse_order)
            elif sort_by_join_time:
                res = sorted(res, key=lambda x: x.get("join_time", 0), reverse=reverse_order)

            matched = []
            clean_kw = keyword.strip().lower()

            import datetime
            for m in res:
                if not isinstance(m, dict):
                    continue
                user_id = str(m.get("user_id", ""))
                nickname = str(m.get("nickname", ""))
                card = str(m.get("card", ""))
                role = str(m.get("role", "member"))
                role_cn = "群主" if role == "owner" else ("管理员" if role == "admin" else "成员")
                
                last_sent_ts = m.get("last_sent_time", 0)
                join_time_ts = m.get("join_time", 0)
                
                last_sent_str = datetime.datetime.fromtimestamp(last_sent_ts).strftime("%m-%d %H:%M") if last_sent_ts else "从未发言"
                join_time_str = datetime.datetime.fromtimestamp(join_time_ts).strftime("%Y-%m-%d %H:%M") if join_time_ts else "未知"

                display_name = card if card else nickname
                level = m.get("level", 0)
                item_str = f"• {display_name} ({user_id}) [{role_cn}] LV.{level} - 最近发言: {last_sent_str} | 入群: {join_time_str}"

                if clean_kw:
                    if clean_kw in user_id.lower() or clean_kw in card.lower() or clean_kw in nickname.lower() or clean_kw in role_cn.lower() or clean_kw in role.lower():
                        matched.append(item_str)
                else:
                    matched.append(item_str)

            total_found = len(matched)
            if not matched:
                return f"在群 ({group_id}) 中未找到匹配 '{keyword}' 的群成员。"

            display_list = matched[:30]
            suffix = f"\n... 等共 {total_found} 人" if total_found > 30 else ""
            
            desc_parts = []
            order_text = "从最旧到最新" if sort_oldest_first else "从最新到最旧"
            level_order_text = "从低到高" if sort_oldest_first else "从高到低"
            if sort_by_last_sent_time:
                desc_parts.append(f"按最近发言时间{order_text}")
            elif sort_by_group_level:
                desc_parts.append(f"按群等级{level_order_text}")
            elif sort_by_join_time:
                desc_parts.append(f"按入群时间{order_text}")

            if keyword:
                desc_parts.append(f"关键词：'{keyword}'")
            else:
                desc_parts.append("展示前30人")
            
            kw_desc = f"（{', '.join(desc_parts)}）"

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
        在 QQ 群聊中对指定成员执行“戳一戳”（拍一拍/戳一下）操作。当用户要求“戳一下某某”或“拍一拍某某”时调用（工具内部会自动校验调用者权限）。

        Args:
            target_user (str): 目标用户的 QQ 号，或消息中 @ 目标的纯数字 ID/文本。
        """
        cleaned_target = re.sub(r"\D", "", str(target_user))
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await self._check_permission(event, tool_name="group_poke")
        if not ok:
            return err_msg

        try:
            await self._call_onebot_action(
                event,
                "group_poke",
                group_id=int(group_id),
                user_id=int(cleaned_target)
            )
            return f"成功：以 [{auth_role}] 身份对成员 ({cleaned_target}) 执行了“戳一戳”操作。"
        except Exception:
            try:
                await self._call_onebot_action(
                    event,
                    "friend_poke",
                    user_id=int(cleaned_target)
                )
                return f"成功：以 [{auth_role}] 身份对成员 ({cleaned_target}) 执行了“戳一戳”操作。"
            except Exception as e2:
                err_str = str(e2)
                logger.error(f"[QQGroupAdmin] 执行 group_poke 失败: {err_str}")
                return f"执行戳一戳失败，API 错误：{err_str}"

    @llm_tool(name="get_group_member_info")
    async def get_group_member_info(
        self,
        event: AstrMessageEvent,
        target_user: str
    ) -> str:
        """
        在 QQ 群聊中获取指定群成员的详细个人资料（包含最后发言时间、入群时间、群等级 LV、群名片、群头衔、角色身份、禁言状态等）。当需要精准了解或查询某位群成员的详细状态、群等级与最近发言时间时调用（工具内部会自动校验调用者权限）。

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
                nickname = res.get("nickname", "未命名")
                card = res.get("card", "")
                role = res.get("role", "member")
                role_cn = "群主" if role == "owner" else ("管理员" if role == "admin" else "普通成员")
                title = res.get("title", "无头衔")
                join_ts = res.get("join_time", 0)
                last_sent_ts = res.get("last_sent_time", 0)
                shut_up_ts = res.get("shut_up_timestamp", 0)

                import datetime
                import time

                join_time_str = datetime.datetime.fromtimestamp(join_ts).strftime("%Y-%m-%d %H:%M:%S") if join_ts else "未知"
                last_sent_str = datetime.datetime.fromtimestamp(last_sent_ts).strftime("%Y-%m-%d %H:%M:%S") if last_sent_ts else "从未发言"
                
                ban_until_str = "未禁言"
                if shut_up_ts and shut_up_ts > time.time():
                    ban_until_str = datetime.datetime.fromtimestamp(shut_up_ts).strftime("%Y-%m-%d %H:%M:%S")

                level = res.get("level", 0)
                info_lines = [
                    f"用户 ({cleaned_target}) 详细群资料：",
                    f"• 昵称/名片：{card or nickname} ({nickname})",
                    f"• 群身份：{role_cn}",
                    f"• 群等级：LV.{level}",
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
        max_group_level: int = 0,
        confirm: bool = False
    ) -> str:
        """
        在 QQ 群聊中查找或清理长时间未发言的潜水成员（工具内部会自动校验调用者权限）。
        支持结合潜水天数与群等级（LV.1~LV.100）双重门槛筛选。
        当 confirm=False 时为安全预检模式（仅查询并列出名单）。在预检模式下，若当前未指定 max_group_level（即 max_group_level<=0），请在大模型最终回复的末尾温柔提示用户：“当前未开启【群等级】过滤。如果想避免误踢早期活跃过的老成员，可以补充群等级筛选要求（例如：清理超过60天未发言且群等级低于LV.10的成员）”。
        当 confirm=True 时执行真正的移出群聊操作。

        Args:
            days (int, optional): 判断潜水的天数阈值，默认 60 天。
            max_group_level (int, optional): 群等级上限门槛（如设为 10 代表仅清理群等级低于 LV.10 的成员）。0 代表不限制群等级。默认 0。
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
                    last_str = datetime.datetime.fromtimestamp(last_sent_ts).strftime("%Y-%m-%d") if last_sent_ts else "从未发言"
                    inactive_members.append({
                        "user_id": user_id,
                        "name": card,
                        "level": level,
                        "last_sent_str": last_str
                    })

            level_filter_desc = f" 且群等级低于 LV.{max_group_level}" if max_group_level > 0 else ""
            if not inactive_members:
                return f"在群 ({group_id}) 中未找到超过 {days} 天未发言{level_filter_desc} 的潜水普通成员。"

            total_cnt = len(inactive_members)

            if not confirm:
                lines = [f"🔍 潜水成员扫描预览（共找到 {total_cnt} 人超过 {days} 天未发言{level_filter_desc}）："]
                for item in inactive_members[:20]:
                    lines.append(f"• {item['name']}({item['user_id']}) LV.{item['level']} - 最近发言: {item['last_sent_str']}")
                if total_cnt > 20:
                    lines.append(f"... 等共 {total_cnt} 人")

                confirm_cmd = f"确认清理超过 {days} 天未发言"
                if max_group_level > 0:
                    confirm_cmd += f"且群等级低于 LV.{max_group_level}"
                confirm_cmd += " 的潜水成员"

                lines.append(f"\n提示：请确认无误后对 Bot 说明 '{confirm_cmd}' 来执行清理。")
                if max_group_level <= 0:
                    lines.append("(系统提示：当前扫描未限制群等级，请在大模型合成回复时提醒用户可补充群等级筛选。)")

                return "\n".join(lines)

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
                    fail_cnt += 1
                    logger.error(f"[QQGroupAdmin] 清理潜水成员 {item['user_id']} 失败: {e}")

            return f"成功：以 [{auth_role}] 身份执行潜水成员清理完成！成功移出 {success_cnt} 人，失败 {fail_cnt} 人。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"[QQGroupAdmin] 执行 kick_inactive_members 失败: {err_str}")
            return f"清理潜水成员 API 出错：{err_str}"
