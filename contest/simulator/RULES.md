 # 企业流程 Agent 挑战赛 - 赛题说明

## 一、赛题背景

在企业数字化场景中，员工每天需要处理大量重复性流程操作：预订会议室、提交流程草稿、查询人员或工位信息等。这些任务通常需要理解自然语言、选择合适工具、遵守业务约束，并在有限步数内完成。

本赛题要求参赛者开发一个**企业流程执行 Agent**，能够理解员工的中文自然语言请求，自主规划并执行工具调用，最终提交结构化任务结果。

## 二、任务定义

### 输入

- 用户请求：中文自然语言，例如“帮我约下周二下午2点到3点在A1园区10人会议室，主题季度复盘”。
- 当前时间：用于计算“明天”“下周二”等相对时间。
- 可用工具列表：每个工具包含名称、描述和参数 schema。
- case 状态：由离线仿真器注入，不需要访问真实企业系统。

### 输出

- 工具调用序列：Agent 在环境中执行的 `env.call_tool(...)` 和多轮场景中的 `env.reply(...)`。
- 最终答案：`MyAgent.run()` 返回的结构化任务结果；平台 runner 负责统一评分。

### 约束

- 每个 case 有最大步数限制，通常为 8-12 步，以 case 中 `scoring.step_budget` 为准。
- 单个 case 超时限制为 60 秒；正式平台会在 Docker/子进程层强制执行。
- 只能调用仿真器公开的工具。
- 必须遵守工具参数 schema 和业务规则。
- 高风险写操作如果 case 要求确认，必须先通过多轮交互获得确认。

## 三、评测环境

参赛者在本地 Docker 或 Python 环境中运行，无需访问真实企业系统。仿真器提供 Gym 风格接口：

```python
from env import IFTKEnv

env = IFTKEnv("cases/")

obs = env.reset("case_id")
tools = env.list_tools()
result = env.call_tool("meetingroom.room.list", {"day": "2026-04-21"})
final_answer = {"booking_result": {...}}
return final_answer
```

多轮 case 可使用：

```python
reply = env.reply("请问是在哪个园区？")
```

完整提交接口和本地测试方式见：

- [`docs/submission_spec.md`](docs/submission_spec.md)
- [`docs/local_test_guide.md`](docs/local_test_guide.md)

## 四、当前工具类型

当前公开仿真器优先覆盖企业流程 Agent 常见任务面：

| 模块 | 工具示例 | 功能 |
|------|----------|------|
| meetingroom | `meetingroom.room.list`, `meetingroom.booking.create` | 查询会议室、创建/取消/延长预订、维护参会人 |
| workflow | `workflow.catalog`, `workflow.schema`, `workflow.save` | 查询流程目录、获取表单结构、保存或提交流程 |
| user | `user.get_info`, `user.get_workspace` | 查询人员和当前用户办公地点 |
| oa | `oa.todo.list`, `oa.done.list` | 查询待办/已办，用于验证流程状态 |
| file | `file.list` | 查询 case 中提供的文件信息 |

具体工具 schema 以 [`tool_specs.json`](tool_specs.json) 和 `env.list_tools()` 返回为准。

## 五、评分规则

总分 100 分，由四个维度组成：

### 1. 任务成功率 TSR（60 分）

检查 Agent 是否完成任务核心目标。每个 case 定义若干 `success_check.must_satisfy` 条件，例如：

- 存在成功会议预订，日期、会议室、时间、主题符合要求。
- 会议室容量、地点、设备等满足用户约束。
- 存在 workflow 草稿或提交记录，流程 ID 和关键字段正确。

计分方式：

```text
TSR = 60 * (通过条件数 / 总条件数)
```

### 2. 动作合法性 AS（20 分）

检查工具调用是否符合规范：

- 参数是否符合 schema。
- 是否调用未授权或不存在的工具。
- 是否违反业务规则，如时间冲突、缺少确认即执行高风险写操作。

扣分规则：

- 每次工具调用返回 `error`：扣 5 分。
- 触发严重 forbidden 条件：AS 直接为 0。

### 3. 效率分 ES（10 分）

在任务成功的前提下，步数越少得分越高：

```text
ES = 10 * (1 - (实际步数 - 最优步数) / 步数预算)
```

### 4. 鲁棒性 RS（10 分）

测试 Agent 在同义改写、口语化表达、信息缺失、多轮澄清等变体场景下的稳定性。正式 runner 会自动展开含 `robustness_variants` 的 case，并按变体通过率聚合 RS：

```text
RS = 10 * variants_passed / variants_total
```

没有 `robustness_variants` 的 case，RS 按 base case 是否通过给分：通过为 10 分，否则为 0 分。

## 六、提交要求

参赛者提交一个压缩包，主入口固定为：

```text
submission/my_agent.py
```

`my_agent.py` 必须定义 `MyAgent` 类：

```python
class MyAgent:
    def __init__(self, env):
        self.env = env

    def run(self, case_id: str) -> dict:
        obs = self.env.reset(case_id)
        tools = self.env.list_tools()

        # 1. 解析 obs["user_query"]
        # 2. 规划工具调用序列
        # 3. 执行 self.env.call_tool(name, args)
        # 4. 返回最终结构化答案，由 runner 统一评分

        return final_answer
```

完整文件结构、依赖白名单、禁止事项和 API key 规范见 [`docs/submission_spec.md`](docs/submission_spec.md)。

## 七、本地调试

推荐使用本地调试 runner。它与官方 evaluator 的评分口径保持一致，但正式线上评测会使用
Docker/子进程隔离 runner，隐藏 cases、evaluator 和评分逻辑只保留在平台侧：

```bash
cd contest
python simulator/test_runner.py \
  --agent my_submission/my_agent.py \
  --case beta_mr_wf_0001 \
  --verbose
```

批量冒烟：

```bash
python simulator/test_runner.py \
  --agent my_submission/my_agent.py \
  --limit 20 \
  --parallel 4 \
  --timeout 60 \
  --output results.json
```

Docker 验证：

```bash
cd contest
docker build -t iftk-contest:local .
docker run --rm \
  -v "$(pwd)/my_submission:/app/submission" \
  iftk-contest:local \
  python /app/simulator/test_runner.py \
    --agent /app/submission/my_agent.py \
    --case beta_mr_wf_0001 \
    --verbose
```

## 八、依赖管理

评测镜像预装 [`requirements.base.txt`](requirements.base.txt) 中的常用依赖。选手如需额外依赖，只能在 `requirements.txt` 中声明白名单内库，且必须使用 `==` 精确版本。

runner 会自动校验提交目录下的 `requirements.txt`；不符合规范的提交会被拒绝评测。

## 九、基线方法

仓库提供规则基线 [`simulator/baseline_agent.py`](simulator/baseline_agent.py)，用于验证环境和提供基础工具调用范式。参赛者可以使用规则 Agent、LLM Agent 或规则 + LLM 混合方案。

建议方向：

1. 使用结构化解析处理时间、地点、人数、主题等关键槽位。
2. 优先调用 `env.list_tools()` 获取可用工具和 schema。
3. 对写操作保留确认逻辑，避免未确认执行高风险操作。
4. 为 LLM API 调用设置 timeout，避免单 case 超时。

## 十、常见问题

### 可以使用外部 LLM API 吗？

可以。评测环境允许访问常见 LLM API，但不能访问真实企业系统。API key 通过环境变量注入，禁止硬编码到提交包中。

### 超过步数预算会怎样？

环境会抛出 `StepLimitExceeded`。建议捕获异常并提交当前已有结果，否则未捕获异常会导致该 case 得 0 分。

### 本地 case 是否有正式 train/dev split？

当前公开仓库没有正式 split 文件。本地批量测试默认按 `contest/cases/*.json` 排序运行，可使用 `--limit` 做快速冒烟。

## 十一、联系方式

- 技术支持：contest-support@iflytek.com
- 问题反馈：GitHub Issues（赛题仓库）
