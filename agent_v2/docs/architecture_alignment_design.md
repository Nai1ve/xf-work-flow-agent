# Agent V2 架构对齐设计

> 日期：2026-07-31
>
> 状态：待用户复核
>
> 参考：`technical_design.md` v1.0
>
> 范围：先完成 `agent_v2/` 骨架调整，再按会议、Workflow、多轮、跨域逐类提分

## 1. 目标

本轮不以增加 Case 特判为目标，而是先将现有 V2 调整为边界清晰、可审计、可逐类优化的纯 Python DAG Agent。

最终业务目标是 Train + Val 全系统正确率达到 85%。实施顺序为：

1. 完成架构骨架对齐并确保 250 个 Case 全部可运行。
2. 优先优化会议类，使 Train + Val 会议 Case 合计通过率达到至少 85%。
3. 依次优化 Workflow、多轮和跨域。
4. 全量回归并以总体正确率 85% 为最终验收门槛。

评分通过统一使用官方结果中的 `task_passed=true`，不以平均总分代替正确率。

## 2. 设计原则

- 模型控制变化：意图拆分、原始语义槽位提取、封闭候选语义排序。
- 程序控制不变：日期和金额计算、工具顺序、字段依赖、确认、写前检查、阻塞条件、结果验证。
- 模型不得生成工具名、字段 ID、Workflow ID、人员 ID、项目编码、房间 ID 或枚举值。
- 所有业务 ID 必须来自静态 Registry 或实时工具证据；执行时以实时工具返回为最高权威。
- Task DAG 动态组合，Skill 子图稳定；重试封装在节点内部，DAG 不产生回边。
- Domain Result 与 Submission Projection 分离；Projection 不重新推断业务语义。
- 不读取 Case ID、gold trajectory、success check 或参考答案作为运行时决策输入。
- 旧 `submission/` 不作为 V2 fallback，现有未提交修改保持不动。

## 3. 当前实现与目标边界

现有 V2 已具备以下可保留组件：

- `ToolRegistry`、`WorkflowRegistry`、`MeetingroomDirectory`
- `IntentGraph`、`TaskSpec`、`NodeSpec`、`DomainResult`
- `BudgetAllocator`
- `DAGExecutor`
- `SkillCatalog`、`SkillHandlers`
- `RoomAvailabilityOperator`、`BookingResolverOperator`
- `ProjectionContract`

当前主要架构缺口：

1. `AgentRuntime` 同时承担生命周期、日历、对话、模型降级、工具网关、投影拼装和异常处理，职责过多。
2. `PreflightGuard`、`BlockDecisionEngine`、`DialogueManager` 尚未形成独立边界，部分规则散落在 Skill 与 Runtime 中。
3. 模型输出直接接近规范化实体，缺少“原始短语 → 确定性归一化”的完整隔离。
4. Evidence 主要用于节点间传值，尚未成为写护栏、领域验证和最终投影的唯一事实源。
5. Projection 仍包含跨 Task 状态拼接和意图分支，容易产生“业务成功但提交字段错误”。
6. 缺少统一全局 deadline 和阶段级超时收敛策略。
7. 模型调用失败时，简单会议和简单 Workflow 的确定性路由降级覆盖不足。

## 4. 目标模块

### 4.1 感知与运行上下文

新增 `utils/context.py`：

```text
StaticContextStore
RuntimeContractSnapshot
RunContext
DeadlineController
```

职责：

- 加载编译后的工具、Workflow、会议室和日历资源。
- 使用 `env.list_tools()` 对静态工具契约进行运行时对账。
- 保存 `now`、日历上下文、mode、step budget、messages 和 deadline。
- 提供只读共享上下文，但不共享业务 Task Evidence。
- 在 52 秒软 deadline 后停止新的模型调用和非必要读操作，保留必要写闭环与最终投影时间。

权威顺序：

```text
实时工具结果 > 运行时工具 Schema > 编译 Registry > 模型语义
```

### 4.2 理解层

新增 `utils/understanding.py`：

```text
LLMGateway
IntentRouter
SlotFrameCompiler
TemporalResolver
OptionMapper
RuleFallbackRouter
```

模型首次调用只输出：

```text
task_id
intent
raw_slots
hard_constraints
soft_preferences
missing_information
submit_mode
dependencies
budget_proposal
confidence
```

其中 `raw_slots` 保留用户原始短语。模型不输出规范日期、业务 ID 或最终工具参数。

确定性编译流程：

```text
raw_slots
→ CalendarContext / TemporalResolver
→ Location Resolver
→ Registry-backed OptionMapper
→ SlotFrame
→ TaskSpec
```

`OptionMapper` 只允许返回输入候选的索引。无法唯一映射时返回 `ambiguous`，不猜测。

`RuleFallbackRouter` 覆盖无需语义推理的基础请求：

- 单次会议预订、查询、取消、延长
- 工位查询
- 请假或费用的显式草稿/提交
- 字面订单号、人员工号、房间号

模型错误、超时或非法 JSON 时，基础请求仍可进入稳定 Skill；无法安全理解的写操作返回 blocked。

### 4.3 规划层

新增 `utils/planning.py`：

```text
TaskGraphValidator
SkillComposer
TaskBindingCompiler
```

职责：

- 校验 Task ID、依赖和输入绑定，拒绝环与越域绑定。
- 将 TaskSpec 绑定到稳定 Skill 子图。
- 为跨 Task 数据建立显式 `input_bindings`。
- 使用 `SkillCostCatalog` 校准模型预算建议。
- 在保持用户顺序的前提下生成稳定拓扑序。

预算原则：

- 模型只能建议 `min/target/max/priority`。
- 程序根据 Skill 固定成本、时段数、比较房间数和写入数计算可信上下界。
- 每个 Task 先获得最小闭环预算；完成后回收未使用预算。
- 预算不足时不启动无法完成写闭环的 Task。
- 多日期查询成本按真实日期段和写入数动态计算，不新增 scenario Skill。

### 4.4 执行层

新增：

```text
utils/guards.py
utils/dialogue.py
utils/evidence.py
```

#### PreflightGuard

所有写调用依次经过：

1. Schema 门：工具存在、参数字段合法、必填齐全、类型和枚举正确。
2. Evidence 门：候选、冲突检查、目标记录或表单字段均有可追溯证据。
3. Confirmation 门：multi-turn 高风险写已有有效确认。
4. Idempotency 门：相同写指纹未成功执行；取消重订净增活跃预订不超过 1。
5. Budget/Deadline 门：剩余资源足以完成写操作和结果投影。

Guard 只返回结构化结论，不直接调用工具：

```text
allowed
reason
required_evidence
write_fingerprint
```

#### BlockDecisionEngine

集中维护确定性阻塞原因：

- 候选不存在
- 候选不唯一
- 会议目标不唯一
- 延长冲突且无改期授权
- Workflow 字段、附件、审批人、项目或物资类目不唯一
- 多轮澄清后仍缺少关键信息

Skill 不再自行发明公共 reason；内部错误映射到固定公共语义。

#### DialogueManager

职责：

- 仅在 `mode=multi_turn` 且信息可通过追问解决时追问。
- 每次只询问一个缺失槽位。
- 用户未提供有效结果时最多再问一次；仍无结果则 blocked。
- 用户修正覆盖旧槽位，并保留 SlotUpdate provenance。
- 确认和信息澄清分离，确认回复不能被当作业务槽位。

#### EvidenceLedger

追加式记录：

```text
tool_call
tool_result
dialogue_turn
decision
candidate_set
selected_candidate
blocked
write_fingerprint
domain_result
```

每条 Evidence 包含：

```text
task_id
node_id
kind
value
source
provenance
timestamp
```

Task Evidence 默认隔离。共享日历、当前用户、工位和 Workflow Schema 只能通过只读缓存引用；跨 Task 业务数据必须使用显式 binding。

### 4.5 领域层

现有 `utils/operators.py` 和 `utils/skills.py` 保留，但职责收紧。

Operator：

- 只做可复用的只读领域计算。
- 输入为 SlotFrame、Registry 和 Evidence。
- 输出候选集合、排序证据和选择结果。
- 不执行写操作，不生成最终 Submission。

Skill：

- 只表达稳定 SOP、节点依赖和失败出口。
- 不根据 Case 或完整句式选择流程。
- 不直接进行跨 Task 投影拼接。
- 所有写节点统一经过 PreflightGuard。

四个 Skill 家族保持：

- `MeetingQuerySkill`
- `MeetingMutationSkill`
- `WorkflowFormSkill`
- `WorkflowRecordSkill`

会议中的单日期、多时段、多日期、同房间交集和多房间比较继续由 `RoomAvailabilityOperator` 参数化完成，不为每个场景新增 Skill。

### 4.6 验证与输出层

新增 `utils/validation.py`，调整 `utils/projection.py`：

```text
DomainValidator
ResultProjector
ProjectionSchemaRegistry
```

`DomainValidator` 在 Skill 结束时生成唯一的 `DomainResult`：

```text
task_id
intent
status
operation
entities
evidence_refs
reason
```

`ResultProjector` 只接受 DomainResult 和其引用的 Evidence，不读取用户原文，不再次选择候选，不重新计算日期。

Projection 模板按“业务操作”而不是临时 intent 分支定义：

- booking created
- booking cancelled
- booking extended
- booking rebooked
- participant added/removed/listed
- room/workspace/booking queried
- workflow draft saved/submitted/replaced
- blocked/partial

跨 Task 合并由顶层 `ProjectionAssembler` 根据 operation 合并；单个 Task 的额外字段不得覆盖兄弟 Task 的官方字段。

## 5. 主链路

```text
env.reset / env.list_tools
→ RunContext + RuntimeContractSnapshot
→ IntentRouter
→ SlotFrameCompiler
→ TaskGraphValidator
→ BudgetAllocator
→ SkillComposer
→ DialogueManager（按需）
→ DAGExecutor
   → Operator
   → BlockDecisionEngine
   → PreflightGuard
   → ToolGateway
→ DomainValidator
→ EvidenceLedger
→ ResultProjector
→ final_answer
```

任何阶段异常均转换为 Evidence 和 DomainResult，不允许异常逃出 `MyAgent.run()`。

## 6. 迁移策略

采用增量重构，不同时重写所有模块。

### M1：运行上下文与可靠性

- 引入 `RunContext` 和 `DeadlineController`。
- 将 Runtime 内的 per-run 可变状态迁入上下文，避免多个 Case 之间泄漏。
- 保持现有业务行为不变。

### M2：EvidenceLedger

- 扩展现有 EvidenceStore 为追加式账本。
- 保留兼容读取接口，逐步迁移 Skill。
- 所有工具调用和决策写入 Ledger。

### M3：Guard、Block 与 Dialogue

- 抽离写前校验、公共阻塞 reason、多轮追问和确认。
- 先通过委托方式接入旧 Runtime，再删除重复逻辑。

### M4：理解层分界

- LLM 输出迁移为 raw slots。
- 确定性编译 SlotFrame、日期、位置和封闭选项。
- 引入模型失败的基础规则降级。

### M5：Planning 与 SkillComposer

- 集中 Task 图校验、binding 和预算编译。
- SkillCatalog 只维护稳定子图模板。

### M6：DomainValidator 与 Projection

- 先建立新的 DomainResult 契约。
- 为旧 payload 添加临时适配器。
- 迁移 Projection 后删除 intent 驱动的二次推断。

### M7：骨架全量验收

- 运行单元、契约、Skill、Train 200、Val 50。
- 生成新旧架构差异报告。
- 只修架构迁移回归，不在本阶段加入类别特判。

### M8：会议专项

- 以新 Ledger 重新统计会议失败链路。
- 先修 Projection 系统性错误，再修预算、fallback、比较排序、取消重订和多日期执行。
- Train + Val 会议通过至少 66/77。

## 7. 测试策略

每个迁移部分必须遵守 RED → GREEN → REFACTOR：

1. 先写表现目标架构契约的失败测试。
2. 运行并确认因缺少目标能力而失败。
3. 编写最小生产实现。
4. 运行定向测试和全量单元测试。
5. 完成审核和修复后运行代表性 E2E。

测试层：

- 单元：Context、Deadline、SlotFrame、日期、OptionMapper、Guard、Block、Evidence、Projection。
- 契约：所有工具参数、候选 provenance、Task Evidence 隔离、跨 Task binding。
- Skill：四个家族的成功、blocked、failed 和 partial。
- 故障注入：模型超时、坏 JSON、工具 error、step limit。
- E2E：Train 200 + Val 50。
- 打包：资源、依赖、密钥和官方目录结构。

架构迁移期间设置两条回归线：

- 结构红线：不得出现未捕获异常、未校验写调用或跨 Task Evidence 泄漏。
- 分数观察线：记录每次分数变化；若分数下降，必须列出回归 Case 和根因。架构阶段允许短暂定向回归，但 M7 合并前不得低于当前基线。

当前基线：

- Train：64/200，32.00%
- Val：18/50，36.00%
- 合计：82/250，32.80%
- 会议合计：46/77，59.74%

## 8. 两轮审核机制

每个 M1–M8 单独维护审核记录：

```text
agent_v2/reports/reviews/<module>_review.md
```

审核轮次最多两轮。

### R1

- 检查设计契约、实现边界、测试真实性、工具安全和回归。
- Critical 和 Important 问题必须修复。
- 记录问题、根因、修复方案、修改文件和验证命令。

### R2

- 验证 R1 修复。
- 检查新增回归和遗漏边界。
- 再次修复 Critical 和 Important 问题。
- 两轮后仍存在的问题进入 `Open Issues`，注明风险、影响 Case、临时保护和后续处理条件。

每部分报告固定包含：

```text
Scope
Tests Before
Review R1 Findings
R1 Fixes
Review R2 Findings
R2 Fixes
Open Issues
Regression Result
```

## 9. 架构阶段验收

M7 完成时必须满足：

- Train 200 和 Val 50 全部可运行。
- 未捕获异常为 0。
- 18 个 capability 均由纯 V2 Skill 路由。
- 所有工具调用通过运行时 ToolRegistry。
- 所有写节点通过 Schema、Evidence、Confirmation、Idempotency 和 Budget/Deadline 门。
- 模型输出不包含可执行工具名或业务 ID。
- Task Evidence 隔离；跨域只通过显式 binding。
- 模型不可用时，基础请求有确定性降级。
- Projection 只消费 DomainResult 和 Evidence。
- 完成 M1–M7 的两轮审核记录。
- 全量正确率不低于当前 82/250 基线。

## 10. 类别优化顺序

骨架验收后按以下顺序推进：

1. 会议：目标至少 66/77，85%。
2. Workflow：先稳定字段、时间、项目与类目映射。
3. 多轮：完善追问、修正、确认和无答案收敛。
4. 跨域：完善预算、Task 独立失败和 Projection 合并。
5. 全量：目标 Train + Val 合计正确率至少 85%。

类别优化仍遵守“模型控制语义变化、Skill 控制流程、Operator 控制领域计算、Guard 控制写安全、Projection 只做证据映射”。
