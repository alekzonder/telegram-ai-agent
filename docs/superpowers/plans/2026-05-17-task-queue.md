# Task Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-channel task queue that processes planned work items automatically in tmux and subprocess modes, pausing when Claude asks a question.

**Architecture:** `TaskQueue` stores tasks per channel in JSON. `TaskQueueRunner` hooks into `MessageQueue._process_next` (fires after every item completes) and reads `session_manager.get_last_response()` to detect `[TASK_COMPLETE]`/`[WAITING_FOR_INPUT]` markers or fall back to a question heuristic. The `qmode` flag (persisted in `topic_config.json`) controls whether auto-processing is active.

**Tech Stack:** Python asyncio, aiogram 3.x, existing `MessageQueue`, `SessionManager`, `TopicConfig`, `TailRunner` patterns.

---

## File Map

**Create:**
- `src/telegram_bot/core/services/task_queue.py` — `Task`, `TaskQueue`, `TaskQueueState`, `TaskQueueRunner`
- `src/telegram_bot/core/handlers/task_queue_cmds.py` — `/qmode`, `/qadd`, `/qlist`, `/qskip`, `/qclear`, `/qpause`, `/qresume`, `/qnext`
- `tests/test_task_queue.py` — unit tests for `Task`, `TaskQueue`, state transitions, persistence
- `tests/test_task_queue_runner.py` — tests for `TaskQueueRunner.on_item_done`, `try_start_next`
- `tests/test_qadd_handler.py` — tests for `/qadd` text, caption, reply-to-media

**Modify:**
- `src/telegram_bot/core/services/message_queue.py` — add `source`/`task_id` to `QueueItem`, add `_on_item_complete` callback
- `src/telegram_bot/core/services/claude.py` (SessionManager) — add `store_last_response` / `get_last_response`
- `src/telegram_bot/core/handlers/streaming.py` — accumulate result text in `ctx` for both modes; call `store_last_response`
- `src/telegram_bot/core/services/topic_config.py` — add `qmode` to `TopicSettings`, add `update_qmode`
- `src/telegram_bot/prompts/task-manager.md` — add `[TASK_COMPLETE]` / `[WAITING_FOR_INPUT]` markers
- `src/telegram_bot/core/services/bot_commands.py` — add queue commands to menu
- `src/telegram_bot/__main__.py` — wire `TaskQueueRunner`, register router, inject into DI

---

## Task 1: Extend QueueItem and MessageQueue with completion hook

**Files:**
- Modify: `src/telegram_bot/core/services/message_queue.py`
- Test: `tests/test_task_queue.py` (initial test section)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_task_queue.py
from __future__ import annotations
import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from telegram_bot.core.services.message_queue import MessageQueue, QueueItem


def test_queue_item_defaults_source_user():
    item = QueueItem(entries=[(1, "hello")], source_messages=[MagicMock()])
    assert item.source == "user"
    assert item.task_id is None


async def test_message_queue_on_item_complete_fires():
    bot = MagicMock()
    session_manager = MagicMock()
    session_manager.cancel = AsyncMock(return_value=False)

    completed: list[tuple] = []

    async def on_complete(key, item):
        completed.append((key, item.source, item.task_id))

    async def process_cb(key, prompt, msgs, sid):
        pass

    queue = MessageQueue(bot, session_manager, process_cb, on_item_complete=on_complete)
    key = (123, 456)
    msg = MagicMock()
    msg.message_id = 1
    queue.enqueue(key, "hello", 1, msg, source="task_queue", task_id="t1")
    # Allow processing loop to run
    await asyncio.sleep(0.05)
    assert len(completed) == 1
    assert completed[0] == (key, "task_queue", "t1")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_task_queue.py::test_queue_item_defaults_source_user tests/test_task_queue.py::test_message_queue_on_item_complete_fires -v
```

Expected: `FAILED` — `QueueItem` has no `source` field, `MessageQueue.__init__` has no `on_item_complete`.

- [ ] **Step 3: Implement changes in message_queue.py**

Add to `QueueItem` dataclass (after existing fields):
```python
source: str = "user"   # "user" | "task_queue"
task_id: str | None = None
```

Add optional `on_item_complete` parameter to `MessageQueue.__init__`:
```python
def __init__(
    self,
    bot: Bot,
    session_manager: SessionManager,
    process_callback: ProcessCallback,
    on_item_complete: Callable[[ChannelKey, "QueueItem"], Awaitable[None]] | None = None,
) -> None:
    ...
    self._on_item_complete = on_item_complete
```

Add `on_item_complete` call in `_process_next`, after the `try/except` block that calls `_process_callback`:
```python
try:
    await self._process_callback(
        channel_key,
        combined_prompt,
        item.source_messages,
        item.target_session_id,
    )
    queue.error_count = 0
except Exception:
    queue.error_count += 1
    backoff_sec = min(2**queue.error_count, 30)
    logger.warning(...)
    await asyncio.sleep(backoff_sec)
    continue  # skip on_item_complete on error

if self._on_item_complete is not None:
    try:
        await self._on_item_complete(channel_key, item)
    except Exception:
        logger.warning(
            "on_item_complete failed for channel=%s source=%s task_id=%s",
            channel_key, item.source, item.task_id,
            exc_info=True,
        )
```

Also add `source` and `task_id` parameters to `enqueue()`:
```python
def enqueue(
    self,
    channel_key: ChannelKey,
    prompt: str,
    message_id: int,
    source_message: Message,
    target_session_id: str | None = None,
    suppress_notification: bool = False,
    source: str = "user",
    task_id: str | None = None,
) -> None:
```

Pass `source=source, task_id=task_id` when constructing `QueueItem` objects inside `enqueue`.

Add the import at the top of `message_queue.py`:
```python
from collections.abc import Awaitable, Callable
```
(already imported — just confirm `Awaitable` is there).

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_task_queue.py::test_queue_item_defaults_source_user tests/test_task_queue.py::test_message_queue_on_item_complete_fires -v
```

Expected: `PASSED`

- [ ] **Step 5: Run full test suite to check for regressions**

```bash
uv run pytest -v
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_bot/core/services/message_queue.py tests/test_task_queue.py
git commit -m "feat(queue): add source/task_id to QueueItem and on_item_complete hook"
```

---

## Task 2: Track last response text in SessionManager

**Files:**
- Modify: `src/telegram_bot/core/services/claude.py`
- Modify: `src/telegram_bot/core/handlers/streaming.py`
- Test: `tests/test_task_queue.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_task_queue.py`:

```python
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.config import Settings


def test_session_manager_store_and_get_last_response(tmp_path):
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test",
        project_root=str(tmp_path),
        default_cwd=str(tmp_path),
    )
    sm = SessionManager(settings)
    key = (1, 2)
    assert sm.get_last_response(key) == ""
    sm.store_last_response(key, "hello [TASK_COMPLETE]")
    assert sm.get_last_response(key) == "hello [TASK_COMPLETE]"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_task_queue.py::test_session_manager_store_and_get_last_response -v
```

Expected: `FAILED` — `SessionManager` has no `store_last_response`.

- [ ] **Step 3: Add methods to SessionManager in claude.py**

Find the `SessionManager` class. Add a `_last_response: dict[ChannelKey, str]` attribute in `__init__`:
```python
self._last_response: dict[ChannelKey, str] = {}
```

Add two methods:
```python
def store_last_response(self, channel_key: ChannelKey, text: str) -> None:
    self._last_response[channel_key] = text

def get_last_response(self, channel_key: ChannelKey) -> str:
    return self._last_response.get(channel_key, "")
```

- [ ] **Step 4: Accumulate response text in streaming.py and call store_last_response**

In `_handle_result_message_event`, add accumulation BEFORE the send call:
```python
async def _handle_result_message_event(ctx: _StreamCtx, event: StreamEvent) -> None:
    ctx.accumulated_text += event.content   # track for task queue (tmux mode)
    await _format_and_send_chunks(
        ctx,
        event.content,
        label=f"result_message {ctx.channel_key}",
        record_fn=lambda mid: _record_tmux_message(ctx, mid),
    )
```

At the end of `send_streaming_response`, after `_send_final_response` (last 3 lines of the function), add:
```python
    # Store final response text for task queue completion detection.
    # For subprocess mode, final_text is the full accumulated response.
    # For tmux mode, accumulated_text was built from result_message events above.
    response_text = ctx.accumulated_text or final_text
    if response_text:
        session_manager.store_last_response(channel_key, response_text)
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_task_queue.py -v && uv run pytest -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_bot/core/services/claude.py src/telegram_bot/core/handlers/streaming.py tests/test_task_queue.py
git commit -m "feat(queue): track last response text per channel for task queue marker detection"
```

---

## Task 3: Task datamodel, TaskQueue, and persistence

**Files:**
- Create: `src/telegram_bot/core/services/task_queue.py`
- Test: `tests/test_task_queue.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_task_queue.py`:

```python
from telegram_bot.core.services.task_queue import Task, TaskQueue, TaskQueueState
import json


def test_task_queue_add_and_list(tmp_path):
    q = TaskQueue(tmp_path / "q.json")
    t1 = q.add("do X", priority=10)
    t2 = q.add("do Y", priority=5)
    tasks = q.list_pending()
    assert tasks[0].id == t2.id   # lower priority number = first
    assert tasks[1].id == t1.id


def test_task_queue_skip(tmp_path):
    q = TaskQueue(tmp_path / "q.json")
    t1 = q.add("task A")
    q.skip(t1.id)
    assert q.list_pending() == []


def test_task_queue_clear(tmp_path):
    q = TaskQueue(tmp_path / "q.json")
    q.add("task A")
    q.add("task B")
    q.clear()
    assert q.list_pending() == []


def test_task_queue_peek_next(tmp_path):
    q = TaskQueue(tmp_path / "q.json")
    assert q.peek_next() is None
    t1 = q.add("task A", priority=10)
    t2 = q.add("task B", priority=1)
    assert q.peek_next().id == t2.id


def test_task_queue_mark_done(tmp_path):
    q = TaskQueue(tmp_path / "q.json")
    t = q.add("task A")
    q.mark_running(t.id)
    assert t.status == "running"
    q.mark_done(t.id)
    assert t.status == "done"
    assert q.list_pending() == []


def test_task_queue_state_default_idle(tmp_path):
    q = TaskQueue(tmp_path / "q.json")
    assert q.state == TaskQueueState.IDLE


def test_task_queue_persistence(tmp_path):
    path = tmp_path / "q.json"
    q1 = TaskQueue(path)
    q1.add("do X")
    q1.save()
    q2 = TaskQueue(path)
    assert len(q2.list_pending()) == 1
    assert q2.list_pending()[0].text == "do X"


def test_task_queue_corrupt_json_loads_empty(tmp_path):
    path = tmp_path / "q.json"
    path.write_text("NOT JSON", encoding="utf-8")
    q = TaskQueue(path)  # must not raise
    assert q.list_pending() == []


def test_task_queue_state_transitions(tmp_path):
    q = TaskQueue(tmp_path / "q.json")
    q.set_state(TaskQueueState.RUNNING)
    assert q.state == TaskQueueState.RUNNING
    q.set_state(TaskQueueState.PAUSED_AWAITING_HUMAN)
    assert q.state == TaskQueueState.PAUSED_AWAITING_HUMAN
    q.set_state(TaskQueueState.IDLE)
    assert q.state == TaskQueueState.IDLE
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_task_queue.py -k "task_queue_add or task_queue_skip or task_queue_clear or task_queue_peek or task_queue_mark or task_queue_state or task_queue_persistence or task_queue_corrupt" -v
```

Expected: `FAILED` — `task_queue` module doesn't exist.

- [ ] **Step 3: Implement task_queue.py**

Create `src/telegram_bot/core/services/task_queue.py`:

```python
"""TaskQueue — per-channel queue of planned tasks for autonomous execution."""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


class TaskQueueState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED_AWAITING_HUMAN = "paused_awaiting_human"
    PAUSED_BY_USER = "paused_by_user"


@dataclass
class Task:
    id: str
    text: str
    priority: int
    status: Literal["pending", "running", "done", "skipped"]
    created_at: float
    media_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "priority": self.priority,
            "status": self.status,
            "created_at": self.created_at,
            "media_paths": self.media_paths,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            id=d["id"],
            text=d["text"],
            priority=d.get("priority", 100),
            status=d.get("status", "pending"),
            created_at=d.get("created_at", 0.0),
            media_paths=d.get("media_paths", []),
        )


class TaskQueue:
    """Per-channel persistent task queue."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._tasks: list[Task] = []
        self._state: TaskQueueState = TaskQueueState.IDLE
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._tasks = [Task.from_dict(t) for t in data.get("tasks", [])]
            # Reset any stuck "running" tasks to "pending" on load (bot restart)
            for t in self._tasks:
                if t.status == "running":
                    t.status = "pending"
        except Exception:
            logger.warning("TaskQueue: corrupt queue file %s, starting empty", self._path)
            self._tasks = []

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"tasks": [t.to_dict() for t in self._tasks]}, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception:
            logger.warning("TaskQueue: failed to save %s", self._path, exc_info=True)

    @property
    def state(self) -> TaskQueueState:
        return self._state

    def set_state(self, state: TaskQueueState) -> None:
        self._state = state

    def add(self, text: str, *, priority: int = 100, media_paths: list[str] | None = None) -> Task:
        task = Task(
            id=str(uuid.uuid4()),
            text=text,
            priority=priority,
            status="pending",
            created_at=time.time(),
            media_paths=media_paths or [],
        )
        self._tasks.append(task)
        self._tasks.sort(key=lambda t: (t.priority, t.created_at))
        self.save()
        return task

    def list_pending(self) -> list[Task]:
        return [t for t in self._tasks if t.status == "pending"]

    def peek_next(self) -> Task | None:
        pending = self.list_pending()
        return pending[0] if pending else None

    def mark_running(self, task_id: str) -> None:
        for t in self._tasks:
            if t.id == task_id:
                t.status = "running"
                self.save()
                return

    def mark_done(self, task_id: str) -> None:
        for t in self._tasks:
            if t.id == task_id:
                t.status = "done"
                self.save()
                return

    def skip(self, task_id: str) -> None:
        for t in self._tasks:
            if t.id == task_id:
                t.status = "skipped"
                self.save()
                return

    def skip_next(self) -> Task | None:
        task = self.peek_next()
        if task:
            self.skip(task.id)
        return task

    def clear(self) -> int:
        count = len(self.list_pending())
        for t in self._tasks:
            if t.status == "pending":
                t.status = "skipped"
        self.save()
        return count
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_task_queue.py -k "task_queue_add or task_queue_skip or task_queue_clear or task_queue_peek or task_queue_mark or task_queue_state or task_queue_persistence or task_queue_corrupt" -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_bot/core/services/task_queue.py tests/test_task_queue.py
git commit -m "feat(queue): add Task, TaskQueue with persistence and state machine"
```

---

## Task 4: TaskQueueRunner — on_item_done and try_start_next

**Files:**
- Modify: `src/telegram_bot/core/services/task_queue.py` (add `TaskQueueRunner`)
- Test: `tests/test_task_queue_runner.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_task_queue_runner.py`:

```python
"""Tests for TaskQueueRunner completion hook and state transitions."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from telegram_bot.core.services.task_queue import TaskQueue, TaskQueueRunner, TaskQueueState
from telegram_bot.core.services.message_queue import QueueItem


def _make_runner(tmp_path: Path):
    queue = TaskQueue(tmp_path / "q.json")
    session_manager = MagicMock()
    session_manager.get_last_response.return_value = ""
    message_queue = MagicMock()
    message_queue.enqueue = MagicMock()
    bot = MagicMock()
    runner = TaskQueueRunner(
        queue=queue,
        session_manager=session_manager,
        message_queue=message_queue,
        bot=bot,
        qmode_enabled=True,
    )
    return runner, queue, session_manager, message_queue


async def test_on_item_done_task_complete_marker_starts_next(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    task = queue.add("do X")
    queue.mark_running(task.id)
    queue.set_state(TaskQueueState.RUNNING)
    next_task = queue.add("do Y")
    sm.get_last_response.return_value = "Done. [TASK_COMPLETE]"

    item = QueueItem(entries=[(1, "do X")], source_messages=[MagicMock()],
                     source="task_queue", task_id=task.id)
    await runner.on_item_done((1, 2), item)

    assert queue.state == TaskQueueState.RUNNING
    assert mq.enqueue.called


async def test_on_item_done_waiting_for_input_pauses(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    task = queue.add("do X")
    queue.mark_running(task.id)
    queue.set_state(TaskQueueState.RUNNING)
    sm.get_last_response.return_value = "[WAITING_FOR_INPUT]\nShould I use A or B?"

    item = QueueItem(entries=[(1, "do X")], source_messages=[MagicMock()],
                     source="task_queue", task_id=task.id)
    await runner.on_item_done((1, 2), item)

    assert queue.state == TaskQueueState.PAUSED_AWAITING_HUMAN
    assert not mq.enqueue.called


async def test_on_item_done_question_heuristic_pauses(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    task = queue.add("do X")
    queue.mark_running(task.id)
    queue.set_state(TaskQueueState.RUNNING)
    sm.get_last_response.return_value = "I need to know: approach A or B?"

    item = QueueItem(entries=[(1, "do X")], source_messages=[MagicMock()],
                     source="task_queue", task_id=task.id)
    await runner.on_item_done((1, 2), item)

    assert queue.state == TaskQueueState.PAUSED_AWAITING_HUMAN


async def test_on_item_done_no_marker_no_question_starts_next(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    task = queue.add("do X")
    queue.mark_running(task.id)
    queue.set_state(TaskQueueState.RUNNING)
    queue.add("do Y")
    sm.get_last_response.return_value = "All done. Files updated."

    item = QueueItem(entries=[(1, "do X")], source_messages=[MagicMock()],
                     source="task_queue", task_id=task.id)
    await runner.on_item_done((1, 2), item)

    assert mq.enqueue.called


async def test_on_item_done_user_source_clears_awaiting_human(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    queue.set_state(TaskQueueState.PAUSED_AWAITING_HUMAN)
    queue.add("next task")

    item = QueueItem(entries=[(1, "answer")], source_messages=[MagicMock()], source="user")
    await runner.on_item_done((1, 2), item)

    assert mq.enqueue.called


async def test_try_start_next_noop_when_qmode_off(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    runner._qmode_enabled = False
    queue.add("task A")

    await runner.try_start_next((1, 2))
    assert not mq.enqueue.called


async def test_try_start_next_noop_when_paused_by_user(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    queue.set_state(TaskQueueState.PAUSED_BY_USER)
    queue.add("task A")

    await runner.try_start_next((1, 2))
    assert not mq.enqueue.called


async def test_try_start_next_sets_idle_when_queue_empty(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    queue.set_state(TaskQueueState.RUNNING)

    await runner.try_start_next((1, 2))
    assert queue.state == TaskQueueState.IDLE
    assert not mq.enqueue.called
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_task_queue_runner.py -v
```

Expected: `FAILED` — `TaskQueueRunner` does not exist.

- [ ] **Step 3: Implement TaskQueueRunner in task_queue.py**

Add after the `TaskQueue` class:

```python
_TASK_COMPLETE_MARKER = "[TASK_COMPLETE]"
_WAITING_FOR_INPUT_MARKER = "[WAITING_FOR_INPUT]"


def _is_question(text: str) -> bool:
    """Heuristic: last non-empty line ends with '?'."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return bool(lines) and lines[-1].endswith("?")


class TaskQueueMessage:
    """Minimal Message-like object for task queue enqueue calls.

    Supports `await message.answer(text)` using the injected bot so
    send_streaming_response can post "Thinking..." into the correct channel.
    """

    message_id: int = 0
    reply_to_message = None
    from_user = None

    def __init__(self, bot: object, chat_id: int, thread_id: int | None) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id

        class _Chat:
            def __init__(self, chat_id: int) -> None:
                self.id = chat_id

        self.chat = _Chat(chat_id)
        self.message_thread_id = thread_id

    async def answer(self, text: str, **kwargs: object) -> object:
        return await self._bot.send_message(  # type: ignore[attr-defined]
            self._chat_id,
            text,
            message_thread_id=self._thread_id,
            **kwargs,
        )


class TaskQueueRunner:
    """Drives automatic task execution: hooks into MessageQueue completion events."""

    def __init__(
        self,
        *,
        queue: TaskQueue,
        session_manager: object,  # SessionManager (avoid circular import)
        message_queue: object,    # MessageQueue (avoid circular import)
        bot: object,              # aiogram Bot
        qmode_enabled: bool = False,
    ) -> None:
        self._queue = queue
        self._session_manager = session_manager
        self._message_queue = message_queue
        self._bot = bot
        self._qmode_enabled = qmode_enabled

    def set_qmode(self, enabled: bool) -> None:
        self._qmode_enabled = enabled

    async def on_item_done(self, channel_key: object, item: object) -> None:
        """Called by MessageQueue after each item completes.

        `item` is a QueueItem with `.source` and `.task_id` fields.
        """
        source = getattr(item, "source", "user")
        task_id = getattr(item, "task_id", None)

        if source == "user":
            # User replied while queue was PAUSED_AWAITING_HUMAN — resume.
            if self._queue.state == TaskQueueState.PAUSED_AWAITING_HUMAN:
                self._queue.set_state(TaskQueueState.IDLE)
                await self.try_start_next(channel_key)
            return

        # source == "task_queue"
        if task_id:
            response_text: str = self._session_manager.get_last_response(channel_key)  # type: ignore[attr-defined]
            if _TASK_COMPLETE_MARKER in response_text:
                self._queue.mark_done(task_id)
                await self.try_start_next(channel_key)
            elif _WAITING_FOR_INPUT_MARKER in response_text:
                self._queue.mark_done(task_id)
                self._queue.set_state(TaskQueueState.PAUSED_AWAITING_HUMAN)
                logger.info("TaskQueueRunner: PAUSED_AWAITING_HUMAN channel=%s", channel_key)
            elif _is_question(response_text):
                self._queue.mark_done(task_id)
                self._queue.set_state(TaskQueueState.PAUSED_AWAITING_HUMAN)
                logger.info(
                    "TaskQueueRunner: question heuristic fired, PAUSED_AWAITING_HUMAN channel=%s",
                    channel_key,
                )
            else:
                self._queue.mark_done(task_id)
                await self.try_start_next(channel_key)

    async def try_start_next(self, channel_key: object) -> None:
        """Dequeue next task and enqueue it into MessageQueue, or set IDLE."""
        if not self._qmode_enabled:
            return
        if self._queue.state == TaskQueueState.PAUSED_BY_USER:
            return

        task = self._queue.peek_next()
        if task is None:
            self._queue.set_state(TaskQueueState.IDLE)
            return

        self._queue.set_state(TaskQueueState.RUNNING)
        self._queue.mark_running(task.id)

        # Build prompt — media paths are injected as file references.
        prompt = task.text
        if task.media_paths:
            refs = "\n".join(f"File: {p}" for p in task.media_paths)
            prompt = f"{prompt}\n\n{refs}"

        logger.info(
            "TaskQueueRunner: starting task_id=%s channel=%s prompt_len=%d",
            task.id, channel_key, len(prompt),
        )
        # Use a synthetic Message placeholder. The enqueue call needs a
        # source_message for queue notification routing; we pass None-safe
        # sentinel. Handlers that display queue position skip None message_id.
        chat_id, thread_id = channel_key  # type: ignore[misc]
        source_msg = TaskQueueMessage(self._bot, chat_id, thread_id)
        self._message_queue.enqueue(  # type: ignore[attr-defined]
            channel_key,
            prompt,
            0,  # message_id placeholder
            source_msg,
            source="task_queue",
            task_id=task.id,
            suppress_notification=True,
        )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_task_queue_runner.py -v
```

Expected: all pass.

- [ ] **Step 5: Run full suite**

```bash
uv run pytest -v
```

- [ ] **Step 6: Commit**

```bash
git add src/telegram_bot/core/services/task_queue.py tests/test_task_queue_runner.py
git commit -m "feat(queue): add TaskQueueRunner with marker detection and state transitions"
```

---

## Task 5: qmode persistence in TopicConfig

**Files:**
- Modify: `src/telegram_bot/core/services/topic_config.py`
- Test: `tests/test_task_queue.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_task_queue.py`:

```python
import json
from telegram_bot.core.services.topic_config import TopicConfig


def test_topic_config_parses_qmode(tmp_path):
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}", encoding="utf-8")
    path = tmp_path / "topic_config.json"
    path.write_text(
        json.dumps({
            "topics": {
                "99": {
                    "name": "T", "type": "assistant", "mode": "free",
                    "cwd": str(tmp_path), "mcp_config": str(mcp),
                    "qmode": True,
                }
            }
        }),
        encoding="utf-8",
    )
    topic = TopicConfig(str(path), ".").get_topic(99)
    assert topic.qmode is True


def test_topic_config_qmode_defaults_false(tmp_path):
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}", encoding="utf-8")
    path = tmp_path / "topic_config.json"
    path.write_text(
        json.dumps({
            "topics": {
                "99": {
                    "name": "T", "type": "assistant", "mode": "free",
                    "cwd": str(tmp_path), "mcp_config": str(mcp),
                }
            }
        }),
        encoding="utf-8",
    )
    topic = TopicConfig(str(path), ".").get_topic(99)
    assert topic.qmode is False


async def test_topic_config_update_qmode(tmp_path):
    path = tmp_path / "topic_config.json"
    tc = TopicConfig(str(path), ".")
    ok = await tc.update_qmode(42, True)
    assert ok
    data = json.loads(path.read_text())
    assert data["topics"]["42"]["qmode"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_task_queue.py -k "qmode" -v
```

Expected: `FAILED`.

- [ ] **Step 3: Add qmode to TopicSettings and TopicConfig**

In `topic_config.py`, add `qmode: bool = False` to `TopicSettings` dataclass:
```python
@dataclass
class TopicSettings:
    name: str
    type: str
    mode: str
    cwd: str | None
    mcp_config: str | None
    stream_mode: StreamMode = _DEFAULT_STREAM_MODE
    exec_mode: ExecMode = _DEFAULT_EXEC_MODE
    engine: Engine = _DEFAULT_ENGINE
    model: str | None = None
    qmode: bool = False      # task queue auto-processing enabled
```

In `_parse_config`, inside the topics-parsing loop (after `model = ...`), add:
```python
raw_qmode = value.get("qmode", False)
qmode = bool(raw_qmode) if isinstance(raw_qmode, bool) else False
```

Pass `qmode=qmode` to the `TopicSettings(...)` constructor at the end of the loop.

In `_default_topic()`, add `qmode=False` (already defaulted, no change needed).

Add `update_qmode` method to `TopicConfig`:
```python
async def update_qmode(self, thread_id: int, enabled: bool) -> bool:
    """Persist qmode for one topic."""
    return await self._update_topic_field(
        thread_id=thread_id,
        field_name="qmode",
        value=enabled,
        log_label="update_qmode",
    )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_task_queue.py -k "qmode" -v && uv run pytest -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_bot/core/services/topic_config.py tests/test_task_queue.py
git commit -m "feat(queue): add qmode field to TopicSettings and update_qmode to TopicConfig"
```

---

## Task 6: Marker protocol in task-manager.md

**Files:**
- Modify: `src/telegram_bot/prompts/task-manager.md`
- Test: `tests/test_task_queue.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_task_queue.py`:

```python
from pathlib import Path


def test_task_manager_prompt_has_markers():
    prompt = Path("src/telegram_bot/prompts/task-manager.md").read_text(encoding="utf-8")
    assert "[TASK_COMPLETE]" in prompt
    assert "[WAITING_FOR_INPUT]" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_task_queue.py::test_task_manager_prompt_has_markers -v
```

Expected: `FAILED`.

- [ ] **Step 3: Update task-manager.md**

Append to `src/telegram_bot/prompts/task-manager.md`:

```markdown

---

## Task completion signals

When you have **fully completed** the assigned task with no remaining actions needed, output this marker on its own line at the end of your response:

[TASK_COMPLETE]

When you need information from the user **before you can continue**, output this marker on its own line before your question:

[WAITING_FOR_INPUT]

Only emit one of these markers per response. Do not emit them during intermediate steps — only at the true end of a task or when genuinely blocked on user input.
```

- [ ] **Step 4: Run test**

```bash
uv run pytest tests/test_task_queue.py::test_task_manager_prompt_has_markers -v
```

Expected: `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_bot/prompts/task-manager.md tests/test_task_queue.py
git commit -m "feat(queue): add [TASK_COMPLETE] and [WAITING_FOR_INPUT] markers to task-manager prompt"
```

---

## Task 7: /qadd handler — text, photo caption, reply to media

**Files:**
- Create: `src/telegram_bot/core/handlers/task_queue_cmds.py`
- Test: `tests/test_qadd_handler.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_qadd_handler.py`:

```python
"""Tests for /qadd handler: text, caption, reply-to-media."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from telegram_bot.core.handlers.task_queue_cmds import (
    _parse_qadd_text,
    _qadd_text_from_reply,
)


def test_parse_qadd_text_strips_command():
    assert _parse_qadd_text("/qadd do the thing") == "do the thing"
    assert _parse_qadd_text("/qadd   spaces  ") == "spaces"
    assert _parse_qadd_text("/qadd") == ""


def test_parse_qadd_text_from_reply_uses_caption_minus_command():
    # caption="/qadd refactor this" on a photo
    assert _parse_qadd_text("/qadd refactor this") == "refactor this"


def test_qadd_text_from_reply_none_when_no_text():
    msg = MagicMock()
    msg.text = None
    msg.caption = None
    assert _qadd_text_from_reply(msg) == ""


def test_qadd_text_from_reply_uses_text():
    msg = MagicMock()
    msg.text = "some context"
    msg.caption = None
    assert _qadd_text_from_reply(msg) == "some context"


def test_qadd_text_from_reply_uses_caption_fallback():
    msg = MagicMock()
    msg.text = None
    msg.caption = "caption text"
    assert _qadd_text_from_reply(msg) == "caption text"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_qadd_handler.py -v
```

Expected: `FAILED` — module doesn't exist.

- [ ] **Step 3: Create task_queue_cmds.py with /qadd and helpers**

Create `src/telegram_bot/core/handlers/task_queue_cmds.py`:

```python
"""Task queue command handlers: /qadd, /qmode, /qlist, /qskip, /qclear, /qpause, /qresume, /qnext."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from telegram_bot.core.services.task_queue import TaskQueue, TaskQueueRunner, TaskQueueState
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.types import ChannelKey, channel_key

logger = logging.getLogger(__name__)
router = Router(name="task_queue")

_MAX_MEDIA_SIZE = 20 * 1024 * 1024


def _parse_qadd_text(text: str) -> str:
    """Strip '/qadd' prefix and return the task text."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _qadd_text_from_reply(msg: Message) -> str:
    """Extract displayable text from a reply-to message."""
    if msg.text:
        return msg.text
    return msg.caption or ""


async def _download_media(bot: Bot, message: Message, file_cache_dir: str) -> list[str]:
    """Download photo or document from message. Returns list of local paths."""
    paths: list[str] = []
    tmp_dir = Path(file_cache_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())

    if message.photo:
        photo = message.photo[-1]
        dest = tmp_dir / f"{ts}_{photo.file_unique_id}.jpg"
        try:
            await bot.download(photo.file_id, destination=dest)
            paths.append(str(dest))
        except Exception:
            logger.warning("Failed to download photo for task queue", exc_info=True)

    elif message.document:
        doc = message.document
        dest = tmp_dir / f"{ts}_{doc.file_unique_id}_{doc.file_name or 'file'}"
        try:
            await bot.download(doc.file_id, destination=dest)
            paths.append(str(dest))
        except Exception:
            logger.warning("Failed to download document for task queue", exc_info=True)

    return paths


def _get_queue(key: ChannelKey, task_queue_runner: TaskQueueRunner) -> TaskQueue:
    return task_queue_runner._queue


@router.message(Command("qadd"))
async def handle_qadd(
    message: Message,
    bot: Bot,
    task_queue_runner: TaskQueueRunner,
    session_manager: object,
) -> None:
    """Add a task to the queue. Works with plain text, photo caption, or reply."""
    key = channel_key(message)
    queue = task_queue_runner._queue

    raw_text = message.text or message.caption or ""
    task_text = _parse_qadd_text(raw_text)
    media_paths: list[str] = []

    # Option A: photo/document with /qadd caption
    if message.photo or message.document:
        cache_dir = getattr(session_manager, "file_cache_dir", "/tmp/bot_cache")
        media_paths = await _download_media(bot, message, cache_dir)
        if not media_paths:
            await message.answer("Не удалось скачать файл. Задача не добавлена.")
            return

    # Option B: reply to media message
    elif message.reply_to_message is not None:
        reply = message.reply_to_message
        if reply.photo or reply.document:
            cache_dir = getattr(session_manager, "file_cache_dir", "/tmp/bot_cache")
            media_paths = await _download_media(bot, reply, cache_dir)
        if not task_text:
            task_text = _qadd_text_from_reply(reply)

    if not task_text and not media_paths:
        await message.answer("Укажи текст задачи: /qadd <текст задачи>")
        return

    task = queue.add(task_text, media_paths=media_paths)
    position = len(queue.list_pending())
    await message.answer(f"Задача #{position} добавлена в очередь: {task_text[:80]}")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_qadd_handler.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_bot/core/handlers/task_queue_cmds.py tests/test_qadd_handler.py
git commit -m "feat(queue): add /qadd handler with text, photo caption, and reply-to-media support"
```

---

## Task 8: Queue management commands

**Files:**
- Modify: `src/telegram_bot/core/handlers/task_queue_cmds.py`
- Test: `tests/test_qadd_handler.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_qadd_handler.py`:

```python
from telegram_bot.core.handlers.task_queue_cmds import router
from aiogram.filters import Command


def test_router_has_all_commands():
    command_names = set()
    for handler in router.message.handlers:
        for filter_ in handler.filters:
            if hasattr(filter_, "commands"):
                command_names.update(filter_.commands)
    expected = {"qadd", "qmode", "qlist", "qskip", "qclear", "qpause", "qresume", "qnext"}
    assert expected <= command_names, f"Missing commands: {expected - command_names}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_qadd_handler.py::test_router_has_all_commands -v
```

Expected: `FAILED`.

- [ ] **Step 3: Add remaining command handlers to task_queue_cmds.py**

Append to `src/telegram_bot/core/handlers/task_queue_cmds.py`:

```python
@router.message(Command("qmode"))
async def handle_qmode(
    message: Message,
    task_queue_runner: TaskQueueRunner,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() not in {"on", "off"}:
        current = "on" if task_queue_runner._qmode_enabled else "off"
        await message.answer(f"Режим очереди: {current}\nИспользование: /qmode on|off")
        return

    enabled = parts[1].strip() == "on"
    task_queue_runner.set_qmode(enabled)

    thread_id = key[1]
    if thread_id is not None:
        await topic_config.update_qmode(thread_id, enabled)

    status = "включён" if enabled else "выключен"
    await message.answer(f"Режим очереди {status}.")


@router.message(Command("qlist"))
async def handle_qlist(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    queue = task_queue_runner._queue
    pending = queue.list_pending()
    state_label = {
        TaskQueueState.IDLE: "готова",
        TaskQueueState.RUNNING: "выполняется",
        TaskQueueState.PAUSED_AWAITING_HUMAN: "ожидает ответа",
        TaskQueueState.PAUSED_BY_USER: "на паузе",
    }.get(queue.state, queue.state.value)

    if not pending:
        await message.answer(f"Очередь пуста. Состояние: {state_label}.")
        return

    lines = [f"Очередь ({len(pending)} задач, состояние: {state_label}):"]
    for i, t in enumerate(pending, 1):
        preview = t.text[:60] + ("…" if len(t.text) > 60 else "")
        lines.append(f"{i}. {preview}")
    await message.answer("\n".join(lines))


@router.message(Command("qskip"))
async def handle_qskip(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    queue = task_queue_runner._queue
    skipped = queue.skip_next()
    if skipped:
        await message.answer(f"Задача пропущена: {skipped.text[:60]}")
    else:
        await message.answer("Нет задач для пропуска.")


@router.message(Command("qclear"))
async def handle_qclear(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    queue = task_queue_runner._queue
    count = queue.clear()
    queue.set_state(TaskQueueState.IDLE)
    await message.answer(f"Очередь очищена ({count} задач удалено).")


@router.message(Command("qpause"))
async def handle_qpause(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    task_queue_runner._queue.set_state(TaskQueueState.PAUSED_BY_USER)
    await message.answer("Очередь на паузе. /qresume для возобновления.")


@router.message(Command("qresume"))
async def handle_qresume(
    message: Message, task_queue_runner: TaskQueueRunner
) -> None:
    import asyncio
    key = channel_key(message)
    task_queue_runner._queue.set_state(TaskQueueState.IDLE)
    await task_queue_runner.try_start_next(key)
    await message.answer("Очередь возобновлена.")


@router.message(Command("qnext"))
async def handle_qnext(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    import asyncio
    key = channel_key(message)
    queue = task_queue_runner._queue
    if queue.state == TaskQueueState.PAUSED_AWAITING_HUMAN:
        queue.set_state(TaskQueueState.IDLE)
    await task_queue_runner.try_start_next(key)
    await message.answer("Следующая задача запущена.")
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_qadd_handler.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_bot/core/handlers/task_queue_cmds.py tests/test_qadd_handler.py
git commit -m "feat(queue): add /qmode, /qlist, /qskip, /qclear, /qpause, /qresume, /qnext handlers"
```

---

## Task 9: Bot command menu update

**Files:**
- Modify: `src/telegram_bot/core/services/bot_commands.py`
- Test: `tests/test_task_queue.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_task_queue.py`:

```python
from telegram_bot.core.services.bot_commands import build_bot_commands


def test_queue_commands_in_menu():
    names = {cmd.command for cmd in build_bot_commands("en")}
    assert "qmode" in names
    assert "qadd" in names
    assert "qlist" in names
    assert "qskip" in names
    assert "qclear" in names
    assert "qpause" in names
    assert "qresume" in names
    assert "qnext" in names
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_task_queue.py::test_queue_commands_in_menu -v
```

Expected: `FAILED`.

- [ ] **Step 3: Add commands to bot_commands.py**

In `PUBLIC_BOT_COMMANDS` tuple, add after the last existing entry:

```python
LocalizedBotCommand("qmode", "Режим очереди задач вкл/выкл", "Enable/disable task queue mode"),
LocalizedBotCommand("qadd", "Добавить задачу в очередь", "Add task to queue"),
LocalizedBotCommand("qlist", "Показать очередь задач", "List task queue"),
LocalizedBotCommand("qskip", "Пропустить следующую задачу", "Skip next task"),
LocalizedBotCommand("qclear", "Очистить очередь задач", "Clear task queue"),
LocalizedBotCommand("qpause", "Приостановить очередь", "Pause task queue"),
LocalizedBotCommand("qresume", "Возобновить очередь", "Resume task queue"),
LocalizedBotCommand("qnext", "Запустить следующую задачу", "Run next task"),
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_task_queue.py::test_queue_commands_in_menu -v && uv run pytest -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/telegram_bot/core/services/bot_commands.py tests/test_task_queue.py
git commit -m "feat(queue): add task queue commands to Telegram bot menu"
```

---

## Task 10: Wire TaskQueueRunner in __main__.py

**Files:**
- Modify: `src/telegram_bot/__main__.py`
- Test: `tests/test_task_queue.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_task_queue.py`:

```python
from pathlib import Path


def test_main_imports_task_queue_router():
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")
    assert "task_queue_cmds" in source
    assert "task_queue_router" in source


def test_main_wires_task_queue_runner():
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")
    assert "TaskQueueRunner" in source
    assert "on_item_complete" in source
    assert 'dp["task_queue_runner"]' in source
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_task_queue.py::test_main_imports_task_queue_router tests/test_task_queue.py::test_main_wires_task_queue_runner -v
```

Expected: `FAILED`.

- [ ] **Step 3: Wire everything in __main__.py**

Add imports after existing imports:
```python
from telegram_bot.core.handlers.task_queue_cmds import router as task_queue_router
from telegram_bot.core.services.task_queue import TaskQueue, TaskQueueRunner
```

In `_start()`, after `message_queue = MessageQueue(bot, session_manager, _process_queue_item)`, add:

```python
# Task queue: one queue + runner per bot instance (shared across all channels).
# Queue file path uses project_root so data survives restarts.
_task_queue_path = Path(settings.project_root) / ".bot" / "task_queue.json"
_task_queue = TaskQueue(_task_queue_path)
task_queue_runner = TaskQueueRunner(
    queue=_task_queue,
    session_manager=session_manager,
    message_queue=message_queue,
    qmode_enabled=False,
)
```

Replace the existing `message_queue = MessageQueue(...)` line with:
```python
async def _on_item_complete(channel_key: ChannelKey, item: object) -> None:
    await task_queue_runner.on_item_done(channel_key, item)

message_queue = MessageQueue(
    bot, session_manager, _process_queue_item, on_item_complete=_on_item_complete
)
```

Note: `task_queue_runner` must be defined before `MessageQueue` but needs `message_queue` — break the cycle by assigning `message_queue` after constructing the runner:

```python
# Build runner first (message_queue assigned below)
_task_queue_path = Path(settings.project_root) / ".bot" / "task_queue.json"
_task_queue = TaskQueue(_task_queue_path)
task_queue_runner = TaskQueueRunner(
    queue=_task_queue,
    session_manager=session_manager,
    message_queue=None,   # assigned below
    bot=bot,
    qmode_enabled=False,
)

async def _on_item_complete(channel_key: ChannelKey, item: object) -> None:
    await task_queue_runner.on_item_done(channel_key, item)

message_queue = MessageQueue(
    bot, session_manager, _process_queue_item, on_item_complete=_on_item_complete
)
task_queue_runner._message_queue = message_queue
```

Add `task_queue_router` to the router list (after `commands_router`):
```python
dp.include_router(task_queue_router)
```

Add to DI injections:
```python
dp["task_queue_runner"] = task_queue_runner
```

Also, on startup, load qmode from topic_config for the default topic (if it exists):
```python
# This is done lazily per-channel on first /qmode command — no startup action needed.
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_task_queue.py::test_main_imports_task_queue_router tests/test_task_queue.py::test_main_wires_task_queue_runner -v
```

Expected: pass.

- [ ] **Step 5: Run full suite and type checks**

```bash
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
```

Fix any ruff/mypy issues before committing.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_bot/__main__.py tests/test_task_queue.py
git commit -m "feat(queue): wire TaskQueueRunner into bot DI and register queue router"
```

---

## Task 11: Final verification

- [ ] **Step 1: Run all checks**

```bash
uv run pytest -v
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ mcp-servers/bot/server.py
```

Expected: all pass, no type errors.

- [ ] **Step 2: Smoke-test the bot manually (tmux mode)**

Start the bot, set exec_mode to tmux in a topic. Send:
```
/qmode on
/qadd Напиши файл hello.py с print("hello")
/qadd Добавь тест для hello.py
```
Verify: first task runs, then second task starts automatically after `[TASK_COMPLETE]`.

- [ ] **Step 3: Final commit if any fixes were made**

```bash
git add -p
git commit -m "fix(queue): address review findings from smoke test"
```
