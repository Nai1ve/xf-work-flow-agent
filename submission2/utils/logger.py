"""控制台分层日志。

为 technical_design.md §2 的五层架构（感知/理解/规划/执行/输出）提供统一的
控制台输出。各层模块通过 ``child()`` 派生带层前缀的 logger：

    from utils.logger import ConsoleLogger

    logger = ConsoleLogger().child("感知层")
    logger.info("静态上下文已加载: tools=22 rooms=139")
    # 输出: 12:00:00 INFO [感知层] 静态上下文已加载: tools=22 rooms=139

设计意图：
- 单一格式化入口（时间 / 级别 / 层前缀），避免各层各自 ``print`` 导致输出无章法；
- ``section()`` 打印分隔线与小节标题，便于在长日志中按层 / 按 case 定位；
- 底层复用 stdlib ``logging``（线程安全），官方 runner 用 ThreadPoolExecutor
  并发跑 case，日志写入天然互斥。

与 AGENT.md 的关系：控制台日志只描述「本层做了什么」，不输出任何 case 答案
（reference / success_check / gold_trajectory 一律不写进日志），仅供本地评估时
人工审查。
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import TextIO

# 统一控制台格式：时间 + 级别 + 消息（消息自带层前缀，见 ConsoleLogger）。
_CONSOLE_FORMATTER = logging.Formatter(
    "%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)

# 根 logger 的 console handler 只安装一次；多线程并发实例化 MyAgent 时用锁保护。
_configure_lock = threading.Lock()
_root_configured = False
_console_handlers: list[logging.Handler] = []


def configure_console(stream: TextIO | None = None, level: int = logging.INFO) -> None:
    """配置根 logger 的唯一控制台 handler（幂等，可多次调用）。

    Args:
        stream: 输出流；None 时用 stderr。官方 runner 会把 stderr 合并进运行日志，
            因此本地评估时能看到本 Agent 的全部输出。
        level: 根 logger 级别，默认 INFO（DEBUG 需显式传入）。

    Returns:
        无。
    """
    global _root_configured
    with _configure_lock:
        root = logging.getLogger()
        root.setLevel(level)
        if _root_configured:
            return
        handler = logging.StreamHandler(stream)  # stream=None → sys.stderr
        handler.setFormatter(_CONSOLE_FORMATTER)
        root.addHandler(handler)
        _console_handlers.append(handler)
        _root_configured = True


def reset_console() -> None:
    """移除 ``configure_console`` 安装的 handler（仅测试 / 重新绑定流时使用）。"""
    global _root_configured
    with _configure_lock:
        root = logging.getLogger()
        for handler in _console_handlers:
            root.removeHandler(handler)
        _console_handlers.clear()
        _root_configured = False


class ConsoleLogger:
    """带层前缀的控制台日志封装。

    一个实例绑定一个前缀；``child()`` 派生更深的子层（如 "感知层" →
    "感知层/对账"），输出时统一包上方括号。多个实例共享同一个底层
    ``logging.Logger``（同名），级别与 handler 由根配置决定，前缀则各实例
    独立持有。

    Attributes:
        name: 底层 logging logger 名（供过滤与隔离）。
        prefix: 层路径字符串，如 "感知层/对账"；输出为 "[感知层/对账] msg"。
    """

    def __init__(self, name: str = "agent", prefix: str = "") -> None:
        """初始化 ConsoleLogger。

        Args:
            name: 底层 logging logger 名。
            prefix: 层路径（不带方括号），空串表示根层。
        """
        self._log = logging.getLogger(name)
        self._prefix = prefix

    def child(self, label: str) -> "ConsoleLogger":
        """派生带子层路径的新 logger（原 logger 不变）。

        Args:
            label: 子层标签，如 "对账"（不含分隔符）。

        Returns:
            新 ConsoleLogger，层路径为 父路径 + "/" + label。
        """
        separator = "/" if self._prefix else ""
        return ConsoleLogger(self._log.name, f"{self._prefix}{separator}{label}")

    def section(self, title: str) -> None:
        """打印分隔线 + 小节标题，用于按层 / 按 case 定位日志段落。

        Args:
            title: 小节标题，如 "感知层" 或 "case=beta_mr_0001"。
        """
        self._log.info("[%s] ──────── %s ────────", self._prefix, title)

    def info(self, message: str) -> None:
        """INFO 级输出。

        Args:
            message: 日志内容（不带层前缀，前缀由实例持有）。
        """
        self._log.info("[%s] %s", self._prefix, message)

    def warning(self, message: str) -> None:
        """WARNING 级输出。

        Args:
            message: 日志内容。
        """
        self._log.warning("[%s] %s", self._prefix, message)

    def debug(self, message: str) -> None:
        """DEBUG 级输出（默认不显示，需根 logger level=DEBUG）。

        Args:
            message: 日志内容。
        """
        self._log.debug("[%s] %s", self._prefix, message)
