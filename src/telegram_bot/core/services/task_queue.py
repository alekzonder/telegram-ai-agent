"""TaskQueue — per-channel queue of planned tasks for autonomous execution."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "priority": self.priority,
            "status": self.status,
            "created_at": self.created_at,
            "media_paths": self.media_paths,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
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
            # Reset stuck "running" tasks to "pending" on load (bot restart)
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
            def __init__(self, cid: int) -> None:
                self.id = cid
                self.type = "supergroup"

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
    """Drives automatic task execution via MessageQueue completion events."""

    def __init__(
        self,
        *,
        queue: TaskQueue,
        session_manager: object,
        message_queue: object,
        bot: object,
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
        """Called by MessageQueue after each item completes."""
        source = getattr(item, "source", "user")
        task_id = getattr(item, "task_id", None)

        if source == "user":
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
        """Dequeue next task and enqueue into MessageQueue, or set IDLE."""
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

        prompt = task.text
        if task.media_paths:
            refs = "\n".join(f"File: {p}" for p in task.media_paths)
            prompt = f"{prompt}\n\n{refs}"

        logger.info(
            "TaskQueueRunner: starting task_id=%s channel=%s prompt_len=%d",
            task.id,
            channel_key,
            len(prompt),
        )

        chat_id: int
        thread_id: int | None
        chat_id, thread_id = channel_key  # type: ignore[misc]
        source_msg = TaskQueueMessage(self._bot, chat_id, thread_id)
        self._message_queue.enqueue(  # type: ignore[attr-defined]
            channel_key,
            prompt,
            0,
            source_msg,
            source="task_queue",
            task_id=task.id,
            suppress_notification=True,
        )
