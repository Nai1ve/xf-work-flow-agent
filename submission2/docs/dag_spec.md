# 纯 Python Task DAG 规范

## 类型

公开类型位于 `utils/dag_runtime.py`：`AgentState`、`TaskSpec`、`SkillSpec`、`NodeSpec`、`NodeOutcome`、`DomainResult`、`DagTask`、`TaskDag`。节点状态为 `PENDING/READY/RUNNING/SUCCEEDED/BLOCKED/FAILED/SKIPPED`。

## 调度

`TaskGraphIR` 的合法依赖下标被映射到 `task-N`；自依赖、越界、重复边和环被移除或降级为用户顺序串行。依赖节点必须 `SUCCEEDED` 才能执行；后继只标记 `SKIPPED`，不影响其他分支。首版不并发调用环境工具，避免共享环境状态竞争。

## 预算与重试

模型每个结构化调用最多初始请求加一次传输/校验重试，并受 case LLM 时间预算约束。Skill 内部只对可恢复工具错误执行有限恢复；接近 step budget 时保留必要写节点和最终投影。`StepLimitExceeded`、工具异常和模型失败均转换为可审计的 Outcome。

## 证据隔离

`CaseContext` 是单用例容器；`EvidenceLedger.active_task_id` 自动标记当前工具/追问证据。跨 Task 只能通过 handler 返回的显式 DomainResult 合并，不读取另一个 Task 的隐式中间变量。
