"""
Simulated workflow tools.
"""

import uuid


class WorkflowTool:
    def __init__(self, state: dict):
        self._state = state

    def _browser_option_key(self, workflow_id: int, field_id: int, dep: dict[str, str]) -> str:
        dep_parts = [f"{key}={dep[key]}" for key in sorted(dep)]
        dep_suffix = "|".join(dep_parts)
        if dep_suffix:
            return f"{workflow_id}:{field_id}:{dep_suffix}"
        return f"{workflow_id}:{field_id}"

    def catalog(self, args: dict) -> dict:
        keyword = args.get("keyword", "")
        results = [
            w for w in self._state.get("workflow_catalog", [])
            if keyword.lower() in w["name"].lower()
        ]
        return {"keyword": keyword, "workflows": results}

    def schema(self, args: dict) -> dict:
        name = args.get("name")
        workflow_id = args.get("workflow_id")

        entry = None
        if workflow_id is not None:
            entry = next(
                (w for w in self._state.get("workflow_catalog", []) if w["workflow_id"] == workflow_id),
                None,
            )
        elif name:
            entry = next(
                (w for w in self._state.get("workflow_catalog", []) if w["name"] == name),
                None,
            )
        else:
            return {"error": "name or workflow_id is required"}

        if entry is None:
            lookup = workflow_id if workflow_id is not None else name
            return {"error": f"Workflow not found: {lookup}"}

        wid = str(entry["workflow_id"])
        schema = self._state.get("workflow_schemas", {}).get(wid)
        if schema is None:
            return {"error": f"Schema not available for workflow_id={wid}"}

        return {"workflow_id": entry["workflow_id"], "name": entry["name"], "schema": schema}

    def search_person(self, args: dict) -> dict:
        keyword = args.get("keyword", "")
        title = args.get("title", "")
        if not keyword and not title:
            return {"error": "keyword or title is required"}

        people = self._state.get("workflow_people", [])

        if keyword:
            people = [
                person for person in people
                if keyword.lower() in person.get("name", "").lower()
                or keyword.lower() in person.get("account", "").lower()
                or keyword.lower() in person.get("employee_no", "").lower()
            ]

        if title:
            people = [
                person for person in people
                if title.lower() in person.get("title", "").lower()
            ]

        return {
            "keyword": keyword,
            "title": title,
            "workflow_id": args.get("workflow_id"),
            "field_id": args.get("field_id"),
            "people": people,
        }

    def browser_search(self, args: dict) -> dict:
        workflow_id = args.get("workflow_id")
        field_id = args.get("field_id")
        dep = args.get("dep", {})
        keyword = args.get("keyword", "")

        if workflow_id is None or field_id is None:
            return {"error": "workflow_id and field_id are required"}
        if not isinstance(dep, dict):
            return {"error": "dep must be an object"}

        key = self._browser_option_key(workflow_id, field_id, dep)
        options = self._state.get("workflow_browser_options", {}).get(key)
        if options is None:
            return {"error": f"Browser options not found: {key}"}

        # 如果提供了 keyword，进行过滤
        if keyword:
            filtered_options = [
                opt for opt in options
                if keyword.lower() in opt.get("label", "").lower()
                or keyword.lower() in opt.get("value", "").lower()
                or keyword.lower() in opt.get("code", "").lower()
            ]
            options = filtered_options

        return {
            "workflow_id": workflow_id,
            "field_id": field_id,
            "dep": dep,
            "keyword": keyword,
            "options": options,
        }

    def project_search(self, args: dict) -> dict:
        project_name = args.get("project_name", "")
        project_code = args.get("project_code", "")
        company_id = args.get("company_id")

        if not project_name and not project_code:
            return {"error": "project_name or project_code is required"}

        # 从 world_state 中查询项目信息
        project_data = self._state.get("project_search_results", {})

        results = []
        for key, projects in project_data.items():
            for project in projects:
                # 按项目名称或项目编码匹配
                name_match = project_name and project_name.lower() in project.get("project_name", "").lower()
                code_match = project_code and project_code.lower() in project.get("project_code", "").lower()

                if name_match or code_match:
                    # 如果指定了 company_id，进一步过滤
                    if company_id is not None:
                        if project.get("company_id") == company_id:
                            results.append(project)
                    else:
                        results.append(project)

        return {
            "project_name": project_name,
            "project_code": project_code,
            "company_id": company_id,
            "projects": results,
        }

    def save(self, args: dict) -> dict:
        workflow_id = args.get("workflow_id")
        workflow_name = args.get("name")
        request_id = args.get("request_id")
        data = args.get("data")
        submit = args.get("submit", False)

        if workflow_id is None and workflow_name:
            entry = next(
                (item for item in self._state.get("workflow_catalog", []) if item["name"] == workflow_name),
                None,
            )
            if entry is None:
                return {"error": f"Workflow not found: {workflow_name}"}
            workflow_id = entry["workflow_id"]

        if workflow_id is None and request_id is None:
            return {"error": "workflow_id, name or request_id is required"}
        if data is None:
            return {"error": "data is required"}

        if workflow_id is None and request_id is not None:
            existing = next(
                (item for item in self._state.get("workflow_drafts", []) if item["request_id"] == request_id),
                None,
            )
            if existing is None:
                return {"error": f"Draft not found: {request_id}"}
            workflow_id = existing["workflow_id"]

        wid = str(workflow_id)
        schema = self._state.get("workflow_schemas", {}).get(wid)

        # validate required fields if schema is known
        if schema:
            missing = [f for f in schema.get("required_fields", []) if f not in data]
            if missing:
                return {"error": f"Missing required fields: {missing}"}

        # validate user_id fields (applicant, approver) exist in workflow_people
        people_list = self._state.get("workflow_people", []) or self._state.get("users", [])
        valid_user_ids = {person.get("user_id") for person in people_list}

        for field_name in ["applicant", "approver"]:
            if field_name in data:
                user_id = data[field_name]
                if user_id and user_id not in valid_user_ids:
                    return {"error": f"Invalid {field_name}: user_id '{user_id}' not found in workflow_people"}

        # validate option fields (leave_type, reason) are valid values
        if schema:
            # 检查 leave_type
            if "leave_type" in data:
                leave_type_options = schema.get("leave_type_options", [])
                if leave_type_options:
                    valid_leave_types = {opt["value"] for opt in leave_type_options}
                    if data["leave_type"] not in valid_leave_types:
                        return {"error": f"Invalid leave_type: '{data['leave_type']}' not in allowed values {sorted(valid_leave_types)}"}

            # 检查 reason
            if "reason" in data:
                reason_options = schema.get("reason_options", [])
                if reason_options:
                    valid_reasons = {opt["value"] for opt in reason_options}
                    if data["reason"] not in valid_reasons:
                        return {"error": f"Invalid reason: '{data['reason']}' not in allowed values {sorted(valid_reasons)}"}

        effective_request_id = request_id or f"REQ-{workflow_id}-{uuid.uuid4().hex[:8]}"
        drafts = self._state.setdefault("workflow_drafts", [])
        existing = next((item for item in drafts if item["request_id"] == effective_request_id), None)
        payload = {
            "request_id": effective_request_id,
            "workflow_id": workflow_id,
            "data": data,
            "status": "submitted" if submit else "draft",
        }
        if existing is None:
            drafts.append(payload)
        else:
            existing.update(payload)

        return {
            "draft_saved": True,
            "submitted": submit,
            "request_id": effective_request_id,
            "workflow_id": workflow_id,
        }

    def delete(self, args: dict) -> dict:
        request_id = args.get("request_id")
        if not request_id:
            return {"error": "request_id is required"}

        drafts = self._state.get("workflow_drafts", [])
        for index, draft in enumerate(drafts):
            if draft["request_id"] == request_id:
                drafts.pop(index)
                return {"deleted": True, "request_id": request_id}
        return {"error": f"Draft not found: {request_id}"}
