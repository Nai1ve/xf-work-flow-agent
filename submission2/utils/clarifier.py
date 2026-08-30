"""共享多轮澄清器：三个域共用的「信息不明确就追问」能力。

设计（2026-08-12 用户定案）：多轮澄清不是 leave/budget 各自抄一份、meeting
缺失的域能力，而是**所有域共用的通用能力**——query 信息不明确就追问。共享
循环只此一份，各域声明自己的槽规格（缺槽检测 / 提问语 / 答复解析）；跨槽依赖
（leave 的 reason 依赖 type_code、end_time 依赖 start_period）通过 missing/parse
可见已收集的 out 表达。

模拟器协议（env.reply，见 simulator/env.py `_simulate_user_reply`）：
- 发提问 → 命中 SLOT_PATTERNS[key] 触发词 → 返回 ``{resolved_slot, user_message}``
  （模拟用户按槽位答复）；槽名 = case dialogue_state.missing_slots。
- 发确认语 → 命中 CONFIRM_PATTERNS → 返回 ``{confirmed_action}``，把该动作从
  ``confirmation_required_before`` 解锁。
- 未命中 → ``{resolved_slot: None, user_message: fallback_reply}``。
触发词 / 槽名以模拟器为准，不在本模块重复维护。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ClarifySlot:
    """单个待澄清槽位的声明式规格。

    Attributes:
        key: 匹配 resolved_slot 的键。模拟器可能用多个槽名命中同一问题
            （budget 的 project 问题命中 project_name / project_code），用 keys 元组。
        question: 发给模拟用户的提问语，**必须包含** 模拟器 SLOT_PATTERNS[key]
            的触发词之一，否则 resolved_slot 不会命中（返回 fallback，本步白耗）。
        missing: (query_text, 已收集 out) -> 是否缺该槽。跨槽依赖在此表达
            （leave 的 reason 依赖 type_code、end_time 依赖 start_period）。
        parse: (user_message, 已收集 out) -> 采纳字段 dict；空 dict = 不采纳。
        keys: 额外可命中的 resolved_slot 键（合并到 key）。
    """

    key: str
    question: str
    missing: Callable[[str, dict[str, Any]], bool]
    parse: Callable[[str, dict[str, Any]], dict[str, Any]]
    keys: tuple[str, ...] = field(default=())

    def match_keys(self) -> tuple[str, ...]:
        return self.keys or (self.key,)


def clarify_slots(
    env: Any,
    text: str,
    specs: list[ClarifySlot],
) -> dict[str, Any]:
    """跑通用澄清循环，返回已收集槽位。

    逐槽按声明顺序：query 缺该槽 → env.reply(提问) → 校验 resolved_slot 命中 →
    parse 采纳。任一环不满足（env 无 reply / 槽不缺 / resolved_slot 未命中）即跳过，
    保留默认解析路径（不因追问失败而 block）。

    Args:
        env: IFTKEnv 受控代理（须暴露 reply）。
        text: 该单元负责的原文字句（缺槽检测输入）。
        specs: 本域按需声明的槽规格（列表顺序 = 提问顺序）。

    Returns:
        已收集槽位 dict（parse 产出并集）；未问 / 未解析成功的键缺省。
    """
    if not (hasattr(env, "reply") and callable(getattr(env, "reply"))):
        return {}
    out: dict[str, Any] = {}
    asked: set[str] = set()
    for spec in specs:
        # 一个 Task 内每个槽位最多问一次；如果环境返回没有命中的槽位或
        # 空答复，本轮只记录失败并继续其余独立槽位，不在同一轮自旋。
        if spec.key in asked:
            continue
        if not spec.missing(text, out):
            continue
        asked.add(spec.key)
        r = env.reply(spec.question)
        if not isinstance(r, dict):
            continue
        if r.get("resolved_slot") in spec.match_keys():
            message = str(r.get("user_message") or "")
            parsed = spec.parse(message, out)
            if parsed:
                out.update(parsed)
    return out
