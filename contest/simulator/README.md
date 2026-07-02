# iftk Contest Simulator

离线 Gym 环境，用于企业流程 Agent 竞赛。参赛者通过 `MyAgent` 与本地仿真器交互，无需访问真实企业系统。

## 文档入口

- 选手提交硬性规范：[`docs/submission_spec.md`](docs/submission_spec.md)
- 本地部署和测试指南：[`docs/local_test_guide.md`](docs/local_test_guide.md)
- 提交入口说明：[`SUBMISSION_GUIDE.md`](SUBMISSION_GUIDE.md)
- 赛题规则说明：[`RULES.md`](RULES.md)

## 快速开始

### 运行基线 Agent

```bash
cd contest/simulator
python baseline_agent.py
```

也可以通过正式 runner 运行基线：

```bash
cd contest
python simulator/test_runner.py --agent simulator/baseline_agent.py --case beta_mr_wf_0001 --verbose
```

### 运行选手 Agent

`simulator/test_runner.py` 是选手本地调试 runner 和官方评分口径参考实现。正式线上评测
会在平台侧使用 Docker/子进程隔离 runner，隐藏 cases、evaluator 和评分逻辑只保留在平台侧。

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

### Docker 运行

```bash
cd contest
docker build -t iftk-contest:local .

docker run --rm \
  -v "$(pwd)/my_submission:/app/submission" \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  iftk-contest:local \
  python /app/simulator/test_runner.py \
    --agent /app/submission/my_agent.py \
    --case beta_mr_wf_0001 \
    --verbose
```

## 目录结构

```text
contest/
├── Dockerfile                    # 评测/本地测试镜像
├── requirements.base.txt         # 评测镜像预装依赖
├── tool_specs.json               # 全局工具定义
├── SUBMISSION_GUIDE.md           # 提交入口说明
├── RULES.md                      # 赛题规则说明
├── data/
│   ├── meetingroom_data.json     # 会议室共享数据
│   └── workflow_data.json        # 流程共享数据
├── docs/
│   ├── submission_spec.md        # 选手提交硬性规范
│   └── local_test_guide.md       # 本地测试环境部署指南
├── simulator/
│   ├── env.py                    # Gym 环境核心
│   ├── evaluator.py              # 评分器
│   ├── baseline_agent.py         # 基线 Agent
│   ├── test_runner.py            # 选手 Agent 本地调试 runner
│   ├── requirements_policy.py    # requirements.txt 白名单校验
│   └── tools/                    # 工具仿真实现
└── cases/
    └── *.json                    # 公开 case 样本
```

## Agent API

```python
from env import IFTKEnv

env = IFTKEnv("cases/")

obs = env.reset("beta_mr_wf_0001")
tools = env.list_tools()

result = env.call_tool("meetingroom.room.list", {
    "day": "2026-04-21",
    "office_address": "0552_A1",
    "capacity_gte": 10,
})

final_answer = {"booking_result": {...}}
return final_answer
```

调用链路：

1. `env.reset(case_id)` 加载 case，返回初始 observation。
2. `env.list_tools()` 获取当前仿真器公开工具 schema。
3. `env.call_tool(name, args)` 执行工具调用。
4. 多轮 case 可用 `env.reply(message)` 与模拟用户交互。
5. `MyAgent.run()` 返回最终结构化答案 `final_answer`。
6. 平台 runner 统一调用官方 evaluator 计算 TSR / AS / ES / RS / total。

## 评分规则

总分 100 分：

| 维度 | 分值 | 说明 |
|------|------|------|
| TSR | 60 | 任务成功率，检查 `success_check.must_satisfy` 条件 |
| AS | 20 | 动作合法性，检查工具调用错误、未授权工具、业务禁忌 |
| ES | 10 | 效率分，任务成功前提下步数越少越高 |
| RS | 10 | 鲁棒性；有 `robustness_variants` 时按变体通过率聚合，无 variants 时按 base case 是否通过给 10/0 |

## 当前工具面

### Meetingroom

- `meetingroom.room.list`
- `meetingroom.room.bookings`
- `meetingroom.booking.list`
- `meetingroom.booking.create`
- `meetingroom.booking.cancel`
- `meetingroom.booking.extend`
- `meetingroom.booking.participant.list`
- `meetingroom.booking.participant.add`
- `meetingroom.booking.participant.remove`

### Workflow

- `workflow.catalog`
- `workflow.schema`
- `workflow.search_person`
- `workflow.browser_search`
- `workflow.project_search`
- `workflow.save`
- `workflow.delete`

### User / OA / File

- `user.get_info`
- `user.get_workspace`
- `oa.todo.list`
- `oa.done.list`
- `file.list`

## 依赖策略

评测镜像会安装 [`requirements.base.txt`](requirements.base.txt) 中的预装依赖。选手提交目录中的
`requirements.txt` 会由 `simulator/requirements_policy.py` 校验，只允许
[`docs/submission_spec.md`](docs/submission_spec.md) 中列出的白名单库，并且必须使用 `==` 固定版本。

## 已覆盖的任务形态

- 单域单轮：会议室查询/预订、workflow 草稿保存
- 跨域单轮：会议室 + workflow
- 多轮澄清：缺少时间、园区、人数、标题时，通过 `reply()` 追问
- 多轮确认：高风险写操作前先确认
- 候选输出：只返回候选会议室，不直接执行预订

## 扩展

新增工具：

1. 在 `simulator/tools/` 下创建工具模块。
2. 在 `env.py` 的 `TOOL_REGISTRY` 中注册。
3. 在 `tool_specs.json` 中补充公开 schema。
4. 在 case 的 `world_state` 和标签字段中声明任务域。

新增 case：

1. 在 `cases/` 下创建 JSON 文件。
2. 复用 `data/` 下的共享 meetingroom/workflow 数据。
3. 通过 `workflow_refs` / `workflow_overrides` 表达流程引用和局部差异。
4. 多轮 case 同步维护 `dialogue_state`、`user_simulator` 和 gold trajectory。

## 许可

内部使用，不对外发布。
