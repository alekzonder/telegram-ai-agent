# Design: /qstatus command

## Summary

Replace `/qskip`, `/qclear`, `/qnext` with a single `/qstatus <id> open|in_progress|closed` command that handles all task status transitions.

## Command Interface

```
/qstatus <id> open|in_progress|closed
```

Examples:
- `/qstatus personal-assistant-1a9 open` — reset stuck in_progress task
- `/qstatus personal-assistant-1a9 closed` — close task
- `/qstatus personal-assistant-1a9 in_progress` — manually claim task

## Implementation

**Status mapping:**
- `closed` → `bd close <id>`
- `open`, `in_progress` → `bd update <id> --status <status>`

**Auto-trigger on `open`:** If qmode is enabled for the channel and no task is currently `in_progress`, call `try_start_next` after the status change.

**Removed commands:** `/qskip`, `/qclear`, `/qnext` — deleted from handlers and bot command list.

**New BeadsQueue method:** `set_status(cwd, task_id, status)` — handles routing to `bd close` vs `bd update --status`.

## Error handling

- Unknown status → reply with usage hint
- Missing id or status → reply with usage
- `bd` failure → reply with error message
