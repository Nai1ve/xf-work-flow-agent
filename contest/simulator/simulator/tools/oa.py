"""
Simulated OA list tools derived from workflow state.
"""


class OATool:
    def __init__(self, state: dict):
        self._state = state

    def todo_list(self, args: dict) -> dict:
        return self._list_items(status="draft", keyword=args.get("keyword", ""))

    def done_list(self, args: dict) -> dict:
        return self._list_items(status="submitted", keyword=args.get("keyword", ""))

    def _list_items(self, status: str, keyword: str) -> dict:
        keyword_lower = keyword.lower()
        catalog_by_id = {
            item["workflow_id"]: item["name"]
            for item in self._state.get("workflow_catalog", [])
        }

        items = []
        for draft in self._state.get("workflow_drafts", []):
            if draft.get("status") != status:
                continue
            workflow_name = catalog_by_id.get(draft.get("workflow_id"), "")
            if keyword_lower and keyword_lower not in workflow_name.lower():
                continue
            items.append(
                {
                    "request_id": draft.get("request_id", ""),
                    "workflow_id": draft.get("workflow_id"),
                    "workflow_name": workflow_name,
                    "status": draft.get("status", ""),
                }
            )

        return {
            "keyword": keyword,
            "items": items,
        }
