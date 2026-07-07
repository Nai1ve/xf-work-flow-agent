# Agent Development Rules

本仓库用于科大讯飞 NL2Workflow / 企业流程执行 Agent 比赛。后续实现必须优先追求隐藏集泛化，不允许继续围绕公开验证集做记忆化修补。

## 核心原则

- 不要把训练集、开发集、公开验证集或本地回归集写成方案或代码。
- 不要按 `case_id`、验证集文件名、固定 case 顺序、`reference_final_answer`、`gold_trajectory`、`success_check` 编写逻辑。
- 不要把本地数据中的项目名、物资名、人员名、会议室 ID、日期、订单号、workflow request id 等整理成固定答案表。
- 不要为了提升本地分数添加不可解释的业务映射，例如把某个本地 case 标题、项目短语、物资短语、人员或会议室硬映射到特定输出。
- 不要用本地标准答案或 success check 反推固定字段值、固定枚举值或固定工具调用序列。
- 允许使用本地数据做误差分类、指标分析和回归测试，但修复必须抽象成通用能力：schema 理解、工具候选验证、置信度判断、上下文维护、preflight、错误恢复。
- 每个修复必须能说明它解决的是哪一类通用能力缺口；每个写操作必须能追溯到当前 case 的真实工具证据。

## LLM 与程序职责

- LLM 应负责自然语言理解、任务图抽取、候选排序、schema/options 下的字段草案、错误后重规划。
- 程序应负责工具白名单、参数 schema 校验、step budget、缓存、上下文 ledger、写操作 preflight、枚举合法性、金额守恒、final_answer 归一化。
- 不要让 LLM 自由编造工具结果、候选 id、workflow_id、project_code、wbs_code、user_id、material value。
- 不要让程序用大量业务词表替代模型理解。业务词表只能作为候选生成或召回辅助，最终必须通过真实工具结果验证。
- Fast LLM task graph 是执行 domain 逻辑前的必经阶段；即使超时或失败，也必须生成 heuristic fallback task graph，并记录耗时、剩余时间、来源、任务数、域、意图、最低置信度、缺槽数、禁止猜测字段数和 fallback 原因，方便后续动态调整 60 秒预算。

## Workflow 设计约束

- 优先做 schema-driven，而不是固定只服务某几个公开 workflow。
- `workflow.catalog`、`workflow.schema`、`workflow.search_person`、`workflow.project_search`、`workflow.browser_search` 的真实返回必须成为保存前证据。
- `workflow.save` 前必须检查 required fields、browser/select 枚举来源、人员/项目来源、明细金额和结构。
- 多候选且用户语义不足时，应返回稳定 blocked/reply，不要硬选以追求验证集通过。
- 对公开验证集中出现过的项目、物资、人员，不能固化成隐藏测试假设。

## Meetingroom 设计约束

- 会议室写操作前必须有真实工具证据：房间候选、已有预订、日程、冲突、容量、屏幕等。
- 日期、时间、地点、人数、主题等槽位可以本地标准化，但不能依赖验证集日期表或 case 特定默认值。
- 房间选择应基于用户约束和工具返回候选，不要按公开 case 固定选某个 room_id。
- 取消、延长、换房必须优先以 `booking.list` 返回的事实为准。

## 评测与报告

- 报告可以引用验证集 case 用于说明失败模式，但不能把这些 case 变成实现规则。
- 涉及 LLM、候选排序、schema 草稿、task graph 的改动，必须用正常 runner 路径和线上模型配置验证；无模型、无 key、纯启发式结果不能作为完成验证。
- 不要读取、打印或提交 `submission/config.local.json`；本地/线上密钥只允许被运行时配置加载使用。
- 汇报分数时同时说明：本地验证集分数、隐藏集反馈、无模型降级分数、违规数量、超时风险。
- 新方案应按能力维度验收，例如：意图识别、schema 填充、候选排序、blocked 判断、跨域上下文、final_answer 格式，而不是只按验证集通过数验收。

## 提交安全

- 不提交真实 API Key，不提交 `config.local.json`。
- 不提交本地原始数据 zip、解压后的 train/val、临时 runner 目录或无关提交包。
- 如果工作区存在与当前任务无关的未跟踪目录或文件，不要擅自删除或提交。
