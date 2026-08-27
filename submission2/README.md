# 企业流程 Agent（v3 重写）

基于 [`technical_design.md`](technical_design.md) 从零实现的企业流程执行 Agent，面向「会议室操作 × 流程草稿/提交 × 多轮澄清与确认」任务面。设计原则：**模型提供鲁棒性与语义理解，程序提供流程化与确定性**（详见设计文档 §1.4）。

## 目录结构

```text
technical_design.md   # 目标架构规格（本分支唯一的设计依据）
contest/
  simulator/          # 官方离线模拟器：IFTKEnv、evaluator、tools、test_runner（纯 stdlib）
  train/              # 本地公开训练集 200 case + data + tool_specs.json（gitignored）
  val/                # 本地公开验证集 50 case + data + tool_specs.json（gitignored）
submission/
  my_agent.py         # 提交入口：MyAgent（run 永不 raise、永不返回 None）
  config.json         # 可提交默认配置，不含 API key
  config.local.example.json  # 本地私密配置模板
  static_context/     # 感知层离线索引（构建脚本生成并提交：tools/meetingrooms/manifest）
  utils/              # 分层实现模块（感知/理解/规划/执行/输出），逐步填充
    logger.py         # 控制台分层日志（[感知层/对账] 等层前缀 + section 分节）
    static_context.py # StaticContextStore：离线静态索引加载与查询（先验合同/目录）
    tool_contract.py  # ToolContractReconciler + EffectiveToolRegistry：运行时对账与防 forbidden 门禁
tests/                # pytest 测试
scripts/
  run_agent.py        # 本地评估入口：拼装 tmp/contest_{split}/ 后调官方 test_runner.py
  summarize_run_results.py
  dashboard.py        # tag 级回归看板（TSR/AS/ES/RS 通过率矩阵 + ES 审计 + §9.3 验收）
  build_static_context.py  # 离线构建静态上下文索引（train 数据 → static_context/）
```

## 开发环境

- 需要 **Python 3.11**（模拟器使用 `dict | None` 等新语法）。
- 创建并激活虚拟环境：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r contest/simulator/requirements.base.txt pytest
```

- 首次运行前需把比赛数据解压到 `contest/train`、`contest/val`（gitignored）。
- LLM key 走环境变量（`OPENAI_API_KEY` / `DASHSCOPE_API_KEY` / `ANTHROPIC_API_KEY`），
  或复制 `submission/config.local.example.json` 为 `submission/config.local.json`（gitignored）。

## 常用命令

冒烟（5 个验证集 case，确认评估闭环）：

```bash
.venv/bin/python scripts/run_agent.py \
  --agent submission/my_agent.py \
  --split val --limit 5 --parallel 4 --python .venv/bin/python
```

官方规则基线（正向得分对照）：

```bash
.venv/bin/python scripts/run_agent.py \
  --agent contest/simulator/simulator/baseline_agent.py \
  --split val --limit 5 --parallel 4 --python .venv/bin/python
```

单个 case：

```bash
.venv/bin/python scripts/run_agent.py \
  --agent submission/my_agent.py --split val \
  --case beta_mr_0001 --verbose --python .venv/bin/python
```

测试：

```bash
.venv/bin/python -m pytest tests/ -v
```

## 基线评估（T1）

全量基线跑分（train 200 + val 50，官方 baseline_agent）：

```bash
.venv/bin/python scripts/run_agent.py \
  --agent contest/simulator/simulator/baseline_agent.py \
  --split train --parallel 4 --python .venv/bin/python \
  --output "$(pwd)/reports/baseline/baseline_agent_train.json"
.venv/bin/python scripts/run_agent.py \
  --agent contest/simulator/simulator/baseline_agent.py \
  --split val --parallel 4 --python .venv/bin/python \
  --output "$(pwd)/reports/baseline/baseline_agent_val.json"
```

tag 级回归看板（按 technical_design.md §9.2/§9.3 聚合：域/mode/难度/tag 的
TSR/AS/ES/RS 通过率矩阵 + ES 审计 + 验收清单 + 低分 case）：

```bash
.venv/bin/python scripts/dashboard.py \
  --results reports/baseline/baseline_agent_train.json \
           reports/baseline/baseline_agent_val.json \
  --split auto --label baseline_full
```

每次改动后回归对比（看板 `comparison` 段列出回退/提升 case）：

```bash
.venv/bin/python scripts/dashboard.py \
  --results reports/baseline/my_agent_train.json \
  --split train --compare-to reports/baseline/baseline_agent_train.json \
  --label my_agent_vs_baseline
```

基线结果摘要（2026-08-06）：全量 avg 33.08，通过率 9.6%（24/250），
AS 扣分 90.4%，forbidden 1。详细分析见 `reports/analysis/BASELINE_ANALYSIS.md`。

## 感知层（P1，最小会议室闭环第一步）

离线构建静态上下文索引（`submission/static_context/`），数据与官方 train 数据
同源、可复现、可哈希校验：

```bash
.venv/bin/python scripts/build_static_context.py
```

产物：`tools.index.json`（22 工具 args_schema + 写名单）、`meetingrooms.index.json`
（139 会议室静态属性 + 二级索引）、`manifest.json`（来源 sha256 + train/val 一致性）。

运行时（`MyAgent.run`）在每个 case 执行：`reset` → `list_tools()` → 与静态索引
**对账**（运行时未公开的工具进 `disabled`，防「调用未授权工具」forbidden）→
输出感知层日志。随后进入理解层 + 执行层（下一节）。

```bash
.venv/bin/python scripts/run_agent.py \
  --agent submission/my_agent.py --split val --limit 3 --parallel 1 \
  --python .venv/bin/python --no-analysis
```

控制台日志形如：

```text
INFO [感知层] 静态上下文已加载: tools=22 write_tools=7 rooms=139 dir=.../static_context
INFO [感知层] ──────── case=beta_mr_0011 ────────
INFO [感知层/对账] 对账完成: available=22 unmapped=0 disabled=0 schema_changed=0
```

## 理解层 + 执行层（意图识别重构第一步 + S1/S2 最小桥接）

理解层（`submission/utils/understanding.py`）的**识别入口**是粗粒度意图识别
（Agent 架构：意图识别 = 分解 + 路由，**不做参数抽取**）：

- `IntentRecognizer.analyze(user_query, now, mode, gateway)` → `TaskGraphIR`：
  把 user_query 分解为 N 个子问题，每个子问题路由到 3 类粗粒度业务单元
  （`meeting` / `leave` / `budget`）；单元词表不区分 book/cancel/extend 等细粒度，
  由各域 SOP 内部处理；输出 `task_units + confidence + source + elapsed`，**无槽位**；
- LLM #1 必发：经 `utils/llm_gateway.py`（LLMGateway，§4.1）调用线上模型（强制 JSON
  + 本地 schema 校验 + 重试一次 + 兜底，预算 ≤35s）；`confidence<0.55` / 空 /
  LLM 不可用 → 规则兜底（`source="fallback"`）；
- **LLM 调用耗时统计**：每 case 打印 `LLM 统计`（calls/success/failed/总耗时/均耗），
  进程退出经 atexit 汇总 `[LLM] 进程级调用统计`（跨 case 聚合），供审计意图识别耗时；
- 会议域细分化与参数抽取（`IntentRouter` 细粒度 + `TemporalResolver` +
  `MeetingConstraintExtractor`）保留，供**桥接**阶段临时喂执行器（不追分）：
  - `TemporalResolver`：今天/明天/下周X/X月X日、起止时刻（上午/下午/半）、
    「最早能订上」逐天搜索区间、多日同会议室（周三和周四）；
  - `MeetingConstraintExtractor`：园区/楼栋/楼层 → 有序 `office_address`、
    容量、屏幕、主题、参会人数、工位偏好、时段柔性（±30 分钟回退）。

执行层（`submission/utils/executor.py`）只做 S1 预订与 S2 查询的**最小闭环**：

- S1：单日预订、多日同会议室交集（S1d）、逐天最早搜索（S1d）、工位关联选址
  （S1w，按官方 `_room_workspace_rank` 语义选离工位最近）、备选楼栋/楼层降级/
  反园区/±30 分钟回退；
- S2：查询工位 / 不可预订清单 / 房间日程 / 本人预订列表（只读，不调写工具）；
- 其余意图（S3~S6/M/S1s）返回空 `{}`（0 分防线），后续阶段逐个接入；
- 每次 `call_tool` 前过 `EffectiveToolRegistry.validate_call`（防 forbidden），
  写操作另过 `can_execute_write` 门禁；`final_answer.office_id` 用房间
  `officeId` UUID（reference 多数派，接受少量楼栋名 case 的 RS 损失）。

当前评估：**意图识别（分解 + 路由）线上目标集 15/15 正确**（2026-08-08，真实
LLM + 本地 key）：meeting-only → 单 `meeting`；跨域（beta_zh_*/beta_mr_wf_*）→
`[meeting, leave]` 或 `[meeting, budget]` 两单元；纯流程 → 单 `leave`/`budget`；
multi_turn → 单 `meeting` + mode 标记。无 key 降级走规则兜底、全量 val 不崩。
桥接阶段的 S1/S2 执行器沿用既有逻辑（不追分）：预订 / 查询最小闭环，其余子意图
返回空 `{}`，请假/预算流程 SOP 属下一里程碑。

```bash
# 单元测试（理解层 + 执行层）
.venv/bin/python -m pytest tests/test_understanding.py tests/test_executor.py -v
```

## 提交说明

- 提交包形态见 `contest/simulator/docs/submission_spec.md`；入口固定为 `submission/my_agent.py`。
- `run_agent.py` 自动在 `tmp/contest_{split}/` 拼装官方 runner 目录形态，避免手工拷贝。
- 禁止在代码/配置/prompt 中硬编码 API key；`config.local.json` 不打进提交包。
