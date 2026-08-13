"""LLMGateway：理解层唯一的线上模型访问通道（technical_design.md §4.1）。

设计意图：
- 理解层只通过 `LLMGateway.structured_call` 访问线上模型，隔离供应商差异；
- 每个 case 的 LLM 总耗时受 `runtime.llm_budget_s`（默认 35s）约束，超预算直接走兜底，
  避免「整个 case 卡在意图识别上」（用户要求意图识别要快）；
- 输出契约：强制 JSON + 本地 schema 校验 + 失败重试一次 + 兜底，永不抛异常；
- 配置合并（低 → 高优先级）：`config.json["llm"]` 默认值 ← `config.local.json["llm"]`
  （gitignored，本地线上 key / base_url / model，只读）← 环境变量
  `OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL`；
- 无 api_key / base_url / model → `available=False`，调用方直接走规则兜底；
- **可注入 backend**（测试用 FakeBackend，不触网）；生产走标准库 urllib 的 OpenAI-compatible
  `/chat/completions`（与官方 `submission_example.py` 同源）。

安全（AGENT.md）：api_key 只在内存使用，绝不打印、绝不写日志、绝不进 final_answer。
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _load_config() -> dict[str, Any]:
    """合并默认/本地/环境三层 LLM 配置。

    优先级（低 → 高）：`config.json["llm"]` < `config.local.json` 各档位
    （`llm_fast_new` > `llm_fast` > `llm` > `llm_strong`）< 环境变量。识别层是快速路径，优先取
    `llm_fast_new`/`llm_fast` 档。`config.local.json` 只读加载，**不打印、不写入任何字段**。
    """
    submission_dir = Path(__file__).resolve().parent.parent
    merged: dict[str, Any] = {}

    def _read_json(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    merged.update(_read_json(submission_dir / "config.json").get("llm", {}))
    local = _read_json(submission_dir / "config.local.json")
    for tier in ("llm_fast", "llm", "llm_strong", "llm_fast_new"):
        if isinstance(local.get(tier), dict):
            merged.update(local[tier])

    if os.getenv("OPENAI_API_KEY"):
        merged["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.getenv("OPENAI_BASE_URL"):
        merged["base_url"] = os.environ["OPENAI_BASE_URL"]
    if os.getenv("OPENAI_MODEL"):
        merged["model"] = os.environ["OPENAI_MODEL"]

    runtime = _read_json(submission_dir / "config.json").get("runtime", {})
    if "llm_budget_s" in merged:
        runtime["llm_budget_s"] = merged["llm_budget_s"]
    merged["llm_budget_s"] = runtime.get("llm_budget_s", 35)
    return merged


class HttpBackend:
    """纯标准库 OpenAI-compatible ``/chat/completions`` 后端。

    不依赖 httpx/openai SDK，避免评测容器缺少第三方包时在模块导入阶段直接失败。
    """

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        timeout_s: float,
        model: str,
        temperature: float,
        max_tokens: int,
        require_json_object: bool = True,
    ) -> str:
        """发一次补全请求，返回 assistant 的 content 文本；异常由调用方兜底。

        Args:
            require_json_object: 是否带 `response_format=json_object`。部分
                openai-compatible 供应商要求消息里出现小写 "json" 才接受该参数，
                否则返回 400；调用方可据此在重试时降级为纯文本 + 宽容 JSON 解析。
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if require_json_object:
            body["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(float(timeout_s), 0.1)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 读取响应体后仍保留 HTTPError；上层据 status/text 判断是否应去掉
            # response_format 重试。禁止记录 Authorization header。
            try:
                exc.response_text = exc.read().decode("utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 错误体不可读不影响原异常传播
                exc.response_text = ""  # type: ignore[attr-defined]
            raise
        return payload["choices"][0]["message"]["content"]


class FakeBackend:
    """测试后端：按消息返回预先注册的 content 序列，不触网。"""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]], **_: Any) -> str:
        self.calls.append(list(messages))
        if not self.contents:
            raise RuntimeError("FakeBackend 已无剩余响应")
        content = self.contents.pop(0)
        # 测试里直接传 dict 更方便：与真实后端一样最终吐给调用方的是 JSON 文本。
        return json.dumps(content, ensure_ascii=False) if not isinstance(content, str) else content


def _parse_json(content: str) -> dict[str, Any] | None:
    """宽容 JSON 解析：剥掉 markdown 代码块后强制解析为 dict。"""
    content = (content or "").strip()
    if not content:
        return None
    if content.startswith("```"):
        lines = content.splitlines()
        if len(lines) >= 3:
            content = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _schema_errors(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """校验模型输出使用到的 JSON Schema 子集，全程仅依赖标准库。

    覆盖 object/array/string/number/integer、required、properties、items、enum、
    minimum/maximum 和 additionalProperties；这些已覆盖当前四类模型契约。
    """
    errors: list[str] = []

    def check(value: Any, rule: dict[str, Any], path: str) -> None:
        expected = rule.get("type")
        type_ok = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }.get(expected, True)
        if not type_ok:
            errors.append(f"{path} 类型应为 {expected}")
            return
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"{path} 不在允许枚举中")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                errors.append(f"{path} 小于最小值")
            if "maximum" in rule and value > rule["maximum"]:
                errors.append(f"{path} 大于最大值")
        if isinstance(value, dict):
            for required in rule.get("required", []):
                if required not in value:
                    errors.append(f"{path} 缺少必填键: {required}")
            properties = rule.get("properties") or {}
            if rule.get("additionalProperties") is False:
                for key in value:
                    if key not in properties:
                        errors.append(f"{path}.{key} 是未允许字段")
            for key, child_rule in properties.items():
                if key in value and isinstance(child_rule, dict):
                    check(value[key], child_rule, f"{path}.{key}")
        if isinstance(value, list) and isinstance(rule.get("items"), dict):
            for index, item in enumerate(value):
                check(item, rule["items"], f"{path}[{index}]")

    if schema:
        check(data, schema, "$")
    return errors


def _rejects_json_object(exc: BaseException) -> bool:
    """是否供应商拒绝 `response_format=json_object` 的 400。

    典型回复（OpenAI 系）："Response input messages must contain the word 'json'
    ... to use '***.format' of type 'json_object'."。命中后重试应去掉该参数。
    """
    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code
        text = str(getattr(exc, "response_text", ""))
    else:
        # 保持对可注入测试后端和其它 HTTP SDK 异常的兼容，不导入对应 SDK。
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        text = str(getattr(response, "text", ""))
    return status == 400 and "json_object" in text


# 供应商级记忆：同进程内一旦检测到某 base_url 拒绝 response_format=json_object，
# 后续对该供应商的调用直接跳过该参数（省掉一次注定 400 的往返，识别更快）。
_providers_rejecting_json_object: set[str] = set()

# 进程级 LLM 调用统计（跨 case 聚合，进程退出时经 atexit 汇总到运行日志）。
_PROCESS_LLM_STATS: dict[str, float | int] = {"n_calls": 0, "n_success": 0, "total_s": 0.0}


def _report_process_llm_stats() -> None:
    """进程退出时汇总 LLM 调用时间（写 stderr，并入 runner 运行日志）。"""
    n = int(_PROCESS_LLM_STATS["n_calls"])
    if not n:
        return
    total = float(_PROCESS_LLM_STATS["total_s"])
    print(
        f"[LLM] 进程级调用统计: calls={n} "
        f"success={int(_PROCESS_LLM_STATS['n_success'])} "
        f"total={total:.2f}s avg={total / n:.2f}s",
        file=sys.stderr,
    )


atexit.register(_report_process_llm_stats)


class LLMGateway:
    """统一模型访问门（§4.1）：structured_call = 强制 JSON + 校验 + 重试一次 + 兜底。

    Args:
        logger: 可选的 ConsoleLogger 子 logger，只记调用耗时与失败原因，不记 key。
        backend: 可注入（默认 HttpBackend；测试传 FakeBackend）。
        config: 覆盖自动合并配置（测试用）；None 时读 config.json + config.local.json + env。
    """

    def __init__(self, logger: Any = None, *, backend: Any = None, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config) if config is not None else _load_config()
        self.provider = str(cfg.get("provider") or "")
        self.base_url = str(cfg.get("base_url") or "").rstrip("/")
        self.api_key = str(cfg.get("api_key") or "")
        self.model = str(cfg.get("model") or "")
        self.temperature = float(cfg.get("temperature", 0.0))
        self.max_tokens = int(cfg.get("max_tokens", 1200))
        self.llm_budget_s = float(cfg.get("llm_budget_s", 35.0))
        self._spent_s = 0.0
        self.logger = logger
        self.available = bool(self.api_key and self.base_url and self.model) and self.provider in ("", "openai_compatible")
        self._backend = backend or (
            HttpBackend(base_url=self.base_url, api_key=self.api_key) if self.available else None
        )
        # 该供应商是否已知拒绝 response_format（进程级记忆，跳过无谓的首次 400）。
        self._prefer_json_object = self.base_url not in _providers_rejecting_json_object
        # 本实例（= 单 case）的 LLM 调用统计。
        self._n_calls = 0
        self._n_success = 0
        self._n_failed = 0
        self._calls_total_s = 0.0

    def stats_summary(self) -> dict[str, Any]:
        """本 case 的 LLM 调用统计（审计用，不含任何密钥）。"""
        n = max(self._n_calls, 1)
        return {
            "llm_calls": self._n_calls,
            "llm_success": self._n_success,
            "llm_failed": self._n_failed,
            "llm_total_s": round(self._calls_total_s, 2),
            "llm_avg_s": round(self._calls_total_s / n, 2),
        }

    def _record_call(self, succeeded: bool, elapsed_s: float) -> None:
        """记录一次 structured_call 的耗时与结果（实例 + 进程两级）。"""
        self._n_calls += 1
        self._n_success += 1 if succeeded else 0
        self._n_failed += 0 if succeeded else 1
        self._calls_total_s += elapsed_s
        _PROCESS_LLM_STATS["n_calls"] = int(_PROCESS_LLM_STATS["n_calls"]) + 1
        _PROCESS_LLM_STATS["n_success"] = int(_PROCESS_LLM_STATS["n_success"]) + (1 if succeeded else 0)
        _PROCESS_LLM_STATS["total_s"] = float(_PROCESS_LLM_STATS["total_s"]) + elapsed_s

    # ------------------------------------------------------------------ 审计 --
    @property
    def spent_s(self) -> float:
        """本 case 已花 LLM 时间（秒），供调用方写审计日志。"""
        return self._spent_s

    def _remaining_s(self) -> float:
        return self.llm_budget_s - self._spent_s

    def _log(self, level: str, message: str) -> None:
        if self.logger is not None:
            getattr(self.logger, level, lambda _m: None)(message)

    # ------------------------------------------------------------ 核心入口 --
    def structured_call(
        self,
        prompt_card: str,
        payload: dict[str, Any],
        output_schema: dict[str, Any],
        timeout_s: float,
        fallback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """执行一次强制 JSON 的识别调用，永不抛异常。

        Args:
            prompt_card: system 提示词卡片（角色 + 任务 + 正反例 + 输出契约）。
            payload: user 消息体（JSON 序列化），如 {user_query, now, mode}。
            output_schema: 期望输出的 JSON Schema（本地校验）。
            timeout_s: 单次网络调用超时（还会被剩余预算二次收窄）。
            fallback: 全部失败时的兜底 dict（默认 {}）。

        Returns:
            校验通过的结构化 dict；否则兜底 dict。
        """
        if not self.available:
            self._log("warning", "LLM 不可用（缺 api_key/base_url/model），走兜底")
            return dict(fallback or {})
        if self._remaining_s() <= 0:
            self._log("warning", "LLM 预算已耗尽，走兜底")
            return dict(fallback or {})

        messages: list[dict[str, str]] = [
            {"role": "system", "content": prompt_card},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        last_error: str = "未知错误"
        call_start = time.monotonic()
        # 供应商拒绝 json_object 时，重试降级为纯文本 + 宽容解析（进程级记忆）。
        use_json_object = self._prefer_json_object
        result: dict[str, Any] = dict(fallback or {})
        succeeded = False

        for attempt in (1, 2, 3):  # 最多三次（长跑瞬态 API 失败定案：2→3 次提升稳健）
            remaining = self._remaining_s()
            if remaining <= 0:
                break
            timeout = min(float(timeout_s), remaining)
            attempt_start = time.monotonic()
            try:
                content = self._backend.chat(
                    messages,
                    timeout_s=timeout,
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    require_json_object=use_json_object,
                )
            except Exception as exc:  # noqa: BLE001 —— 网络/超时/HTTP 错误全部兜底
                last_error = repr(exc)
                self._log("warning", f"LLM 调用失败（第 {attempt} 次）: {last_error}")
                if _rejects_json_object(exc):
                    use_json_object = False  # 400 拒绝 response_format → 重试不带它
                    _providers_rejecting_json_object.add(self.base_url)
                    self._prefer_json_object = False
                    self._log("warning", "供应商拒绝 json_object，重试降级为纯文本 JSON")
            else:
                parsed = _parse_json(content)
                if parsed is None:
                    last_error = "LLM 返回非 JSON"
                    self._log("warning", f"LLM 返回非 JSON（第 {attempt} 次），重试")
                else:
                    errors = _schema_errors(parsed, output_schema)
                    if errors:
                        last_error = f"LLM 输出未过 schema 校验: {errors}"
                        self._log("warning", f"LLM 输出未过校验（第 {attempt} 次）: {errors}")
                    else:
                        self._spent_s += time.monotonic() - attempt_start
                        self._log("info", f"LLM 识别成功（第 {attempt} 次）: 耗时 {time.monotonic() - attempt_start:.2f}s")
                        result = parsed
                        succeeded = True
                        break
            self._spent_s += time.monotonic() - attempt_start
            if attempt < 3:
                time.sleep(0.25)  # 失败后短暂退避，避免长跑中对抖动供应商连续冲击

        call_elapsed = time.monotonic() - call_start
        if not succeeded:
            self._log("warning", f"LLM 识别失败（总耗时 {call_elapsed:.2f}s）: {last_error}，走兜底")
        self._record_call(succeeded, call_elapsed)
        return result
