"""运行时 Workflow Schema 注册表。

静态包不携带 train/val case 数据。注册表优先消费每个 case 的
``workflow.schema`` 返回值，静态索引只能作为可选的结构先验；因此字段、枚举和
明细依赖不会因为训练数据变化而被写死。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class FieldSpec:
    key: str
    field_type: str = ""
    required: bool = False
    options_key: str | None = None
    depends_on: tuple[str, ...] = ()
    field_id: str | int | None = None


@dataclass
class WorkflowSpec:
    workflow_id: int | str
    name: str = ""
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    option_sets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    detail_tables: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(k for k, v in self.fields.items() if v.required)


class WorkflowSchemaRegistry:
    """按 workflow_id 缓存当前用例已观测到的 Schema。"""

    def __init__(self) -> None:
        self._specs: dict[str, WorkflowSpec] = {}

    def ingest(self, payload: Any) -> WorkflowSpec | None:
        if not isinstance(payload, dict):
            return None
        schema = payload.get("schema") if isinstance(payload.get("schema"), dict) else payload
        workflow_id = payload.get("workflow_id") or schema.get("workflow_id")
        if workflow_id is None:
            return None
        name = str(payload.get("name") or schema.get("name") or "")
        fields: dict[str, FieldSpec] = {}
        raw_fields = schema.get("fields") or schema.get("form_fields") or []
        if isinstance(raw_fields, dict):
            raw_fields = [dict(value, key=key) if isinstance(value, dict) else {"key": key} for key, value in raw_fields.items()]
        for raw in raw_fields if isinstance(raw_fields, list) else []:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or raw.get("name") or raw.get("field") or "").strip()
            if not key:
                continue
            deps = raw.get("depends_on") or raw.get("dependencies") or []
            if isinstance(deps, str):
                deps = [deps]
            fields[key] = FieldSpec(
                key=key,
                field_type=str(raw.get("type") or raw.get("field_type") or ""),
                required=bool(raw.get("required")),
                options_key=raw.get("options_key"),
                depends_on=tuple(str(x) for x in deps if x),
                field_id=raw.get("field_id") or raw.get("id"),
            )
        # simulator schema 使用 required_fields + *_options 的扁平形态。
        for key in schema.get("required_fields") or []:
            key = str(key)
            fields.setdefault(key, FieldSpec(key=key, required=True))
            if not fields[key].required:
                fields[key] = FieldSpec(**{**fields[key].__dict__, "required": True})
        option_sets = {
            str(k): list(v) for k, v in schema.items()
            if str(k).endswith("_options") and isinstance(v, list)
        }
        detail_tables: dict[str, dict[str, Any]] = {}
        raw_details = schema.get("detail_tables") or schema.get("details") or {}
        if isinstance(raw_details, dict):
            detail_tables = {str(k): v for k, v in raw_details.items() if isinstance(v, dict)}
        spec = WorkflowSpec(workflow_id=workflow_id, name=name, fields=fields, option_sets=option_sets, detail_tables=detail_tables)
        self._specs[str(workflow_id)] = spec
        return spec

    def get(self, workflow_id: int | str) -> WorkflowSpec | None:
        return self._specs.get(str(workflow_id))

    def required(self, workflow_id: int | str) -> tuple[str, ...]:
        spec = self.get(workflow_id)
        return spec.required_fields if spec else ()

    def options(self, workflow_id: int | str, options_key: str) -> list[dict[str, Any]]:
        spec = self.get(workflow_id)
        return list(spec.option_sets.get(options_key, [])) if spec else []

    def field_id(self, workflow_id: int | str, key: str) -> str | int | None:
        spec = self.get(workflow_id)
        field = spec.fields.get(key) if spec else None
        return field.field_id if field else None

    def validate_data(self, workflow_id: int | str, data: dict[str, Any]) -> list[str]:
        spec = self.get(workflow_id)
        if spec is None:
            return []
        return [key for key in spec.required_fields if key not in data]

    def snapshot(self) -> dict[str, Any]:
        return {
            key: {
                "workflow_id": spec.workflow_id,
                "name": spec.name,
                "required_fields": list(spec.required_fields),
                "fields": list(spec.fields),
                "option_sets": list(spec.option_sets),
            }
            for key, spec in self._specs.items()
        }


__all__ = ["FieldSpec", "WorkflowSpec", "WorkflowSchemaRegistry"]
