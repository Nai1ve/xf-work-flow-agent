# 测试指南

## 单元与契约

```bash
PYTHONPATH=submission2 .venv/bin/python -m pytest \
  tests/test_build_static_context.py tests/test_static_context.py \
  tests/test_tool_contract.py tests/test_understanding.py \
  tests/test_agent_v2_runtime.py -q
python -m compileall -q submission2
git diff --check
```

## 单 case

```bash
AGENT_EXECUTION_PROFILE=hybrid_compat PYTHONPATH=submission2 \
  .venv/bin/python scripts/run_agent.py --split val \
  --agent submission2/my_agent.py --case beta_mr_wf_0006 \
  --skip-variants --timeout 60 --verbose
```

日志同时写控制台和 `AGENT_LOG_FILE` 指定的普通 UTF-8 `.log`；一行包含 Prompt、原始模型输出、结构化结果、工具调用/结果、追问、Task 状态、Projection 和耗时。

## 全量

```bash
AGENT_EXECUTION_PROFILE=hybrid_compat PYTHONPATH=submission2 \
  .venv/bin/python scripts/run_agent.py --split train \
  --agent submission2/my_agent.py --parallel 4 --timeout 60 --skip-variants \
  --output reports/runs/v2_train.json --log-output reports/runs/v2_train.stdout
AGENT_EXECUTION_PROFILE=hybrid_compat PYTHONPATH=submission2 \
  .venv/bin/python scripts/run_agent.py --split val \
  --agent submission2/my_agent.py --parallel 4 --timeout 60 --skip-variants \
  --output reports/runs/v2_val.json --log-output reports/runs/v2_val.stdout
```

## 构建带 key 的本地提交包

默认打包会清空认证字段；用户已确认复用当前 key 时，用独立输出文件执行：

```bash
python scripts/package_submission.py --include-key \
  --output submission2/dist/submit_v2_with_key.zip
```

`--include-key` 只读取本地 `submission2/config.json`（或环境变量
`OPENAI_API_KEY`），不修改源文件；该 zip 已被 `.gitignore` 忽略，切勿提交到 Git 或分享。

公开 Gold 冲突只能通过 `scripts/audit_conflicts.py` 离线分析；该脚本不被提交包导入。
