"""日志/诊断通道的最小脱敏工具。

业务执行仍使用原始值；只有写入本地日志或带外 telemetry 前调用本模块。这样
候选 ID、项目编码和会议订单仍可用于排障，但手机号和临时 URL 参数不会落盘。
"""

from __future__ import annotations

import json
import re
from typing import Any


# 中国大陆手机号码（允许 +86/空格/短横线前缀）。不要把普通 11 位业务编号
# 当成电话：首位必须是 1，第二位 3-9。
_PHONE = re.compile(r"(?<!\d)(?:(?:\+?86)[ -]?)?(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
_URL_QUERY = re.compile(r"(https?://[^\s?]+)\?[^\s]+", re.IGNORECASE)


def redact_text(value: Any) -> str:
    """转换为单行文本并移除敏感片段。"""

    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    text = _PHONE.sub(lambda m: "*******" + m.group(3), text)
    text = _URL_QUERY.sub(r"\1", text)
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def redact_value(value: Any) -> Any:
    """递归脱敏结构，保留 dict/list 形状供 telemetry 聚合。"""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return value


__all__ = ["redact_text", "redact_value"]
