"""TaskQueue — per-channel queue of planned tasks for autonomous execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from telegram_bot.core.messages import t

logger = logging.getLogger(__name__)


@dataclass
class BeadsTask:
    id: str  # beads ID, e.g. "personal-assistant-1a9"
    title: str
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


_TASK_PROMPT_TEMPLATE = (
    "Issue: {task_id}\n"
    "IMPORTANT: This issue has been marked as in_progress.\n"
    "When you have completely finished all work:\n"
    "  1. Run: bd close {task_id}\n"
    "Do not ask for approval or confirmation — work autonomously.\n"
    "If you absolutely need user input to proceed, end with: [WAITING_FOR_INPUT]\n"
    "\n"
    "---\n"
    "{task_text}"
)


def _build_task_prompt(task_id: str, task_text: str) -> str:
    return _TASK_PROMPT_TEMPLATE.format(task_id=task_id, task_text=task_text)


class TaskQueueMessage:
    """Minimal Message-like object for task queue enqueue calls."""

    message_id: int = 0
    reply_to_message = None
    from_user = None

    def __init__(self, bot: object, chat_id: int, thread_id: int | None) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._thread_id = thread_id
        self.bot = bot

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
    """Drives automatic task execution by polling beads every minute.

    Logic:
    - Every 60s: for each qmode-enabled channel, check beads.
    - If any task is in_progress → skip (Claude is working).
    - If no in_progress → claim next open task and dispatch it.
    """

    def __init__(
        self,
        *,
        beads_queue: BeadsQueue,
        session_manager: object,
        message_queue: object,
        bot: object,
        tmux_manager: object | None = None,
    ) -> None:
        self._beads_queue = beads_queue
        self._session_manager = session_manager
        self._message_queue = message_queue
        self._bot = bot
        self._tmux_manager = tmux_manager
        self._qmode_channels: set[Any] = set()
        self._periodic_task: asyncio.Task[None] | None = None

    def set_qmode(self, channel_key: object, enabled: bool) -> None:
        if enabled:
            self._qmode_channels.add(channel_key)
        else:
            self._qmode_channels.discard(channel_key)

    def is_qmode(self, channel_key: object) -> bool:
        return channel_key in self._qmode_channels

    def start(self, interval: int = 60) -> None:
        self._periodic_task = asyncio.create_task(
            self._periodic_check_loop(interval), name="task_queue_periodic"
        )

    async def stop(self) -> None:
        if self._periodic_task is not None:
            self._periodic_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._periodic_task
            self._periodic_task = None

    async def _periodic_check_loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            for channel_key in list(self._qmode_channels):
                try:
                    await self.try_start_next(channel_key, silent=True)
                except Exception:
                    logger.warning(
                        "TaskQueueRunner: periodic check failed channel=%s",
                        channel_key,
                        exc_info=True,
                    )

    def get_cwd(self, channel_key: object) -> str:
        return str(self._session_manager._get_session(channel_key).cwd)  # type: ignore[attr-defined]

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

    async def try_start_next(self, channel_key: object, *, silent: bool = False) -> None:
        """Check beads and start next task if nothing is in_progress."""
        if channel_key not in self._qmode_channels:
            return

        cwd = self.get_cwd(channel_key)

        if await self._beads_queue.has_in_progress(cwd):
            logger.info(
                "TaskQueueRunner: in_progress task exists, skipping channel=%s", channel_key
            )
            return

        task = await self._beads_queue.get_next(cwd)
        if task is None:
            if not silent:
                await self._notify(channel_key, t("ui.queue_empty"))
            return

        await self._beads_queue.claim_task(cwd, task.id)
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
