# SOP 目录

## 会议

查询现有预订/日程 → 解析硬约束和软偏好 → 查询实时合法候选 → 过滤冲突/权限/容量/设备 → 稳定排序或按用户明确顺序回退 → 写前校验 → 创建/取消/延长/参会人变更 → 验证最终领域状态 → 晚绑定 Projection。

换大房必须先创建并验证新房，再取消旧房；延长冲突或恢复失败时保护原会议。没有用户授权时只读查询或返回 blocked。多日期使用候选交集，工位最近使用 workspace 事实作为 anchor。默认会议时长是配置项，不由关键词硬编码。

## 请假

识别假种和操作语气 → 读取运行时 workflow schema → 解析日期/时段/时长 → 搜索合法人员候选并处理唯一性 → 根据部门/职位配置处理审批人 → 填写必填字段和原因码 → preflight → 草稿或提交 → 必要时验证待办/已办 → 投影 `workflow_draft_result`。

时长计算器独立记录 raw、explicit_hours、explicit_day_count、workday、calendar、half_day 候选；明确口径优先，无明确口径在 hybrid 保留 raw 默认，generic 不按假种猜测。

## 费用

识别流程 → schema/catalog → 查询项目、类别、物料候选 → 多候选时追问或阻断 → 计算数量×单价 → 校验明细金额守恒 → preflight → 保存草稿/提交 → 验证 → 投影。无工具价格证据的固定明细只在 legacy 兼容档位使用，generic 禁止猜测。

## 跨域

每个领域独立生成 Task，按用户顺序和显式依赖串行运行；一个 Task blocked 不抹掉其他已成功 DomainResult。任务间只传递显式结果，不共享隐式缓存。
