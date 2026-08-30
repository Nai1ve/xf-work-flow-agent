# 评分策略

## TSR

先保证最终业务状态和必需工具调用；工具顺序由 SOP 固定，候选和字段由实时证据绑定。

## AS

调用前检查工具公开性、schema、必填、权限、冲突、枚举来源、金额守恒和写前确认。禁止模型直接发明 ID。

## ES

命中唯一合法候选立即停止 fallback；不为“保险”重复读写。模型超时只做一次重试。OA postcheck 仅在任务/跨域契约要求时执行。

## RS

以语义实体、候选 provenance、意图类型和证据处理同义改写，不使用 Case ID、训练原句或 Gold 映射。

## Profile

`legacy_current` 冻结旧行为；`hybrid_compat` 启用可解释的 Projection/日历/恢复策略并保留少量兼容模板；`generic_v2` 关闭不可观测的模板和评测兼容默认。通过 `AGENT_EXECUTION_PROFILE`、`AGENT_CALENDAR_PROFILE`、`AGENT_LEGACY_BUDGET_TEMPLATES` 切换。

冲突裁决会写入 `兼容裁决` 日志；不能无损解决的值冲突保持 legacy 隔离，不伪装为业务规则。
