"""
Simulated meetingroom tools.
All data comes from world_state injected at env.reset().
"""

def _time_overlap(s1, e1, s2, e2) -> bool:
    """Return True if [s1,e1) overlaps [s2,e2)."""
    return s1 < e2 and e1 > s2


def _add_minutes(time_value: str, minutes: int) -> str:
    hour, minute = [int(part) for part in time_value.split(":")]
    total = hour * 60 + minute + minutes
    return f"{total // 60:02d}:{total % 60:02d}"


class MeetingroomTool:
    def __init__(self, state: dict):
        self._state = state

    def _room_has_feature(self, room: dict, feature: str) -> bool:
        if feature == "screen":
            return room.get("hasScreen", False) or "screen" in room.get("features", [])
        return feature in room.get("features", [])

    def _match_office_address(self, room: dict, office_address: str) -> bool:
        """
        匹配 office_address 编码到会议室属性

        office_address 格式：
        - 0552: 讯飞小镇（所有）
        - 0552_A1: 小镇A1楼栋
        - 0552_A1_3F: 小镇A1楼栋3楼
        - 0551: 合肥总部（所有）
        - 0551_A4: 合肥A4楼栋
        - 0551_TYDK: 天源迪科
        - 0551_0023: 中国声谷
        - 0551_0056: 高新区产业园
        - 0551_0058: B3
        - 0551_0041: 上源汇展科技园
        - 0551_0071: 中安创谷2期K5栋
        """
        # 地点编码到 location 的映射
        location_mapping = {
            "0552": "讯飞小镇",
            "0551": "合肥总部",
            "0551_TYDK": "天源迪科",
            "0551_0023": "中国声谷",
            "0551_0056": "高新区产业园",
            "0551_0058": "B3",
            "0551_0041": "上源汇展科技园",
            "0551_0071": "中安创谷2期K5栋",
        }

        # 解析 office_address
        parts = office_address.split("_")

        # 第一部分：地区编码（0552 或 0551）
        region_code = parts[0]

        # 检查是否匹配完整的地点编码（如 0551_TYDK）
        if office_address in location_mapping:
            return room.get("location") == location_mapping[office_address]

        # 检查地区级别匹配（0552 或 0551）
        if len(parts) == 1:
            if region_code == "0552":
                return room.get("campus") == "小镇"
            elif region_code == "0551":
                return room.get("campus") == "合肥"
            return False

        # 检查楼栋级别匹配（如 0552_A1, 0551_A4）
        if len(parts) >= 2:
            building = parts[1]

            # 先检查地区是否匹配
            if region_code == "0552" and room.get("campus") != "小镇":
                return False
            if region_code == "0551" and room.get("campus") != "合肥":
                return False

            # 检查楼栋是否匹配
            if room.get("building") != building:
                return False

            # 如果有楼层信息（如 0552_A1_3F）
            if len(parts) >= 3:
                floor = parts[2]
                if room.get("floor") != floor:
                    return False

            return True

        return False

    def _find_booking(self, order_id: str) -> dict | None:
        return next(
            (
                item for item in self._state.get("bookings", [])
                if item.get("order_id", item.get("booking_id")) == order_id
            ),
            None,
        )

    def _find_user(self, user_id: str) -> dict | None:
        return next(
            (
                item for item in self._state.get("users", [])
                if item.get("user_id") == user_id
            ),
            None,
        )

    def room_list(self, args: dict) -> dict:
        day = args.get("day")
        office_id = args.get("office_id")  # 向后兼容，映射到 building
        building = office_id  # office_id 作为 building 的别名
        office_address = args.get("office_address")
        office_name = args.get("office_name", "")
        capacity_gte = args.get("capacity_gte", 0)
        capacity_lte = args.get("capacity_lte")
        has_screen = args.get("has_screen")
        required_features = set(args.get("required_features", []))
        bookable = args.get("bookable")  # None=不过滤, True=只看可预订, False=只看无权限

        if not day:
            return {"error": "day is required"}

        rooms = []
        for r in self._state.get("meetingroom_inventory", []):
            if office_address:
                if not self._match_office_address(r, office_address):
                    continue
            if building and r.get("building") != building:
                continue
            if office_name and office_name not in r.get("name", ""):
                continue
            if r["capacity"] < capacity_gte:
                continue
            if capacity_lte is not None and r["capacity"] > capacity_lte:
                continue
            if has_screen and not r.get("hasScreen", False):
                continue
            if required_features and not all(
                self._room_has_feature(r, feature) for feature in required_features
            ):
                continue
            if bookable is not None and r.get("bookable", True) != bookable:
                continue
            busy_slots = r.get("busy_slots_by_day", {}).get(day, [])
            features = list(r.get("features", []))
            if r.get("hasScreen", False) and "screen" not in features:
                features.append("screen")
            rooms.append({
                "room_id":   r["room_id"],
                "officeId":  r.get("officeId"),
                "name":      r["name"],
                "capacity":  r["capacity"],
                "campus":    r.get("campus"),
                "building":  r.get("building"),
                "floor":     r.get("floor"),
                "hasScreen": r.get("hasScreen", False),
                "features":  features,
                "bookable":  r.get("bookable", True),
                "busy_slots": busy_slots,
            })

        return {
            "day": day,
            "office_id": office_id,  # 向后兼容
            "office_address": office_address,
            "office_name": office_name,
            "rooms": rooms,
        }

    def booking_list(self, args: dict) -> dict:
        day = args.get("day")
        keyword = args.get("keyword", "")
        status = args.get("status")

        bookings = []
        for booking in self._state.get("bookings", []):
            if day and booking.get("day") != day:
                continue
            if status and booking.get("status") != status:
                continue
            if keyword and keyword not in booking.get("title", ""):
                continue
            bookings.append(dict(booking))

        return {"bookings": bookings}

    def room_schedule(self, args: dict) -> dict:
        """查询指定会议室一段时间内的预订记录"""
        room_id = args.get("room_id")
        start_date = args.get("start_date")
        end_date = args.get("end_date")
        day = args.get("day")

        # 兼容旧接口：meetingroom.room.bookings 只传 day + room_id
        if day and not start_date and not end_date:
            start_date = day
            end_date = day

        if not room_id or not start_date or not end_date:
            return {"error": "room_id, start_date and end_date are required"}

        # 验证房间是否存在
        room = next(
            (item for item in self._state.get("meetingroom_inventory", []) if item["room_id"] == room_id),
            None,
        )
        if room is None:
            return {"error": f"Room not found: {room_id}"}

        # 生成日期范围
        from datetime import datetime, timedelta
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            return {"error": "Invalid date format, use YYYY-MM-DD"}

        if start > end:
            return {"error": "start_date must be before or equal to end_date"}

        # 收集该时间段内的所有预订
        bookings = []
        current = start
        while current <= end:
            day_str = current.strftime("%Y-%m-%d")
            # 查找该房间该天的所有预订
            for booking in self._state.get("bookings", []):
                if (booking.get("room_id") == room_id
                    and booking.get("day") == day_str
                    and booking.get("status") != "cancelled"):
                    bookings.append({
                        "day": booking["day"],
                        "order_id": booking.get("order_id", booking.get("booking_id")),
                        "booking_id": booking.get("booking_id", booking.get("order_id")),
                        "start": booking["start"],
                        "end": booking["end"],
                        "title": booking.get("title", ""),
                        "organizer_name": booking.get("organizer_name", ""),
                        "organizer_user_id": booking.get("organizer_user_id", ""),
                        "status": booking.get("status", "active"),
                        "attendees": booking.get("attendees"),
                    })
            current += timedelta(days=1)

        result = {
            "room_id": room_id,
            "room_name": room.get("name", ""),
            "start_date": start_date,
            "end_date": end_date,
            "bookings": bookings,
        }
        if start_date == end_date:
            result["day"] = start_date
            result["busy_slots"] = room.get("busy_slots_by_day", {}).get(start_date, [])
        return result

    def booking_create(self, args: dict) -> dict:
        required = ["day", "start", "end", "title"]
        for f in required:
            if f not in args:
                return {"error": f"Missing required field: {f}"}

        office_id = args.get("office_id")  # officeId UUID，优先使用
        room_id = args.get("room_id")
        if not office_id and not room_id:
            return {"error": "Missing required field: office_id or room_id"}

        start = args["start"]
        end   = args["end"]

        # find room: office_id 为 UUID 格式时按 officeId 匹配，否则按 room_id 匹配
        inventory = self._state.get("meetingroom_inventory", [])
        is_uuid = office_id and len(office_id) == 32 and office_id.replace("-", "").isalnum()
        if is_uuid:
            room = next((r for r in inventory if r.get("officeId") == office_id), None)
        elif room_id:
            room = next((r for r in inventory if r["room_id"] == room_id), None)
        else:
            return {"error": "Missing required field: office_id (UUID) or room_id"}

        if room is None:
            identifier = office_id or room_id
            return {"error": f"Room not found: {identifier}"}

        room_id = room["room_id"]

        if not room.get("bookable", True):
            return {
                "error": f"No permission to book room: {room_id}",
                "unauthorized": True,
                "bookable": False,
            }

        # conflict check
        day = args["day"]
        day_slots = room.setdefault("busy_slots_by_day", {}).setdefault(day, [])
        for bs, be in day_slots:
            if _time_overlap(start, end, bs, be):
                return {
                    "error": f"Time conflict with existing booking {bs}-{be}",
                    "conflict": True,
                }

        # commit to state
        day_slots.append([start, end])
        booking_id = f"BK-{room_id}-{start.replace(':','')}"
        self._state.setdefault("bookings", []).append({
            "booking_id": booking_id,
            "order_id": booking_id,
            "status": "active",
            **args,
            "room_id": room_id,  # 确保始终用内部 room_id，覆盖 args 里可能没有的情况
        })

        return {
            "success": True,
            "booking_id": booking_id,
            "room_id": room_id,
            "day": args["day"],
            "start": start,
            "end": end,
            "title": args["title"],
        }

    def booking_cancel(self, args: dict) -> dict:
        order_id = args.get("order_id")
        if not order_id:
            return {"error": "order_id is required"}

        booking = self._find_booking(order_id)
        if booking is None:
            return {"error": f"Booking not found: {order_id}"}

        # 权限检查：只能取消自己的预订
        current_user_id = self._state.get("current_user_id")
        organizer_user_id = booking.get("organizer_user_id")
        if organizer_user_id and current_user_id != organizer_user_id:
            return {
                "error": f"Permission denied: cannot cancel booking organized by another user",
                "permission_denied": True
            }

        booking["status"] = "cancelled"
        room = next(
            (item for item in self._state.get("meetingroom_inventory", []) if item["room_id"] == booking["room_id"]),
            None,
        )
        if room is not None:
            day_slots = room.get("busy_slots_by_day", {}).get(booking["day"], [])
            try:
                day_slots.remove([booking["start"], booking["end"]])
            except ValueError:
                pass

        return {"cancelled": True, "order_id": order_id}

    def booking_extend(self, args: dict) -> dict:
        order_id = args.get("order_id")
        minutes = args.get("minutes")
        if not order_id or minutes is None:
            return {"error": "order_id and minutes are required"}

        booking = self._find_booking(order_id)
        if booking is None:
            return {"error": f"Booking not found: {order_id}"}
        if booking.get("status") == "cancelled":
            return {"error": f"Booking already cancelled: {order_id}"}

        # 权限检查：只能延长自己的预订
        current_user_id = self._state.get("current_user_id")
        organizer_user_id = booking.get("organizer_user_id")
        if organizer_user_id and current_user_id != organizer_user_id:
            return {
                "error": f"Permission denied: cannot extend booking organized by another user",
                "permission_denied": True
            }

        room = next(
            (item for item in self._state.get("meetingroom_inventory", []) if item["room_id"] == booking["room_id"]),
            None,
        )
        if room is None:
            return {"error": f"Room not found: {booking['room_id']}"}

        new_end = _add_minutes(booking["end"], int(minutes))
        day_slots = room.get("busy_slots_by_day", {}).get(booking["day"], [])
        for bs, be in day_slots:
            if [bs, be] == [booking["start"], booking["end"]]:
                continue
            if _time_overlap(booking["start"], new_end, bs, be):
                return {
                    "error": f"Time conflict with existing booking {bs}-{be}",
                    "conflict": True,
                }

        for index, slot in enumerate(day_slots):
            if slot == [booking["start"], booking["end"]]:
                day_slots[index] = [booking["start"], new_end]
                break
        booking["end"] = new_end
        return {
            "extended": True,
            "order_id": order_id,
            "start": booking["start"],
            "end": booking["end"],
        }

    def booking_participant_list(self, args: dict) -> dict:
        order_id = args.get("order_id")
        if not order_id:
            return {"error": "order_id is required"}

        booking = self._find_booking(order_id)
        if booking is None:
            return {"error": f"Booking not found: {order_id}"}

        return {
            "order_id": order_id,
            "participants": list(booking.get("participants", [])),
        }

    def booking_participant_add(self, args: dict) -> dict:
        order_id = args.get("order_id")
        user_id = args.get("user_id")
        if not order_id or not user_id:
            return {"error": "order_id and user_id are required"}

        booking = self._find_booking(order_id)
        if booking is None:
            return {"error": f"Booking not found: {order_id}"}

        # 权限检查：只能管理自己的预订
        current_user_id = self._state.get("current_user_id")
        organizer_user_id = booking.get("organizer_user_id")
        if organizer_user_id and current_user_id != organizer_user_id:
            return {
                "error": f"Permission denied: cannot add participant to booking organized by another user",
                "permission_denied": True
            }

        user = self._find_user(user_id)
        if user is None:
            return {"error": f"User not found: {user_id}"}

        participants = booking.setdefault("participants", [])
        if any(item.get("user_id") == user_id for item in participants):
            return {"error": f"Participant already exists: {user_id}"}

        participants.append(
            {
                "user_id": user["user_id"],
                "name": user.get("name", ""),
                "employee_no": user.get("employee_no", ""),
                "title": user.get("title", ""),
            }
        )
        return {
            "added": True,
            "order_id": order_id,
            "participants": list(participants),
        }

    def booking_participant_remove(self, args: dict) -> dict:
        order_id = args.get("order_id")
        user_id = args.get("user_id")
        if not order_id or not user_id:
            return {"error": "order_id and user_id are required"}

        booking = self._find_booking(order_id)
        if booking is None:
            return {"error": f"Booking not found: {order_id}"}

        # 权限检查：只能管理自己的预订
        current_user_id = self._state.get("current_user_id")
        organizer_user_id = booking.get("organizer_user_id")
        if organizer_user_id and current_user_id != organizer_user_id:
            return {
                "error": f"Permission denied: cannot remove participant from booking organized by another user",
                "permission_denied": True
            }

        participants = booking.setdefault("participants", [])
        filtered = [item for item in participants if item.get("user_id") != user_id]
        if len(filtered) == len(participants):
            return {"error": f"Participant not found: {user_id}"}

        booking["participants"] = filtered
        return {
            "removed": True,
            "order_id": order_id,
            "participants": list(filtered),
        }
