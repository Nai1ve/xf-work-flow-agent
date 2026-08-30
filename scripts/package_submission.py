#!/usr/bin/env python3
"""构建官方可提交的 V2 zip。

默认把 ``submission2`` 编译为 zip 内的 ``submission/`` 目录。脚本不会读取或复制
``config.local.json``、日志、缓存、训练数据和 reports；``config.json`` 默认递归清空
认证字段，运行时请通过环境变量提供 API key。若明确传入 ``--include-key``，则从
本地 ``submission2/config.json`` 读取当前 key 写入**未跟踪的本地测试包**，适合提交
前联调；该模式不会修改源文件，也不会进入 Git。打包过程只使用标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "submission2"
MAX_BYTES = 10 * 1024 * 1024
SECRET_KEYS = {
    "api_key", "apikey", "access_token", "token", "secret", "password",
    "authorization", "client_secret",
}
SECRET_PATTERN = re.compile(r"(?i)sk-[A-Za-z0-9_-]{16,}")


def _redact(value: Any, key: str | None = None) -> Any:
    """递归清空认证字段，保留模型/base_url 等非敏感配置。"""
    if key and key.lower() in SECRET_KEYS:
        return ""
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str) and SECRET_PATTERN.search(value):
        return SECRET_PATTERN.sub("[REDACTED]", value)
    return value


def _copy_source(stage_submission: Path, *, include_key: bool = False) -> None:
    if not SOURCE.is_dir():
        raise FileNotFoundError(f"找不到源目录: {SOURCE}")
    for source in SOURCE.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(SOURCE)
        if any(part in {"__pycache__", ".pytest_cache"} for part in relative.parts):
            continue
        if relative.name in {
            "config.local.json",
            "config.local.example.json",
            "agent_runtime.log",
            "README.md",
            ".DS_Store",
        }:
            continue
        if relative.suffix in {".pyc", ".pyo", ".log"}:
            continue
        # docs、运行缓存和本地 README 不属于官方 submission 运行时资源；文档在
        # submission2/docs 单独交付，避免把分析样本带进评测包。
        if relative.parts and relative.parts[0] in {"docs", "dist"}:
            continue
        destination = stage_submission / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.name == "config.json":
            try:
                config = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"config.json 无法解析: {exc}") from exc
            if include_key:
                # 仅从本地 config.json 读取 key；若该文件未配置，则允许使用已确认
                # 的环境变量。值只在临时 staging 和 zip 内存中流转，绝不打印。
                llm = config.setdefault("llm", {})
                if not isinstance(llm, dict):
                    raise RuntimeError("config.json 的 llm 配置不是对象")
                if not str(llm.get("api_key") or "").strip() and os.getenv("OPENAI_API_KEY"):
                    llm["api_key"] = os.environ["OPENAI_API_KEY"]
                if not str(llm.get("api_key") or "").strip():
                    raise RuntimeError("--include-key 需要 submission2/config.json 或 OPENAI_API_KEY 中存在可用 key")
                output_config = config
            else:
                output_config = _redact(config)
            destination.write_text(
                json.dumps(output_config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            shutil.copy2(source, destination)


def _write_readme(stage_root: Path, *, include_key: bool = False) -> None:
    key_note = (
        "submission/config.json 已包含本地测试 key；请勿将此包或 key 提交到 Git。"
        if include_key
        else
        "submission/config.json 不包含密钥，请通过 OPENAI_API_KEY 注入。"
    )
    (stage_root / "README.md").write_text(
        f"""# NL2Workflow V2 submission

入口为 `submission/my_agent.py`，依赖仅使用 Python 标准库。
{key_note}
""",
        encoding="utf-8",
    )


def _check_archive(path: Path, *, allow_config_key: bool = False) -> tuple[int, list[str]]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        required = {"submission/my_agent.py", "submission/config.json", "submission/utils/__init__.py"}
        missing = sorted(required - set(names))
        for name in names:
            if any(part in {"__pycache__", ".git", "contest", "reports"} for part in Path(name).parts):
                missing.append(f"禁止路径: {name}")
            if name.endswith((".log", ".pyc", ".pyo")):
                missing.append(f"运行缓存: {name}")
            content = archive.read(name)
            # 只对明确的配置文件检查认证字段；manifest 中的 sha256 不应被误判为 key。
            config_secret = False
            if name.endswith("config.json"):
                try:
                    parsed = json.loads(content.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    config_secret = True
                else:
                    def has_secret(value: Any, key: str | None = None) -> bool:
                        if key and key.lower() in SECRET_KEYS:
                            return bool(value)
                        if isinstance(value, dict):
                            return any(has_secret(v, str(k)) for k, v in value.items())
                        if isinstance(value, list):
                            return any(has_secret(v) for v in value)
                        return False
                    config_secret = has_secret(parsed)
            config_allowed = allow_config_key and name == "submission/config.json"
            if (config_secret and not config_allowed) or (not name.endswith(".json") and SECRET_PATTERN.search(content.decode("utf-8", errors="ignore"))):
                missing.append(f"疑似密钥: {name}")
    return path.stat().st_size, missing


def build(output: Path, *, include_key: bool = False) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nl2workflow_v2_") as temp_dir:
        stage_root = Path(temp_dir)
        stage_submission = stage_root / "submission"
        _copy_source(stage_submission, include_key=include_key)
        _write_readme(stage_root, include_key=include_key)
        if not (stage_submission / "my_agent.py").is_file():
            raise RuntimeError("submission/my_agent.py 不存在")
        if output.exists():
            output.unlink()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for source in sorted(stage_root.rglob("*")):
                if source.is_file():
                    archive.write(source, source.relative_to(stage_root).as_posix())
    size, problems = _check_archive(output, allow_config_key=include_key)
    if problems:
        output.unlink(missing_ok=True)
        raise RuntimeError("提交包检查失败: " + "; ".join(problems))
    if size > MAX_BYTES:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"提交包超过 10MB: {size} bytes")
    print(f"提交包已生成: {output}")
    print(f"大小: {size / 1024:.1f} KB")
    with zipfile.ZipFile(output) as archive:
        print(f"文件数: {len(archive.namelist())}")
        print("入口: submission/my_agent.py")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "submission2" / "dist" / "submit_v2.zip",
        help="输出 zip 路径",
    )
    parser.add_argument(
        "--include-key",
        action="store_true",
        help="将本地 config.json/API key 写入测试包（仅本地使用，勿提交 Git）",
    )
    args = parser.parse_args()
    build(args.output, include_key=args.include_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
