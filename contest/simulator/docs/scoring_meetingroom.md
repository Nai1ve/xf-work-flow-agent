# 评分逻辑说明：会议室任务

本文档说明 `contest/simulator/evaluator.py` 中与会议室任务相关的评分逻辑，帮助维护 case、工具实现和评测器时保持一致。

## 总分结构

总分 100 分，由四部分组成：

- `TSR`：60 分，任务成功率
- `AS`：20 分，动作合法性
- `ES`：10 分，效率分
- `RS`：10 分，鲁棒性或无变体时的整题通过得分

整题通过条件：

1. `success_check.must_satisfy` 全部通过
2. `reference_final_answer` 中要求的字段全部匹配
3. `success_check.forbidden` 中没有任何违规

对应实现见：

- `contest/simulator/evaluator.py`

## TSR：会议室相关条件

### 1. 存在成功会议预订

条件格式示例：

```text
存在成功会议预订，day=2026-04-21, office_id=A1, start=14:00, end=15:00
```

判定规则：

- 在 `state["bookings"]` 中查找未取消的预订
- `day`、`start`、`end` 直接和预订记录比对
- `office_id` 额外做两层校验：
  - 房间所属 `building` 必须匹配，比如 `A1`
  - 预订记录中的 `office_id` 也必须是合法值，允许为：
    - case 条件中的楼栋值，例如 `A1`
    - 房间 `officeId`
    - 房间 `room_id`

这样可以避免“房间订对了，但工具参数里的 `office_id` 乱传”仍然得分。

### 2. 会议室容量

条件格式示例：

```text
会议室容量>=10
```

判定规则：

- 只统计未取消的预订
- 找到该预订对应房间
- 比较房间 `capacity`

如果预订已取消，则不再满足容量条件。

### 3. 预订的会议室有屏幕

条件格式示例：

```text
预订的会议室有屏幕（hasScreen=true）
```

判定规则：

- 只统计未取消的预订
- 房间满足以下任一条件即可：
  - `hasScreen == true`
  - `features` 中包含 `"screen"`

评测器把 `hasScreen` 和 `screen` feature 视为同一语义。

### 4. 预订的会议室位于指定园区

条件格式示例：

```text
预订的会议室位于A1园区（小镇）
```

判定规则：

- `building` 匹配 `A1`
- 如果条件里包含园区名称，例如 `小镇`，则 `campus` 也必须匹配

### 5. 存在会议室候选

条件格式示例：

```text
存在会议室候选，office_id=A1, capacity>=10, feature=screen, start=14:00, end=15:00
```

判定规则：

- 回看历史中的 `meetingroom.room.list` 调用结果
- 结果中至少有一个房间同时满足：
  - `office_id` 对应的楼栋匹配
  - 容量满足 `capacity>=...`
  - feature 满足，例如 `screen`
  - 若条件里给出 `start/end`，则该房间在该时段不能与 `busy_slots` 冲突

注意：

- 候选评分不再只看属性，也会看目标时段是否可用
- `feature=screen` 同样兼容 `hasScreen=true`

### 6. 最终仅新增一条活跃会议预订

条件格式示例：

```text
最终仅新增一条活跃会议预订
```

判定规则：

- 只统计本轮通过 `meetingroom.booking.create` 成功创建的预订
- 如果这些新建预订在最终状态中仍为活跃，计入候选集合
- 该集合大小必须恰好为 `1`

注意：

- case 里的 `meetingroom_seed_bookings` 不计入这条规则
- 这条规则适合基础预订题，用来约束“不能一边试一边多订几个房间不清理”
- 对“换房重订”或“取消后重订”类题，不建议直接使用这条规则，应该改用更具体的重订条件

### 7. 预订的会议室可预订

条件格式示例：

```text
预订的会议室可预订
```

判定规则：

- 只统计未取消的预订
- 找到该预订对应房间
- 房间的 `bookable` 必须为 `true`

这条规则适合用在“候选里混有不可预订房间”的题目里，防止 Agent 只按容量、屏幕或距离选到了实际上没有预订权限的房间。

### 8. 不存在新的成功会议预订

条件格式示例：

```text
不存在新的成功会议预订
```

判定规则：

- 只统计本轮通过 `meetingroom.booking.create` 成功创建的预订
- 如果这些新建预订在最终状态中仍为活跃，计入集合
- 该集合大小必须为 `0`

这条规则适合：

- “全部候选都不可预订”时要求 Agent 正确阻塞
- “先给候选，不要直接预订”的题型

## AS：会议室相关违规

会议室任务常见违规包括：

- `调用未授权工具`
- `会议预订时间与房间占用冲突`
- `未确认即执行高风险写操作`
- `存在额外新增活跃会议预订`

对于会议室任务，`调用未授权工具` 还包括：

- 对 `bookable=false` 的会议室直接执行 `meetingroom.booking.create`

规则：

- 只要触发任一 `forbidden`，`AS` 直接为 `0`
- 如果没有触发 `forbidden`，则每个工具错误或提交字段错误扣 5 分，最低扣到 0

其中：

- `存在额外新增活跃会议预订` 的含义是：
  - 本轮通过 `meetingroom.booking.create` 成功创建
  - 且最终仍为活跃状态
  - 的预订数量大于 `1`

这条违规规则和“最终仅新增一条活跃会议预订”通常建议配套使用：

- `TSR` 负责约束最终状态不应多留会议
- `AS` 负责把“乱订多个房间不清理”明确判为不规范操作

## ES：效率分

效率分前提：

- 所有 `must_satisfy` 条件都通过

计算规则：

- 如果超过步数预算，`ES = 0`
- 否则优先使用 case 中的 `optimal_steps`
- 若 case 未配置，则退化为 `gold_trajectory` 长度

公式：

```text
ES = 10 * (1 - max(used - optimal_steps, 0) / step_budget)
```

## RS：鲁棒性聚合

单次 evaluator 输出中：

- `task_passed = true` 时，`RS = 10`
- 否则 `RS = 0`

正式选手 runner 会在 base case 之外自动运行 `robustness_variants`，并用变体通过率覆盖
base case 的 RS：

```text
RS = 10 * (通过变体数 / 变体总数)
```

没有配置变体的 case 继续使用单次 evaluator 的 RS。

## 会议室工具与评分口径统一点

会议室工具 `meetingroom.room.list` 当前与评分器保持以下一致性：

- `office_id` 作为 `building` 的兼容别名
- `has_screen=true` 和 `"screen"` feature 语义一致
- 返回的房间结果中会包含：
  - `building`
  - `campus`
  - `hasScreen`
  - `features`
  - `busy_slots`

这样评测器可以直接基于调用结果判断候选质量。

## 当前建议

如果后续继续扩会议室题，建议遵守以下原则：

1. 预订题尽量显式写出 `day/start/end/office_id`
2. 候选题如果要求“可订候选”，应在 `must_satisfy` 中带上 `start/end`
3. 屏幕类要求优先写成 `feature=screen` 或明确写“有屏幕”
4. 高风险写操作题尽量配 `confirmation_required_before`
5. 基础预订题建议同时配置：
   - `最终仅新增一条活跃会议预订`
   - `存在额外新增活跃会议预订`
6. 变更类题建议不要直接复用基础预订题的唯一性条件，而应使用更具体的：
   - `调用过 meetingroom.booking.extend`
   - `存在已取消会议`
   - `原会议已取消且存在新的成功会议预订`
   - `原会议已取消且存在更大会议室预订`
7. “不可预订房间混在候选中”的题建议额外配置：
   - `预订的会议室可预订`
8. “所有候选都不可预订，应正确拒绝”的题建议额外配置：
   - `不存在新的成功会议预订`

## 相关文件

- 评测器：`contest/simulator/evaluator.py`
- 会议室工具：`contest/simulator/tools/meetingroom.py`
- 会议室共享数据：`contest/data/meetingroom_data.json`
- 会议室样例：
  - `contest/cases/beta_mr_0002.json`
  - `contest/cases/beta_mr_0004.json`
  - `contest/cases/beta_mr_0008.json`
  - `contest/cases/beta_mt_0001.json`
