"""
AstrBot QQ群大模型管理工具 v3.0.0

功能描述：
- 提供注册给大模型调用的全套 QQ 群管理与互动工具，可用自然语言指挥 Bot 进行群管理操作，并支持自动入群审核和人机验证等。

作者: 往昔的涟漪
版本: 3.0.0
日期: 2026-08-10
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Any, List

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import llm_tool
from astrbot.api.message_components import At, BaseMessageComponent, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .constants import (
    AT_INSTRUCTION,
    AT_PATTERN,
    LOG_PREFIX,
    POKE_PROMPT_TEMPLATE,
)
from .permission import check_permission
from .utils import (
    call_onebot_action,
    clean_qq_number,
    extract_reply_message_id,
    format_group_msg_history,
    format_member_list,
    format_timestamp,
    get_bot_role_in_group,
    is_blacklisted_group,
    scan_inactive_members,
    summarize_group_level,
)

logger = logging.getLogger("astrbot")


@register(
    "astrbot_plugin_qq_group_admin",
    "往昔的涟漪",
    "提供注册给大模型调用的全套 QQ 群管理与互动工具，可用自然语言指挥 Bot 进行群管理操作，并支持自动入群审核和人机验证等。",
    "3.0.1",
    "https://github.com/CyreneLian/astrbot_plugin_qq_group_admin"
)
class QQGroupAdminPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        # 兼容分组配置（群聊管理与互动工具 / 自动同意入群工具）：
        # 将各分组内的配置项摊平到顶层，便于各处按原键名读取，同时兼容旧的扁平配置。
        # 注意：必须通过「创建新字典」合并，绝不能修改传入的 config 对象本身，
        # 否则会污染 AstrBot 共享的 AstrBotConfig 实例，导致配置面板渲染出多余的扁平配置项。
        _meta_keys = {"type", "description", "hint", "obvious_hint", "items", "default", "options", "slider"}
        _flattened = {}
        for _group_val in list(self.config.values()):
            if isinstance(_group_val, dict):
                _items = _group_val.get("items") if isinstance(_group_val.get("items"), dict) else _group_val
                if isinstance(_items, dict):
                    for _k, _v in _items.items():
                        if _k not in _meta_keys:
                            _flattened[_k] = _v
        if _flattened:
            self.config = {**self.config, **_flattened}
        # 入群审核管理面板 Web API（查看/管理入群失败次数与黑名单）
        try:
            from .web import JoinVerifyWebController
            self._web = JoinVerifyWebController(context, self.config)
            self._web.register_routes()
            logger.info(f"{LOG_PREFIX} 入群审核管理面板已注册")
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 注册入群审核管理面板失败: {e}")
            self._web = None

        # 入群人机验证状态：{(group_id, user_id): {"answer": int, "attempts": int, "max_attempts": int, "task": Task, "event": event}}
        self._join_verify_state: dict = {}
        # 入群人机验证黑名单持久化：{user_id: {"failures": int, "blacklisted": bool}}
        try:
            self._verify_data_dir = os.path.join(
                get_astrbot_plugin_data_path(), "astrbot_plugin_qq_group_admin"
            )
            os.makedirs(self._verify_data_dir, exist_ok=True)
            self._verify_blacklist_file = os.path.join(
                self._verify_data_dir, "join_verify_blacklist.json"
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 初始化入群黑名单数据目录失败: {e}")
            self._verify_blacklist_file = ""
        # 从 context 获取 Bot 全局配置中的 admins_id 列表
        try:
            raw_admins = context.get_config().get("admins_id", [])
            self.admins_id = [str(a).strip() for a in raw_admins if a]
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 获取 admins_id 失败: {e}")
            self.admins_id = []

        # 正则表达式：用于匹配符合规范的艾特标签，例如 [at:123456] 或 [at:all]
        self.valid_at_pattern = AT_PATTERN

    @filter.on_llm_request()
    async def inject_at_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """
        在 LLM 发出请求前注入艾特功能提示词，告知大模型如何格式化输出 [at:QQ号]。
        当配置中关闭「允许普通 @成员 功能」时，不注入该提示词。
        """
        enable_at_feature = self.config.get("enable_at_feature", True)
        if not enable_at_feature:
            return

        if is_blacklisted_group(self.config, event.get_group_id()):
            return

        req.system_prompt = (req.system_prompt or "") + AT_INSTRUCTION

    @filter.on_decorating_result(priority=2)
    async def process_at_tags(self, event: AstrMessageEvent):
        """
        拦截器：在消息发送给用户前，将 [at:数字] 和 [at:all] 解析为平台原生的 At 组件，并补充防连连看字符。
        """
        if not self.config.get("enable_at_feature", True):
            return

        if is_blacklisted_group(self.config, event.get_group_id()):
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
        if is_blacklisted_group(self.config, group_id):
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
        prompt = POKE_PROMPT_TEMPLATE.format(username=username)

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
            logger.warning(f"{LOG_PREFIX} 戳一戳获取 conversation 失败: {e}")

        yield event.request_llm(prompt=prompt, conversation=conversation)

    async def _get_user_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取用户昵称（退群用户已不在群，改用陌生人信息接口查询）"""
        try:
            info = await call_onebot_action(event, "get_stranger_info", user_id=int(user_id))
            if isinstance(info, dict):
                return str(info.get("nickname") or info.get("name") or "").strip()
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 获取用户 {user_id} 昵称失败: {e}")
        return ""

    async def _get_operator_info(self, event: AstrMessageEvent, group_id: str, user_id: str) -> tuple[str, str]:
        """获取操作者群信息：返回 (身份中文名, 显示名)。

        身份：群主/管理员（无则空字符串）；
        显示名：优先群名片（card/群昵称），无群名片则回退 QQ 昵称（nickname）。
        查询失败返回 ("", "")。
        """
        try:
            info = await call_onebot_action(
                event, "get_group_member_info",
                group_id=int(group_id), user_id=int(user_id),
            )
            if isinstance(info, dict):
                role = str(info.get("role", "")).lower()
                role_cn = ""
                if role == "owner":
                    role_cn = "群主"
                elif role in ("admin", "administrator"):
                    role_cn = "管理员"
                card = str(info.get("card") or "").strip()
                display = card or str(info.get("nickname") or "").strip()
                return role_cn, display
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 获取操作者 {user_id} 群信息失败: {e}")
        return "", ""

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_decrease(self, event: AstrMessageEvent):
        """
        监听群成员退群事件（notice/group_decrease）并在群内提示：
        - leave：成员主动退群 → 显示「xxx 已主动退群」
        - kick：被管理员/群主移出 → 显示「xxx 已被 yyy 移出群聊」
        - kick_me：Bot 自己被移出 → 不提示
        受「退群提示开关」(enable_group_decrease_notice) 控制，黑名单群不生效。
        """
        if not self.config.get("enable_group_decrease_notice", True):
            return

        raw_msg = getattr(event.message_obj, "raw_message", {})
        raw_dict = raw_msg if isinstance(raw_msg, dict) else {}
        if not raw_dict:
            return
        if not (raw_dict.get("post_type") == "notice"
                and raw_dict.get("notice_type") == "group_decrease"):
            return

        group_id = str(raw_dict.get("group_id", "") or "")
        user_id = str(raw_dict.get("user_id", "") or "")
        operator_id = str(raw_dict.get("operator_id", "") or "")
        sub_type = raw_dict.get("sub_type", "")
        if not group_id or not user_id:
            return
        # 黑名单群不处理
        if is_blacklisted_group(self.config, group_id):
            return
        # Bot 自己被移出：不提示（兼容协议端 sub_type 不规范的情况，额外校验退群者是否为 Bot 自身）
        if sub_type == "kick_me" or str(user_id) == str(event.get_self_id()):
            return
        # Bot 自己移除成员时不发退群提示（避免踢人操作后再补一条多余提示）
        if sub_type == "kick" and str(operator_id) == str(event.get_self_id()):
            logger.info(f"{LOG_PREFIX} 退群提示跳过：用户 {user_id} 被 Bot 自身移出群 {group_id}（不重复提示）")
            return

        nickname = await self._get_user_nickname(event, user_id)
        if not nickname:
            nickname = user_id

        if sub_type == "leave":
            msg = f"{nickname}({user_id}) 已主动退群"
        elif sub_type == "kick":
            # 操作者（移除群聊的人）：显示「身份+群昵称/QQ昵称」（如 群主小丽 / 管理员小明），不加 QQ 号
            op_role = ""
            op_nick = ""
            if operator_id:
                op_role, op_nick = await self._get_operator_info(event, group_id, operator_id)
            if op_role and op_nick:
                msg = f"{nickname}({user_id}) 已被 {op_role}{op_nick} 移出群聊"
            elif op_nick:
                msg = f"{nickname}({user_id}) 已被 {op_nick} 移出群聊"
            else:
                msg = f"{nickname}({user_id}) 已被管理员/群主移出群聊"
        else:
            return

        try:
            await event.send(event.chain_result([
                Plain(f" {msg}"),
            ]))
            logger.info(f"{LOG_PREFIX} 退群提示已发送：用户 {user_id} 在群 {group_id}（sub_type={sub_type}）")
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 发送退群提示失败: {e}")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_increase(self, event: AstrMessageEvent):
        """
        监听新人入群事件（notice/group_increase）：
        人机验证关闭时，黑名单等整套人机防线都不生效；
        开启时：黑名单用户发提示后移出群聊，其余新人触发随机加减法人机验证。
        """
        # 人机验证总开关：关闭则整套人机防线（含黑名单）都不生效，
        # 同时取消所有进行中的人机验证任务（防止残留任务继续答题/超时踢人）
        if not self.config.get("enable_join_verify", False):
            self._cancel_all_join_verify()
            return

        raw_msg = getattr(event.message_obj, "raw_message", {})
        raw_dict = raw_msg if isinstance(raw_msg, dict) else {}
        if not raw_dict:
            return

        # 退群事件：清理该用户的验证状态（避免退群后快速重新入群时旧状态残留导致不发新题）
        if (raw_dict.get("post_type") == "notice"
                and raw_dict.get("notice_type") == "group_decrease"):
            group_id = str(raw_dict.get("group_id", "") or "")
            user_id = str(raw_dict.get("user_id", "") or "")
            state = self._join_verify_state.pop((group_id, user_id), None)
            if state:
                if state["task"]:
                    state["task"].cancel()
                logger.info(f"{LOG_PREFIX} 用户 {user_id} 退群 {group_id}，已清理人机验证状态")
            return

        # 仅处理新人入群通知（notice/group_increase）
        if not (raw_dict.get("post_type") == "notice"
                and raw_dict.get("notice_type") == "group_increase"):
            return

        group_id = str(raw_dict.get("group_id", "") or "")
        user_id = str(raw_dict.get("user_id", "") or "")
        if not group_id or not user_id:
            return
        # 排除 Bot 自己入群
        if str(user_id) == str(event.get_self_id()):
            return
        # 黑名单群不处理
        if is_blacklisted_group(self.config, group_id):
            return
        # Bot 权限自检：只在 Bot 为群主或管理员的群生效（否则无法踢人，无需验证）
        bot_role = await get_bot_role_in_group(event, group_id)
        if bot_role and bot_role not in {"owner", "admin", "administrator"}:
            logger.info(
                f"{LOG_PREFIX} Bot 在群 {group_id} 中无管理权限（当前角色：{bot_role}），跳过入群人机验证与黑名单拦截。"
            )
            return
        # 入群黑名单用户：先发送提示，10秒后移出群聊并拉黑（防止通过其他途径进群）
        if self._is_join_verify_blacklisted(user_id):
            # 发送提示
            msg = "很抱歉，你是黑名单中的用户，你将在10秒后被移除群聊！"
            try:
                await event.send(event.chain_result([
                    At(qq=user_id),
                    Plain(f" {msg}"),
                ]))
                logger.info(
                    f"{LOG_PREFIX} 黑名单用户入群提示已发送：用户 {user_id} 在群 {group_id}（10秒后移出）"
                )
            except Exception as e:
                logger.error(f"{LOG_PREFIX} 发送黑名单用户入群提示失败: {e}")

            # 等待 10 秒
            await asyncio.sleep(10)

            # 真正执行踢出
            try:
                await call_onebot_action(
                    event,
                    "set_group_kick",
                    group_id=int(group_id),
                    user_id=int(user_id),
                    reject_add_request=True
                )
                logger.info(
                    f"{LOG_PREFIX} 入群黑名单用户移出群聊：用户 {user_id} 移出群 {group_id}"
                )
            except Exception as e:
                logger.error(f"{LOG_PREFIX} 入群黑名单用户移出群聊失败：用户 {user_id} → 群 {group_id}，错误: {e}")
            return
        # 避免重复验证
        if (group_id, user_id) in self._join_verify_state:
            return

        # 生成随机加减法题目（结果非负）
        a = random.randint(1, 50)
        b = random.randint(1, 50)
        if random.random() < 0.5:
            answer = a + b
            expr = f"{a} + {b}"
        else:
            if a < b:
                a, b = b, a
            answer = a - b
            expr = f"{a} - {b}"

        # 安全读取超时与错误次数配置（防御非法配置值，异常时使用默认值，与配置面板默认对齐）
        try:
            timeout = max(10, int(self.config.get("join_verify_timeout", 180) or 180))
        except (ValueError, TypeError):
            timeout = 180
        try:
            max_attempts = max(1, int(self.config.get("join_verify_max_attempts", 3) or 3))
        except (ValueError, TypeError):
            max_attempts = 3

        # @新人发送题目
        try:
            await event.send(event.chain_result([
                At(qq=user_id),
                Plain(
                    f" 欢迎入群！请完成人机验证：\n"
                    f"请计算 {expr} = ?\n"
                    f"请直接回复数字答案（限 {timeout} 秒内，答错 {max_attempts} 次将被移出群聊）"
                ),
            ]))
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 发送人机验证题目失败: {e}")
            return

        # 记录验证状态并启动超时定时器
        task = asyncio.create_task(
            self._join_verify_timeout_kick(group_id, user_id, timeout)
        )
        self._join_verify_state[(group_id, user_id)] = {
            "answer": answer,
            "attempts": 0,
            "max_attempts": max_attempts,
            "task": task,
            "event": event,
            "expr": expr,
        }
        logger.info(
            f"{LOG_PREFIX} 人机验证已启动：用户 {user_id} 加入群 {group_id}，题目 {expr} = ?，限时 {timeout} 秒"
        )

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message_join_verify(self, event: AstrMessageEvent):
        """
        监测待验证用户的群消息，判断其回复的答案是否正确。
        仅在存在待验证状态时介入，不影响其他消息的正常处理。
        """
        # 人机验证总开关关闭：取消所有进行中的验证任务并停止处理（防止残留任务继续答题/踢人）
        if not self.config.get("enable_join_verify", False):
            self._cancel_all_join_verify()
            return

        if not self._join_verify_state:
            return

        # 跳过 notice/request 等系统事件（其 message_str 为空，会导致误触发「请直接回复数字答案」提示）
        raw_msg = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_msg, dict) and raw_msg.get("post_type") in ("notice", "request"):
            return
        if not event.message_str or not event.message_str.strip():
            return

        group_id = str(event.get_group_id() or "")
        sender_id = str(event.get_sender_id() or "")
        key = (group_id, sender_id)
        state = self._join_verify_state.get(key)
        if state is None:
            return

        # 提取纯文本段（跳过 At/Reply 等非文本元素）：
        # 兼容用户 @Bot 或引用消息回答时，message_str 可能含 @ 昵称/引用残留的情况
        text_parts = []
        msg_chain = getattr(event.message_obj, "message", None)
        if isinstance(msg_chain, list):
            for comp in msg_chain:
                # 只取纯文本段（Plain 组件），跳过 At/Reply 等非文本元素
                if isinstance(comp, Plain):
                    t = getattr(comp, "text", "") or ""
                    if t.strip():
                        text_parts.append(t)
        content = "".join(text_parts).strip() if text_parts else event.message_str.strip()
        if not content or not content.isdigit():
            await event.send(event.chain_result([
                At(qq=sender_id),
                Plain(" 请直接回复数字答案"),
            ]))
            # 终止事件传播：避免待验证用户 @Bot 的消息继续触发 LLM 调用
            event.stop_event()
            return

        answer = int(content)
        if answer == state["answer"]:
            # 验证成功
            self._join_verify_state.pop(key, None)
            if state["task"]:
                state["task"].cancel()
            await event.send(event.chain_result([
                At(qq=sender_id),
                Plain(" ✅ 验证成功，欢迎加入！"),
            ]))
            # 终止事件传播：避免 @Bot 的验证消息继续触发 LLM 调用
            event.stop_event()
            logger.info(f"{LOG_PREFIX} 人机验证通过：用户 {sender_id} 在群 {group_id}")
        else:
            state["attempts"] += 1
            remaining = state["max_attempts"] - state["attempts"]
            if remaining <= 0:
                # 错误次数达上限，踢出
                self._join_verify_state.pop(key, None)
                if state["task"]:
                    state["task"].cancel()
                await self._kick_join_verify_user(
                    state["event"], group_id, sender_id, "人机验证答错次数超限"
                )
            else:
                await event.send(event.chain_result([
                At(qq=sender_id),
                Plain(f" ❌ 答案错误，还剩 {remaining} 次机会"),
            ]))
            # 终止事件传播：避免 @Bot 的验证消息继续触发 LLM 调用
            event.stop_event()

    async def _join_verify_timeout_kick(
        self, group_id: str, user_id: str, timeout: int
    ):
        """超时未通过验证则移出群聊；若剩余时间少于 1 分钟仍未答对，先 @新人 提醒并重发题目。

        每次唤醒先检查人机验证总开关：开关已关闭则静默取消本次验证（不提醒、不踢人）。
        """
        # 若总时限大于 60 秒，在剩余 60 秒时发送提醒并重发题目
        if timeout > 60:
            await asyncio.sleep(timeout - 60)
            if not self.config.get("enable_join_verify", False):
                self._join_verify_state.pop((group_id, user_id), None)
                return  # 开关已关闭：静默取消，不提醒
            state = self._join_verify_state.get((group_id, user_id))
            if state is None:
                return  # 用户已通过验证或被清理，无需提醒
            try:
                await state["event"].send(state["event"].chain_result([
                    At(qq=user_id),
                    Plain(
                        f" ⏰ 还剩 1 分钟，请尽快完成人机验证：请计算 {state['expr']} = ?"
                    ),
                ]))
                logger.info(
                    f"{LOG_PREFIX} 人机验证剩余1分钟提醒：用户 {user_id} 在群 {group_id}，重发题目 {state['expr']} = ?"
                )
            except Exception as e:
                logger.error(f"{LOG_PREFIX} 发送人机验证剩余时间提醒失败: {e}")
            await asyncio.sleep(60)
        else:
            await asyncio.sleep(timeout)

        # 超时未通过验证 → 移出群聊（踢人前再检查一次开关，关闭则静默取消，杜绝幽灵踢人）
        if not self.config.get("enable_join_verify", False):
            self._join_verify_state.pop((group_id, user_id), None)
            return
        state = self._join_verify_state.pop((group_id, user_id), None)
        if state is None:
            return
        await self._kick_join_verify_user(
            state["event"], group_id, user_id, "人机验证超时未通过"
        )

    def _cancel_all_join_verify(self) -> int:
        """取消所有进行中的人机验证任务并清空状态（人机验证开关关闭 / 插件停用时调用）。

        Returns:
            取消的任务数量。
        """
        count = 0
        for key, state in list(self._join_verify_state.items()):
            task = state.get("task")
            if task and not task.done():
                task.cancel()
            self._join_verify_state.pop(key, None)
            count += 1
        if count:
            logger.info(f"{LOG_PREFIX} 已取消 {count} 个进行中的人机验证任务")
        return count

    async def terminate(self):
        """插件卸载/停用时的清理钩子：取消所有进行中的人机验证任务，杜绝幽灵踢人。"""
        try:
            self._cancel_all_join_verify()
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 插件停用清理人机验证任务异常: {e}")

    async def _kick_join_verify_user(
        self, event: AstrMessageEvent, group_id: str, user_id: str, reason: str
    ):
        """将未通过人机验证的用户移出群聊：先 @用户 发送提示（10 秒后移除），再执行踢出。
        记录一次失败次数，达到上限则自动拉入入群黑名单，并通过 OneBot API 真正拉入群聊黑名单。
        """
        # 记录人机验证失败次数，获取剩余机会与拉黑状态
        info = await self._record_join_verify_failure(user_id)
        remaining = info["remaining"]
        blacklisted = info["blacklisted"]

        # 构造提示消息
        msg = "很抱歉，你未在规定时间或次数内完成人机验证，你将在10秒后被移除群聊"
        if remaining >= 0:  # 已设置失败次数上限
            if blacklisted or remaining <= 0:
                msg = "很抱歉，你未在规定时间或次数内完成人机验证，你的入群次数已用完，你将在10秒后被移除群聊并拉入黑名单！"
            else:
                msg += f"，你还有 {remaining} 次申请入群机会"

        # 先发送提示（@用户）
        try:
            await event.send(event.chain_result([
                At(qq=user_id),
                Plain(f" {msg}"),
            ]))
            logger.info(
                f"{LOG_PREFIX} 人机验证失败提示已发送：用户 {user_id} 在群 {group_id}（{msg[:30]}...）"
            )
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 发送人机验证失败提示失败: {e}")

        # 等待 10 秒后真正执行踢出
        await asyncio.sleep(10)

        try:
            await call_onebot_action(
                event,
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                # 达到上限 → reject_add_request=True，QQ 侧真正拉黑（拒绝再次申请）；否则普通踢出
                reject_add_request=blacklisted
            )
            if blacklisted:
                logger.info(
                    f"{LOG_PREFIX} 人机验证失败移出群聊并拉黑：用户 {user_id} 移出群 {group_id}（{reason}，已拉入QQ群黑名单）"
                )
            else:
                logger.info(f"{LOG_PREFIX} 人机验证失败移出群聊：用户 {user_id} 移出群 {group_id}（{reason}）")
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 人机验证移出群聊失败：用户 {user_id} → 群 {group_id}，错误: {e}")

    # ============ 入群黑名单（持久化） ============

    def _load_join_verify_blacklist(self) -> dict:
        """加载入群黑名单数据"""
        if not self._verify_blacklist_file:
            return {}
        try:
            if os.path.exists(self._verify_blacklist_file):
                with open(self._verify_blacklist_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 加载入群黑名单失败: {e}")
        return {}

    def _save_join_verify_blacklist(self, data: dict) -> None:
        """保存入群黑名单数据"""
        if not self._verify_blacklist_file:
            return
        try:
            with open(self._verify_blacklist_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 保存入群黑名单失败: {e}")

    def _is_join_verify_blacklisted(self, user_id: str) -> bool:
        """判断用户是否已在入群黑名单中"""
        data = self._load_join_verify_blacklist()
        rec = data.get(str(user_id), {})
        return bool(rec.get("blacklisted", False))

    async def _record_join_verify_failure(self, user_id: str) -> dict:
        """记录一次人机验证失败；累计达到上限后自动拉入入群黑名单。

        Returns:
            字典：{"failures": 累计失败次数, "remaining": 剩余机会次数（-1 表示未设置次数限制）, "blacklisted": 是否已拉黑}
        """
        try:
            max_failures = int(self.config.get("join_verify_max_failures", 0) or 0)
        except (ValueError, TypeError):
            max_failures = 0
        if max_failures <= 0:
            # 未启用次数限制：仍记录失败次数（仅统计），不自动拉黑、剩余不限；
            # 手动拉黑的用户保持拉黑状态
            data = self._load_join_verify_blacklist()
            rec = data.setdefault(str(user_id), {"failures": 0, "blacklisted": False})
            rec["failures"] = int(rec.get("failures", 0)) + 1
            if rec.get("blacklisted"):
                rec["remaining"] = 0
                self._save_join_verify_blacklist(data)
                return {"failures": rec["failures"], "remaining": 0, "blacklisted": True}
            rec["remaining"] = -1
            self._save_join_verify_blacklist(data)
            return {"failures": rec["failures"], "remaining": -1, "blacklisted": False}

        data = self._load_join_verify_blacklist()
        rec = data.setdefault(
            str(user_id),
            {"failures": 0, "blacklisted": False, "remaining": max_failures},
        )
        rec["failures"] = int(rec.get("failures", 0)) + 1  # 失败次数：纯累计统计
        # 剩余次数独立管理：新用户初始=上限；每次失败扣 1；已拉黑恒为 0
        if rec.get("blacklisted"):
            rec["remaining"] = 0
        else:
            rec["remaining"] = max(0, int(rec.get("remaining", max_failures)) - 1)
        # 剩余机会用完 → 拉黑
        if rec.get("blacklisted") or rec["remaining"] <= 0:
            rec["blacklisted"] = True
            rec["remaining"] = 0
            logger.info(
                f"{LOG_PREFIX} 用户 {user_id} 人机验证失败（累计 {rec['failures']} 次），剩余机会用完，已拉入入群黑名单"
            )
            self._save_join_verify_blacklist(data)
            return {"failures": rec["failures"], "remaining": 0, "blacklisted": True}
        logger.info(
            f"{LOG_PREFIX} 用户 {user_id} 人机验证失败（累计 {rec['failures']} 次，剩余 {rec['remaining']} 次）"
        )
        self._save_join_verify_blacklist(data)
        return {"failures": rec["failures"], "remaining": rec["remaining"], "blacklisted": False}

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_add_request(self, event: AstrMessageEvent):
        """
        监听加群申请事件，按配置的等级门槛与入群白词自动处理入群申请。
        - 自动拒绝（两个独立开关；开启「拒绝入群双重验证」（auto_reject_dual_verify）后需两个拒绝条件同时满足才拒绝，关闭则任一满足即拒绝）：
          - auto_reject_below_level 开启且申请人 QQ 等级低于门槛时，自动拒绝入群。
          - auto_reject_whitelist_miss 开启且申请人验证信息未命中任何入群白词时，自动拒绝入群。
        - 自动同意：
          - 仅等级渠道（auto_accept_group_request）：等级达标（或无门槛）→ 自动同意，白词不参与。
          - 仅白词渠道（auto_accept_whitelist）：验证信息命中任一白词 → 自动同意，不受等级门槛影响。
          - 双开关同时开启时：开启「入群双重审核」（auto_accept_dual_verify）→ 需「白词命中（或未配置白词）」且「等级达标（或无门槛）」同时满足才自动同意（AND）；关闭 → 任一满足即自动同意（OR）。
        - 其余情况保持人工审核（不干预）。仅处理 add 类型申请；黑名单群不生效。
        """
        raw_msg = getattr(event.message_obj, "raw_message", {})
        raw_dict = raw_msg if isinstance(raw_msg, dict) else {}
        if not raw_dict:
            return

        # 仅处理加群申请（request/group/add）
        if not (raw_dict.get("post_type") == "request"
                and raw_dict.get("request_type") == "group"
                and raw_dict.get("sub_type") == "add"):
            return

        group_id = raw_dict.get("group_id")
        user_id = raw_dict.get("user_id")
        flag = raw_dict.get("flag")
        if not group_id or not user_id or not flag:
            return

        # 黑名单群不自动处理
        if is_blacklisted_group(self.config, group_id):
            return

        # 入群黑名单用户：直接自动拒绝（仅人机验证开启时生效；关闭则整套人机防线停用）
        if self.config.get("enable_join_verify", False) and self._is_join_verify_blacklisted(user_id):
            try:
                await call_onebot_action(
                    event,
                    "set_group_add_request",
                    flag=str(flag),
                    sub_type="add",
                    approve=False,
                    reason="您因多次未通过人机验证，已被拉入群黑名单"
                )
                logger.info(
                    f"{LOG_PREFIX} 自动拒绝入群（黑名单用户）：用户 {user_id} 申请加入群 {group_id}"
                )
            except Exception as e:
                logger.error(f"{LOG_PREFIX} 自动拒绝入群失败：用户 {user_id} → 群 {group_id}，错误: {e}")
            return

        # 读取等级门槛并安全转换（防御非法配置值；填小数时直接截断保留整数部分，如 30.5 → 30；异常时按 0 处理 = 不限制等级）
        try:
            raw_level = self.config.get("auto_accept_group_level", 0) or 0
            min_level = int(float(raw_level)) if float(raw_level) >= 0 else 0
        except (ValueError, TypeError):
            logger.warning(
                f"{LOG_PREFIX} auto_accept_group_level 配置值非法，已按 0（不限制等级）处理"
            )
            min_level = 0
        reject_enabled = self.config.get("auto_reject_below_level", False)
        reject_whitelist_miss_enabled = self.config.get("auto_reject_whitelist_miss", False)
        accept_enabled = self.config.get("auto_accept_group_request", False)
        accept_whitelist_enabled = self.config.get("auto_accept_whitelist", False)
        accept_dual_enabled = self.config.get("auto_accept_dual_verify", False)  # 入群双重审核：双同意开关同时开启时的 AND/OR 控制
        reject_dual_enabled = self.config.get("auto_reject_dual_verify", False)  # 拒绝入群双重验证：双拒绝开关同时开启时的 AND/OR 控制
        # 读取白词列表并做类型防御（面板配置为 list；若误配成字符串则视为单个白词，其他异常类型按空处理）
        raw_whitelist = self.config.get("auto_accept_group_whitelist") or []
        if not isinstance(raw_whitelist, (list, tuple, set)):
            raw_whitelist = [raw_whitelist] if isinstance(raw_whitelist, str) and raw_whitelist.strip() else []
        whitelist = [str(w).strip() for w in raw_whitelist if w is not None and str(w).strip()]

        # 读取申请人填写的验证信息
        comment = str(raw_dict.get("comment", "") or "")

        # 主动查询申请人的 QQ 等级（NapCat 加群申请事件不推送 level 字段，
        # 需调用 get_stranger_info 查询，与本地 qqadmin 插件方式一致）
        level = 0
        try:
            info = await call_onebot_action(
                event,
                "get_stranger_info",
                user_id=int(user_id)
            )
            if isinstance(info, dict):
                if info.get("isHideQQLevel"):
                    level = 0  # 用户隐藏了 QQ 等级，视为未知
                else:
                    level = int(info.get("qqLevel") or info.get("level") or 0)
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 获取申请人 {user_id} QQ等级失败: {e}")
            level = 0
        logger.info(
            f"{LOG_PREFIX} 收到加群申请（用户 {user_id} → 群 {group_id}）：申请人QQ等级={level}，验证信息='{comment}'"
        )

        level_requirement = min_level > 0
        whitelist_requirement = len(whitelist) > 0
        level_passed = (level >= min_level) if level_requirement else True
        whitelist_passed = any(w in comment for w in whitelist) if whitelist_requirement else True

        # 1/2. 自动拒绝判定（两个独立拒绝开关）
        #    - 开启「拒绝入群双重验证」（reject_dual_enabled）→ AND：两个拒绝条件同时满足才自动拒绝，只满足一个时不拦截
        #    - 关闭（默认）→ OR：任一拒绝条件满足即自动拒绝（白词未命中优先于等级未达标）
        wl_reject = whitelist_requirement and reject_whitelist_miss_enabled and not whitelist_passed
        lv_reject = level_requirement and reject_enabled and not level_passed
        if wl_reject or lv_reject:
            if reject_dual_enabled:
                # AND：需「白词未命中」且「等级未达标」同时满足才拒绝
                if wl_reject and lv_reject:
                    try:
                        await call_onebot_action(
                            event,
                            "set_group_add_request",
                            flag=str(flag),
                            sub_type="add",
                            approve=False,
                            reason="入群验证信息未包含指定白词且QQ等级未达到入群门槛"
                        )
                        logger.info(
                            f"{LOG_PREFIX} 自动拒绝入群：用户 {user_id} 申请加入群 {group_id}（拒绝双重验证：白词未命中 且 QQ等级 {level} < 门槛 {min_level}）"
                        )
                    except Exception as e:
                        logger.error(f"{LOG_PREFIX} 自动拒绝入群失败：用户 {user_id} → 群 {group_id}，错误: {e}")
                    return
                # 只满足一个拒绝条件 → 不拦截，继续走同意流程
            else:
                # OR：任一满足即拒绝
                if wl_reject:
                    try:
                        await call_onebot_action(
                            event,
                            "set_group_add_request",
                            flag=str(flag),
                            sub_type="add",
                            approve=False,
                            reason="入群验证信息未包含指定白词"
                        )
                        logger.info(
                            f"{LOG_PREFIX} 自动拒绝入群：用户 {user_id} 申请加入群 {group_id}（验证信息未命中入群白词）"
                        )
                    except Exception as e:
                        logger.error(f"{LOG_PREFIX} 自动拒绝入群失败：用户 {user_id} → 群 {group_id}，错误: {e}")
                    return
                if lv_reject:
                    try:
                        await call_onebot_action(
                            event,
                            "set_group_add_request",
                            flag=str(flag),
                            sub_type="add",
                            approve=False,
                            reason=f"QQ等级未达到入群门槛（要求不低于{min_level}级）"
                        )
                        logger.info(
                            f"{LOG_PREFIX} 自动拒绝入群：用户 {user_id} 申请加入群 {group_id}（QQ等级 {level} < 门槛 {min_level}）"
                        )
                    except Exception as e:
                        logger.error(f"{LOG_PREFIX} 自动拒绝入群失败：用户 {user_id} → 群 {group_id}，错误: {e}")
                    return

        # 3. 自动同意判定：
        #    - 双开关同时开启 + 「入群双重审核」开启 → AND：白词命中（或未配置白词）且 等级达标（或无门槛）同时满足才自动同意
        #    - 双开关同时开启 + 「入群双重审核」关闭（默认）→ OR：白词命中 或 等级达标，任一满足即自动同意
        #    - 仅开等级开关 → 等级渠道独立：等级达标（或无门槛）即自动同意，白词不参与
        #    - 仅开白词开关 → 白词渠道独立：验证信息命中白词即自动同意，等级门槛不参与
        level_ok = (not level_requirement) or level_passed
        whitelist_ok = (not whitelist_requirement) or whitelist_passed

        if accept_enabled and accept_whitelist_enabled:
            lv_desc = "无等级门槛" if not level_requirement else (f"QQ等级{level}≥{min_level}" if level_passed else f"QQ等级{level}<{min_level}")
            wl_desc = "未配置白词" if not whitelist_requirement else ("白词命中" if whitelist_passed else "白词未命中")
            if accept_dual_enabled:
                agree_flag = level_ok and whitelist_ok
                reason = f"双重审核（{wl_desc}且{lv_desc}）"
            else:
                agree_flag = level_ok or (whitelist_requirement and whitelist_passed)
                reason = f"同意审核（{wl_desc}或{lv_desc}）"
        elif accept_enabled:
            agree_flag = level_ok
            reason = f"等级渠道（{'无等级门槛，全部放行' if not level_requirement else (f'QQ等级{level}≥{min_level}' if level_passed else f'QQ等级{level}<{min_level}')}）"
        elif accept_whitelist_enabled:
            agree_flag = whitelist_requirement and whitelist_passed
            reason = "白词渠道（验证信息命中白词）"
        else:
            agree_flag = False
            reason = "未开启任何自动同意开关"

        if not agree_flag:
            logger.info(
                f"{LOG_PREFIX} 收到加群申请（用户 {user_id} → 群 {group_id}），"
                f"自动同意条件未满足（{reason}），保持人工审核。"
            )
            return

        # 4. 自动同意入群
        try:
            await call_onebot_action(
                event,
                "set_group_add_request",
                flag=str(flag),
                sub_type="add",
                approve=True
            )
            logger.info(
                f"{LOG_PREFIX} 自动同意入群：用户 {user_id} 申请加入群 {group_id}（{reason}）"
            )
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 自动同意入群失败：用户 {user_id} → 群 {group_id}，错误: {e}")

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
        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="at_all_members")
        if not ok:
            return err_msg

        # 2. 预检 Bot 账号自身在群内是否具备管理员/群主权限
        bot_role = await get_bot_role_in_group(event, group_id)
        if bot_role and bot_role not in {"owner", "admin"}:
            return f"操作失败：Bot 账号自身在群 ({group_id}) 内缺乏管理员或群主权限（当前角色为普通成员）。请先将 Bot 设为群管理员后再试。"

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
        cleaned_target = clean_qq_number(target_user)
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="ban_group_member")
        if not ok:
            return err_msg

        duration_seconds = max(0, duration_minutes * 60)
        action_name = "禁言" if duration_seconds > 0 else "解除禁言"

        try:
            await call_onebot_action(
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
            logger.error(f"{LOG_PREFIX} 执行 {action_name} 失败: {err_str}")
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
        cleaned_target = clean_qq_number(target_user)
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="kick_group_member")
        if not ok:
            return err_msg

        try:
            await call_onebot_action(
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
            retcode = getattr(e, "retcode", None)
            result = getattr(e, "result", None) or {}
            err_msg = result.get("message", "") if isinstance(result, dict) else ""
            logger.error(f"{LOG_PREFIX} 执行踢人失败: retcode={retcode}, message={err_msg}, {err_str}")
            # 降级判断：NapCat 对 set_group_kick 的 reject_add_request 附带操作实现不完整，
            # 常出现「踢人已生效但整体返回 retcode=100」的情况。
            # 若确认踢人已生效，降级返回「踢人成功但附加操作可能未完全成功」，不再误报整体失败。
            lowered = False
            if retcode == 100:
                if reject_add_request:
                    # 拉黑踢人场景：NapCat 已知问题，踢出通常已生效
                    lowered = True
                elif any(kw in err_msg for kw in ("移出", "移除", "踢出", "已移出", "已移除", "success", "成功")):
                    # 普通踢人：报错信息含「已移出」等特征，说明踢人已生效
                    lowered = True
            if lowered:
                block_info = "（已同步拒绝后续加群申请）" if reject_add_request else ""
                reason_info = f"，原因：{reason}" if reason else ""
                return (
                    f"成功：以 [{auth_role}] 身份已将成员 ({cleaned_target}) 移除群聊{block_info}{reason_info}。"
                    f"（注意：NapCat 返回 retcode={retcode}，踢人已生效，但附加操作可能未完全成功，"
                    f"详情请查看 NapCat 日志）"
                )
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
        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="delete_group_message")
        if not ok:
            return err_msg

        target_ids: List[int] = []

        if message_id.strip():
            raw_tokens = re.split(r"[,;\s]+", message_id.strip())
            for tok in raw_tokens:
                cleaned = clean_qq_number(tok)
                if cleaned:
                    target_ids.append(int(cleaned))

        if not target_ids:
            reply_id = extract_reply_message_id(event)
            if reply_id:
                target_ids.append(int(reply_id))

        if not target_ids:
            return "操作失败：未提供要撤回的 message_id，且当前消息未回复/引用任何特定消息。"

        # 硬性截断：单次最多批量撤回 10 条
        target_ids = target_ids[:10]

        success_count = 0
        fail_errors = []

        for mid in target_ids:
            try:
                await call_onebot_action(
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
        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="set_group_essence_message")
        if not ok:
            return err_msg

        target_id = clean_qq_number(message_id) if message_id.strip() else ""

        if not target_id:
            target_id = extract_reply_message_id(event)

        if not target_id:
            return "操作失败：未指定 message_id，且当前消息未回复/引用任何目标消息。"

        action_name = "设置群精华" if enable else "移除群精华"
        api_action = "set_essence_msg" if enable else "delete_essence_msg"

        try:
            await call_onebot_action(
                event,
                api_action,
                message_id=int(target_id)
            )
            return f"成功：以 [{auth_role}] 身份成功将消息 (ID: {target_id}) {action_name}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"{LOG_PREFIX} 执行 {action_name} 失败: {err_str}")
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
        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="get_group_msg_history")
        if not ok:
            return err_msg

        count = max(1, min(100, count))

        try:
            kwargs = {
                "group_id": int(group_id),
                "count": count
            }
            if message_seq.strip():
                cleaned_seq = clean_qq_number(message_seq)
                if cleaned_seq:
                    kwargs["message_seq"] = int(cleaned_seq)

            res = await call_onebot_action(event, "get_group_msg_history", **kwargs)

            formatted = format_group_msg_history(res)
            if not formatted:
                return f"未获取到群 ({group_id}) 的历史消息记录。"

            line_count = formatted.count("\n") + 1
            return f"获取群 ({group_id}) 最近 {line_count} 条历史消息成功：\n{formatted}"

        except Exception as e:
            err_str = str(e)
            logger.error(f"{LOG_PREFIX} 执行 get_group_msg_history 失败: {err_str}")
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
        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="set_group_whole_ban")
        if not ok:
            return err_msg

        action_name = "开启全员禁言" if enable else "解除全员禁言"

        try:
            await call_onebot_action(
                event,
                "set_group_whole_ban",
                group_id=int(group_id),
                enable=enable
            )
            return f"成功：以 [{auth_role}] 身份成功为群 ({group_id}) {action_name}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"{LOG_PREFIX} 执行 {action_name} 失败: {err_str}")
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
        cleaned_target = clean_qq_number(target_user)
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="set_group_card")
        if not ok:
            return err_msg

        action_desc = f"修改群名片为 '{card}'" if card else "清空群名片"

        try:
            await call_onebot_action(
                event,
                "set_group_card",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                card=card
            )
            return f"成功：以 [{auth_role}] 身份成功为成员 ({cleaned_target}) {action_desc}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"{LOG_PREFIX} 修改群名片失败: {err_str}")
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
        cleaned_target = clean_qq_number(target_user)
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="set_group_special_title")
        if not ok:
            return err_msg

        duration_seconds = -1 if duration_days <= 0 else duration_days * 86400

        try:
            await call_onebot_action(
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
            logger.error(f"{LOG_PREFIX} 设置专属头衔失败: {err_str}")
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
        cleaned_target = clean_qq_number(target_user)
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="set_group_admin")
        if not ok:
            return err_msg

        action_name = "设置群管理员" if enable else "取消群管理员"

        try:
            await call_onebot_action(
                event,
                "set_group_admin",
                group_id=int(group_id),
                user_id=int(cleaned_target),
                enable=enable
            )
            return f"成功：以 [{auth_role}] 身份为成员 ({cleaned_target}) {action_name}。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"{LOG_PREFIX} 执行 {action_name} 失败: {err_str}")
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

        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="set_group_name")
        if not ok:
            return err_msg

        try:
            await call_onebot_action(
                event,
                "set_group_name",
                group_id=int(group_id),
                group_name=group_name.strip()
            )
            return f"成功：以 [{auth_role}] 身份将群聊名称修改为 '{group_name.strip()}'。"
        except Exception as e:
            err_str = str(e)
            logger.error(f"{LOG_PREFIX} 修改群名称失败: {err_str}")
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
        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="send_group_notice")
        if not ok:
            return err_msg

        action = action.strip().lower() if action else "publish"

        # 检查 Bot 自身是否具备群管理员/群主身份（发布与删除公告需要权限）
        if action in {"publish", "delete"}:
            bot_role = await get_bot_role_in_group(event, group_id)
            if bot_role and bot_role not in {"owner", "admin"}:
                action_cn = "发布" if action == "publish" else "删除"
                return f"操作失败：Bot 账号自身在群 ({group_id}) 内缺乏管理员或群主权限（当前角色为普通成员）。{action_cn}群公告要求 Bot 账号必须具备群管理员或群主身份。"

        # 读取群公告
        if action == "get":
            try:
                res = await call_onebot_action(
                    event,
                    "_get_group_notice",
                    group_id=int(group_id)
                )
                return f"成功：以 [{auth_role}] 身份查询到群 ({group_id}) 的公告列表：\n{res}"
            except Exception as e:
                err_str = str(e)
                logger.error(f"{LOG_PREFIX} 读取群公告失败: {err_str}")
                return f"读取群公告失败，API 错误：{err_str}"

        # 删除群公告
        if action == "delete":
            if not notice_id.strip():
                return "操作失败：删除群公告需要提供要删除的公告 ID（notice_id）。"
            try:
                await call_onebot_action(
                    event,
                    "_del_group_notice",
                    group_id=int(group_id),
                    notice_id=notice_id.strip()
                )
                return f"成功：以 [{auth_role}] 身份删除了群 ({group_id}) 中 ID 为 '{notice_id.strip()}' 的群公告。"
            except Exception as e:
                err_str = str(e)
                logger.error(f"{LOG_PREFIX} 删除群公告失败: {err_str}")
                return f"删除群公告失败，API 错误：{err_str}"

        # 默认：发布群公告
        if not content.strip():
            return "操作失败：群公告文案内容不能为空。"

        try:
            await call_onebot_action(
                event,
                "_send_group_notice",
                group_id=int(group_id),
                content=content.strip()
            )
            return f"成功：以 [{auth_role}] 身份在群 ({group_id}) 中发布了全新群公告。"
        except Exception:
            try:
                await call_onebot_action(
                    event,
                    "send_group_notice",
                    group_id=int(group_id),
                    content=content.strip()
                )
                return f"成功：以 [{auth_role}] 身份在群 ({group_id}) 中发布了全新群公告。"
            except Exception as e2:
                err_str = str(e2)
                logger.error(f"{LOG_PREFIX} 发布群公告失败: {err_str}")
                return f"发布群公告失败，API 错误：{err_str}"

    @llm_tool(name="get_group_info")
    async def get_group_info(
        self,
        event: AstrMessageEvent
    ) -> str:
        """
        在 QQ 群聊中获取当前群聊的详细信息（群名称、群主 QQ、成员数、最大容量等）。当需要了解群基础信息时调用（工具内部会自动校验调用者权限）。
        """
        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="get_group_info")
        if not ok:
            return err_msg

        try:
            res = await call_onebot_action(
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
            logger.error(f"{LOG_PREFIX} 执行 get_group_info 失败: {err_str}")
            return f"获取群信息 API 出错：{err_str}"

    @llm_tool(name="get_group_member_list")
    async def get_group_member_list(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        sort_by_join_time: bool = False,
        sort_by_last_sent_time: bool = False,
        sort_by_group_level: bool = False,
        sort_oldest_first: bool = False,
        summary_only: bool = False
    ) -> str:
        """
        在 QQ 群聊中获取全员列表或根据关键词（昵称/名片/QQ号）搜索群成员，或按进群时间、最近发言时间、群等级进行多维度排序，或统计各群等级人数分布。列表与关键词搜索默认展示前 30 人，超出时附「… 等共 N 人」总数提示。当需要查找某群员信息、统计全员、查看最新进群新人、高/低群等级成员、按时间/等级升降序排列，或统计“某个群等级有多少人”时调用（工具内部会自动校验调用者权限）。

        Args:
            keyword (str, optional): 搜索关键词，支持匹配昵称、群名片或QQ号。若为空则默认列出前 30 名成员。
            sort_by_join_time (bool, optional): 是否按进群时间排序。当用户询问“进群的人”、“加群的新人/老人”或按入群时间查看时设置为 True。默认 False。
            sort_by_last_sent_time (bool, optional): 是否按最近发言时间排序。当用户询问“最近谁发言了”、“最久没发言的人”、“按发言时间查看”时设置为 True。默认 False。
            sort_by_group_level (bool, optional): 是否按群等级排序。当用户询问“群等级最高/最低的人”、“按群等级排序”时设置为 True。默认 False。
            sort_oldest_first (bool, optional): 是否升序排序（从旧到新 / 从低到高）。当用户询问“最久没发言”、“最低群等级”、“最早进群”、“从小到大/升序”时设置为 True。默认 False（即默认降序：最新/最高）。
            summary_only (bool, optional): 是否仅统计各群等级人数分布（遍历全部成员，不受 30 人展示截断影响）。当用户询问“某个群等级有多少人”、“群等级分布/人数统计”时设置为 True。开启后忽略 keyword 与排序参数。默认 False。
        """
        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="get_group_member_list")
        if not ok:
            return err_msg

        try:
            res = await call_onebot_action(
                event,
                "get_group_member_list",
                group_id=int(group_id),
                no_cache=True
            )
            if not isinstance(res, list):
                return f"获取群 ({group_id}) 成员列表数据失败。"

            # 统计模式：遍历全部成员统计各群等级人数（不受 30 人展示截断影响）
            if summary_only:
                summary, total = summarize_group_level(res)
                if not summary:
                    return f"群 ({group_id}) 群等级人数统计失败或无有效成员数据。"
                return f"群 ({group_id}) 群等级人数统计（共 {total} 人）：\n" + summary
            formatted, total_found = format_member_list(
                res,
                keyword=keyword,
                sort_by_join_time=sort_by_join_time,
                sort_by_last_sent_time=sort_by_last_sent_time,
                sort_by_group_level=sort_by_group_level,
                sort_oldest_first=sort_oldest_first
            )
            if not formatted:
                return f"在群 ({group_id}) 中未找到匹配 '{keyword}' 的群成员。"

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

            return f"群 ({group_id}) 成员列表{kw_desc}：\n" + formatted + suffix

        except Exception as e:
            err_str = str(e)
            logger.error(f"{LOG_PREFIX} 执行 get_group_member_list 失败: {err_str}")
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
        cleaned_target = clean_qq_number(target_user)
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="group_poke")
        if not ok:
            return err_msg

        try:
            await call_onebot_action(
                event,
                "group_poke",
                group_id=int(group_id),
                user_id=int(cleaned_target)
            )
            return f"成功：以 [{auth_role}] 身份对成员 ({cleaned_target}) 执行了“戳一戳”操作。"
        except Exception:
            try:
                await call_onebot_action(
                    event,
                    "friend_poke",
                    user_id=int(cleaned_target)
                )
                return f"成功：以 [{auth_role}] 身份对成员 ({cleaned_target}) 执行了“戳一戳”操作。"
            except Exception as e2:
                err_str = str(e2)
                logger.error(f"{LOG_PREFIX} 执行 group_poke 失败: {err_str}")
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
        cleaned_target = clean_qq_number(target_user)
        if not cleaned_target:
            return f"操作失败：无法从输入 '{target_user}' 中解析出有效的 QQ 号。"

        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="get_group_member_info")
        if not ok:
            return err_msg

        try:
            res = await call_onebot_action(
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
                level = res.get("level", 0)
                shut_up_ts = res.get("shut_up_timestamp", 0)

                join_time_str = format_timestamp(res.get("join_time", 0), "%Y-%m-%d %H:%M:%S") or "未知"
                last_sent_str = format_timestamp(res.get("last_sent_time", 0), "%Y-%m-%d %H:%M:%S") or "从未发言"

                ban_until_str = "未禁言"
                if shut_up_ts and shut_up_ts > time.time():
                    ban_until_str = format_timestamp(shut_up_ts, "%Y-%m-%d %H:%M:%S")

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
            logger.error(f"{LOG_PREFIX} 执行 get_group_member_info 失败: {err_str}")
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
        ok, auth_role, group_id, err_msg = await check_permission(event, self.config, self.admins_id, tool_name="kick_inactive_members")
        if not ok:
            return err_msg

        days = max(1, days)

        try:
            members = await call_onebot_action(
                event,
                "get_group_member_list",
                group_id=int(group_id),
                no_cache=True
            )
            if not isinstance(members, list):
                return "操作失败：无法获取群成员列表数据。"

            inactive_members = scan_inactive_members(members, days, max_group_level)

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
                    await call_onebot_action(
                        event,
                        "set_group_kick",
                        group_id=int(group_id),
                        user_id=int(item["user_id"]),
                        reject_add_request=False
                    )
                    success_cnt += 1
                except Exception as e:
                    fail_cnt += 1
                    logger.error(f"{LOG_PREFIX} 清理潜水成员 {item['user_id']} 失败: {e}")

            return f"成功：以 [{auth_role}] 身份执行潜水成员清理完成！成功移出 {success_cnt} 人，失败 {fail_cnt} 人。"

        except Exception as e:
            err_str = str(e)
            logger.error(f"{LOG_PREFIX} 执行 kick_inactive_members 失败: {err_str}")
            return f"清理潜水成员 API 出错：{err_str}"
