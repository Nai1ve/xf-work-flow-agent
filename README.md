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
  utils/              # 分层实现模块（感知/理解/规划/执行/输出），逐步填充
tests/                # pytest 测试
scripts/
  run_agent.py        # 本地评估入口：拼装 tmp/contest_{split}/ 后调官方 test_runner.py
  summarize_run_results.py
  dashboard.py        # tag 级回归看板（TSR/AS/ES/RS 通过率矩阵 + ES 审计 + §9.3 验收）
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

## 提交说明

- 提交包形态见 `contest/simulator/docs/submission_spec.md`；入口固定为 `submission/my_agent.py`。
- `run_agent.py` 自动在 `tmp/contest_{split}/` 拼装官方 runner 目录形态，避免手工拷贝。
- 禁止在代码/配置/prompt 中硬编码 API key；`config.local.json` 不打进提交包。
