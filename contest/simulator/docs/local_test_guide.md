# 本地测试环境部署指南

本文档说明如何在本地搭建测试环境，并在提交前验证 `my_agent.py`。提交物结构和接口规范以
[`submission_spec.md`](submission_spec.md) 为准。

## 一、前置要求

| 工具 | 版本要求 | 说明 |
|------|----------|------|
| Python | 3.11.x | 评测容器使用 Python 3.11 |
| Docker | 20.10+ | 推荐方式，与评测环境最接近 |
| Git | 任意 | 克隆赛题仓库 |

## 二、准备提交目录

在 `contest/` 目录下准备自己的提交目录：

```text
contest/
├── my_submission/
│   ├── my_agent.py
│   └── requirements.txt
├── simulator/
├── cases/
└── Dockerfile
```

`my_agent.py` 必须定义 `MyAgent` 类：

```python
class MyAgent:
    def __init__(self, env):
        self.env = env

    def run(self, case_id: str) -> dict:
        obs = self.env.reset(case_id)
        final_answer = {}
        return final_answer
```

## 三、方式一：Docker（推荐）

Docker 方式使用仓库内置的 `contest/Dockerfile`。当前镜像包含仿真器、cases、工具
schema、共享数据和 Python 运行环境；预装三方库会在后续评测镜像阶段单独固化。
仓库中的 `simulator/test_runner.py` 是本地调试 runner 和评分口径参考实现；正式线上评测
会使用 Docker/子进程隔离 runner，隐藏 cases、evaluator 和评分逻辑只保留在平台侧。

### 3.1 构建镜像

```bash
cd contest
docker build -t iftk-contest:local .
```

### 3.2 运行单个 case

```bash
docker run --rm \
  -v "$(pwd)/my_submission:/app/submission" \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  -e DASHSCOPE_API_KEY="$DASHSCOPE_API_KEY" \
  iftk-contest:local \
  python /app/simulator/test_runner.py \
    --agent /app/submission/my_agent.py \
    --case beta_mr_wf_0001 \
    --verbose
```

### 3.3 批量测试

```bash
docker run --rm \
  -v "$(pwd)/my_submission:/app/submission" \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  iftk-contest:local \
  python /app/simulator/test_runner.py \
    --agent /app/submission/my_agent.py \
    --limit 20 \
    --output /app/submission/results.json
```

当前公开仓库没有正式 `train/dev` split 文件；本地批量测试默认按 `contest/cases/*.json`
排序运行。需要快速冒烟时使用 `--limit`，需要指定 case 时重复传入 `--case`。

## 四、方式二：本地 Python 环境

如果不方便使用 Docker，可以直接在本地 Python 环境中运行。注意：本地环境与评测容器可能
存在差异，最终提交前仍建议至少跑一次 Docker 验证。

### 4.1 创建虚拟环境

```bash
python3.11 -m venv venv

# Linux/macOS
source venv/bin/activate

# Windows PowerShell
venv\Scripts\Activate.ps1
```

### 4.2 安装依赖

当前仿真器本身主要依赖 Python 标准库。若你的 Agent 有白名单内额外依赖，可以安装：

```bash
cd contest
pip install -r my_submission/requirements.txt --no-deps
```

如果你的 Agent 使用 OpenAI、Anthropic、DashScope 等 SDK，请确认本地环境已安装对应库；
正式评测镜像的预装库清单见 [`submission_spec.md`](submission_spec.md)。

### 4.3 设置环境变量

```bash
# Linux/macOS
export OPENAI_API_KEY=sk-xxx
export ANTHROPIC_API_KEY=sk-ant-xxx
export DASHSCOPE_API_KEY=xxx

# Windows PowerShell
$env:OPENAI_API_KEY = "sk-xxx"
$env:ANTHROPIC_API_KEY = "sk-ant-xxx"
$env:DASHSCOPE_API_KEY = "xxx"
```

### 4.4 运行测试

```bash
cd contest

# 单个 case
python simulator/test_runner.py \
  --agent my_submission/my_agent.py \
  --case beta_mr_wf_0001 \
  --verbose

# 多个指定 case
python simulator/test_runner.py \
  --agent my_submission/my_agent.py \
  --case beta_mr_wf_0001 \
  --case beta_mr_0001 \
  --output results.json

# 批量冒烟
python simulator/test_runner.py \
  --agent my_submission/my_agent.py \
  --limit 20 \
  --output results.json
```

## 五、测试输出说明

### 5.1 单 case 详细输出

开启 `--verbose` 后会输出 case、总分、分项分、步数、耗时和错误信息。示例：

```text
[Case] beta_mr_wf_0001
  total=78.75 TSR=45.00 AS=20.00 ES=3.75 RS=10.00
  task_passed=False steps=5 elapsed=12.30s
```

### 5.2 批量结果文件

`--output results.json` 会写出 JSON，形态如下：

```json
[
  {
    "case_id": "beta_mr_wf_0001",
    "total": 78.75,
    "TSR": 45.0,
    "AS": 20.0,
    "ES": 3.75,
    "RS": 10.0,
    "task_passed": false,
    "steps_used": 5,
    "elapsed_seconds": 12.3
  }
]
```

## 六、调试技巧

### 6.1 调试单个 case

优先用单 case 命令复现失败：

```bash
python simulator/test_runner.py \
  --agent my_submission/my_agent.py \
  --case beta_mr_wf_0001 \
  --verbose
```

### 6.2 测试鲁棒性变体

如果 case JSON 中包含 `robustness_variants`，可以用 `case_id::variant_id` 指定变体：

```bash
python simulator/test_runner.py \
  --agent my_submission/my_agent.py \
  --case beta_mr_wf_0001::paraphrase_1 \
  --verbose
```

本地 runner 对 base case 会自动展开 `robustness_variants` 并按变体通过率聚合 RS：
`RS = 10 * variants_passed / variants_total`。没有 variants 的 case，RS 按 base case 是否通过
给 10/0。指定 `case_id::variant_id` 主要用于单个变体调试。

### 6.3 在代码中直接调用仿真器

```python
import sys
sys.path.insert(0, "contest/simulator")

from env import IFTKEnv

env = IFTKEnv(cases_dir="contest/cases")
obs = env.reset("beta_mr_wf_0001")
print(obs["user_query"])
print(obs["step_budget"])

tools = env.list_tools()
print([tool["name"] for tool in tools])

score = env.done({})
print(score)
```

注意：`env.done()` 是仿真器维护和手动验分接口。正式提交的 `MyAgent.run()` 只返回
`final_answer`，平台 runner 统一调用评分器。

## 七、常见问题排查

### Q1: `ImportError: No module named 'openai'`

本地 Python 环境缺少你的 Agent 依赖。先确认该库在白名单内，再安装：

```bash
cd contest
pip install -r my_submission/requirements.txt --no-deps
```

### Q2: 本地运行正常，但评测超时

检查是否存在以下问题：

- LLM API 调用没有设置 `timeout` 参数
- 存在无限重试循环
- 步数预算耗尽后未捕获 `StepLimitExceeded`

建议在循环中捕获步数异常并提交当前结果：

```python
from simulator.env import StepLimitExceeded

def run(self, case_id):
    final_answer = {}
    try:
        obs = self.env.reset(case_id)
        # ... your logic ...
        return final_answer
    except StepLimitExceeded:
        return final_answer
```

### Q3: `error: Unauthorized tool: xxx`

工具名称拼写错误，或该工具未在仿真器中注册。先调用 `env.list_tools()` 确认可用工具列表。

### Q4: 多轮对话 case 中 `env.reply()` 返回意外内容

多轮 case 的 `mode` 字段为 `"multi_turn"`，需要通过 `env.reply()` 与模拟用户交互补全缺失信息。
检查 `obs["mode"]` 并按需处理。

### Q5: Docker 构建失败

```bash
docker --version
cd contest
docker build --no-cache -t iftk-contest:local .
```

## 八、评测环境规格参考

| 项目 | 规格 |
|------|------|
| OS | Ubuntu 22.04 |
| Python | 3.11.x |
| 网络 | 允许访问 LLM API（OpenAI、Anthropic、阿里云等） |
| 单 case 超时 | 60 秒 |
| 总评测超时 | 以后续正式平台配置为准 |

## 九、提交前最终验证

1. 语法检查：`python -m py_compile my_submission/my_agent.py`
2. 单 case 冒烟：选 3-5 个不同类型的 case 跑通
3. 批量测试：使用 `--limit 20` 或更多 case 检查稳定性
4. Docker 验证：使用 Docker 方式重跑关键 case
5. 依赖检查：确认 `requirements.txt` 中所有库在白名单内，版本号使用 `==`
6. 安全检查：确认代码中无硬编码 API key
