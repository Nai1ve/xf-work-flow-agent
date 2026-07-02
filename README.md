# NL2Workflow 比赛工作区

这个目录用于分析科大讯飞 NL2Workflow / 企业流程执行 Agent 比赛数据，运行官方离线模拟器，并开发后续提交方案。

## 目录结构

```text
contest/
  simulator/          # 官方离线模拟器
  source_zips/        # 本地原始 zip 文件，不提交
  train/              # 本地公开训练集 cases、data、tool_specs.json，不提交
  val/                # 本地公开验证集 cases、data、tool_specs.json，不提交
reports/
  analysis/           # 本地生成的数据集分析报告，不提交
  baseline/           # 本地生成的跑分结果和日志，不提交
scripts/
  analyze_dataset.py  # 静态数据分析 + runner 结果汇总
  run_agent.py        # 官方 test_runner.py 包装脚本
submission/
  my_agent.py         # V1 LLM Agent 主入口
  config.json         # 可提交默认配置，不含真实 Key
  config.local.example.json # 本地私密配置模板
tmp/                  # 自动生成的 runner 临时目录
```

官方 runner 要求运行目录形态如下：

```text
contest_root/
  cases/
  data/
  tool_specs.json
  simulator/
```

`scripts/run_agent.py` 会根据 `train` 或 `val` 自动生成 `tmp/contest_train`、`tmp/contest_val`，避免手工拷贝目录造成路径错误。

源码仓库不包含原始数据 zip、解压后的 `train/val`、本地跑分报告和真实 API Key。首次本地运行前，把比赛数据解压到 `contest/train`、`contest/val`，并按需复制 `submission/config.local.example.json` 为 `submission/config.local.json` 填写私密模型配置。

## 环境要求

需要使用 Python 3.11。模拟器代码使用了 `dict | None` 等新语法，系统自带 Python 3.9 会运行失败。

当前机器可用解释器：

```bash
/opt/homebrew/bin/python3.11
```

## 常用命令

生成数据集分析报告：

```bash
/opt/homebrew/bin/python3.11 scripts/analyze_dataset.py
```

运行官方 baseline 验证集：

```bash
/opt/homebrew/bin/python3.11 scripts/run_agent.py \
  --agent contest/simulator/simulator/baseline_agent.py \
  --split val \
  --output reports/baseline/baseline_val.json
```

结合 baseline 结果重新生成分析报告：

```bash
/opt/homebrew/bin/python3.11 scripts/analyze_dataset.py \
  --runner-results reports/baseline/baseline_val.json \
  --runner-split val
```

运行单个 case：

```bash
/opt/homebrew/bin/python3.11 scripts/run_agent.py \
  --agent contest/simulator/simulator/baseline_agent.py \
  --split val \
  --case beta_mr_0011 \
  --verbose
```

运行 V1 LLM Agent：

```bash
/opt/homebrew/bin/python3.11 scripts/run_agent.py \
  --agent submission/my_agent.py \
  --split val \
  --case beta_mr_0011 \
  --verbose
```

## V1 LLM Agent

`submission/my_agent.py` 当前实现为 OpenAI-compatible LLM 编排器：

- 运行时读取 `submission/config.json`，本地优先叠加 `submission/config.local.json`，环境变量可覆盖 `OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_API_KEY`、`OPENAI_TIMEOUT`、`OPENAI_TEMPERATURE`、`MAX_LLM_ROUNDS`。
- `config.json` 是可提交默认配置，不包含真实 Key；`config.local.json` 只用于本地测试，已放入 `.gitignore`，不要打进提交包。
- 可复制 `submission/config.local.example.json` 为 `submission/config.local.json`，再填写真实 `base_url`、`model` 和 `api_key`。
- LLM 只允许输出 JSON action，本地校验工具白名单、参数对象、步数预算和异常。
- 为降低慢模型多轮超时风险，本地实现了通用 scaffold：会议室预订/换房、请假草稿、费用类物资申请。scaffold 只使用用户请求和工具返回候选，不读取 `success_check`、`gold_trajectory`、`reference_final_answer`，也不按 case_id 分支。

## 当前结论

- 本地 zip 实际包含训练集 200 条、验证集 50 条。
- 所有公开 case 都包含 `reference_final_answer` 和 `gold_trajectory`；这些字段只用于分析任务模式，不应做 case id 记忆。
- 真实选手环境 API 是 `reset`、`list_tools`、`call_tool`、`reply`；本地模拟器没有 `ask_user`。
- workflow 是主要得分机会，也是官方 baseline 最弱的部分。
- 隐藏测试应按“同一工具系统、同一业务域下的未见组合/别名/时间表达/改写”来设计，不要按公开 case 背答案。

## 已生成报告

- 数据分析报告：[reports/analysis/dataset_analysis.md](/Users/naive/code/work-flow-agent/reports/analysis/dataset_analysis.md)
- 数据分析 JSON：[reports/analysis/dataset_analysis.json](/Users/naive/code/work-flow-agent/reports/analysis/dataset_analysis.json)
- baseline 验证集结果：[reports/baseline/baseline_val.json](/Users/naive/code/work-flow-agent/reports/baseline/baseline_val.json)
- baseline 验证集日志：[reports/baseline/baseline_val.stdout](/Users/naive/code/work-flow-agent/reports/baseline/baseline_val.stdout)
