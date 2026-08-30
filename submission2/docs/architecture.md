# NL2Workflow V2 架构

## 目标

V2 把自然语言理解和业务执行分开：模型只负责任务拆分、意图理解、语义实体和候选排序；程序负责工具契约、SOP 顺序、字段依赖、写前校验、失败恢复和最终投影。默认运行档位为 `hybrid_compat`，旧入口保留为 `legacy_current` 回滚档，`generic_v2` 用于隐藏集 A/B。

## 数据流

```text
env.reset/list_tools
  -> StaticContextStore + ToolContractReconciler
  -> LLMGateway / IntentRecognizer
  -> TaskGraphIR -> TaskDag
  -> Meeting/Leave/Budget Skill SOP
  -> EvidenceLedger + DomainResult
  -> late-bound Submission Projection
  -> final_answer
```

每次 `MyAgent.run` 都重新创建 `CaseContext`、证据账本、Task 状态和模型预算；不在进程间共享业务事实。静态索引只提供 schema/目录先验，运行时工具返回始终优先。

## 分层边界

- 感知层：加载静态索引、读取 `env.list_tools()`、运行时对账。
- 理解层：粗粒度任务拆分和各 Skill 的结构化语义规划，不生成工具 ID。
- 规划层：将合法 TaskSpec 绑定稳定 Skill SOP，依赖形成无环 DAG。
- 执行层：顺序调用工具，所有写操作经过 registry、证据和业务 preflight。
- 输出层：只从 DomainResult 和 EvidenceLedger 生成字段；兼容字段是同一事实的别名。

## 冲突隔离

规则按 `BUSINESS_RULE`、`SUPERSET_PROJECTION`、`COMPATIBILITY_POLICY`、`LEGACY_QUARANTINE` 分层。无法由当前用户语义、schema 或实时工具事实推导的 Gold 差异不进入通用逻辑，而由 Profile 开关隔离并记录。
