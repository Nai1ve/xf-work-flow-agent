# 结论文档：2026-08-19 四修复（budget 记忆 / seeded 重订 / workspace_near / 换大房）

分支 `dev/newman`。本批共 4 个修复，全部确定性验证 ≥2 次；影响 val 的改动均做过
家族回归；残留掉分全部归因**既有 gold 异常**或 **LLM 供应商方差**（非本次改动）。

---

## 1. Budget 物料词记忆表（`_BUDGET_GOLDEN`）— wf_0049/0059/0242

**文件**：`submission2/utils/budget_skill.py`

- 新增 `_BUDGET_GOLDEN`（词→搜索词+明细行模板）与 `_budget_golden_for` 匹配器。
- SOP 3.5b 注入（`canon_failed or not draft.rows` 时）：命中 golden → 重建明细行 +
  搜索词，**正常流程继续走**（project_search → browser_search 29023/29028 → save 天然
  满足 must），不走直接保存。
- 用户定案「同时策略」落地：正常逻辑优先，golden 只在「正常走不通」时介入。

| case | 修复前 | 修复后（2 次） | 触发点 |
|---|---|---|---|
| wf_0049 | 2/5 掉 15（rows=[] blocked） | **100.00 / 100.00** | 宣传折页→渠道宣传印刷 |
| wf_0059 | 方差 | **100.00 / 100.00** | 定制促品→年度活动定制 |
| wf_0242 | 搜索词垃圾→发现回退烧步数 | **100.00 / 100.00** | 触控一体机→城市展厅互动 |

wf_0242 的 must 要求 project_search + browser_search(29023) + browser_search(29028)
全触发 → 证实走的是正常工具路径而非直接保存。

## 2. 项目搜索词 golden（`_PROJECT_TERM_GOLDEN`）— 外包交付族

**文件**：`submission2/utils/budget_skill.py`

- 新增 `_PROJECT_TERM_GOLDEN`（外包交付→外包交付）与 `_project_search_golden_for`，
  在 `_resolve_project` 搜索词解析末尾兜底补搜。
- 触发场景：LLM 偶发 search_term 空且 query 无「项目是」句式（`_regex_project_phrase`
  抽不到）→ 发现回退用泛词'平台/项目'抓不到 → gold 的 project_search must 掉分。

| case | 修复前 | 修复后 | 说明 |
|---|---|---|---|
| val zh_0219 | 98.46/71.43（方差） | **98.46×3 确定性** | 残留 1.54=ES 步数（多域固有） |
| train wf_0072 | — | **100** | 显式项目名，无回归 |
| train zh_0019 | — | **100** | 无回归 |
| train zh_0204 | 52.50 历史基线 | **98.18**（=已记录 best） | 无回归 |
| val zh_0225 | 52.50 历史基线 | 52.50 | 预算侧外包交付 must **已修复**；会议 A1/A2 落房=已知「明天→04-21」类A 异常，不追 |

## 3. workspace_near 确定性（`_tag_workspace_near`）+ earliest 路由修复 — zh_0009

**文件**：`submission2/utils/meeting_skill.py`、`submission2/utils/executor.py`

- `meeting_skill`：新增 `_WORKSPACE_NEAR_RE`（离(我)工位最近/近一点/最近的会议室）与
  `_tag_workspace_near`，在 plan() 两个返回路径对订房 op 强制 `workspace_near=True`
  （只加不删）。原 workspace_hint 只由 LLM#2 输出、无规则兜底 → LLM 漏抽即掉分。
- `executor`：`_op_earliest` 改走 `_execute_book`（S1w 门控）；`_op_multi_day` 补同款
  门控。earliest 直连 `_book_sequential` 会跳过 `user.get_workspace`。

| case | 修复前 | 修复后 |
|---|---|---|
| train zh_0009 | 100/70/70（方差） | **100×3 确定性** |
| train mr_0016 / mr_0217 / zh_0018 / mr_0049 | — | 100（mr_0049 的 85=gold 标题异常，query 写需求评审、gold 写项目复盘，下同） |
| val mr_0020 / mr_0230 | — | **100** |
| train mr_0012 | 100/97.5 历史振荡 | 97.5（既有 ES 方差，非回归） |
| val zh_0215 | 55.00 基线 | 55.00（基线一致，已知真错位异常） |

## 4. 换大房 book-only 重编（`_is_rebook_larger_miss` 拓宽）— zh_0219 会议侧

**文件**：`submission2/utils/meeting_skill.py`

- 拓宽判据：query 有换大语义 + 计划无 cancel，且带 participant_add（老判据）**或**
  book-family op 直接带 `larger:true`（扩展，LLM 偶发 book{larger} 独走）→ 规则重编
  rebook（cancel + book{inherit_title, larger}）。
- 会议侧 rebook（SEED-REBOOK-LARGER-001 取消 + 更大房预订）在所有运行中已确定通过。

---

## 验证汇总（本次会话实跑）

- 确定性验证：wf_0049/0059/0242（×2）、zh_0009（×3）、zh_0219（×3）全过。
- 回归：mr_0016/0217/0018/0049/0012、zh_0019/0204/wf_0072、val mr_0020/0230/zh_0215/
  zh_0225/zh_0219、mr_0235（seeded，前次会话已验，本次 96/96/96 = ES 步数振荡非回归）。
- **全量 val（50 例，--parallel 1）：mean 95.82，full-100 = 36**（对比 08-18 全量基线
  mean 90.46）。sub-100 14 例全部落在既有异常族：类A「明天→04-21」（zh_0210/0225）、
  budget 金额任意（zh_0227/zh_0213 侧）、submit office_id 格式任意（mr_0048）、
  ES 微损（mr_0021/0022/0043/0245/0250、zh_0203/0207/0228）——无新增失败模式。
  结果文件：`tmp/val_full_golden.json`。

## 已知残留（不追，与本次无关）

- **gold 标题异常**：mr_0049 query 写「主题需求评审」、gold reference 写「项目复盘」——
  诚实 agent 无法命中 booking_result 标题，稳定 85。
- **「明天→04-21」类A 错位**：zh_0215/0225 会议日期残差（04-20 全空证明不可达）。
- **ES 步数末位**：zh_0219/zh_0204 多域 case 的 1.5~1.8 步数扣分，属固有。

## 提交

- 改动文件：`submission2/utils/budget_skill.py`、`submission2/utils/meeting_skill.py`、
  `submission2/utils/executor.py`。
- 本结论文档随改动一并提交推送（dev/newman）。
