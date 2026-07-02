"""
iftk Contest Simulator — Gym-style offline environment.

Usage:
    env = IFTKEnv("cases/")
    obs = env.reset("beta_mr_wf_0001")
    tools = env.list_tools()
    result = env.call_tool("meetingroom.room.list", {"day": "2026-04-21", "office_id": "A1"})
    final_answer = {"booking_result": {...}, "workflow_draft_result": {...}}
    score = env.done(final_answer)  # platform runner / local scoring
"""

import json
import time
from pathlib import Path
from typing import Any

try:
    from .tools.meetingroom import MeetingroomTool
    from .tools.oa import OATool
    from .tools.user import UserTool
    from .tools.workflow import WorkflowTool
    from .tools.file import FileTool
    from .evaluator import Evaluator
except ImportError:
    from tools.meetingroom import MeetingroomTool
    from tools.oa import OATool
    from tools.user import UserTool
    from tools.workflow import WorkflowTool
    from tools.file import FileTool
    from evaluator import Evaluator


class StepLimitExceeded(Exception):
    pass


SLOT_PATTERNS = {
    # 会议室相关
    "day": ["哪天", "什么时候", "时间", "几点", "时段"],
    "office_id": ["园区", "地点", "哪个楼", "在哪"],
    "attendees": ["多少人", "几个人", "人数"],
    "title": ["主题", "标题", "会议名称"],
    "order_id": ["订单号", "order id", "order_id", "编号", "预订号"],
    # 请假相关
    "start_time": ["几点开始", "开始时间", "从几点", "什么时候开始", "几点到几点", "请假时间"],
    "end_time": ["几点结束", "结束时间", "到几点", "什么时候结束"],
    "leave_type": ["什么类型", "假期类型", "哪种假", "类型", "什么假", "请什么假"],
    "reason": ["原因", "为什么", "什么事", "请假原因"],
    "approver": ["审批人", "谁审批", "找谁批", "审批"],
    # 费用类相关
    "project_name": ["项目", "项目名称", "哪个项目", "归哪个项目"],
    "project_code": ["项目编码", "project code", "项目code", "项目编号"],
    "material_category": ["大类", "物资大类", "费用大类", "选哪个大类"],
    "material_subclass": ["小类", "物资小类", "具体小类", "选哪个小类"],
    "total_amount": ["预算", "金额", "总金额", "多少钱", "预算多少"],
}

CONFIRM_PATTERNS = [
    "确认",
    "可以直接订",
    "直接帮你预订",
    "现在帮你预订",
]


class IFTKEnv:
    TOOL_REGISTRY = {
        "meetingroom.room.list":      (MeetingroomTool, "room_list"),
        "meetingroom.room.bookings":  (MeetingroomTool, "room_schedule"),
        "meetingroom.room.schedule":  (MeetingroomTool, "room_schedule"),
        "meetingroom.booking.list":   (MeetingroomTool, "booking_list"),
        "meetingroom.booking.create": (MeetingroomTool, "booking_create"),
        "meetingroom.booking.cancel": (MeetingroomTool, "booking_cancel"),
        "meetingroom.booking.extend": (MeetingroomTool, "booking_extend"),
        "meetingroom.booking.participant.list": (MeetingroomTool, "booking_participant_list"),
        "meetingroom.booking.participant.add": (MeetingroomTool, "booking_participant_add"),
        "meetingroom.booking.participant.remove": (MeetingroomTool, "booking_participant_remove"),
        "oa.todo.list":              (OATool,         "todo_list"),
        "oa.done.list":              (OATool,         "done_list"),
        "user.get_info":              (UserTool,        "get_info"),
        "user.get_workspace":         (UserTool,        "get_workspace"),
        "workflow.catalog":           (WorkflowTool,    "catalog"),
        "workflow.schema":            (WorkflowTool,    "schema"),
        "workflow.search_person":     (WorkflowTool,    "search_person"),
        "workflow.browser_search":    (WorkflowTool,    "browser_search"),
        "workflow.project_search":    (WorkflowTool,    "project_search"),
        "workflow.save":              (WorkflowTool,    "save"),
        "workflow.delete":            (WorkflowTool,    "delete"),
        "file.list":                  (FileTool,        "list"),
    }

    def __init__(
        self,
        cases_dir: str = "cases/",
        tool_specs_path: str | Path | None = None,
        workflow_data_path: str | Path | None = None,
        meetingroom_data_path: str | Path | None = None,
    ):
        self._cases_dir = Path(cases_dir)
        if tool_specs_path is None:
            tool_specs_path = Path(__file__).resolve().parent.parent / "tool_specs.json"
        self._tool_specs_path = Path(tool_specs_path)
        if workflow_data_path is None:
            workflow_data_path = self._cases_dir.parent / "data" / "workflow_data.json"
        self._workflow_data_path = Path(workflow_data_path)
        if meetingroom_data_path is None:
            meetingroom_data_path = self._cases_dir.parent / "data" / "meetingroom_data.json"
        self._meetingroom_data_path = Path(meetingroom_data_path)
        self._case: dict | None = None
        self._state: dict | None = None
        self._history: list[dict] = []
        self._step_count = 0
        self._start_time: float = 0.0
        self._tool_instances: dict = {}
        self._active_case_id: str | None = None
        self._active_variant_id: str | None = None
        self._tool_specs = self._load_tool_specs()
        self._shared_workflow_data = self._load_shared_workflow_data()
        self._shared_meetingroom_data = self._load_shared_meetingroom_data()
        self._messages: list[dict[str, str]] = []
        self._dialogue_state: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self, case_id: str) -> dict:
        """Load a case and return the initial observation."""
        base_case_id, variant_id = self._parse_case_ref(case_id)
        path = self._cases_dir / f"{base_case_id}.json"
        with open(path, encoding="utf-8") as f:
            self._case = json.load(f)

        active_query = self._case["user_query"]
        if variant_id is not None:
            variant = next(
                (
                    item for item in self._case.get("robustness_variants", [])
                    if item["variant_id"] == variant_id
                ),
                None,
            )
            if variant is None:
                raise ValueError(f"Unknown variant_id: {variant_id}")
            active_query = variant["user_query"]

        state = self._merge_shared_workflows(self._case)
        self._state = self._merge_shared_meetingrooms(state)
        self._history = []
        self._step_count = 0
        self._start_time = time.time()
        self._tool_instances = {}
        self._active_case_id = base_case_id
        self._active_variant_id = variant_id
        self._dialogue_state = json.loads(json.dumps(self._case.get("dialogue_state", {})))

        mode = self._case.get("mode", "single_turn")
        if mode == "multi_turn":
            opening = self._case.get("opening_user_message", active_query)
            self._messages = [{"role": "user", "content": opening}]
        else:
            self._messages = [{"role": "user", "content": active_query}]

        return {
            "case_id": base_case_id,
            "variant_id": variant_id,
            "mode": mode,
            "user_query": active_query,
            "messages": json.loads(json.dumps(self._messages)),
            "now": self._case.get("now"),
            "step_budget": self._case["scoring"]["step_budget"],
        }

    def list_tools(self) -> list[dict]:
        """Return globally available tool schemas."""
        self._require_reset()
        return list(self._tool_specs.values())

    def call_tool(self, name: str, args: dict) -> dict:
        """Execute a tool and return its JSON result."""
        self._require_reset()

        budget = self._case["scoring"]["step_budget"]
        if self._step_count >= budget:
            raise StepLimitExceeded(f"Step budget {budget} exceeded.")

        if name not in self._tool_specs or name not in self.TOOL_REGISTRY:
            result = {"error": f"Unauthorized tool: {name}", "unauthorized": True}
        elif self._needs_confirmation(name):
            result = {
                "error": f"Action requires confirmation before execution: {name}",
                "needs_confirmation": True,
                "unconfirmed_action": True,
            }
        else:
            cls, method = self.TOOL_REGISTRY[name]
            if name not in self._tool_instances:
                self._tool_instances[name] = cls(self._state)
            try:
                result = getattr(self._tool_instances[name], method)(args)
            except Exception as e:
                result = {"error": str(e)}

        self._history.append({
            "step": self._step_count,
            "tool": name,
            "args": args,
            "result": result,
        })
        self._step_count += 1
        return result

    def reply(self, message: str) -> dict:
        """Send an assistant message and receive a simulated user reply."""
        self._require_reset()

        budget = self._case["scoring"]["step_budget"]
        if self._step_count >= budget:
            raise StepLimitExceeded(f"Step budget {budget} exceeded.")

        self._messages.append({"role": "assistant", "content": message})
        result = self._simulate_user_reply(message)
        self._messages.append({"role": "user", "content": result["user_message"]})

        self._history.append({
            "step": self._step_count,
            "action_type": "reply",
            "tool": "__reply__",
            "args": {"message": message},
            "result": result,
        })
        self._step_count += 1
        return {
            **result,
            "assistant_message": message,
            "messages": json.loads(json.dumps(self._messages)),
        }

    def done(self, final_answer: dict) -> dict:
        """Submit final answer and receive scores."""
        self._require_reset()
        elapsed = round(time.time() - self._start_time, 2)
        evaluator = Evaluator(self._case, self._state, self._history)
        scores = evaluator.evaluate(final_answer)
        scores["elapsed_seconds"] = elapsed
        scores["steps_used"] = self._step_count
        return scores

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_reset(self):
        if self._case is None:
            raise RuntimeError("Call env.reset(case_id) first.")

    def _parse_case_ref(self, case_ref: str) -> tuple[str, str | None]:
        if "::" not in case_ref:
            return case_ref, None
        base_case_id, variant_id = case_ref.split("::", 1)
        return base_case_id, variant_id

    def _needs_confirmation(self, action_name: str) -> bool:
        required = set(self._dialogue_state.get("confirmation_required_before", []))
        confirmed = set(self._dialogue_state.get("confirmed_actions", []))
        return action_name in required and action_name not in confirmed

    def _simulate_user_reply(self, message: str) -> dict:
        simulator = self._case.get("user_simulator", {})
        lowered = message.lower()

        if self._looks_like_confirmation(message):
            required = self._dialogue_state.get("confirmation_required_before", [])
            confirmed = self._dialogue_state.setdefault("confirmed_actions", [])
            action_name = required[0] if required else None
            if action_name and action_name not in confirmed:
                confirmed.append(action_name)
            return {
                "user_message": simulator.get("confirmation_reply", "可以，直接订吧。"),
                "confirmed_action": action_name,
            }

        for slot_name in list(self._dialogue_state.get("missing_slots", [])):
            patterns = SLOT_PATTERNS.get(slot_name, [])
            if any(pattern in lowered for pattern in patterns):
                self._dialogue_state["missing_slots"].remove(slot_name)
                self._dialogue_state.setdefault("collected_slots", []).append(slot_name)
                return {
                    "user_message": simulator.get("slot_replies", {}).get(
                        slot_name,
                        simulator.get("fallback_reply", "你再具体说一下。"),
                    ),
                    "resolved_slot": slot_name,
                }

        return {
            "user_message": simulator.get("fallback_reply", "你再具体说一下。"),
            "resolved_slot": None,
        }

    def _looks_like_confirmation(self, message: str) -> bool:
        return any(pattern in message for pattern in CONFIRM_PATTERNS)

    def _load_tool_specs(self) -> dict[str, dict]:
        with open(self._tool_specs_path, encoding="utf-8") as f:
            payload = json.load(f)

        if isinstance(payload, list):
            return {item["name"]: item for item in payload}
        return payload

    def _load_shared_workflow_data(self) -> dict[str, Any]:
        if not self._workflow_data_path.exists():
            return {
                "workflow_catalog": [],
                "workflow_schemas": {},
                "workflow_browser_options": {},
            }
        with open(self._workflow_data_path, encoding="utf-8") as f:
            return json.load(f)

    def _load_shared_meetingroom_data(self) -> dict[str, Any]:
        if not self._meetingroom_data_path.exists():
            return {"rooms": {}}
        with open(self._meetingroom_data_path, encoding="utf-8") as f:
            return json.load(f)

    def _merge_shared_workflows(self, case: dict) -> dict:
        world_state = json.loads(json.dumps(case["world_state"]))
        refs = case.get("workflow_refs", [])
        if not refs:
            return world_state

        shared_catalog = {
            item["workflow_id"]: json.loads(json.dumps(item))
            for item in self._shared_workflow_data.get("workflow_catalog", [])
        }
        merged_catalog = {
            item["workflow_id"]: json.loads(json.dumps(item))
            for item in world_state.get("workflow_catalog", [])
        }
        for workflow_id in refs:
            if workflow_id in shared_catalog:
                merged_catalog.setdefault(workflow_id, shared_catalog[workflow_id])
        if merged_catalog:
            world_state["workflow_catalog"] = list(merged_catalog.values())

        merged_schemas = {}
        for workflow_id in refs:
            schema = self._shared_workflow_data.get("workflow_schemas", {}).get(str(workflow_id))
            if schema is not None:
                merged_schemas[str(workflow_id)] = json.loads(json.dumps(schema))
        for workflow_id, schema in world_state.get("workflow_schemas", {}).items():
            merged_schemas[str(workflow_id)] = json.loads(json.dumps(schema))
        for workflow_id, override in case.get("workflow_overrides", {}).items():
            workflow_key = str(workflow_id)
            base = merged_schemas.get(workflow_key, {})
            merged_schemas[workflow_key] = self._deep_merge_dicts(
                base,
                override,
            )
        if merged_schemas:
            world_state["workflow_schemas"] = merged_schemas

        merged_browser_options = {}
        for key, options in self._shared_workflow_data.get("workflow_browser_options", {}).items():
            workflow_prefix = key.split(":", 1)[0]
            if workflow_prefix.isdigit() and int(workflow_prefix) in refs:
                merged_browser_options[key] = json.loads(json.dumps(options))
        for key, options in world_state.get("workflow_browser_options", {}).items():
            merged_browser_options[key] = json.loads(json.dumps(options))
        if merged_browser_options:
            world_state["workflow_browser_options"] = merged_browser_options

        return world_state

    def _merge_shared_meetingrooms(self, world_state: dict) -> dict:
        """Merge shared meetingroom data with case-specific refs and seed bookings."""
        state = json.loads(json.dumps(world_state))

        refs = state.get("meetingroom_refs", [])
        seed_bookings = state.get("meetingroom_seed_bookings", [])

        shared_rooms = self._shared_meetingroom_data.get("rooms", {})
        # 没有 refs 时使用全量房间
        room_ids = refs if refs else list(shared_rooms.keys())
        inventory = []

        for room_id in room_ids:
            if room_id in shared_rooms:
                room = json.loads(json.dumps(shared_rooms[room_id]))
                room["room_id"] = room_id
                room["busy_slots_by_day"] = {}
                inventory.append(room)

        if inventory:
            state["meetingroom_inventory"] = inventory

        bookings = []
        inventory_by_room_id = {
            room["room_id"]: room for room in state.get("meetingroom_inventory", [])
        }
        for seed in seed_bookings:
            booking = json.loads(json.dumps(seed))
            room_id = booking.get("room_id")
            if room_id is None:
                continue
            if "booking_id" not in booking and "order_id" in booking:
                booking["booking_id"] = booking["order_id"]
            if "order_id" not in booking and "booking_id" in booking:
                booking["order_id"] = booking["booking_id"]
            if "status" not in booking:
                booking["status"] = "active"
            room = inventory_by_room_id.get(room_id)
            if room is not None:
                booking.setdefault("office_id", room.get("officeId", room.get("building", room_id)))
                if booking.get("status") != "cancelled":
                    day = booking.get("day")
                    start = booking.get("start")
                    end = booking.get("end")
                    if day and start and end:
                        room.setdefault("busy_slots_by_day", {}).setdefault(day, []).append([start, end])
            bookings.append(booking)

        if bookings:
            state["bookings"] = bookings
        else:
            state.pop("bookings", None)

        return state

    def _deep_merge_dicts(self, base: dict, override: dict) -> dict:
        merged = json.loads(json.dumps(base))
        for key, value in override.items():
            current = merged.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                merged[key] = self._deep_merge_dicts(current, value)
            else:
                merged[key] = json.loads(json.dumps(value))
        return merged
