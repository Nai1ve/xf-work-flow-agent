# Routing Context

Use this context only to map a user request to declared business capabilities and slot hints.

- Domains: meetingroom, workflow, user, oa, file.
- Meetingroom capabilities: book, query booking, query schedule, cancel, extend, rebook, cancel then rebook, participant add/remove/list.
- Workflow capabilities: leave draft/submit, expense material draft/submit.
- User-provided ids, codes, room names, project names, people, dates, amounts, and materials are only hints until verified by tools.
- Do not produce tool calls or final answers in routing output.
