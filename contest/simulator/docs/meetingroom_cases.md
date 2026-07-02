# 会议室 Cases 总览

共 **42** 个 case，分为纯会议室域（`beta_mr_*`）和会议室+审批跨域（`beta_mr_wf_*`）两类。

---

## 一、纯会议室域

### 基础预订

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_0001 | medium | A1小镇查询并预订（需跳过冲突房间） | room.list → booking.create | 4 |
| beta_mr_0002 | easy | A2园区预订 | room.list → booking.create | 4 |
| beta_mr_0004 | easy | 当日预订 | room.list → booking.create | 4 |
| beta_mr_0008 | easy | A3大会议室预订 | room.list → booking.create | 4 |

### 改期 / 延长 / 取消

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_0011 | hard | 参会人增加后换大房间（推断容量） | booking.list → booking.cancel → room.list → booking.create | 6 |
| beta_mr_0012 | hard | 目标时段冲突后自动改到前半小时 | room.list → booking.create | 4 |
| beta_mr_0014 | medium | 会议可延长时使用 extend | booking.extend | 4 |
| beta_mr_0015 | hard | 会议不可延长时保持原会议不变 | booking.extend（失败后不操作） | 4 |
| beta_mr_0019 | medium | 取消指定会议 | booking.list → booking.cancel | 4 |
| beta_mr_0020 | hard | 取消后重订同一时段 | booking.list → booking.cancel → room.list → booking.create | 6 |
| beta_mr_0021 | hard | 取消后重订不同时段 | booking.list → booking.cancel → room.list → booking.create | 6 |

### 约束推断 / 备选策略

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_0013 | hard | A1无解后切换 A2（保留屏幕约束） | room.list × 2 → booking.create | 5 |
| beta_mr_0022 | hard | 容量不足时自动升级到更大房间 | room.list → booking.create | 4 |
| beta_mr_0023 | hard | 无屏幕房间全满后放宽屏幕约束 | room.list × 2 → booking.create | 5 |

### 查询 / 日程查看

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_0016 | medium | 查询我的会议列表 | booking.list | 3 |
| beta_mr_0017 | medium | 查询某房间当天日程 | room.schedule | 3 |
| beta_mr_0018 | medium | 查询某房间一周日程 | room.schedule | 3 |
| beta_mr_0024 | easy | 查询今天所有预订 | booking.list | 3 |
| beta_mr_0025 | medium | 查询某会议详情 | booking.list → booking.detail | 4 |

### 权限 / 边界

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_0026 | hard | 无权限房间需跳过并找可预订的 | room.list → booking.create | 4 |
| beta_mr_0027 | hard | 所有候选房间均无权限，返回失败 | room.list | 3 |
| beta_mr_0028 | medium | 预订时间超出工作时间边界 | room.list → booking.create（拒绝） | 3 |
| beta_mr_0029 | medium | 预订时长超出单次上限 | room.list → booking.create（拒绝） | 3 |

### 工位关联

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_0030 | medium | 找离我工位最近的会议室 | user.get_workspace → room.list → booking.create | 5 |
| beta_mr_0031 | hard | 找离指定同事工位最近的会议室 | user.search → user.get_workspace → room.list → booking.create | 6 |
| beta_mr_0032 | hard | 多人工位分散时找折中楼层 | user.search × N → user.get_workspace × N → room.list → booking.create | 8 |

### 对比 / 推荐

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_0033 | medium | 推荐最空闲的会议室 | room.list → room.schedule × N → booking.create | 6 |
| beta_mr_0034 | medium | 推荐容量最匹配的会议室 | room.list → booking.create | 4 |
| beta_mr_0035 | hard | 对比两个指定房间哪个更空闲 | room.schedule × 2 → booking.create | 5 |
| beta_mr_0036 | hard | 对比三个房间本周预订次数最少的 | room.schedule × 3 → booking.create | 6 |

### 地址解析

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_0037 | medium | 用 office_address 精确到楼层查询 | room.list（office_address=0552_A3_3F）→ booking.create | 4 |

### 多步查询 / 时间维度

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_0038 | hard | 跨楼栋查询：小镇1F分楼栋逐一查 | room.list(0552_A1_1F) + room.list(0552_A2_1F) → booking.create | 5 |
| beta_mr_0039 | hard | 两个固定日期取交集（本周五+下周五） | room.list × 2 → booking.create × 2 | 6 |
| beta_mr_0040 | medium | 本周内逐天查找第一个空闲（周四才有） | room.list × 4 → booking.create | 7 |
| beta_mr_0041 | hard | 连续两天（周三+周四）取交集 | room.list × 2 → booking.create × 2 | 6 |
| beta_mr_0042 | hard | 连续三天（周二+周三+周四）取交集 | room.list × 3 → booking.create × 3 | 8 |
| beta_mr_0043 | medium | 同一天同一房间上午+下午各订一次 | room.list → booking.create × 2 | 5 |
| beta_mr_0044 | hard | 分析日程找最长连续空闲时段（需≥3小时） | room.list → room.schedule × 3 → booking.create | 7 |

---

## 二、会议室 + 审批跨域

| Case ID | 难度 | 场景 | 关键工具 | step_budget |
|---------|------|------|----------|-------------|
| beta_mr_wf_0001 | medium | A1预订 + 请假草稿 | room.list → booking.create → workflow.* | 10 |
| beta_mr_wf_0006 | medium | A2预订 + 请假草稿 | room.list → booking.create → workflow.* | 10 |
| beta_mr_wf_0007 | medium | A3预订 + 请假草稿 | room.list → booking.create → workflow.* | 10 |
| beta_mr_wf_0010 | medium | A4预订 + 请假草稿 | room.list → booking.create → workflow.* | 10 |

---

## 三、难度分布

| 难度 | 数量 |
|------|------|
| easy | 4 |
| medium | 19 |
| hard | 19 |

---

## 四、能力覆盖

| 能力点 | 相关 Case |
|--------|-----------|
| 基础查询+预订 | 0001~0004, 0008 |
| 冲突检测与跳过 | 0001, 0012, 0022 |
| 换房重订 | 0011, 0020, 0021 |
| 延长会议 | 0014, 0015 |
| 取消会议 | 0019, 0020, 0021 |
| 备选策略（换园区/放宽约束） | 0013, 0023 |
| 权限边界 | 0026, 0027 |
| 时间边界 | 0028, 0029 |
| 工位关联 | 0030, 0031, 0032 |
| 日程查询与对比 | 0033, 0034, 0035, 0036 |
| 地址解析（office_address） | 0037, 0038 |
| 跨楼栋多次查询 | 0038 |
| 多日期交集 | 0039, 0041, 0042 |
| 逐天搜索（找最早/最晚） | 0040 |
| 同天多时段同房间 | 0043 |
| 日程分析（最长空闲） | 0044 |
| 跨域（会议室+审批） | wf_0001, wf_0006, wf_0007, wf_0010 |
