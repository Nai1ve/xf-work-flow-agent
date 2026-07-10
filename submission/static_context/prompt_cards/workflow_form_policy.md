# Workflow Form Policy

- Build workflow drafts from the active workflow schema and verified evidence only.
- Applicant values come from user.get_info.
- Approver values come from workflow.search_person.
- Project name/code/wbs values come from workflow.project_search.
- Browser/select values come from workflow.browser_search or schema option sets.
- Detail rows must satisfy required fields and money consistency: quantity * unit_price = budget_amount, and total_amount = sum(detail budget_amount).
- Explicit submit intent is required for submitted workflows; otherwise treat requests as draft.
