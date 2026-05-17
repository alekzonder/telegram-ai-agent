# Task Queue Design

**Date**: 2026-05-17  
**Status**: Approved

## Overview

Add a per-channel task queue that lets users pre-load work items and have the
bot process them automatically one by one. The queue is opt-in via `/qmode on`.
When Claude finishes a task, the next one starts automatically — unless Claude
asked a question, in which case the queue pauses until the user responds.

Targets both `tmux` and `subprocess` exec modes. Primary focus is tmux.

---

## Data Model

### Task

```python
@dataclass
class Task:
    id: str                                          # uuid4
    text: str                                        # prompt text
    media: list[MediaBlob]                           # downloaded at enqueue time
    priority: int                                    # lower = higher priority (default 100)
    status: Literal["pending", "running", "done", "skipped"]
    created_at: float
```

Media is downloaded at `/qadd` time, not at execution time — Telegram download
URLs expire and the file may no longer be available when the task runs.

`MediaBlob` stores either a local file path or inline bytes (base64). The same
format the existing `photo.py` handler uses to build prompts.

### TaskQueueConfig (persisted per channel)

```python
@dataclass
class TaskQueueConfig:
    enabled: bool = False    # controlled by /qmode on|off
```

Stored as a new field in the existing `topic_config.json` alongside `exec_mode`
and `stream_mode`. Survives bot restarts.

### Queue file (persisted per channel)

```
{cwd}/.bot/task_queue_{chat_id}_{thread_id}.json
```

List of `Task` objects serialised as JSON. Survives bot restarts.

### Runtime state (in-memory per channel)

```
IDLE                   — no task running; queue may have pending items
RUNNING                — a queue task is currently in flight
PAUSED_AWAITING_HUMAN  — Claude emitted [WAITING_FOR_INPUT] or question heuristic fired
PAUSED_BY_USER         — user ran /qpause
```

`qmode enabled/disabled` and `queue runtime state` are orthogonal. Tasks can
always be added via `/qadd` regardless of qmode. When qmode is off, tasks
accumulate but are never started automatically.

---

## State Machine

```
IDLE → RUNNING                  : dequeue next task → enqueue into MessageQueue
RUNNING → RUNNING               : task done, next task available
RUNNING → IDLE                  : task done, queue empty
RUNNING → PAUSED_AWAITING_HUMAN : [WAITING_FOR_INPUT] marker or question heuristic
PAUSED_AWAITING_HUMAN → RUNNING : user sent a message, it was processed, auto-resume
PAUSED_BY_USER → RUNNING        : /qresume
Any → IDLE                      : /qclear
```

---

## Completion Hook

### Marker protocol

`TASK_MODE_PROMPT` gains two instructions:

```
When you have fully completed the assigned task, output on its own line:
[TASK_COMPLETE]

When you need information from the user before you can continue, output:
[WAITING_FOR_INPUT]
...your question here...
```

Markers are stripped before text is sent to Telegram.

### Hook attachment points

**tmux mode**: `tail_runner.py` already knows when an assistant turn ends.
Add `on_assistant_turn_complete(channel_key, response_text)` callback, called
once per complete assistant response.

**subprocess mode**: `MessageQueue._process_next` awaits `_process_callback`.
After the await, call the same `TaskQueueRunner.on_response_complete()`. The
response text is already captured by the streaming handler.

Single `TaskQueueRunner.on_response_complete()` handles both modes.

### on_response_complete logic

```
on_response_complete(channel_key, response_text, source):
  if source == "user":
    if state == PAUSED_AWAITING_HUMAN:
      state → IDLE
      try_start_next()       # user replied to Claude's question; resume queue
    return

  # source == "task_queue"
  if "[TASK_COMPLETE]" in response_text:
    mark_task_done()
    try_start_next()
  elif "[WAITING_FOR_INPUT]" in response_text:
    state → PAUSED_AWAITING_HUMAN
  else:
    # fallback heuristic (subprocess mode, non-task prompts)
    if response_text.rstrip().endswith("?"):
      state → PAUSED_AWAITING_HUMAN
    else:
      mark_task_done()
      try_start_next()
```

### try_start_next

```
try_start_next():
  if qmode == off: return
  if state == PAUSED_BY_USER: return
  task = queue.peek_next()
  if task is None:
    state → IDLE
    return
  state → RUNNING
  queue.mark_running(task.id)
  message_queue.enqueue(
    channel_key,
    prompt=build_prompt(task.text, task.media),
    source="task_queue",
    task_id=task.id,
  )
```

---

## MessageQueue changes

`QueueItem` gains two optional fields with defaults (backward-compatible):

```python
source: Literal["user", "task_queue"] = "user"
task_id: str | None = None
```

`on_response_complete` uses `source` to decide whether queue logic applies.

---

## Media handling

### A — photo with `/qadd` caption

`photo.py` handler intercepts: if caption starts with `/qadd`, download the
file immediately, create a `Task` with the caption text (minus `/qadd`) and
the downloaded `MediaBlob`. Do not run the normal photo processing flow.

### B — `/qadd` as reply to a media message

`commands.py` `/qadd` handler checks `message.reply_to_message`. If it
contains a photo or document, download it and attach as `Task.media`.

In both cases, prompt construction at execution time reuses the existing logic
from `photo.py` (same code path that builds prompts for normal media messages).

---

## Commands

```
/qmode on|off   — enable/disable auto-processing (persisted in topic config)
/qadd <text>    — add task to queue (also works as photo caption or reply)
/qlist          — show queue with positions and statuses
/qskip          — skip the next pending task
/qclear         — clear all pending tasks
/qpause         — pause auto-processing for this session (PAUSED_BY_USER)
/qresume        — resume (clears PAUSED_BY_USER)
/qnext          — manually trigger next task (also clears PAUSED_AWAITING_HUMAN)
```

---

## New files

```
src/telegram_bot/core/services/task_queue.py        — Task, TaskQueue, TaskQueueRunner
src/telegram_bot/core/handlers/task_queue_cmds.py   — /qmode, /qadd, /qlist, /qskip, /qclear, /qpause, /qresume, /qnext
tests/test_task_queue.py                            — unit tests: model, state transitions, priority sort
tests/test_task_queue_runner.py                     — on_response_complete, try_start_next
tests/test_qadd_handler.py                          — /qadd with caption and reply
```

## Changed files

```
src/telegram_bot/core/services/message_queue.py     — QueueItem.source, QueueItem.task_id
src/telegram_bot/core/services/tail_runner.py       — on_assistant_turn_complete callback
src/telegram_bot/core/services/topic_config.py      — TaskQueueConfig field
src/telegram_bot/prompts/task.md                    — [TASK_COMPLETE] / [WAITING_FOR_INPUT] markers
src/telegram_bot/__main__.py                        — wire TaskQueueRunner into DI
```

---

## Error handling

- If queue JSON is corrupt on load: log warning, start with empty queue (do not crash).
- If media download fails at `/qadd` time: reject the add, notify user immediately.
- If `try_start_next` enqueue fails: log error, state → IDLE (do not loop).
- If task runs and bot restarts mid-task: on recovery, task remains `running` in
  persisted state. On startup, tasks stuck in `running` are reset to `pending`.

---

## Testing strategy

- `test_task_queue.py`: add, priority sort, skip, clear, state transitions (pure unit, no I/O)
- `test_task_queue_runner.py`: `on_response_complete` with all marker/heuristic variants; `try_start_next` with qmode off/paused/empty
- `test_qadd_handler.py`: caption parse, reply-to-media parse, media download mock
- Existing `test_message_queue.py` unchanged — new `QueueItem` fields have defaults
