# NL2Workflow Agent V3 架构设计讨论稿（2026-07-07）

本文是 V3 重构的讨论基线。目标不是继续提高本地公开验证集分数，而是重建可泛化、可控时延、证据驱动的 Agent 架构，用于冲击隐藏集 80 分。

## 0. 不可违反的反过拟合约束

V3 以及后续任何实现都不能根据本地训练集、开发集、公开验证集或本地回归集写固定代码。

明确禁止：

- 按 `case_id`、文件名、case 顺序、公开验证集前缀写逻辑。
- 读取或依赖 `reference_final_answer`、`gold_trajectory`、`success_check`、隐藏在本地样例中的标准答案。
- 把开发集或验证集里的项目名、物资名、人员名、会议室 ID、日期、订单号、workflow request id 整理成固定答案表。
- 为某个本地 case 的标题、短语、项目、物资、人员、会议室写不可解释的特殊分支。
- 用本地验证集失败 case 反推固定字段值、固定枚举值或固定工具调用序列。

允许：

- 使用本地数据做误差分类、能力评估和回归测试。
- 从失败中抽象通用能力，例如任务图、schema 理解、候选验证、枚举绑定、preflight、错误恢复、final_answer 校验。
- 使用真实工具返回作为当前 case 的证据，但不得把这些证据跨 case 固化成常量答案。

验收标准：

- 每个修复必须能说明它解决的是哪一类通用能力缺口。
- 每个写操作必须能追溯到当前 case 的真实工具证据。
- 如果没有足够证据，应稳定 `blocked` 或追问，而不是硬猜。

## 1. V3 核心判断

前几轮工作中有四个方向是正确的，应保留并升级为主协议：

1. 意图识别：识别用户要做什么、属于哪个域、是否涉及写操作、是否需要确认或追问。
2. Task Graph：把一句话拆成子任务、依赖关系、缺槽、风险字段和禁止猜测字段。
3. 问题上下文维护：维护多轮 messages、已确认槽位、工具证据、候选决策、写入结果。
4. 枚举值对应：把自然语言 hint 映射到 schema enum、browser options、人员、项目、物资等真实候选。

之前不足的核心点是时间预算。正式环境单 case 最多 60 秒，因此 V3 必须把时间、step budget、LLM 次数和 prompt 尺寸作为一等约束。

## 2. 目标执行模型

V3 的目标不是让模型自由调用工具，而是让模型负责理解和判断，程序负责执行和安全。

```text
Input / Messages
  -> TaskGraphExtractor
  -> ContextStore + EvidenceLedger
  -> ToolSchemaRegistry
  -> DomainExecutors
       -> MeetingroomExecutor
       -> WorkflowExecutor
  -> EnumBinder / CandidateRanker
  -> PreflightGuard
  -> FinalAnswerBuilder
```

职责边界：

| 模块 | LLM 负责 | 程序负责 |
| --- | --- | --- |
| TaskGraphExtractor | 意图、子任务、依赖、缺槽、禁止猜测字段 | JSON schema 校验、低置信度降级 |
| ContextStore | 无 | messages、slots、tool evidence、候选、ledger |
| ToolSchemaRegistry | schema 字段语义辅助 | `list_tools`、`workflow.catalog`、`workflow.schema`、browser options 管理 |
| EnumBinder | 语义排序、歧义判断 | 只允许选择真实候选、置信度阈值、歧义收口 |
| DomainExecutor | 错误后重规划建议 | 工具白名单、参数 schema、step budget、真实调用 |
| PreflightGuard | 字段草案修复建议 | required fields、枚举合法性、金额守恒、权限/冲突/证据链 |
| FinalAnswerBuilder | 不直接生成最终答案 | 只从 ledger 生成最终答案并校验一致性 |

## 3. 60 秒预算策略

建议硬预算：

```text
0-6s    fast LLM 生成 task graph；失败则 heuristic 兜底
6-18s   必要 read tools：user/catalog/schema/project/browser/room/list/schedule
18-35s  strong LLM 最多 1 次，用于候选排序或表单草案
35-45s  preflight + 写操作
45-52s  final_answer verifier
52s+    收口模式：禁止新 LLM，禁止新复杂分支，只能 done/blocked
```

默认限制：

- fast LLM 每 case 最多 1 次。
- strong LLM 默认最多 1 次；只有剩余时间充足且收益明确时允许第 2 次。
- final_answer 不调用 LLM。
- 每个工具错误最多触发一轮修复或重规划。
- 接近 deadline 时优先返回已完成域和稳定 blocked。

Fast LLM task graph 阶段是 V3 的必经流程。执行任何 domain executor 之前，必须先完成 task graph 阶段；如果 fast LLM 超时或失败，也必须生成 heuristic fallback task graph，并记录本阶段日志。不能因为某些本地样例看起来简单就绕过该阶段。

必须记录的 task graph 日志字段：

```json
{
  "event": "task_graph",
  "case_id": "case id",
  "mode": "single_turn|multi_turn",
  "source": "fast_llm|heuristic_fallback",
  "started_at": 0.0,
  "elapsed_seconds": 0.0,
  "remaining_seconds": 0.0,
  "fast_timeout_seconds": 0,
  "fast_llm_success": true,
  "task_count": 0,
  "domains": [],
  "intents": [],
  "min_confidence": 0.0,
  "missing_slots_count": 0,
  "must_not_guess_count": 0,
  "fallback_reason": "",
  "prompt_chars": 0,
  "response_chars": 0
}
```

这些日志用于后续动态调整时间预算，例如 fast LLM timeout、是否进入 fallback、复杂题 task 数量、低置信任务比例、剩余时间是否足够进入 strong LLM 阶段。

## 4. Task Graph 协议

V3 task graph 应成为 executor 的输入，而不是旁路提示。

建议结构：

```json
{
  "tasks": [
    {
      "task_id": "wf_1",
      "domain": "workflow",
      "intent": "expense_material",
      "goal": "提交费用类物资申请",
      "source_text": "用户原文片段",
      "slots": {},
      "missing_slots": [],
      "must_not_guess": ["project_code", "material_subclass"],
      "risk": "write",
      "confidence": 0.84,
      "deps": []
    }
  ]
}
```

执行规则：

- 每个 task 独立维护状态：`pending/read_ready/write_ready/done/blocked`。
- 跨域任务通过 `deps` 表示依赖，不允许后执行的域覆盖先前成功结果。
- 对同域多任务必须逐个完成或逐个 blocked，不能只取 primary task。
- 低置信或缺关键槽时，优先追问或 blocked。

## 5. WorkflowExecutor V3

Workflow 是 V3 第一优先级，因为隐藏集更可能通过 workflow 类型、字段、枚举、项目和物资变化拉开差距。

目标流程：

```text
workflow.catalog
  -> workflow.schema
  -> resolve applicant/person/project/browser options
  -> EnumBinder
  -> optional LLM form draft
  -> PreflightGuard
  -> workflow.save or blocked
```

关键约束：

- 不把 `72247`、`34747`、`29023`、`29028`、`detail_2` 当作唯一真理。
- workflow_id、field_id、detail table、required fields 应来自 `catalog/schema` 或真实工具返回。
- 人员、项目、物资、枚举值必须来自当前 case 的真实工具返回。
- 多候选低置信时不能硬选。
- 金额明细必须守恒。
- 用户只给泛类描述时，不强行选择物资小类。

## 6. EnumBinder / CandidateRanker

统一处理：

- `workflow.search_person` 人员候选。
- `workflow.project_search` 项目候选。
- `workflow.browser_search` 大类/小类/options。
- `meetingroom.room.list` 房间候选。
- `meetingroom.booking.list` 已有会议候选。

输出协议：

```json
{
  "decision": "select|ambiguous|need_more_info|blocked",
  "selected_id": "existing candidate id or empty",
  "confidence": 0.0,
  "ranked": [
    {"id": "candidate_id", "score": 0.82, "reason": "简短理由"}
  ],
  "reason": "简短说明"
}
```

程序规则：

- `selected_id` 必须存在于真实候选。
- top1 低置信或 top1/top2 差距小，不能写入。
- ranker 结果必须写入 evidence ledger。
- ranker 不允许输出工具未返回的 id/code/value。

## 7. ContextStore 与 EvidenceLedger

ContextStore 维护：

- 原始 `obs`、`messages`、`now`、`step_budget`。
- task graph 和每个 task 状态。
- 已确认槽位与用户修正。
- read tool evidence。
- candidate decisions。
- write results。
- blocked reason。

EvidenceLedger 原则：

- 所有写操作必须能追溯到当前 case 的 read evidence。
- final_answer 只从 ledger 生成。
- 组合题中各域结果独立记录，最终合并，不互相覆盖。

## 8. PreflightGuard

所有写操作前必须通过 preflight。

会议室写操作：

- 房间来自 `room.list` 或 `room.schedule`。
- 时间、日期、容量、权限、冲突状态可验证。
- cancel/extend/rebook 基于 `booking.list` 返回的真实订单。

Workflow 写操作：

- required fields 完整。
- select/browser/person/project 字段来自真实 options 或 search 结果。
- submit/draft 与用户意图一致。
- 明细表结构来自 schema。
- 金额总额与明细一致。
- 不确定字段必须 blocked 或追问。

## 9. FinalAnswerBuilder

最终答案不交给 LLM 生成。

规则：

- 只从 ledger 的真实执行结果生成。
- 检查 `booking_result` 与会议室工具历史一致。
- 检查 `workflow_draft_result` 与 `workflow.save` 的 submit/draft 状态一致。
- blocked reason 使用稳定枚举。
- 组合题保留各域结果，不因某个域失败覆盖另一个域成功。

## 10. V3 MVP 范围

第一版不要全量重写。

MVP 建议：

1. 新增 `CaseRuntime`：deadline、step budget、LLM 次数、收口模式。
2. 让 `TaskGraph` 成为 executor 输入。
3. 新增 `ContextStore` 与 `EvidenceLedger`。
4. 优先实现 `WorkflowExecutorV3`。
5. 新增统一 `EnumBinder`。
6. 新增统一 `PreflightGuard`。
7. 小范围改造 `FinalAnswerBuilder`。
8. Meetingroom 暂时保留当前策略，只接入 task graph 和 ledger。

MVP 成功标准：

- 不新增基于本地验证集或开发集的固定规则。
- 无模型或模型失败时保持基础可用。
- 模型可用时隐藏集分数应明显高于当前 52-59 区间。
- 平均耗时受控，复杂 case 接近 45 秒后稳定收口。
- `workflow.save` error、`booking_result` / `workflow_draft_result` submission mismatch 明显下降。
