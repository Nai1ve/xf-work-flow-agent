"""统一解析工作流写入语气。

模型只负责抽取槽位，是否保存草稿或提交由这个小型、可审计的语义规则决定。
规则不依赖 case id、Gold 或训练题文本；调用方可以通过 ``selected`` 识别互相
冲突的表达，并在写入前追问/阻断。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


_FORBID_SUBMIT = re.compile(r"不提交|别提交|晚点提交|稍后提交|暂不提交|先不要提交|先别提交")
_FORBID_DRAFT = re.compile(
    r"不要保存草稿|不要存草稿|别存草稿|别保存草稿|不用存草稿|不要草稿|不保存草稿"
)
_DRAFT = re.compile(r"草稿|存草稿|存一下|存个|存一个|先存|先保存|暂存|保存一下")
# 口语「提」只在后面跟着明确的流程对象/动作时成立；不能用裸「提」匹配
# 「提取」「提示」等普通词。这样「提品牌广告服务费用」「提一批设备」「直接提单」
# 都会进入提交分支，同时保留自然语言抽取的安全边界。
_SUBMIT = re.compile(
    r"提交|直接提|提(?:掉|上去|一下|一个|一批|一笔|品牌|费用|预算|物资|单|就行|流程)"
    r"|走审批|送审|发起审批"
)


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


def parse_speech_act(text: str, *, event_default: bool = False) -> SpeechActDecision:
    """从用户文字解析保存/提交动作，固定优先级且不猜测隐含 ID。"""

    value = " ".join(str(text or "").replace("\n", " ").split())
    forbid_submit = bool(_FORBID_SUBMIT.search(value))
    forbid_draft = bool(_FORBID_DRAFT.search(value))
    # 负向短语本身含“提交”二字；先移除它们，避免“暂不提交”被误判为
    # 同时要求提交。只有剩余文本里仍有积极动词时才算真实冲突。
    positive_text = _FORBID_SUBMIT.sub(" ", value)
    explicit_submit = bool(_SUBMIT.search(positive_text))
    explicit_draft = bool(_DRAFT.search(value)) and not forbid_draft
    conflict = forbid_submit and (explicit_submit or forbid_draft)
    if conflict:
        selected: bool | None = None
    elif forbid_submit:
        selected = False
    elif forbid_draft:
        # “不要保存草稿”本身就是直接提交意图。
        selected = True
    elif explicit_submit:
        selected = True
    elif explicit_draft:
        selected = False
    elif event_default:
        selected = True
    else:
        selected = False
    default_policy = "event_submit" if event_default else "draft"
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
