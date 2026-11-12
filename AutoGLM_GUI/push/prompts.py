"""Prompt templates for push-driven group messages."""

from __future__ import annotations


def build_group_message_prompt(
    *,
    group_name: str,
    message: str,
    mention_all: bool = True,
) -> str:
    """Build the natural-language instruction sent to the phone agent."""
    mention_step = (
        "2. 点击底部输入框，输入 @ 符号，在弹出的成员列表中选择「所有人」。\n"
        if mention_all
        else ""
    )
    send_step_number = 3 if mention_all else 2
    return (
        "打开微信，完成以下群消息发送任务：\n"
        f"1. 在微信搜索框中搜索群名「{group_name}」，从搜索结果中进入该群聊。\n"
        f"{mention_step}"
        f"{send_step_number}. 在输入框中输入下面的消息内容。必须逐字保留"
        "（包括所有换行、标点、空格和符号），不要修改、不要翻译、不要添加引号：\n"
        "<message>\n"
        f"{message}\n"
        "</message>\n"
        f"{send_step_number + 1}. 点击发送按钮，确认消息已发出。\n"
        f"{send_step_number + 2}. 发送成功后结束任务。\n"
    )
