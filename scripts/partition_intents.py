"""按意图识别（分解 + 路由）对数据集做分类划分，输出 Excel。

用法：
    # 1) 首次：线上识别（每 case 一次 LLM，与 my_agent 同路径）→ 存 JSON + 写 Excel
    PYTHONPATH=submission .venv/bin/python scripts/partition_intents.py \
        --split train --parallel 4 --out reports/intent_partition/intent_partition_train.xlsx

    # 2) 旧 Excel 一次性迁移为 JSON（不重新调模型，识别结果唯一真源）
    PYTHONPATH=submission .venv/bin/python scripts/partition_intents.py \
        --split train --migrate-xlsx reports/intent_partition/intent_partition_train.xlsx

    # 3) 重建 Excel（不触网，0 次 LLM 调用）
    PYTHONPATH=submission .venv/bin/python scripts/partition_intents.py \
        --split train --rebuild --out reports/intent_partition/intent_partition_train.xlsx

要点（与 my_agent 线上路径一致）：
- 每个 case 用 case JSON 的 `user_query / now / mode` 作为识别输入；
- 每个 case 新建 LLMGateway（llm_budget_s 按 case 隔离）；
- **多流程标明**：分类明细含 `流程数 / 是否多流程 / 多流程组合 / 确认状态` 列，多流程行黄底高亮，
  另有「多流程汇总」sheet 一行一个列全流程；
- **跨多个业务的一段话全部写出来**：分类明细排序 `(case_id, 分类)`，同一 case 跨多个业务的行连续展开；
- **确认状态**：`confirm` ∈ model / manual；`--manual-confirm <json>` 可在重建时把指定 case 标为
  人工已确认（不重跑 LLM）；「人工确认记录」sheet 记录人工确认的历史。

安全（AGENT.md）：只读取 case 字段与识别结果；LLM 密钥由 LLMGateway 内部读取，
本脚本不读、不打印、不写入任何 API key / config.local.json 字段。
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# 确保能 import submission/utils（调用方用 PYTHONPATH=submission 亦可兜底）。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "submission") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "submission"))

from utils.llm_gateway import LLMGateway  # noqa: E402
from utils.understanding import IntentRecognizer  # noqa: E402

# 粗粒度单元词表（与提示词卡片一致，仅意图，无槽位）。
UNIT_LABELS = {"meeting": "会议", "leave": "请假", "budget": "预算申报"}
UNIT_ORDER = {"meeting": 0, "leave": 1, "budget": 2}

# 待确认阈值：LLM 来源且置信度不低于该值 → 视为「模型确认」；否则需要人工确认。
CONF_CONFIRM = 0.7

# 多流程行高亮底色（浅黄）。
HIGHLIGHT_FILL = "FFF2CC"


def _combo_of(units: list[str]) -> str:
    return ",".join(units) if units else "(空)"


def _ordered(units: list[str]) -> list[str]:
    """去重并按会议/请假/预算固定序排。"""
    return sorted({u for u in units if u in UNIT_LABELS}, key=UNIT_ORDER.get)


class _CaptureLogger:
    """收集 LLM/识别器的 warning 原因（填充「人工确认记录」的原因列），不做日志输出。"""

    def __init__(self) -> None:
        self.records: list[str] = []

    def child(self, *_args: Any, **_kwargs: Any) -> "_CaptureLogger":
        return self

    def info(self, _message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        self.records.append(str(message))

    def section(self, _message: str) -> None:
        pass


def recognize_one(case_path: Path) -> dict[str, Any]:
    """对单个 case 做意图识别，返回可序列化的识别结果（确认状态在 build 阶段填充）。"""
    case = json.loads(case_path.read_text(encoding="utf-8"))
    query = str(case.get("user_query") or "")
    now = str(case.get("now") or "")
    mode = case.get("mode")

    capture = _CaptureLogger()
    gateway = LLMGateway(logger=capture)
    ir = IntentRecognizer(logger=capture).analyze(query, now, mode, gateway)
    stats = gateway.stats_summary()

    units = _ordered([u.unit_type for u in ir.task_units])
    source = ir.source
    conf = float(ir.confidence or 0.0)

    if source == "fallback":
        reason = "规则兜底（LLM 未给出可信结果）"
    elif conf < CONF_CONFIRM:
        reason = "置信度偏低"
    else:
        reason = ""
    if capture.records:
        detail = capture.records[-1]
        if len(detail) > 200:
            detail = detail[:200] + "…"
        reason = (reason + "；" if reason else "") + detail

    return {
        "case_id": case.get("case_id"),
        "user_query": query,
        "now": now,
        "mode": mode,
        "units": units,
        "source": source,
        "confidence": conf,
        "elapsed_s": round(float(ir.elapsed_s or 0.0), 2),
        "reason": reason,
        "domains": ",".join(case.get("primary_domains") or []),
        "tags": ",".join(case.get("tags") or []),
        "llm_stats": stats,
        "confirm": None,  # model / manual，由迁移或 --manual-confirm 填充
        "review_note": "",
    }


def apply_manual_confirm(records: list[dict[str, Any]], mconf_path: str | None) -> None:
    """把 `--manual-confirm` 指定的 case 标为人工已确认（units 以人工结论为准）。"""
    if not mconf_path:
        return
    data = json.loads(Path(mconf_path).read_text(encoding="utf-8"))
    for rec in records:
        spec = data.get(rec["case_id"])
        if not spec:
            continue
        units = spec.get("units")
        if units is not None:
            rec["units"] = _ordered([str(u) for u in units])
        rec["confirm"] = "manual"
        rec["review_note"] = str(spec.get("note") or "")
        rec["reason"] = str(spec.get("reason") or rec["reason"])


def migrate_xlsx(xlsx_path: Path, out_json: Path, mconf_path: str | None) -> list[dict[str, Any]]:
    """从旧 Excel 回填识别结果 JSON（不重新调模型）。

    旧「分类明细」按 case 聚合 units；`是否待确认==是` 的 case 按用户确认结论标
    `confirm="manual"`，其余 `confirm="model"`。
    """
    from openpyxl import load_workbook

    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    detail = wb["分类明细"]
    per_case: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in detail.iter_rows(min_row=2, values_only=True):
        if not row[0]:
            continue
        cid = str(row[0])
        if cid not in per_case:
            per_case[cid] = {
                "case_id": cid,
                "user_query": row[2],
                "mode": row[8] or None,
                "units": [],
                "source": row[6],
                "confidence": float(row[5] or 0.0),
                "elapsed_s": float(row[7] or 0.0),
                "domains": row[10] or "",
                "tags": row[11] or "",
                "reason": "",
                "llm_stats": {},
                "confirm": "model",
                "review_note": "",
                "now": "",
            }
            order.append(cid)
        units = str(row[3] or "")
        if units and units not in per_case[cid]["units"]:
            per_case[cid]["units"].append(units)
        if str(row[9]) == "是":
            per_case[cid]["confirm"] = "manual"

    # 旧「待确认」sheet：补 reason / review_note。
    if "待确认" in wb.sheetnames:
        for row in wb["待确认"].iter_rows(min_row=2, values_only=True):
            if not row[0]:
                continue
            rec = per_case.get(str(row[0]))
            if rec:
                rec["reason"] = str(row[5] or "")
                if len(row) > 9:
                    rec["review_note"] = str(row[-1] or "")

    records = [per_case[cid] for cid in order]
    apply_manual_confirm(records, mconf_path)
    _save_json(records, out_json)
    n_manual = sum(1 for r in records if r["confirm"] == "manual")
    print(f"[partition] 迁移 {len(records)} case -> {out_json}（人工确认 {n_manual}）", flush=True)
    return records


def _save_json(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(records, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def _load_json(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------- Excel 构建 --

def build_excel(records: list[dict[str, Any]], out_path: Path, meta: dict[str, Any]) -> None:
    """把识别结果写成 Excel：统计概览 / 分类明细 / 多流程汇总 / 人工确认记录。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    header_fill = PatternFill("solid", fgColor="D9E1F2")
    multi_fill = PatternFill("solid", fgColor=HIGHLIGHT_FILL)
    header_font = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")

    # 确认状态填充：pending 保留；confirm in (model/manual) 视为已确认。
    pending = [r for r in records if not r.get("confirm")]
    confirmed = [r for r in records if r.get("confirm")]
    manual = [r for r in confirmed if r["confirm"] == "manual"]
    model = [r for r in confirmed if r["confirm"] == "model"]
    total = len(records)

    # ---- 组合统计（仅已确认；未确认单独列出）----
    combo_counter: dict[str, dict[str, int]] = collections.defaultdict(lambda: {"confirmed": 0, "pending": 0})
    for r in confirmed:
        combo_counter[_combo_of(r["units"])]["confirmed"] += 1
    for r in pending:
        combo_counter[_combo_of(r["units"])]["pending"] += 1
    combo_order = sorted(combo_counter, key=lambda k: (UNIT_ORDER.get(k.split(",")[0], 9), k))

    unit_counter: dict[str, int] = collections.Counter()
    for r in confirmed:
        for u in r["units"]:
            unit_counter[u] += 1

    # 多流程统计（已确认）
    multi = [r for r in confirmed if len(r["units"]) > 1]
    multi_combo: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in multi:
        multi_combo[_combo_of(r["units"])].append(r)

    n_llm = sum(1 for r in confirmed if r["source"] == "llm")
    n_fallback = len(confirmed) - n_llm
    avg_conf = sum(r["confidence"] for r in confirmed) / len(confirmed) if confirmed else 0.0
    avg_elapsed = sum(r["elapsed_s"] for r in confirmed) / len(confirmed) if confirmed else 0.0

    wb = Workbook()

    # ================= Sheet 1: 统计概览 =================
    ws = wb.active
    ws.title = "统计概览"
    r = 1
    ws.cell(row=r, column=1, value="一、基本信息").font = header_font
    r += 1
    base_rows = [
        ("数据集", meta.get("dataset", "")),
        ("识别入口", "IntentRecognizer（粗粒度分解 + 路由，仅意图，无槽位）"),
        ("词表", "meeting=会议 / leave=请假 / budget=预算申报"),
        ("LLM", meta.get("model", "")),
        ("确认阈值", f"source=llm 且 confidence >= {CONF_CONFIRM}；未达标需人工确认"),
        ("生成时间", meta.get("generated_at", "")),
    ]
    for label, value in base_rows:
        ws.cell(row=r, column=1, value=label)
        ws.cell(row=r, column=2, value=value)
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="二、组合分布（全部已确认，按 case 计）").font = header_font
    r += 1
    ws.cell(row=r, column=1, value="组合")
    ws.cell(row=r, column=2, value="确认数")
    ws.cell(row=r, column=3, value="未确认")
    ws.cell(row=r, column=4, value="合计")
    ws.cell(row=r, column=5, value="占比")
    ws.cell(row=r, column=6, value="说明")
    for c in range(1, 7):
        ws.cell(row=r, column=c).fill = header_fill
        ws.cell(row=r, column=c).font = header_font
    r += 1
    for key in combo_order:
        counts = combo_counter[key]
        c_conf, c_pend = counts["confirmed"], counts["pending"]
        sub = [UNIT_LABELS.get(u, u) for u in key.split(",") if u]
        if len(sub) == 1:
            desc = "单一流程"
        elif sub:
            desc = "多流程（同段内容跨多业务，按单元展开）"
        else:
            desc = "识别为空"
        ws.cell(row=r, column=1, value=key)
        ws.cell(row=r, column=2, value=c_conf)
        ws.cell(row=r, column=3, value=c_pend)
        ws.cell(row=r, column=4, value=c_conf + c_pend)
        ws.cell(row=r, column=5, value=f"{100.0 * (c_conf + c_pend) / total:.1f}%" if total else "-")
        ws.cell(row=r, column=6, value=f"{'+'.join(sub)} · {desc}")
        r += 1
    ws.cell(row=r, column=1, value="合计")
    ws.cell(row=r, column=2, value=len(confirmed))
    ws.cell(row=r, column=3, value=len(pending))
    ws.cell(row=r, column=4, value=total)
    ws.cell(row=r, column=5, value="100.0%")
    for c in range(1, 6):
        ws.cell(row=r, column=c).font = header_font
    r += 1

    r += 1
    ws.cell(row=r, column=1, value="三、多流程汇总（一段内容跨多个业务，全部展开）").font = header_font
    r += 1
    ws.cell(row=r, column=1, value="组合")
    ws.cell(row=r, column=2, value="case 数")
    ws.cell(row=r, column=3, value="占全部")
    ws.cell(row=r, column=4, value="流程说明")
    for c in range(1, 5):
        ws.cell(row=r, column=c).fill = header_fill
        ws.cell(row=r, column=c).font = header_font
    r += 1
    for key in sorted(multi_combo, key=lambda k: (UNIT_ORDER.get(k.split(",")[0], 9), k)):
        sub = "+".join(UNIT_LABELS.get(u, u) for u in key.split(","))
        ws.cell(row=r, column=1, value=sub)
        ws.cell(row=r, column=2, value=len(multi_combo[key]))
        ws.cell(row=r, column=3, value=f"{100.0 * len(multi_combo[key]) / total:.1f}%" if total else "-")
        ws.cell(row=r, column=4, value=f"同段内容同时进入 {sub} 两条流程")
        r += 1
    ws.cell(row=r, column=1, value="小计")
    ws.cell(row=r, column=2, value=len(multi))
    ws.cell(row=r, column=3, value=f"{100.0 * len(multi) / total:.1f}%" if total else "-")
    ws.cell(row=r, column=4, value="多流程 case 总数")
    for c in range(1, 4):
        ws.cell(row=r, column=c).font = header_font
    r += 1

    r += 1
    ws.cell(row=r, column=1, value="四、单元统计（单元出现次数，多流程 case 计多次）").font = header_font
    r += 1
    ws.cell(row=r, column=1, value="单元")
    ws.cell(row=r, column=2, value="出现次数")
    for c in range(1, 3):
        ws.cell(row=r, column=c).fill = header_fill
        ws.cell(row=r, column=c).font = header_font
    r += 1
    for unit in sorted(unit_counter, key=UNIT_ORDER.get):
        ws.cell(row=r, column=1, value=f"{unit}（{UNIT_LABELS[unit]}）")
        ws.cell(row=r, column=2, value=unit_counter[unit])
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="五、质量统计").font = header_font
    r += 1
    quality = [
        ("LLM 来源（已确认）", n_llm),
        ("规则兜底来源（经人工复核）", n_fallback),
        ("人工复核 case 数", len(manual)),
        ("模型确认 case 数", len(model)),
        ("未确认（待人工）", len(pending)),
        ("平均置信度", round(avg_conf, 3)),
        ("平均识别耗时(s)", round(avg_elapsed, 2)),
    ]
    for label, value in quality:
        ws.cell(row=r, column=1, value=label)
        ws.cell(row=r, column=2, value=value)
        r += 1
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 10
    ws.column_dimensions["D"].width = 10
    ws.column_dimensions["E"].width = 10
    ws.column_dimensions["F"].width = 46

    # ================= Sheet 2: 分类明细 =================
    ws2 = wb.create_sheet("分类明细")
    headers2 = [
        "case_id", "识别序号", "问题(user_query)", "分类", "分类说明",
        "流程数", "是否多流程", "多流程组合", "置信度", "来源", "确认状态",
        "耗时(s)", "mode", "数据集标签(primary_domains)", "tags",
    ]
    ws2.append(headers2)
    for c in range(1, len(headers2) + 1):
        ws2.cell(row=1, column=c).fill = header_fill
        ws2.cell(row=1, column=c).font = header_font
    detail_rows: list[tuple[Any, ...]] = []
    for idx, rec in enumerate(confirmed, start=1):
        is_multi = len(rec["units"]) > 1
        combo = _combo_of(rec["units"]) if is_multi else ""
        state = "人工复核" if rec["confirm"] == "manual" else "模型确认"
        for u in rec["units"]:
            detail_rows.append(
                (
                    rec["case_id"], idx, rec["user_query"], u, UNIT_LABELS[u],
                    len(rec["units"]), "是" if is_multi else "否", combo,
                    rec["confidence"], rec["source"], state,
                    rec["elapsed_s"], rec["mode"] or "", rec["domains"], rec["tags"],
                )
            )
        if not rec["units"]:
            detail_rows.append(
                (
                    rec["case_id"], idx, rec["user_query"], "", "识别为空",
                    0, "否", "", rec["confidence"], rec["source"], state,
                    rec["elapsed_s"], rec["mode"] or "", rec["domains"], rec["tags"],
                )
            )
    # 排序 (case_id, 分类)：同一段跨多业务的话连续展开，逐业务都写出来。
    detail_rows.sort(key=lambda row: (row[0], UNIT_ORDER.get(row[3], 9)))
    for row in detail_rows:
        ws2.append(row)
    # 多流程行黄底高亮（标明）。列 G（index 6）= 是否多流程。
    for row in ws2.iter_rows(min_row=2):
        if row[6].value == "是":
            for cell in row:
                cell.fill = multi_fill
    ws2.auto_filter.ref = ws2.dimensions
    ws2.freeze_panes = "A2"
    widths2 = [16, 8, 60, 10, 12, 8, 11, 18, 8, 10, 11, 9, 12, 26, 30]
    for i, w in enumerate(widths2, start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    for row in ws2.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap

    # ================= Sheet 3: 多流程汇总 =================
    ws3 = wb.create_sheet("多流程汇总")
    headers3 = [
        "组合", "case_id", "问题(user_query)", "流程明细", "流程数",
        "确认状态", "数据集标签(primary_domains)",
    ]
    ws3.append(headers3)
    for c in range(1, len(headers3) + 1):
        ws3.cell(row=1, column=c).fill = header_fill
        ws3.cell(row=1, column=c).font = header_font
    multi_rows: list[tuple[Any, ...]] = []
    for rec in multi:
        combo_cn = "+".join(UNIT_LABELS.get(u, u) for u in rec["units"])
        multi_rows.append(
            (
                combo_cn, rec["case_id"], rec["user_query"],
                " / ".join(f"{u}({UNIT_LABELS[u]})" for u in rec["units"]),
                len(rec["units"]),
                "人工复核" if rec["confirm"] == "manual" else "模型确认",
                rec["domains"],
            )
        )
    multi_rows.sort(key=lambda row: (UNIT_ORDER.get(row[3].split("(")[0], 9), row[1]))
    for row in multi_rows:
        ws3.append(row)
    ws3.auto_filter.ref = ws3.dimensions
    ws3.freeze_panes = "A2"
    widths3 = [22, 16, 60, 40, 8, 11, 26]
    for i, w in enumerate(widths3, start=1):
        ws3.column_dimensions[get_column_letter(i)].width = w
    for row in ws3.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap

    # ================= Sheet 4: 人工确认记录 =================
    ws4 = wb.create_sheet("人工确认记录")
    headers4 = [
        "case_id", "问题(user_query)", "识别单元(初判)", "置信度(初判)", "来源(初判)",
        "初判原因", "复核备注", "确认状态",
    ]
    ws4.append(headers4)
    for c in range(1, len(headers4) + 1):
        ws4.cell(row=1, column=c).fill = header_fill
        ws4.cell(row=1, column=c).font = header_font
    for rec in sorted(manual, key=lambda r: r["case_id"]):
        ws4.append(
            [
                rec["case_id"], rec["user_query"],
                ",".join(UNIT_LABELS.get(u, u) for u in rec["units"]) or "(空)",
                rec["confidence"], rec["source"], rec["reason"],
                rec["review_note"], "人工已确认",
            ]
        )
    ws4.auto_filter.ref = ws4.dimensions
    ws4.freeze_panes = "A2"
    widths4 = [16, 60, 20, 10, 10, 46, 46, 12]
    for i, w in enumerate(widths4, start=1):
        ws4.column_dimensions[get_column_letter(i)].width = w
    for row in ws4.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = wrap

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--split", default="train", choices=["train", "val"])
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--out", type=str, default="")
    ap.add_argument("--results-json", type=str, default="")
    ap.add_argument("--rebuild", action="store_true", help="不触网，从 JSON 重建 Excel")
    ap.add_argument("--migrate-xlsx", type=str, default="", help="从旧 xlsx 回填 JSON（不调 LLM）")
    ap.add_argument("--manual-confirm", type=str, default="", help="人工确认 JSON：case_id -> {units, note}")
    args = ap.parse_args()

    split_dir = REPO_ROOT / "contest" / args.split
    results_json = (
        Path(args.results_json)
        if args.results_json
        else REPO_ROOT / "reports" / "intent_partition" / f"intent_partition_{args.split}.json"
    )
    out_path = (
        Path(args.out)
        if args.out
        else REPO_ROOT / "reports" / "intent_partition" / f"intent_partition_{args.split}.xlsx"
    )

    # 模式 1：旧 xlsx → JSON（一次性迁移）
    if args.migrate_xlsx:
        migrate_xlsx(Path(args.migrate_xlsx), results_json, args.manual_confirm or None)
        return 0

    # 模式 2：--rebuild 不触网重建
    if args.rebuild:
        records = _load_json(results_json)
        apply_manual_confirm(records, args.manual_confirm or None)
        model_id = "gpt-5.4（https://cf.api.fan/v1，openai_compatible）"
        meta = {
            "dataset": f"contest/{args.split}（{len(records)} case）",
            "model": model_id,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        build_excel(records, out_path, meta)
        n_conf = sum(1 for r in records if r.get("confirm"))
        n_man = sum(1 for r in records if r.get("confirm") == "manual")
        print(
            f"[partition] 重建完成（0 次 LLM 调用）: {len(records)} case "
            f"已确认={n_conf} 人工复核={n_man} -> {out_path}",
            flush=True,
        )
        return 0

    # 模式 3：线上识别（LLM）→ JSON + Excel
    cases_dir = split_dir / "cases"
    case_paths = sorted(cases_dir.glob("*.json"))
    if args.limit:
        case_paths = case_paths[: args.limit]
    print(f"[partition] 数据集={args.split} 总数={len(case_paths)} 并发={args.parallel}", flush=True)
    started = time.monotonic()
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for done in pool.map(recognize_one, case_paths):
            records.append(done)
            if len(records) % 20 == 0:
                print(f"[partition] 已识别 {len(records)}/{len(case_paths)}", flush=True)
    elapsed = time.monotonic() - started

    # 默认来源：模型确认；可再用 --manual-confirm 覆盖为人工确认。
    for rec in records:
        if rec["confirm"] is None:
            rec["confirm"] = "model"
    apply_manual_confirm(records, args.manual_confirm or None)
    _save_json(records, results_json)
    meta = {
        "dataset": f"contest/{args.split}（{len(records)} case）",
        "model": "gpt-5.4（https://cf.api.fan/v1，openai_compatible）",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    build_excel(records, out_path, meta)
    n_llm = sum(1 for r in records if r["source"] == "llm")
    n_man = sum(1 for r in records if r["confirm"] == "manual")
    print(
        f"[partition] 完成: {len(records)} case 总耗时 {elapsed:.1f}s "
        f"LLM来源={n_llm} 人工复核={n_man} -> {out_path} + {results_json}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
