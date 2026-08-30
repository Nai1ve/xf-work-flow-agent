# NL2Workflow V2

这是一个独立的纯 Python DAG 实现。模型只负责任务拆分、自然语言语义抽取和候选排序；工具顺序、字段依赖、写前校验、冲突恢复和最终投影由 Skill/SOP 与执行器负责。默认 Profile 为 `hybrid_compat`，`generic_v2` 用于不依赖旧兼容模板的 A/B，`legacy_current` 用于回滚。

## 目录

```text
submission2/
  my_agent.py                 # 官方入口（MyAgent.__init__/run）
  config.json                 # 可提交配置，不含 API key
  static_context/             # tools/rooms/workflows/manifest 静态索引
  utils/
    context.py                # CaseContext + EvidenceLedger
    dag_runtime.py            # TaskDag、节点状态和失败转译
    profiles.py               # Profile 与冲突隔离策略
    llm_gateway.py            # OpenAI-compatible 结构化调用与审计
    meeting_skill.py          # 会议语义编排与多轮澄清
    leave_skill.py            # 请假 Schema/SOP
    budget_skill.py           # 费用/物资 Schema/SOP
    executor.py               # 会议工具执行、冲突恢复、Projection
    tool_contract.py          # 运行时工具对账与调用前校验
    workflow_registry.py      # Workflow Schema/字段依赖
scripts/
  run_agent.py                # 官方 simulator runner 包装器
  build_static_context.py    # 离线编译静态索引
  audit_conflicts.py          # 只读 Gold 冲突审计（不进入提交包）
  package_submission.py      # 构建并检查官方 zip
submission2/docs/             # 架构、数据契约、SOP、评分和测试说明
tests/test_agent_v2_runtime.py  # 无模型 DAG/证据隔离测试
```

## 环境与密钥

需要 Python 3.11。模型配置按 `config.json` →（本地未提交的）`config.local.json` → 环境变量合并；推荐通过 `OPENAI_API_KEY`、可选的 `OPENAI_BASE_URL`、`OPENAI_MODEL` 注入。密钥不会写入日志、静态索引、提交包或 `final_answer`。

首次使用请确认 `contest/train`、`contest/val` 数据已放置（目录被 gitignore），再编译索引：

```bash
PYTHONPATH=submission2 .venv/bin/python scripts/build_static_context.py
```

## 快速验证

纯 V2 定向单元/契约测试：

```bash
PYTHONPATH=submission2 .venv/bin/python -m pytest \
  tests/test_build_static_context.py tests/test_static_context.py \
  tests/test_tool_contract.py tests/test_understanding.py \
  tests/test_agent_v2_runtime.py -q
python -m compileall -q submission2 scripts
git diff --check
```

单 case（日志同时写控制台和普通 UTF-8 `.log`）：

```bash
AGENT_EXECUTION_PROFILE=hybrid_compat \
PYTHONPATH=submission2 .venv/bin/python scripts/run_agent.py \
  --split val --agent submission2/my_agent.py \
  --case beta_mr_wf_0006 --skip-variants --timeout 60 --verbose
```

全量 Train/Val：

```bash
AGENT_EXECUTION_PROFILE=hybrid_compat \
PYTHONPATH=submission2 .venv/bin/python scripts/run_agent.py \
  --split train --agent submission2/my_agent.py --parallel 4 --timeout 60 \
  --skip-variants --output reports/runs/v2_train.json \
  --log-output reports/runs/v2_train.stdout

AGENT_EXECUTION_PROFILE=hybrid_compat \
PYTHONPATH=submission2 .venv/bin/python scripts/run_agent.py \
  --split val --agent submission2/my_agent.py --parallel 4 --timeout 60 \
  --skip-variants --output reports/runs/v2_val.json \
  --log-output reports/runs/v2_val.stdout
```

日志行会记录 Prompt、payload、原始模型输出、结构化结果、工具调用/结果、追问/回复、DAG 节点状态、阻断/终止、Projection 和计时；默认文件为 `submission2/agent_runtime.log`，可用 `AGENT_LOG_FILE` 覆盖。

## 打包

```bash
python scripts/package_submission.py \
  --output submission2/dist/submit_v2.zip
```

脚本把 `submission2/` 映射为 zip 内的 `submission/`，剔除文档、训练数据、报告、缓存、日志和本地配置，递归清空认证字段，并检查 `submission/my_agent.py`、大小和疑似密钥。当前验收包约 185KB。

## 设计文档

- [architecture.md](docs/architecture.md)：模块边界和数据流
- [data_contracts.md](docs/data_contracts.md)：三类静态数据、运行时权威顺序和 provenance
- [sop_catalog.md](docs/sop_catalog.md)、[skill_catalog.md](docs/skill_catalog.md)：固定流程与 Skill 输入/输出
- [dag_spec.md](docs/dag_spec.md)：状态机、依赖、预算和证据隔离
- [scoring_strategy.md](docs/scoring_strategy.md)：TSR/AS/ES/RS 与冲突分层
- [testing_guide.md](docs/testing_guide.md)：单元、E2E、全量和打包命令
- [evaluation_report.md](docs/evaluation_report.md)：最近一次 Train/Val 回归结果与失败归因

Gold 冲突分析只能离线运行：

```bash
PYTHONPATH=submission2 .venv/bin/python scripts/audit_conflicts.py \
  --output reports/conflict_audit_v2.log
```

该脚本不被 `MyAgent` 导入，分析结果不会进入提交包或运行时决策。
