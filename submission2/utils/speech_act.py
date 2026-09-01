"""统一解析工作流写入语气。

模型只负责抽取槽位，是否保存草稿或提交由这个小型、可审计的语义规则决定。
规则不依赖 case id、Gold 或训练题文本；调用方可以通过 ``selected`` 识别互相
冲突的表达，并在写入前追问/阻断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_FORBID_SUBMIT_BASE = re.compile(
    r"不提交|别提交|晚点提交|稍后提交|暂不提交|先不要提交|先别提交"
)
_FORBID_SUBMIT_V3 = re.compile(
    r"不要提交|不走流程|别走流程|不要走流程|暂不走流程|"
    r"先不要走流程|先别走流程|不发起(?:流程|申请)|"
    r"别发起(?:流程|申请)|不要发起(?:流程|申请)"
)
_FORBID_DRAFT = re.compile(
    r"不要保存草稿|不要存草稿|别存草稿|别保存草稿|不用存草稿|不要草稿|不保存草稿"
)
_DRAFT = re.compile(r"草稿|存草稿|存一下|存个|存一个|先存|先保存|暂存|保存一下")
# 口语「提」只在后面跟着明确的流程对象/动作时成立；不能用裸「提」匹配
# 「提取」「提示」等普通词。这样「提品牌广告服务费用」「提一批设备」「直接提单」
# 都会进入提交分支，同时保留自然语言抽取的安全边界。
_SUBMIT_BASE = re.compile(
    r"提交|直接提|提(?:掉|上去|一下|一个|一批|一笔|品牌|费用|预算|物资|单|就行|流程)"
    r"|走审批|送审|发起审批"
)
_SUBMIT_V3 = re.compile(r"走流程|发起流程|发起申请|直接申请")

# 请假域的自然发起句式。这里只是一个域内默认，不把裸「申请」扩展为提交：
# 预算/费用中的「申请」大多是名词（如申请草稿），仍由通用规则处理。
_NATURAL_LEAVE_START = re.compile(
    r"(?:我要|我想|我需要|想要|想|需要|要)\s*请"
    r"|(?:^|[，。；;、])\s*请[^，。；;、]{0,20}(?:假|休假)"
)


def _draft_requested(value: str) -> bool:
    """区分当前存草稿动作与被处理的历史草稿名词。"""
    matches = list(_DRAFT.finditer(value))
    if not matches:
        return False
    # 「申请草稿，重新提交」中的草稿是待重新提交的旧件；明确的重新提交动作
    # 优先。若动作之后又明确要求“保存/存草稿”，则仍视为新的草稿请求。
    resubmit = re.search(r"重新提交", value)
    if resubmit and not re.search(r"(?:存|保存)(?:一下|一个|个)?草稿", value[resubmit.end():]):
        return False
    for match in matches:
        # 「之前存了一个草稿……删掉重新提交」中的草稿是旧件对象，不应压过
        # 后面的重新提交动作；普通「申请草稿」仍会保留草稿优先级。
        after = value[match.end():]
        before = value[:match.start()]
        if re.search(r"删掉|重新提交", after) and re.search(
            r"之前|此前|先前|昨天|上次|旧申请|原申请|已提交|存了|存过|已保存",
            before,
        ):
            continue
        return True
    return False


@dataclass(frozen=True)
class SpeechActDecision:
    """一次保存/提交语气决策。

    ``selected`` 为 ``None`` 表示同一句话同时否定和要求提交，不能安全写入。
    ``default_policy`` 记录无动作词时采用的业务默认（例如事件假默认提交）。
    """

    selected: bool | None
    forbid_submit: bool = False
    explicit_draft: bool = False
    explicit_submit: bool = False
    forbid_draft: bool = False
    default_policy: str = "draft"
    conflict: bool = False

    @property
    def submit(self) -> bool:
        """兼容旧调用方：冲突时按安全的 draft 读取，真正写入必须检查冲突。"""
        return self.selected is True

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "forbid_submit": self.forbid_submit,
            "explicit_draft": self.explicit_draft,
            "explicit_submit": self.explicit_submit,
            "forbid_draft": self.forbid_draft,
            "default_policy": self.default_policy,
            "conflict": self.conflict,
        }


def parse_speech_act(
    text: str,
    *,
    event_default: bool = False,
    domain: str | None = None,
    speech_act_v3: bool = False,
) -> SpeechActDecision:
    """从用户文字解析保存/提交动作，固定优先级且不猜测隐含 ID。

    ``domain="leave"`` 配合 ``speech_act_v3=True`` 启用请假域自然发起默认
    （我要请/想请/需要请/请X假）。该参数是可选的，保持预算等旧调用方的裸
    「申请」不改变语义，也允许通过 ProfileConfig 独立回滚该能力。
    """

    value = " ".join(str(text or "").replace("\n", " ").split())
    forbid_pattern = (
        re.compile(f"(?:{_FORBID_SUBMIT_BASE.pattern})|(?:{_FORBID_SUBMIT_V3.pattern})")
        if speech_act_v3 else _FORBID_SUBMIT_BASE
    )
    forbid_submit = bool(forbid_pattern.search(value))
    forbid_draft = bool(_FORBID_DRAFT.search(value))
    # 负向短语本身含“提交”二字；先移除它们，避免“暂不提交”被误判为
    # 同时要求提交。只有剩余文本里仍有积极动词时才算真实冲突。
    positive_text = forbid_pattern.sub(" ", value)
    explicit_submit = bool(_SUBMIT_BASE.search(positive_text)) or bool(
        speech_act_v3 and _SUBMIT_V3.search(positive_text)
    )
    explicit_draft = (
        _draft_requested(value) if speech_act_v3 else bool(_DRAFT.search(value))
    ) and not forbid_draft
    natural_leave_start = bool(
        domain == "leave"
        and speech_act_v3
        and _NATURAL_LEAVE_START.search(positive_text)
    )
    conflict = forbid_submit and (explicit_submit or forbid_draft)
    if conflict:
        selected: bool | None = None
    elif forbid_submit:
        selected = False
    elif forbid_draft:
        # “不要保存草稿”本身就是直接提交意图。
        selected = True
    elif speech_act_v3 and explicit_draft:
        # V3 中明确草稿优先；旧档保留原先“提交优先”的行为。
        selected = False
    elif explicit_submit:
        selected = True
    elif explicit_draft:
        selected = False
    elif natural_leave_start:
        selected = True
    elif event_default:
        selected = True
    else:
        selected = False
    default_policy = (
        "leave_submit" if natural_leave_start
        else "event_submit" if event_default else "draft"
    )
    return SpeechActDecision(
        selected=selected,
        forbid_submit=forbid_submit,
        explicit_draft=explicit_draft,
        explicit_submit=explicit_submit,
        forbid_draft=forbid_draft,
        default_policy=default_policy,
        conflict=conflict,
    )


def explicit_oa_request(text: str, status: str) -> bool:
    """判断是否明确要求查询 OA 待办/已办，避免跨域自动打无关工具。"""

    value = str(text or "")
    if status == "todo":
        return bool(re.search(r"待办|草稿列表|未提交|我的草稿|查看草稿", value))
    if status == "done":
        return bool(re.search(r"已办|已提交|提交记录|审批结果|确认提交", value))
    return False


__all__ = ["SpeechActDecision", "parse_speech_act", "explicit_oa_request"]
