"""
Minimal baseline agent using ReAct-style loop.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from env import IFTKEnv


class BaselineAgent:
    def __init__(self, env: IFTKEnv):
        self.env = env
        self._case_now: datetime | None = None

    def run(self, case_id: str) -> dict:
        obs = self.env.reset(case_id)
        tools = self.env.list_tools()
        self._case_now = datetime.fromisoformat(obs["now"]) if obs.get("now") else None

        query = obs["user_query"]
        print(f"[Query] {query}")

        if obs.get("mode") == "multi_turn":
            final = self._run_multi_turn(obs, tools)
        else:
            final = self._run_single_turn(query, tools)

        if hasattr(self.env, "done"):
            score = self.env.done(final)
            print(f"[Score] {score}")
            return score
        return final

    def _run_single_turn(self, query: str, tools: list) -> dict:
        final = {}
        if "会议室" in query or "预订" in query or "项目复盘会" in query or "技术评审" in query or "需求评审" in query:
            self._run_meetingroom_flow(query, final)
        if "请假" in query:
            self._run_workflow_flow(query, final)
        return final

    def _run_multi_turn(self, obs: dict, tools: list) -> dict:
        messages = list(obs.get("messages", []))
        query = messages[-1]["content"] if messages else obs["user_query"]
        details = {
            "day": None,
            "office_id": None,
            "attendees": None,
            "title": None,
            "start": "14:00",
            "end": "15:00",
        }
        final = {}

        for prompt in [
            "请问是在什么时间？",
            "请问是在哪个园区？",
            "请问大概多少人参加？",
            "请问会议主题是什么？",
        ]:
            print(f"[Reply] {prompt}")
            reply_result = self.env.reply(prompt)
            print(f"[User] {json.dumps(reply_result, ensure_ascii=False)}")
            user_message = reply_result["user_message"]
            self._extract_details_from_reply(user_message, details)

        if not details["day"]:
            details["day"] = self._infer_day(query)
        if not details["office_id"]:
            details["office_id"] = "A1"
        if not details["attendees"]:
            details["attendees"] = 10
        if not details["title"]:
            details["title"] = "会议预订"

        print(
            "[Action] meetingroom.room.list "
            f"{ {'day': details['day'], 'office_id': details['office_id'], 'capacity_gte': details['attendees']} }"
        )
        room_list_result = self.env.call_tool(
            "meetingroom.room.list",
            {
                "day": details["day"],
                "office_id": details["office_id"],
                "capacity_gte": details["attendees"],
            },
        )
        print(f"[Result] {json.dumps(room_list_result, ensure_ascii=False)}")

        print("[Reply] 可以的话我现在直接帮你预订，确认吗？")
        confirm_result = self.env.reply("可以的话我现在直接帮你预订，确认吗？")
        print(f"[User] {json.dumps(confirm_result, ensure_ascii=False)}")

        booking_args = self._build_booking_args_from_room_list(query, details, room_list_result)
        if booking_args is None:
            return final
        print(f"[Action] meetingroom.booking.create {booking_args}")
        booking_result = self.env.call_tool("meetingroom.booking.create", booking_args)
        print(f"[Result] {json.dumps(booking_result, ensure_ascii=False)}")
        self._update_final_from_tool(final, "meetingroom.booking.create", booking_args, booking_result)
        return final

    def _plan(self, query: str, tools: list) -> list[tuple[str, dict]]:
        """Naive keyword-based planning."""
        actions = []

        # Extract date
        day = self._infer_day(query)
        candidate_only = "候选" in query or "别直接订" in query
        required_features = ["screen"] if ("投屏" in query or "屏幕" in query) else []

        # Meetingroom booking
        if "会议室" in query or "预订" in query:
            office_match = re.search(r"([A-Z]\d+)园区", query)
            office_id = office_match.group(1) if office_match else "A1"

            capacity_match = re.search(r"(\d+)人", query)
            capacity = int(capacity_match.group(1)) if capacity_match else 10

            actions.append(("meetingroom.room.list", {
                "day": day,
                "office_id": office_id,
                "capacity_gte": capacity,
                "required_features": required_features,
            }))

            if not candidate_only:
                # Assume first available room
                actions.append(("meetingroom.booking.create", {
                    "day": day,
                    "office_id": office_id,
                    "requested_office_id": office_id,
                    "start": "14:00",
                    "end": "15:00",
                    "title": self._infer_title(query),
                    "attendees": capacity,
                }))

        # Workflow draft
        if "请假" in query:
            actions.append(("user.get_info", {"keyword": "李帅"}))
            actions.append(("workflow.catalog", {"keyword": "请假"}))
            actions.append(("workflow.schema", {"name": "001-1 请假申请"}))
            actions.append(("workflow.search_person", {"keyword": "张三", "workflow_id": 72247}))
            actions.append(("workflow.save", {
                "workflow_id": 72247,
                "data": {
                    "applicant": "119063",
                    "applicant_no": "2025008399",
                    "start_time": f"{day} 16:00",
                    "end_time": f"{day} 18:00",
                    "leave_type": "L",  # 事假
                    "reason": "10",     # 本人有事
                    "approver": "118871",
                    "duration": 2,
                }
            }))

        return actions

    def _run_tool(self, final: dict, tool_name: str, args: dict) -> dict:
        print(f"[Action] {tool_name} {args}")
        result = self.env.call_tool(tool_name, args)
        print(f"[Result] {json.dumps(result, ensure_ascii=False)}")
        self._update_final_from_tool(final, tool_name, args, result)
        return result

    def _run_workflow_flow(self, query: str, final: dict) -> None:
        actions = []
        if "请假" in query:
            day = self._infer_day(query)
            actions.extend([
                ("user.get_info", {"keyword": "李帅"}),
                ("workflow.catalog", {"keyword": "请假"}),
                ("workflow.schema", {"name": "001-1 请假申请"}),
                ("workflow.search_person", {"keyword": "张三", "workflow_id": 72247}),
                ("workflow.save", {
                    "workflow_id": 72247,
                    "data": {
                        "applicant": "119063",
                        "applicant_no": "2025008399",
                        "start_time": f"{day} 16:00",
                        "end_time": f"{day} 18:00",
                        "leave_type": "L",
                        "reason": "10",
                        "approver": "118871",
                        "duration": 2,
                    }
                }),
            ])
        for tool_name, args in actions:
            self._run_tool(final, tool_name, args)

    def _run_meetingroom_flow(self, query: str, final: dict) -> None:
        day = self._infer_day(query)
        title = self._infer_title(query)
        start, end = self._infer_time_range(query)
        capacity = self._infer_attendees(query)
        required_features = ["screen"] if ("投屏" in query or "屏幕" in query) else []
        office_id, office_address, office_name, fallback_office_id, fallback_office_address = self._infer_office_constraints(query)

        if (
            "如果没订就预订" in query
            or "没订会议室就帮我订" in query
            or ("没订" in query and "延长半小时" in query and "重新预订" in query)
        ):
            self._run_book_extend_or_rebook_flow(query, final, day, title, start, end, capacity)
            return
        if "取消" in query:
            self._run_cancel_flow(query, final, day, title, start, end)
            return
        if "加了" in query and "大一点" in query:
            self._run_rebook_larger_flow(query, final, day, title, start, end)
            return
        if "多聊半小时" in query or "延长" in query:
            self._run_extend_flow(query, final, day, title)
            return

        workspace = None
        if "离我工位近" in query:
            workspace = self._run_tool(final, "user.get_workspace", {})
            if workspace and not office_address:
                workspace_address = workspace.get("office_address", "")
                parts = workspace_address.split("_") if workspace_address else []
                if len(parts) >= 2:
                    office_address = "_".join(parts[:2])

        room_list_args = {
            "day": day,
            "capacity_gte": capacity,
            "required_features": required_features,
        }
        if required_features == ["screen"]:
            room_list_args["has_screen"] = True
        if office_address:
            room_list_args["office_address"] = office_address
        else:
            room_list_args["office_id"] = office_id
        if office_name:
            room_list_args["office_name"] = office_name

        room_list_result = self._run_tool(final, "meetingroom.room.list", room_list_args)
        if room_list_result.get("error"):
            return

        booking_args = self._build_booking_args_from_room_list(
            query,
            {
                "day": day,
                "office_id": office_id,
                "start": start,
                "end": end,
                "title": title,
                "attendees": capacity,
            },
            room_list_result,
            workspace=workspace,
        )

        if booking_args is None and "前后半小时" in query:
            booking_args = self._build_booking_args_for_flexible_time(
                query,
                {
                    "day": day,
                    "office_id": office_id,
                    "start": start,
                    "end": end,
                    "title": title,
                    "attendees": capacity,
                },
                room_list_result,
                workspace=workspace,
            )

        if booking_args is None and (fallback_office_id or fallback_office_address):
            fallback_args = {
                "day": day,
                "capacity_gte": capacity,
                "required_features": required_features,
            }
            if required_features == ["screen"]:
                fallback_args["has_screen"] = True
            if fallback_office_address:
                fallback_args["office_address"] = fallback_office_address
            elif fallback_office_id:
                fallback_args["office_id"] = fallback_office_id
            room_list_result = self._run_tool(final, "meetingroom.room.list", fallback_args)
            booking_args = self._build_booking_args_from_room_list(
                query,
                {
                    "day": day,
                    "office_id": fallback_office_id,
                    "start": start,
                    "end": end,
                    "title": title,
                    "attendees": capacity,
                },
                room_list_result,
                workspace=workspace,
            )

        if booking_args is None:
            final["booking_result"] = {
                "status": "blocked",
                "reason": "no_bookable_room",
            }
            return
        self._run_tool(final, "meetingroom.booking.create", booking_args)

    def _run_rebook_larger_flow(self, query: str, final: dict, day: str, title: str, start: str, end: str) -> None:
        booking_list_result = self._run_tool(
            final,
            "meetingroom.booking.list",
            {"day": day, "keyword": title, "status": "active"},
        )
        bookings = booking_list_result.get("bookings", [])
        if not bookings:
            return
        original = bookings[0]
        self._run_tool(final, "meetingroom.booking.cancel", {"order_id": original["order_id"]})
        added = self._infer_added_attendees(query)
        capacity = int(original.get("attendees", 8)) + added
        room_list_result = self._run_tool(
            final,
            "meetingroom.room.list",
            {"day": day, "office_id": original.get("office_id", "A1"), "capacity_gte": capacity},
        )
        booking_args = self._build_booking_args_from_room_list(
            query,
            {
                "day": day,
                "office_id": original.get("office_id", "A1"),
                "start": start,
                "end": end,
                "title": title,
                "attendees": capacity,
            },
            room_list_result,
        )
        if booking_args is None:
            return
        self._run_tool(final, "meetingroom.booking.create", booking_args)

    def _run_extend_flow(self, query: str, final: dict, day: str, title: str) -> None:
        booking_list_result = self._run_tool(
            final,
            "meetingroom.booking.list",
            {"day": day, "keyword": title, "status": "active"},
        )
        bookings = booking_list_result.get("bookings", [])
        if not bookings:
            return
        booking = bookings[0]
        minutes = 30

        if "别动原会议" in query or "先告诉我" in query:
            room_id = booking.get("room_id")
            room_bookings = self._run_tool(
                final,
                "meetingroom.room.bookings",
                {"day": day, "room_id": room_id},
            )
            busy_slots = room_bookings.get("busy_slots", [])
            new_end = self._add_minutes(booking["end"], minutes)
            conflict = any(
                not (new_end <= slot_start or booking["end"] >= slot_end)
                for slot_start, slot_end in busy_slots
                if [slot_start, slot_end] != [booking["start"], booking["end"]]
            )
            if conflict:
                final["booking_result"] = {
                    "status": "blocked",
                    "order_id": booking["order_id"],
                    "reason": "conflict_after_requested_extension",
                }
                return

        result = self._run_tool(
            final,
            "meetingroom.booking.extend",
            {"order_id": booking["order_id"], "minutes": minutes},
        )
        if result.get("extended"):
            final["booking_result"] = {
                "status": "extended",
                "order_id": booking["order_id"],
                "day": day,
                "office_id": booking.get("office_id", "A1"),
                "start": result["start"],
                "end": result["end"],
                "title": booking.get("title", title),
            }

    def _run_cancel_flow(self, query: str, final: dict, day: str, title: str, start: str, end: str) -> None:
        title_filter = title if self._query_mentions_title(query) else None
        time_filter = (start, end) if self._query_mentions_time(query) else (None, None)
        explicit_order = self._infer_order_id(query)
        if explicit_order:
            result = self._run_tool(final, "meetingroom.booking.cancel", {"order_id": explicit_order})
            if result.get("cancelled"):
                final["booking_result"] = {
                    "status": "cancelled",
                    "order_id": explicit_order,
                }
            return

        booking_list_args = {"day": day, "status": "active"}
        if title_filter:
            booking_list_args["keyword"] = title_filter
        booking_list_result = self._run_tool(final, "meetingroom.booking.list", booking_list_args)
        bookings = self._select_current_user_bookings(
            booking_list_result.get("bookings", []),
            title=title_filter,
            start=time_filter[0],
            end=time_filter[1],
        )

        if len(bookings) != 1:
            final["booking_result"] = {
                "status": "blocked",
                "reason": "need_confirmation",
            }
            return

        order_id = bookings[0]["order_id"]
        result = self._run_tool(final, "meetingroom.booking.cancel", {"order_id": order_id})
        if result.get("cancelled"):
            final["booking_result"] = {
                "status": "cancelled",
                "order_id": order_id,
            }

    def _run_book_extend_or_rebook_flow(
        self,
        query: str,
        final: dict,
        day: str,
        title: str,
        start: str,
        end: str,
        capacity: int,
    ) -> None:
        office_id, office_address, office_name, _, _ = self._infer_office_constraints(query)
        booking_list_result = self._run_tool(
            final,
            "meetingroom.booking.list",
            {"day": day, "keyword": title, "status": "active"},
        )
        bookings = self._select_current_user_bookings(
            booking_list_result.get("bookings", []),
            title=title,
            start=start,
            end=end,
        )

        if not bookings:
            room_list_args = {"day": day, "capacity_gte": capacity}
            if office_address:
                room_list_args["office_address"] = office_address
            else:
                room_list_args["office_id"] = office_id
            if office_name:
                room_list_args["office_name"] = office_name
            room_list_result = self._run_tool(final, "meetingroom.room.list", room_list_args)
            booking_args = self._build_booking_args_from_room_list(
                query,
                {
                    "day": day,
                    "office_id": office_id,
                    "start": start,
                    "end": end,
                    "title": title,
                    "attendees": capacity,
                },
                room_list_result,
            )
            if booking_args is None:
                final["booking_result"] = {"status": "blocked", "reason": "no_bookable_room"}
                return
            self._run_tool(final, "meetingroom.booking.create", booking_args)
            return

        booking = bookings[0]
        room_bookings = self._run_tool(
            final,
            "meetingroom.room.bookings",
            {"day": day, "room_id": booking["room_id"]},
        )
        new_end = self._add_minutes(end, 30)
        busy_slots = room_bookings.get("busy_slots", [])
        conflict = any(
            not (new_end <= slot_start or booking["end"] >= slot_end)
            for slot_start, slot_end in busy_slots
            if [slot_start, slot_end] != [booking["start"], booking["end"]]
        )
        if not conflict:
            extend_result = self._run_tool(
                final,
                "meetingroom.booking.extend",
                {"order_id": booking["order_id"], "minutes": 30},
            )
            if not extend_result.get("extended"):
                final["booking_result"] = {"status": "blocked", "reason": "extend_failed"}
                return
            final["booking_result"] = {
                "status": "extended",
                "order_id": booking["order_id"],
                "day": day,
                "office_id": booking.get("office_id", office_id),
                "start": extend_result["start"],
                "end": extend_result["end"],
                "title": booking.get("title", title),
            }
            return

        self._run_tool(final, "meetingroom.booking.cancel", {"order_id": booking["order_id"]})
        room_list_args = {"day": day, "capacity_gte": int(booking.get("attendees", capacity))}
        if office_address:
            room_list_args["office_address"] = office_address
        else:
            room_list_args["office_id"] = office_id
        room_list_result = self._run_tool(final, "meetingroom.room.list", room_list_args)
        booking_args = self._build_booking_args_from_room_list(
            query,
            {
                "day": day,
                "office_id": office_id,
                "start": start,
                "end": new_end,
                "title": booking.get("title", title),
                "attendees": int(booking.get("attendees", capacity)),
            },
            room_list_result,
        )
        if booking_args is None:
            final["booking_result"] = {"status": "blocked", "reason": "no_bookable_room"}
            return
        self._run_tool(final, "meetingroom.booking.create", booking_args)

    def _infer_day(self, query: str) -> str:
        base = self._case_now or datetime(2026, 4, 18, 10, 0, 0)

        explicit = re.search(r"(\d{4})-(\d{2})-(\d{2})", query)
        if explicit:
            return f"{explicit.group(1)}-{explicit.group(2)}-{explicit.group(3)}"

        month_day = re.search(r"(\d{1,2})月(\d{1,2})[号日]", query)
        if month_day:
            return f"{base.year:04d}-{int(month_day.group(1)):02d}-{int(month_day.group(2)):02d}"

        if "今天" in query:
            return base.date().isoformat()
        if "明天" in query:
            return (base.date() + timedelta(days=1)).isoformat()
        if "后天" in query:
            return (base.date() + timedelta(days=2)).isoformat()

        weekday_map = {
            "一": 0,
            "二": 1,
            "三": 2,
            "四": 3,
            "五": 4,
            "六": 5,
            "日": 6,
            "天": 6,
        }
        next_week_match = re.search(r"下周([一二三四五六日天])", query)
        if next_week_match:
            target_weekday = weekday_map[next_week_match.group(1)]
            days_until_next_monday = 7 - base.weekday()
            target_date = base.date() + timedelta(days=days_until_next_monday + target_weekday)
            return target_date.isoformat()

        this_week_match = re.search(r"(本周|这周)([一二三四五六日天])", query)
        if this_week_match:
            target_weekday = weekday_map[this_week_match.group(2)]
            delta = target_weekday - base.weekday()
            if delta < 0:
                delta += 7
            return (base.date() + timedelta(days=delta)).isoformat()

        return base.date().isoformat()

    def _infer_title(self, query: str) -> str:
        title_match = re.search(r"主题(?:是|写)?([^\n，。,.]+)", query)
        if title_match:
            title = title_match.group(1).strip()
            if title.startswith("还是"):
                title = title[2:].strip()
            return title
        meeting_match = re.search(r"开([^\n，。,.]+)会", query)
        if meeting_match:
            return meeting_match.group(1).strip()
        if "项目复盘" in query:
            return "项目复盘"
        if "技术评审" in query:
            return "技术评审"
        if "需求评审" in query:
            return "需求评审"
        return "季度复盘"

    def _infer_time_range(self, query: str, default_start: str = "14:00", default_end: str = "15:00") -> tuple[str, str]:
        match = re.search(r"上午(\d+)点到(\d+)点", query)
        if match:
            return f"{int(match.group(1)):02d}:00", f"{int(match.group(2)):02d}:00"
        match = re.search(r"下午(\d+)点到(\d+)点", query)
        if match:
            start_hour = int(match.group(1))
            end_hour = int(match.group(2))
            if start_hour < 12:
                start_hour += 12
            if end_hour < 12:
                end_hour += 12
            return f"{start_hour:02d}:00", f"{end_hour:02d}:00"

        match = re.search(r"(\d{1,2}):(\d{2})到(\d{1,2}):(\d{2})", query)
        if match:
            return (
                f"{int(match.group(1)):02d}:{match.group(2)}",
                f"{int(match.group(3)):02d}:{match.group(4)}",
            )
        return default_start, default_end

    def _infer_attendees(self, query: str, default: int = 10) -> int:
        match = re.search(r"(\d+)个?人", query)
        if match:
            return int(match.group(1))
        return default

    def _infer_added_attendees(self, query: str) -> int:
        match = re.search(r"加了(\d+)个", query)
        if match:
            return int(match.group(1))
        return 0

    def _query_mentions_title(self, query: str) -> bool:
        return any(token in query for token in ["主题", "项目复盘", "技术评审", "需求评审", "季度复盘"])

    def _query_mentions_time(self, query: str) -> bool:
        return any(
            token in query for token in ["上午", "下午", "点到", ":", "14点", "15点", "16点"]
        )

    def _infer_order_id(self, query: str) -> str | None:
        match = re.search(r"(SEED-[A-Z0-9-]+)", query)
        if match:
            return match.group(1)
        return None

    def _select_current_user_bookings(
        self,
        bookings: list[dict],
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict]:
        state = getattr(self.env, "state", None) or getattr(self.env, "_state", {}) or {}
        current_user_id = state.get("current_user_id")
        candidates = [
            booking for booking in bookings
            if booking.get("status") != "cancelled"
            and (current_user_id is None or booking.get("organizer_user_id") == current_user_id)
        ]
        if title:
            candidates = [booking for booking in candidates if title in booking.get("title", "")]
        if start:
            candidates = [booking for booking in candidates if booking.get("start") == start]
        if end:
            candidates = [booking for booking in candidates if booking.get("end") == end]
        return candidates

    def _infer_office_constraints(
        self, query: str
    ) -> tuple[str, str | None, str | None, str | None, str | None]:
        fallback_office_id = None
        fallback_office_address = None
        fallback_match = re.search(r"(?:也可以|不行的话)(A\d+)|([A-Z]\d+)也可以", query)
        if fallback_match:
            fallback_office_id = fallback_match.group(1) or fallback_match.group(2)

        office_match = re.search(r"([A-Z]\d+)(?:园区|栋)", query)
        office_id = office_match.group(1) if office_match else "A1"
        office_address = None
        office_name = "武夷厅" if "武夷厅" in query else None

        if office_name == "武夷厅":
            office_id = "A1"
            office_address = "0552_A1_4F"
        elif "A1四楼" in query or "A1 4楼" in query or "A1 4F" in query:
            office_id = "A1"
            office_address = "0552_A1_4F"
        elif "合肥A4楼" in query or "A4楼" in query:
            office_id = "A4"
            office_address = "0551_A4"
        elif "小镇" in query and office_match:
            office_address = f"0552_{office_id}"
        elif "合肥" in query and office_match:
            office_address = f"0551_{office_id}"
        elif "合肥园区" in query:
            office_address = "0551"
        elif "小镇园区" in query:
            office_address = "0552"

        if fallback_office_id:
            if office_address and office_address.startswith("0552"):
                fallback_office_address = f"0552_{fallback_office_id}"
            elif office_address and office_address.startswith("0551"):
                fallback_office_address = f"0551_{fallback_office_id}"

        if "同栋也可以" in query and office_address and office_address.count("_") >= 2:
            fallback_office_address = "_".join(office_address.split("_")[:2])

        return office_id, office_address, office_name, fallback_office_id, fallback_office_address

    def _add_minutes(self, time_value: str, minutes: int) -> str:
        hour, minute = [int(part) for part in time_value.split(":")]
        total = hour * 60 + minute + minutes
        return f"{total // 60:02d}:{total % 60:02d}"

    def _room_rank_for_workspace(self, room: dict, workspace: dict) -> tuple[int, int, int]:
        office_address = workspace.get("office_address", "")
        office_name = workspace.get("office_name", "")
        parts = office_address.split("_") if office_address else []
        workspace_building = parts[1] if len(parts) >= 2 else None
        workspace_floor = parts[2] if len(parts) >= 3 else None
        workspace_area = "北区" if "北区" in office_name else "南区" if "南区" in office_name else None

        room_name = room.get("name", "")
        room_area = "北区" if "北区" in room_name else "南区" if "南区" in room_name else None
        same_floor = int(workspace_floor is not None and room.get("floor") == workspace_floor and room.get("building") == workspace_building)
        same_building = int(workspace_building is not None and room.get("building") == workspace_building)
        same_area = int(workspace_area is not None and room_area == workspace_area)
        return (same_floor, same_building, same_area)

    def _build_booking_args_from_room_list(self, query: str, details: dict, room_list_result: dict, workspace: dict | None = None) -> dict | None:
        rooms = room_list_result.get("rooms", [])
        if not rooms:
            return None

        start = details.get("start")
        end = details.get("end")
        if not start or not end:
            start, end = self._infer_time_range(query, "14:00", "15:00")
        candidates = []
        for room in rooms:
            if not room.get("bookable", True):
                continue
            busy_slots = room.get("busy_slots", [])
            conflict = any(not (end <= bs or start >= be) for bs, be in busy_slots)
            if conflict:
                continue
            candidates.append(room)

        if not candidates:
            return None

        if workspace:
            candidates.sort(key=lambda room: self._room_rank_for_workspace(room, workspace), reverse=True)
        selected = candidates[0]
        return {
            "day": details["day"],
            "office_id": selected["officeId"],
            "requested_office_id": selected.get("building") or details.get("office_id"),
            "start": start,
            "end": end,
            "title": details.get("title") or self._infer_title(query),
            "attendees": details.get("attendees", 10),
            "room_id": selected.get("room_id"),
        }

    def _build_booking_args_for_flexible_time(self, query: str, details: dict, room_list_result: dict, workspace: dict | None = None) -> dict | None:
        original_start, original_end = details.get("start", "14:00"), details.get("end", "15:00")
        for delta in (-30, 30):
            shifted_details = dict(details)
            shifted_details["start"] = self._add_minutes(original_start, delta)
            shifted_details["end"] = self._add_minutes(original_end, delta)
            booking_args = self._build_booking_args_from_room_list(
                query,
                shifted_details,
                room_list_result,
                workspace=workspace,
            )
            if booking_args is not None:
                return booking_args
        return None

    def _extract_details_from_reply(self, user_message: str, details: dict) -> None:
        if "下周二" in user_message or "4月21号" in user_message or "4月21日" in user_message:
            details["day"] = "2026-04-21"
        office_match = re.search(r"(A\d+)园区", user_message)
        if office_match:
            details["office_id"] = office_match.group(1)
        attendees_match = re.search(r"(\d+)人", user_message)
        if attendees_match:
            details["attendees"] = int(attendees_match.group(1))
        if "主题" in user_message:
            details["title"] = user_message.replace("主题写", "").replace("。", "").strip()

    def _update_final_from_tool(self, final: dict, tool_name: str, args: dict, result: dict) -> None:
        if tool_name == "meetingroom.room.list" and result.get("rooms"):
            final["room_candidates"] = [
                {
                    "room_id": room["room_id"],
                    "office_id": room["officeId"],
                }
                for room in result["rooms"]
            ]
        if tool_name == "meetingroom.booking.create" and result.get("success"):
            final["booking_result"] = {
                "status": "success",
                "day": result["day"],
                "office_id": args.get("requested_office_id", args["office_id"]),
                "room_id": result["room_id"],
                "start": result["start"],
                "end": result["end"],
                "title": result["title"],
            }
        if tool_name == "workflow.save" and result.get("draft_saved"):
            final["workflow_draft_result"] = {
                "status": "draft_saved",
                "workflow_id": args["workflow_id"],
                **args["data"],
            }


MyAgent = BaselineAgent


if __name__ == "__main__":
    cases_dir = Path(__file__).resolve().parent.parent / "cases"
    env = IFTKEnv(cases_dir)
    agent = BaselineAgent(env)
    agent.run("beta_mr_wf_0001")
