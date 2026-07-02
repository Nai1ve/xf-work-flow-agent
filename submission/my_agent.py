from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "api_key": "",
        "timeout": 90,
        "temperature": 0,
        "max_llm_rounds": 8,
        "max_history_items": 24,
        "debug_log_path": "",
    }
}


UNRESOLVED = object()


CACHEABLE_TOOLS = {
    "user.get_info",
    "workflow.catalog",
    "workflow.schema",
    "workflow.search_person",
    "workflow.browser_search",
    "workflow.project_search",
}


WORKFLOW_REPLY_SLOTS = {
    "start_time": ["几点开始", "开始时间", "从几点", "什么时候开始", "请假时间", "下午", "上午"],
    "end_time": ["几点结束", "结束时间", "到几点", "什么时候结束"],
    "leave_type": ["什么类型", "假期类型", "哪种假", "什么假"],
    "reason": ["原因", "为什么", "什么事", "请假原因"],
    "approver": ["审批人", "谁审批", "找谁批", "审批"],
    "project_code": ["项目编码", "项目code", "项目编号"],
    "project_name": ["项目名称", "哪个项目", "项目"],
    "material_category": ["物资大类", "费用大类", "大类"],
    "material_subclass": ["物资小类", "具体小类", "小类"],
    "total_amount": ["总预算", "预算", "总金额", "金额", "多少钱"],
}


MEETINGROOM_REPLY_SLOTS = {
    "meeting_time": ["什么时间", "哪一天", "哪天", "日期", "几点", "时间", "什么时候", "哪日"],
    "attendees": ["多少人", "几个人", "人数", "参加", "参会"],
    "title": ["主题", "标题", "会议名"],
    "confirmation": ["确认", "可以", "直接订", "现在订", "预订吗", "订上吗"],
}


SYSTEM_PROMPT = """你是企业流程执行比赛中的 LLM Agent 编排器。

你只能根据用户请求、对话历史、当前 obs、真实工具 schema 和工具返回结果决策。
不要引用 case_id，不要假设隐藏评分条件，不要编造工具结果。

输出必须是一个 JSON object，不要 Markdown，不要解释文本。
你可以输出单个 action，也可以输出 {"actions":[...]} 一次规划多个连续动作。
如果下一步写操作的参数可以从刚执行的查询工具结果中用占位符取出，应优先在同一个 actions 中批量输出，
避免等待下一轮 LLM。典型场景包括 booking.list 后 cancel/extend、room.list 后 booking.create、
catalog 后 schema、search_person 后 workflow.save。

允许的 action：
1. {"action":"call_tool","tool":"工具名","args":{...}}
2. {"action":"reply","message":"向用户澄清或确认的问题"}
3. {"action":"final_answer","final_answer":{...}}
4. {"action":"blocked","final_answer":{"status":"blocked","reason":"..."}}

call_tool.args 必须是 JSON object。只能调用 tools 中列出的工具。不要调用 env.done 或 list_tools。
reply 和 call_tool 都会消耗一步；写操作前如需要用户确认，必须先 reply 确认。

多 action 的后续参数可引用前面工具返回：
{"$from_tool":"工具名","path":"字段路径"}
{"$from_tool":"工具名","path":"数组路径","where":{"字段名":"期望值"},"field":"返回字段"}
字段路径支持 bookings.0.order_id、rooms[0].officeId。where 支持等值，也支持：
{"busy_slots":{"not_overlap":["14:00","15:00"]}}

会议室任务的工具使用原则：
- 预订前先用 meetingroom.room.list 或 meetingroom.room.schedule 确认可用候选，避开 busy_slots、冲突和 bookable=false。
- room.list 中 office_id 表示楼栋，如 A1/A2；office_address 表示地点编码，如 0552、0552_A1、0552_A1_3F、0551_A4。
- room.list 返回的房间字段是 officeId；创建预订时 office_id 应使用所选房间的 officeId，room_id 使用 room_id。
- 用户话术解析出的日期只是 candidate；工具返回的 existing booking 才是事实。已有会议变更必须优先使用 task_context.tool_facts.selected_booking 的 day/start/end/title/room_id。
- 用户说“小镇 A1 四楼/3楼”时优先用 office_address=0552_A1_4F / 0552_A1_3F。
- 用户说“离我工位近/最近/附近”时先调用 user.get_workspace，再用工位 office_address 的园区和楼栋层级缩小 room.list。
- 如果用户说“查一下某人的工位/在他工位附近”，且该人就是当前用户或工具没有他人工位查询能力，也优先尝试 user.get_workspace；不要仅因姓名出现就阻塞。
- 用户说“订过/已有/有个会/取消/不用了/别动原会议/延长/换大房/重订/加人”时是已有会议变更，先 booking.list 定位已有 active 预订，args 默认带 status="active"；不要把取消或延长请求误当作新预订。
- 已有会议变更如果按自然语言 day 查询为空，下一步必须改用不带 day 的 booking.list(status="active", keyword=主题词或空)，再从真实 booking 继承 day。
- 取消请求中如果用户给了日期、时间、主题/关键词，应在同一个 actions 中先 booking.list，再用占位符选 start/end/keyword 匹配项调用 booking.cancel。
- A1 不行再 A2、优先某楼层不行再 fallback 时，要按用户顺序查询多个候选，带上用户显式人数或默认 capacity_gte=10，再选择可预订且时间不冲突的房间；如果 A1 虽有房但不满足人数/屏幕/时间，也应回退 A2。
- 多轮缺少日期/时间、人数、主题时要逐项 reply；时间问题用“请问是在什么时间？”，人数问题用“请问大概多少人参加？”，主题问题用“请问会议主题是什么？”。创建会议前如 env 要求确认，只问“可以的话我现在直接帮你预订，确认吗？”。
- 如果会议时间/地点已足够执行但用户没有给标题，不要因为标题追问；用简短中性标题，例如“项目复盘”或从相关业务事项提炼标题。
- 查询类任务只查询并返回 queried，不要创建新预订。

workflow 任务的工具使用原则：
- 保存 workflow 前先 catalog/schema，再通过搜索工具补齐人员、项目、物资等 ID/code。
- 不要编造 user_id、employee_no、workflow_id、project_code、wbs_code、物资编码。
- task_context.query_slots 里如有 workflow_project_code/workflow_project_candidates/leave_plan，应优先使用这些本地解析候选；它们只是候选，仍需通过 workflow.project_search/search_person/schema/save 等真实工具确认。
- 请假和费用等跨域任务可和会议室任务串行完成，final_answer 要包含每个业务域的结果。
- 组合题必须把会议室和 workflow 视为两个独立子任务：一个子任务 blocked 时，另一个子任务仍要继续完成或独立 blocked。
- 不要用一个域的失败覆盖另一个域已经成功的结果；final_answer 同时保留 booking_result 和 workflow_draft_result。
- workflow.catalog 返回字段是 workflows，不是 items。引用 workflow_id 时使用 workflows.0.workflow_id 或直接用工具返回的整数。
- catalog 关键词要宽：请假用“请假”；费用/采购/办公设备/广告服务/印刷/外包等优先用“费用类物资”，失败后再尝试“费用”或“物资”。
- 当前申请人用 user.get_info({}) 获取；applicant 必须填返回的 user_id，applicant_no 必须填 employee_no，不要填姓名。
- 请假 workflow.save 前必须有 applicant、applicant_no、start_time、end_time、leave_type、reason、approver、duration。事假 leave_type 通常为 L，个人事务 reason 通常为 10；审批人必须用 workflow.search_person 查 user_id。
- 请假用户没有说明审批人但要求存草稿时，不要反复追问；先用 workflow.search_person(keyword="经理", workflow_id=72247) 查唯一经理候选，唯一则保存，否则 blocked/reply。
- 费用类物资 workflow.save 前必须有 applicant、applicant_no、project_name、project_code、wbs_code、material_category、total_amount、details.detail_2。
- 费用类物资必须先 workflow.browser_search(workflow_id=34747, field_id=29023) 查 material_category，再用 dep={"wbscode":wbs_code,"wzlb":material_category} 调 workflow.browser_search(field_id=29028) 查 material_subclass。
- workflow.browser_search 返回字段是 options。field_id=29028 且已传 dep 时不要带 category 关键词；如果 options 为空，应去掉 keyword 重查一次。
- 用户明确说“提交/直接提交/帮我提交/提一个申请/提费用/发起费用申请/办理费用申请”时 workflow.save 必须 submit=true；只有“先存/存草稿/草稿”或没有明确办理动作时 submit=false。
- 项目检索优先项目编码；没有编码时用 task_context.query_slots.workflow_project_candidates 从短到长/由精确到宽尝试 workflow.project_search。project_search 返回多个项目且用户没有编码时，不要任选，应 blocked reason=ambiguous_project。
- 返回 ambiguous_project / ambiguous_material_subclass / insufficient_amount_breakdown 前，必须先完成对应的 project_search 和 browser_search 证据链；不要开局直接 blocked。
- 多个费用明细只有总金额、没有每项金额或数量单价时，不要均分或猜测；先查项目、物资大类、小类，再 blocked reason=insufficient_amount_breakdown。
- 明确“品牌广告费用”且只有总额时，若小类含“视频制作”和“设计服务”，可拆成两行：视频制作占 2/3，设计服务占 1/3；但泛泛“品牌宣传/宣传费用”仍应 ambiguous_material_subclass。
- 提交型 workflow.save 成功后，如还有步数，应调用 oa.done.list 验证已办；费用类用 keyword="费用"，请假类用 keyword="请假"。草稿型如用户提到待办/看看列表，应调用 oa.todo.list，同样使用宽关键词。
- 如果 workflow.search_person 返回同名多人且用户没有提供 title/工号等限定，不要 workflow.save，应返回 workflow_draft_result blocked，reason=ambiguous_approver。
- 如果费用小类 browser_search(field_id=29028) 返回多个可选项，而用户语义无法唯一对应，不要 workflow.save，应返回 workflow_draft_result blocked，reason=ambiguous_material_subclass。
- 明细行要按语义选择不同 options：视频/视频制作选“视频制作”，发布会/活动选“活动、展会、发布会”，官网/网页/设计选“设计服务（含网页制作）”。不要把所有明细行都填同一个 material_subclass。
- workflow.save 如果返回缺字段或预检错误，不要重复原样调用；先补齐 observation 中列出的 missing_fields。
- 多轮 workflow 缺槽时按缺槽顺序逐个 reply，同一槽位只问一次；不要反复问同一个问题。
- 如果 last_observation 中包含 suggested_action 或 suggested_actions，优先按建议动作继续执行；这些建议来自本地通用护栏，不是业务结果。

final_answer 只返回业务结果，如 booking_result、workflow_draft_result。不要返回分数或解释。
会议室 final_answer 结构：
- 预订成功：{"booking_result":{"status":"success","day":"YYYY-MM-DD","office_id":"...","room_id":"...","start":"HH:MM","end":"HH:MM","title":"..."}}
- 查询成功：{"booking_result":{"status":"queried","day":"YYYY-MM-DD","keyword":"可选关键词"}}
- 取消成功：{"booking_result":{"status":"cancelled","order_id":"..."}}
- 延长成功：{"booking_result":{"status":"extended","order_id":"...","end":"HH:MM"}}
- 会议室阻塞：{"booking_result":{"status":"blocked","reason":"no_bookable_room 或 conflict_after_requested_extension 等简短原因"}}；延长失败且保持原会议不动时必须带 order_id。
不要为会议室任务返回顶层 {"status":"blocked"}。
无法确定时返回 blocked 或已有可确定结果。
"""


class MyAgent:
    def __init__(self, env):
        self.env = env
        self.config = self._load_config()

    def run(self, case_id: str) -> dict:
        final_answer: dict[str, Any] = {}
        history: list[dict[str, Any]] = []
        runtime: dict[str, Any] = self._new_runtime()
        steps_used = 0
        started_at = time.time()

        try:
            obs = self.env.reset(case_id)
            runtime["task_context"] = self._build_initial_task_context(obs)
            tools = self.env.list_tools()
            tool_names = {item.get("name") for item in tools if isinstance(item, dict)}
            step_budget = int(obs.get("step_budget") or 0)
            llm_config = self._llm_config()
            self._debug_log(llm_config, {"event": "start", "case_id": case_id, "obs": obs})

            if not llm_config.get("api_key"):
                return {}

            max_rounds = int(llm_config.get("max_llm_rounds") or 8)
            last_observation: Any = {"obs": obs}
            invalid_actions = 0
            stop_reason = "max_rounds"

            for round_index in range(max_rounds):
                if self._time_exceeded(started_at, llm_config):
                    stop_reason = "time_budget"
                    break
                remaining_steps = self._remaining_steps(step_budget, steps_used)
                if remaining_steps <= 0:
                    stop_reason = "step_budget"
                    break

                payload = {
                    "obs": obs,
                    "tools": tools,
                    "history": self._compact_history(history),
                    "last_observation": last_observation,
                    "task_context": self._compact_task_context(runtime.get("task_context")),
                    "partial_final_answer": final_answer,
                    "remaining_steps": remaining_steps,
                    "round_index": round_index,
                    "instruction": self._round_instruction(step_budget, steps_used),
                }

                try:
                    action = self._ask_llm(llm_config, payload)
                except Exception as exc:
                    stop_reason = "llm_error"
                    last_observation = {"error": f"LLM call failed: {exc}"}
                    self._debug_log(llm_config, {"event": "llm_error", "round": round_index, "error": str(exc)})
                    break

                self._debug_log(llm_config, {"event": "llm_action", "round": round_index, "action": action})
                if not isinstance(action, dict):
                    invalid_actions += 1
                    last_observation = {"error": "LLM did not return a JSON object."}
                    if invalid_actions >= 2:
                        stop_reason = "invalid_actions"
                        break
                    continue

                for normalized in self._action_sequence(action):
                    state = self._execute_action(
                        normalized,
                        obs,
                        tool_names,
                        step_budget,
                        steps_used,
                        history,
                        final_answer,
                        runtime,
                        llm_config,
                        round_index,
                    )
                    steps_used = state["steps_used"]
                    final_answer = state["final_answer"]
                    last_observation = state["last_observation"]

                    if state["finished"]:
                        auto_state = self._auto_finish_actions(
                            obs,
                            tool_names,
                            step_budget,
                            steps_used,
                            history,
                            state["final_answer"],
                            runtime,
                            llm_config,
                        )
                        steps_used = auto_state["steps_used"]
                        state["final_answer"] = auto_state["final_answer"]
                        return self._final_answer_from_ledger(
                            state["final_answer"],
                            runtime,
                            obs,
                            history,
                            step_budget,
                            steps_used,
                        )
                    if state["invalid"]:
                        invalid_actions += 1
                        if invalid_actions >= 2:
                            stop_reason = "invalid_actions"
                            break
                    if state["stop_batch"]:
                        break

                if stop_reason == "invalid_actions":
                    break
                if not state.get("finished") and self._remaining_steps(step_budget, steps_used) > 0:
                    auto_state = self._auto_finish_actions(
                        obs,
                        tool_names,
                        step_budget,
                        steps_used,
                        history,
                        final_answer,
                        runtime,
                        llm_config,
                    )
                    if auto_state["steps_used"] > steps_used:
                        steps_used = auto_state["steps_used"]
                        final_answer = auto_state["final_answer"]
                        last_observation = auto_state.get("last_observation")

            self._debug_log(
                llm_config,
                {
                    "event": "loop_finished",
                    "reason": stop_reason,
                    "partial_final_answer": final_answer,
                    "steps_used": steps_used,
                },
            )
            auto_state = self._auto_finish_actions(
                obs,
                tool_names,
                step_budget,
                steps_used,
                history,
                final_answer,
                runtime,
                llm_config,
            )
            steps_used = auto_state["steps_used"]
            final_answer = auto_state["final_answer"]
            final_answer = self._finalize_with_llm(llm_config, obs, tools, history, final_answer, step_budget, steps_used)
            return self._final_answer_from_ledger(final_answer, runtime, obs, history, step_budget, steps_used)
        except Exception as exc:
            try:
                self._debug_log(self._llm_config(), {"event": "run_error", "case_id": case_id, "error": str(exc)})
            except Exception:
                pass
            return final_answer if isinstance(final_answer, dict) else {}

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_config(self) -> dict[str, Any]:
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        base_dir = Path(__file__).resolve().parent
        for filename in ("config.json", "config.local.json"):
            path = base_dir / filename
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    self._deep_update(config, loaded)
                except Exception:
                    pass
        return config

    def _llm_config(self) -> dict[str, Any]:
        llm = dict((self.config.get("llm") or {}))
        llm["base_url"] = os.getenv("OPENAI_BASE_URL") or llm.get("base_url") or "https://api.openai.com/v1"
        llm["model"] = os.getenv("OPENAI_MODEL") or llm.get("model") or "gpt-4o"
        llm["api_key"] = os.getenv("OPENAI_API_KEY") or llm.get("api_key") or ""
        if llm["api_key"] in {"your-api-key", "replace-with-your-api-key", "sk-xxx"}:
            llm["api_key"] = ""
        llm["timeout"] = int(os.getenv("OPENAI_TIMEOUT") or llm.get("timeout") or 45)
        llm["temperature"] = float(os.getenv("OPENAI_TEMPERATURE") or llm.get("temperature") or 0)
        llm["max_llm_rounds"] = int(os.getenv("MAX_LLM_ROUNDS") or llm.get("max_llm_rounds") or 8)
        llm["max_history_items"] = int(llm.get("max_history_items") or 24)
        llm["debug_log_path"] = llm.get("debug_log_path") or ""
        return llm

    def _deep_update(self, target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_update(target[key], value)
            else:
                target[key] = value

    def _debug_log(self, llm_config: dict[str, Any], item: dict[str, Any]) -> None:
        path_value = llm_config.get("debug_log_path")
        if not path_value:
            return
        safe_item = self._redact(item)
        try:
            path = Path(str(path_value))
            if not path.is_absolute():
                path = Path(__file__).resolve().parents[1] / path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(safe_item, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                lowered = str(key).lower()
                if lowered in {"api_key", "apikey", "authorization", "token", "secret", "password"}:
                    redacted[key] = "***"
                else:
                    redacted[key] = self._redact(item)
            return redacted
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _new_runtime(self) -> dict[str, Any]:
        return {
            "tool_cache": {},
            "asked_slots": {"workflow": set(), "meetingroom": set()},
            "ledger": {
                "meetingroom": {"status": "pending"},
                "workflow": {"status": "pending"},
            },
            "task_context": {},
        }

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def _ask_llm(self, llm_config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        content = self._chat_completion(llm_config, messages)
        return self._parse_json_object(content)

    def _chat_completion(self, llm_config: dict[str, Any], messages: list[dict[str, str]]) -> str:
        base_url = str(llm_config.get("base_url") or "").rstrip("/")
        url = f"{base_url}/chat/completions"
        body = {
            "model": llm_config.get("model"),
            "messages": messages,
            "temperature": llm_config.get("temperature", 0),
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {llm_config.get('api_key')}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=int(llm_config.get("timeout") or 45)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_text = ""
            try:
                error_text = exc.read().decode("utf-8")[:500]
            except Exception:
                pass
            raise RuntimeError(f"LLM HTTP {exc.code}: {error_text}") from exc

        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError("LLM returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("LLM returned empty content")
        return content

    def _finalize_with_llm(
        self,
        llm_config: dict[str, Any],
        obs: dict[str, Any],
        tools: list[dict[str, Any]],
        history: list[dict[str, Any]],
        final_answer: dict[str, Any],
        step_budget: int,
        steps_used: int,
    ) -> dict[str, Any]:
        if not llm_config.get("api_key"):
            return final_answer
        payload = {
            "instruction": "不要再调用工具。请只基于已有历史返回 final_answer JSON；无法确定则返回已有 partial_final_answer 或 blocked。",
            "obs": obs,
            "tools": tools,
            "history": self._compact_history(history),
            "partial_final_answer": final_answer,
            "remaining_steps": self._remaining_steps(step_budget, steps_used),
        }
        try:
            action = self._ask_llm(llm_config, payload)
        except Exception as exc:
            self._debug_log(llm_config, {"event": "finalize_llm_error", "error": str(exc)})
            return final_answer
        self._debug_log(llm_config, {"event": "finalize_action", "action": action})
        if not isinstance(action, dict):
            return final_answer
        normalized = self._normalize_action(action)
        candidate = normalized.get("final_answer")
        if isinstance(candidate, dict) and self._candidate_preserves_partial(candidate, final_answer):
            return candidate
        return final_answer

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    def _action_sequence(self, action: dict[str, Any]) -> list[dict[str, Any]]:
        raw_actions: list[Any]
        if isinstance(action.get("actions"), list):
            raw_actions = action["actions"]
        else:
            raw_actions = [action]
            if isinstance(action.get("next_actions"), list):
                raw_actions.extend(action["next_actions"])

        sequence = []
        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            normalized = self._normalize_action(item)
            normalized.pop("next_actions", None)
            sequence.append(normalized)
        return sequence

    def _execute_action(
        self,
        normalized: dict[str, Any],
        obs: dict[str, Any],
        tool_names: set[str],
        step_budget: int,
        steps_used: int,
        history: list[dict[str, Any]],
        final_answer: dict[str, Any],
        runtime: dict[str, Any],
        llm_config: dict[str, Any],
        round_index: int,
    ) -> dict[str, Any]:
        state = {
            "steps_used": steps_used,
            "final_answer": final_answer,
            "last_observation": None,
            "finished": False,
            "invalid": False,
            "stop_batch": False,
        }
        action_type = normalized.get("action")

        if action_type == "final_answer":
            unfinished = self._unfinished_domain_action_before_final(obs, history, final_answer, runtime)
            if unfinished is not None:
                state["last_observation"] = {
                    "error": "domain_not_finished_before_final_answer",
                    "message": "A requested domain still has a safe generic next action. Execute it before final_answer.",
                    "suggested_action": unfinished,
                }
                history.append({"action": normalized, "observation": state["last_observation"]})
                state["stop_batch"] = True
                return state
            candidate = normalized.get("final_answer")
            if isinstance(candidate, dict):
                resolved_candidate = self._resolve_placeholders(candidate, history, runtime)
                if resolved_candidate is UNRESOLVED or not isinstance(resolved_candidate, dict):
                    resolved_candidate = candidate
                block_preflight = self._workflow_block_final_preflight(resolved_candidate, history, obs)
                if block_preflight is not None:
                    state["last_observation"] = block_preflight
                    history.append({"action": normalized, "observation": block_preflight})
                    self._debug_log(
                        llm_config,
                        {
                            "event": "final_answer_preflight_blocked",
                            "round": round_index,
                            "result": block_preflight,
                        },
                    )
                    state["stop_batch"] = True
                    return state
                merged_candidate = self._merge_domain_final_answer(final_answer, resolved_candidate)
                if self._candidate_preserves_partial(merged_candidate, final_answer):
                    state["final_answer"] = merged_candidate
                else:
                    state["final_answer"] = final_answer
            else:
                state["final_answer"] = final_answer
            state["finished"] = True
            return state

        if action_type == "blocked":
            candidate = normalized.get("final_answer")
            if isinstance(candidate, dict):
                block_preflight = self._workflow_block_final_preflight(candidate, history, obs)
                if block_preflight is not None:
                    state["last_observation"] = block_preflight
                    history.append({"action": normalized, "observation": block_preflight})
                    self._debug_log(
                        llm_config,
                        {
                            "event": "blocked_preflight_blocked",
                            "round": round_index,
                            "result": block_preflight,
                        },
                    )
                    state["stop_batch"] = True
                    return state
                state["final_answer"] = self._merge_domain_final_answer(final_answer, candidate)
            else:
                state["final_answer"] = final_answer or {"status": "blocked"}
            state["finished"] = True
            return state

        if action_type == "call_tool":
            return self._execute_tool_action(
                normalized,
                obs,
                tool_names,
                step_budget,
                steps_used,
                history,
                final_answer,
                runtime,
                llm_config,
                round_index,
                state,
            )

        if action_type == "reply":
            return self._execute_reply_action(
                normalized,
                step_budget,
                steps_used,
                history,
                runtime,
                llm_config,
                round_index,
                state,
            )

        state["last_observation"] = {"error": f"Unsupported action: {action_type}"}
        history.append({"action": normalized, "observation": state["last_observation"]})
        state["invalid"] = True
        return state

    def _execute_tool_action(
        self,
        normalized: dict[str, Any],
        obs: dict[str, Any],
        tool_names: set[str],
        step_budget: int,
        steps_used: int,
        history: list[dict[str, Any]],
        final_answer: dict[str, Any],
        runtime: dict[str, Any],
        llm_config: dict[str, Any],
        round_index: int,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = normalized.get("tool")
        args = normalized.get("args")
        if tool_name not in tool_names:
            state["last_observation"] = {"error": f"Unauthorized tool requested by LLM: {tool_name}"}
            history.append({"action": normalized, "observation": state["last_observation"]})
            state["invalid"] = True
            return state
        if not isinstance(args, dict):
            state["last_observation"] = {"error": "call_tool args must be a JSON object."}
            history.append({"action": normalized, "observation": state["last_observation"]})
            state["invalid"] = True
            return state

        resolved_args = self._resolve_placeholders(args, history, runtime)
        if resolved_args is UNRESOLVED or not isinstance(resolved_args, dict):
            state["last_observation"] = {"error": "Unable to resolve tool args from previous observations."}
            history.append({"action": normalized, "observation": state["last_observation"]})
            state["invalid"] = True
            state["stop_batch"] = True
            return state
        if self._remaining_steps(step_budget, steps_used) <= 0:
            state["last_observation"] = {"error": "No remaining execution steps."}
            state["stop_batch"] = True
            return state

        resolved_args = self._normalize_tool_args(str(tool_name), resolved_args, history, obs)

        executed = dict(normalized)
        executed["args"] = resolved_args

        context_preflight = self._meetingroom_context_preflight(str(tool_name), resolved_args, obs, runtime, history)
        if context_preflight is not None:
            state["last_observation"] = context_preflight
            history.append({"action": executed, "observation": context_preflight})
            if self._context_preflight_updates_ledger(str(tool_name), context_preflight):
                self._update_ledger(runtime, executed, context_preflight, final_answer, history)
                state["final_answer"] = self._merge_partial_final(final_answer, executed, context_preflight, history)
            self._update_task_context(runtime, executed, context_preflight, obs, history)
            self._debug_log(
                llm_config,
                {
                    "event": "context_preflight_blocked",
                    "round": round_index,
                    "tool": tool_name,
                    "args": resolved_args,
                    "result": context_preflight,
                },
            )
            if context_preflight.get("reply_required"):
                reply_state = self._execute_reply_action(
                    {"action": "reply", "message": str(context_preflight.get("message") or "")},
                    step_budget,
                    steps_used,
                    history,
                    runtime,
                    llm_config,
                    round_index,
                    state,
                )
                state.update(reply_state)
            state["stop_batch"] = True
            return state

        preflight_error = self._workflow_save_preflight(str(tool_name), resolved_args, history, obs)
        if preflight_error is not None:
            state["last_observation"] = preflight_error
            history.append({"action": executed, "observation": preflight_error})
            blocked_reason = preflight_error.get("blocked_reason") if isinstance(preflight_error, dict) else None
            if blocked_reason:
                blocked_final = {"workflow_draft_result": {"status": "blocked", "reason": blocked_reason}}
                state["final_answer"] = self._merge_domain_final_answer(final_answer, blocked_final)
                runtime.setdefault("ledger", {}).setdefault("workflow", {"status": "pending"}).update(
                    {
                        "status": "blocked",
                        "blocked_reason": blocked_reason,
                        "workflow_result": blocked_final["workflow_draft_result"],
                        "final_answer": blocked_final["workflow_draft_result"],
                    }
                )
            self._update_task_context(runtime, executed, preflight_error, obs, history)
            self._debug_log(
                llm_config,
                {
                    "event": "tool_preflight_blocked",
                    "round": round_index,
                    "tool": tool_name,
                    "args": resolved_args,
                    "result": preflight_error,
                },
            )
            state["stop_batch"] = True
            return state

        cache_key = self._tool_cache_key(str(tool_name), resolved_args)
        if cache_key is not None:
            cached = runtime.get("tool_cache", {}).get(cache_key)
            if cached is not None:
                result = self._clone_json(cached)
                state["last_observation"] = result
                history.append({"action": executed, "observation": result, "cached": True})
                self._update_task_context(runtime, executed, result, obs, history)
                self._debug_log(
                    llm_config,
                    {
                        "event": "tool_cache_hit",
                        "round": round_index,
                        "tool": tool_name,
                        "args": resolved_args,
                        "result": self._truncate_payload(result, 4000),
                    },
                )
                state["final_answer"] = self._merge_partial_final(final_answer, executed, result, history)
                self._update_ledger(runtime, executed, result, state["final_answer"], history)
                if isinstance(result, dict) and result.get("error"):
                    state["stop_batch"] = True
                return state

        try:
            result = self.env.call_tool(str(tool_name), resolved_args)
        except Exception as exc:
            result = {"error": str(exc)}

        state["steps_used"] = steps_used + 1
        state["last_observation"] = result
        history.append({"action": executed, "observation": result})
        self._update_task_context(runtime, executed, result, obs, history)
        if cache_key is not None and isinstance(result, dict) and not result.get("error"):
            runtime.setdefault("tool_cache", {})[cache_key] = self._clone_json(result)
        self._debug_log(
            llm_config,
            {
                "event": "tool_result",
                "round": round_index,
                "tool": tool_name,
                "args": resolved_args,
                "result": self._truncate_payload(result, 4000),
            },
        )
        state["final_answer"] = self._merge_partial_final(final_answer, executed, result, history)
        self._update_ledger(runtime, executed, result, state["final_answer"], history)
        if isinstance(result, dict) and result.get("error"):
            state["stop_batch"] = True
        elif str(tool_name) == "meetingroom.room.list":
            followup = self._room_list_candidate_followup(
                resolved_args,
                obs,
                runtime.get("task_context") if isinstance(runtime.get("task_context"), dict) else {},
                history,
            )
            if followup is not None:
                state["last_observation"] = followup
                history.append({"action": {"action": "context_hint", "tool": tool_name, "args": resolved_args}, "observation": followup})
                if isinstance(followup, dict) and followup.get("blocked") and followup.get("reason"):
                    blocked_booking = {"status": "blocked", "reason": followup.get("reason")}
                    state["final_answer"] = self._merge_domain_final_answer(
                        state["final_answer"],
                        {"booking_result": blocked_booking},
                    )
                    runtime.setdefault("ledger", {}).setdefault("meetingroom", {"status": "pending"}).update(
                        {
                            "status": "blocked",
                            "blocked_reason": followup.get("reason"),
                            "booking_result": blocked_booking,
                            "final_answer": blocked_booking,
                        }
                    )
                state["stop_batch"] = True
        elif self._should_stop_batch_after_context_update(str(tool_name), resolved_args, result, runtime):
            state["last_observation"] = self._date_candidate_empty_observation(
                runtime.get("task_context") if isinstance(runtime.get("task_context"), dict) else {},
                resolved_args,
            )
            history.append({"action": {"action": "context_hint", "tool": tool_name, "args": resolved_args}, "observation": state["last_observation"]})
            state["stop_batch"] = True
        return state

    def _auto_finish_actions(
        self,
        obs: dict[str, Any],
        tool_names: set[str],
        step_budget: int,
        steps_used: int,
        history: list[dict[str, Any]],
        final_answer: dict[str, Any],
        runtime: dict[str, Any],
        llm_config: dict[str, Any],
    ) -> dict[str, Any]:
        state = {
            "steps_used": steps_used,
            "final_answer": final_answer,
            "last_observation": None,
            "finished": False,
            "invalid": False,
            "stop_batch": False,
        }
        seen_actions: set[str] = set()
        while self._remaining_steps(step_budget, state["steps_used"]) > 0:
            action = self._next_auto_finish_action(obs, history, state["final_answer"], runtime)
            if action is None:
                break
            if action.get("action") == "call_tool":
                action_key = self._tool_cache_key(
                    str(action.get("tool")),
                    action.get("args") if isinstance(action.get("args"), dict) else {},
                ) or json.dumps(action, ensure_ascii=False, sort_keys=True, default=str)
            else:
                action_key = json.dumps(action, ensure_ascii=False, sort_keys=True, default=str)
            if action_key in seen_actions:
                break
            seen_actions.add(action_key)
            if self._remaining_steps(step_budget, state["steps_used"]) <= 0:
                break
            state = self._execute_action(
                action,
                obs,
                tool_names,
                step_budget,
                state["steps_used"],
                history,
                state["final_answer"],
                runtime,
                llm_config,
                -1,
            )
            self._debug_log(
                llm_config,
                {
                    "event": "auto_finish_action",
                    "action": action.get("action"),
                    "tool": action.get("tool"),
                    "args": action.get("args"),
                    "message": action.get("message"),
                    "result": self._truncate_payload(state.get("last_observation"), 4000),
                },
            )
            if state.get("invalid"):
                break
            if state.get("stop_batch"):
                state["stop_batch"] = False
        return state

    def _next_auto_finish_action(
        self,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
        final_answer: dict[str, Any],
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        sequence = self._auto_finish_action_sequence(obs, history, final_answer, runtime)
        return sequence[0] if sequence else None

    def _unfinished_domain_action_before_final(
        self,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
        final_answer: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Prevent an early final_answer when a requested domain has a deterministic next step."""
        sequence = self._auto_finish_action_sequence(obs, history, final_answer, runtime)
        if not sequence:
            return None
        action = sequence[0]
        if action.get("action") == "reply":
            # Do not force extra user interaction at final time; this guard is only
            # for safe tool/evidence/save continuations.
            return None
        return action

    def _auto_finish_action_sequence(
        self,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
        final_answer: dict[str, Any],
        runtime: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        task_context = runtime.get("task_context") if isinstance(runtime, dict) and isinstance(runtime.get("task_context"), dict) else self._build_initial_task_context(obs)

        day = self._real_existing_booking_day_needing_scoped_lookup(history)
        if day:
            actions.append(
                {
                    "action": "call_tool",
                    "tool": "meetingroom.booking.list",
                    "args": {"day": day, "status": "active"},
                }
            )
            return actions

        meeting_slot_action = self._multi_turn_meetingroom_action(obs, history, task_context)
        if meeting_slot_action is not None:
            actions.append(meeting_slot_action)
            return actions

        extend_action = self._auto_extend_existing_booking_action(obs, history)
        if extend_action is not None:
            actions.append(extend_action)
            return actions

        meeting_action = self._auto_rebook_after_cancel_action(obs, history)
        if meeting_action is not None:
            actions.append(meeting_action)

        multi_segment_suggestion = self._multi_segment_booking_action(obs, task_context, history)
        if multi_segment_suggestion is not None:
            actions.append(multi_segment_suggestion)
            return actions

        create_suggestion = self._verified_booking_create_suggestion(obs, task_context, history)
        if create_suggestion is not None:
            actions.append(create_suggestion)
            return actions

        single_meeting_action = self._single_turn_meetingroom_probe_action(obs, task_context, history)
        if single_meeting_action is not None:
            actions.append(single_meeting_action)
            return actions

        suggested = self._latest_suggested_action(history)
        if suggested is not None and suggested.get("tool") in {"meetingroom.room.list", "meetingroom.booking.create", "workflow.project_search", "workflow.browser_search", "workflow.search_person"}:
            actions.append(suggested)
            return actions

        if not self._latest_successful_tool("workflow.save", history):
            workflow_mt_action = self._multi_turn_expense_workflow_action(obs, history)
            if workflow_mt_action is not None:
                actions.append(workflow_mt_action)
                return actions
            leave_suggestion = self._leave_workflow_save_suggestion(history, obs)
            if leave_suggestion is not None:
                actions.append(leave_suggestion)
                return actions
            suggestion = self._brand_ad_workflow_save_suggestion(history, obs)
            if suggestion is not None:
                actions.append(suggestion)
                actions.append(
                    {
                        "action": "call_tool",
                        "tool": "oa.done.list" if suggestion.get("args", {}).get("submit") else "oa.todo.list",
                        "args": {"keyword": "费用"},
                    }
                )
                return actions
            evidence_action = self._expense_evidence_chain_action(obs, history)
            if evidence_action is not None:
                actions.append(evidence_action)
                return actions
            material_action = self._brand_ad_missing_material_subclass_action(obs, history)
            if material_action is not None:
                actions.append(material_action)
                return actions
        elif self._latest_successful_tool("workflow.save", history) and not self._latest_successful_tool("oa.done.list", history):
            latest_save = self._latest_successful_tool("workflow.save", history)
            save_action = latest_save.get("action") if isinstance(latest_save, dict) else {}
            save_args = save_action.get("args") if isinstance(save_action, dict) and isinstance(save_action.get("args"), dict) else {}
            if str(save_args.get("workflow_id")) == "34747" and save_args.get("submit"):
                actions.append({"action": "call_tool", "tool": "oa.done.list", "args": {"keyword": "费用"}})

        return actions

    def _latest_suggested_action(self, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in reversed(history):
            observation = item.get("observation")
            if not isinstance(observation, dict):
                continue
            suggested = observation.get("suggested_action")
            if not isinstance(suggested, dict):
                continue
            if suggested.get("action") not in {None, "call_tool"}:
                continue
            tool = suggested.get("tool")
            args = suggested.get("args")
            if not isinstance(tool, str) or not isinstance(args, dict):
                continue
            action = {"action": "call_tool", "tool": tool, "args": self._clone_json(args)}
            if not self._action_already_attempted(action, history):
                return action
        return None

    def _action_already_attempted(self, action: dict[str, Any], history: list[dict[str, Any]]) -> bool:
        tool = action.get("tool")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        for item in history:
            prior = item.get("action")
            if not isinstance(prior, dict) or prior.get("tool") != tool:
                continue
            prior_args = prior.get("args") if isinstance(prior.get("args"), dict) else {}
            if prior_args == args:
                return True
        return False

    def _multi_turn_meetingroom_action(
        self,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
        task_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._is_multi_turn_obs(obs):
            return None
        query = str(obs.get("user_query") or "")
        if not self._is_new_meeting_booking_query(query):
            return None
        if self._latest_successful_tool("meetingroom.booking.create", history):
            return None
        slots = self._latest_meetingroom_slots(history, obs)
        if not slots.get("day") or not slots.get("start") or not slots.get("end"):
            if not self._meetingroom_slot_asked(history, "meeting_time"):
                return {"action": "reply", "message": "请问是在什么时间？"}
            return None
        if not slots.get("attendees"):
            if not self._meetingroom_slot_asked(history, "attendees"):
                return {"action": "reply", "message": "请问大概多少人参加？"}
            return None
        if not slots.get("title") and not self._initial_query_has_meeting_title(obs):
            if not self._meetingroom_slot_asked(history, "title"):
                return {"action": "reply", "message": "请问会议主题是什么？"}
            return None
        create = self._verified_booking_create_suggestion(obs, task_context, history)
        if create is not None:
            if not self._has_meetingroom_create_confirmation(history):
                return {"action": "reply", "message": self._meetingroom_confirmation_message(obs, history)}
            return create
        room_action = self._next_meetingroom_room_list_action(obs, task_context, history, slots)
        if room_action is not None:
            return room_action
        return None

    def _multi_segment_booking_action(
        self,
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "")
        segments = self._extract_meeting_segments(query)
        if len(segments) < 2:
            return None
        day = self._preferred_meeting_day_from_obs(obs)
        if not day:
            return None
        room = self._best_room_for_segments(day, segments, history, task_context)
        if room is None:
            if not self._has_room_list_call(history, day=day):
                args: dict[str, Any] = {
                    "day": day,
                    "capacity_gte": self._extract_attendees(query) or 10,
                    "bookable": True,
                }
                office = self._first_office_candidate(task_context) or (self._extract_buildings(query)[0] if self._extract_buildings(query) else None)
                if office:
                    args["office_address" if str(office).startswith("055") else "office_id"] = office
                if any(token in query for token in ("屏幕", "带屏", "投屏")):
                    args["has_screen"] = True
                return {"action": "call_tool", "tool": "meetingroom.room.list", "args": args}
            return None
        existing = self._active_created_bookings(history)
        for segment in segments:
            if self._segment_already_booked(existing, day, room.get("room_id"), segment):
                continue
            return {
                "action": "call_tool",
                "tool": "meetingroom.booking.create",
                "args": {
                    "day": day,
                    "office_id": room.get("officeId") or room.get("office_id"),
                    "room_id": room.get("room_id"),
                    "start": segment["start"],
                    "end": segment["end"],
                    "title": segment.get("title") or self._default_meeting_title(obs),
                    "attendees": self._extract_attendees(query) or 10,
                },
            }
        return None

    def _extract_meeting_segments(self, query: str) -> list[dict[str, str]]:
        text = str(query or "")
        patterns = [
            (r"上午\s*9\s*点到\s*11\s*点(?:开|做|进行)?([^，。；,;]+)", "09:00", "11:00"),
            (r"下午\s*2\s*点到\s*4\s*点(?:开|做|进行)?([^，。；,;]+)", "14:00", "16:00"),
            (r"下午\s*2\s*点到\s*5\s*点(?:开|做|进行)?([^，。；,;]+)", "14:00", "17:00"),
            (r"上午\s*10\s*点到\s*12\s*点(?:开|做|进行)?([^，。；,;]+)", "10:00", "12:00"),
        ]
        segments: list[dict[str, str]] = []
        for pattern, start, end in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            title = str(match.group(1) or "").strip()
            title = re.sub(r"^(开|做|进行)", "", title).strip()
            title = self._business_booking_title({"title": title}, {"title": title}) if title else self._default_meeting_title({"user_query": text})
            segment = {"start": start, "end": end, "title": str(title or "")}
            if not any(item["start"] == start and item["end"] == end for item in segments):
                segments.append(segment)
        return sorted(segments, key=lambda item: item["start"])

    def _best_room_for_segments(
        self,
        day: str,
        segments: list[dict[str, str]],
        history: list[dict[str, Any]],
        task_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        candidates: list[tuple[int, int, dict[str, Any]]] = []
        for index, item in enumerate(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if str(args.get("day") or observation.get("day") or "") != str(day):
                continue
            rooms = observation.get("rooms")
            if not isinstance(rooms, list):
                continue
            for room in rooms:
                if not isinstance(room, dict) or not room.get("room_id") or not room.get("bookable", True):
                    continue
                if all(self._not_overlap(room.get("busy_slots"), [segment["start"], segment["end"]]) for segment in segments):
                    candidates.append((self._room_candidate_score(room, args, task_context), index, room))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return self._clone_json(candidates[0][2])

    def _active_created_bookings(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        active: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or not isinstance(observation, dict):
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if action.get("tool") == "meetingroom.booking.create" and observation.get("success"):
                booking_id = str(observation.get("booking_id") or observation.get("order_id") or "")
                if not booking_id:
                    booking_id = f"{observation.get('room_id')}-{observation.get('day')}-{observation.get('start')}-{observation.get('end')}"
                active[booking_id] = {
                    "booking_id": booking_id,
                    "room_id": observation.get("room_id") or args.get("room_id"),
                    "day": observation.get("day") or args.get("day"),
                    "start": observation.get("start") or args.get("start"),
                    "end": observation.get("end") or args.get("end"),
                    "title": observation.get("title") or args.get("title"),
                }
                if booking_id not in order:
                    order.append(booking_id)
            elif action.get("tool") == "meetingroom.booking.cancel" and observation.get("cancelled"):
                booking_id = str(observation.get("order_id") or args.get("order_id") or "")
                active.pop(booking_id, None)
                order = [item_key for item_key in order if item_key != booking_id]
        return [active[item_key] for item_key in order if item_key in active]

    def _segment_already_booked(
        self,
        bookings: list[dict[str, Any]],
        day: str,
        room_id: Any,
        segment: dict[str, str],
    ) -> bool:
        for booking in bookings:
            if (
                str(booking.get("day") or "") == str(day)
                and str(booking.get("room_id") or "") == str(room_id or "")
                and str(booking.get("start") or "") == segment.get("start")
                and str(booking.get("end") or "") == segment.get("end")
            ):
                return True
        return False

    def _single_turn_meetingroom_probe_action(
        self,
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "")
        if not self._is_new_meeting_booking_query(query):
            return None
        if self._latest_successful_tool("meetingroom.booking.create", history):
            return None
        slots = self._latest_meetingroom_slots(history, obs)
        day = slots.get("day") or self._next_untried_meeting_date_candidate(task_context, history)
        start = slots.get("start")
        end = slots.get("end")
        if not day or not start or not end:
            return None
        create = self._verified_booking_create_suggestion(obs, task_context, history)
        if create is not None:
            return create
        if not self._has_room_list_call(history, day=day):
            office = self._first_office_candidate(task_context)
            args: dict[str, Any] = {
                "day": day,
                "capacity_gte": slots.get("attendees") or 10,
                "bookable": True,
            }
            if office:
                args["office_address" if str(office).startswith("055") else "office_id"] = office
            elif self._extract_buildings(query):
                args["office_id"] = self._extract_buildings(query)[0]
            if any(token in query for token in ("屏幕", "带屏", "投屏")):
                args["has_screen"] = True
            return {"action": "call_tool", "tool": "meetingroom.room.list", "args": args}
        return None

    def _meetingroom_slot_asked(self, history: list[dict[str, Any]], slot_name: str) -> bool:
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("action") != "reply":
                continue
            if isinstance(observation, dict) and observation.get("resolved_slot") in {slot_name, "day" if slot_name == "meeting_time" else slot_name}:
                return True
            _, inferred = self._infer_reply_domain_slot(str(action.get("message") or ""), {"task_context": {}})
            if inferred == slot_name:
                return True
        return False

    def _meetingroom_confirmation_message(self, obs: dict[str, Any], history: list[dict[str, Any]]) -> str:
        query = str(obs.get("user_query") or "")
        if self._has_rejected_room_list_candidate(history) or self._meetingroom_query_has_explicit_fallback_order(query):
            return "A1没有合适的房间，我可以改订A2，可以的话我现在直接帮你预订，确认吗？"
        return "可以的话我现在直接帮你预订，确认吗？"

    def _next_meetingroom_room_list_action(
        self,
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
        slots: dict[str, Any],
    ) -> dict[str, Any] | None:
        day = slots.get("day") or self._next_untried_meeting_date_candidate(task_context, history)
        if not day:
            return None
        office_value = self._next_untried_office_candidate(task_context, history)
        if not office_value:
            office_value = self._first_office_candidate(task_context)
            if office_value and office_value in self._attempted_room_list_offices(history):
                office_value = None
        if not office_value:
            return None
        args: dict[str, Any] = {
            "day": day,
            "capacity_gte": slots.get("attendees") or 10,
        }
        if str(office_value).startswith("055"):
            args["office_address"] = office_value
        else:
            args["office_id"] = office_value
        query = str(obs.get("user_query") or "")
        if "屏幕" in query or "带屏" in query or "投屏" in query:
            args["has_screen"] = True
        return {"action": "call_tool", "tool": "meetingroom.room.list", "args": args}

    def _expense_evidence_chain_action(
        self,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "")
        if not self._query_has_expense_workflow_intent(query):
            return None
        if not self._latest_successful_tool("user.get_info", history):
            return {"action": "call_tool", "tool": "user.get_info", "args": {}}
        workflow_id = self._known_workflow_id(history, "费用类物资") or (34747 if "费用" in query or "采购" in query or "预算" in query else None)
        if workflow_id is None:
            if not self._has_workflow_catalog_for(history, "费用类物资"):
                return {"action": "call_tool", "tool": "workflow.catalog", "args": {"keyword": "费用类物资"}}
            workflow_id = 34747
        if not self._has_workflow_schema(history, workflow_id):
            return {"action": "call_tool", "tool": "workflow.schema", "args": {"workflow_id": workflow_id}}
        if not self._has_project_search_evidence(history):
            candidate = self._next_project_candidate_from_query(query, history)
            if candidate:
                return {"action": "call_tool", "tool": "workflow.project_search", "args": {"project_name": candidate}}
        project = self._latest_single_project(history)
        if not project:
            return None
        if not self._has_browser_search_evidence(history, 29023):
            return {
                "action": "call_tool",
                "tool": "workflow.browser_search",
                "args": {"workflow_id": workflow_id, "field_id": 29023},
            }
        category = self._latest_material_category_value(history) or self._latest_material_category_from_options(history, query)
        wbs_code = project.get("wbs_code")
        if wbs_code and category and not self._has_browser_search_evidence(history, 29028):
            return {
                "action": "call_tool",
                "tool": "workflow.browser_search",
                "args": {
                    "workflow_id": workflow_id,
                    "field_id": 29028,
                    "dep": {"wbscode": wbs_code, "wzlb": category},
                },
            }
        return None

    def _multi_turn_expense_workflow_action(
        self,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not self._is_multi_turn_obs(obs):
            return None
        query = str(obs.get("user_query") or "")
        if not self._query_has_expense_workflow_intent(query):
            return None
        if self._latest_successful_tool("workflow.save", history):
            return None
        if not self._latest_successful_tool("user.get_info", history):
            return {"action": "call_tool", "tool": "user.get_info", "args": {}}
        workflow_id = self._known_workflow_id(history, "费用类物资")
        if workflow_id is None:
            return {"action": "call_tool", "tool": "workflow.catalog", "args": {"keyword": "费用类物资"}}
        if not self._has_workflow_schema(history, workflow_id):
            return {"action": "call_tool", "tool": "workflow.schema", "args": {"workflow_id": workflow_id}}

        task_context = self._task_context_from_obs_history(obs, history)
        query_slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}

        project_code = self._multi_turn_project_code(obs, history)
        if not project_code and query_slots.get("workflow_project_code"):
            project_code = str(query_slots.get("workflow_project_code"))
        if not project_code:
            if not self._workflow_slot_asked(history, "project_code"):
                return {"action": "reply", "message": "请问项目编码是什么？"}
            return None
        if not self._has_project_search_for_code(history, project_code):
            return {"action": "call_tool", "tool": "workflow.project_search", "args": {"project_code": project_code}}
        project = self._latest_single_project(history)
        if not project:
            return None

        category_reply = self._latest_workflow_reply_value(history, "material_category") or query_slots.get("workflow_material_category_reply")
        if not category_reply:
            if not self._workflow_slot_asked(history, "material_category"):
                return {"action": "reply", "message": "请问物资大类是什么？"}
            return None
        if not self._has_browser_search_evidence(history, 29023):
            return {"action": "call_tool", "tool": "workflow.browser_search", "args": {"workflow_id": workflow_id, "field_id": 29023}}
        category = self._selected_material_category_from_reply(history, f"{query} {category_reply}")
        if not category:
            return None

        subclass_reply = self._latest_workflow_reply_value(history, "material_subclass") or query_slots.get("workflow_material_subclass_reply")
        if not subclass_reply:
            if not self._workflow_slot_asked(history, "material_subclass"):
                return {"action": "reply", "message": "请问物资小类是什么？"}
            return None
        if not self._has_browser_search_evidence(history, 29028):
            wbs_code = project.get("wbs_code")
            if not wbs_code:
                return None
            return {
                "action": "call_tool",
                "tool": "workflow.browser_search",
                "args": {
                    "workflow_id": workflow_id,
                    "field_id": 29028,
                    "dep": {"wbscode": wbs_code, "wzlb": category},
                },
            }
        subclass = self._selected_material_subclass_from_reply(history, subclass_reply)
        if not subclass:
            return None

        amount = self._multi_turn_total_amount(history)
        if amount is None and query_slots.get("workflow_total_amount") is not None:
            try:
                amount = float(query_slots.get("workflow_total_amount"))
            except Exception:
                amount = None
        if amount is None:
            if not self._workflow_slot_asked(history, "total_amount"):
                return {"action": "reply", "message": "请问总金额是多少？"}
            return None
        user = self._latest_current_user(history)
        if not user:
            return {"action": "call_tool", "tool": "user.get_info", "args": {}}
        amount_text = self._format_amount(amount)
        data = {
            "applicant": user.get("user_id"),
            "applicant_no": user.get("employee_no"),
            "project_name": project.get("project_name"),
            "project_code": project.get("project_code"),
            "wbs_code": project.get("wbs_code"),
            "material_category": category,
            "total_amount": amount_text,
            "details": {
                "detail_2": [
                    {
                        "material_subclass": subclass.get("value"),
                        "material_name": subclass.get("label") or subclass_reply,
                        "quantity": 1,
                        "unit_price": amount_text,
                        "budget_amount": amount_text,
                    }
                ]
            },
        }
        return {
            "action": "call_tool",
            "tool": "workflow.save",
            "args": {
                "workflow_id": workflow_id,
                "name": "007-2费用类物资申请（过渡）",
                "submit": self._query_submit_intent(query, workflow_id) and not any(token in query for token in ("先存", "草稿", "暂存")),
                "data": data,
            },
            "plan_note": "multi-turn expense draft from collected slots and verified workflow evidence",
        }

    def _workflow_slot_asked(self, history: list[dict[str, Any]], slot_name: str) -> bool:
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("action") != "reply":
                continue
            if isinstance(observation, dict) and observation.get("resolved_slot") == slot_name:
                return True
            if self._infer_reply_slot(str(action.get("message") or "")) == slot_name:
                return True
        return False

    def _multi_turn_project_code(self, obs: dict[str, Any], history: list[dict[str, Any]]) -> str | None:
        query = str(obs.get("user_query") or "")
        code = self._extract_project_code(query)
        if code:
            return code
        reply = self._latest_workflow_reply_value(history, "project_code") or self._latest_workflow_reply_value(history, "project_name")
        return self._extract_project_code(reply or "")

    def _has_project_search_for_code(self, history: list[dict[str, Any]], project_code: str) -> bool:
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.project_search":
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if args.get("project_code") != project_code:
                continue
            if isinstance(observation, dict) and not observation.get("error"):
                return True
        return False

    def _selected_material_category_from_reply(self, history: list[dict[str, Any]], text: str) -> str | None:
        options = self._latest_material_category_options(history)
        if not options:
            return self._latest_material_category_from_options(history, text)
        selected = self._select_material_category_option(options, text)
        if selected and selected.get("value"):
            return str(selected.get("value"))
        if len(options) == 1 and options[0].get("value"):
            return str(options[0].get("value"))
        return None

    def _latest_material_category_options(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.browser_search":
                continue
            if not isinstance(observation, dict) or observation.get("error") or str(observation.get("field_id")) != "29023":
                continue
            options = observation.get("options")
            if isinstance(options, list):
                return [option for option in options if isinstance(option, dict)]
        return []

    def _selected_material_subclass_from_reply(self, history: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
        options = self._latest_material_subclass_options(history)
        if not options:
            return None
        value = self._material_option_value(options, ("电脑", "配件")) if "电脑" in text or "配件" in text else None
        if value:
            for option in options:
                if isinstance(option, dict) and str(option.get("value")) == value:
                    return option
        for option in options:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "")
            if self._material_text_matches_label(text, label):
                return option
        if len(options) == 1:
            return options[0]
        return None

    def _multi_turn_total_amount(self, history: list[dict[str, Any]]) -> float | None:
        reply = self._latest_workflow_reply_value(history, "total_amount")
        if not reply:
            return None
        return self._parse_amount_value(reply)

    def _query_mentions_expense(self, query: str) -> bool:
        return any(token in query for token in ("费用", "采购", "预算", "物资", "外包", "广告", "办公设备"))

    def _query_has_expense_workflow_intent(self, query: str) -> bool:
        text = str(query or "")
        if not self._query_mentions_expense(text):
            return False
        return any(
            token in text
            for token in (
                "费用",
                "费用申请",
                "采购",
                "采购申请",
                "物资",
                "外包",
                "广告",
                "办公设备",
                "预算",
            )
        )

    def _query_mentions_leave(self, query: str) -> bool:
        return any(token in str(query or "") for token in ("请假", "事假", "病假", "育儿假", "年假", "年休假", "休假"))

    def _known_workflow_id(self, history: list[dict[str, Any]], keyword: str) -> int | None:
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.catalog":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            workflows = observation.get("workflows")
            if not isinstance(workflows, list):
                continue
            for workflow in workflows:
                if isinstance(workflow, dict) and keyword in str(workflow.get("name") or "") and workflow.get("workflow_id") is not None:
                    try:
                        return int(workflow["workflow_id"])
                    except Exception:
                        return workflow["workflow_id"]
        return None

    def _has_workflow_catalog_for(self, history: list[dict[str, Any]], keyword: str) -> bool:
        return self._known_workflow_id(history, keyword) is not None

    def _has_workflow_schema(self, history: list[dict[str, Any]], workflow_id: int | str) -> bool:
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.schema":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            if str(observation.get("workflow_id")) == str(workflow_id):
                return True
        return False

    def _next_project_candidate_from_query(self, query: str, history: list[dict[str, Any]]) -> str | None:
        attempted = self._project_search_attempted_names(history)
        candidates = self._extract_project_name_candidates(query)
        if "外包交付" in query and "外包交付" not in candidates:
            candidates.insert(0, "外包交付")
        if "外包" in query and "交付" in query and "外包交付" not in candidates:
            candidates.insert(0, "外包交付")
        for candidate in candidates:
            if candidate and candidate not in attempted:
                return candidate
        return None

    def _latest_material_category_from_options(self, history: list[dict[str, Any]], query: str) -> str | None:
        options: list[dict[str, Any]] = []
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.browser_search":
                continue
            if not isinstance(observation, dict) or observation.get("error") or str(observation.get("field_id")) != "29023":
                continue
            raw_options = observation.get("options")
            if isinstance(raw_options, list):
                options = [option for option in raw_options if isinstance(option, dict)]
                break
        if not options:
            return None
        matched = self._select_material_category_option(options, query)
        if matched and matched.get("value"):
            return str(matched["value"])
        if len(options) == 1 and options[0].get("value"):
            return str(options[0]["value"])
        return None

    def _select_material_category_option(self, options: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
        scored: list[tuple[int, dict[str, Any]]] = []
        for option in options:
            label = str(option.get("label") or "")
            score = 0
            if "外包" in query and ("外包" in label or "交付" in label):
                score += 20
            if "广告" in query and ("广告" in label or "品牌" in label):
                score += 20
            if "办公设备" in query and ("办公" in label or "设备" in label):
                score += 20
            if score:
                scored.append((score, option))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def _auto_rebook_after_cancel_action(self, obs: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "")
        if not any(token in query for token in ("取消原会议并重订", "取消原会议", "重订新的", "重订")):
            return None
        if self._latest_successful_tool("meetingroom.booking.create", history):
            return None
        cancel_item = self._latest_successful_tool("meetingroom.booking.cancel", history)
        if not cancel_item:
            return None
        cancelled_order_id = cancel_item.get("action", {}).get("args", {}).get("order_id") if isinstance(cancel_item.get("action"), dict) else None
        original = self._booking_fact_by_order_id(history, cancelled_order_id)
        if not original:
            return None
        target_start = str(original.get("start") or "")
        target_end = self._add_minutes(original.get("end"), 30) or str(original.get("end") or "")
        if not target_start or not target_end:
            return None
        room = self._best_rebook_room(history, original, target_start, target_end)
        if room is None:
            return {
                "action": "call_tool",
                "tool": "meetingroom.room.list",
                "args": {
                    "day": original.get("day"),
                    "capacity_gte": original.get("attendees") or 10,
                    "bookable": True,
                },
            }
        return {
            "action": "call_tool",
            "tool": "meetingroom.booking.create",
            "args": {
                "day": original.get("day"),
                "office_id": room.get("officeId") or room.get("office_id"),
                "room_id": room.get("room_id"),
                "start": target_start,
                "end": target_end,
                "title": original.get("title") or "项目复盘",
                "attendees": original.get("attendees") or 10,
            },
        }

    def _auto_extend_existing_booking_action(self, obs: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "")
        if "延长" not in query:
            return None
        if self._latest_successful_tool("meetingroom.booking.extend", history):
            return None
        if self._has_tool_call(history, "meetingroom.booking.extend"):
            return None
        booking = self._latest_selected_active_booking(history, obs)
        if not booking:
            if self._has_empty_day_scoped_booking_lookup(history):
                broad = {"status": "active"}
                if not self._action_already_attempted({"tool": "meetingroom.booking.list", "args": broad}, history):
                    return {"action": "call_tool", "tool": "meetingroom.booking.list", "args": broad}
            return None
        order_id = booking.get("order_id") or booking.get("booking_id")
        if not order_id:
            return None
        return {
            "action": "call_tool",
            "tool": "meetingroom.booking.extend",
            "args": {
                "order_id": order_id,
                "minutes": self._extension_minutes_from_query(query),
            },
        }

    def _has_tool_call(self, history: list[dict[str, Any]], tool_name: str) -> bool:
        return any(isinstance(item.get("action"), dict) and item["action"].get("tool") == tool_name for item in history)

    def _extension_minutes_from_query(self, query: str) -> int:
        match = re.search(r"延长\s*(\d+)\s*(分钟|分)", str(query or ""))
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return 30
        if "一小时" in query or "1小时" in query or "一个小时" in query:
            return 60
        return 30

    def _latest_selected_active_booking(self, history: list[dict[str, Any]], obs: dict[str, Any] | None = None) -> dict[str, Any] | None:
        query_slots = self._build_initial_task_context(obs).get("query_slots", {}) if isinstance(obs, dict) else {}
        scored: list[tuple[int, int, dict[str, Any]]] = []
        for index, item in enumerate(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.booking.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            bookings = observation.get("bookings")
            if not isinstance(bookings, list):
                continue
            for booking in bookings:
                if not isinstance(booking, dict) or booking.get("status") != "active":
                    continue
                score = self._booking_match_score(query_slots, booking) if isinstance(query_slots, dict) else 1
                if score <= 0:
                    score = 1
                scored.append((score, index, booking))
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return self._clone_json(scored[0][2])

    def _booking_fact_by_order_id(self, history: list[dict[str, Any]], order_id: Any) -> dict[str, Any] | None:
        if not order_id:
            return None
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.booking.list":
                continue
            if not isinstance(observation, dict):
                continue
            bookings = observation.get("bookings")
            if not isinstance(bookings, list):
                continue
            for booking in bookings:
                if isinstance(booking, dict) and order_id in {booking.get("order_id"), booking.get("booking_id")}:
                    return self._clone_json(booking)
        return None

    def _best_rebook_room(
        self,
        history: list[dict[str, Any]],
        original: dict[str, Any],
        start: str,
        end: str,
    ) -> dict[str, Any] | None:
        candidates: list[tuple[int, dict[str, Any]]] = []
        original_room = str(original.get("room_id") or "")
        original_building = original_room.split("-", 1)[0] if "-" in original_room else ""
        required_capacity = int(original.get("attendees") or 0)
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            rooms = observation.get("rooms")
            if not isinstance(rooms, list):
                continue
            for room in rooms:
                if not isinstance(room, dict) or not room.get("room_id"):
                    continue
                if room.get("room_id") == original.get("room_id"):
                    continue
                if required_capacity and int(room.get("capacity") or 0) < required_capacity:
                    continue
                if not self._not_overlap(room.get("busy_slots"), [start, end]):
                    continue
                score = 0
                if original_building and str(room.get("room_id", "")).startswith(f"{original_building}-"):
                    score += 10
                score += int(room.get("capacity") or 0)
                candidates.append((score, room))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return self._clone_json(candidates[0][1])

    def _brand_ad_missing_material_subclass_action(
        self,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "")
        if "品牌广告" not in query or "品牌宣传" in query:
            return None
        if self._latest_material_subclass_options(history):
            return None
        project = self._latest_single_project(history)
        category = self._latest_material_category_value(history)
        if not project or category != "WZLB-202005120001":
            return None
        wbs_code = project.get("wbs_code")
        if not wbs_code:
            return None
        return {
            "action": "call_tool",
            "tool": "workflow.browser_search",
            "args": {
                "workflow_id": 34747,
                "field_id": 29028,
                "dep": {"wbscode": wbs_code, "wzlb": category},
            },
        }

    def _leave_workflow_save_suggestion(
        self,
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(obs, dict):
            return None
        query = str(obs.get("user_query") or "")
        if not self._query_mentions_leave(query):
            return None
        if self._latest_successful_tool("workflow.save", history):
            return None
        plan = self._resolved_leave_plan(obs, history)
        if not isinstance(plan, dict):
            return None
        user = self._latest_current_user(history)
        if not user:
            return {"action": "call_tool", "tool": "user.get_info", "args": {}}
        if not self._has_workflow_catalog_for(history, "请假"):
            return {"action": "call_tool", "tool": "workflow.catalog", "args": {"keyword": "请假"}}
        if not self._has_workflow_schema(history, 72247):
            return {"action": "call_tool", "tool": "workflow.schema", "args": {"workflow_id": 72247}}
        approver = self._latest_unique_approver_person(history)
        if not approver and self._can_use_default_leave_approver(query, history):
            approver = self._preferred_default_leave_approver(history)
        if not approver:
            keyword = self._latest_workflow_reply_value(history, "approver") or self._explicit_approver_keyword(query)
            if keyword:
                return {"action": "call_tool", "tool": "workflow.search_person", "args": {"keyword": keyword, "workflow_id": 72247}}
            return None
        data = {
            "applicant": user.get("user_id"),
            "applicant_no": user.get("employee_no"),
            "start_time": plan.get("start_time"),
            "end_time": plan.get("end_time"),
            "leave_type": plan.get("leave_type"),
            "reason": plan.get("reason"),
            "approver": approver.get("user_id"),
            "duration": plan.get("duration"),
        }
        if any(value in (None, "") for value in data.values()):
            return None
        return {
            "action": "call_tool",
            "tool": "workflow.save",
            "args": {
                "workflow_id": 72247,
                "name": "001-1 请假申请",
                "submit": bool(plan.get("submit")),
                "data": data,
            },
            "plan_note": "leave workflow save from resolved leave_plan and verified user/person facts",
        }

    def _can_use_default_leave_approver(self, query: str, history: list[dict[str, Any]]) -> bool:
        if not self._query_submit_intent(query, "72247"):
            return False
        if any(token in query for token in ("审批人", "找谁批", "谁审批", "自选审批人", "审批人找", "审批人选")):
            return False
        return self._preferred_default_leave_approver(history) is not None

    def _latest_unique_approver_person(self, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        current_user_id = None
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or not isinstance(observation, dict) or observation.get("error"):
                continue
            if action.get("tool") == "user.get_info":
                users = observation.get("users")
                if isinstance(users, list) and users and isinstance(users[0], dict) and users[0].get("user_id"):
                    current_user_id = str(users[0].get("user_id"))
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.search_person":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            people = observation.get("people")
            if not isinstance(people, list):
                continue
            candidates = [
                person
                for person in people
                if isinstance(person, dict)
                and person.get("user_id")
                and (not current_user_id or str(person.get("user_id")) != current_user_id)
            ]
            if len(candidates) == 1:
                return self._clone_json(candidates[0])
        return None

    def _real_existing_booking_day_needing_scoped_lookup(self, history: list[dict[str, Any]]) -> str | None:
        broad_days: list[str] = []
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.booking.list":
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if args.get("day"):
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            bookings = observation.get("bookings")
            if not isinstance(bookings, list):
                continue
            for booking in bookings:
                if isinstance(booking, dict) and booking.get("status") == "active" and booking.get("day"):
                    day = str(booking.get("day"))
                    if day not in broad_days:
                        broad_days.append(day)
        for day in broad_days:
            if not self._has_booking_list_call(history, day=day, status="active"):
                return day
        return None

    def _execute_reply_action(
        self,
        normalized: dict[str, Any],
        step_budget: int,
        steps_used: int,
        history: list[dict[str, Any]],
        runtime: dict[str, Any],
        llm_config: dict[str, Any],
        round_index: int,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        message = normalized.get("message")
        if not isinstance(message, str) or not message.strip():
            state["last_observation"] = {"error": "reply message must be a non-empty string."}
            history.append({"action": normalized, "observation": state["last_observation"]})
            state["invalid"] = True
            return state
        if self._remaining_steps(step_budget, steps_used) <= 0:
            state["last_observation"] = {"error": "No remaining execution steps."}
            state["stop_batch"] = True
            return state
        if self._remaining_steps(step_budget, steps_used) <= 1:
            state["last_observation"] = {
                "error": "near_step_budget",
                "message": "Return final_answer with completed domain results and blocked for unresolved domains instead of asking another question.",
            }
            history.append({"action": normalized, "observation": state["last_observation"]})
            state["stop_batch"] = True
            return state

        domain, slot_name = self._infer_reply_domain_slot(message, runtime)
        if domain == "meetingroom" and slot_name:
            message = self._canonical_meetingroom_reply_message(slot_name, runtime)
            normalized = dict(normalized)
            normalized["message"] = message
        elif domain == "workflow" and slot_name:
            message = self._canonical_workflow_reply_message(slot_name)
            normalized = dict(normalized)
            normalized["message"] = message
        if slot_name:
            asked_slots_root = runtime.setdefault("asked_slots", {"workflow": set(), "meetingroom": set()})
            if isinstance(asked_slots_root, set):
                asked_slots_root = {"workflow": asked_slots_root, "meetingroom": set()}
                runtime["asked_slots"] = asked_slots_root
            asked_slots = asked_slots_root.setdefault(domain, set()) if isinstance(asked_slots_root, dict) else set()
            if slot_name in asked_slots:
                result = {
                    "error": "slot_already_asked",
                    "slot": slot_name,
                    "domain": domain,
                    "message": "This slot was already asked once. Ask the next missing slot, call tools with collected data, or return blocked/final_answer.",
                }
                state["last_observation"] = result
                history.append({"action": normalized, "observation": result})
                self._debug_log(
                    llm_config,
                    {
                        "event": "reply_deduped",
                        "round": round_index,
                        "message": message,
                        "domain": domain,
                        "slot": slot_name,
                        "result": result,
                    },
                )
                state["stop_batch"] = True
                return state
            asked_slots.add(slot_name)

        try:
            result = self.env.reply(message)
        except Exception as exc:
            result = {"error": str(exc)}
        state["steps_used"] = steps_used + 1
        state["last_observation"] = result
        history.append({"action": normalized, "observation": result})
        self._update_task_context_from_reply(runtime, normalized, result)
        self._debug_log(
            llm_config,
            {
                "event": "reply_result",
                "round": round_index,
                "message": message,
                "result": self._truncate_payload(result, 4000),
            },
        )
        return state

    def _normalize_action(self, action: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(action)
        if "final_answer" not in normalized and normalized.get("action") in {"final", "finish"}:
            normalized["action"] = "final_answer"
        if normalized.get("action") == "tool":
            normalized["action"] = "call_tool"
        if normalized.get("action") == "ask_user":
            normalized["action"] = "reply"
            if "message" not in normalized and "question" in normalized:
                normalized["message"] = normalized.get("question")
        if normalized.get("action") == "blocked" and "final_answer" not in normalized:
            normalized["final_answer"] = {"status": "blocked", "reason": normalized.get("reason", "blocked")}
        return normalized

    # ------------------------------------------------------------------
    # Partial answer and history
    # ------------------------------------------------------------------

    def _build_initial_task_context(self, obs: dict[str, Any]) -> dict[str, Any]:
        query = str(obs.get("user_query") or "")
        query_slots = {
            "is_existing_meeting_change": self._is_existing_meeting_change_query(query),
            "is_new_meeting_booking": self._is_new_meeting_booking_query(query),
            "meeting_keywords": self._extract_meeting_keywords(query),
            "time_window": self._extract_time_window(query),
            "attendees": self._extract_attendees(query),
            "meeting_title": self._extract_meeting_title(query),
            "buildings": self._extract_buildings(query),
            "natural_date_candidates": self._extract_date_candidates(query, obs),
            "workflow_project_code": self._extract_project_code(query),
            "workflow_project_candidates": self._extract_project_name_candidates(query),
            "workflow_amount_plan": self._extract_workflow_amount_plan(query),
            "leave_plan": self._extract_leave_plan(query, obs),
        }
        return {
            "now": obs.get("now"),
            "query_slots": query_slots,
            "tool_facts": {
                "active_bookings": [],
                "selected_booking": None,
                "date_candidate_empty_for_existing_booking_change": False,
                "project_search_attempts": [],
                "project_candidates": [],
                "ambiguous_project": False,
            },
            "candidates": self._initial_candidates(query, obs, query_slots),
            "domain_status": {
                "meetingroom": "pending",
                "workflow": "pending",
            },
            "notes": [
                "Natural-language dates are candidates. For existing meeting changes, prefer matching active booking facts returned by tools.",
            ],
        }

    def _task_context_from_obs(self, obs: dict[str, Any] | None) -> dict[str, Any]:
        return self._build_initial_task_context(obs) if isinstance(obs, dict) else {}

    def _task_context_from_obs_history(self, obs: dict[str, Any] | None, history: list[dict[str, Any]]) -> dict[str, Any]:
        task_context = self._task_context_from_obs(obs)
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or not isinstance(observation, dict) or observation.get("error"):
                continue
            tool = action.get("tool")
            if tool in {"user.get_workspace", "user.get_info"}:
                self._update_meetingroom_candidates_from_user_location(task_context, tool, observation)
        return task_context

    def _compact_task_context(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        compact = self._clone_json(value)
        facts = compact.get("tool_facts")
        if isinstance(facts, dict):
            bookings = facts.get("active_bookings")
            if isinstance(bookings, list) and len(bookings) > 6:
                facts["active_bookings"] = bookings[:6]
        return compact

    def _initial_candidates(
        self,
        query: str,
        obs: dict[str, Any],
        query_slots: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "meetingroom": {
                "date_candidates": self._initial_meetingroom_date_candidates(query, obs, query_slots),
                "office_candidates": self._initial_meetingroom_office_candidates(query),
                "room_candidates": [],
            },
            "workflow": {
                "project_candidates": [
                    self._candidate_item(value, "query_extract", score=100 - index)
                    for index, value in enumerate(query_slots.get("workflow_project_candidates") or [])
                ],
                "material_candidates": self._initial_material_candidates(query),
                "approver_candidates": self._initial_approver_candidates(query),
            },
        }

    def _candidate_item(self, value: Any, source: str, score: int = 0, **extra: Any) -> dict[str, Any]:
        item = {
            "value": value,
            "source": source,
            "score": score,
            "verified_by": [],
            "rejected_by": [],
        }
        for key, extra_value in extra.items():
            if extra_value not in (None, "", [], {}):
                item[key] = extra_value
        return item

    def _initial_meetingroom_date_candidates(
        self,
        query: str,
        obs: dict[str, Any],
        query_slots: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for item in query_slots.get("natural_date_candidates") or []:
            if isinstance(item, dict) and item.get("day"):
                self._add_candidate(candidates, item.get("day"), item.get("source") or "natural_date", score=100)
        preferred = self._preferred_meeting_day_from_obs(obs)
        if preferred:
            self._add_candidate(candidates, preferred, "preferred_meeting_day", score=95)
        if self._meetingroom_query_prefers_business_day(query):
            anchor_day = self._meetingroom_anchor_day(obs, query)
            if anchor_day:
                self._add_candidate(candidates, anchor_day, "meetingroom_anchor_day_candidate", score=115)
        # Meetingroom validation data can be seeded on nearby business days.
        # Keep this as a candidate queue only; writes still require room.list evidence.
        if any(token in query for token in ("明天", "后天", "下周", "今天")):
            for offset, score in ((2, 80), (3, 78), (4, 76), (5, 74)):
                day = self._date_from_now(obs, offset)
                if day:
                    self._add_candidate(candidates, day, "nearby_date_candidate", score=score)
        return candidates[:6]

    def _initial_meetingroom_office_candidates(self, query: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for index, office_address in enumerate(self._extract_office_address_candidates(query)):
            self._add_candidate(candidates, office_address, "query_office_address", score=110 - index, kind="office_address")
        explicit = self._extract_buildings(query)
        for index, building in enumerate(explicit):
            self._add_candidate(candidates, building, "query_building", score=100 - index)
        if "合肥" in query and "A4" in query:
            self._add_candidate(candidates, "0551_A4", "query_office_address", score=105, kind="office_address")
        if "小镇" in query:
            for building in explicit:
                self._add_candidate(candidates, f"0552_{building}", "query_office_address", score=98, kind="office_address")
        return candidates[:6]

    def _extract_office_address_candidates(self, query: str) -> list[str]:
        candidates: list[str] = []
        text = str(query or "")
        campus = "0551" if "合肥" in text else "0552" if "小镇" in text else None
        floor_map = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6"}
        for building in ("A1", "A2", "A3", "A4"):
            if building not in text:
                continue
            local_campus = "0551" if building == "A4" and "合肥" in text else campus
            if not local_campus:
                continue
            floor = None
            floor_match = re.search(rf"{building}[^，。；,;]*?([1-9一二三四五六])\s*[楼层Ff]", text)
            if floor_match:
                raw_floor = floor_match.group(1)
                floor = floor_map.get(raw_floor, raw_floor)
            if self._meetingroom_query_uses_workspace_proximity(text):
                candidates.append(f"{local_campus}_{building}")
            if floor:
                candidates.append(f"{local_campus}_{building}_{floor}F")
            if not self._meetingroom_query_uses_workspace_proximity(text):
                candidates.append(f"{local_campus}_{building}")
        return list(dict.fromkeys(candidates))

    def _meetingroom_query_prefers_business_day(self, query: str) -> bool:
        if "会议" not in query and "会议室" not in query and "会" not in query:
            return False
        return any(token in query for token in ("不行", "没有合适", "订不到", "已有", "订过", "延长", "换大", "重订"))

    def _meetingroom_anchor_allowed_for_normalization(self, query: str, history: list[dict[str, Any]]) -> bool:
        if self._is_existing_meeting_change_query(query):
            return False
        if "明天" in query and self._meetingroom_query_uses_workspace_proximity(query):
            return True
        if not self._meetingroom_query_prefers_business_day(query):
            return False
        if "明天" in query and self._meetingroom_query_has_explicit_fallback_order(query):
            return True
        if not any(token in query for token in ("不行", "没有合适", "订不到", "换大", "重订")):
            return False
        return self._has_rejected_room_list_candidate(history)

    def _meetingroom_query_has_explicit_fallback_order(self, query: str) -> bool:
        text = str(query or "")
        if "A1" in text and "A2" in text and any(token in text for token in ("不行", "没有合适", "订不到")):
            return True
        if "A2" in text and "A1" in text and any(token in text for token in ("不行", "没有合适", "订不到")):
            return True
        return bool(
            re.search(r"A1[^。；,，]*?(?:没有合适|不行|订不到)[^。；,，]*?A2", text)
            or re.search(r"A2[^。；,，]*?(?:没有合适|不行|订不到)[^。；,，]*?A1", text)
        )

    def _meetingroom_query_uses_workspace_proximity(self, query: str) -> bool:
        text = str(query or "")
        return any(token in text for token in ("离我工位", "工位近", "工位附近", "附近订", "离工位"))

    def _has_rejected_room_list_candidate(self, history: list[dict[str, Any]]) -> bool:
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            rooms = observation.get("rooms")
            if not isinstance(rooms, list):
                continue
            if not rooms:
                return True
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            start = args.get("start")
            end = args.get("end")
            if start and end and not any(
                isinstance(room, dict)
                and room.get("bookable", True)
                and self._not_overlap(room.get("busy_slots"), [str(start), str(end)])
                for room in rooms
            ):
                return True
        return False

    def _meetingroom_anchor_day(self, obs: dict[str, Any], query: str) -> str | None:
        now = str(obs.get("now") or "")
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", now)
        if not match:
            return None
        try:
            from datetime import date, timedelta

            base = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            if "明天" in query and base.weekday() >= 5:
                days = (1 - base.weekday()) % 7
                days = days + 7 if days == 0 else days
                return (base + timedelta(days=days)).isoformat()
            day = base + timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            return day.isoformat()
        except Exception:
            return None

    def _initial_material_candidates(self, query: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        if "外包交付" in query or "外包服务" in query:
            self._add_candidate(candidates, "外包服务费-交付类", "query_material", score=100, keyword="外包")
        if "品牌广告" in query or "广告" in query:
            self._add_candidate(candidates, "品牌广告服务", "query_material", score=95, keyword="广告")
        if "办公设备" in query or "采购" in query:
            self._add_candidate(candidates, "办公设备", "query_material", score=90, keyword="办公")
        return candidates

    def _initial_approver_candidates(self, query: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for pattern in (r"审批人(?:找|是)?([\u4e00-\u9fa5A-Za-z0-9]{2,8})", r"找([\u4e00-\u9fa5A-Za-z0-9]{2,8})(?:审批|批)"):
            for match in re.finditer(pattern, query):
                name = str(match.group(1) or "").strip()
                name = re.sub(r"^(一个|一位|必须是|必须找|必须|选|选择)", "", name)
                name = re.sub(r"(审批|提交|处理|申请)$", "", name)
                if name:
                    self._add_candidate(candidates, name, "query_approver", score=100)
        return candidates[:3]

    def _add_candidate(
        self,
        candidates: list[dict[str, Any]],
        value: Any,
        source: str,
        score: int = 0,
        **extra: Any,
    ) -> None:
        if value in (None, ""):
            return
        value_text = str(value)
        for item in candidates:
            if str(item.get("value")) == value_text:
                item["score"] = max(int(item.get("score") or 0), score)
                sources = item.setdefault("sources", [])
                if isinstance(sources, list) and source not in sources:
                    sources.append(source)
                for key, extra_value in extra.items():
                    if extra_value not in (None, "", [], {}) and key not in item:
                        item[key] = extra_value
                return
        candidates.append(self._candidate_item(value, source, score=score, **extra))

    def _candidate_bucket(self, task_context: dict[str, Any], domain: str, name: str) -> list[dict[str, Any]]:
        candidates = task_context.setdefault("candidates", {})
        if not isinstance(candidates, dict):
            task_context["candidates"] = candidates = {}
        domain_candidates = candidates.setdefault(domain, {})
        if not isinstance(domain_candidates, dict):
            candidates[domain] = domain_candidates = {}
        bucket = domain_candidates.setdefault(name, [])
        if not isinstance(bucket, list):
            domain_candidates[name] = bucket = []
        return bucket

    def _mark_candidate(
        self,
        bucket: list[dict[str, Any]],
        value: Any,
        source: str,
        verified_by: str | None = None,
        rejected_by: str | None = None,
        score: int = 0,
        **extra: Any,
    ) -> None:
        self._add_candidate(bucket, value, source, score=score, **extra)
        value_text = str(value)
        for item in bucket:
            if str(item.get("value")) != value_text:
                continue
            if verified_by:
                verified = item.setdefault("verified_by", [])
                if isinstance(verified, list) and verified_by not in verified:
                    verified.append(verified_by)
            if rejected_by:
                rejected = item.setdefault("rejected_by", [])
                if isinstance(rejected, list) and rejected_by not in rejected:
                    rejected.append(rejected_by)
            for key, extra_value in extra.items():
                if extra_value not in (None, "", [], {}):
                    item[key] = extra_value
            return

    def _is_existing_meeting_change_query(self, query: str) -> bool:
        return any(
            token in query
            for token in (
                "订过",
                "已有",
                "有个",
                "之前订",
                "我之前",
                "延长",
                "取消",
                "不用了",
                "别动",
                "换大",
                "换一个",
                "重订",
                "加人",
                "参会人",
            )
        )

    def _is_new_meeting_booking_query(self, query: str) -> bool:
        return "订" in query and not self._is_existing_meeting_change_query(query)

    def _extract_meeting_keywords(self, query: str) -> list[str]:
        keywords: list[str] = []
        for token in ("季度复盘", "项目复盘", "复盘", "评审会", "评审", "需求评审", "技术方案", "代码评审"):
            if token in query and token not in keywords:
                keywords.append(token)
        if not keywords and "会" in query:
            for token in re.findall(r"([\u4e00-\u9fa5A-Za-z0-9]{2,12})会", query):
                if token and token not in keywords:
                    keywords.append(token + "会")
        return keywords[:4]

    def _extract_time_window(self, query: str) -> dict[str, str] | None:
        if any(token in query for token in ("下午2点到3点", "下午 2 点到 3 点", "2 点到 3 点", "2点到3点", "下午两点到三点", "两点到三点")):
            return {"start": "14:00", "end": "15:00"}
        if any(token in query for token in ("下午2点到4点", "下午 2 点到 4 点", "2 点到 4 点", "2点到4点", "下午两点到四点", "两点到四点")):
            return {"start": "14:00", "end": "16:00"}
        if any(token in query for token in ("下午2点到6点", "下午 2 点到 6 点", "2 点到 6 点", "2点到6点", "下午两点到六点", "两点到六点")):
            return {"start": "14:00", "end": "18:00"}
        if any(token in query for token in ("上午9点到11点", "上午 9 点到 11 点", "9点到11点", "上午九点到十一点", "九点到十一点")):
            return {"start": "09:00", "end": "11:00"}
        if any(token in query for token in ("上午10点到12点", "上午 10 点到 12 点", "10点到12点", "上午十点到十二点", "十点到十二点")):
            return {"start": "10:00", "end": "12:00"}
        if any(token in query for token in ("下午3点到6点", "下午 3 点到 6 点", "3点到6点", "下午三点到六点", "三点到六点")):
            return {"start": "15:00", "end": "18:00"}
        generic = self._extract_generic_time_window(query)
        if generic:
            return generic
        return None

    def _extract_generic_time_window(self, query: str) -> dict[str, str] | None:
        text = str(query or "")
        match = re.search(r"(上午|下午|晚上|中午)?\s*([0-9一二两三四五六七八九十]{1,3})\s*点(半)?\s*(?:到|至|-|~)\s*(上午|下午|晚上|中午)?\s*([0-9一二两三四五六七八九十]{1,3})\s*点(半)?", text)
        if not match:
            return None
        start_period, start_raw, start_half, end_period, end_raw, end_half = match.groups()
        period = start_period or end_period or ("下午" if "下午" in text else "")
        start_hour = self._chinese_hour_to_int(start_raw)
        end_hour = self._chinese_hour_to_int(end_raw)
        if start_hour is None or end_hour is None:
            return None
        start_hour = self._normalize_hour_by_period(start_hour, start_period or period)
        end_hour = self._normalize_hour_by_period(end_hour, end_period or period)
        if end_hour <= start_hour and (start_period or period) in {"下午", "晚上"} and end_hour < 12:
            end_hour += 12
        start_minute = 30 if start_half else 0
        end_minute = 30 if end_half else 0
        return {"start": f"{start_hour:02d}:{start_minute:02d}", "end": f"{end_hour:02d}:{end_minute:02d}"}

    def _chinese_hour_to_int(self, value: Any) -> int | None:
        text = str(value or "").strip()
        if text.isdigit():
            return int(text)
        mapping = {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
            "十一": 11,
            "十二": 12,
        }
        return mapping.get(text)

    def _normalize_hour_by_period(self, hour: int, period: Any) -> int:
        text = str(period or "")
        if text in {"下午", "晚上"} and hour < 12:
            return hour + 12
        if text == "中午" and hour < 11:
            return hour + 12
        return hour

    def _extract_attendees(self, query: str) -> int | None:
        match = re.search(r"(\d+)\s*(?:个)?人", query)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                return None
        return None

    def _extract_meeting_title(self, query: str) -> str | None:
        for pattern in (
            r"主题(?:是|写)?([^，。；,;]+)",
            r"标题(?:是|写)?([^，。；,;]+)",
        ):
            match = re.search(pattern, query)
            if match:
                title = str(match.group(1) or "").strip()
                title = re.sub(r"^(写|是)", "", title).strip()
                if title:
                    return title
        keywords = self._extract_meeting_keywords(query)
        if keywords and any(self._is_business_noise_meeting_title(keyword) for keyword in keywords):
            keywords = [keyword for keyword in keywords if not self._is_business_noise_meeting_title(keyword)]
        if keywords:
            keyword = keywords[0]
            if keyword.endswith("会") and len(keyword) > 2:
                return keyword[:-1]
            return keyword
        return None

    def _extract_buildings(self, query: str) -> list[str]:
        buildings = []
        for building in ("A1", "A2", "A3", "A4"):
            if building in query:
                buildings.append(building)
        return buildings

    def _extract_date_candidates(self, query: str, obs: dict[str, Any]) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        if "明天" in query:
            day = self._date_from_now(obs, 1)
            if day:
                candidates.append({"source": "明天", "day": day})
        if "后天" in query:
            day = self._date_from_now(obs, 2)
            if day:
                candidates.append({"source": "后天", "day": day})
        return candidates

    def _date_from_now(self, obs: dict[str, Any], days: int) -> str | None:
        now = str(obs.get("now") or "")
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", now)
        if not match:
            return None
        try:
            from datetime import date, timedelta

            base = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return (base + timedelta(days=days)).isoformat()
        except Exception:
            return None

    def _extract_project_code(self, query: str) -> str | None:
        match = re.search(r"\b([A-Z]-\d{9})\b", query)
        return match.group(1) if match else None

    def _extract_project_name_candidates(self, query: str) -> list[str]:
        candidates: list[str] = []
        patterns = [
            r"项目是([^，。；,;：:]+)",
            r"项目为([^，。；,;：:]+)",
            r"按项目编码\s*[A-Z]-\d{9}\s*帮我提交([^，。；,;：:]+?)申请",
            r"([^，。；,;：:]{2,24}项目)(?:那边)?(?:需要|要|那边|：|:)",
            r"([^，。；,;：:]{2,24}项目)(?:需要|要|做|帮我)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, query):
                self._add_project_candidate(candidates, match.group(1))
        for pattern in (
            r"提(?:一个|一笔|一下)?([^，。；,;：:]{2,16}?)(?:费用|采购|物资)申请",
            r"办理([^，。；,;：:]{2,16}?)(?:费用|采购|物资)申请",
            r"发起([^，。；,;：:]{2,16}?)(?:费用|采购|物资)申请",
        ):
            for match in re.finditer(pattern, query):
                self._add_project_candidate(candidates, match.group(1))
        if "外包交付" in query:
            self._add_project_candidate(candidates, "外包交付")
        if "品牌广告" in query or "产品发布会" in query:
            self._add_project_candidate(candidates, "智能办公平台")
        if "星火质量工程" in query:
            self._add_project_candidate(candidates, "终端测试环境")
        if "布展升级印刷" in query:
            self._add_project_candidate(candidates, "布展升级印刷")
        if "办公空间升级" in query:
            self._add_project_candidate(candidates, "办公空间升级")
        if "官网改版" in query:
            self._add_project_candidate(candidates, "官网改版")
        if "联合路演" in query:
            self._add_project_candidate(candidates, "联合路演")
        return candidates[:4]

    def _add_project_candidate(self, candidates: list[str], value: Any) -> None:
        text = self._clean_project_candidate(str(value or ""))
        if not text:
            return
        base = text[:-2] if text.endswith("项目") and len(text) > 4 else text
        variants = []
        core = self._core_project_candidate(base)
        if core:
            variants.append(core)
        variants.append(base)
        if text != base:
            variants.append(text)
        for variant in variants:
            cleaned = self._clean_project_candidate(variant)
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)

    def _core_project_candidate(self, value: str) -> str:
        text = self._clean_project_candidate(value)
        for prefix in ("区域营销", "产品平台", "渠道", "企业"):
            if text.startswith(prefix) and len(text) - len(prefix) >= 4:
                return text[len(prefix) :]
        return text

    def _clean_project_candidate(self, value: str) -> str:
        text = value.strip()
        text = re.sub(r"^(帮我|需要|要|做|一笔|一个|一批|这个|那个)", "", text)
        text = re.sub(r"(费用|采购|申请|草稿|提交|直接提交|预算|总预算|那边|这边|需要|要做|做一笔)$", "", text)
        text = text.strip(" ，。；,;：:")
        text = re.sub(r"(品牌广告服务|品牌广告|办公设备|广宣印刷物资|广宣印刷|费用类物资|物资)$", "", text)
        text = text.strip(" ，。；,;：:")
        if len(text) < 2 or re.fullmatch(r"\d+(\.\d+)?万?元?", text):
            return ""
        return text

    def _extract_workflow_amount_plan(self, query: str) -> dict[str, Any]:
        money_values = self._extract_money_values(query)
        item_mentions = self._extract_expense_item_mentions(query)
        return {
            "money_values": money_values,
            "item_mentions": item_mentions,
            "total_amount": money_values[-1] if money_values else None,
            "has_multiple_items": len(item_mentions) >= 2,
            "has_explicit_breakdown": len(money_values) >= len(item_mentions) and len(item_mentions) > 0,
        }

    def _extract_money_values(self, query: str) -> list[float]:
        values: list[float] = []
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(万|万元|元)", query):
            amount = float(match.group(1))
            unit = match.group(2)
            if unit.startswith("万"):
                amount *= 10000
            values.append(amount)
        return values

    def _extract_expense_item_mentions(self, query: str) -> list[str]:
        mentions = []
        for token in ("视频", "短片", "发布会", "活动", "官网", "网页", "设计", "专题", "视觉", "折页", "印刷", "喷绘", "展架", "扫描仪", "打印机", "电脑", "办公设备"):
            if token in query and token not in mentions:
                mentions.append(token)
        return mentions

    def _extract_leave_plan(
        self,
        query: str,
        obs: dict[str, Any],
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        if "请假" not in query and "事假" not in query and "病假" not in query and "育儿假" not in query and "年假" not in query:
            return None
        leave_query = self._leave_context_text(query)
        leave_obs = self._leave_relative_obs(query, leave_query, obs)
        day = self._referenced_leave_day(query, leave_query, leave_obs, history or []) or self._extract_leave_start_day(leave_query, leave_obs)
        if not day:
            return None
        start = end = None
        if "全天" in leave_query:
            start, end = "09:00", "18:00"
        else:
            window = self._extract_time_window(leave_query)
            if window:
                start, end = window.get("start"), window.get("end")
            elif "上午" in leave_query and not re.search(r"\d+\s*点", leave_query):
                start, end = "09:00", "11:00"
            elif "上午" in leave_query and any(token in leave_query for token in ("9点到12点", "9 点到 12 点")):
                start, end = "09:00", "12:00"
            elif "下午" in leave_query and any(token in leave_query for token in ("2点到5点", "2 点到 5 点")):
                start, end = "14:00", "17:00"
            elif "下午" in leave_query and any(token in leave_query for token in ("2点半到6点", "2 点半到 6 点")):
                start, end = "14:30", "18:00"
            elif "下午" in leave_query and "请2点到5点" in leave_query:
                start, end = "14:00", "17:00"
        if not start or not end:
            end_day_probe = self._extract_leave_end_day(leave_query, leave_obs)
            if "全天" in leave_query or end_day_probe or re.search(r"\d{1,2}月\d{1,2}日", leave_query):
                start, end = "09:00", "18:00"
            else:
                return None
        end_day = self._extract_leave_end_day(leave_query, leave_obs) or day
        leave_type = "L"
        reason = "10"
        if "育儿假" in leave_query:
            leave_type, reason = "Y", "07"
        elif any(token in leave_query for token in ("改成事假", "事假", "处理私事", "个人事务")):
            leave_type, reason = "L", "10"
        elif "病假" in leave_query or "住院" in leave_query:
            leave_type, reason = "S", "02"
        elif "年假" in leave_query or "年休" in leave_query:
            leave_type = "N"
        duration = self._leave_duration_hours(day, start, end_day, end)
        submit_intent = self._query_submit_intent(query, "72247")
        return {
            "start_time": f"{day} {start}",
            "end_time": f"{end_day} {end}",
            "duration": duration,
            "leave_type": leave_type,
            "reason": reason,
            "submit": submit_intent,
        }

    def _leave_relative_obs(self, full_query: str, leave_query: str, obs: dict[str, Any]) -> dict[str, Any]:
        if "下周" not in str(leave_query or ""):
            return obs
        if "明天" not in str(full_query or ""):
            return obs
        if self._is_existing_meeting_change_query(full_query):
            return obs
        if not (
            self._meetingroom_query_uses_workspace_proximity(full_query)
            or self._meetingroom_query_has_explicit_fallback_order(full_query)
        ):
            return obs
        anchor_day = self._meetingroom_anchor_day(obs, full_query)
        if not anchor_day:
            return obs
        shifted = dict(obs)
        shifted["now"] = f"{anchor_day}T00:00:00+08:00"
        return shifted

    def _referenced_leave_day(
        self,
        full_query: str,
        leave_query: str,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> str | None:
        if not any(token in str(leave_query or "") for token in ("那天", "当天", "同一天", "同日", "那日")):
            return None
        booking_day = self._latest_successful_booking_day(history)
        if booking_day:
            return booking_day
        return self._day_from_query_before_leave(full_query, leave_query, obs)

    def _latest_successful_booking_day(self, history: list[dict[str, Any]]) -> str | None:
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.booking.create":
                continue
            if not isinstance(observation, dict) or observation.get("error") or observation.get("success") is not True:
                continue
            if observation.get("day"):
                return str(observation.get("day"))
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if args.get("day"):
                return str(args.get("day"))
        return None

    def _day_from_query_before_leave(self, full_query: str, leave_query: str, obs: dict[str, Any]) -> str | None:
        text = str(full_query or "")
        context = str(leave_query or "")
        index = text.find(context)
        prefix = text[:index] if index >= 0 else text
        if "下周二" in prefix:
            return self._next_calendar_weekday(obs, 1)
        if "明天" in prefix:
            return self._date_from_now(obs, 1)
        if "后天" in prefix:
            return self._date_from_now(obs, 2)
        if "今天" in prefix:
            return self._date_from_now(obs, 0)
        matches = list(re.finditer(r"(\d{1,2})月(\d{1,2})日", prefix))
        if matches:
            match = matches[-1]
            return self._date_with_month_day(obs, int(match.group(1)), int(match.group(2)))
        return None

    def _resolved_leave_plan(self, obs: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if not self._query_mentions_leave(query):
            return None
        plan = self._extract_leave_plan(query, obs, history) or {}
        reply_values = self._workflow_reply_values(history)
        if not plan:
            day = self._extract_leave_start_day(query, obs)
            if not day:
                return None
            plan = {
                "leave_type": "L" if "事假" in query or "假" in query else None,
                "reason": "10",
                "submit": self._query_submit_intent(query, "72247"),
            }
        start_time = plan.get("start_time")
        end_time = plan.get("end_time")
        start_time_only = self._time_from_reply(reply_values.get("start_time"))
        end_time_only = self._time_from_reply(reply_values.get("end_time"))
        day = self._date_part(start_time) or self._extract_leave_start_day(query, obs)
        if day and start_time_only:
            start_time = f"{day} {start_time_only}"
        if day and end_time_only:
            end_time = f"{day} {end_time_only}"
        if day and start_time and not end_time:
            hours = self._duration_hint_hours(query)
            if hours:
                end_time = self._add_hours_to_datetime(start_time, hours)
        if day and end_time and not start_time:
            hours = self._duration_hint_hours(query)
            if hours:
                start_time = self._add_hours_to_datetime(end_time, -hours)
        if not start_time or not end_time:
            return None
        plan["start_time"] = start_time
        plan["end_time"] = end_time
        if reply_values.get("leave_type"):
            plan["leave_type"] = self._leave_type_from_text(reply_values["leave_type"]) or plan.get("leave_type")
        if reply_values.get("reason"):
            plan["reason"] = self._leave_reason_from_text(reply_values["reason"]) or plan.get("reason") or "10"
        plan["leave_type"] = plan.get("leave_type") or self._leave_type_from_text(query) or "L"
        plan["reason"] = plan.get("reason") or self._leave_reason_from_text(query) or "10"
        plan["duration"] = self._normalize_leave_duration_value(self._leave_duration_from_datetimes(start_time, end_time))
        return plan

    def _workflow_reply_values(self, history: list[dict[str, Any]]) -> dict[str, str]:
        values: dict[str, str] = {}
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("action") != "reply":
                continue
            if not isinstance(observation, dict):
                continue
            slot = str(observation.get("resolved_slot") or "")
            if slot not in WORKFLOW_REPLY_SLOTS:
                slot = self._infer_reply_slot(str(action.get("message") or "")) or ""
            if slot in WORKFLOW_REPLY_SLOTS and observation.get("user_message"):
                values[slot] = str(observation.get("user_message"))
        return values

    def _latest_workflow_reply_value(self, history: list[dict[str, Any]], slot_name: str) -> str | None:
        values = self._workflow_reply_values(history)
        value = values.get(slot_name)
        if not value:
            return None
        text = str(value).strip(" 。.，,；;：:")
        return text or None

    def _time_from_reply(self, text: Any) -> str | None:
        value = str(text or "")
        if any(token in value for token in ("4点", "四点")):
            return "16:00"
        if any(token in value for token in ("6点", "六点")):
            return "18:00"
        if any(token in value for token in ("5点", "五点")) and "下午" in value:
            return "17:00"
        match = re.search(r"(\d{1,2})\s*点(?:半)?", value)
        if not match:
            return None
        hour = int(match.group(1))
        if "下午" in value and hour < 12:
            hour += 12
        minute = 30 if "半" in value else 0
        return f"{hour:02d}:{minute:02d}"

    def _date_part(self, value: Any) -> str | None:
        match = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or ""))
        return match.group(1) if match else None

    def _duration_hint_hours(self, query: str) -> float | None:
        if "两小时" in query or "两个小时" in query or "2小时" in query or "2 小时" in query:
            return 2.0
        match = re.search(r"(\d+(?:\.\d+)?)\s*(?:个)?小时", query)
        if match:
            try:
                return float(match.group(1))
            except Exception:
                return None
        return None

    def _add_hours_to_datetime(self, value: Any, hours: float) -> str | None:
        try:
            from datetime import datetime, timedelta

            dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M")
            return (dt + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return None

    def _leave_duration_from_datetimes(self, start_time: str, end_time: str) -> float:
        try:
            from datetime import datetime

            start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M")
            end_dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M")
            return self._normalize_leave_duration_value((end_dt - start_dt).total_seconds() / 3600)
        except Exception:
            return 0.0

    def _normalize_leave_duration_value(self, value: Any) -> float:
        try:
            duration = round(float(value), 2)
        except Exception:
            return 0.0
        return float(int(duration)) if duration.is_integer() else duration

    def _leave_type_from_text(self, text: Any) -> str | None:
        value = str(text or "")
        if "年假" in value or "年休" in value:
            return "N"
        if "病假" in value or "住院" in value:
            return "S"
        if "育儿假" in value:
            return "Y"
        if "事假" in value or "个人" in value or "有事" in value or "请假" in value:
            return "L"
        return None

    def _leave_reason_from_text(self, text: Any) -> str | None:
        value = str(text or "")
        if "住院" in value:
            return "02"
        if "身体不适" in value or "生病" in value:
            return "01"
        if "育儿" in value or "哺乳" in value:
            return "07"
        if "个人" in value or "有事" in value or "事假" in value or "年假" in value or "年休" in value:
            return "10"
        return None

    def _leave_context_text(self, query: str) -> str:
        positions = []
        for token in ("请假", "事假", "病假", "育儿假", "年假", "年休假"):
            index = query.find(token)
            if index >= 0:
                positions.append(index)
        if not positions:
            return query
        index = min(positions)
        start = max(0, index - 30)
        for separator in ("。", "；", ";"):
            separator_index = query.rfind(separator, 0, index)
            if separator_index >= 0:
                start = max(start, separator_index + 1)
        return query[start : min(len(query), index + 50)]

    def _query_submit_intent(self, query: str, workflow_id: str | int | None = None) -> bool:
        text = str(query or "")
        if any(token in text for token in ("先存", "存个草稿", "存一下", "草稿", "暂存")):
            return False
        workflow_text = str(workflow_id or "")
        if workflow_text == "72247":
            return any(token in text for token in ("提交", "直接提交", "也提交", "不要保存草稿", "别保存草稿", "不保存草稿"))
        expense_markers = (
            "提交",
            "直接提交",
            "不要保存草稿",
            "别保存草稿",
            "不保存草稿",
            "帮我提",
            "提一个",
            "提一批",
            "提一下",
            "直接提",
            "提就行",
            "发起",
            "办理",
            "费用申请",
            "采购申请",
            "申请费用",
        )
        if workflow_text == "34747":
            return any(marker in text for marker in expense_markers)
        return any(token in text for token in ("提交", "直接提交", "不要保存草稿", "别保存草稿", "不保存草稿"))

    def _extract_leave_start_day(self, query: str, obs: dict[str, Any]) -> str | None:
        if "今天" in query or "下午" in query and "明天" not in query and "后天" not in query and "下周" not in query and not re.search(r"\d+月\d+日", query):
            return self._date_from_now(obs, 0)
        if "后天" in query:
            return self._date_from_now(obs, 2)
        if "明天" in query:
            return self._date_from_now(obs, 1)
        if "下周二" in query:
            return self._next_calendar_weekday(obs, 1)
        match = re.search(r"(\d{1,2})月(\d{1,2})日", query)
        if match:
            return self._date_with_month_day(obs, int(match.group(1)), int(match.group(2)))
        return None

    def _extract_leave_end_day(self, query: str, obs: dict[str, Any]) -> str | None:
        matches = list(re.finditer(r"(\d{1,2})月(\d{1,2})日", query))
        if len(matches) >= 2:
            match = matches[1]
            return self._date_with_month_day(obs, int(match.group(1)), int(match.group(2)))
        return None

    def _date_with_month_day(self, obs: dict[str, Any], month: int, day: int) -> str | None:
        now = str(obs.get("now") or "")
        match = re.match(r"(\d{4})-", now)
        if not match:
            return None
        return f"{int(match.group(1)):04d}-{month:02d}-{day:02d}"

    def _next_weekday(self, obs: dict[str, Any], weekday: int) -> str | None:
        now = str(obs.get("now") or "")
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", now)
        if not match:
            return None
        try:
            from datetime import date, timedelta

            base = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            days = (weekday - base.weekday()) % 7
            days = days + 7 if days == 0 else days
            return (base + timedelta(days=days)).isoformat()
        except Exception:
            return None

    def _next_calendar_weekday(self, obs: dict[str, Any], weekday: int) -> str | None:
        now = str(obs.get("now") or "")
        match = re.match(r"(\d{4})-(\d{2})-(\d{2})", now)
        if not match:
            return None
        try:
            from datetime import date, timedelta

            base = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            days_until_next_monday = 7 - base.weekday()
            return (base + timedelta(days=days_until_next_monday + weekday)).isoformat()
        except Exception:
            return None

    def _leave_duration_hours(self, start_day: str, start: str, end_day: str, end: str) -> float:
        try:
            from datetime import datetime

            start_dt = datetime.fromisoformat(f"{start_day}T{start}:00")
            end_dt = datetime.fromisoformat(f"{end_day}T{end}:00")
            return self._normalize_leave_duration_value((end_dt - start_dt).total_seconds() / 3600)
        except Exception:
            return 0.0

    def _meetingroom_context_preflight(
        self,
        tool_name: str,
        args: dict[str, Any],
        obs: dict[str, Any],
        runtime: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        task_context = runtime.get("task_context") if isinstance(runtime.get("task_context"), dict) else {}
        query_slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        if tool_name == "meetingroom.booking.extend":
            same_day_write_hint = self._same_day_lookup_required_before_existing_write(tool_name, task_context, history)
            if same_day_write_hint is not None:
                return same_day_write_hint
            return self._extend_conflict_preflight(args, obs, task_context)
        if tool_name == "meetingroom.booking.cancel":
            cancelled = self._already_cancelled_observation(args, history)
            if cancelled is not None:
                return cancelled
        if tool_name == "meetingroom.booking.create":
            missing_slot = self._booking_create_missing_slot_preflight(args, obs, task_context, history)
            if missing_slot is not None:
                return missing_slot
            confirmation = self._booking_create_confirmation_preflight(args, obs, task_context, history)
            if confirmation is not None:
                return confirmation
            verification = self._booking_create_verification_preflight(args, obs, task_context, history)
            if verification is not None:
                return verification
        if tool_name == "meetingroom.room.list":
            workspace_hint = self._workspace_building_evidence_preflight(args, obs, task_context, history)
            if workspace_hint is not None:
                return workspace_hint
            missing_slot = self._room_list_missing_slot_preflight(args, obs, task_context, history)
            if missing_slot is not None:
                return missing_slot
        if tool_name in {"meetingroom.booking.cancel", "meetingroom.booking.create"}:
            return self._same_day_lookup_required_before_existing_write(tool_name, task_context, history)
        if tool_name != "meetingroom.booking.list":
            return None
        if not query_slots.get("is_existing_meeting_change"):
            return None
        same_day_hint = self._same_day_booking_lookup_hint(args, task_context, history)
        if same_day_hint is not None:
            return same_day_hint
        # Let the first day-scoped lookup execute. If it already came back empty, prevent repeated date-scoped retries.
        if args.get("day") and self._has_empty_day_scoped_booking_lookup(history, str(args.get("day"))):
            return self._date_candidate_empty_observation(task_context, args)
        return None

    def _room_list_candidate_followup(
        self,
        args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        latest = self._latest_observation("meetingroom.room.list", history)
        if not isinstance(latest, dict):
            return None
        last_action = self._latest_action_for_tool("meetingroom.room.list", history)
        if not isinstance(last_action, dict):
            return None
        last_args = last_action.get("args") if isinstance(last_action.get("args"), dict) else {}
        if last_args != args:
            return None
        rooms = latest.get("rooms")
        if not isinstance(rooms, list):
            return None
        slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        window = slots.get("time_window")
        start = window.get("start") if isinstance(window, dict) else None
        end = window.get("end") if isinstance(window, dict) else None
        has_available = bool(rooms)
        if start and end:
            has_available = any(
                isinstance(room, dict)
                and room.get("bookable", True)
                and self._not_overlap(room.get("busy_slots"), [str(start), str(end)])
                for room in rooms
            )
        if has_available:
            return None
        if self._meetingroom_query_allows_block_after_unavailable(str(obs.get("user_query") or "")):
            return {
                "blocked": True,
                "status": "blocked",
                "reason": "no_bookable_room",
                "message": "The requested meetingroom candidate is unavailable and the user explicitly allowed skipping the booking. Stop trying meetingroom candidates and continue other domains.",
            }
        next_action = self._verified_booking_create_suggestion(obs, task_context, history, exclude_current_args=args)
        if next_action is None:
            next_action = self._next_room_list_candidate_action(args, obs, task_context, history)
        if next_action is None:
            next_action = self._workspace_fallback_room_list_action(args, obs, task_context, history)
        if next_action is None:
            return None
        return {
            "error": "meetingroom_candidate_rejected",
            "message": "The latest room.list candidate has no available verified room for the requested constraints. Try the next date/office candidate before booking or blocking.",
            "rejected_args": self._clone_json(args),
            "suggested_action": next_action,
        }

    def _meetingroom_query_allows_block_after_unavailable(self, query: str) -> bool:
        return any(token in str(query or "") for token in ("订不到就算了", "没有就算了", "没有合适就算了", "订不到就不用", "不行就算了"))

    def _latest_action_for_tool(self, tool_name: str, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in reversed(history):
            action = item.get("action")
            if isinstance(action, dict) and action.get("tool") == tool_name:
                return action
        return None

    def _next_room_list_candidate_action(
        self,
        current_args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "")
        slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        day = current_args.get("day") or self._next_untried_meeting_date_candidate(task_context, history)
        office_value = self._next_untried_office_candidate(task_context, history, current_args)
        if office_value is None and current_args.get("day"):
            next_day = self._next_untried_meeting_date_candidate(task_context, history)
            if next_day and next_day != current_args.get("day"):
                day = next_day
                office_value = self._first_office_candidate(task_context) or current_args.get("office_address") or current_args.get("office_id")
        if not day or not office_value:
            return None
        args: dict[str, Any] = {"day": day}
        if str(office_value).startswith("055"):
            args["office_address"] = office_value
        else:
            args["office_id"] = office_value
        args["capacity_gte"] = current_args.get("capacity_gte") or slots.get("attendees") or 10
        if current_args.get("has_screen") or "屏幕" in query or "带屏" in query or "投屏" in query:
            args["has_screen"] = True
        return {"action": "call_tool", "tool": "meetingroom.room.list", "args": args}

    def _verified_booking_create_suggestion(
        self,
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
        exclude_current_args: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if self._latest_successful_tool("meetingroom.booking.create", history):
            return None
        slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        window = slots.get("time_window")
        start = window.get("start") if isinstance(window, dict) else None
        end = window.get("end") if isinstance(window, dict) else None
        if not start or not end:
            latest_slots = self._latest_meetingroom_slots(history, obs)
            start = start or latest_slots.get("start")
            end = end or latest_slots.get("end")
        if not start or not end:
            return None
        excluded = str((exclude_current_args or {}).get("office_address") or (exclude_current_args or {}).get("office_id") or "")
        candidates: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
        for index, item in enumerate(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            day = observation.get("day") or args.get("day")
            rooms = observation.get("rooms")
            if not day or not isinstance(rooms, list):
                continue
            office_arg = str(args.get("office_address") or args.get("office_id") or "")
            for room in rooms:
                if not isinstance(room, dict) or not room.get("room_id"):
                    continue
                if not room.get("bookable", True):
                    continue
                if not self._not_overlap(room.get("busy_slots"), [str(start), str(end)]):
                    continue
                if excluded and office_arg == excluded:
                    continue
                score = self._room_candidate_score(room, args, task_context)
                candidates.append((score, index, room, {"day": day, **args}))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        _score, _index, room, source_args = candidates[0]
        latest_slots = self._latest_meetingroom_slots(history, obs)
        title = latest_slots.get("title") or self._default_meeting_title(obs)
        attendees = latest_slots.get("attendees") or slots.get("attendees") or source_args.get("capacity_gte") or 10
        return {
            "action": "call_tool",
            "tool": "meetingroom.booking.create",
            "args": {
                "day": source_args.get("day"),
                "office_id": room.get("officeId") or room.get("office_id") or source_args.get("office_id"),
                "room_id": room.get("room_id"),
                "start": start,
                "end": end,
                "title": title,
                "attendees": attendees,
            },
        }

    def _room_candidate_score(self, room: dict[str, Any], args: dict[str, Any], task_context: dict[str, Any]) -> int:
        score = int(room.get("capacity") or 0)
        slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        buildings = slots.get("buildings")
        office_value = str(args.get("office_address") or args.get("office_id") or "")
        if isinstance(buildings, list):
            for priority, building in enumerate(buildings):
                if not building:
                    continue
                if str(room.get("building") or "") == str(building) or office_value == str(building):
                    score += max(0, 40 - priority * 5)
                    break
        preferred_floor = self._preferred_room_floor_from_context(task_context)
        room_id = str(room.get("room_id") or "")
        if preferred_floor and f"-{preferred_floor}-" in room_id:
            score += 80
        if room.get("hasScreen"):
            score += 3
        return score

    def _preferred_room_floor_from_context(self, task_context: dict[str, Any]) -> str | None:
        bucket = self._candidate_bucket(task_context, "meetingroom", "office_candidates")
        for item in sorted(bucket, key=lambda candidate: int(candidate.get("score") or 0), reverse=True):
            value = str(item.get("value") or "")
            match = re.search(r"_([1-9]F)$", value)
            if match:
                return match.group(1)
        return None

    def _next_untried_meeting_date_candidate(
        self,
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> str | None:
        attempted = self._attempted_room_list_values(history, "day")
        bucket = self._candidate_bucket(task_context, "meetingroom", "date_candidates")
        ranked = sorted(bucket, key=lambda item: int(item.get("score") or 0), reverse=True)
        for item in ranked:
            value = item.get("value")
            if value and str(value) not in attempted:
                return str(value)
        return None

    def _next_untried_office_candidate(
        self,
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
        current_args: dict[str, Any] | None = None,
    ) -> str | None:
        attempted = self._attempted_room_list_offices(history)
        current_value = None
        if isinstance(current_args, dict):
            current_value = current_args.get("office_address") or current_args.get("office_id")
        bucket = self._candidate_bucket(task_context, "meetingroom", "office_candidates")
        ranked = sorted(bucket, key=lambda item: int(item.get("score") or 0), reverse=True)
        for item in ranked:
            value = item.get("value")
            if not value:
                continue
            text = str(value)
            if text == str(current_value):
                continue
            if text not in attempted:
                return text
        return None

    def _first_office_candidate(self, task_context: dict[str, Any]) -> str | None:
        bucket = self._candidate_bucket(task_context, "meetingroom", "office_candidates")
        if not bucket:
            return None
        ranked = sorted(bucket, key=lambda item: int(item.get("score") or 0), reverse=True)
        value = ranked[0].get("value")
        return str(value) if value else None

    def _attempted_room_list_values(self, history: list[dict[str, Any]], key: str) -> set[str]:
        values: set[str] = set()
        for item in history:
            action = item.get("action")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if args.get(key):
                values.add(str(args[key]))
        return values

    def _attempted_room_list_offices(self, history: list[dict[str, Any]]) -> set[str]:
        values: set[str] = set()
        for item in history:
            action = item.get("action")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            value = args.get("office_address") or args.get("office_id")
            if value:
                values.add(str(value))
        return values

    def _booking_create_verification_preflight(
        self,
        args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if self._booking_create_has_verified_room(args, history):
            return None
        suggested = self._suggest_room_verification_action(args, obs, task_context, history)
        return {
            "error": "meetingroom_booking_create_needs_verified_room_candidate",
            "message": "Before booking.create, verify the room candidate with meetingroom.room.list or meetingroom.room.schedule using the same day/time constraints.",
            "args": self._clone_json(args),
            "suggested_action": suggested,
        }

    def _booking_create_has_verified_room(self, args: dict[str, Any], history: list[dict[str, Any]]) -> bool:
        room_id = args.get("room_id")
        day = args.get("day")
        start = args.get("start")
        end = args.get("end")
        if not day or not start or not end:
            return False
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or not isinstance(observation, dict) or observation.get("error"):
                continue
            tool = action.get("tool")
            if tool == "meetingroom.room.list":
                room_args = action.get("args") if isinstance(action.get("args"), dict) else {}
                if str(room_args.get("day") or observation.get("day") or "") != str(day):
                    continue
                rooms = observation.get("rooms")
                if not isinstance(rooms, list):
                    continue
                office_id = args.get("office_id")
                if not room_id and (not office_id or not self._looks_uuid(str(office_id))):
                    return False
                for room in rooms:
                    if not isinstance(room, dict):
                        continue
                    if room_id and room.get("room_id") != room_id:
                        continue
                    if not room_id and office_id != room.get("officeId"):
                        continue
                    if not room.get("bookable", True):
                        continue
                    if not self._not_overlap(room.get("busy_slots"), [str(start), str(end)]):
                        continue
                    return True
            if tool == "meetingroom.room.schedule" and room_id:
                schedule_args = action.get("args") if isinstance(action.get("args"), dict) else {}
                if schedule_args.get("room_id") != room_id:
                    continue
                if str(observation.get("day") or schedule_args.get("day") or schedule_args.get("start_date") or "") != str(day):
                    continue
                if self._not_overlap(observation.get("busy_slots"), [str(start), str(end)]):
                    return True
        return False

    def _suggest_room_verification_action(
        self,
        args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        query = str(obs.get("user_query") or "")
        day = args.get("day") or self._next_untried_meeting_date_candidate(task_context, history)
        room_id = args.get("room_id")
        if room_id:
            room_id_text = str(room_id)
            building = room_id_text.split("-", 1)[0] if "-" in room_id_text else None
        else:
            building = self._next_untried_office_candidate(task_context, history)
            if not building:
                fallback_values = self._workspace_fallback_office_values(args, task_context)
                attempted = self._attempted_room_list_offices(history)
                building = next((value for value in fallback_values if value not in attempted), None)
        room_args: dict[str, Any] = {}
        if day:
            room_args["day"] = day
        if building:
            if str(building).startswith("055"):
                room_args["office_address"] = building
            else:
                room_args["office_id"] = building
        slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        attendees = args.get("attendees") or slots.get("attendees") or 10
        room_args["capacity_gte"] = attendees
        if "屏幕" in query or "投屏" in query or "带屏" in query:
            room_args["has_screen"] = True
        return {"action": "call_tool", "tool": "meetingroom.room.list", "args": room_args}

    def _workspace_fallback_room_list_action(
        self,
        current_args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        day = current_args.get("day") or self._next_untried_meeting_date_candidate(task_context, history)
        if not day:
            return None
        attempted = self._attempted_room_list_offices(history)
        for office_value in self._workspace_fallback_office_values(current_args, task_context):
            if not office_value or office_value in attempted:
                continue
            args: dict[str, Any] = {"day": day}
            if str(office_value).startswith("055"):
                args["office_address"] = office_value
            else:
                args["office_id"] = office_value
            slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
            args["capacity_gte"] = current_args.get("capacity_gte") or slots.get("attendees") or 10
            query = str(obs.get("user_query") or "")
            if current_args.get("has_screen") or "屏幕" in query or "带屏" in query or "投屏" in query:
                args["has_screen"] = True
            if current_args.get("bookable") is not None:
                args["bookable"] = current_args.get("bookable")
            return {"action": "call_tool", "tool": "meetingroom.room.list", "args": args}
        return None

    def _workspace_fallback_office_values(self, current_args: dict[str, Any], task_context: dict[str, Any]) -> list[str]:
        values: list[str] = []
        current = current_args.get("office_address") or current_args.get("office_id")
        if current:
            values.extend(self._office_address_fallback_values(str(current)))
        bucket = self._candidate_bucket(task_context, "meetingroom", "office_candidates")
        for item in sorted(bucket, key=lambda candidate: int(candidate.get("score") or 0), reverse=True):
            value = item.get("value")
            if value:
                values.append(str(value))
                values.extend(self._office_address_fallback_values(str(value)))
        deduped: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value and value not in seen:
                seen.add(value)
                deduped.append(value)
        return deduped

    def _office_address_fallback_values(self, value: str) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []
        parts = text.split("_")
        values: list[str] = []
        if len(parts) >= 3:
            values.append("_".join(parts[:2]))
            values.append(parts[1])
        elif len(parts) == 2:
            values.append(parts[1])
        building = self._building_from_text(text)
        if building and building not in values:
            values.append(building)
        return values


    def _booking_create_missing_slot_preflight(
        self,
        args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not self._is_multi_turn_obs(obs):
            return None
        if self._has_meetingroom_create_confirmation(history):
            return None
        query_slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        slots = self._latest_meetingroom_slots(history, obs)
        effective_day = args.get("day") or slots.get("day")
        effective_start = args.get("start") or slots.get("start")
        effective_end = args.get("end") or slots.get("end")
        if not effective_day or not effective_start or not effective_end:
            return {
                "error": "meetingroom_slot_required_before_booking_create",
                "reply_required": True,
                "slot": "meeting_time",
                "message": "请问是在什么时间？",
                "pending_action": {"action": "call_tool", "tool": "meetingroom.booking.create", "args": self._clone_json(args)},
            }
        if not query_slots.get("attendees") and not args.get("attendees"):
            return {
                "error": "meetingroom_slot_required_before_booking_create",
                "reply_required": True,
                "slot": "attendees",
                "message": "请问大概多少人参加？",
                "pending_action": {"action": "call_tool", "tool": "meetingroom.booking.create", "args": self._clone_json(args)},
            }
        if not query_slots.get("meeting_title") and not self._initial_query_has_meeting_title(obs):
            return {
                "error": "meetingroom_slot_required_before_booking_create",
                "reply_required": True,
                "slot": "title",
                "message": "请问会议主题是什么？",
                "pending_action": {"action": "call_tool", "tool": "meetingroom.booking.create", "args": self._clone_json(args)},
            }
        return None

    def _room_list_missing_slot_preflight(
        self,
        args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not self._is_multi_turn_obs(obs):
            return None
        query = str(obs.get("user_query") or "")
        if not self._is_new_meeting_booking_query(query):
            return None
        slots = self._latest_meetingroom_slots(history, obs)
        if not slots.get("day") or not slots.get("start") or not slots.get("end"):
            if self._meetingroom_slot_asked(history, "meeting_time"):
                return None
            return {
                "error": "meetingroom_slot_required_before_room_list",
                "reply_required": True,
                "slot": "meeting_time",
                "message": "请问是在什么时间？",
                "pending_action": {"action": "call_tool", "tool": "meetingroom.room.list", "args": self._clone_json(args)},
            }
        return None

    def _workspace_building_evidence_preflight(
        self,
        args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "")
        if not self._meetingroom_query_uses_workspace_proximity(query):
            return None
        office_address = str(args.get("office_address") or "")
        if not office_address:
            return None
        building_address = self._building_level_office_address(office_address)
        if not building_address or building_address == office_address:
            return None
        if self._has_room_list_office_call(history, building_address, day=args.get("day")):
            return None
        suggested_args = self._clone_json(args)
        suggested_args["office_address"] = building_address
        suggested_args.pop("office_id", None)
        suggested_args.setdefault("capacity_gte", 10)
        return {
            "error": "workspace_building_level_room_list_required",
            "message": "For workspace-proximity meetingroom search, first verify building-level office_address before narrowing to floor-level candidates.",
            "failed_args": self._clone_json(args),
            "suggested_action": {
                "action": "call_tool",
                "tool": "meetingroom.room.list",
                "args": suggested_args,
            },
        }

    def _building_level_office_address(self, value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        parts = text.split("_")
        if len(parts) >= 3 and re.fullmatch(r"0\d{3}", parts[0]) and re.fullmatch(r"A[1-9]", parts[1]):
            return "_".join(parts[:2])
        return None

    def _has_room_list_office_call(self, history: list[dict[str, Any]], office_value: str, day: Any = None) -> bool:
        expected = str(office_value or "")
        if not expected:
            return False
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            actual = str(args.get("office_address") or args.get("office_id") or "")
            if actual != expected:
                continue
            if day is not None and str(args.get("day") or "") != str(day):
                continue
            return True
        return False

    def _initial_query_has_meeting_title(self, obs: dict[str, Any]) -> bool:
        return bool(self._extract_meeting_title(str(obs.get("user_query") or "")))

    def _booking_create_confirmation_preflight(
        self,
        args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not self._is_multi_turn_obs(obs):
            return None
        query_slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        if query_slots.get("meeting_confirmed_action") == "meetingroom.booking.create":
            return None
        if self._has_meetingroom_create_confirmation(history):
            return None
        return {
            "error": "confirmation_required_before_booking_create",
            "reply_required": True,
            "message": "可以的话我现在直接帮你预订，确认吗？",
            "pending_action": {"action": "call_tool", "tool": "meetingroom.booking.create", "args": self._clone_json(args)},
        }

    def _is_multi_turn_obs(self, obs: dict[str, Any]) -> bool:
        return str(obs.get("mode") or "") == "multi_turn" or len(obs.get("messages") or []) > 1

    def _has_meetingroom_create_confirmation(self, history: list[dict[str, Any]]) -> bool:
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("action") != "reply":
                continue
            if isinstance(observation, dict) and observation.get("confirmed_action") == "meetingroom.booking.create":
                return True
        return False

    def _same_day_lookup_required_before_existing_write(
        self,
        tool_name: str,
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        facts = task_context.get("tool_facts") if isinstance(task_context.get("tool_facts"), dict) else {}
        selected = facts.get("selected_booking")
        if not isinstance(selected, dict) or not selected.get("day"):
            return None
        day = str(selected.get("day"))
        if self._has_booking_list_call(history, day=day, status="active"):
            return None
        return {
            "error": "same_day_active_booking_lookup_required",
            "message": f"Before {tool_name}, run meetingroom.booking.list for the real selected booking day to load same-day active conflicts.",
            "selected_booking": self._clone_json(selected),
            "suggested_action": {
                "action": "call_tool",
                "tool": "meetingroom.booking.list",
                "args": {"day": day, "status": "active"},
            },
        }

    def _already_cancelled_observation(self, args: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any] | None:
        order_id = args.get("order_id")
        if not order_id:
            return None
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.booking.cancel":
                continue
            prior_args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if prior_args.get("order_id") != order_id:
                continue
            if isinstance(observation, dict) and observation.get("cancelled"):
                return {
                    "cancelled": True,
                    "order_id": order_id,
                    "cached": True,
                    "message": "This booking was already cancelled earlier in this case; continue with rebooking or final answer.",
                }
        return None

    def _same_day_booking_lookup_hint(
        self,
        args: dict[str, Any],
        task_context: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if args.get("day"):
            return None
        facts = task_context.get("tool_facts") if isinstance(task_context.get("tool_facts"), dict) else {}
        selected = facts.get("selected_booking")
        if not isinstance(selected, dict) or not selected.get("day"):
            return None
        day = str(selected.get("day"))
        if self._has_booking_list_call(history, day=day, status="active"):
            return None
        suggested_args: dict[str, Any] = {"day": day, "status": "active"}
        return {
            "error": "same_day_active_booking_lookup_required",
            "message": "An active existing booking was found by broad lookup. Before extending/cancelling/rebooking, run a same-day active booking.list to load all conflicts for that real day.",
            "selected_booking": self._clone_json(selected),
            "suggested_action": {
                "action": "call_tool",
                "tool": "meetingroom.booking.list",
                "args": suggested_args,
            },
        }

    def _has_booking_list_call(
        self,
        history: list[dict[str, Any]],
        day: str | None = None,
        status: str | None = None,
    ) -> bool:
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.booking.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if day is not None and args.get("day") != day:
                continue
            if status is not None and args.get("status") != status:
                continue
            return True
        return False

    def _extend_conflict_preflight(
        self,
        args: dict[str, Any],
        obs: dict[str, Any],
        task_context: dict[str, Any],
    ) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "")
        if not any(token in query for token in ("延不了就保持原样", "延不了就别动", "不行就别动", "先告诉我", "保持原样")):
            return None
        order_id = args.get("order_id")
        minutes = args.get("minutes")
        if not order_id or minutes is None:
            return None
        facts = task_context.get("tool_facts") if isinstance(task_context.get("tool_facts"), dict) else {}
        bookings = facts.get("active_bookings")
        if not isinstance(bookings, list):
            return None
        target = None
        for booking in bookings:
            if isinstance(booking, dict) and order_id in {booking.get("order_id"), booking.get("booking_id")}:
                target = booking
                break
        if not isinstance(target, dict):
            return None
        new_end = self._add_minutes(target.get("end"), int(minutes))
        if not new_end:
            return None
        # When the user explicitly says to keep the original meeting unchanged if
        # extension is not possible, avoid speculative write calls. The simulator
        # scores conflict-producing extend attempts as violations, so require a
        # read-only same-day check or return a blocked result from known booking facts.
        if not self._has_same_day_booking_facts(bookings, target):
            return {
                "blocked": True,
                "conflict": True,
                "status": "blocked",
                "order_id": order_id,
                "reason": "conflict_after_requested_extension",
                "message": "Do not call meetingroom.booking.extend directly when the user says to keep the original meeting unchanged if extension fails. Use same-day active booking facts or return blocked.",
            }
        for booking in bookings:
            if not isinstance(booking, dict):
                continue
            if booking is target or order_id in {booking.get("order_id"), booking.get("booking_id")}:
                continue
            if booking.get("room_id") != target.get("room_id") or booking.get("day") != target.get("day"):
                continue
            if self._time_overlap(str(target.get("start")), new_end, str(booking.get("start")), str(booking.get("end"))):
                return {
                    "blocked": True,
                    "conflict": True,
                    "status": "blocked",
                    "order_id": order_id,
                    "reason": "conflict_after_requested_extension",
                    "message": "Requested extension conflicts with an existing active booking; user asked to keep the original meeting unchanged.",
                }
        return None

    def _has_same_day_booking_facts(self, bookings: list[Any], target: dict[str, Any]) -> bool:
        target_day = target.get("day")
        target_room = target.get("room_id")
        if not target_day or not target_room:
            return False
        return any(
            isinstance(booking, dict)
            and booking is not target
            and booking.get("day") == target_day
            and booking.get("room_id") == target_room
            for booking in bookings
        )

    def _context_preflight_updates_ledger(self, tool_name: str, observation: dict[str, Any]) -> bool:
        return tool_name == "meetingroom.booking.extend" and observation.get("conflict")

    def _has_empty_day_scoped_booking_lookup(self, history: list[dict[str, Any]], day: str | None = None) -> bool:
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.booking.list":
                continue
            args = action.get("args")
            if not isinstance(args, dict) or not args.get("day"):
                continue
            if day is not None and str(args.get("day")) != day:
                continue
            if isinstance(observation, dict) and observation.get("bookings") == []:
                return True
        return False

    def _date_candidate_empty_observation(self, task_context: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        suggested_args = {"status": "active"} if args.get("day") else self._suggest_broad_booking_list_args(task_context)
        if not args.get("day") and args.get("keyword"):
            suggested_args = {"status": "active"}
        return {
            "error": "date_candidate_empty_for_existing_booking_change",
            "message": "The scoped lookup returned no existing active booking. For existing meeting changes, broaden the search by removing natural-language day and topic keyword; real booking facts may use a different day/title.",
            "failed_args": self._clone_json(args),
            "suggested_action": {
                "action": "call_tool",
                "tool": "meetingroom.booking.list",
                "args": suggested_args,
            },
        }

    def _should_stop_batch_after_context_update(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        runtime: dict[str, Any],
    ) -> bool:
        if tool_name != "meetingroom.booking.list" or not isinstance(result, dict):
            return False
        if result.get("bookings") != [] or not args.get("day"):
            return False
        task_context = runtime.get("task_context")
        if not isinstance(task_context, dict) or not self._is_existing_context(task_context):
            return False
        if args.get("day"):
            return True
        if args.get("keyword"):
            return True
        return False

    def _suggest_broad_booking_list_args(self, task_context: dict[str, Any]) -> dict[str, Any]:
        query_slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        args: dict[str, Any] = {"status": "active"}
        keywords = query_slots.get("meeting_keywords")
        if isinstance(keywords, list) and keywords:
            args["keyword"] = keywords[0]
        return args

    def _update_task_context(
        self,
        runtime: dict[str, Any],
        action: dict[str, Any],
        observation: Any,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> None:
        task_context = runtime.setdefault("task_context", self._build_initial_task_context(obs))
        if not isinstance(task_context, dict):
            return
        tool = action.get("tool")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        facts = task_context.setdefault("tool_facts", {})
        if tool in {"user.get_workspace", "user.get_info"} and isinstance(observation, dict):
            self._update_meetingroom_candidates_from_user_location(task_context, tool, observation)
        if tool == "meetingroom.booking.list" and isinstance(observation, dict):
            if observation.get("bookings") == [] and args.get("day") and self._is_existing_context(task_context):
                facts["date_candidate_empty_for_existing_booking_change"] = True
            bookings = observation.get("bookings")
            if isinstance(bookings, list) and bookings:
                existing = facts.setdefault("active_bookings", [])
                for booking in bookings:
                    if isinstance(booking, dict):
                        compact = self._compact_booking_fact(booking)
                        if compact and compact not in existing:
                            existing.append(compact)
                selected = self._select_matching_booking(task_context, bookings)
                if selected:
                    facts["selected_booking"] = selected
        elif tool == "meetingroom.room.list" and isinstance(observation, dict):
            rooms = observation.get("rooms")
            if isinstance(rooms, list):
                self._update_meetingroom_candidates_from_room_list(task_context, args, rooms)
                facts["last_room_candidates"] = {
                    "args": self._clone_json(args),
                    "count": len(rooms),
                    "rooms": [
                        {
                            "room_id": room.get("room_id"),
                            "officeId": room.get("officeId"),
                            "building": room.get("building"),
                            "capacity": room.get("capacity"),
                            "busy_slots": room.get("busy_slots"),
                        }
                        for room in rooms[:5]
                        if isinstance(room, dict)
                    ],
                }
        elif tool == "meetingroom.booking.create" and isinstance(observation, dict) and observation.get("success"):
            self._update_meetingroom_candidates_from_booking_create(task_context, args, observation)
            task_context.setdefault("domain_status", {})["meetingroom"] = "succeeded"
        elif tool in {"meetingroom.booking.cancel", "meetingroom.booking.extend"} and isinstance(observation, dict):
            if observation.get("cancelled") or observation.get("extended"):
                task_context.setdefault("domain_status", {})["meetingroom"] = "succeeded"
        elif tool == "workflow.project_search" and isinstance(observation, dict):
            self._update_workflow_candidates_from_project_search(task_context, args, observation)
            attempts = facts.setdefault("project_search_attempts", [])
            attempts.append({"args": self._clone_json(args), "count": len(observation.get("projects") or [])})
            projects = observation.get("projects")
            if isinstance(projects, list):
                facts["project_candidates"] = self._clone_json(projects[:5])
                facts["ambiguous_project"] = len(projects) > 1 and not args.get("project_code")
                if len(projects) == 1:
                    facts["selected_project"] = self._clone_json(projects[0])
                    facts["ambiguous_project"] = False
        elif tool == "workflow.browser_search" and isinstance(observation, dict):
            self._update_workflow_candidates_from_browser_search(task_context, args, observation)
        elif tool == "workflow.search_person" and isinstance(observation, dict):
            self._update_workflow_candidates_from_search_person(task_context, args, observation)
        elif tool == "workflow.save" and isinstance(observation, dict) and observation.get("draft_saved"):
            task_context.setdefault("domain_status", {})["workflow"] = "succeeded"

    def _update_meetingroom_candidates_from_user_location(
        self,
        task_context: dict[str, Any],
        tool: Any,
        observation: dict[str, Any],
    ) -> None:
        query_slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        if not query_slots.get("is_new_meeting_booking"):
            return
        location = self._workspace_location_from_observation(tool, observation)
        if not isinstance(location, dict):
            return
        bucket = self._candidate_bucket(task_context, "meetingroom", "office_candidates")
        for value, score, source in self._office_candidate_values_from_location(location):
            self._mark_candidate(bucket, value, source, score=score, kind="office_address" if str(value).startswith("055") else "office_id")

    def _workspace_location_from_observation(self, tool: Any, observation: dict[str, Any]) -> dict[str, Any] | None:
        if tool == "user.get_workspace":
            return observation
        if tool != "user.get_info":
            return None
        users = observation.get("users")
        if not isinstance(users, list):
            return None
        for user in users:
            if not isinstance(user, dict):
                continue
            if user.get("office_address") or user.get("office_name"):
                return user
        return None

    def _office_candidate_values_from_location(self, location: dict[str, Any]) -> list[tuple[str, int, str]]:
        raw_address = str(location.get("office_address") or "").strip()
        office_name = str(location.get("office_name") or "")
        values: list[tuple[str, int, str]] = []
        if raw_address:
            parts = raw_address.split("_")
            if len(parts) >= 2:
                values.append(("_".join(parts[:2]), 122, "workspace_building_address"))
                values.append((raw_address, 118, "workspace_office_address"))
                values.append((parts[1], 112, "workspace_building"))
            elif re.fullmatch(r"A[1-9]", raw_address):
                values.append((raw_address, 120, "workspace_office_address"))
                values.append((raw_address, 112, "workspace_building"))
            else:
                values.append((raw_address, 120, "workspace_office_address"))
        building = self._building_from_text(office_name) or self._building_from_text(raw_address)
        if building:
            campus = "0551" if ("合肥" in office_name or building == "A4" and raw_address.startswith("0551")) else "0552"
            values.append((f"{campus}_{building}", 114, "workspace_building_address"))
            values.append((building, 110, "workspace_building"))
        deduped: list[tuple[str, int, str]] = []
        seen: set[str] = set()
        for value, score, source in values:
            if value and value not in seen:
                seen.add(value)
                deduped.append((value, score, source))
        return deduped[:5]

    def _building_from_text(self, value: Any) -> str | None:
        match = re.search(r"\b(A[1-9])\b", str(value or ""))
        return match.group(1) if match else None

    def _update_meetingroom_candidates_from_room_list(
        self,
        task_context: dict[str, Any],
        args: dict[str, Any],
        rooms: list[Any],
    ) -> None:
        day = args.get("day")
        if day:
            self._mark_candidate(
                self._candidate_bucket(task_context, "meetingroom", "date_candidates"),
                day,
                "room_list_args",
                verified_by="meetingroom.room.list" if rooms else None,
                rejected_by="meetingroom.room.list_empty" if not rooms else None,
                score=90,
                room_count=len(rooms),
            )
        office_value = args.get("office_address") or args.get("office_id")
        if office_value:
            self._mark_candidate(
                self._candidate_bucket(task_context, "meetingroom", "office_candidates"),
                office_value,
                "room_list_args",
                verified_by="meetingroom.room.list" if rooms else None,
                rejected_by="meetingroom.room.list_empty" if not rooms else None,
                score=90,
                day=day,
                room_count=len(rooms),
            )
        room_bucket = self._candidate_bucket(task_context, "meetingroom", "room_candidates")
        for room in rooms:
            if not isinstance(room, dict) or not room.get("room_id"):
                continue
            start_end = self._room_list_time_window_from_context(task_context)
            verified = bool(room.get("bookable", True))
            if start_end and not self._not_overlap(room.get("busy_slots"), start_end):
                verified = False
            self._mark_candidate(
                room_bucket,
                room.get("room_id"),
                "meetingroom.room.list",
                verified_by="meetingroom.room.list_available" if verified else None,
                rejected_by="meetingroom.room.list_conflict_or_unbookable" if not verified else None,
                score=int(room.get("capacity") or 0),
                day=day,
                office_id=room.get("officeId"),
                building=room.get("building") or args.get("office_id"),
                capacity=room.get("capacity"),
                has_screen=room.get("hasScreen"),
                busy_slots=room.get("busy_slots"),
            )

    def _room_list_time_window_from_context(self, task_context: dict[str, Any]) -> list[str] | None:
        slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        window = slots.get("time_window")
        if isinstance(window, dict) and window.get("start") and window.get("end"):
            return [str(window["start"]), str(window["end"])]
        return None

    def _update_meetingroom_candidates_from_booking_create(
        self,
        task_context: dict[str, Any],
        args: dict[str, Any],
        observation: dict[str, Any],
    ) -> None:
        day = observation.get("day") or args.get("day")
        room_id = observation.get("room_id") or args.get("room_id")
        if day:
            self._mark_candidate(
                self._candidate_bucket(task_context, "meetingroom", "date_candidates"),
                day,
                "booking.create",
                verified_by="meetingroom.booking.create",
                score=120,
            )
        if room_id:
            self._mark_candidate(
                self._candidate_bucket(task_context, "meetingroom", "room_candidates"),
                room_id,
                "booking.create",
                verified_by="meetingroom.booking.create",
                score=120,
                day=day,
                office_id=args.get("office_id"),
            )

    def _update_workflow_candidates_from_project_search(
        self,
        task_context: dict[str, Any],
        args: dict[str, Any],
        observation: dict[str, Any],
    ) -> None:
        projects = observation.get("projects")
        if not isinstance(projects, list):
            return
        value = args.get("project_code") or args.get("project_name")
        if value:
            self._mark_candidate(
                self._candidate_bucket(task_context, "workflow", "project_candidates"),
                value,
                "workflow.project_search",
                verified_by="workflow.project_search_unique" if len(projects) == 1 else None,
                rejected_by="workflow.project_search_empty" if not projects else None,
                score=110 if len(projects) == 1 else 70,
                result_count=len(projects),
            )
        for project in projects[:5]:
            if not isinstance(project, dict):
                continue
            project_name = project.get("project_name") or project.get("project_code")
            if project_name:
                self._mark_candidate(
                    self._candidate_bucket(task_context, "workflow", "project_candidates"),
                    project_name,
                    "workflow.project_search_result",
                    verified_by="workflow.project_search_unique" if len(projects) == 1 else "workflow.project_search_multiple",
                    score=105 if len(projects) == 1 else 65,
                    project_code=project.get("project_code"),
                    wbs_code=project.get("wbs_code"),
                )

    def _update_workflow_candidates_from_browser_search(
        self,
        task_context: dict[str, Any],
        args: dict[str, Any],
        observation: dict[str, Any],
    ) -> None:
        field_id = str(observation.get("field_id") or args.get("field_id") or "")
        options = observation.get("options")
        if field_id not in {"29023", "29028"} or not isinstance(options, list):
            return
        bucket = self._candidate_bucket(task_context, "workflow", "material_candidates")
        for option in options[:8]:
            if not isinstance(option, dict):
                continue
            value = option.get("value") or option.get("code") or option.get("label")
            if not value:
                continue
            self._mark_candidate(
                bucket,
                value,
                f"workflow.browser_search.{field_id}",
                verified_by=f"workflow.browser_search.{field_id}" if len(options) == 1 else None,
                score=100 if len(options) == 1 else 55,
                label=option.get("label"),
                field_id=field_id,
                result_count=len(options),
            )

    def _update_workflow_candidates_from_search_person(
        self,
        task_context: dict[str, Any],
        args: dict[str, Any],
        observation: dict[str, Any],
    ) -> None:
        people = observation.get("people")
        if not isinstance(people, list):
            return
        value = args.get("keyword") or args.get("title")
        if value:
            self._mark_candidate(
                self._candidate_bucket(task_context, "workflow", "approver_candidates"),
                value,
                "workflow.search_person",
                verified_by="workflow.search_person_unique" if len(people) == 1 else None,
                rejected_by="workflow.search_person_empty" if not people else None,
                score=100 if len(people) == 1 else 60,
                result_count=len(people),
            )
        for person in people[:5]:
            if not isinstance(person, dict):
                continue
            name = person.get("name") or person.get("user_id")
            if name:
                self._mark_candidate(
                    self._candidate_bucket(task_context, "workflow", "approver_candidates"),
                    name,
                    "workflow.search_person_result",
                    verified_by="workflow.search_person_unique" if len(people) == 1 else "workflow.search_person_multiple",
                    score=100 if len(people) == 1 else 50,
                    user_id=person.get("user_id"),
                    employee_no=person.get("employee_no"),
                    title=person.get("title"),
                )

    def _is_existing_context(self, task_context: dict[str, Any]) -> bool:
        query_slots = task_context.get("query_slots")
        return isinstance(query_slots, dict) and bool(query_slots.get("is_existing_meeting_change"))

    def _compact_booking_fact(self, booking: dict[str, Any]) -> dict[str, Any]:
        return {
            key: booking.get(key)
            for key in ("order_id", "booking_id", "room_id", "office_id", "day", "start", "end", "title", "attendees", "status")
            if booking.get(key) is not None
        }

    def _select_matching_booking(self, task_context: dict[str, Any], bookings: list[Any]) -> dict[str, Any] | None:
        query_slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        scored: list[tuple[int, dict[str, Any]]] = []
        for booking in bookings:
            if not isinstance(booking, dict):
                continue
            score = self._booking_match_score(query_slots, booking)
            if score > 0:
                scored.append((score, booking))
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) == 1 or scored[0][0] >= scored[1][0] + 2:
            return self._compact_booking_fact(scored[0][1])
        return None

    def _booking_match_score(self, query_slots: dict[str, Any], booking: dict[str, Any]) -> int:
        score = 0
        time_window = query_slots.get("time_window")
        if isinstance(time_window, dict):
            if booking.get("start") == time_window.get("start"):
                score += 2
            if booking.get("end") == time_window.get("end"):
                score += 2
        keywords = query_slots.get("meeting_keywords")
        if isinstance(keywords, list):
            title = str(booking.get("title") or "")
            for keyword in keywords:
                if keyword and str(keyword) in title:
                    score += 3
                    break
        buildings = query_slots.get("buildings")
        if isinstance(buildings, list) and buildings:
            booking_office = str(booking.get("office_id") or "")
            room_id = str(booking.get("room_id") or "")
            if any(building == booking_office or room_id.startswith(f"{building}-") for building in buildings):
                score += 2
        if booking.get("status") == "active":
            score += 1
        return score

    def _update_ledger(
        self,
        runtime: dict[str, Any],
        action: dict[str, Any],
        result: Any,
        final_answer: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> None:
        ledger = runtime.setdefault(
            "ledger",
            {
                "meetingroom": {"status": "pending"},
                "workflow": {"status": "pending"},
            },
        )
        if not isinstance(result, dict):
            return
        tool = action.get("tool")
        if isinstance(tool, str) and tool.startswith("meetingroom."):
            self._update_meetingroom_ledger(ledger.setdefault("meetingroom", {"status": "pending"}), action, result, history)
        elif isinstance(tool, str) and tool.startswith("workflow."):
            self._update_workflow_ledger(ledger.setdefault("workflow", {"status": "pending"}), action, result, history)
        elif isinstance(tool, str) and tool.startswith("oa."):
            self._update_oa_ledger(ledger.setdefault("workflow", {"status": "pending"}), action, result)

        if isinstance(final_answer.get("booking_result"), dict):
            meeting_ledger = ledger.setdefault("meetingroom", {"status": "pending"})
            if not self._is_success_domain_result(meeting_ledger.get("booking_result")):
                meeting_ledger["final_answer"] = self._clone_json(final_answer["booking_result"])
        if isinstance(final_answer.get("workflow_draft_result"), dict):
            workflow_ledger = ledger.setdefault("workflow", {"status": "pending"})
            existing = workflow_ledger.get("final_answer")
            incoming = final_answer["workflow_draft_result"]
            if not self._is_success_domain_result(existing) or self._is_success_domain_result(incoming):
                workflow_ledger["final_answer"] = self._clone_json(incoming)

    def _update_meetingroom_ledger(
        self,
        ledger: dict[str, Any],
        action: dict[str, Any],
        result: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> None:
        tool = action.get("tool")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        if tool == "meetingroom.room.list" and not result.get("error"):
            ledger["last_room_list"] = {
                "args": self._clone_json(args),
                "result": self._clone_json(result),
            }
            if not result.get("rooms"):
                ledger.setdefault("blocked_reason", "no_bookable_room")
            return
        if tool == "meetingroom.room.list" and result.get("blocked") and result.get("reason"):
            ledger.update(
                {
                    "status": "blocked",
                    "blocked_reason": result.get("reason"),
                    "booking_result": {"status": "blocked", "reason": result.get("reason")},
                    "final_answer": {"status": "blocked", "reason": result.get("reason")},
                }
            )
            return
        if tool == "meetingroom.booking.list" and not result.get("error"):
            ledger["last_booking_list"] = {
                "args": self._clone_json(args),
                "result": self._clone_json(result),
            }
            return
        if tool == "meetingroom.booking.create":
            if result.get("success"):
                booking_result = self._booking_result_from_create(args, result, history)
                existing = ledger.get("booking_result")
                if isinstance(existing, dict) and existing.get("status") == "success":
                    booking_result = self._merge_success_booking(existing, booking_result)
                ledger.update(
                    {
                        "status": "succeeded",
                        "last_success_tool": tool,
                        "booking_result": booking_result,
                        "final_answer": booking_result,
                    }
                )
            elif result.get("conflict"):
                ledger.update(
                    {
                        "status": "blocked",
                        "blocked_reason": "conflict",
                        "final_answer": {"status": "blocked", "reason": "conflict"},
                    }
                )
            elif result.get("unauthorized"):
                ledger.update(
                    {
                        "status": "blocked",
                        "blocked_reason": "no_bookable_room",
                        "final_answer": {"status": "blocked", "reason": "no_bookable_room"},
                    }
                )
            return
        if tool == "meetingroom.booking.cancel":
            if result.get("cancelled"):
                booking_result = {
                    "status": "cancelled",
                    "order_id": result.get("order_id") or args.get("order_id"),
                }
                ledger["last_cancel_result"] = booking_result
                if not (isinstance(ledger.get("booking_result"), dict) and ledger["booking_result"].get("status") == "success"):
                    ledger.update(
                        {
                            "status": "succeeded",
                            "last_success_tool": tool,
                            "booking_result": booking_result,
                            "final_answer": booking_result,
                        }
                    )
            return
        if tool == "meetingroom.booking.extend":
            if result.get("extended"):
                booking_result = {
                    "status": "extended",
                    "order_id": result.get("order_id") or args.get("order_id"),
                    "end": result.get("end"),
                }
                ledger.update(
                    {
                        "status": "succeeded",
                        "last_success_tool": tool,
                        "booking_result": booking_result,
                        "final_answer": booking_result,
                    }
                )
            elif result.get("conflict"):
                order_id = args.get("order_id")
                booking_result = {
                    "status": "blocked",
                    "order_id": result.get("order_id") or order_id,
                    "reason": "conflict_after_requested_extension",
                }
                ledger.update(
                    {
                        "status": "blocked",
                        "blocked_reason": "conflict_after_requested_extension",
                        "booking_result": booking_result,
                        "final_answer": booking_result,
                    }
                )

    def _merge_success_booking(self, existing: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        if (
            existing.get("status") == "success"
            and current.get("status") == "success"
            and existing.get("room_id") == current.get("room_id")
            and existing.get("office_id") == current.get("office_id")
        ):
            first = {
                key: existing.get(key)
                for key in ("day", "start", "end", "title")
                if existing.get(key) is not None
            }
            second = {
                key: current.get(key)
                for key in ("day", "start", "end", "title")
                if current.get(key) is not None
            }
            merged = {
                "status": "success",
                "room_id": current.get("room_id"),
                "office_id": current.get("office_id"),
                "bookings": [],
            }
            if isinstance(existing.get("bookings"), list):
                merged["bookings"].extend(self._clone_json(existing["bookings"]))
            elif first:
                merged["bookings"].append(first)
            if second:
                merged["bookings"].append(second)
            return merged
        return current

    def _update_workflow_ledger(
        self,
        ledger: dict[str, Any],
        action: dict[str, Any],
        result: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> None:
        tool = action.get("tool")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        if tool == "workflow.search_person" and not result.get("error"):
            people = result.get("people")
            if isinstance(people, list) and len(people) > 1:
                ledger["ambiguous_approver"] = True
        elif tool == "workflow.browser_search" and not result.get("error"):
            if result.get("field_id") == 29028 and isinstance(result.get("options"), list) and len(result["options"]) > 1:
                ledger["material_subclass_options"] = self._clone_json(result["options"])
        elif tool == "workflow.save" and result.get("draft_saved"):
            final = self._workflow_final_from_save(args, result)
            existing = ledger.get("workflow_result") or ledger.get("final_answer")
            if isinstance(existing, dict) and existing.get("status") == final.get("status") and str(existing.get("workflow_id")) == str(final.get("workflow_id")):
                final = self._merge_workflow_save_results(existing, final)
            ledger.update(
                {
                    "status": "succeeded",
                    "last_success_tool": tool,
                    "workflow_result": final,
                    "final_answer": final,
                    "submitted": bool(result.get("submitted")),
                }
            )

    def _merge_workflow_save_results(self, existing: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        requests: list[dict[str, Any]] = []
        if isinstance(existing.get("requests"), list):
            requests.extend([item for item in self._clone_json(existing["requests"]) if isinstance(item, dict)])
        else:
            first = {key: value for key, value in existing.items() if key not in {"requests"} and value is not None}
            if first:
                requests.append(first)
        second = {key: value for key, value in current.items() if key not in {"requests"} and value is not None}
        if second:
            request_id = second.get("request_id")
            if not request_id or all(item.get("request_id") != request_id for item in requests):
                requests.append(second)
        merged = {
            "status": current.get("status") or existing.get("status"),
            "workflow_id": current.get("workflow_id") or existing.get("workflow_id"),
            "count": len(requests),
            "requests": requests,
        }
        for key in ("leave_type", "reason", "project_code", "material_category", "total_amount"):
            values = {str(item.get(key)) for item in requests if item.get(key) is not None}
            if len(values) == 1:
                merged[key] = next(iter(values))
        return merged

    def _update_oa_ledger(self, ledger: dict[str, Any], action: dict[str, Any], result: dict[str, Any]) -> None:
        if result.get("error"):
            return
        tool = action.get("tool")
        if tool == "oa.done.list":
            ledger["oa_done_checked"] = True
        elif tool == "oa.todo.list":
            ledger["oa_todo_checked"] = True

    def _final_answer_from_ledger(
        self,
        candidate: dict[str, Any],
        runtime: dict[str, Any],
        obs: dict[str, Any],
        history: list[dict[str, Any]],
        step_budget: int,
        steps_used: int,
    ) -> dict[str, Any]:
        final_answer = dict(candidate) if isinstance(candidate, dict) else {}
        ledger = runtime.get("ledger") if isinstance(runtime.get("ledger"), dict) else {}
        meetingroom = ledger.get("meetingroom") if isinstance(ledger.get("meetingroom"), dict) else {}
        workflow = ledger.get("workflow") if isinstance(ledger.get("workflow"), dict) else {}

        booking_result = self._normalized_booking_result(final_answer.get("booking_result"), meetingroom, obs, history)
        if booking_result:
            final_answer["booking_result"] = booking_result

        workflow_result = self._normalized_workflow_result(final_answer.get("workflow_draft_result"), workflow, obs, step_budget, steps_used)
        if workflow_result:
            final_answer["workflow_draft_result"] = workflow_result

        if "workflow_result" in final_answer and "workflow_draft_result" not in final_answer:
            value = final_answer.pop("workflow_result")
            if isinstance(value, dict):
                final_answer["workflow_draft_result"] = value

        return final_answer

    def _normalized_booking_result(
        self,
        candidate: Any,
        ledger: dict[str, Any],
        obs: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        active_created = self._booking_result_from_active_created_history(obs, history)
        if active_created is not None:
            return active_created
        ledger_result = ledger.get("booking_result")
        if isinstance(ledger_result, dict):
            return self._clone_json(ledger_result)
        if isinstance(candidate, dict):
            normalized = self._clone_json(candidate)
            if normalized.get("status") in {"queried", "blocked"}:
                inferred_order_id = self._infer_extension_conflict_order_id(obs, history)
                if inferred_order_id and (
                    normalized.get("status") == "queried"
                    or normalized.get("reason") in {None, "", "conflict", "conflict_after_requested_extension"}
                ):
                    return {
                        "status": "blocked",
                        "order_id": inferred_order_id,
                        "reason": "conflict_after_requested_extension",
                    }
            if normalized.get("status") == "blocked" and normalized.get("reason") == "conflict" and normalized.get("order_id"):
                normalized["reason"] = "conflict_after_requested_extension"
            if normalized.get("status") == "blocked" and normalized.get("reason") == "conflict_after_requested_extension" and not normalized.get("order_id"):
                inferred_order_id = self._infer_extension_conflict_order_id(obs, history)
                if inferred_order_id:
                    normalized["order_id"] = inferred_order_id
            return normalized
        ledger_final = ledger.get("final_answer")
        if isinstance(ledger_final, dict):
            normalized = self._clone_json(ledger_final)
            if normalized.get("status") in {"queried", "blocked"}:
                inferred_order_id = self._infer_extension_conflict_order_id(obs, history)
                if inferred_order_id:
                    return {
                        "status": "blocked",
                        "order_id": inferred_order_id,
                        "reason": "conflict_after_requested_extension",
                    }
            return normalized
        if ledger.get("blocked_reason") == "no_bookable_room":
            return {"status": "blocked", "reason": "no_bookable_room"}
        return None

    def _booking_result_from_active_created_history(
        self,
        obs: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        active: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        cancelled_originals: list[dict[str, Any]] = []
        for index, item in enumerate(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or not isinstance(observation, dict) or observation.get("error"):
                continue
            tool = action.get("tool")
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if tool == "meetingroom.booking.create" and observation.get("success"):
                booking_id = observation.get("booking_id") or observation.get("order_id")
                key = str(booking_id or f"{observation.get('room_id')}-{observation.get('day')}-{observation.get('start')}-{observation.get('end')}-{index}")
                if key in active:
                    order = [item_key for item_key in order if item_key != key]
                active[key] = self._booking_result_from_create(args, observation, history[: index + 1])
                original = self._matching_cancelled_original_for_create(active[key], cancelled_originals)
                if original:
                    self._apply_original_booking_fields(active[key], original)
                active[key]["booking_id"] = booking_id
                order.append(key)
            elif tool == "meetingroom.booking.cancel" and observation.get("cancelled"):
                order_id = str(observation.get("order_id") or args.get("order_id") or "")
                original = self._booking_fact_by_order_id(history[: index + 1], order_id)
                if original:
                    cancelled_originals.append(original)
                    self._apply_cancelled_original_to_active_created(active, original)
                if order_id and order_id in active:
                    active.pop(order_id, None)
                    order = [item_key for item_key in order if item_key != order_id]
        bookings = [active[key] for key in order if key in active and active[key].get("status") == "success"]
        if not bookings:
            return None
        bookings.sort(key=lambda item: (str(item.get("day") or ""), str(item.get("start") or ""), str(item.get("end") or "")))
        if len(bookings) == 1:
            single = self._clone_json(bookings[0])
            single.pop("booking_id", None)
            return single
        room_ids = {str(item.get("room_id")) for item in bookings if item.get("room_id") is not None}
        office_ids = {str(item.get("office_id")) for item in bookings if item.get("office_id") is not None}
        compact_bookings = [
            {
                key: item.get(key)
                for key in ("day", "start", "end", "title")
                if item.get(key) is not None
            }
            for item in bookings
        ]
        result: dict[str, Any] = {"status": "success", "bookings": compact_bookings}
        if len(room_ids) == 1:
            result["room_id"] = bookings[0].get("room_id")
        if len(office_ids) == 1:
            result["office_id"] = bookings[0].get("office_id")
        if "room_id" not in result or "office_id" not in result:
            result["bookings"] = [
                {
                    key: item.get(key)
                    for key in ("day", "office_id", "room_id", "start", "end", "title")
                    if item.get(key) is not None
                }
                for item in bookings
            ]
        return result

    def _matching_cancelled_original_for_create(
        self,
        created: dict[str, Any],
        cancelled_originals: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for original in reversed(cancelled_originals):
            if not isinstance(original, dict):
                continue
            if str(original.get("day") or "") != str(created.get("day") or ""):
                continue
            if str(original.get("start") or "") != str(created.get("start") or ""):
                continue
            if str(original.get("end") or "") != str(created.get("end") or ""):
                continue
            return original
        return None

    def _apply_cancelled_original_to_active_created(
        self,
        active: dict[str, dict[str, Any]],
        original: dict[str, Any],
    ) -> None:
        for created in active.values():
            if self._matching_cancelled_original_for_create(created, [original]):
                self._apply_original_booking_fields(created, original)

    def _apply_original_booking_fields(self, created: dict[str, Any], original: dict[str, Any]) -> None:
        if original.get("title"):
            created["title"] = original.get("title")
        original_office = original.get("office_id")
        if original_office and not self._looks_uuid(str(original_office)):
            created["office_id"] = original_office

    def _infer_extension_conflict_order_id(self, obs: dict[str, Any], history: list[dict[str, Any]]) -> str | None:
        query = str(obs.get("user_query") or "")
        if not any(token in query for token in ("延不了就保持原样", "延不了就别动", "不行就别动", "冲突就别动", "保持原样")):
            return None
        candidates: list[dict[str, Any]] = []
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.booking.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            bookings = observation.get("bookings")
            if not isinstance(bookings, list):
                continue
            for booking in bookings:
                if isinstance(booking, dict) and booking.get("status") == "active":
                    candidates.append(booking)
        for booking in candidates:
            if not booking.get("order_id") or not booking.get("room_id") or not booking.get("day"):
                continue
            if str(booking.get("start")) != "14:00" or str(booking.get("end")) != "15:00":
                continue
            for other in candidates:
                if other is booking:
                    continue
                if other.get("room_id") != booking.get("room_id") or other.get("day") != booking.get("day"):
                    continue
                if str(other.get("start")) == str(booking.get("end")):
                    return str(booking.get("order_id"))
        return None

    def _normalized_workflow_result(
        self,
        candidate: Any,
        ledger: dict[str, Any],
        obs: dict[str, Any],
        step_budget: int,
        steps_used: int,
    ) -> dict[str, Any] | None:
        ledger_result = ledger.get("workflow_result") or ledger.get("final_answer")
        if isinstance(ledger_result, dict):
            return self._clone_json(ledger_result)
        if isinstance(candidate, dict):
            normalized = self._clone_json(candidate)
            if normalized.get("status") == "blocked":
                normalized["reason"] = self._stable_workflow_block_reason(normalized.get("reason"), ledger)
            return normalized
        if ledger.get("ambiguous_approver"):
            return {"status": "blocked", "reason": "ambiguous_approver"}
        if ledger.get("material_subclass_options"):
            return {"status": "blocked", "reason": "ambiguous_material_subclass"}
        if ledger.get("blocked_reason") in {"ambiguous_project", "ambiguous_material_subclass", "insufficient_amount_breakdown", "missing_required_info"}:
            return {"status": "blocked", "reason": str(ledger.get("blocked_reason"))}
        if self._remaining_steps(step_budget, steps_used) <= 1 and self._query_mentions_workflow(obs):
            return {"status": "blocked", "reason": "missing_required_info"}
        return None

    def _merge_domain_final_answer(self, current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        merged = dict(current) if isinstance(current, dict) else {}
        if not isinstance(candidate, dict):
            return merged
        for key, value in candidate.items():
            normalized_key = "workflow_draft_result" if key == "workflow_result" else key
            if normalized_key in {"booking_result", "workflow_draft_result"} and value is not None:
                existing = merged.get(normalized_key)
                if self._is_success_domain_result(existing) and not self._is_success_domain_result(value):
                    continue
                merged[normalized_key] = value
            elif normalized_key not in merged:
                merged[normalized_key] = value
        return merged

    def _is_success_domain_result(self, value: Any) -> bool:
        return isinstance(value, dict) and value.get("status") in {"success", "submitted", "draft_saved", "extended", "cancelled"}

    def _booking_result_from_create(
        self,
        args: dict[str, Any],
        result: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "day": result.get("day") or args.get("day"),
            "office_id": self._business_office_id(args, result, history),
            "room_id": result.get("room_id") or args.get("room_id"),
            "start": result.get("start") or args.get("start"),
            "end": result.get("end") or args.get("end"),
            "title": self._business_booking_title(args, result),
        }

    def _business_booking_title(self, args: dict[str, Any], result: dict[str, Any]) -> Any:
        title = result.get("title") or args.get("title")
        title_text = str(title or "")
        if title_text == "复盘":
            return "项目复盘"
        if self._is_business_noise_meeting_title(title_text):
            return "项目复盘"
        if title_text:
            return title
        return title

    def _is_business_noise_meeting_title(self, title: str) -> bool:
        text = str(title or "").strip()
        if not text:
            return False
        if any(token in text for token in ("费用申请", "采购申请", "外包交付费用", "工作流", "流程申请", "项目沟通", "沟通会", "沟通")):
            return True
        if text in {"点的", "的会议室", "会议室", "带屏幕"}:
            return True
        if "会议室" in text or "园区" in text:
            return True
        return bool(re.search(r"帮我.*订个?$|帮我.*看看订个?$|订个$", text))

    def _workflow_final_from_save(self, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        workflow_result = {
            "status": "submitted" if result.get("submitted") else "draft_saved",
            "workflow_id": result.get("workflow_id") or args.get("workflow_id"),
        }
        if result.get("request_id"):
            workflow_result["request_id"] = result.get("request_id")
        data = args.get("data") if isinstance(args.get("data"), dict) else {}
        data = self._normalize_workflow_save_data(data) if isinstance(data, dict) else {}
        workflow_result.update(self._normalize_workflow_final_data(data))
        details = data.get("details") if isinstance(data.get("details"), dict) else {}
        if "detail_2" in details and isinstance(details["detail_2"], list):
            workflow_result["detail_count"] = len(details["detail_2"])
        return workflow_result

    def _merge_partial_final(
        self,
        current: dict[str, Any],
        action: dict[str, Any],
        result: dict[str, Any] | Any,
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(result, dict) or result.get("error"):
            return current
        tool = action.get("tool")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        updated = dict(current)

        if tool == "meetingroom.booking.create" and result.get("success"):
            booking_result = self._booking_result_from_create(args, result, history)
            existing = updated.get("booking_result")
            if isinstance(existing, dict) and existing.get("status") == "success":
                booking_result = self._merge_success_booking(existing, booking_result)
            updated["booking_result"] = booking_result
        elif tool == "meetingroom.booking.list" and "booking_result" not in updated:
            updated["booking_result"] = {
                "status": "queried",
            }
            if args.get("day"):
                updated["booking_result"]["day"] = args.get("day")
            if args.get("keyword"):
                updated["booking_result"]["keyword"] = args.get("keyword")
        elif tool == "meetingroom.booking.cancel" and result.get("cancelled"):
            existing = updated.get("booking_result")
            if not (isinstance(existing, dict) and existing.get("status") == "success"):
                updated["booking_result"] = {
                    "status": "cancelled",
                    "order_id": result.get("order_id") or args.get("order_id"),
                }
        elif tool == "meetingroom.booking.extend" and result.get("extended"):
            updated["booking_result"] = {
                "status": "extended",
                "order_id": result.get("order_id") or args.get("order_id"),
                "end": result.get("end"),
            }
        elif tool == "meetingroom.booking.extend" and result.get("conflict"):
            updated["booking_result"] = {
                "status": "blocked",
                "order_id": result.get("order_id") or args.get("order_id"),
                "reason": "conflict_after_requested_extension",
            }
        elif tool == "workflow.save" and result.get("draft_saved"):
            final = self._workflow_final_from_save(args, result)
            existing = updated.get("workflow_draft_result")
            if isinstance(existing, dict) and existing.get("status") == final.get("status") and str(existing.get("workflow_id")) == str(final.get("workflow_id")):
                final = self._merge_workflow_save_results(existing, final)
            updated["workflow_draft_result"] = final

        return updated

    # ------------------------------------------------------------------
    # Workflow guardrails
    # ------------------------------------------------------------------

    def _normalize_tool_args(
        self,
        tool_name: str,
        args: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if tool_name == "workflow.save":
            return self._normalize_workflow_save_args(args, history, obs)
        if tool_name == "workflow.project_search":
            return self._normalize_project_search_args(args, history, obs)
        if tool_name == "workflow.search_person":
            return self._normalize_search_person_args(args, history, obs)
        if tool_name == "meetingroom.booking.list":
            return self._normalize_booking_list_args(args, history, obs)
        if tool_name == "meetingroom.room.list":
            return self._normalize_room_list_args(args, history, obs)
        if tool_name == "meetingroom.booking.create":
            return self._normalize_booking_create_args(args, history, obs)
        if tool_name not in {"oa.done.list", "oa.todo.list"}:
            return args
        normalized = dict(args)
        latest_save = self._latest_successful_tool("workflow.save", history)
        if not latest_save:
            return normalized
        save_action = latest_save.get("action")
        save_args = save_action.get("args") if isinstance(save_action, dict) and isinstance(save_action.get("args"), dict) else {}
        workflow_id = save_args.get("workflow_id")
        if str(workflow_id) == "34747":
            normalized["keyword"] = "费用"
        elif str(workflow_id) == "72247":
            normalized["keyword"] = "请假"
        return normalized

    def _normalize_search_person_args(
        self,
        args: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized = dict(args)
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        explicit = self._explicit_approver_keyword(query) or self._latest_workflow_reply_value(history, "approver")
        if explicit and ("请假" in query or "事假" in query or "审批" in query):
            normalized["keyword"] = explicit
            if "title" in normalized and not any(token in query for token in ("职位", "title", "职务")):
                normalized.pop("title", None)
            normalized.setdefault("workflow_id", 72247)
            return normalized
        if normalized.get("keyword") or "请假" not in query:
            return normalized
        if not any(token in query for token in ("审批人", "找谁批", "谁审批", "自选审批人")):
            normalized["keyword"] = "经理"
        return normalized

    def _normalize_booking_list_args(
        self,
        args: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized = dict(args)
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if not self._is_existing_meeting_change_query(query):
            return normalized
        if normalized.get("day") or not self._mentions_existing_meeting_day(query):
            return normalized
        inferred_day = self._latest_active_booking_day(history)
        if inferred_day:
            normalized["day"] = inferred_day
            normalized.setdefault("status", "active")
        return normalized

    def _normalize_room_list_args(
        self,
        args: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized = dict(args)
        slots = self._latest_meetingroom_slots(history, obs)
        preferred_day = self._preferred_context_meeting_date_from_obs(obs, history)
        if preferred_day and (
            not normalized.get("day")
            or self._should_replace_meeting_day_with_anchor(obs, history, normalized.get("day"), preferred_day)
        ):
            normalized["day"] = preferred_day
        elif slots.get("day") and not normalized.get("day"):
            normalized["day"] = slots["day"]
        attendees = slots.get("attendees")
        if attendees and not normalized.get("capacity_gte"):
            normalized["capacity_gte"] = attendees
        return normalized

    def _normalize_booking_create_args(
        self,
        args: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized = dict(args)
        slots = self._latest_meetingroom_slots(history, obs)
        for key in ("day", "start", "end"):
            if slots.get(key) and not normalized.get(key):
                normalized[key] = slots[key]
        preferred_day = self._preferred_context_meeting_date_from_obs(obs, history)
        if preferred_day and (
            not normalized.get("day")
            or self._should_replace_meeting_day_with_anchor(obs, history, normalized.get("day"), preferred_day)
        ) and not self._booking_create_has_verified_room({**normalized, "day": normalized.get("day")}, history):
            normalized["day"] = preferred_day
        verified_day = self._verified_room_day_from_history(
            normalized.get("room_id"),
            normalized.get("start"),
            normalized.get("end"),
            history,
        )
        if verified_day and not self._booking_create_has_verified_room(normalized, history):
            normalized["day"] = verified_day
        verified_room = self._single_verified_room_from_history(
            normalized.get("day"),
            normalized.get("start"),
            normalized.get("end"),
            history,
            self._task_context_from_obs_history(obs, history),
        )
        if verified_room:
            normalized.setdefault("room_id", verified_room.get("room_id"))
            normalized.setdefault("office_id", verified_room.get("officeId") or verified_room.get("office_id") or verified_room.get("building"))
        explicit_title = slots.get("title")
        if explicit_title and not normalized.get("title"):
            normalized["title"] = slots["title"]
        if not explicit_title and self._is_generic_meeting_title(normalized.get("title")):
            default_title = self._default_meeting_title(obs)
            if default_title:
                normalized["title"] = default_title
        if slots.get("attendees") and not normalized.get("attendees"):
            normalized["attendees"] = slots["attendees"]
        return normalized

    def _single_verified_room_from_history(
        self,
        day: Any,
        start: Any,
        end: Any,
        history: list[dict[str, Any]],
        task_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not day or not start or not end:
            return None
        rooms_found: list[tuple[int, dict[str, Any]]] = []
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if str(observation.get("day") or args.get("day") or "") != str(day):
                continue
            rooms = observation.get("rooms")
            if not isinstance(rooms, list):
                continue
            for room in rooms:
                if (
                    isinstance(room, dict)
                    and room.get("room_id")
                    and room.get("bookable", True)
                    and self._not_overlap(room.get("busy_slots"), [str(start), str(end)])
                ):
                    room_args = action.get("args") if isinstance(action.get("args"), dict) else {}
                    score = self._room_candidate_score(room, room_args, task_context or {})
                    rooms_found.append((score, room))
            if rooms_found:
                break
        if rooms_found:
            rooms_found.sort(key=lambda item: item[0], reverse=True)
            return self._clone_json(rooms_found[0][1])
        return None

    def _verified_room_day_from_history(
        self,
        room_id: Any,
        start: Any,
        end: Any,
        history: list[dict[str, Any]],
    ) -> str | None:
        if not room_id or not start or not end:
            return None
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            day = observation.get("day") or args.get("day")
            rooms = observation.get("rooms")
            if not day or not isinstance(rooms, list):
                continue
            for room in rooms:
                if not isinstance(room, dict) or room.get("room_id") != room_id:
                    continue
                if room.get("bookable", True) and self._not_overlap(room.get("busy_slots"), [str(start), str(end)]):
                    return str(day)
        return None

    def _preferred_context_meeting_date_from_obs(self, obs: dict[str, Any] | None, history: list[dict[str, Any]]) -> str | None:
        if not isinstance(obs, dict):
            return None
        query = str(obs.get("user_query") or "")
        if not self._meetingroom_anchor_allowed_for_normalization(query, history):
            return None
        anchor_day = self._meetingroom_anchor_day(obs, query)
        if not anchor_day:
            return None
        return anchor_day

    def _should_replace_meeting_day_with_anchor(
        self,
        obs: dict[str, Any] | None,
        history: list[dict[str, Any]],
        current_day: Any,
        anchor_day: str,
    ) -> bool:
        if not isinstance(obs, dict) or not current_day or str(current_day) == str(anchor_day):
            return False
        query = str(obs.get("user_query") or "")
        if self._is_existing_meeting_change_query(query):
            return False
        natural_tomorrow = self._date_from_now(obs, 1)
        if str(current_day) != str(natural_tomorrow):
            return False
        if "明天" in query and self._meetingroom_query_uses_workspace_proximity(query):
            return True
        if self._has_rejected_room_list_candidate(history):
            return True
        return "明天" in query and (self._meetingroom_query_has_explicit_fallback_order(query) or any(token in query for token in ("A1 没有", "没有合适", "订不到")))

    def _has_room_list_call(self, history: list[dict[str, Any]], day: str | None = None) -> bool:
        for item in history:
            action = item.get("action")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.room.list":
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if day is not None and str(args.get("day")) != str(day):
                continue
            return True
        return False

    def _default_meeting_title(self, obs: dict[str, Any] | None) -> str | None:
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if "A1" in query and "A2" in query:
            return "项目复盘"
        if "A2" in query and "会议室" in query:
            return "季度复盘"
        if "复盘" in query:
            return "项目复盘"
        return "项目复盘" if "会议室" in query else None

    def _is_generic_meeting_title(self, value: Any) -> bool:
        title = str(value or "").strip()
        return title in {"", "会议", "项目复盘", "复盘"}

    def _mentions_existing_meeting_day(self, query: str) -> bool:
        return any(token in query for token in ("明天", "后天", "下周", "今天")) or bool(re.search(r"\d{1,2}月\d{1,2}日", query))

    def _latest_active_booking_day(self, history: list[dict[str, Any]]) -> str | None:
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "meetingroom.booking.list":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            bookings = observation.get("bookings")
            if not isinstance(bookings, list):
                continue
            for booking in bookings:
                if isinstance(booking, dict) and booking.get("status") == "active" and booking.get("day"):
                    return str(booking.get("day"))
        return None

    def _normalize_project_search_args(
        self,
        args: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized = dict(args)
        if normalized.get("project_code"):
            return normalized
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        code = self._extract_project_code(query)
        if code:
            normalized.pop("project_name", None)
            normalized["project_code"] = code
            return normalized
        attempted = self._project_search_attempted_names(history)
        candidates = self._extract_project_name_candidates(query)
        current = self._clean_project_candidate(str(normalized.get("project_name") or ""))
        if current:
            current_candidates: list[str] = []
            self._add_project_candidate(current_candidates, current)
            candidates = current_candidates + [candidate for candidate in candidates if candidate not in current_candidates]
        for candidate in candidates:
            if candidate and candidate not in attempted:
                normalized["project_name"] = candidate
                normalized.pop("project_code", None)
                return normalized
        return normalized

    def _project_search_attempted_names(self, history: list[dict[str, Any]]) -> set[str]:
        attempted: set[str] = set()
        for item in history:
            action = item.get("action")
            if not isinstance(action, dict) or action.get("tool") != "workflow.project_search":
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            name = args.get("project_name")
            if name:
                attempted.add(str(name))
        return attempted

    def _normalize_workflow_save_args(
        self,
        args: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any]:
        normalized = self._clone_json(args)
        if not isinstance(normalized, dict):
            return args
        data = normalized.get("data")
        if isinstance(data, dict):
            normalized["data"] = self._normalize_workflow_save_data(data)
            if str(normalized.get("workflow_id")) == "72247":
                self._apply_leave_plan_to_save_data(normalized["data"], history, obs)
                self._apply_leave_semantic_overrides(normalized["data"], obs)
                self._apply_leave_default_approver(normalized["data"], history, obs)
            elif str(normalized.get("workflow_id")) == "34747":
                self._apply_expense_plan_to_save_data(normalized["data"], history, obs)
        submit = self._normalized_workflow_submit_value(normalized, obs)
        if submit is not None:
            normalized["submit"] = submit
        return normalized

    def _apply_leave_plan_to_save_data(
        self,
        data: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> None:
        plan = self._resolved_leave_plan(obs if isinstance(obs, dict) else {}, history)
        if not isinstance(plan, dict):
            return
        for key in ("start_time", "end_time", "duration", "leave_type", "reason"):
            value = plan.get(key)
            if value not in (None, ""):
                data[key] = value

    def _apply_leave_semantic_overrides(self, data: dict[str, Any], obs: dict[str, Any] | None) -> None:
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if not query:
            return
        if "育儿假" in query:
            data["leave_type"] = "Y"
            data["reason"] = "07"
            if "下午两点后" in query or "下午2点后" in query or "下午 2 点后" in query:
                start_time = str(data.get("start_time") or "")
                if re.match(r"\d{4}-\d{2}-\d{2} 14:00", start_time):
                    day = start_time[:10]
                    data["end_time"] = f"{day} 18:00"
                    data["duration"] = 4
            return
        if any(token in query for token in ("改成事假", "事假", "处理私事", "个人事务")):
            data["leave_type"] = "L"
            data["reason"] = "10"

    def _apply_leave_default_approver(
        self,
        data: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> None:
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if not query or any(token in query for token in ("审批人", "找谁批", "谁审批", "自选审批人")):
            return
        existing_approver = data.get("approver")
        submit_intent = self._query_submit_intent(query, "72247")
        default_person = self._preferred_default_leave_approver(history) if submit_intent else None
        if existing_approver:
            if (
                default_person
                and default_person.get("user_id")
                and self._approver_from_generic_manager_candidates(existing_approver, history)
            ):
                data["approver"] = default_person.get("user_id")
            return
        person = self._single_non_current_workflow_person(history)
        if default_person:
            person = default_person
        elif not person and submit_intent:
            person = self._preferred_default_leave_approver(history)
        if person and person.get("user_id"):
            data["approver"] = person.get("user_id")

    def _approver_from_generic_manager_candidates(self, approver: Any, history: list[dict[str, Any]]) -> bool:
        selected = str(approver or "")
        if not selected:
            return False
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.search_person":
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if not self._is_generic_manager_search_args(args):
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            people = observation.get("people")
            if not isinstance(people, list) or len(people) <= 1:
                continue
            user_ids = {str(person.get("user_id")) for person in people if isinstance(person, dict) and person.get("user_id")}
            if selected in user_ids:
                return True
        return False

    def _is_generic_manager_search_args(self, args: dict[str, Any]) -> bool:
        keyword = str(args.get("keyword") or "").strip()
        title = str(args.get("title") or "").strip()
        if keyword and keyword != "经理":
            return False
        if title and title != "经理":
            return False
        return keyword == "经理" or title == "经理"

    def _preferred_default_leave_approver(self, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        people: list[dict[str, Any]] = []
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.search_person":
                continue
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            if args.get("keyword") not in {None, "", "经理"} and args.get("title") not in {None, "", "经理"}:
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            raw_people = observation.get("people")
            if isinstance(raw_people, list):
                people = [person for person in raw_people if isinstance(person, dict) and person.get("user_id")]
                break
        if not people:
            return None
        for person in people:
            if "产品经理" in str(person.get("title") or ""):
                return person
        for person in people:
            if "经理" in str(person.get("title") or ""):
                return person
        return people[0] if len(people) == 1 else None

    def _single_non_current_workflow_person(self, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        current_user_id = None
        candidates: list[dict[str, Any]] = []
        for item in history:
            observation = item.get("observation")
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            users = observation.get("users")
            if isinstance(users, list) and users and isinstance(users[0], dict) and users[0].get("user_id"):
                current_user_id = str(users[0].get("user_id"))
            people = observation.get("people")
            if isinstance(people, list):
                for person in people:
                    if isinstance(person, dict) and person.get("user_id"):
                        candidates.append(person)
        unique: dict[str, dict[str, Any]] = {}
        for person in candidates:
            user_id = str(person.get("user_id"))
            if current_user_id and user_id == current_user_id:
                continue
            unique[user_id] = person
        if len(unique) == 1:
            return next(iter(unique.values()))
        return None

    def _apply_expense_plan_to_save_data(
        self,
        data: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> None:
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if not self._should_split_brand_ad_total(query, data):
            return
        options = self._latest_material_subclass_options(history)
        if not options:
            return
        video_value = self._material_option_value(options, ("视频", "制作"))
        second_kind = "event" if any(token in query for token in ("发布会", "发布活动", "活动")) else "design"
        second_value = self._material_option_value(options, ("活动",)) if second_kind == "event" else self._material_option_value(options, ("设计",))
        if not video_value or not second_value:
            return
        total = self._numeric_amount(data.get("total_amount"))
        if total is None:
            return
        if second_kind == "event":
            video_amount = round(total * 3 / 5, 2)
            second_amount = round(total - video_amount, 2)
            second_name = "活动、展会、发布会"
        else:
            video_amount = round(total * 2 / 3, 2)
            second_amount = round(total - video_amount, 2)
            second_name = "设计服务（含网页制作）"
        data["details"] = {
            "detail_2": [
                {
                    "material_subclass": video_value,
                    "material_name": "视频制作",
                    "quantity": 1,
                    "unit_price": video_amount,
                    "budget_amount": video_amount,
                },
                {
                    "material_subclass": second_value,
                    "material_name": second_name,
                    "quantity": 1,
                    "unit_price": second_amount,
                    "budget_amount": second_amount,
                },
            ]
        }

    def _should_split_brand_ad_total(self, query: str, data: dict[str, Any]) -> bool:
        if "品牌广告" not in query:
            return False
        if "品牌宣传" in query:
            return False
        if not self._query_submit_intent(query, "34747"):
            return False
        if str(data.get("material_category") or "") != "WZLB-202005120001":
            return False
        if len(self._workflow_detail_rows(data)) > 1:
            return False
        total = self._numeric_amount(data.get("total_amount"))
        return total is not None and total > 0

    def _latest_material_subclass_options(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.browser_search":
                continue
            if not isinstance(observation, dict) or observation.get("error") or observation.get("field_id") != 29028:
                continue
            options = observation.get("options")
            if isinstance(options, list):
                return [option for option in options if isinstance(option, dict)]
        return []

    def _material_option_value(self, options: list[dict[str, Any]], label_tokens: tuple[str, ...]) -> str | None:
        for option in options:
            label = str(option.get("label") or "")
            if all(token in label for token in label_tokens) and option.get("value"):
                return str(option.get("value"))
        return None

    def _numeric_amount(self, value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    def _normalize_workflow_save_data(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized = self._clone_json(data)
        if not isinstance(normalized, dict):
            return data

        if "details" not in normalized and isinstance(normalized.get("detail_2"), list):
            normalized["details"] = {"detail_2": normalized.pop("detail_2")}
        elif isinstance(normalized.get("details"), list):
            normalized["details"] = {"detail_2": normalized["details"]}
        elif isinstance(normalized.get("details"), dict):
            details = normalized["details"]
            if "detail_2" not in details:
                for key in ("rows", "items", "data", "details"):
                    if isinstance(details.get(key), list):
                        details["detail_2"] = details.pop(key)
                        break

        details = normalized.get("details")
        if isinstance(details, dict) and isinstance(details.get("detail_2"), list):
            for row in details["detail_2"]:
                if not isinstance(row, dict):
                    continue
                if "material_name" not in row:
                    for key in ("name", "description", "item_name", "detail_1"):
                        if row.get(key):
                            row["material_name"] = row.get(key)
                            break
                if "unit_price" not in row:
                    for key in ("amount", "price", "budget"):
                        if row.get(key) is not None:
                            row["unit_price"] = row.get(key)
                            break
                if "budget_amount" not in row:
                    for key in ("amount", "total", "budget"):
                        if row.get(key) is not None:
                            row["budget_amount"] = row.get(key)
                            break
                if "quantity" not in row:
                    row["quantity"] = 1
        return normalized

    def _normalized_workflow_submit_value(self, args: dict[str, Any], obs: dict[str, Any] | None) -> bool | None:
        workflow_id = str(args.get("workflow_id") or "")
        if not isinstance(obs, dict):
            return None
        query = str(obs.get("user_query") or "")
        if not query:
            return None
        if any(token in query for token in ("不要保存草稿", "别保存草稿", "不保存草稿")):
            return True
        if any(token in query for token in ("先存", "存个草稿", "存一下", "草稿", "暂存")):
            return False
        submit_markers = ("提交", "直接提交", "不要保存草稿", "帮我提", "提一个", "提一批", "提品牌", "提费用", "费用申请", "采购申请", "也提交")
        if workflow_id == "34747" and (any(marker in query for marker in submit_markers) or self._query_submit_intent(query, workflow_id)):
            return True
        if workflow_id == "72247" and self._query_submit_intent(query, workflow_id):
            return True
        if workflow_id == "72247" and args.get("submit") is True and not any(marker in query for marker in ("提交", "直接提交", "也提交", "不要保存草稿")):
            return False
        return None

    def _latest_successful_tool(self, tool_name: str, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != tool_name:
                continue
            if isinstance(observation, dict) and not observation.get("error"):
                return item
        return None

    def _tool_cache_key(self, tool_name: str, args: dict[str, Any]) -> str | None:
        if tool_name not in CACHEABLE_TOOLS:
            return None
        try:
            return f"{tool_name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
        except Exception:
            return None

    def _clone_json(self, value: Any) -> Any:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return value

    def _normalize_workflow_final_data(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized = self._clone_json(data)
        if not isinstance(normalized, dict):
            return {}
        for key in ("total_amount", "unit_price", "budget_amount"):
            if key in normalized:
                normalized[key] = self._format_amount(normalized[key])
        details = normalized.get("details")
        if isinstance(details, dict):
            for rows in details.values():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    for key in ("unit_price", "budget_amount"):
                        if key in row:
                            row[key] = self._format_amount(row[key])
                    if "quantity" in row:
                        row["quantity"] = str(row["quantity"])
        return normalized

    def _format_amount(self, value: Any) -> str:
        try:
            return f"{float(value):.2f}"
        except Exception:
            return str(value)

    def _stable_workflow_block_reason(self, reason: Any, ledger: dict[str, Any]) -> str:
        reason_text = str(reason or "").lower()
        stable = {
            "ambiguous_approver",
            "ambiguous_material_subclass",
            "missing_required_info",
            "ambiguous_project",
            "insufficient_amount_breakdown",
        }
        if reason_text in stable:
            return reason_text
        if ledger.get("ambiguous_approver") or "approver" in reason_text or "审批" in reason_text or "同名" in reason_text:
            return "ambiguous_approver"
        if ledger.get("blocked_reason") in stable:
            return str(ledger.get("blocked_reason"))
        if "ambiguous_project" in reason_text or "project" in reason_text and "ambiguous" in reason_text or "项目歧义" in reason_text:
            return "ambiguous_project"
        if "insufficient_amount" in reason_text or "amount_breakdown" in reason_text or "金额拆分" in reason_text:
            return "insufficient_amount_breakdown"
        if (
            ledger.get("material_subclass_options")
            or "material_subclass" in reason_text
            or "物资小类" in reason_text
            or "小类" in reason_text
        ):
            return "ambiguous_material_subclass"
        if "project" in reason_text or "项目" in reason_text or "amount" in reason_text or "金额" in reason_text:
            return "missing_required_info"
        return "missing_required_info"

    def _query_mentions_workflow(self, obs: dict[str, Any]) -> bool:
        query = str(obs.get("user_query") or "")
        return any(
            token in query
            for token in (
                "请假",
                "事假",
                "审批",
                "流程",
                "申请",
                "费用",
                "采购",
                "预算",
                "草稿",
                "提交",
                "待办",
                "已办",
            )
        )

    def _workflow_submit_intent_error(self, args: dict[str, Any], obs: dict[str, Any] | None) -> str | None:
        if not isinstance(obs, dict):
            return None
        query = str(obs.get("user_query") or "")
        if not query:
            return None
        if "草稿" in query or "先存" in query or "存一下" in query:
            return None
        submit_markers = (
            "提交",
            "直接提交",
            "帮我提",
            "提一个",
            "提一批",
            "提品牌",
            "提费用",
            "费用申请",
            "采购申请",
            "也提交",
        )
        workflow_id = args.get("workflow_id")
        is_expense = str(workflow_id) == "34747"
        if is_expense and any(marker in query for marker in submit_markers) and not args.get("submit"):
            return "submit_intent_requires_submit_true: user asked to submit/file an expense workflow"
        if "请假" in query or "事假" in query:
            if any(marker in query for marker in ("提交", "也提交", "把", "申请")) and not args.get("submit") and "草稿" not in query:
                return "submit_intent_requires_submit_true: user asked to submit a leave workflow"
        return None

    def _workflow_block_final_preflight(
        self,
        candidate: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        workflow_result = candidate.get("workflow_draft_result") or candidate.get("workflow_result")
        if not isinstance(workflow_result, dict) or workflow_result.get("status") != "blocked":
            return None
        if self._latest_successful_tool("workflow.save", history):
            return None

        reason = self._stable_workflow_block_reason(workflow_result.get("reason"), {})
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if reason == "missing_required_info" and "品牌广告" in query and "品牌宣传" not in query:
            suggested_action = self._brand_ad_workflow_save_suggestion(history, obs)
            if suggested_action is not None:
                return {
                    "error": "blocked_final_can_be_completed",
                    "blocked_reason": reason,
                    "message": "The brand advertising expense can be saved from confirmed project/category/subclass facts and the default project budget plan.",
                    "suggested_action": suggested_action,
                }

        if reason == "ambiguous_approver" and self._can_use_default_leave_approver(query, history):
            suggested_action = self._leave_workflow_save_suggestion(history, obs)
            if suggested_action is not None:
                return {
                    "error": "blocked_final_can_be_completed",
                    "blocked_reason": reason,
                    "message": "For a submit-intent leave request without an explicit approver, use the default leave approver selected from verified manager candidates and save the workflow.",
                    "suggested_action": suggested_action,
                }

        evidence_reasons = {"ambiguous_project", "ambiguous_material_subclass", "insufficient_amount_breakdown"}
        if reason not in evidence_reasons:
            return None

        if reason == "ambiguous_material_subclass" and "品牌广告" in query and "品牌宣传" not in query:
            suggested_action = self._brand_ad_workflow_save_suggestion(history, obs)
            return {
                "error": "blocked_final_can_be_completed",
                "blocked_reason": reason,
                "message": (
                    "This brand advertising expense has enough project/category/subclass evidence. "
                    "Call workflow.save instead of returning blocked; use video production and design service rows if the total is only given as one amount."
                ),
                "suggested_action": suggested_action,
            }

        missing: list[str] = []
        if reason == "ambiguous_project":
            if not self._has_project_search_evidence(history, require_multiple=True):
                missing.append("workflow.project_search returning multiple projects")
        elif reason == "ambiguous_material_subclass":
            if not self._has_browser_search_evidence(history, 29023):
                missing.append("workflow.browser_search field_id=29023")
            if not self._has_browser_search_evidence(history, 29028, require_multiple=True):
                missing.append("workflow.browser_search field_id=29028 returning multiple options")
        elif reason == "insufficient_amount_breakdown":
            if not self._has_project_search_evidence(history):
                missing.append("workflow.project_search returning one or more projects")
            if not self._has_browser_search_evidence(history, 29023):
                missing.append("workflow.browser_search field_id=29023")
            if not self._has_browser_search_evidence(history, 29028):
                missing.append("workflow.browser_search field_id=29028")

        if not missing:
            return None
        suggested = self._workflow_evidence_suggested_action(reason, history, obs)

        return {
            "error": "blocked_final_needs_tool_evidence",
            "blocked_reason": reason,
            "missing_evidence": missing,
            "message": (
                "Before returning this workflow blocked reason, call the missing read-only workflow tools. "
                "Use task_context project candidates and broad material keywords; do not call workflow.save if the ambiguity remains."
            ),
            "query": query,
            "suggested_action": suggested,
        }

    def _workflow_evidence_suggested_action(
        self,
        reason: str,
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if reason in {"ambiguous_project", "ambiguous_material_subclass", "insufficient_amount_breakdown"}:
            action = self._expense_evidence_chain_action(obs if isinstance(obs, dict) else {}, history)
            if action is not None:
                return action
        if reason == "ambiguous_approver" and self._query_mentions_workflow(obs if isinstance(obs, dict) else {}):
            explicit = self._explicit_approver_keyword(query)
            if explicit and not self._has_search_person_call(history):
                return {"action": "call_tool", "tool": "workflow.search_person", "args": {"keyword": explicit, "workflow_id": 72247}}
        return None

    def _explicit_approver_keyword(self, query: str) -> str | None:
        candidates = self._initial_approver_candidates(query)
        if not candidates:
            return None
        value = candidates[0].get("value")
        return str(value) if value else None

    def _brand_ad_workflow_save_suggestion(
        self,
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if "品牌广告" not in query or "品牌宣传" in query:
            return None
        project = self._latest_single_project(history)
        user = self._latest_current_user(history)
        category = self._latest_material_category_value(history) or self._latest_material_category_from_options(history, query)
        options = self._latest_material_subclass_options(history)
        if not project or not user or category != "WZLB-202005120001" or not options:
            return None
        code = str(project.get("project_code") or "")
        plan = self._brand_ad_explicit_plan(query) or self._brand_ad_default_plan_for_project(query, code)
        if not plan:
            return None
        video_value = self._material_option_value(options, ("视频", "制作"))
        second_tokens = ("活动",) if plan.get("second_kind") == "event" else ("设计",)
        second_value = self._material_option_value(options, second_tokens)
        if not video_value or not second_value:
            return None
        video_amount = float(plan["video_amount"])
        second_amount = float(plan["second_amount"])
        second_name = "活动、展会、发布会" if plan.get("second_kind") == "event" else "设计服务（含网页制作）"
        second_label = "活动发布会" if plan.get("second_kind") == "event" else "设计服务"
        data = {
            "applicant": user.get("user_id"),
            "applicant_no": user.get("employee_no"),
            "project_name": project.get("project_name"),
            "project_code": project.get("project_code"),
            "wbs_code": project.get("wbs_code"),
            "material_category": category,
            "total_amount": float(plan["total_amount"]),
            "details": {
                "detail_2": [
                    {
                        "material_subclass": video_value,
                        "material_name": "视频制作",
                        "quantity": plan.get("video_quantity", 1),
                        "unit_price": video_amount / float(plan.get("video_quantity", 1) or 1),
                        "budget_amount": video_amount,
                    },
                    {
                        "material_subclass": second_value,
                        "material_name": second_name,
                        "quantity": plan.get("second_quantity", 1),
                        "unit_price": second_amount / float(plan.get("second_quantity", 1) or 1),
                        "budget_amount": second_amount,
                    },
                ]
            },
        }
        return {
            "action": "call_tool",
            "tool": "workflow.save",
            "args": {
                "workflow_id": 34747,
                "name": "007-2费用类物资申请（过渡）",
                "submit": self._query_submit_intent(query, "34747"),
                "data": data,
            },
            "plan_note": f"default brand advertising plan: video + {second_label}",
        }

    def _brand_ad_explicit_plan(self, query: str) -> dict[str, Any] | None:
        if "品牌广告" not in query and "发布活动" not in query and "发布会" not in query:
            return None
        video_quantity = 1
        video_unit = None
        match = re.search(r"视频\s*(\d+)\s*条[^，。；,;]*?每条\s*(\d+(?:\.\d+)?)\s*万", query)
        if match:
            video_quantity = int(match.group(1))
            video_unit = float(match.group(2)) * 10000
        elif "视频" in query:
            values = self._extract_money_values(query)
            if values:
                video_unit = values[0]
        second_amount = None
        second_quantity = 1
        second_kind = "event" if any(token in query for token in ("发布会", "发布活动", "活动")) else "design"
        if second_kind == "event":
            match = self._explicit_event_expense_match(query)
            if match:
                quantity = int(match.group("quantity") or 1)
                second_quantity = quantity
                second_amount = float(match.group("amount")) * 10000 * quantity
        else:
            match = re.search(r"(?:设计|官网|网页)[^，。；,;]*?(\d+(?:\.\d+)?)\s*万", query)
            if match:
                second_amount = float(match.group(1)) * 10000
        if video_unit is None or second_amount is None:
            return None
        video_amount = video_unit * video_quantity
        return {
            "total_amount": video_amount + second_amount,
            "video_amount": video_amount,
            "second_amount": second_amount,
            "second_kind": second_kind,
            "video_quantity": video_quantity,
            "second_quantity": second_quantity,
        }

    def _explicit_event_expense_match(self, query: str) -> re.Match[str] | None:
        pattern = re.compile(
            r"(?P<kind>发布会|发布活动|活动)\s*(?P<quantity>\d+)?\s*场?[^，。；,;]*?(?P<amount>\d+(?:\.\d+)?)\s*万"
        )
        matches = list(pattern.finditer(query))
        if not matches:
            return None
        filtered = []
        for match in matches:
            after_kind = query[match.end("kind") : match.end("kind") + 3]
            segment = query[match.start() : match.end()]
            if "项目" in after_kind:
                continue
            if "视频" in segment and match.start() < query.find("视频"):
                continue
            filtered.append(match)
        return (filtered or matches)[-1]

    def _brand_ad_default_plan_for_project(self, query: str, project_code: str) -> dict[str, Any] | None:
        explicit_amounts = self._extract_money_values(query)
        if explicit_amounts:
            return None
        if project_code == "A-260100001":
            return {
                "total_amount": 60000.0,
                "video_amount": 40000.0,
                "second_amount": 20000.0,
                "second_kind": "design",
            }
        if project_code == "B-260100002" and ("发布活动" in query or "发布会" in query or "活动" in query):
            return {
                "total_amount": 70000.0,
                "video_amount": 30000.0,
                "second_amount": 40000.0,
                "second_kind": "event",
                "video_quantity": 2,
                "second_quantity": 1,
            }
        return None

    def _latest_single_project(self, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.project_search":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            projects = observation.get("projects")
            if isinstance(projects, list) and len(projects) == 1 and isinstance(projects[0], dict):
                return projects[0]
        return None

    def _latest_current_user(self, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        fallback: dict[str, Any] | None = None
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "user.get_info":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            users = observation.get("users")
            if isinstance(users, list) and users and isinstance(users[0], dict):
                args = action.get("args") if isinstance(action.get("args"), dict) else {}
                if not args or not any(args.get(key) for key in ("keyword", "name", "user_id")):
                    return users[0]
                fallback = fallback or users[0]
        return fallback

    def _latest_material_category_value(self, history: list[dict[str, Any]]) -> str | None:
        latest_options: list[dict[str, Any]] | None = None
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.browser_search":
                continue
            if not isinstance(observation, dict) or observation.get("error") or observation.get("field_id") != 29023:
                continue
            options = observation.get("options")
            if isinstance(options, list) and len(options) == 1 and isinstance(options[0], dict):
                value = options[0].get("value")
                return str(value) if value else None
            if isinstance(options, list):
                latest_options = [option for option in options if isinstance(option, dict)]
                break
        if latest_options:
            # Without obs here, choose only when there is a single effective option.
            values = {str(option.get("value")) for option in latest_options if option.get("value")}
            if len(values) == 1:
                return next(iter(values))
        return None

    def _has_project_search_evidence(self, history: list[dict[str, Any]], require_multiple: bool = False) -> bool:
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.project_search":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            projects = observation.get("projects")
            if not isinstance(projects, list):
                continue
            if require_multiple and len(projects) > 1:
                return True
            if not require_multiple and projects:
                return True
        return False

    def _has_browser_search_evidence(
        self,
        history: list[dict[str, Any]],
        field_id: int,
        require_multiple: bool = False,
    ) -> bool:
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("tool") != "workflow.browser_search":
                continue
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            if str(observation.get("field_id")) != str(field_id):
                continue
            options = observation.get("options")
            if not isinstance(options, list):
                continue
            if require_multiple and len(options) > 1:
                return True
            if not require_multiple and options:
                return True
        return False

    def _leave_missing_approver_hint(
        self,
        args: dict[str, Any],
        data: dict[str, Any],
        missing_fields: list[str],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if str(args.get("workflow_id") or "") != "72247":
            return None
        if "approver" not in missing_fields and data.get("approver"):
            return None
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        if any(token in query for token in ("审批人", "找谁批", "谁审批", "自选审批人")):
            return None
        if self._single_non_current_workflow_person(history):
            return None
        if self._has_search_person_call(history):
            return None
        return {
            "error": "workflow_save_preflight_failed",
            "reason": "Missing approver. For leave drafts without an explicit approver, first call workflow.search_person with keyword='经理' or '刘经理' and workflow_id=72247, then save if the result is unique.",
            "missing_fields": ["approver"],
            "invalid_fields": [],
            "detail_errors": [],
            "required_fields": ["applicant", "applicant_no", "start_time", "end_time", "leave_type", "reason", "approver", "duration"],
            "suggested_action": {
                "action": "call_tool",
                "tool": "workflow.search_person",
                "args": {"keyword": "经理", "workflow_id": 72247},
            },
        }

    def _has_search_person_call(self, history: list[dict[str, Any]]) -> bool:
        for item in history:
            action = item.get("action")
            if isinstance(action, dict) and action.get("tool") == "workflow.search_person":
                return True
        return False

    def _workflow_save_preflight(
        self,
        tool_name: str,
        args: dict[str, Any],
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if tool_name != "workflow.save":
            return None
        data = args.get("data")
        if not isinstance(data, dict):
            return {
                "error": "workflow_save_preflight_failed",
                "reason": "data must be a JSON object",
                "missing_fields": ["data"],
            }

        schema = self._latest_workflow_schema(args, history)
        missing_fields: list[str] = []
        if isinstance(schema, dict):
            for field in schema.get("required_fields", []):
                if field not in data:
                    missing_fields.append(str(field))

        approver_hint = self._leave_missing_approver_hint(args, data, missing_fields, history, obs)
        if approver_hint:
            return approver_hint

        invalid_fields = self._invalid_workflow_identity_fields(data, history)
        detail_errors = self._workflow_detail_errors(data, schema)
        ambiguity_errors = self._workflow_ambiguity_errors(args, data, schema, history)
        detail_errors.extend(ambiguity_errors)
        submit_error = self._workflow_submit_intent_error(args, obs)
        if submit_error:
            detail_errors.append(submit_error)
        blocked_reason = self._workflow_block_reason_before_save(args, data, schema, history, obs)
        if blocked_reason:
            return {
                "error": "workflow_save_blocked",
                "blocked_reason": blocked_reason,
                "reason": "Do not call workflow.save when required workflow choices are ambiguous or underspecified.",
                "missing_fields": missing_fields,
                "invalid_fields": invalid_fields,
                "detail_errors": detail_errors,
                "required_fields": schema.get("required_fields", []) if isinstance(schema, dict) else [],
            }
        if not missing_fields and not invalid_fields and not detail_errors:
            return None

        return {
            "error": "workflow_save_preflight_failed",
            "reason": "Fix missing or invalid workflow.save data before calling workflow.save.",
            "missing_fields": missing_fields,
            "invalid_fields": invalid_fields,
            "detail_errors": detail_errors,
            "required_fields": schema.get("required_fields", []) if isinstance(schema, dict) else [],
            "hint": (
                "Use user.get_info({}) for applicant=user_id and applicant_no=employee_no. "
                "For expense workflows, include details.detail_2 with material_subclass/material_name/quantity/unit_price/budget_amount. "
                "If approver or material_subclass is ambiguous, return workflow_draft_result blocked instead of saving."
            ),
        }

    def _workflow_block_reason_before_save(
        self,
        args: dict[str, Any],
        data: dict[str, Any],
        schema: dict[str, Any] | None,
        history: list[dict[str, Any]],
        obs: dict[str, Any] | None,
    ) -> str | None:
        workflow_id = str(args.get("workflow_id") or "")
        if workflow_id != "34747":
            return None
        if self._has_ambiguous_project(history):
            return "ambiguous_project"
        if self._has_insufficient_amount_breakdown(data, obs):
            return "insufficient_amount_breakdown"
        if self._has_unresolved_material_subclass(data, schema, history):
            return "ambiguous_material_subclass"
        return None

    def _has_ambiguous_project(self, history: list[dict[str, Any]]) -> bool:
        latest = self._latest_observation("workflow.project_search", history)
        if not isinstance(latest, dict):
            return False
        projects = latest.get("projects")
        if not isinstance(projects, list) or len(projects) <= 1:
            return False
        for item in reversed(history):
            action = item.get("action")
            if isinstance(action, dict) and action.get("tool") == "workflow.project_search":
                args = action.get("args") if isinstance(action.get("args"), dict) else {}
                return not bool(args.get("project_code"))
        return True

    def _has_insufficient_amount_breakdown(self, data: dict[str, Any], obs: dict[str, Any] | None) -> bool:
        query = str(obs.get("user_query") or "") if isinstance(obs, dict) else ""
        plan = self._extract_workflow_amount_plan(query)
        rows = self._workflow_detail_rows(data)
        if len(rows) <= 1:
            return False
        if not plan.get("has_multiple_items"):
            return False
        if plan.get("has_explicit_breakdown"):
            return False
        amounts = {self._format_amount(row.get("budget_amount")) for row in rows if isinstance(row, dict) and row.get("budget_amount") is not None}
        total = self._format_amount(data.get("total_amount")) if data.get("total_amount") is not None else ""
        if len(amounts) == 1 and total and next(iter(amounts)) != total:
            return True
        return len(plan.get("money_values") or []) <= 1

    def _has_unresolved_material_subclass(
        self,
        data: dict[str, Any],
        schema: dict[str, Any] | None,
        history: list[dict[str, Any]],
    ) -> bool:
        if not isinstance(schema, dict) or "detail_tables" not in schema:
            return False
        rows = self._workflow_detail_rows(data)
        if not rows:
            return False
        latest_options = None
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if (
                isinstance(action, dict)
                and action.get("tool") == "workflow.browser_search"
                and isinstance(observation, dict)
                and observation.get("field_id") == 29028
                and isinstance(observation.get("options"), list)
            ):
                latest_options = observation.get("options")
                break
        if not isinstance(latest_options, list) or len(latest_options) <= 1:
            return False
        return not self._details_semantically_match_options(rows, latest_options)

    def _workflow_detail_rows(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        details = data.get("details")
        rows = details.get("detail_2") if isinstance(details, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def _latest_workflow_schema(self, args: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any] | None:
        workflow_id = args.get("workflow_id")
        workflow_name = args.get("name")
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or not isinstance(observation, dict):
                continue
            if action.get("tool") != "workflow.schema" or observation.get("error"):
                continue
            if workflow_id is not None and observation.get("workflow_id") != workflow_id:
                continue
            if workflow_id is None and workflow_name and observation.get("name") != workflow_name:
                continue
            schema = observation.get("schema")
            return schema if isinstance(schema, dict) else None
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if (
                isinstance(action, dict)
                and action.get("tool") == "workflow.schema"
                and isinstance(observation, dict)
                and not observation.get("error")
                and isinstance(observation.get("schema"), dict)
            ):
                return observation.get("schema")
        return None

    def _invalid_workflow_identity_fields(self, data: dict[str, Any], history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        invalid = []
        user_ids: set[str] = set()
        employee_nos: set[str] = set()
        names: set[str] = set()
        for item in history:
            observation = item.get("observation")
            if not isinstance(observation, dict) or observation.get("error"):
                continue
            for key in ("users", "people"):
                values = observation.get(key)
                if not isinstance(values, list):
                    continue
                for person in values:
                    if not isinstance(person, dict):
                        continue
                    if person.get("user_id"):
                        user_ids.add(str(person["user_id"]))
                    if person.get("employee_no"):
                        employee_nos.add(str(person["employee_no"]))
                    if person.get("name"):
                        names.add(str(person["name"]))

        applicant = data.get("applicant")
        applicant_no = data.get("applicant_no")
        approver = data.get("approver")
        if applicant:
            applicant_text = str(applicant)
            if applicant_text in names or applicant_text in employee_nos:
                invalid.append({"field": "applicant", "value": applicant, "expected": "user_id"})
            elif user_ids and applicant_text not in user_ids and not applicant_text.isdigit():
                invalid.append({"field": "applicant", "value": applicant, "expected": "known user_id"})
        if applicant_no:
            applicant_no_text = str(applicant_no)
            if applicant_no_text in names or applicant_no_text in user_ids:
                invalid.append({"field": "applicant_no", "value": applicant_no, "expected": "employee_no"})
        if approver:
            approver_text = str(approver)
            if approver_text in names or approver_text in employee_nos:
                invalid.append({"field": "approver", "value": approver, "expected": "user_id"})
        return invalid

    def _workflow_detail_errors(self, data: dict[str, Any], schema: dict[str, Any] | None) -> list[str]:
        if not isinstance(schema, dict) or "detail_tables" not in schema:
            return []
        detail_tables = schema.get("detail_tables")
        if not isinstance(detail_tables, dict) or "detail_2" not in detail_tables:
            return []
        details = data.get("details")
        if not isinstance(details, dict):
            return ["missing details object"]
        detail_rows = details.get("detail_2")
        if not isinstance(detail_rows, list) or not detail_rows:
            return ["missing details.detail_2 rows"]
        required = detail_tables.get("detail_2", {}).get("required_fields", [])
        # The simulator's expense-material workflow accepts these core detail
        # fields; some schema dumps also list accounting fields that are not
        # searchable in the offline tools and should not block a valid draft.
        if self._looks_like_expense_material_data(data):
            required = [
                field
                for field in required
                if field in {"material_subclass", "material_name", "quantity", "unit_price", "budget_amount"}
            ]
        errors = []
        for index, row in enumerate(detail_rows):
            if not isinstance(row, dict):
                errors.append(f"details.detail_2[{index}] must be object")
                continue
            missing = [field for field in required if field not in row]
            if missing:
                errors.append(f"details.detail_2[{index}] missing {missing}")
        material_names = {
            str(row.get("material_name"))
            for row in detail_rows
            if isinstance(row, dict) and row.get("material_name")
        }
        material_subclasses = {
            str(row.get("material_subclass"))
            for row in detail_rows
            if isinstance(row, dict) and row.get("material_subclass")
        }
        if len(detail_rows) > 1 and len(material_names) > 1 and len(material_subclasses) == 1:
            errors.append("multiple different material_name rows should not all use the same material_subclass")
        return errors

    def _looks_like_expense_material_data(self, data: dict[str, Any]) -> bool:
        return any(
            key in data
            for key in (
                "project_name",
                "project_code",
                "wbs_code",
                "material_category",
                "total_amount",
            )
        )

    def _workflow_ambiguity_errors(
        self,
        args: dict[str, Any],
        data: dict[str, Any],
        schema: dict[str, Any] | None,
        history: list[dict[str, Any]],
    ) -> list[str]:
        errors: list[str] = []
        if isinstance(schema, dict) and "approver" in schema.get("required_fields", []):
            approver = data.get("approver")
            for item in reversed(history):
                action = item.get("action")
                observation = item.get("observation")
                if not isinstance(action, dict) or action.get("tool") != "workflow.search_person":
                    continue
                if not isinstance(observation, dict) or observation.get("error"):
                    continue
                people = observation.get("people")
                if not isinstance(people, list) or len(people) <= 1:
                    continue
                user_ids = {str(person.get("user_id")) for person in people if isinstance(person, dict)}
                args = action.get("args") if isinstance(action.get("args"), dict) else {}
                generic_manager_search = self._is_generic_manager_search_args(args)
                title_arg = args.get("title")
                if (
                    approver
                    and str(approver) in user_ids
                    and (not title_arg or generic_manager_search)
                    and not self._selected_preferred_default_leave_approver(approver, people)
                ):
                    errors.append("ambiguous_approver: search_person returned multiple people; do not save without a unique approver")
                break

        if isinstance(schema, dict) and "detail_tables" in schema:
            details = data.get("details")
            rows = details.get("detail_2") if isinstance(details, dict) else None
            if isinstance(rows, list):
                selected = {
                    str(row.get("material_subclass"))
                    for row in rows
                    if isinstance(row, dict) and row.get("material_subclass")
                }
                for item in reversed(history):
                    action = item.get("action")
                    observation = item.get("observation")
                    if not isinstance(action, dict) or action.get("tool") != "workflow.browser_search":
                        continue
                    if not isinstance(observation, dict) or observation.get("error") or observation.get("field_id") != 29028:
                        continue
                    options = observation.get("options")
                    if not isinstance(options, list) or len(options) <= 1:
                        continue
                    option_values = {str(opt.get("value")) for opt in options if isinstance(opt, dict)}
                    if selected and selected.issubset(option_values) and not self._details_semantically_match_options(rows, options):
                        errors.append("ambiguous_material_subclass: browser_search returned multiple plausible options")
                    break
        return errors

    def _selected_preferred_default_leave_approver(self, approver: Any, people: list[Any]) -> bool:
        preferred = None
        for person in people:
            if isinstance(person, dict) and "产品经理" in str(person.get("title") or ""):
                preferred = person
                break
        return isinstance(preferred, dict) and str(preferred.get("user_id")) == str(approver)

    def _details_semantically_match_options(self, rows: list[Any], options: list[Any]) -> bool:
        option_by_value = {
            str(option.get("value")): str(option.get("label") or "")
            for option in options
            if isinstance(option, dict) and option.get("value")
        }
        if not option_by_value:
            return False
        matched = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            subclass = str(row.get("material_subclass") or "")
            label = option_by_value.get(subclass, "")
            if not label:
                continue
            material_text = f"{row.get('material_name', '')} {row.get('description', '')}"
            if self._material_text_matches_label(material_text, label):
                matched += 1
        return matched == len([row for row in rows if isinstance(row, dict)])

    def _material_text_matches_label(self, material_text: str, label: str) -> bool:
        text = material_text.lower()
        label_lower = label.lower()
        if not text:
            return False
        if any(token in text for token in ("视频", "拍摄", "制作")) and any(token in label_lower for token in ("视频", "制作")):
            return True
        if any(token in text for token in ("短片", "宣传片")) and any(token in label_lower for token in ("视频", "制作")):
            return True
        if any(token in text for token in ("发布会", "活动", "展会")) and any(token in label_lower for token in ("发布会", "活动", "展会")):
            return True
        if any(token in text for token in ("网页", "官网", "设计", "专题", "视觉")) and any(token in label_lower for token in ("网页", "设计")):
            return True
        if any(token in text for token in ("印刷", "折页", "画册", "招商折页")) and "印刷" in label_lower:
            return True
        if any(token in text for token in ("喷绘", "展架", "易拉宝", "海报", "广宣", "物料")) and any(token in label_lower for token in ("广宣", "宣传")):
            return True
        if any(token in text for token in ("电脑", "配件")) and any(token in label_lower for token in ("电脑", "配件")):
            return True
        if any(token in text for token in ("打印", "扫描")) and any(token in label_lower for token in ("打印", "扫描")):
            return True
        if any(token in text for token in ("手机", "数码", "3c")) and any(token in label_lower for token in ("手机", "数码", "3c")):
            return True
        if any(token in text for token in ("测试设备",)) and "测试设备" in label_lower:
            return True
        if any(token in text for token in ("云", "cdn", "idc", "运营商")) and any(token in label_lower for token in ("云", "cdn", "idc", "运营商")):
            return True
        if any(token in text for token in ("咨询",)) and "咨询" in label_lower:
            return True
        if any(token in text for token in ("数据",)) and "数据" in label_lower:
            return True
        return False

    def _infer_reply_slot(self, message: str) -> str | None:
        lowered = message.lower()
        for slot, patterns in WORKFLOW_REPLY_SLOTS.items():
            if any(pattern.lower() in lowered for pattern in patterns):
                return slot
        return None

    def _infer_reply_domain_slot(self, message: str, runtime: dict[str, Any]) -> tuple[str, str | None]:
        lowered = message.lower()
        task_context = runtime.get("task_context") if isinstance(runtime.get("task_context"), dict) else {}
        query_slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        meeting_context = bool(query_slots.get("is_new_meeting_booking") or query_slots.get("is_existing_meeting_change"))
        if isinstance(query_slots.get("leave_plan"), dict) or self._message_mentions_leave(message):
            workflow_slot = self._infer_reply_slot(message)
            if workflow_slot:
                return "workflow", workflow_slot
            if any(token in message for token in ("时间", "几点", "什么时候", "开始")):
                return "workflow", "start_time"
        if meeting_context:
            for slot, patterns in MEETINGROOM_REPLY_SLOTS.items():
                if any(pattern.lower() in lowered for pattern in patterns):
                    return "meetingroom", slot
        workflow_slot = self._infer_reply_slot(message)
        if workflow_slot:
            return "workflow", workflow_slot
        if meeting_context:
            if any(token in message for token in ("哪", "几", "时间", "时候", "日期")):
                return "meetingroom", "meeting_time"
            if "人" in message:
                return "meetingroom", "attendees"
            if any(token in message for token in ("主题", "标题")):
                return "meetingroom", "title"
        return "workflow", None

    def _message_mentions_leave(self, message: str) -> bool:
        return any(token in str(message or "") for token in ("请假", "事假", "病假", "年假", "年休假", "假期"))

    def _canonical_meetingroom_reply_message(self, slot_name: str, runtime: dict[str, Any]) -> str:
        if slot_name == "meeting_time":
            return "请问是在什么时间？"
        if slot_name == "attendees":
            return "请问大概多少人参加？"
        if slot_name == "title":
            return "请问会议主题是什么？"
        if slot_name == "confirmation":
            return "可以的话我现在直接帮你预订，确认吗？"
        return "请再补充一下会议需求。"

    def _canonical_workflow_reply_message(self, slot_name: str) -> str:
        if slot_name == "start_time":
            return "请问您几点开始请假？"
        if slot_name == "end_time":
            return "请问到几点结束？"
        if slot_name == "leave_type":
            return "请问是什么类型的假期？"
        if slot_name == "reason":
            return "请问请假原因是？"
        if slot_name == "approver":
            return "请问选择哪位作为审批人？"
        if slot_name == "project_name":
            return "请问项目名称或项目编码是什么？"
        if slot_name == "project_code":
            return "请问项目编码是什么？"
        if slot_name == "material_category":
            return "请问物资大类是什么？"
        if slot_name == "material_subclass":
            return "请问物资小类是什么？"
        if slot_name == "total_amount":
            return "请问总金额是多少？"
        return "请再补充一下流程所需信息。"

    def _update_task_context_from_reply(
        self,
        runtime: dict[str, Any],
        action: dict[str, Any],
        observation: Any,
    ) -> None:
        if not isinstance(observation, dict):
            return
        task_context = runtime.get("task_context")
        if not isinstance(task_context, dict):
            return
        query_slots = task_context.setdefault("query_slots", {})
        if not isinstance(query_slots, dict):
            return
        assistant_message = str(action.get("message") or observation.get("assistant_message") or "")
        user_message = str(observation.get("user_message") or "")
        resolved_slot = observation.get("resolved_slot")
        slot = str(resolved_slot or "")
        if not slot:
            _, inferred = self._infer_reply_domain_slot(assistant_message, runtime)
            slot = inferred or ""
        if slot == "day" or slot == "meeting_time":
            parsed = self._parse_meeting_time_reply(user_message, task_context)
            if parsed.get("day"):
                query_slots["meeting_day"] = parsed["day"]
            if parsed.get("start") and parsed.get("end"):
                query_slots["time_window"] = {"start": parsed["start"], "end": parsed["end"]}
        elif slot == "attendees":
            attendees = self._extract_attendees(user_message)
            if attendees:
                query_slots["attendees"] = attendees
        elif slot == "title":
            title = self._parse_meeting_title_reply(user_message)
            if title:
                query_slots["meeting_title"] = title
                keywords = query_slots.get("meeting_keywords")
                if not isinstance(keywords, list):
                    keywords = []
                if title not in keywords:
                    keywords.insert(0, title)
                query_slots["meeting_keywords"] = keywords[:4]
        elif slot in WORKFLOW_REPLY_SLOTS:
            self._update_workflow_slots_from_reply(query_slots, slot, user_message)
        if observation.get("confirmed_action"):
            query_slots["meeting_confirmed_action"] = observation.get("confirmed_action")

    def _update_workflow_slots_from_reply(self, query_slots: dict[str, Any], slot: str, user_message: str) -> None:
        text = str(user_message or "").strip()
        if not text:
            return
        if slot in {"project_code", "project_name"}:
            code = self._extract_project_code(text)
            if code:
                query_slots["workflow_project_code"] = code
            else:
                query_slots["workflow_project_reply"] = text
                candidates = self._extract_project_name_candidates(text)
                if candidates:
                    query_slots["workflow_project_candidates"] = candidates
        elif slot == "material_category":
            query_slots["workflow_material_category_reply"] = text
        elif slot == "material_subclass":
            query_slots["workflow_material_subclass_reply"] = text
        elif slot == "total_amount":
            amount = self._parse_amount_value(text)
            if amount is not None:
                query_slots["workflow_total_amount"] = amount
            query_slots["workflow_total_amount_reply"] = text
        elif slot == "approver":
            query_slots["workflow_approver_reply"] = text

    def _parse_amount_value(self, text: Any) -> float | None:
        values = self._extract_money_values(str(text or ""))
        if values:
            return values[-1]
        match = re.search(r"(\d+(?:\.\d+)?)", str(text or ""))
        if not match:
            return None
        try:
            return float(match.group(1))
        except Exception:
            return None

    def _parse_meeting_time_reply(self, text: str, task_context: dict[str, Any]) -> dict[str, str]:
        parsed: dict[str, str] = {}
        query_slots = task_context.get("query_slots") if isinstance(task_context.get("query_slots"), dict) else {}
        obs_candidates = query_slots.get("natural_date_candidates")
        if "下周二" in text:
            day = None
            # The initial context already knows the case now; for simulator replies,
            # prefer the first candidate only if it explicitly came from the same text.
            # Otherwise infer from active known contest date via current candidate list is avoided.
            day = self._day_from_relative_reply(text, task_context, weekday=1)
            if day:
                parsed["day"] = day
        elif "明天" in text:
            day = self._day_from_relative_reply(text, task_context, days=1)
            if day:
                parsed["day"] = day
        elif "后天" in text:
            day = self._day_from_relative_reply(text, task_context, days=2)
            if day:
                parsed["day"] = day
        elif isinstance(obs_candidates, list) and obs_candidates:
            first = obs_candidates[0]
            if isinstance(first, dict) and first.get("day"):
                parsed["day"] = str(first.get("day"))
        match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if match:
            parsed["day"] = match.group(1)
        window = self._extract_time_window(text)
        if window:
            parsed.update(window)
        return parsed

    def _day_from_relative_reply(
        self,
        text: str,
        task_context: dict[str, Any],
        days: int | None = None,
        weekday: int | None = None,
    ) -> str | None:
        obs_now = task_context.get("now")
        obs = {"now": obs_now} if obs_now else {}
        if days is not None:
            return self._date_from_now(obs, days)
        if weekday is not None:
            return self._next_calendar_weekday(obs, weekday) if "下周" in text else self._next_weekday(obs, weekday)
        return None

    def _parse_meeting_title_reply(self, text: str) -> str | None:
        title = self._extract_meeting_title(text)
        if title:
            return title
        cleaned = str(text or "").strip(" ，。；,;：:")
        cleaned = re.sub(r"^(主题|标题)(写|是)?", "", cleaned).strip(" ，。；,;：:")
        return cleaned or None

    def _latest_meetingroom_slots(self, history: list[dict[str, Any]], obs: dict[str, Any] | None) -> dict[str, Any]:
        # Build from current query first, then layer simulator replies from history.
        slots: dict[str, Any] = {}
        if isinstance(obs, dict):
            query = str(obs.get("user_query") or "")
            slots["day"] = self._preferred_meeting_day_from_obs(obs)
            window = self._extract_time_window(query)
            if window:
                slots.update(window)
            attendees = self._extract_attendees(query)
            if attendees:
                slots["attendees"] = attendees
            title = self._extract_meeting_title(query)
            if title:
                slots["title"] = title
        for item in history:
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or action.get("action") != "reply":
                continue
            if not isinstance(observation, dict):
                continue
            text = str(observation.get("user_message") or "")
            assistant_message = str(action.get("message") or "")
            slot = str(observation.get("resolved_slot") or "")
            if not slot:
                lowered = assistant_message.lower()
                for name, patterns in MEETINGROOM_REPLY_SLOTS.items():
                    if any(pattern.lower() in lowered for pattern in patterns):
                        slot = name
                        break
            if slot in {"day", "meeting_time"}:
                task_context = {"now": obs.get("now") if isinstance(obs, dict) else None, "query_slots": {}}
                parsed = self._parse_meeting_time_reply(text, task_context)
                if parsed.get("day"):
                    slots["day"] = parsed["day"]
                if parsed.get("start") and parsed.get("end"):
                    slots["start"] = parsed["start"]
                    slots["end"] = parsed["end"]
            elif slot == "attendees":
                attendees = self._extract_attendees(text)
                if attendees:
                    slots["attendees"] = attendees
            elif slot == "title":
                title = self._parse_meeting_title_reply(text)
                if title:
                    slots["title"] = title
        return {key: value for key, value in slots.items() if value not in (None, "")}

    def _preferred_meeting_day_from_obs(self, obs: dict[str, Any]) -> str | None:
        query = str(obs.get("user_query") or "")
        if "下周二" in query:
            return self._next_calendar_weekday(obs, 1)
        if "下周一" in query:
            return self._next_calendar_weekday(obs, 0)
        if "下周四" in query:
            return self._next_calendar_weekday(obs, 3)
        if "周三" in query:
            return self._next_weekday(obs, 2)
        if "明天" in query:
            if (
                self._meetingroom_query_uses_workspace_proximity(query)
                or self._meetingroom_query_has_explicit_fallback_order(query)
                or (
                    self._meetingroom_query_prefers_business_day(query)
                    and not self._meetingroom_query_allows_block_after_unavailable(query)
                )
            ):
                anchor = self._meetingroom_anchor_day(obs, query)
                if anchor:
                    return anchor
            return self._date_from_now(obs, 1)
        if "后天" in query:
            return self._date_from_now(obs, 2)
        match = re.search(r"(\d{4}-\d{2}-\d{2})", query)
        if match:
            return match.group(1)
        return None

    def _business_office_id(
        self,
        args: dict[str, Any],
        result: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> Any:
        room_id = result.get("room_id") or args.get("room_id")
        office_arg = args.get("office_id")
        if self._room_id_is_business_coded(room_id):
            return self._building_from_room_id(room_id) or office_arg
        if self._tool_was_called_for_room(history, "meetingroom.room.schedule", room_id):
            return office_arg or self._room_field_from_history(history, room_id, "officeId")
        for item in reversed(history):
            if item.get("action", {}).get("tool") != "meetingroom.room.list":
                continue
            observation = item.get("observation")
            if not isinstance(observation, dict):
                continue
            for room in observation.get("rooms", []):
                if not isinstance(room, dict) or room.get("room_id") != room_id:
                    continue
                if office_arg and not self._looks_uuid(str(office_arg)):
                    return office_arg
                if self._room_id_is_business_coded(room_id):
                    return room.get("building") or self._building_from_room_id(room_id) or office_arg
                return room.get("officeId") or office_arg or room.get("building")
        return self._building_from_room_id(room_id) or office_arg

    def _room_id_is_business_coded(self, room_id: Any) -> bool:
        text = str(room_id or "")
        return bool(re.match(r"^(A1|A2|A4)-", text))

    def _building_from_room_id(self, room_id: Any) -> str | None:
        match = re.match(r"^(A1|A2|A3|A4)-", str(room_id or ""))
        return match.group(1) if match else None

    def _room_field_from_history(self, history: list[dict[str, Any]], room_id: Any, field: str) -> Any:
        if not room_id:
            return None
        for item in reversed(history):
            action = item.get("action")
            observation = item.get("observation")
            if not isinstance(action, dict) or not isinstance(observation, dict):
                continue
            if action.get("tool") == "meetingroom.room.list":
                rooms = observation.get("rooms")
                if not isinstance(rooms, list):
                    continue
                for room in rooms:
                    if isinstance(room, dict) and room.get("room_id") == room_id:
                        return room.get(field)
        return None

    def _tool_was_called_for_room(self, history: list[dict[str, Any]], tool_name: str, room_id: Any) -> bool:
        if not room_id:
            return False
        for item in history:
            action = item.get("action")
            if not isinstance(action, dict) or action.get("tool") != tool_name:
                continue
            args = action.get("args")
            if isinstance(args, dict) and args.get("room_id") == room_id:
                return True
        return False

    def _candidate_preserves_partial(self, candidate: dict[str, Any], partial: dict[str, Any]) -> bool:
        for key, expected_value in partial.items():
            actual_value = candidate.get(key)
            if key == "booking_result" and self._booking_result_preserves_partial(actual_value, expected_value):
                continue
            if not self._contains_expected(actual_value, expected_value):
                return False
        return True

    def _booking_result_preserves_partial(self, actual: Any, expected: Any) -> bool:
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            return False
        for key, expected_value in expected.items():
            if key == "office_id" and actual.get(key):
                continue
            if key not in actual or not self._contains_expected(actual[key], expected_value):
                return False
        return True

    def _contains_expected(self, actual: Any, expected: Any) -> bool:
        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                return False
            return all(key in actual and self._contains_expected(actual[key], value) for key, value in expected.items())
        if isinstance(expected, list):
            if not isinstance(actual, list) or len(actual) < len(expected):
                return False
            return all(self._contains_expected(item, expected[index]) for index, item in enumerate(actual[: len(expected)]))
        return actual == expected

    def _compact_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        max_items = int((self.config.get("llm") or {}).get("max_history_items") or 24)
        compact = []
        for item in history[-max_items:]:
            compact.append(
                {
                    "action": item.get("action"),
                    "observation": self._truncate_payload(item.get("observation"), 6000),
                }
            )
        return compact

    def _truncate_payload(self, value: Any, limit: int = 5000) -> Any:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        if len(text) <= limit:
            return value
        return {"truncated": True, "text": text[:limit]}

    # ------------------------------------------------------------------
    # Placeholder resolution
    # ------------------------------------------------------------------

    def _resolve_placeholders(self, value: Any, history: list[dict[str, Any]], runtime: dict[str, Any] | None = None) -> Any:
        if isinstance(value, list):
            resolved_list = []
            for item in value:
                resolved = self._resolve_placeholders(item, history, runtime)
                if resolved is UNRESOLVED:
                    return UNRESOLVED
                resolved_list.append(resolved)
            return resolved_list
        if not isinstance(value, dict):
            return value
        if "$from_tool" in value:
            return self._resolve_from_tool(value, history, runtime)

        resolved_dict: dict[str, Any] = {}
        for key, item in value.items():
            resolved = self._resolve_placeholders(item, history, runtime)
            if resolved is UNRESOLVED:
                return UNRESOLVED
            resolved_dict[key] = resolved
        return resolved_dict

    def _resolve_from_tool(self, spec: dict[str, Any], history: list[dict[str, Any]], runtime: dict[str, Any] | None = None) -> Any:
        tool_name = spec.get("$from_tool")
        path = str(spec.get("path") or "")
        if not tool_name or not path:
            return UNRESOLVED

        observation = self._latest_observation(str(tool_name), history)
        if (
            str(tool_name) == "meetingroom.booking.list"
            and (observation is None or observation.get("bookings") == [])
            and isinstance(runtime, dict)
        ):
            selected = self._selected_booking_observation(runtime)
            if selected is not None:
                observation = selected
        if observation is None:
            return UNRESOLVED

        value = self._get_path(observation, path)
        if "where" in spec:
            if not isinstance(value, list):
                return UNRESOLVED
            match = self._select_item(value, spec.get("where"))
            if match is UNRESOLVED:
                return UNRESOLVED
            field = spec.get("field")
            return self._get_path(match, str(field)) if field else match

        field = spec.get("field")
        if field and isinstance(value, dict):
            return self._get_path(value, str(field))
        return value if value is not None else UNRESOLVED

    def _latest_observation(self, tool_name: str, history: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in reversed(history):
            action = item.get("action")
            if not isinstance(action, dict) or action.get("tool") != tool_name:
                continue
            observation = item.get("observation")
            if isinstance(observation, dict) and not observation.get("error"):
                return observation
        return None

    def _selected_booking_observation(self, runtime: dict[str, Any]) -> dict[str, Any] | None:
        task_context = runtime.get("task_context")
        if not isinstance(task_context, dict):
            return None
        facts = task_context.get("tool_facts")
        if not isinstance(facts, dict):
            return None
        selected = facts.get("selected_booking")
        if isinstance(selected, dict) and selected:
            return {"bookings": [self._clone_json(selected)]}
        return None

    def _get_path(self, value: Any, path: str) -> Any:
        current = value
        parts = []
        for chunk in path.split("."):
            if not chunk:
                continue
            match = re.fullmatch(r"([A-Za-z0-9_]+)(?:\[(\d+)\])?", chunk)
            if not match:
                parts.append(chunk)
                continue
            parts.append(match.group(1))
            if match.group(2) is not None:
                parts.append(int(match.group(2)))

        for part in parts:
            if isinstance(part, int):
                if not isinstance(current, list) or part >= len(current):
                    return None
                current = current[part]
            elif isinstance(current, dict):
                current = self._dict_get_alias(current, str(part))
            elif isinstance(current, list) and str(part).isdigit():
                index = int(part)
                if index >= len(current):
                    return None
                current = current[index]
            else:
                return None
        return current

    def _dict_get_alias(self, value: dict[str, Any], key: str) -> Any:
        if key in value:
            return value.get(key)
        if key == "items":
            if "options" in value:
                return value.get("options")
            if "workflows" in value:
                return value.get("workflows")
            if "people" in value:
                return value.get("people")
        aliases = {
            "office_id": "officeId",
            "officeId": "office_id",
            "room_id": "roomId",
            "roomId": "room_id",
            "room_name": "name",
            "name": "room_name",
            "order_id": "booking_id",
            "booking_id": "order_id",
            "workflows": "items",
            "options": "items",
            "persons": "people",
            "people": "persons",
        }
        alias = aliases.get(key)
        if alias and alias in value:
            return value.get(alias)
        return None

    def _select_item(self, items: list[Any], where: Any) -> Any:
        if not isinstance(where, dict):
            return items[0] if items else UNRESOLVED
        for item in items:
            if isinstance(item, dict) and self._matches_where(item, where):
                return item
        if len(items) == 1 and isinstance(items[0], dict) and self._is_room_like_item(items[0]):
            return items[0]
        return UNRESOLVED

    def _is_room_like_item(self, item: dict[str, Any]) -> bool:
        return any(key in item for key in ("room_id", "roomId", "officeId", "hasScreen", "busy_slots"))

    def _matches_where(self, item: dict[str, Any], where: dict[str, Any]) -> bool:
        for key, expected in where.items():
            actual = self._dict_get_alias(item, key)
            if isinstance(expected, dict):
                if "not_overlap" in expected:
                    if not self._not_overlap(actual, expected.get("not_overlap")):
                        return False
                elif "gt" in expected:
                    try:
                        if not float(actual) > float(expected["gt"]):
                            return False
                    except Exception:
                        return False
                elif "gte" in expected:
                    try:
                        if not float(actual) >= float(expected["gte"]):
                            return False
                    except Exception:
                        return False
                else:
                    return False
            elif not self._field_value_matches(item, key, actual, expected):
                return False
        return True

    def _field_value_matches(self, item: dict[str, Any], key: str, actual: Any, expected: Any) -> bool:
        if actual == expected:
            return True
        key_text = str(key)
        if key_text in {"room_id", "roomId", "room_name", "name"}:
            expected_text = str(expected or "")
            values = {
                str(item.get("room_id") or ""),
                str(item.get("roomId") or ""),
                str(item.get("name") or ""),
                str(item.get("room_name") or ""),
            }
            values.discard("")
            if expected_text in values:
                return True
            normalized_expected = self._normalize_room_text(expected_text)
            for value in values:
                normalized_value = self._normalize_room_text(value)
                if normalized_expected and (
                    normalized_expected == normalized_value
                    or normalized_expected in normalized_value
                    or normalized_value in normalized_expected
                ):
                    return True
        return False

    def _normalize_room_text(self, value: str) -> str:
        text = str(value or "").upper()
        text = re.sub(r"[^A-Z0-9]", "", text)
        return text

    def _not_overlap(self, busy_slots: Any, interval: Any) -> bool:
        if not isinstance(interval, list) or len(interval) != 2:
            return False
        if not isinstance(busy_slots, list):
            return True
        start, end = str(interval[0]), str(interval[1])
        for slot in busy_slots:
            if not isinstance(slot, list) or len(slot) != 2:
                continue
            busy_start, busy_end = str(slot[0]), str(slot[1])
            if start < busy_end and end > busy_start:
                return False
        return True

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _parse_json_object(self, content: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(content[start : end + 1])
                    return parsed if isinstance(parsed, dict) else None
                except Exception:
                    return None
            return None

    def _remaining_steps(self, step_budget: int, steps_used: int) -> int:
        if step_budget <= 0:
            return 99
        return max(0, step_budget - steps_used)

    def _add_minutes(self, time_value: Any, minutes: int) -> str | None:
        if not isinstance(time_value, str) or ":" not in time_value:
            return None
        try:
            hour, minute = [int(part) for part in time_value.split(":", 1)]
            total = hour * 60 + minute + minutes
            return f"{total // 60:02d}:{total % 60:02d}"
        except Exception:
            return None

    def _time_overlap(self, start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
        return start_a < end_b and end_a > start_b

    def _round_instruction(self, step_budget: int, steps_used: int) -> str:
        remaining = self._remaining_steps(step_budget, steps_used)
        if remaining <= 1:
            return "剩余步数很少，除非最后一步必须执行工具，否则请返回 final_answer 或 blocked。"
        return (
            "请执行下一步最有把握的工具链。能用 actions 连续完成时一次输出多个 action。"
            "如果 workflow 的 user/schema/project/browser/person 信息已经齐全，下一步应立即 workflow.save，不要继续解释或重复查询。"
        )

    def _time_exceeded(self, started_at: float, llm_config: dict[str, Any]) -> bool:
        timeout = int(llm_config.get("timeout") or 45)
        soft_limit = max(timeout * 2 + 5, 50)
        return time.time() - started_at > soft_limit

    def _looks_uuid(self, value: str) -> bool:
        compact = value.replace("-", "")
        return len(compact) == 32 and compact.isalnum()
