"""
Simulated user directory tools.
"""


class UserTool:
    def __init__(self, state: dict):
        self._state = state

    def _find_current_user(self) -> dict | None:
        current_user_id = self._state.get("current_user_id")
        if current_user_id is None:
            users = self._state.get("users", [])
            return users[0] if users else None
        return next(
            (user for user in self._state.get("users", []) if user.get("user_id") == current_user_id),
            None,
        )

    def get_info(self, args: dict) -> dict:
        keyword = args.get("keyword", "")

        # 无参数时返回当前登录用户
        if not keyword:
            current = self._state.get("current_user")
            if current is None:
                return {"error": "current_user not set in world_state"}
            return {
                "users": [{
                    "user_id": current.get("user_id", ""),
                    "name": current.get("name", ""),
                    "employee_no": current.get("employee_no", ""),
                    "title": current.get("title", ""),
                }]
            }

        lowered = keyword.lower()
        # 从 users 或 workflow_people 中查找
        user_list = self._state.get("users", []) or self._state.get("workflow_people", [])
        users = [
            {
                "user_id": user.get("user_id", ""),
                "name": user.get("name", ""),
                "employee_no": user.get("employee_no", ""),
                "title": user.get("title", ""),
            }
            for user in user_list
            if lowered in user.get("name", "").lower()
        ]
        return {
            "keyword": keyword,
            "users": users,
        }

    def get_workspace(self, args: dict) -> dict:
        user = self._find_current_user()
        if user is None:
            return {"error": "Current user not found"}

        workspace = user.get("workspace", {})
        return {
            "user_id": user.get("user_id", ""),
            "office_address": workspace.get("office_address", ""),
            "office_name": workspace.get("office_name", ""),
        }
