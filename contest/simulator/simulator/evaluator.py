"""
Evaluator: compute TSR, AS, ES, RS scores.
"""

from typing import Any


class Evaluator:
    def __init__(self, case: dict, state: dict, history: list[dict]):
        self._case = case
        self._state = state
        self._history = history

    def evaluate(self, final_answer: dict) -> dict:
        """Return scoring breakdown."""
        success_checks = self._check_success_conditions()
        tsr = self._task_success_rate(success_checks)
        submission_checks = self._check_submission(final_answer)
        violations = self._check_forbidden_conditions()
        task_passed = (
            all(item["passed"] for item in success_checks)
            and all(item["passed"] for item in submission_checks)
            and not violations
        )
        as_ = self._action_legality_score(submission_checks, violations)
        es = self._efficiency_score(success_checks)
        rs = 10.0 if task_passed else 0.0

        total = tsr + as_ + es + rs
        return {
            "TSR": tsr,
            "AS": as_,
            "ES": es,
            "RS": rs,
            "total": total,
            "max_total": 100,
            "task_passed": task_passed,
            "success_checks": success_checks,
            "submission_checks": submission_checks,
            "violations": violations,
        }

    # ------------------------------------------------------------------
    # TSR: Task Success Rate (60 points)
    # ------------------------------------------------------------------

    def _task_success_rate(self, success_checks: list[dict] | None = None) -> float:
        checks = success_checks if success_checks is not None else self._check_success_conditions()
        passed = sum(1 for item in checks if item["passed"])
        return 60.0 * (passed / len(checks)) if checks else 0.0

    def _eval_condition(self, cond: str) -> bool:
        """Simple rule-based checker (extend with AST/LLM for production)."""
        # Example: "存在成功会议预订，day=2026-04-21, office_id=A1, start=14:00, end=15:00"
        if cond.startswith("未调用过 "):
            called_form = cond.replace("未调用过 ", "调用过 ", 1)
            return not self._check_tool_called(called_form)
        if "存在成功会议预订" in cond:
            return self._check_booking_exists(cond)
        if "最终仅新增一条活跃会议预订" in cond:
            return self._check_single_created_active_booking()
        if cond.startswith("调用过 "):
            return self._check_tool_called(cond)
        if "存在已取消会议" in cond:
            return self._check_cancelled_booking_exists(cond)
        if "原会议已取消且存在更大会议室预订" in cond:
            return self._check_rebook_after_cancel(cond, require_larger_room=True)
        if "原会议已取消且存在新的成功会议预订" in cond:
            return self._check_rebook_after_cancel(cond, require_larger_room=False)
        if "会议室容量>=" in cond:
            return self._check_room_capacity(cond)
        if "预订的会议室可预订" in cond:
            return self._check_booked_room_bookable()
        if "预订的会议室有屏幕" in cond:
            return self._check_booked_room_has_screen()
        if "预订的会议室位于" in cond:
            return self._check_booked_room_campus(cond)
        if "预订的是离工位最近的会议室" in cond:
            return self._check_booked_room_nearest_to_workspace()
        if "存在会议室候选" in cond:
            return self._check_room_candidates_exist(cond)
        if "不存在新的成功会议预订" in cond:
            return self._check_no_created_active_bookings()
        if "存在请假流程提交" in cond or "存在费用类物资申请提交" in cond:
            return self._check_workflow_draft_exists(cond, required_status="submitted")
        if "存在请假流程草稿" in cond or "存在费用类物资申请草稿" in cond:
            return self._check_workflow_draft_exists(cond)
        if "请假草稿字段完整" in cond or "费用类物资申请草稿字段完整" in cond:
            return self._check_workflow_fields_complete(cond)
        if "包含 " in cond and ("请假草稿 " in cond or "费用类物资申请草稿 " in cond):
            return self._check_workflow_detail_row(cond)
        if "请假草稿 " in cond or "费用类物资申请草稿 " in cond:
            return self._check_workflow_draft_field_value(cond)
        return False

    def _find_room(self, room_id: str | None) -> dict | None:
        if room_id is None:
            return None
        return next(
            (room for room in self._state.get("meetingroom_inventory", []) if room["room_id"] == room_id),
            None,
        )

    def _room_has_feature(self, room: dict | None, feature: str) -> bool:
        if room is None:
            return False
        if feature == "screen":
            return room.get("hasScreen", False) or feature in room.get("features", [])
        return feature in room.get("features", [])

    def _infer_room_floor(self, room: dict | None) -> str | None:
        import re

        if room is None:
            return None
        floor = room.get("floor")
        if floor:
            return str(floor)

        room_id = str(room.get("room_id", ""))
        room_id_match = re.search(r"-(\d+F)-", room_id)
        if room_id_match:
            return room_id_match.group(1)

        name = str(room.get("name", ""))
        name_match = re.search(r"(\d+)楼", name)
        if name_match:
            return f"{name_match.group(1)}F"
        return None

    def _infer_room_area(self, room: dict | None) -> str | None:
        if room is None:
            return None
        area = room.get("area")
        if area:
            return str(area)

        name = str(room.get("name", ""))
        if "北区" in name:
            return "北区"
        if "南区" in name:
            return "南区"
        return None

    def _workspace_context(self) -> dict[str, str | None]:
        user = next(
            (
                item for item in self._state.get("users", [])
                if item.get("user_id") == self._state.get("current_user_id")
            ),
            None,
        )
        if user is None:
            users = self._state.get("users", [])
            user = users[0] if users else None
        workspace = user.get("workspace", {}) if user else {}

        office_address = str(workspace.get("office_address", ""))
        office_name = str(workspace.get("office_name", ""))
        parts = office_address.split("_") if office_address else []
        campus = None
        if parts:
            if parts[0] == "0552":
                campus = "小镇"
            elif parts[0] == "0551":
                campus = "合肥"

        area = workspace.get("area")
        if not area:
            if "北区" in office_name:
                area = "北区"
            elif "南区" in office_name:
                area = "南区"

        return {
            "campus": campus,
            "building": parts[1] if len(parts) >= 2 else None,
            "floor": parts[2] if len(parts) >= 3 else None,
            "area": str(area) if area else None,
        }

    def _room_workspace_rank(self, room: dict, workspace: dict[str, str | None]) -> tuple[int, int, int]:
        room_floor = self._infer_room_floor(room)
        room_building = str(room.get("building", "")) or None
        room_area = self._infer_room_area(room)
        same_floor = int(
            workspace.get("floor") is not None
            and workspace.get("building") is not None
            and room_floor == workspace.get("floor")
            and room_building == workspace.get("building")
        )
        same_building = int(
            workspace.get("building") is not None
            and room_building == workspace.get("building")
        )
        same_area = int(
            workspace.get("area") is not None
            and room_area == workspace.get("area")
        )
        return (same_floor, same_building, same_area)

    def _parse_constraints(self, cond: str) -> dict[str, str]:
        import re

        return {
            key.strip(): value.strip()
            for key, value in re.findall(r"([A-Za-z0-9_.]+)=([^,]+)", cond)
        }

    def _get_nested_value(self, payload: dict[str, Any], key: str) -> Any:
        current: Any = payload
        for part in key.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _check_booking_exists(self, cond: str) -> bool:
        bookings = self._state.get("bookings", [])
        constraints = self._parse_constraints(cond)

        for b in bookings:
            if b.get("status") == "cancelled":
                continue
            match = True
            for k, v in constraints.items():
                if k == "office_id":
                    # office_id 既可能是 building 名（如 A1），也可能是 room.officeId / room_id
                    room = next(
                        (r for r in self._state.get("meetingroom_inventory", []) if r["room_id"] == b.get("room_id")),
                        None,
                    )
                    if room is None:
                        match = False
                        break
                    office_arg = b.get("office_id")
                    valid_office_values = {
                        str(v),
                        str(room.get("officeId", "")),
                        str(room.get("room_id", "")),
                        str(room.get("building", "")),
                    }
                    building_matches = str(room.get("building", "")) == v
                    office_id_matches = str(room.get("officeId", "")) == v
                    room_id_matches = str(room.get("room_id", "")) == v
                    if not (building_matches or office_id_matches or room_id_matches):
                        match = False
                        break
                    if str(office_arg) not in valid_office_values:
                        match = False
                        break
                elif str(b.get(k)) != v:
                    match = False
                    break
            if match:
                return True
        return False

    def _check_tool_called(self, cond: str) -> bool:
        import re

        tool_match = re.search(r"调用过\s+([A-Za-z0-9_.]+)", cond)
        if not tool_match:
            return False
        tool_name = tool_match.group(1)
        constraints = self._parse_constraints(cond)

        for item in self._history:
            if item.get("tool") != tool_name:
                continue
            args = item.get("args", {})
            if any(str(self._get_nested_value(args, key)) != value for key, value in constraints.items()):
                continue
            if item.get("result", {}).get("error"):
                continue
            return True
        return False

    def _check_cancelled_booking_exists(self, cond: str) -> bool:
        constraints = self._parse_constraints(cond)
        for booking in self._state.get("bookings", []):
            if booking.get("status") != "cancelled":
                continue
            if all(str(booking.get(key)) == value for key, value in constraints.items()):
                return True
        return False

    def _check_rebook_after_cancel(self, cond: str, require_larger_room: bool) -> bool:
        constraints = self._parse_constraints(cond)
        original_order_id = constraints.pop("original_order_id", None)
        if not original_order_id:
            return False

        original = next(
            (
                booking for booking in self._state.get("bookings", [])
                if booking.get("order_id", booking.get("booking_id")) == original_order_id
            ),
            None,
        )
        if original is None or original.get("status") != "cancelled":
            return False

        original_room = self._find_room(original.get("room_id"))
        original_capacity = original_room.get("capacity", 0) if original_room else 0

        for booking in self._state.get("bookings", []):
            booking_order_id = booking.get("order_id", booking.get("booking_id"))
            if booking_order_id == original_order_id or booking.get("status") == "cancelled":
                continue

            match = True
            for key, value in constraints.items():
                if key == "office_id":
                    room = self._find_room(booking.get("room_id"))
                    if room is None:
                        match = False
                        break
                    valid_office_values = {
                        str(room.get("officeId", "")),
                        str(room.get("room_id", "")),
                        str(room.get("building", "")),
                    }
                    office_arg = booking.get("office_id")
                    if value not in valid_office_values:
                        match = False
                        break
                    if str(office_arg) not in valid_office_values:
                        match = False
                        break
                elif str(booking.get(key)) != value:
                    match = False
                    break
            if not match:
                continue

            if require_larger_room:
                room = self._find_room(booking.get("room_id"))
                if room is None or room.get("capacity", 0) <= original_capacity:
                    continue
            return True
        return False

    def _created_active_bookings(self) -> list[dict]:
        created_order_ids = []
        for item in self._history:
            if item.get("tool") != "meetingroom.booking.create":
                continue
            result = item.get("result", {})
            if result.get("error"):
                continue
            booking_id = result.get("booking_id") or result.get("order_id")
            if booking_id:
                created_order_ids.append(str(booking_id))

        active = []
        for booking in self._state.get("bookings", []):
            booking_order_id = str(booking.get("order_id", booking.get("booking_id", "")))
            if booking_order_id not in created_order_ids:
                continue
            if booking.get("status") == "cancelled":
                continue
            active.append(booking)
        return active

    def _check_single_created_active_booking(self) -> bool:
        return len(self._created_active_bookings()) == 1

    def _check_no_created_active_bookings(self) -> bool:
        return len(self._created_active_bookings()) == 0

    def _check_room_capacity(self, cond: str) -> bool:
        # "会议室容量>=10"
        import re
        m = re.search(r">=(\d+)", cond)
        if not m:
            return False
        threshold = int(m.group(1))
        bookings = self._state.get("bookings", [])
        for b in bookings:
            if b.get("status") == "cancelled":
                continue
            room_id = b.get("room_id")
            room = self._find_room(room_id)
            if room and room["capacity"] >= threshold:
                return True
        return False

    def _check_booked_room_has_screen(self) -> bool:
        for b in self._state.get("bookings", []):
            if b.get("status") == "cancelled":
                continue
            room = self._find_room(b.get("room_id"))
            if self._room_has_feature(room, "screen"):
                return True
        return False

    def _check_booked_room_bookable(self) -> bool:
        for b in self._state.get("bookings", []):
            if b.get("status") == "cancelled":
                continue
            room = self._find_room(b.get("room_id"))
            if room is not None and room.get("bookable", True):
                return True
        return False

    def _check_booked_room_campus(self, cond: str) -> bool:
        # "预订的会议室位于A1园区（小镇）" — 检查 campus=="小镇" 或 building=="A1"
        import re
        campus_map = {"小镇": "小镇", "合肥": "合肥"}
        campus = next((v for k, v in campus_map.items() if k in cond), None)
        building = None
        m = re.search(r"([A-Z]\d+)园区", cond)
        if m:
            building = m.group(1)
        for b in self._state.get("bookings", []):
            if b.get("status") == "cancelled":
                continue
            room = self._find_room(b.get("room_id"))
            if room is None:
                continue
            if campus and room.get("campus") != campus:
                continue
            if building and room.get("building") != building:
                continue
            return True
        return False

    def _check_booked_room_nearest_to_workspace(self) -> bool:
        workspace = self._workspace_context()
        if not any(workspace.values()):
            return False

        booking_step = None
        booking_index = None
        for index, item in enumerate(self._history, start=1):
            if item.get("tool") != "meetingroom.booking.create":
                continue
            if item.get("result", {}).get("error"):
                continue
            booking_step = item
            booking_index = index

        if booking_step is None or booking_index is None:
            return False

        room_list_result = None
        for index, item in enumerate(self._history, start=1):
            if item.get("tool") != "meetingroom.room.list":
                continue
            if index >= booking_index:
                continue
            if item.get("result", {}).get("error"):
                continue
            room_list_result = item.get("result")

        if room_list_result is None:
            return False

        start = booking_step.get("args", {}).get("start")
        end = booking_step.get("args", {}).get("end")
        booked_office_id = booking_step.get("args", {}).get("office_id")
        if not start or not end or not booked_office_id:
            return False

        legal_rooms = []
        for room in room_list_result.get("rooms", []):
            if not room.get("bookable", True):
                continue
            busy_slots = room.get("busy_slots", [])
            if any(start < busy_end and end > busy_start for busy_start, busy_end in busy_slots):
                continue
            legal_rooms.append(room)

        if not legal_rooms:
            return False

        ranked = [
            (self._room_workspace_rank(room, workspace), room)
            for room in legal_rooms
        ]
        best_rank = max(rank for rank, _ in ranked)
        best_rooms = [room for rank, room in ranked if rank == best_rank]
        if len(best_rooms) != 1:
            return False

        best_room = best_rooms[0]
        return str(best_room.get("officeId", "")) == str(booked_office_id)


    def _check_workflow_draft_exists(self, cond: str, required_status: str | None = None) -> bool:
        # "存在请假流程草稿，workflow_id=72247"
        import re
        m = re.search(r"workflow_id=(\d+)", cond)
        if not m:
            return False
        wid = int(m.group(1))
        drafts = self._state.get("workflow_drafts", [])
        return any(
            d["workflow_id"] == wid
            and (required_status is None or d.get("status") == required_status)
            for d in drafts
        )

    def _check_workflow_fields_complete(self, cond: str) -> bool:
        # check all drafts have required fields
        drafts = self._state.get("workflow_drafts", [])
        for d in drafts:
            wid = str(d["workflow_id"])
            schema = self._state.get("workflow_schemas", {}).get(wid)
            if schema:
                required = schema.get("required_fields", [])
                data = d.get("data", {})
                if not all(f in data for f in required):
                    return False
        return len(drafts) > 0

    def _check_workflow_draft_field_value(self, cond: str) -> bool:
        # "请假草稿 leave_type=L" or "费用类物资申请草稿 material_category=WZLB-202005120001"
        # Supports multiple constraints: "请假草稿 leave_type=L, approver=120002"
        import re
        constraints = {
            key.strip(): value.strip()
            for key, value in re.findall(r"([A-Za-z_]+)=(.+?)(?=,\s*[A-Za-z_]+=|\s*$)", cond)
        }
        if not constraints:
            return False
        drafts = self._state.get("workflow_drafts", [])
        if not drafts:
            return False
        # check the most recently saved draft
        draft = drafts[-1]
        data = draft.get("data", {})
        for key, expected in constraints.items():
            actual = data.get(key)
            if actual is None:
                return False
            # numeric comparison for amount/quantity fields
            try:
                if float(str(actual)) == float(expected):
                    continue
            except (ValueError, TypeError):
                pass
            if str(actual) != expected:
                return False
        return True

    def _check_workflow_detail_row(self, cond: str) -> bool:
        # "费用类物资申请草稿 details.detail_2 包含 material_subclass=WZ_202210110009, budget_amount=50000.00"
        import re
        # 解析 table 名称
        m = re.search(r"details\.(\w+)\s+包含\s+(.+)$", cond)
        if not m:
            return False
        table_name = m.group(1)
        constraints_str = m.group(2)
        constraints = {
            key.strip(): value.strip()
            for key, value in re.findall(r"([A-Za-z_]+)=(.+?)(?=,\s*[A-Za-z_]+=|\s*$)", constraints_str)
        }
        if not constraints:
            return False
        drafts = self._state.get("workflow_drafts", [])
        if not drafts:
            return False
        draft = drafts[-1]
        rows = draft.get("data", {}).get("details", {}).get(table_name, [])
        for row in rows:
            matched = True
            for key, expected in constraints.items():
                actual = row.get(key)
                if actual is None:
                    matched = False
                    break
                try:
                    if float(str(actual)) == float(expected):
                        continue
                except (ValueError, TypeError):
                    pass
                if str(actual) != expected:
                    matched = False
                    break
            if matched:
                return True
        return False

    def _check_room_candidates_exist(self, cond: str) -> bool:
        import re

        office_match = re.search(r"office_id=([^,]+)", cond)
        capacity_match = re.search(r"capacity>=(\d+)", cond)
        feature_match = re.search(r"feature=([^,]+)", cond)
        start_match = re.search(r"start=([^,]+)", cond)
        end_match = re.search(r"end=([^,]+)", cond)

        office_id = office_match.group(1).strip() if office_match else None
        capacity = int(capacity_match.group(1)) if capacity_match else 0
        feature = feature_match.group(1).strip() if feature_match else None
        start = start_match.group(1).strip() if start_match else None
        end = end_match.group(1).strip() if end_match else None

        for item in self._history:
            if item["tool"] != "meetingroom.room.list":
                continue
            result = item["result"]
            if result.get("error"):
                continue
            for room in result.get("rooms", []):
                if office_id and result.get("office_id") != office_id:
                    continue
                if room.get("capacity", 0) < capacity:
                    continue
                if not room.get("bookable", True):
                    continue
                if feature and not self._room_has_feature(room, feature):
                    continue
                if start and end:
                    busy_slots = room.get("busy_slots", [])
                    if any(start < busy_end and end > busy_start for busy_start, busy_end in busy_slots):
                        continue
                if office_id:
                    building = room.get("building")
                    if building and building != office_id:
                        continue
                if feature == "screen" and not room.get("hasScreen", False) and "screen" not in room.get("features", []):
                    continue
                return True
        return False

    def _check_success_conditions(self) -> list[dict]:
        checks = self._case["success_check"]["must_satisfy"]
        return [{"condition": c, "passed": self._eval_condition(c)} for c in checks]

    # ------------------------------------------------------------------
    # AS: Action Legality Score (20 points)
    # ------------------------------------------------------------------

    def _action_legality_score(self, submission_checks: list[dict], violations: list[str] | None = None) -> float:
        if violations is None:
            violations = self._check_forbidden_conditions()
        if violations:
            return 0.0
        # check schema compliance
        errors = sum(1 for h in self._history if "error" in h["result"])
        submission_errors = sum(1 for item in submission_checks if not item["passed"])
        errors += submission_errors
        if errors == 0:
            return 20.0
        penalty = min(errors * 5, 20)
        return max(0, 20 - penalty)

    def _check_forbidden_conditions(self) -> list[str]:
        forbidden = self._case["success_check"]["forbidden"]
        violations = []
        for f in forbidden:
            if f.startswith("调用过 "):
                if self._check_tool_called(f):
                    violations.append(f)
            if "调用未授权工具" in f:
                if self._has_unauthorized_tool_calls():
                    violations.append(f)
            if "尝试操作他人预订" in f or "操作他人会议" in f:
                if self._has_permission_denied_calls():
                    violations.append(f)
            if "会议预订时间与房间占用冲突" in f:
                if self._has_booking_conflict():
                    violations.append(f)
            if "存在额外新增活跃会议预订" in f:
                if self._has_extra_created_active_bookings():
                    violations.append(f)
            if "workflow.save 缺少 schema 必填字段" in f:
                if self._has_workflow_missing_fields():
                    violations.append(f)
            if "未确认即执行高风险写操作" in f:
                if self._has_unconfirmed_write_action():
                    violations.append(f)
        return violations

    def _has_unauthorized_tool_calls(self) -> bool:
        return any(h["result"].get("unauthorized") for h in self._history)

    def _has_permission_denied_calls(self) -> bool:
        """检查是否有权限被拒绝的工具调用"""
        return any(h["result"].get("permission_denied") for h in self._history)

    def _has_booking_conflict(self) -> bool:
        # check if any booking result has conflict=True
        return any(
            h["result"].get("conflict") for h in self._history
            if h["tool"] in {"meetingroom.booking.create", "meetingroom.booking.extend"}
        )

    def _has_workflow_missing_fields(self) -> bool:
        # check if any workflow.save returned error about missing fields
        return any(
            "Missing required fields" in h["result"].get("error", "")
            for h in self._history if h["tool"] == "workflow.save"
        )

    def _has_unconfirmed_write_action(self) -> bool:
        return any(h["result"].get("unconfirmed_action") for h in self._history)

    def _has_extra_created_active_bookings(self) -> bool:
        return len(self._created_active_bookings()) > 1

    # ------------------------------------------------------------------
    # ES: Efficiency Score (10 points)
    # ------------------------------------------------------------------

    def _efficiency_score(self, success_checks: list[dict] | None = None) -> float:
        budget = self._case["scoring"]["step_budget"]
        used = len(self._history)
        if used > budget:
            return 0.0
        if success_checks is None:
            success_checks = self._check_success_conditions()
        if not all(item["passed"] for item in success_checks):
            return 0.0

        optimal_steps = self._case["scoring"].get("optimal_steps")
        if optimal_steps is None:
            optimal_steps = len(self._case.get("gold_trajectory", [])) or used

        overrun = max(used - optimal_steps, 0)
        score = 10.0 * (1 - (overrun / budget))
        return max(0.0, round(score, 2))

    def _check_submission(self, final_answer: dict) -> list[dict]:
        expected = self._case.get("reference_final_answer", {})
        if not expected:
            return []

        checks = []
        for key, expected_value in expected.items():
            actual_value = None
            if isinstance(final_answer, dict):
                actual_value = final_answer.get(key)
            checks.append(
                {
                    "field": key,
                    "passed": self._matches_expected(actual_value, expected_value),
                }
            )
        return checks

    def _matches_expected(self, actual: Any, expected: Any) -> bool:
        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                return False
            return all(
                key in actual and self._matches_expected(actual[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            if not isinstance(actual, list) or len(actual) != len(expected):
                return False
            return all(
                self._matches_expected(actual_item, expected_item)
                for actual_item, expected_item in zip(actual, expected)
            )
        return actual == expected

    # ------------------------------------------------------------------
    # RS: Robustness Score (10 points) — placeholder
    # ------------------------------------------------------------------
    # Requires running multiple variants of the same case with paraphrases/typos.
    # For now, always return 10 if task succeeds, else 0.
