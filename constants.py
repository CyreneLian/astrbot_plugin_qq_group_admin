"""
AstrBot QQ群大模型管理工具 - 常量定义模块

功能描述：
- 集中管理插件全局常量、正则表达式、工具中文名映射与权限要求映射
- 供 main.py / utils.py / permission.py 共享引用

作者: 往昔的涟漪
版本: 2.2.0
日期: 2026-08-10
"""

import re
from typing import Dict, Pattern

# 日志前缀
LOG_PREFIX = "[QQGroupAdmin]"

# 正则表达式：用于匹配符合规范的艾特标签，例如 [at:123456] 或 [at:all]
AT_PATTERN: Pattern = re.compile(r"\[at:(\d+|all)\]")

# 默认允许普通群友调用的工具列表（中文名）
DEFAULT_MEMBER_ALLOWED_TOOLS = [
    "读取历史消息",
    "获取群信息",
    "获取全员列表",
    "查询成员信息",
    "戳一戳"
]

# 工具英文名 -> 中文名映射（用于普通群友授权匹配）
TOOL_CN_MAP: Dict[str, str] = {
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

# 要求 Bot 账号自身具备群管理员/群主身份的工具
ADMIN_REQUIRED_TOOLS: Dict[str, str] = {
    "ban_group_member": "禁言/解禁",
    "kick_group_member": "踢人/拉黑",
    "delete_group_message": "撤回消息",
    "set_group_essence_message": "设置/取消精华",
    "set_group_whole_ban": "全体禁言",
    "set_group_card": "修改群名片",
    "set_group_name": "修改群名称",
    "kick_inactive_members": "清理潜水成员",
}

# 要求 Bot 账号自身具备群主身份的工具
OWNER_REQUIRED_TOOLS: Dict[str, str] = {
    "set_group_special_title": "设置专属头衔",
    "set_group_admin": "设置/取消管理员"
}

# 艾特成员功能注入的系统提示词
AT_INSTRUCTION = (
    "\n\n【艾特成员提示】\n"
    "当你想在回复中艾特（提及）某个群成员时，请在回复文本中插入格式为 [at:用户ID] 的标签。\n"
    "例如：你好[at:123456789]，关于你的问题...\n"
    "其中用户ID必须是纯数字，可以先调用 get_group_member_list 工具查询成员 QQ 号。"
)

# 戳一戳被动响应模板
POKE_PROMPT_TEMPLATE = (
    "【系统提示：{username} 刚在聊天中戳了戳你】\n"
    "请完全契合你的性格角色与灵魂，自然地回应这次“戳一戳”互动。"
)
