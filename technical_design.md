# 企业流程 Agent 技术方案设计文档

> 版本：v1.0（2026-07-30）
> 状态：可接手开发
> 范围：`submission/` 提交包全部代码；本文档是目标架构规格，第 10 节给出与现有代码的对照和剩余任务。

---

## 1. 目标与约束

### 1.1 任务

实现 `submission/my_agent.py` 中的 `MyAgent`，在离线仿真器（`IFTKEnv`）中理解中文自然语言请求，执行工具调用序列，返回结构化 `final_answer`。任务面：会议室（查询/预订/取消/延长/参会人/候选输出）、流程（请假 72247、费用物资 34747 的草稿保存与提交）、跨域组合、多轮澄清与确认。

### 1.2 评分口径（设计的第一约束）

| 维度 | 分值 | 架构含义 |
|------|------|----------|
| TSR | 60 | evaluator 做**结构化检查**（环境侧状态 + `final_answer` 字段正则匹配），不做语义打分 → 最终状态和字段值必须精确 |
| AS | 20 | 每次工具 error 扣 5 分；forbidden（未授权工具/冲突/未确认写/多余活跃预订）→ 0 分 → 调用前本地校验 + 写护栏，杜绝试错式调用 |
| ES | 10 | 步数越少越高 → DAG 步数确定且贴近最优 |
| RS | 10 | 变体=同义改写/口语化/信息缺失 → 语义理解交给 LLM，流程不变 |

### 1.3 硬约束

- 每 case `step_budget` 8–12 步（`call_tool` 和 `reply` 各消耗 1 步），超出抛 `StepLimitExceeded`。
- 单 case 60 秒超时；未捕获异常 = 该 case 0 分。
- 依赖白名单（`submission_spec.md` §3）；禁止本地模型文件；API key 走环境变量。
- 每天最多提交 5 次，**最终排名以最后一次提交为准**。

### 1.4 设计原则（本方案的核心分工）

**模型提供鲁棒性和语义理解，程序提供流程化和确定性。**

- LLM 只做三件事：意图识别（分解为任务单元）、信息抽取（原始短语级槽位）、语义决策（选项映射，如费用子类选择）。
- LLM **永远不输出**最终数值（日期、时长、金额换算）、不决定调用哪个工具、不生成工具参数。
- 所有时间计算、schema 校验、冲突判断、fallback 排序、阻塞判定由确定性代码完成。
- 每一次 LLM 调用都必须有确定性降级路径（规则/关键词抽取），LLM 全挂时系统仍能跑通简单 case。

---

## 2. 总体架构

```
                          ┌─────────────────────────────────────┐
                          │  MyAgent.run(case_id)               │
                          └───────────────┬─────────────────────┘
                                          ▼
  ┌──────────────┐   obs   ┌─────────────────────────────────────┐
  │  IFTKEnv     │────────▶│ ① 感知层                             │
  │  reset/      │         │  StaticContextStore（离线索引）       │
  │  list_tools/ │         │  ToolContractReconciler（运行时对账） │
  │  call_tool/  │         └───────────────┬─────────────────────┘
  │  reply       │                         ▼
  └──────┬───────┘         ┌─────────────────────────────────────┐
         │                 │ ② 理解层（LLM + 规则降级）            │
         │                 │  IntentRouter → TaskGraph IR         │
         │                 │  SlotExtractor → SlotFrame（原始短语）│
         │                 │  TemporalResolver（代码计算日期/时长） │
         │                 └───────────────┬─────────────────────┘
         │                                 ▼
         │                 ┌─────────────────────────────────────┐
         │                 │ ③ 规划层                             │
         │                 │  SkillComposer：任务单元 → 技能子图    │
         │                 │  拼接为单个可执行 DAG                 │
         │                 └───────────────┬─────────────────────┘
         │                                 ▼
         │    call_tool    ┌─────────────────────────────────────┐
         └────────────────▶│ ④ 执行层                             │
              reply        │  SkillScheduler（DAG 执行器）         │
                           │  PreflightGuard（schema/确认/幂等）    │
                           │  BlockDecisionEngine（阻塞判定）       │
                           │  DialogueManager（多轮澄清/确认）      │
                           │  EvidenceLedger（调用与状态证据账本）  │
                           └───────────────┬─────────────────────┘
                                           ▼
                           ┌─────────────────────────────────────┐
                           │ ⑤ 输出层                             │
                           │  ResultProjector：证据 → final_answer │
                           │  （含降级：无论何处失败都有可返回结果） │
                           └─────────────────────────────────────┘
```

### 2.1 一次 run 的生命周期

1. `env.reset(case_id)` → 拿到 `user_query / now / step_budget / mode / messages`。
2. `env.list_tools()`（不消耗步数预算的前置认知，以运行时返回为准与静态索引对账）。
3. 理解：LLM 一次调用产出「任务单元列表 + 每个单元的原始槽位短语」；`TemporalResolver` 把短语解析成规范值；缺失槽位记录到 `SlotFrame.missing`。
4. 规划：`SkillComposer` 按任务单元选技能子图（§5），按依赖关系（如「先订会议室再请假」通常无依赖，可顺序执行；「取消后重订」有依赖）拼成一个 DAG。
5. 执行：`SkillScheduler` 拓扑序推进节点；多轮 case 先走澄清节点补槽位；写节点前过 `PreflightGuard`；阻塞条件命中即短路该子图并登记 blocked 证据。
6. 输出：`ResultProjector` 从 `EvidenceLedger` 投影出 `final_answer` 并返回。任何异常（含 `StepLimitExceeded`、LLM 超时）都会落到这一步，返回当前已有证据的投影。

### 2.2 步数预算分配策略

调度器持有 `budget_remaining`。原则：

- 每个技能子图声明 `min_steps`（理想路径步数）与 `max_steps`。
- 组合任务按子图 `min_steps` 之和预留；某子图执行超出其 `max_steps` 时强制收敛（放弃重试，走 blocked 或部分结果）。
- 剩余步数 ≤ 1 时禁止发起新的读操作，只允许完成必要的写或直接投影输出。

---

## 3. 感知层

### 3.1 StaticContextStore（已有，维护即可）

打包进提交物的离线索引（`submission/static_context/`），由 `scripts/` 从 train 数据生成：

- `tools.index.json`：22 个工具的 args_schema（与 `contest/train/tool_specs.json` 同源）。
- `workflows.index.json`：流程目录（72247 请假 / 34747 费用）与字段 schema、选项表。
- `meetingrooms.index.json`：139 个会议室的静态属性（园区/楼层/容量/设备/bookable）。
- `prompt_cards/`：路由与策略提示词卡片。

**关键设计**：静态索引只作**先验**，运行时一律以 `env.list_tools()`、`workflow.catalog`、`workflow.schema`、`meetingroom.room.list` 的实际返回为准（case 可通过 `workflow_overrides` 做局部差异）。`ToolContractReconciler` 在 run 开始时对账：静态索引中存在但运行时未公开的工具，从可用集合剔除（防「调用未授权工具」forbidden）。

### 3.2 节假日表

`static_context` 内置 2026 年中国法定节假日与调休表（至少覆盖 train/val 时间窗 2026-04 ~ 2026-06；建议全年）。来源：从 train case 的 gold 数值反推校验（如 beta_wf_0014：4-28→5-6 请年假 duration=32h，可反推 5-1~5-5 为假期）。工作日判定函数：

```python
def is_workday(d: date) -> bool:
    return (d.weekday() < 5 or d in MAKEUP_WORKDAYS) and d not in HOLIDAYS
```

---

## 4. 理解层

### 4.1 LLMGateway（统一 LLM 出入口）

```python
class LLMGateway:
    def structured_call(self, prompt_card: str, payload: dict,
                        output_schema: dict, timeout_s: float,
                        fallback: Callable[[], dict]) -> dict: ...
```

- 输出强制 JSON（response_format 或提示词 + `jsonschema` 本地校验，校验失败重试 1 次后走 fallback）。
- 超时预算：单次调用 timeout 建议 12s；全 case LLM 总预算 ≤ 35s（留 20s+ 给工具调用与本地计算）。
- 模型配置读 `config.json`（不含 key）；key 读环境变量 `OPENAI_API_KEY` / `DASHSCOPE_API_KEY` 等。
- 记录每次调用耗时到运行日志，供本地回归分析超时风险。

### 4.2 IntentRouter（LLM 调用 #1：意图分解 + 槽位抽取，合并为一次调用）

输入：`user_query`、`now`、任务单元类型清单（见 §5 技能清单）、槽位定义。
输出 schema（TaskGraph IR 的理解层部分）：

```json
{
  "task_units": [
    {
      "unit_type": "book_room | cancel_booking | cancel_rebook | extend_booking |
                    manage_participants | list_candidates | query_schedule |
                    leave_draft | expense_draft | query_info",
      "raw_slots": {
        "date_phrase": "下周二", "time_phrase": "下午两点到三点",
        "office_phrase": "A1园区", "capacity_phrase": "10个人",
        "title": "季度复盘", "feature_phrases": ["带屏幕"],
        "leave_type_phrase": "年假", "reason_phrase": "…",
        "approver_phrase": "刘经理", "submit_intent": "submit | draft | unspecified",
        "expense_item_phrases": ["定制促品"], "project_phrase": "…",
        "amount_phrases": ["…"], "target_booking_phrase": "上周订的那个周会"
      },
      "depends_on": []
    }
  ],
  "confidence": 0.0
}
```

规则：

- `raw_slots` 只放**原文短语**，不做归一化（归一化是 TemporalResolver / OptionMapper 的职责）。
- `depends_on` 表达单元间依赖（如 cancel_rebook 内部有序；跨域「订会议室+请假」通常互不依赖）。
- `confidence < 0.55` 或 `task_units` 为空 → 进入兜底路径（§7.4）。
- **降级 fallback**：正则+关键词规则版意图分类器（覆盖高频形态：订会议室/请假/费用/取消/延长），保证 LLM 不可用时仍有基本盘。

### 4.3 TemporalResolver（纯代码）

职责与规则：

| 输入 | 输出 | 规则 |
|------|------|------|
| 「明天/后天/下周二/本周五」+ `now` | `day: YYYY-MM-DD` | ISO 周规则；「下周」= now 所在周的下一周 |
| 「下午两点到三点」 | `start/end: HH:MM` | 12 小时制消歧（下午+2 → 14:00）；「半」→ :30 |
| 请假「X月X号到Y月Y号」 | `start_time/end_time: YYYY-MM-DD HH:mm` | 未说明时刻默认 09:00–18:00；「下午请2小时」→ 14:00–16:00 这类锚点规则从 train 归纳 |
| 请假时长 | `duration`（小时） | 逐日累加：完整工作日计 8h，跨节假日/周末剔除（§3.2）；半天 4h；小时假按实际 |
| 「上午9-11点空闲」逐天/交集搜索 | 迭代日期序列 | 由技能子图驱动，Resolver 只给日期算术 |

全部函数必须有单元测试覆盖 train 中出现的每一种时间表达（跨月、跨午夜、精确小时边界、整周、最小 1 小时）。

### 4.4 OptionMapper（LLM 调用 #2：受限语义映射，仅在需要时发起）

用于「自然语言 → 封闭选项集」的映射，输入永远带候选列表：

- 请假类型 → `leave_type_options`（N/L/S/M/P/H/Y/F/V/AL），原因 → `reason_options`（01–10，注意有 `72247:reason:leave_type=L/S` 的条件选项表）。
- 费用大类/小类 → `workflow.browser_search` 返回的类目树。
- 项目描述 → `workflow.project_search` 候选。
- 审批人 → `workflow.search_person` 候选（结合职位/部门线索）。

输出 schema：

```json
{ "selection": "value 或 null", "candidates_considered": ["..."],
  "unique": true, "abstain_reason": "ambiguous | too_broad | no_match | null" }
```

**保守性约束（写进提示词卡片，也是阻塞判定的输入）**：只有当语义指向**唯一**候选时才返回 `unique=true`；两个以上候选都说得通、或描述比任何候选更宽泛时，必须 `unique=false` 并给出 `abstain_reason`。train 数据的标准答案表明：模棱两可时的正确行为是阻塞，不是猜。

简单映射（请假类型 10 个固定 label）优先走精确/同义词词典，命中则不消耗 LLM 调用；词典不命中才升级 LLM。

---

## 5. 规划层：技能子图库

### 5.1 设计原则

**固化单位是域级子图，不是 scenario。** train 的 180+ scenario 是 `会议室操作 × 流程操作 × 阻塞条件` 的组合爆炸，逐 scenario 固化必然在隐藏集漏项。每个技能子图 = 节点（读工具/写工具/决策/澄清/确认/阻塞出口）+ 边（含条件转移），由 SkillComposer 按任务单元实例化后拼接。

### 5.2 节点类型

| 类型 | 说明 | 消耗步数 |
|------|------|----------|
| `read` | 只读工具调用（room.list / booking.list / schema / search_person…） | 1 |
| `write` | 写工具调用（booking.create / cancel / extend / participant.* / workflow.save / delete） | 1 |
| `clarify` | `env.reply` 提问补槽位（仅 multi_turn 模式启用） | 1 |
| `confirm` | `env.reply` 请求确认（写操作门禁） | 1 |
| `decide` | 本地决策（选房、冲突判断、阻塞判定），不消耗步数 | 0 |
| `block_exit` | 登记 blocked 证据并结束该子图 | 0 |

### 5.3 技能子图规格

#### S1 `book_room`（会议室预订）

```
clarify*(缺失槽位) → room.list(带全部可编码约束)
  → decide:候选过滤与排序
      规则链: bookable=true → 容量≥attendees → 楼层/屏幕/设备约束
              → 冲突剔除(用返回的占用信息或 room.bookings 补查)
              → 排序: 工位/指定楼层同层 > 同楼栋 > 同园区其他 > 容量最小满足
  → [候选=0] block_exit(reason=no_available_room)
  → [multi_turn 且需确认] confirm → booking.create → 校验返回 success
```

- `room.list` 尽量一次带上 `day/office_id/capacity_gte/has_screen/required_features`，减少读步数。
- 冲突判断在本地做：目标时段与占用区间重叠检测（左闭右开）。
- 变体：「逐天找本周最早空闲」= 外层日期迭代包裹 S1 的读+决策段，每天 1 次 `room.list`（或 `room.schedule`），预算内逐天推进；「多天交集」= 多次读后本地求交集再 create。
- 工位关联：先 `user.get_workspace`（+1 读）拿楼栋/楼层，再进入排序规则。

#### S2 `cancel_booking`（取消）

```
booking.list(关键词/时间过滤) → decide:唯一定位
  → [唯一] confirm?(case 要求时) → booking.cancel
  → [多条且 multi_turn] clarify(追问订单号) → booking.cancel
  → [多条且单轮] block_exit(reason=ambiguous_booking)
  → [0 条] block_exit(reason=booking_not_found)
```

「本人的」预订用 `organizer_user_id == current_user` 过滤（必要时先 `user.get_info`）。

#### S3 `cancel_rebook`（取消重订）：S2 → S1 串联，S1 依赖 S2 成功；「换大房」在 S1 决策中带 `capacity > 原房间` 约束。**幂等护栏**：全程只允许净增 1 条活跃预订。

#### S4 `extend_or_keep`（延长）

```
booking.list 定位目标会议（模糊则 clarify 追问订单号）
  → room.bookings 查该房间后续占用
  → decide: [无冲突] booking.extend
            [冲突] 按 case 语义分支:
               默认: 保持原会不动, block_exit(reason=extend_conflict)
               显式要求"冲突则取消重订更长": 走 S3
```

「延长冲突时保持原会」是 train 中反复出现的标准答案——**禁止**在冲突时擅自取消或改时段，除非用户明说。

#### S5 `manage_participants`（参会人）

```
booking.list 定位会议 → participant.list(避免重复添加)
  → [需 user_id] user.get_info / workflow.search_person 解析人名
  → participant.add / remove（批量：schema 支持数组则一次调用，否则逐个）
```

#### S6 `list_candidates`（只出候选不预订）：S1 去掉 create 节点，候选列表直接进 `final_answer.room_candidates`。**识别信号**：「有哪些/帮我看看/推荐一下」且无明确预订指令——这是意图识别的一个易错分叉，提示词卡片需给正反例。

#### S7 `query_schedule` / `query_info`：单读节点（`room.schedule` / `booking.list` / `user.get_workspace` / `oa.todo.list`），结果投影进 final_answer。

#### S8 `leave_draft`（请假流程）

```
workflow.catalog(定位 72247，可用静态索引跳过此步，但需容忍 override → 保留为可选节点)
  → workflow.schema(72247)   # 必调：case 可能 override 字段
  → user.get_info + workflow.search_person(申请人) → applicant/applicant_no
  → workflow.search_person(审批人短语)
      → decide: 候选=1 → 通过
                候选>1 且有职位/部门线索可唯一化 → 选中
                候选>1 且不可唯一化 → block_exit(reason=approver_ambiguous)
  → OptionMapper: leave_type / reason（注意条件选项表 reason:leave_type=*）
  → TemporalResolver: start_time / end_time / duration(工作日×8h)
  → decide: 附件要求（婚假→结婚证、病假→病假条、陪产假→出生证明）
      → file.list 核对附件存在 → 缺失则 block_exit(reason=missing_attachment)
  → decide: 已有同类型草稿？（case 有"需先确认再新建/更新现有草稿"形态）
  → 本地校验 required_fields 全齐 → workflow.save(data=…, submit=按意图)
```

`submit` 语义：「提交/审批」→ `submit=true`；「保存草稿/先存着」→ `submit=false`；未明说 → 默认草稿（train 中 draft 是安全默认，提交是显式动作）。

#### S9 `expense_draft`（费用物资流程）

```
workflow.schema(34747)
  → workflow.project_search(项目短语)
      → decide/OptionMapper: 唯一 → project_name/project_code/wbs_code
                             编码前缀对多项目 / 描述歧义不可唯一 → block_exit(reason=ambiguous_project)
  → workflow.browser_search(大类/小类类目)
      → OptionMapper: 子类唯一 → 通过
                      子类不唯一 / 描述过宽 → block_exit(reason=ambiguous_material_subclass)
  → decide: 明细行构造（数量×单价换算由代码完成），total_amount = Σ明细金额
            —— 三处金额字段（明细、汇总、total_amount）必须一致
  → 本地校验 → workflow.save(submit=按意图)
```

#### S10 `clarify_loop`（多轮澄清，仅 mode=multi_turn）

- 触发：`SlotFrame.missing` 非空。
- 提问模板**逐槽位固定**（user simulator 按关键词匹配回复，措辞从 train gold trajectory 抄）：
  - day/time → 「请问是在什么时间？」
  - office_id → 「请问是在哪个园区？」
  - attendees → 「请问大概多少人参加？」
  - title → 「请问会议主题是什么？」
  - approver → 「请问审批人是谁？」
  - reason → 「请问请假原因是什么？」
  - order_id → 「请问要操作哪个订单号？」
  - 费用槽位 → 「请问项目是哪个？/大类小类是什么？/预算多少？」
- 每次 reply 后用 LLM/规则解析回复短语回填 `SlotFrame`；解析失败一次则用 `fallback` 措辞重问一次，再失败则以已有信息推进或阻塞。
- **只问缺失槽位**（省步数）；一问一答，不合并多槽（simulator 按关键词单槽应答）。
- 用户中途修正（「时间改成…」）：以**最后一次**约束为准——SlotFrame 回填带覆盖语义。

#### S11 `confirm_gate`（确认门禁，见 §6.2）

### 5.4 SkillComposer 拼接规则

- 每个任务单元映射到一个子图实例；`depends_on` 建立子图间边。
- 跨域 case（zh/mr_wf）：默认会议室子图在前、流程子图在后（与 gold 一致），二者无数据依赖时失败互不传染——**一个子图 blocked 不影响另一个继续执行**（大量 case 是「A 成功 + B 阻塞」形态）。
- 组合后校验总 `min_steps` ≤ `step_budget`，超出则裁剪可选读节点（如跳过 catalog、合并 room.list 条件）。

---

## 6. 执行层

### 6.1 SkillScheduler（DAG 执行器）

- 拓扑序推进；节点执行结果（含工具原始返回）全部写入 `EvidenceLedger`。
- 节点失败策略按类型：`read` 失败可重试 1 次（换参数需经 decide 节点批准，禁止盲目重试——每次 error 扣 AS 5 分）；`write` 失败不自动重试，转入 decide 评估（冲突→fallback 或 blocked）。
- 每步前检查 `budget_remaining`；捕获 `StepLimitExceeded` 后立即跳到输出层。

### 6.2 PreflightGuard（写操作三重门禁）

写节点（`booking.create/cancel/extend`、`participant.*`、`workflow.save/delete`）执行前必须依次通过：

1. **Schema 门**：参数经 `jsonschema` 对 `list_tools()` 返回的 args_schema 校验；必填缺失/类型不符/pattern 不符 → 拒绝调用，回到 decide 或 clarify。**任何未过校验的参数不允许出网。**
2. **确认门**：multi_turn 模式且该写操作属于高风险（create/cancel/extend/save(submit=true)/delete）→ 必须已存在一次肯定确认回复。确认话术：「可以的话我现在直接帮你预订，确认吗？」；肯定判定用规则词表（可以/好的/确认/直接订）+ LLM 兜底。**未确认执行高风险写 = forbidden = AS 0 分**，此门禁不允许任何 bypass。
3. **幂等门**：`EvidenceLedger` 记录每个写意图指纹（工具名 + 规范化参数摘要）。同指纹的 create 已成功 → 拒绝二次执行（防「存在额外新增活跃会议预订」forbidden）；cancel_rebook 场景校验净增活跃预订 ≤ 1。

冲突预检：`booking.create` 前必须存在本地冲突判断证据（room.list 返回的占用或 room.bookings 补查），目标时段无重叠才放行——「会议预订时间与房间占用冲突」是 forbidden。

### 6.3 BlockDecisionEngine（阻塞判定，确定性规则表）

| 编号 | 条件 | reason（写入 final_answer） |
|------|------|------|
| B1 | 审批人搜索候选 >1 且职位/部门线索不可唯一化 | `approver_ambiguous` |
| B2 | 费用子类语义候选 >1（OptionMapper `unique=false`） | `ambiguous_material_subclass` |
| B3 | 描述比类目更宽（OptionMapper `abstain_reason=too_broad`） | `ambiguous_material_subclass` |
| B4 | 项目编码前缀/描述对应多个项目 | `ambiguous_project` |
| B5 | 会议室候选为 0（含 fallback 链穷尽） | `no_available_room` |
| B6 | 取消/延长目标不可唯一定位且不可追问（单轮） | `ambiguous_booking` |
| B7 | 延长遇冲突且无用户改期授权 | `extend_conflict`（保持原会） |
| B8 | 必需附件缺失（婚假/病假/陪产假） | `missing_attachment` |
| B9 | 多轮澄清后槽位仍不可唯一 | 对应域的 ambiguous reason |

原则：**规则能表达的阻塞判定不交给 LLM**；LLM 只提供 B2/B3/B4 的语义映射结果（`unique/abstain_reason`），判定动作本身是代码。阻塞时**绝不写入**，且已成功的兄弟子图结果照常输出。

### 6.4 EvidenceLedger

追加式账本，记录：每次工具调用（名称/参数/返回/是否 error）、每次 reply 问答、每个决策节点的输入输出、写操作指纹、blocked 事件。它是 ResultProjector 的唯一数据源，也是幂等门与调试回放的依据。

---

## 7. 输出层与可靠性

### 7.1 ResultProjector

从 EvidenceLedger 投影 `final_answer`，字段形状**从 train 全量 `reference_final_answer` 归纳成投影模板**（开发任务 T8 产出对照表），核心形态：

```json
// 预订成功
{"booking_result": {"status": "success", "day": "…", "office_id": "…",
                    "room_id": "…", "start": "…", "end": "…", "title": "…"}}
// 流程草稿/提交
{"workflow_draft_result": {"status": "draft_saved | submitted", "workflow_id": 72247,
                           "start_time": "…", "end_time": "…", "leave_type": "…"}}
// 阻塞
{"workflow_draft_result": {"status": "blocked", "reason": "ambiguous_material_subclass"}}
// 候选输出
{"room_candidates": [{"room_id": "…", "capacity": 12, "…": "…"}]}
```

跨域 case 输出多个顶层键并存；数值/字符串类型与 reference 严格一致（`workflow_id` 是 int）。**所有 id 类值运行时解析，禁止硬编码 train 常量**（72247 只允许出现在静态先验里，最终以 catalog/schema 返回为准）。

### 7.2 异常与超时策略（0 分防线）

```python
def run(self, case_id):
    answer_holder = FinalAnswerHolder()          # 全程可投影
    try:
        deadline = monotonic() + 52              # 60s 留 8s 安全边际
        …主流程，每个阶段检查 deadline…
    except StepLimitExceeded:
        pass
    except Exception:
        log…
    return answer_holder.project()               # 永不 raise、永不返回 None
```

- 全局 deadline 52s：越过后跳过所有剩余 LLM 调用（只走规则降级）并尽快投影。
- LLM 单次 timeout 12s，失败即降级，不做多次重试。
- `run()` 顶层 try/except 兜住一切；空结果也返回 `{}` 而不是抛异常。

### 7.3 LLM 调用预算（典型 case）

| 调用 | 时机 | 必需性 |
|------|------|--------|
| #1 意图分解+槽位抽取 | run 开始 | 必发（有规则降级） |
| #2 OptionMapper | 仅费用子类/项目/审批人歧义等需要语义映射时 | 按需，0–2 次 |
| #3 回复解析 | 多轮每次 reply 后 | 优先规则解析，规则失败才发 |

典型 case 1–3 次调用，最坏 5 次 ≈ 30s 内。

### 7.4 兜底路径

`IntentRouter` 置信度低 / 无匹配技能时，进入**受限通用循环**：LLM 在「当前证据 + 工具 schema」上下文中逐步提议下一个动作，但每个动作仍必须通过 PreflightGuard 三门禁与预算检查。目标是从隐藏集的陌生形态中抢部分 TSR，而不是保证全对。该路径的写操作确认要求与主路径一致。

---

## 8. 关键数据结构（IR 定义）

```python
@dataclass
class SlotFrame:
    values: dict[str, Any]          # 规范化后的槽位值
    raw: dict[str, str]             # LLM 抽取的原始短语
    missing: list[str]              # 该技能 required 但未获取
    history: list[SlotUpdate]       # 覆盖历史（"以最后一次为准"）

@dataclass
class TaskUnit:
    unit_type: str                  # §4.2 枚举
    slots: SlotFrame
    depends_on: list[str]
    skill_graph: SkillGraph | None  # Composer 填充

@dataclass
class SkillNode:
    node_id: str
    kind: Literal["read","write","clarify","confirm","decide","block_exit"]
    tool: str | None
    args_builder: Callable[[Context], dict]     # 从 SlotFrame+证据构参
    guards: list[str]                           # 引用 PreflightGuard 规则
    on_success: str | None                      # 下一节点
    on_branch: dict[str, str]                   # decide 分支表

@dataclass
class Evidence:
    kind: Literal["tool_call","reply","decision","blocked","write_fingerprint"]
    payload: dict
    ts: float
```

---

## 9. 测试与评估体系

### 9.1 分层测试

| 层 | 内容 | 工具 |
|----|------|------|
| 单元 | TemporalResolver 全部时间表达、duration/工作日、OptionMapper 词典、冲突区间判断、schema 校验、投影模板 | `pytest`（`tests/`） |
| 子图 | 每个技能子图在 mock env 上的路径覆盖（成功/阻塞/降级各至少 1 条） | mock IFTKEnv |
| 集成 | train 200 + val 50 全量回归 | `contest/simulator/test_runner.py --parallel 4 --output results.json` |
| 故障注入 | LLM 超时/坏 JSON/限流下全量跑分不归零 | gateway 注入开关 |

### 9.2 回归看板（扩展现有 `scripts/compare_evaluation_runs.py`）

- 按 `tags` 聚合（meetingroom/workflow/leave/expense/cross_domain/multi_turn/clarification/confirmation/blocked…）输出 TSR/AS/ES/RS 通过率矩阵。
- 每次改动对比基线 run，列出回归 case 清单（case_id + 失败的 must_satisfy 条目）。
- ES 审计：实际步数 vs `gold_trajectory` 长度，超 2 步的 case 单列。

### 9.3 验收标准（本地口径）

- train+val 全量：TSR ≥ 92%、AS 扣分 case ≤ 3%、forbidden 触发 = 0、平均 ES ≥ 8、超时 case = 0、异常 0 分 case = 0。
- 故障注入（LLM 100% 超时）：总分不低于纯规则基线。
- Docker 镜像内跑通 `submission_spec` 检查清单全部项。

---

## 10. 现状对照与开发任务分解

现有 `submission/my_agent.py`（约 1.5 万行单文件）已实现大部分骨架，类与本设计的映射：

| 设计模块 | 现有实现 | 状态 |
|----------|----------|------|
| StaticContextStore / 对账 | `StaticContextStore`、`ToolContractReconciler`、`EffectiveToolRegistry` | 已有，审计对账剔除逻辑 |
| 理解层 | `SlotResolver`、`TemporalIR`、`CaseCalendar` | 已有，按 §4.3 补齐用例并验证节假日表 |
| 规划/执行 | `BusinessSkillRegistry`、`SkillScheduler`、`ReadPlan(Executor)`、`TaskGraphContractNormalizer` | 已有，对照 §5 核对子图覆盖 |
| 护栏 | `PreflightGuard` | 已有，逐条核对 §6.2 三门禁 |
| 证据/投影 | `EvidenceLedger`、`ResultProjectionRegistry`、`SemanticFactStore` | 已有，对照 §7.1 模板表核对 |

### 任务清单（按优先级）

| # | 任务 | 交付物 | 验收 |
|---|------|--------|------|
| T1 | 全量基线跑分：train+val 250 case，产出 tag 级看板 | `reports/runs/` 基线 + 看板脚本扩展 | 看板可复现 |
| T2 | 阻塞判定引擎对照 §6.3 规则表逐条审计/补齐，重点 B2/B3/B4 的 OptionMapper 保守阈值 | 规则表实现 + 子图测试 | blocked 类 tag 通过率 ≥ 95% |
| T3 | 多轮对话：澄清模板对齐 train gold 措辞、确认门禁审计、回复解析降级 | S10/S11 实现 | mt 类 tag 全过 |
| T4 | 时间体系：节假日表落库 + duration/工作日单测全覆盖 | `static_context` 节假日 + tests | wf 时间类 case 全过 |
| T5 | 可靠性：全局 deadline、LLM 故障注入开关、`run()` 0 分防线审计 | gateway 注入 + 集成测试 | 故障注入验收达标 |
| T6 | 幂等门与净增预订校验（cancel_rebook / 重试路径） | PreflightGuard 补强 | forbidden = 0 |
| T7 | 兜底受限通用循环（§7.4） | fallback 路径 + 低置信路由测试 | 陌生 query 冒烟不归零 |
| T8 | `reference_final_answer` 全量归纳 → 投影模板对照表 | 文档 + ResultProjector 校对 | 投影字段与 reference 逐键一致 |
| T9 | 提交流程：打包脚本、requirements 白名单校验、Docker 验证、提交前检查清单自动化 | `scripts/package_submission.py` | Docker 内全量跑通 |

依赖：T1 先行（建立度量），T2–T6 并行，T7/T8 随后，T9 收尾。每项完成必须过 T1 看板回归无倒退后才合入。

---

## 11. 风险与对策

| 风险 | 对策 |
|------|------|
| scenario 级过拟合，隐藏集换组合即漏 | 域级子图组合（§5）；禁止 case_id/常量特判；兜底路径（§7.4） |
| 阻塞判定召回不足（该 block 没 block → forbidden/TSR 双输） | 规则化判定 + OptionMapper 保守阈值；blocked 类 tag 单独看板 |
| LLM 超时导致整 case 0 分 | 全局 deadline + 单调用 12s + 全链路规则降级 + 顶层兜底返回 |
| case override 与静态索引漂移 | 运行时 schema/catalog 必调对账，静态索引仅作先验 |
| 重复写入触发 forbidden | 写指纹幂等门 + 净增活跃预订校验 |
| 线上以最后一次提交为准 | 提交前必须走 T9 自动化检查 + Docker 全量回归；禁止未回归的"顺手改动"进最终包 |
