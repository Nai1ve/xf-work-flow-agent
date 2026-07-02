# 提交指南

本文档是参赛提交入口说明。硬性提交规则以
[`docs/submission_spec.md`](docs/submission_spec.md) 为准，包括主入口文件名、代码接口、
依赖白名单、安全限制和评分口径。

## 一、提交物要求

参赛者提交一个 `.zip` 或 `.tar.gz` 压缩包。压缩包解开后应包含：

```text
submission/
├── my_agent.py          # 必需：主入口文件，文件名固定
├── requirements.txt     # 可选：额外依赖声明，仅限白名单内的库
├── config.json          # 可选：模型参数等配置，不得包含 API key
├── utils/               # 可选：辅助模块
└── prompts/             # 可选：prompt 模板
```

`my_agent.py` 必须定义 `MyAgent` 类，并实现：

```python
class MyAgent:
    def __init__(self, env):
        self.env = env

    def run(self, case_id: str) -> dict:
        ...
```

`run()` 应调用 `env.reset(case_id)` 开始单个 case，并最终返回 `final_answer`
结构化答案。平台 runner 会在选手代码结束后统一评分。完整接口说明、异常处理建议和 `final_answer` 规范见
[`docs/submission_spec.md`](docs/submission_spec.md)。

## 二、依赖与 API Key

评测环境会预装一批常用三方库；额外依赖只能从白名单中选择，并写入
`requirements.txt`。版本号必须使用 `==` 精确指定，评测时会以
`pip install -r requirements.txt --no-deps` 安装。

API key 通过环境变量注入，代码中读取即可：

```python
import os

openai_key = os.getenv("OPENAI_API_KEY")
anthropic_key = os.getenv("ANTHROPIC_API_KEY")
dashscope_key = os.getenv("DASHSCOPE_API_KEY")
```

禁止在代码、配置文件或 prompt 文件中硬编码真实 API key。

## 三、本地测试

提交前请优先使用 Docker 方式验证，确保本地行为与评测环境一致：

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

更多本地部署、单 case 调试、批量运行和常见问题见
[`docs/local_test_guide.md`](docs/local_test_guide.md)。

## 四、提交前检查清单

- [ ] 主入口文件名为 `my_agent.py`
- [ ] `my_agent.py` 中定义了 `MyAgent` 类
- [ ] `MyAgent` 实现了 `__init__(self, env)` 和 `run(self, case_id)` 方法
- [ ] 本地运行 `python -m py_compile my_agent.py` 无语法错误
- [ ] 所有额外依赖都在 `requirements.txt` 中声明，且在白名单内
- [ ] `requirements.txt` 中所有版本号使用 `==`
- [ ] 代码、配置文件和 prompt 中无硬编码 API key
- [ ] 压缩包大小不超过 50MB
- [ ] 已通过 [`docs/local_test_guide.md`](docs/local_test_guide.md) 中的本地测试
- [ ] 至少在 10 个不同 case 上测试过

## 五、评测与提交

提交后，评测系统会：

1. 解压并验证文件结构
2. 检查 `requirements.txt` 是否符合白名单
3. 安装额外依赖
4. 在隐藏测试集上运行 `MyAgent.run()` 获取 `final_answer`
5. 调用官方 evaluator 计算总分并更新排行榜

正式线上评测使用 Docker/子进程隔离 runner。选手容器只运行 `MyAgent` 并通过受控接口调用工具；
隐藏 cases、evaluator 和评分逻辑不会放入选手代码路径。仓库中的
`simulator/test_runner.py` 用于本地调试和对齐评分口径。

总分 100 分，由 TSR、AS、ES、RS 四部分组成：

| 维度 | 分值 | 说明 |
|------|------|------|
| TSR 任务成功率 | 60 分 | 核心目标完成情况 |
| AS 动作合法性 | 20 分 | 工具调用规范性 |
| ES 效率分 | 10 分 | 任务成功前提下，步数越少得分越高 |
| RS 鲁棒性 | 10 分 | 有 variants 时按变体通过率聚合；无 variants 时按 base case 是否通过给 10/0 |

## 六、常见问题

### 可以使用本地模型吗？

可以，但模型文件必须满足提交包大小限制，且推理时间计入单 case 超时。通常建议使用
API 调用或轻量规则/混合方案。

### 超过步数预算会怎样？

环境会抛出 `StepLimitExceeded`。建议捕获该异常并提交当前已有结果，否则未捕获异常会导致
该 case 得 0 分。

### 如何调试评测失败的 case？

先用 `docs/local_test_guide.md` 中的单 case 命令复现，再开启 `--verbose` 查看工具调用、
评分结果和错误信息。

## 七、联系方式

- 技术支持：contest-support@iflytek.com
- 问题反馈：GitHub Issues（赛题仓库）
