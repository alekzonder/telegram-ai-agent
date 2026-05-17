"""TaskQueue — per-channel queue of planned tasks for autonomous execution."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from telegram_bot.core.messages import t

logger = logging.getLogger(__name__)


class TaskQueueState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED_AWAITING_HUMAN = "paused_awaiting_human"
    PAUSED_BY_USER = "paused_by_user"


@dataclass
class BeadsTask:
    id: str  # beads ID, e.g. "bd-a1b2"
    title: str  # issue title
    priority: int  # 0-4
    status: str = "open"  # open, in_progress


class BeadsQueue:
    """Async wrapper around the `bd` CLI. Stateless — all methods take an explicit cwd."""

    async def _run(self, cwd: str, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "bd",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "bd %s failed (rc=%d): %s",
                " ".join(args),
                proc.returncode,
                stderr.decode().strip(),
            )
        return stdout.decode().strip()

    async def get_next(self, cwd: str) -> BeadsTask | None:
        """Return highest-priority open task, or None if queue is empty."""
        raw = await self._run(cwd, "ready", "--json")
        if not raw:
            return None
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("BeadsQueue.get_next: invalid JSON from bd")
            return None
        if not items:
            return None
        item = items[0]
        return BeadsTask(
            id=item["id"],
            title=item["title"],
            priority=item.get("priority", 2),
            status=item.get("status", "open"),
        )

    async def has_in_progress(self, cwd: str) -> bool:
        """True if any task is currently in_progress."""
        raw = await self._run(cwd, "list", "--status", "in_progress", "--json")
        if not raw:
            return False
        try:
            return len(json.loads(raw)) > 0
        except json.JSONDecodeError:
            return False

    async def claim_task(self, cwd: str, task_id: str) -> None:
        await self._run(cwd, "update", task_id, "--status", "in_progress")

    async def close_task(self, cwd: str, task_id: str) -> None:
        await self._run(cwd, "close", task_id)

    async def reset_task(self, cwd: str, task_id: str) -> None:
        await self._run(cwd, "update", task_id, "--status", "open")

    async def add_task(self, cwd: str, text: str, priority: int = 2) -> str:
        """Create task via `bd q`, return the new task ID."""
        return await self._run(cwd, "q", text, "-p", str(priority))

    async def list_tasks(self, cwd: str) -> list[BeadsTask]:
        """Return all open and in_progress tasks."""
        raw = await self._run(cwd, "list", "--status", "open,in_progress", "--json")
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("BeadsQueue.list_tasks: invalid JSON from bd")
            return []
        return [
            BeadsTask(
                id=item["id"],
                title=item["title"],
                priority=item.get("priority", 2),
                status=item.get("status", "open"),
            )
            for item in items
        ]


class TaskQueue:
    """Compatibility stub — replaced by BeadsQueue in Task 2.

    __main__.py still instantiates this; Task 4 will remove the instantiation.
    """

    def __init__(self, path: Any) -> None:
        self._state: TaskQueueState = TaskQueueState.IDLE
        self._tasks: list[Any] = []

    @property
    def state(self) -> TaskQueueState:
        return self._state

    def set_state(self, state: TaskQueueState) -> None:
        self._state = state

    def add(self, text: str, **kwargs: Any) -> Any:
        raise NotImplementedError("TaskQueue is a stub; use BeadsQueue")

    def list_pending(self) -> list[Any]:
        return []

    def peek_next(self) -> Any:
        return None

    def mark_running(self, task_id: str) -> None:
        pass

    def mark_pending(self, task_id: str) -> None:
        pass

    def mark_done(self, task_id: str) -> None:
        pass

    def get_by_id(self, task_id: str) -> Any:
        return None

    def skip_next(self) -> Any:
        return None

    def clear(self) -> int:
        return 0


_TASK_COMPLETE_MARKER = "[TASK_COMPLETE]"
_WAITING_FOR_INPUT_MARKER = "[WAITING_FOR_INPUT]"

_TASK_PROMPT_TEMPLATE = (
    "Issue: {task_id}\n"
    "IMPORTANT: This issue has been marked as in_progress.\n"
    "When you have completely finished all work:\n"
    "  1. Run: bd close {task_id}\n"
    "  2. End your response with: [TASK_COMPLETE]\n"
    "Do not ask for approval or confirmation — work autonomously.\n"
    "If you absolutely need user input to proceed, end with: [WAITING_FOR_INPUT]\n"
    "\n"
    "---\n"
    "{task_text}"
)


def _is_question(text: str) -> bool:
    """Heuristic: last non-empty line ends with '?'."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return bool(lines) and lines[-1].endswith("?")


def _build_task_prompt(task_id: str, task_text: str) -> str:
    return _TASK_PROMPT_TEMPLATE.format(task_id=task_id, task_text=task_text)


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
        self.bot = bot  # send_streaming_response reads message.bot for live buffer setup

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
        beads_queue: BeadsQueue,
        session_manager: object,
        message_queue: object,
        bot: object,
        tmux_manager: object | None = None,
        qmode_enabled: bool = False,
    ) -> None:
        self._beads_queue = beads_queue
        self._session_manager = session_manager
        self._message_queue = message_queue
        self._bot = bot
        self._tmux_manager = tmux_manager
        self._qmode_enabled = qmode_enabled
        self._state: dict[object, TaskQueueState] = {}
        self._current_task_titles: dict[object, str] = {}

    def set_qmode(self, enabled: bool) -> None:
        self._qmode_enabled = enabled

    def get_state(self, channel_key: object) -> TaskQueueState:
        return self._state.get(channel_key, TaskQueueState.IDLE)

    def set_state(self, channel_key: object, state: TaskQueueState) -> None:
        self._state[channel_key] = state

    def _get_cwd(self, channel_key: object) -> str:
        return self._session_manager._get_session(channel_key).cwd  # type: ignore[attr-defined]

    async def _reset_session(self, channel_key: object) -> None:
        if (
            self._tmux_manager is not None
            and self._tmux_manager.is_active(channel_key)  # type: ignore[attr-defined]
        ):
            await self._tmux_manager.clear_context(  # type: ignore[attr-defined]
                channel_key, self._session_manager
            )
        else:
            await self._session_manager.kill_session(channel_key)  # type: ignore[attr-defined]

    async def _notify(self, channel_key: object, text: str) -> None:
        chat_id: int
        thread_id: int | None
        chat_id, thread_id = channel_key  # type: ignore[misc]
        try:
            await self._bot.send_message(  # type: ignore[attr-defined]
                chat_id,
                text,
                message_thread_id=thread_id,
            )
        except Exception:
            logger.warning("TaskQueueRunner: failed to send notification", exc_info=True)

    async def try_start_next(self, channel_key: object) -> None:
        """Dequeue next task and enqueue into MessageQueue, or set IDLE."""
        if not self._qmode_enabled:
            return
        if self.get_state(channel_key) == TaskQueueState.PAUSED_BY_USER:
            return

        cwd = self._get_cwd(channel_key)

        if await self._beads_queue.has_in_progress(cwd):
            logger.info(
                "TaskQueueRunner: in_progress task exists, skipping channel=%s", channel_key
            )
            return

        task = await self._beads_queue.get_next(cwd)
        if task is None:
            self.set_state(channel_key, TaskQueueState.IDLE)
            await self._notify(channel_key, t("ui.queue_empty"))
            return

        await self._beads_queue.claim_task(cwd, task.id)
        self.set_state(channel_key, TaskQueueState.RUNNING)
        self._current_task_titles[channel_key] = task.title

        await self._reset_session(channel_key)

        preview = task.title[:60] + ("…" if len(task.title) > 60 else "")
        await self._notify(channel_key, t("ui.queue_task_started", preview=preview))

        prompt = _build_task_prompt(task.id, task.title)

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

    async def on_item_done(self, channel_key: object, item: object) -> None:
        """Called by MessageQueue after each item completes."""
        source = getattr(item, "source", "user")
        task_id = getattr(item, "task_id", None)

        if source == "user":
            if self.get_state(channel_key) == TaskQueueState.PAUSED_AWAITING_HUMAN:
                self.set_state(channel_key, TaskQueueState.IDLE)
                await self.try_start_next(channel_key)
            return

        # source == "task_queue"
        if task_id is None:
            return

        cwd = self._get_cwd(channel_key)
        response_text: str = self._session_manager.get_last_response(channel_key)  # type: ignore[attr-defined]
        preview = self._current_task_titles.get(channel_key, task_id)[:60]

        if not response_text:
            # Cancelled/dropped — reset so it runs next time
            await self._beads_queue.reset_task(cwd, task_id)
            self.set_state(channel_key, TaskQueueState.IDLE)
            logger.info(
                "TaskQueueRunner: task cancelled, reset to open task_id=%s channel=%s",
                task_id,
                channel_key,
            )
            return

        if _TASK_COMPLETE_MARKER in response_text:
            # Claude already ran `bd close <id>` and wrote [TASK_COMPLETE]
            self.set_state(channel_key, TaskQueueState.IDLE)
            await self._notify(channel_key, t("ui.queue_task_done", preview=preview))
            await self.try_start_next(channel_key)
        elif _WAITING_FOR_INPUT_MARKER in response_text:
            await self._beads_queue.close_task(cwd, task_id)
            self.set_state(channel_key, TaskQueueState.PAUSED_AWAITING_HUMAN)
            logger.info("TaskQueueRunner: PAUSED_AWAITING_HUMAN channel=%s", channel_key)
        elif _is_question(response_text):
            await self._beads_queue.close_task(cwd, task_id)
            self.set_state(channel_key, TaskQueueState.PAUSED_AWAITING_HUMAN)
            logger.info(
                "TaskQueueRunner: question heuristic fired, PAUSED_AWAITING_HUMAN channel=%s",
                channel_key,
            )
        else:
            # No explicit marker — treat as done
            self.set_state(channel_key, TaskQueueState.IDLE)
            await self._notify(channel_key, t("ui.queue_task_done", preview=preview))
            await self.try_start_next(channel_key)
