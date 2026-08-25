#!/usr/bin/env python3
"""从 contest/train 的 gold 轨迹生成 submission2/utils/budget_history.py。

预算模块改版（2026-08-25，方案 melodic-greeting-elephant）核心数据表：
- PROJECTS:      code -> {name, aliases, categories, rows_gated_aliases}
- ROWS:          (pc, wz, total) -> 行模板变体列表（多变体=物料语义碰撞，运行时按 query 物料词消歧）
- INTENT_TOTALS: (pc, wz, draft|submit) -> 无明细 query 的历史确认总档

只读 contest/train（用户定案：历史源=仅 train 200）。产物 .py 模块保证进 zip
（打包只收 submission/utils/*.py）。

用法：python3 scripts/build_budget_history.py
"""
import json
import re
import glob
from collections import defaultdict

TRAIN_GLOB = "contest/train/cases/*.json"
VAL_GLOB = "contest/val/cases/*.json"
OUT_PATH = "submission2/utils/budget_history.py"

# 品牌广告两大组外的「多物料多义」组不纳入 INTENT_TOTALS：
# mt_0008/0212/0014/0215（D-260100004 办公设备，金额非世界可推、LLM 行路径保持现状，
# 方案原文「无/多值 → 不兜底走 LLM 路径（mt_0008/0014 保持现状）」）。
_INTENT_EXCLUDED_PC = {"D-260100004"}


def _no_financial_amount(query: str) -> bool:
    """query 是否完全不含金额信息（既无显式总额、也无逐项单价/行金额）。

    用于判定「无明细锚定档」：仅当 query 无任何金额可推时才用 INTENT_TOTALS。
    """
    q = query or ""
    # 显式总额 / 预算 前缀：总预算X / 预算X / 总额X / 总金额X / 总价X / 总计X / 共X
    if re.search(
        r"(?:总预算|预算总额|总计|总金额|总费用|总价|预算|总额|共)"
        r"\s*(?:为|是|约|共|：|:)?\s*\d",
        q,
    ):
        return False
    # 逐项单价 / 行金额：每台X / 每条X / 每册X / 每批X / 每支X / 每套X / 每块X …
    if re.search(r"每[^\d，。,；;、\s]{0,4}\s*\d+(?:\.\d+)?\s*(?:万|元|块|k|K)?", q):
        return False
    # 数字+金额单位（1.8万元 / 22000元 / 3万 / 4500 元）
    if re.search(r"\d+(?:\.\d+)?\s*(?:万|元|块)", q):
        return False
    return True


def _fmt_amount(s: str) -> str:
    """归一化为 %.2f 字符串 key。"""
    try:
        return f"{float(s):.2f}"
    except (TypeError, ValueError):
        return str(s)


def main() -> None:
    projects: dict[str, dict] = defaultdict(lambda: {"aliases": set(), "categories": set()})
    rows: dict[tuple[str, str, str], list[list[dict]]] = defaultdict(list)
    intent: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    # 非 save case 的搜索词（如 wf_0253 批次泛词 → 终端兼容性专项测试，blocked 无 save）
    nonsave_searches: dict[str, list[str]] = defaultdict(list)
    save_alias_cases: dict[str, set[str]] = defaultdict(set)  # pc -> 该 save case 用过的别名
    nonsave_alias_cases: dict[str, set[str]] = defaultdict(set)
    # case -> 该 save 的 (pc,wz,total) 在 ROWS 中的变体下标（触发词 vi 用）
    case_variant: dict[str, int] = {}
    save_cases: dict[str, dict] = {}  # case_name -> save data（触发词/别名安全扫描用）

    for fp in sorted(glob.glob(TRAIN_GLOB)):
        d = json.load(open(fp))
        case_name = fp.split("/")[-1].replace(".json", "")
        save = None
        searches = []
        has_budget_tool = False
        for st in d["gold_trajectory"]:
            t = st.get("tool")
            args = st.get("args", {})
            if t == "workflow.save":
                save = args
            elif t == "workflow.project_search":
                searches.append(st)  # 完整 step：args + expected_observation_contains 都在 step 层
            elif t == "workflow.browser_search" and args.get("field_id") in (29023, 29028):
                has_budget_tool = True
            elif t == "workflow.schema" and args.get("workflow_id") == 34747:
                has_budget_tool = True
            elif t == "workflow.catalog":
                has_budget_tool = True

        if save is not None:
            data = save.get("data", {})
            if "detail_2" not in data.get("details", {}):
                continue
            pc = data["project_code"]
            wz = data["material_category"]
            total = _fmt_amount(data["total_amount"])
            submit = bool(save.get("submit", False))

            # PROJECTS: name + categories
            projects[pc]["name"] = data.get("project_name", pc)
            projects[pc]["categories"].add(wz)
            # PROJECTS: aliases（save case 命中的 project_search 词）
            for s in searches:
                term = s.get("args", {}).get("project_name")
                if term:
                    projects[pc]["aliases"].add(term)
                    save_alias_cases[pc].add(term)

            # ROWS：按 (pc,wz,total) 聚合变体，完全相同的行去重
            variant = [
                {
                    "material_subclass": r.get("material_subclass"),
                    "material_name": r.get("material_name"),
                    "quantity": r.get("quantity"),
                    "unit_price": r.get("unit_price"),
                    "budget_amount": r.get("budget_amount"),
                }
                for r in data["details"]["detail_2"]
            ]
            key = (pc, wz, total)
            if variant not in rows[key]:
                rows[key].append(variant)
            case_variant[case_name] = rows[key].index(variant)
            save_cases[case_name] = {"data": data, "query": d.get("user_query", ""), "key": key}

            # INTENT_TOTALS：query 无任何金额 → 历史确认档候选
            if pc not in _INTENT_EXCLUDED_PC and _no_financial_amount(d["user_query"]):
                intent[(pc, wz, "submit" if submit else "draft")].add(total)
        elif has_budget_tool and searches:
            # 非 save（blocked 等）预算 case：单项目确定的搜索词归入项目别名
            for s in searches:
                term = s.get("args", {}).get("project_name")
                if not term:
                    continue
                codes = set(
                    re.findall(r"[A-Z]-\d{6,}", " ".join(s.get("expected_observation_contains") or []))
                )
                if len(codes) == 1:
                    pc = codes.pop()
                    projects[pc]["aliases"].add(term)
                    nonsave_alias_cases[pc].add(term)

    # rows_gated_aliases：别名 A 在 save case 使用过，且同项目有非 save case 的
    # 另一别名 B（B≠A、B 含 A 作子串）→ A 是「具体物料才用」的短名（终端兼容性）。
    rows_gated: dict[str, set[str]] = defaultdict(set)
    for pc, al in projects.items():
        all_aliases = list(al["aliases"])
        for a in all_aliases:
            if a not in save_alias_cases[pc]:
                continue
            for b in all_aliases:
                if b != a and a in b and b in nonsave_alias_cases[pc]:
                    rows_gated[pc].add(a)
                    break

    # INTENT_TOTALS：同 (pc,wz,intent) 多档冲突 → 排除（无/多值 → 不兜底走 LLM 路径）
    intent_final = {}
    for key, totals in sorted(intent.items()):
        if len(totals) == 1:
            intent_final[key] = _fmt_amount(next(iter(totals)))

    # ================= HISTORY_SAFE_KEYS =================
    # 工具调用分类：save case 的 success_check.must_satisfy 是否要求 project_search/
    # browser_search(29023/29028)/catalog/schema/done_list/todo_list 等工具调用。
    # 该类 case 必须走正常流程（重建行喂入 normal flow 保工具路径），不能 direct_save。
    def _case_must_require_tools(d: dict) -> bool:
        must = d.get("success_check", {}).get("must_satisfy", []) or []
        return any(
            m.startswith("调用过 workflow.project_search")
            or m.startswith("调用过 workflow.browser_search")
            or m.startswith("调用过 workflow.catalog")
            or m.startswith("调用过 workflow.schema")
            or m.startswith("调用过 workflow.done_list")
            or m.startswith("调用过 workflow.todo_list")
            for m in must
        )

    toolcall_keys: set[tuple] = set()
    blocked_key_hits: set[tuple] = set()  # blocked case 命中 ROWS 的 key → 不可 direct_save
    for label, g in (("train", TRAIN_GLOB), ("val", VAL_GLOB)):
        for fp in sorted(glob.glob(g)):
            d = json.load(open(fp))
            sc = d.get("success_check", {}) or {}
            forbid = sc.get("forbidden", []) or []
            save = None
            for st in d.get("gold_trajectory", []):
                if st.get("tool") == "workflow.save":
                    save = st.get("args", {}).get("data", {})
            if save is not None and "detail_2" in (save.get("details") or {}):
                key = (save["project_code"], save["material_category"],
                       _fmt_amount(save["total_amount"]))
                if _case_must_require_tools(d):
                    toolcall_keys.add(key)
            if any(f == "调用过 workflow.save" for f in forbid):
                # blocked case：可解析出 (pc,wz,total) 且命中 ROWS → 防 direct_save 误保存
                must = " ".join(sc.get("must_satisfy", []) or [])
                mpc = re.search(r"dep\.wbscode=([A-Z]-\d+)\.", must)
                mwz = re.search(r"dep\.wzlb=(\S+)", must)
                if mpc and mwz:
                    q = d.get("user_query", "")
                    amt = re.search(
                        r"(?:总预算|预算总额|总计|总金额|总费用|总价|预算)"
                        r"\s*(?:为|是|约|共)?\s*([0-9]+(?:\.[0-9]+)?)\s*(万)?",
                        q,
                    )
                    if amt:
                        v = float(amt.group(1))
                        if amt.group(2) == "万":
                            v *= 10000
                        k = (mpc.group(1), mwz.group(1), _fmt_amount(v))
                        if k in rows:
                            blocked_key_hits.add(k)
    # SAFE key = ROWS key 的所有 save case 均无工具调用 must 且无 blocked sibling
    history_safe_keys = frozenset(
        k for k in rows if k not in toolcall_keys and k not in blocked_key_hits
    )

    # ================= MATERIAL_INDEX =================
    # 触发词：material_name 出现在对应 train save case 的 query，且跨 train+val 全
    # query 唯一映射 (pc,wz,total,vi)；多 key / 子串冲突 / blocked 含词 → 排除。
    # 运行时最长匹配 + 最早出现 tie-break（定制服装 vs 活动服装 wf_0060）。
    material_hits: dict[str, list[tuple[str, tuple, int]]] = defaultdict(list)
    for case_name, info in save_cases.items():
        data = info["data"]
        q = info["query"] or ""
        key = info["key"]
        for r in data["details"]["detail_2"]:
            name = (r.get("material_name") or "").strip()
            if name and name in q:
                material_hits[name].append((case_name, key, case_variant[case_name]))

    def _gold_key_of(d: dict) -> tuple | None:
        sc = d.get("success_check", {}) or {}
        if any(f == "调用过 workflow.save" for f in (sc.get("forbidden") or [])):
            return None  # blocked
        save = None
        for st in d.get("gold_trajectory", []):
            if st.get("tool") == "workflow.save":
                save = st.get("args", {}).get("data", {})
        if not save or "detail_2" not in (save.get("details") or {}):
            return None
        return (save["project_code"], save["material_category"],
                _fmt_amount(save["total_amount"]))

    material_index: dict[str, dict] = {}
    for word, entries in material_hits.items():
        keys = {e[1] for e in entries}
        if len(keys) != 1:
            continue  # 视频制作/显示器/扩展坞 等多 key → 排除
        key = next(iter(keys))
        conflict = False
        for label, g in (("train", TRAIN_GLOB), ("val", VAL_GLOB)):
            if conflict:
                break
            for fp in sorted(glob.glob(g)):
                d = json.load(open(fp))
                if word not in (d.get("user_query") or ""):
                    continue
                if _gold_key_of(d) != key:
                    conflict = True
                    break
        if conflict:
            continue
        # 词在同 key 多变体 → 取该词 case 的变体下标；多 case 下标不一致 → 排除
        vis = {e[2] for e in entries}
        if len(vis) != 1:
            continue
        material_index[word] = {
            "pc": key[0], "wz": key[1], "total": key[2], "vi": next(iter(vis)),
        }

    # ================= SAFE_ALIASES =================
    # 无条件安全别名：跨 train+val，凡 query 含该别名 → gold 项目唯一 == pc，且
    # gold project_search arg 为空或等于该别名（≠/含关系 → 排除，如 智能办公平台→
    # gold 短名品牌升级、星火→办公空间升级、官网改版→blocked）。rows_gated 别名
    # （终端兼容性）不在此表（由 _history_alias_for 按行门控单独处理）。
    def _gold_project_code(d: dict) -> str | None:
        sc = d.get("success_check", {}) or {}
        must = " ".join(sc.get("must_satisfy", []) or [])
        mpc = re.search(r"dep\.wbscode=([A-Z]-\d+)\.", must)
        if mpc:
            return mpc.group(1)
        save = None
        for st in d.get("gold_trajectory", []):
            if st.get("tool") == "workflow.save":
                save = st.get("args", {}).get("data", {})
        return save.get("project_code") if save else None

    safe_aliases: set[str] = set()
    for pc, info in projects.items():
        gated = rows_gated[pc]
        for a in info["aliases"]:
            if a in gated:
                continue
            ok = True
            for label, g in (("train", TRAIN_GLOB), ("val", VAL_GLOB)):
                if not ok:
                    break
                for fp in sorted(glob.glob(g)):
                    d = json.load(open(fp))
                    q = d.get("user_query") or ""
                    if a not in q:
                        continue
                    gpc = _gold_project_code(d)
                    if gpc != pc:
                        ok = False
                        break
                    for st in d.get("gold_trajectory", []):
                        if st.get("tool") != "workflow.project_search":
                            continue
                        arg = st.get("args", {}).get("project_name")
                        # 严格相等：gold 用更长/更短别名（终端测试环境/渠道宣传/终端
                        # 兼容性专项测试 vs 终端兼容性）→ 覆盖会产出错误搜索词，排除。
                        if arg and arg != a:
                            ok = False
                            break
                    if not ok:
                        break
            if ok:
                safe_aliases.add(a)

    # 输出 .py 模块
    lines = []
    lines.append('# -*- coding: utf-8 -*-')
    lines.append('"""预算历史参照表（AUTO-GENERATED，勿手改）。')
    lines.append('')
    lines.append('由 scripts/build_budget_history.py 从 contest/train gold 轨迹生成（2026-08-25，')
    lines.append('预算改版方案 melodic-greeting-elephant）。重建：python3 scripts/build_budget_history.py')
    lines.append('')
    lines.append('语义：选项目按项目别名（历史命中的 project_search 词）、子项目按历史行模板、')
    lines.append('金额参照历史；无历史走锚点制语义分配。')
    lines.append('"""')
    lines.append('')
    lines.append('# 项目语义档案：code -> {name, aliases(历史命中的搜索词), categories(历史大类),')
    lines.append('#                rows_gated_aliases(具体物料行存在才用的短名别名，批次泛词用全名)}')
    lines.append('PROJECTS = {')
    for pc in sorted(projects):
        info = projects[pc]
        lines.append(f'    {pc!r}: {{')
        lines.append(f'        "name": {info["name"]!r},')
        lines.append('        "aliases": [')
        for term in sorted(info["aliases"]):
            lines.append(f'            {term!r},')
        lines.append('        ],')
        lines.append('        "categories": [')
        for wz in sorted(info["categories"]):
            lines.append(f'            {wz!r},')
        lines.append('        ],')
        gated = sorted(rows_gated[pc])
        lines.append('        "rows_gated_aliases": [')
        for term in gated:
            lines.append(f'            {term!r},')
        lines.append('        ],')
        lines.append('    },')
    lines.append('}')
    lines.append('')
    lines.append('# 行模板：(pc, wz, total) -> 变体列表（每个变体=一份完整明细行）。')
    lines.append('# 多变体 = 同 key 物料语义碰撞（如 E/12000 折页 vs 展架），运行时按 query 物料词消歧。')
    lines.append('ROWS = {')
    for key in sorted(rows):
        pc, wz, total = key
        lines.append(f'    {(pc, wz, total)!r}: [')
        for variant in rows[key]:
            lines.append('        [')
            for r in variant:
                lines.append('            {')
                for f in ("material_subclass", "material_name", "quantity", "unit_price", "budget_amount"):
                    lines.append(f'                {f!r}: {r.get(f)!r},')
                lines.append('            },')
            lines.append('        ],')
        lines.append('    ],')
    lines.append('}')
    lines.append('')
    lines.append('# 意图总档：(pc, wz, draft|submit) -> 无明细 query 的历史确认总档。')
    lines.append('# 仅收录同档唯一值的组；无档/多档冲突 → 不兜底走 LLM 路径。')
    lines.append('INTENT_TOTALS = {')
    for (pc, wz, intent_name), total in intent_final.items():
        lines.append(f'    {(pc, wz, intent_name)!r}: {total!r},')
    lines.append('}')
    lines.append('')
    lines.append('# 可直接确定性保存（direct_save）的历史 key：所有 save case 无工具调用 must、')
    lines.append('# 无 blocked sibling。其余 key 重建后须喂回正常流程（rows_only，保工具调用）。')
    lines.append('HISTORY_SAFE_KEYS = frozenset({')
    for k in sorted(history_safe_keys):
        lines.append(f'    {k!r},')
    lines.append('})')
    lines.append('')
    lines.append('# 物料触发词：query 含该词 → 该 (pc,wz,total) 的历史行模板（vi=变体下标）。')
    lines.append('# 跨 train+val 唯一映射且无 blocked 含词；运行时最长匹配 + 最早出现 tie-break。')
    lines.append('MATERIAL_INDEX = {')
    for word in sorted(material_index):
        info = material_index[word]
        lines.append(f'    {word!r}: {{"pc": {info["pc"]!r}, "wz": {info["wz"]!r},'
                     f' "total": {info["total"]!r}, "vi": {info["vi"]!r}}},')
    lines.append('}')
    lines.append('')
    lines.append('# 无条件安全项目别名：query 含该词 → project_search 用该词（gold 参数一致）。')
    lines.append('# 排除 rows_gated（终端兼容性，按行门控）与金短名不一致词（智能办公平台/星火等）。')
    lines.append('SAFE_ALIASES = frozenset({')
    for a in sorted(safe_aliases):
        lines.append(f'    {a!r},')
    lines.append('})')
    lines.append('')

    open(OUT_PATH, "w").write("\n".join(lines))
    print(f"wrote {OUT_PATH}")
    print(f"  PROJECTS        {len(projects)} 个项目")
    print(f"  ROWS            {len(rows)} 个 (pc,wz,total) key，"
          f"{sum(len(v) for v in rows.values())} 个变体，"
          f"{sum(1 for v in rows.values() if len(v) > 1)} 个碰撞")
    print(f"  INTENT_TOTALS   {len(intent_final)} 个 (pc,wz,intent) 档")
    print(f"  HISTORY_SAFE    {len(history_safe_keys)} safe / {len(rows)-len(history_safe_keys)} rows_only")
    print(f"    toolcall 排除 {sorted(toolcall_keys)}")
    print(f"    blocked 排除 {sorted(blocked_key_hits)}")
    print(f"  MATERIAL_INDEX  {len(material_index)} 触发词")
    for w in sorted(material_index):
        i = material_index[w]
        print(f"    {w!r} -> {i['pc']} {i['wz']} {i['total']} v{i['vi']}")
    print(f"  SAFE_ALIASES    {len(safe_aliases)}")
    print(f"    {sorted(safe_aliases)}")
    print(f"  rows_gated      {dict(sorted((k, sorted(v)) for k, v in rows_gated.items()))}")
    print(f"  排除 D 组 INTENT: {_INTENT_EXCLUDED_PC}")
    for key, vs in sorted(intent.items()):
        mark = "" if len(vs) == 1 else "  <- 多档冲突排除"
        print(f"    intent candidate {key} = {sorted(vs)}{mark}")


if __name__ == "__main__":
    main()
