#!/usr/bin/env python3
"""离线构建静态上下文索引（submission/static_context/）。

从比赛公开数据生成 Agent 感知层的先验合同 / 目录元数据：

    contest/train/tool_specs.json            → tools.index.json
    contest/train/data/meetingroom_data.json → meetingrooms.index.json
    manifest.json                            → 来源 sha256 + train/val 一致性校验

用法：

    .venv/bin/python scripts/build_static_context.py
    .venv/bin/python scripts/build_static_context.py \\
        --split-dir contest/train --val-dir contest/val --output-dir submission2/static_context

设计意图：
- 索引与官方数据同源、可复现、可哈希校验，生成物直接进提交包；
- 写工具名单 / 会议室静态属性在此**离线编译**，Agent 运行时代码与数据解耦；
- 只含静态属性，不含可用性 / 时间冲突（那些以运行时工具证据为准，见 AGENT.md）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# 使脚本能以 `import utils.static_context` 引用 schema 版本常量（单一来源）。
# 当前开发入口是 submission2；兼容旧 checkout 时再回退到 submission。
_PACKAGE_DIR = ROOT / "submission2" if (ROOT / "submission2").is_dir() else ROOT / "submission"
sys.path.insert(0, str(_PACKAGE_DIR))
from utils.static_context import SCHEMA_VERSION  # noqa: E402

# 写类工具名单：meetingroom 的增删改 + workflow 的保存/删除。
# 这是静态合同元数据（域无关），供执行层写操作门禁先验；读类工具不列。
WRITE_TOOLS = [
    "meetingroom.booking.create",
    "meetingroom.booking.cancel",
    "meetingroom.booking.extend",
    "meetingroom.booking.participant.add",
    "meetingroom.booking.participant.remove",
    "workflow.save",
    "workflow.delete",
]


def tool_risk(name: str) -> str:
    """按工具职责编译风险等级；运行时仍以 list_tools/schema 为权威。"""
    if name in WRITE_TOOLS:
        return "high"
    if name in {"meetingroom.booking.list", "meetingroom.room.schedule", "user.get_info", "user.get_workspace", "workflow.project_search", "workflow.browser_search", "workflow.search_person"}:
        return "medium"
    return "low"


def tool_cost(name: str) -> int:
    """编译相对步数成本，用于预算排序而非替代 step_budget。"""
    return 2 if name in WRITE_TOOLS else 1


def sha256_file(path: Path) -> str:
    """计算文件 sha256（分块读，避免大文件占内存）。

    Args:
        path: 目标文件。

    Returns:
        sha256 十六进制字符串。
    """
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def build_tools_index(tool_specs: dict[str, Any]) -> dict[str, Any]:
    """把 tool_specs.json 编译成 tools.index.json 结构。

    Args:
        tool_specs: contest/train/tool_specs.json 的 dict（name → spec）。

    Returns:
        tools.index.json 的 dict（schema_version / counts / write_tools / by_name）。
    """
    by_name: dict[str, Any] = {}
    for name, spec in tool_specs.items():
        if not isinstance(spec, dict):
            continue
        by_name[name] = {
            "name": spec.get("name") or name,
            "description": spec.get("description") or "",
            "args_schema": spec.get("args_schema") or {},
            "risk": tool_risk(name),
            "cost": tool_cost(name),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "counts": {"tools": len(by_name), "write_tools": len(WRITE_TOOLS)},
        "write_tools": sorted(WRITE_TOOLS),
        "by_name": dict(sorted(by_name.items())),
    }


def build_workflow_index(workflow_data: dict[str, Any]) -> dict[str, Any]:
    """从 workflow_data 编译流程边界、字段依赖和候选枚举。

    只保留可作为流程约束的 schema/catalog/options；丢弃 sample_draft 等示例值，
    避免把训练答案或固定金额带进静态资源包。实际运行时仍须优先使用
    ``workflow.schema`` / ``workflow.browser_search`` 的返回值。
    """
    catalog = []
    for item in workflow_data.get("workflow_catalog") or []:
        if isinstance(item, dict) and item.get("workflow_id") is not None:
            catalog.append({"workflow_id": item.get("workflow_id"), "name": item.get("name", "")})

    schemas: dict[str, Any] = {}
    for workflow_id, raw in (workflow_data.get("workflow_schemas") or {}).items():
        if not isinstance(raw, dict):
            continue
        schema: dict[str, Any] = {}
        for key in ("required_fields", "field_descriptions", "field_types", "field_aliases", "optional_fields", "auto_filled_fields"):
            value = raw.get(key)
            if value is not None:
                schema[key] = value
        # fields/detail_tables 描述的是结构和依赖，不包含 sample_draft。
        if isinstance(raw.get("fields"), list):
            schema["fields"] = [
                {
                    key: item[key]
                    for key in ("id", "field_id", "key", "name", "type", "field_type", "required", "depends_on", "dependencies")
                    if key in item
                }
                for item in raw["fields"]
                if isinstance(item, dict)
            ]
        detail_tables = raw.get("detail_tables") or {}
        if isinstance(detail_tables, dict):
            schema["detail_tables"] = {}
            for table_id, table in detail_tables.items():
                if not isinstance(table, dict):
                    continue
                schema["detail_tables"][str(table_id)] = {
                    key: table[key]
                    for key in ("required_fields", "field_types", "fields", "field_descriptions")
                    if key in table
                }
        schemas[str(workflow_id)] = schema

    options: dict[str, list[dict[str, Any]]] = {}
    for key, values in (workflow_data.get("workflow_browser_options") or {}).items():
        if not isinstance(values, list):
            continue
        options[str(key)] = [
            {
                field: item[field]
                for field in ("label", "value", "code")
                if field in item
            }
            for item in values
            if isinstance(item, dict)
        ]
    return {
        "schema_version": SCHEMA_VERSION,
        "counts": {"workflows": len(catalog), "schemas": len(schemas), "option_sets": len(options)},
        "catalog": catalog,
        "schemas": schemas,
        "browser_options": options,
    }


def build_meetingrooms_index(meetingroom_data: dict[str, Any]) -> dict[str, Any]:
    """把 meetingroom_data.json 的 rooms dict 编译成目录 + 二级索引。

    原数据以 dict key 承载 room_id，这里把 room_id 补进每条记录使其自描述，
    并构建 by_office_id / by_building / by_campus 二级索引，供执行层做归一化
    与候选过滤先验。**不含可用性 / 时间冲突**（以运行时 room.list 为准）。

    Args:
        meetingroom_data: contest/train/data/meetingroom_data.json 的 dict。

    Returns:
        meetingrooms.index.json 的 dict。
    """
    rooms = meetingroom_data.get("rooms") or {}
    by_room_id: dict[str, Any] = {}
    by_office_id: dict[str, str] = {}
    by_building: dict[str, list[str]] = {}
    by_campus: dict[str, list[str]] = {}

    for room_id, record in rooms.items():
        if not isinstance(record, dict):
            continue
        enriched = dict(record)
        enriched["room_id"] = room_id
        by_room_id[room_id] = enriched

        office_id = record.get("officeId")
        if office_id:
            by_office_id.setdefault(office_id, room_id)
        building = record.get("building")
        if building:
            by_building.setdefault(building, []).append(room_id)
        campus = record.get("campus")
        if campus:
            by_campus.setdefault(campus, []).append(room_id)

    return {
        "schema_version": SCHEMA_VERSION,
        "counts": {"rooms": len(by_room_id)},
        "by_room_id": by_room_id,
        "by_office_id": by_office_id,
        "by_building": by_building,
        "by_campus": by_campus,
    }


def check_split_hash(train_path: Path, val_path: Path) -> dict[str, Any]:
    """返回 train/val 数据一致性检查结果（文件缺失不崩溃，标记 same=False）。

    Args:
        train_path: train 侧文件路径。
        val_path: val 侧文件路径。

    Returns:
        一致性检查 dict。
    """
    train_sha = sha256_file(train_path) if train_path.exists() else None
    val_sha = sha256_file(val_path) if val_path.exists() else None
    return {
        "file": "data/meetingroom_data.json",
        "same": train_sha is not None and train_sha == val_sha,
        "train_sha256": train_sha,
        "val_sha256": val_sha,
        "train_missing": train_sha is None,
        "val_missing": val_sha is None,
    }


def build_manifest(
    sources: dict[str, dict[str, Any]],
    split_checks: list[dict[str, Any]],
    counts: dict[str, int],
) -> dict[str, Any]:
    """组装 manifest.json（来源 sha256 + 一致性校验 + 规模汇总）。

    Args:
        sources: 源文件信息 {name: {"path", "sha256"}}。
        split_checks: check_split_hash 的结果列表。
        counts: 各索引的规模汇总。

    Returns:
        manifest.json 的 dict。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "counts": counts,
        "sources": sources,
        "split_hash_checks": split_checks,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    """写 JSON（ensure_ascii=False 保留中文，indent=2 便于 diff）。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_static_context(
    split_dir: Path,
    val_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """读取 train 数据、构建索引并写入 output_dir，返回 manifest。

    Args:
        split_dir: 训练侧目录（含 tool_specs.json 与 data/）。
        val_dir: 验证侧目录（用于 split 一致性校验）。
        output_dir: 索引输出目录。

    Returns:
        生成的 manifest dict。
    """
    tool_specs_path = split_dir / "tool_specs.json"
    meetingroom_data_path = split_dir / "data" / "meetingroom_data.json"
    workflow_data_path = split_dir / "data" / "workflow_data.json"

    tool_specs = json.loads(tool_specs_path.read_text(encoding="utf-8"))
    meetingroom_data = json.loads(meetingroom_data_path.read_text(encoding="utf-8"))
    workflow_data = json.loads(workflow_data_path.read_text(encoding="utf-8")) if workflow_data_path.exists() else {}

    tools_index = build_tools_index(tool_specs)
    rooms_index = build_meetingrooms_index(meetingroom_data)
    workflow_index = build_workflow_index(workflow_data)
    manifest = build_manifest(
        sources={
            "tool_specs": {
                "path": "contest/train/tool_specs.json",
                "sha256": sha256_file(tool_specs_path),
            },
            "meetingroom_data": {
                "path": "contest/train/data/meetingroom_data.json",
                "sha256": sha256_file(meetingroom_data_path),
            },
            "workflow_data": {
                "path": "contest/train/data/workflow_data.json",
                "sha256": sha256_file(workflow_data_path) if workflow_data_path.exists() else None,
            },
        },
        split_checks=[
            check_split_hash(meetingroom_data_path, val_dir / "data" / "meetingroom_data.json")
        ],
        counts={**tools_index["counts"], **rooms_index["counts"], **workflow_index["counts"]},
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "tools.index.json", tools_index)
    write_json(output_dir / "meetingrooms.index.json", rooms_index)
    write_json(output_dir / "workflows.index.json", workflow_index)
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split-dir", type=Path, default=ROOT / "contest" / "train")
    parser.add_argument("--val-dir", type=Path, default=ROOT / "contest" / "val")
    default_output = ROOT / "submission2" / "static_context"
    if not default_output.parent.exists():
        default_output = ROOT / "submission" / "static_context"
    parser.add_argument("--output-dir", type=Path, default=default_output)
    args = parser.parse_args()

    manifest = build_static_context(args.split_dir, args.val_dir, args.output_dir)
    print(f"静态上下文已生成: {args.output_dir}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
