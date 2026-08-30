# V2 评估记录

## 当前基线

历史线上反馈为 78.43686；本地旧实现曾达到 Train 约 96.66、Val 约 98.05。V2 默认使用 `hybrid_compat`，以保留这些可观测行为并隔离不可解释 Gold。

## 已验证改动

- Task DAG 依赖、环检测、状态转换和证据隔离：纯单元测试通过。
- 会议延长恢复分支继承配置默认 30 分钟，代表性恢复用例由原先 71 分提升为 100 分。
- 跨域定向回归 11 个用例：10 个通过，平均 96.65；唯一失败是 Gold 要求把“下周二”从 2026-04-18 推到 4 月 28 日，而运行时没有该事实，按正常公历得到 4 月 21 日，已列入 legacy/批次异常隔离。
- 静态工具、会议室、workflow schema 索引已重新编译，运行时 schema 仍为权威。

## 未解决且不应数据泄漏

同义请假时长存在 24/57 小时冲突、半天时段口径冲突、缺少工具证据的费用隐藏明细、单域会议 `office_id` UUID/语义双契约。这些没有可观测分离变量，只能保持兼容策略或 blocked，并在 `reports/review_rounds.log` 记录。

全量 Train/Val 运行后，将在本文件追加平均分、TSR/AS/ES/RS、通过率、最低分簇、超时和违规统计；不把结果表编译进运行时。

## 2026-08-30 全量回归

运行配置：`AGENT_EXECUTION_PROFILE=hybrid_compat`、标准 `scripts/run_agent.py`、Train 200 + Val 50、`--parallel 4`、跳过 robustness variants。完整逐 Case 结果和普通文本日志保存在 `reports/runs/`（该目录不进入提交包）。

| 数据集 | Case | 平均分 | 通过率 | TSR | AS | ES | 平均步数 | 平均耗时 | violation |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Train | 200 | 96.57 | 89.0% (178/200) | 59.00 | 19.50 | 9.17 | 5.78 | 9.60s | 0 |
| Val | 50 | 97.78 | 92.0% (46/50) | 59.73 | 19.60 | 9.25 | 5.74 | 9.43s | 0 |

### 失败簇与归因

- Train 的 submission 失败为 `booking_result` 11、`workflow_draft_result` 7、`workflow_result` 1；Val 为 `booking_result` 2、`workflow_draft_result` 2。均无 violation 或未捕获异常。
- Val 最低分是 `beta_zh_0215`（67.50）：会议任务完成，剩余差异是请假日期。`obs.now=2026-04-18` 时“下周二”按正常公历为 2026-04-21，而该 Gold 要求 2026-04-28；运行时没有可观测依据，未加入用例特判。
- Val `beta_wf_0224`（69.00）是提交/草稿语义冲突；系统按用户明确操作执行，Gold 要求另一提交模式，保留在兼容隔离层。
- Val 的两个会议 Projection 残差（83/85 分）业务状态均已成功，差异位于 `booking_result` 的字段契约/语义房间地址；工具调用仍使用实时合法 UUID，采用 late-bound projector。
- Train 低分还包含请假 raw 时长与 Gold 的不可观测冲突、会议日程比较/预约结果投影、费用项目/金额边界。它们已按 `BUSINESS_RULE`、`SUPERSET_PROJECTION`、`COMPATIBILITY_POLICY`、`LEGACY_QUARANTINE` 分层，不把 Gold 常量写入运行时。

### 结论

当前 `hybrid_compat` 已超过候选晋级门槛（Train/Val 均 ≥95，且无 violation）；相对线上 78.43686 的差异不能直接等价，因为线上环境、模型和数据批次不同。提交前保留 `legacy_current` 回滚 Profile，并使用 [testing_guide.md](testing_guide.md) 中的命令重跑。
