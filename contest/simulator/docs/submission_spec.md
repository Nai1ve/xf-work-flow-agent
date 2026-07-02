# 选手提交规范

本文档说明参赛者提交代码时需要遵守的文件结构、接口规范、依赖管理和安全要求。

---

## 一、文件结构

提交一个 `.zip` 或 `.tar.gz` 压缩包，解压后目录结构如下：

```
submission/
├── my_agent.py          # 必需：主入口文件，文件名固定
├── requirements.txt     # 可选：额外依赖声明（仅限白名单内的库）
├── config.json          # 可选：配置文件（不得包含 API key）
├── utils/               # 可选：辅助模块
│   ├── __init__.py
│   └── ...
└── prompts/             # 可选：prompt 模板
    └── system.txt
```

**限制**：
- 压缩包大小 ≤ 50MB
- 主入口文件名必须是 `my_agent.py`，不可更改
- 不允许包含预训练模型文件（`.pt`、`.ckpt`、`.bin`、`.safetensors` 等）
- 不允许包含二进制可执行文件
- 文件名必须使用 ASCII 字符，避免编码问题

---

## 二、代码接口规范

`my_agent.py` 必须定义 `MyAgent` 类，并实现以下两个方法：

```python
class MyAgent:
    def __init__(self, env):
        """
        初始化 Agent。

        Args:
            env: IFTKEnv 实例，提供环境交互接口

        注意：
        - 不要在 __init__ 中调用 env.reset()
        - 可以在此处初始化 LLM 客户端、加载 prompt 模板等
        - 初始化时间不计入单个 case 的超时时间
        """
        self.env = env

    def run(self, case_id: str) -> dict:
        """
        执行单个 case。

        Args:
            case_id: case 标识符，如 "beta_mr_wf_0001"

        Returns:
            final_answer 结构化答案。平台 runner 会在选手代码结束后统一评分。
            常见字段包括：
            {
                "booking_result": {...},
                "workflow_draft_result": {...},
                "room_candidates": [...],
                "status": "blocked",
                ...
            }
        """
        obs = self.env.reset(case_id)
        tools = self.env.list_tools()

        # 实现你的 Agent 逻辑：
        # 1. 解析 obs["user_query"]
        # 2. 规划工具调用序列
        # 3. 执行 self.env.call_tool(name, args)
        # 4. 返回 final_answer；不要在选手代码中计算或返回分数

        final_answer = {}
        return final_answer
```

### 环境 API 速查

| 方法 | 说明 | 返回值 |
|------|------|--------|
| `env.reset(case_id)` | 加载 case，返回初始观察 | `{user_query, now, step_budget, mode, messages}` |
| `env.list_tools()` | 返回可用工具列表（含 schema） | `list[dict]` |
| `env.call_tool(name, args)` | 调用工具，消耗一步预算 | 工具执行结果 `dict` |
| `env.reply(message)` | 多轮对话：发送助手消息，获取用户回复 | `{user_message, messages, ...}` |

正式 runner 会在 `run()` 返回后统一调用官方 evaluator。选手环境不暴露 `env.done()`，
也不会采信 `run()` 返回值中的 `TSR`、`AS`、`ES`、`RS`、`total` 等分数字段。

正式线上评测使用 Docker/子进程隔离 runner。选手容器只运行 `MyAgent` 并通过受控接口调用工具；
隐藏 cases、evaluator 和评分逻辑只保留在平台侧。仓库中的 `simulator/test_runner.py`
用于本地调试和对齐评分口径。

### 超时与步数限制

- 单个 case 最多运行 **60 秒**，超时强制终止，该 case 得分为 0
- 步数超出 `step_budget` 时，`env.call_tool()` 和 `env.reply()` 会抛出 `StepLimitExceeded` 异常
- 建议在循环中检查步数，并在接近上限时提前返回当前 `final_answer`

```python
from simulator.env import StepLimitExceeded

try:
    result = self.env.call_tool(name, args)
except StepLimitExceeded:
    return current_answer
```

### 异常处理建议

```python
def run(self, case_id: str) -> dict:
    final_answer = {}
    try:
        obs = self.env.reset(case_id)
        # ... 你的逻辑 ...
        return final_answer
    except StepLimitExceeded:
        return final_answer
    except Exception as e:
        # 未捕获异常会导致该 case 得 0 分
        return {}
```

---

## 三、依赖管理

### 3.1 预装库（无需声明）

以下库已预装在评测容器中，**不需要**在 `requirements.txt` 中声明：

```
# Python 标准库
json, re, datetime, pathlib, typing, os, sys, time, collections, itertools, ...

# LLM SDK
openai==1.30.0
anthropic==0.25.0
dashscope==1.14.0

# Agent 框架
langchain==0.1.20
langchain-openai==0.1.7
langchain-anthropic==0.1.8
llama-index==0.10.30

# 数据处理
pydantic==2.7.0
jsonschema==4.21.1

# HTTP 客户端
requests==2.31.0
httpx==0.27.0

# 工具库
python-dateutil==2.9.0
pytz==2024.1
```

### 3.2 可选依赖白名单

如需使用以下库，在 `requirements.txt` 中声明（必须指定精确版本号）：

```
# 更多 LLM SDK
google-generativeai==0.5.0
cohere==5.2.0
together==1.1.0

# Agent 框架
autogen==0.2.25
crewai==0.1.30

# NLP 工具
jieba==0.42.1
pypinyin==0.50.0

# 数据处理
pandas==2.2.0
numpy==1.26.4

# 工具库
tenacity==8.2.3
tiktoken==0.6.0
```

**requirements.txt 格式要求**：
- 必须使用 `==` 指定精确版本，不接受 `>=`、`~=`、`^` 等范围写法
- 只能包含白名单内的库，否则提交得分为 0
- 评测时执行 `pip install -r requirements.txt --no-deps`，安装超时（30 秒）或失败则得分为 0

示例：
```
jieba==0.42.1
tenacity==8.2.3
```

### 3.3 禁止使用的库

```
# 浏览器自动化（评测环境无图形界面）
selenium, playwright, pyppeteer

# 大型模型库（容器资源限制）
transformers, torch, tensorflow, jax

# 危险操作
os.system（禁止）
eval, exec（会被监控）
subprocess（部分功能受限）
```

---

## 四、API Key 配置

### 推荐方式：环境变量

评测时通过环境变量注入 API key，在代码中读取：

```python
import os

openai_key    = os.getenv("OPENAI_API_KEY")
anthropic_key = os.getenv("ANTHROPIC_API_KEY")
dashscope_key = os.getenv("DASHSCOPE_API_KEY")  # 阿里云通义千问
```

### 可选方式：配置文件

`config.json` 可存放模型参数（不得包含 key 值）：

```json
{
  "model": "gpt-4o",
  "temperature": 0.0,
  "max_tokens": 2000,
  "timeout": 30
}
```

### 禁止事项

- 禁止在代码中硬编码 API key
- 禁止提交包含真实 key 的配置文件
- 禁止在代码中访问外部 URL 获取 key

违反以上规定的提交将被取消资格。

---

## 五、提交前检查清单

- [ ] 主入口文件名为 `my_agent.py`（不是 `agent.py`）
- [ ] `my_agent.py` 中定义了 `MyAgent` 类
- [ ] `MyAgent` 实现了 `__init__(self, env)` 和 `run(self, case_id)` 方法
- [ ] 本地运行 `python my_agent.py` 无语法错误
- [ ] 所有额外依赖已在 `requirements.txt` 中声明，且在白名单内
- [ ] `requirements.txt` 中所有版本号使用 `==` 格式
- [ ] 代码中无硬编码的 API key
- [ ] 压缩包大小 < 50MB
- [ ] 已通过本地测试环境验证（见 `local_test_guide.md`）
- [ ] 在至少 10 个不同 case 上测试过

---

## 六、评分规则

总分 **100 分**，由四个维度组成：

| 维度 | 分值 | 说明 |
|------|------|------|
| TSR 任务成功率 | 60 分 | 核心目标完成情况，按通过条件数比例计分 |
| AS 动作合法性 | 20 分 | 工具调用规范性；每次 error 扣 5 分，触发 forbidden 直接 0 分 |
| ES 效率分 | 10 分 | 任务成功前提下，步数越少得分越高 |
| RS 鲁棒性 | 10 分 | 有 variants 时按变体通过率聚合；无 variants 时按 base case 是否通过给 10/0 |

效率分公式：
```
ES = 10 × (1 - (实际步数 - 最优步数) / 步数预算)
```

---

## 七、提交方式

1. 登录比赛平台
2. 进入"企业流程 Agent 挑战赛"页面
3. 点击"提交代码"，上传压缩包（`.zip` 或 `.tar.gz`）
4. 等待评测结果（通常 15–30 分钟）

**提交限制**：
- 每天最多提交 **5 次**
- 每次提交间隔至少 **1 小时**
- 最终排名以**最后一次提交**为准（不是最高分）

评测日志会在评测完成后发送到注册邮箱。
