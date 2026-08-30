# Skill 目录

| Skill | 输入 | 输出 | 关键节点 |
|---|---|---|---|
| MeetingSkill | 会议子句、now、实时工具 | booking/participant/query DomainResult | 规划、候选、冲突检查、写入、验证 |
| LeaveSkill | 请假子句、schema、候选证据 | workflow_draft_result | schema、日期时长、审批人、preflight、save、验证 |
| BudgetSkill | 费用子句、schema、项目/物料候选 | workflow_draft_result | 项目、明细、金额守恒、preflight、save、验证 |
| WorkflowRecordSkill | 记录查询/替换/删除意图 | record DomainResult | list、匹配、保护性删除、验证 |

Skill 的固定流程不由关键词改变；模型只产出受 schema 约束的语义计划。模型输出不包含工具名、workflow ID、人员 ID、项目码、房间 UUID。工具选择和参数绑定由程序从 registry/候选证据完成。

## 失败语义

- `SUCCEEDED`：领域状态已验证或合法查询结果已生成。
- `BLOCKED`：事实缺失、候选不唯一、写前校验失败或用户未确认。
- `FAILED`：工具/运行时异常，保留已有 DomainResult。
- `SKIPPED`：依赖未成功或能力被配置关闭。

以上状态不会让异常逃出 `MyAgent.run`。
