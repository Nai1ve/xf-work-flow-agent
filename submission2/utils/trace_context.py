"""单个 case 的诊断追踪上下文。

该对象只携带关联元数据，不保存密钥或 Gold；每次 ``MyAgent.run`` 新建，case
结束后由引用释放。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceContext:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    case_id: str = ""
    package_version: str = "v2"
    profile: str = ""
    prompt_versions: dict[str, str] = field(default_factory=dict)
    task_id: str = ""
    stage: str = ""
    attempt: int = 0
    started_at: float = field(default_factory=time.time)

    def headers(self, *, task_id: str | None = None, stage: str | None = None,
                attempt: int | None = None) -> dict[str, str]:
        return {
            "X-Agent-Run-Id": self.run_id,
            "X-Agent-Case-Id": self.case_id,
            "X-Agent-Task-Id": task_id if task_id is not None else self.task_id,
            "X-Agent-Stage": stage if stage is not None else self.stage,
            "X-Agent-Prompt-Version": self.prompt_versions.get(stage or self.stage, "v2"),
            "X-Agent-Attempt": str(attempt if attempt is not None else self.attempt),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "package_version": self.package_version,
            "profile": self.profile,
            "prompt_versions": dict(self.prompt_versions),
            "started_at": self.started_at,
        }


__all__ = ["TraceContext"]
