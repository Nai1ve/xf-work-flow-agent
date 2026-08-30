# 数据契约与权威顺序

## 权威顺序

```text
用户明确事实 -> 多轮补充 -> 实时工具结果 -> Workflow Schema
-> 公司业务配置 -> 兼容策略 -> legacy fallback/blocked
```

## ToolRegistry

`scripts/build_static_context.py` 将 `tool_specs.json` 编译为 `tools.index.json`，记录名称、参数 schema、写风险和相对成本。运行时 `env.list_tools()` 与静态索引对账，运行时 schema 为调用权威；未公开工具和必填/类型错误在 `env.call_tool` 前阻断。

## WorkflowRegistry

`workflows.index.json` 只保存目录、字段类型、required/optional、依赖和候选结构，不保存 `sample_draft`、金额或答案。运行时 `workflow.schema`、`workflow.project_search`、`workflow.browser_search` 的返回是保存前证据。人员、项目、物料和枚举值不得由模型创造。

## MeetingroomDirectory

`meetingrooms.index.json` 建立 room/building/campus/office 反向索引和静态容量/设备属性。静态数据只用于位置理解与排序；可执行房间、冲突和权限必须来自当前 `meetingroom.room.list` 等工具。调用使用工具返回的合法标识，Projection 再按 Profile 晚绑定。

## EvidenceLedger

证据包含 observation、tool_result、tool_error、reply、policy_decision 和 task 生命周期，带 `task_id`。不同 Task 默认隔离；跨域传递必须经过显式 DomainResult，最终答案不得读取 Gold 或 Case 元数据。
