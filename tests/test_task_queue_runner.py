"""Tests for TaskQueueRunner with BeadsQueue."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from telegram_bot.core.services.message_queue import QueueItem
from telegram_bot.core.services.task_queue import (
    BeadsQueue,
    BeadsTask,
    TaskQueueRunner,
)

CHANNEL = (1, 2)
CWD = "/tmp/project"


def _make_item(source: str = "user", task_id: str | None = None) -> QueueItem:
    return QueueItem(
        entries=[(1, "prompt")],
        source_messages=[MagicMock()],
        source=source,
        task_id=task_id,
    )


def _make_runner(has_in_progress: bool = False, next_task: BeadsTask | None = None):
    beads = BeadsQueue()
    beads.has_in_progress = AsyncMock(return_value=has_in_progress)
    beads.get_next = AsyncMock(return_value=next_task)
    beads.claim_task = AsyncMock()
    beads.close_task = AsyncMock()
    beads.set_status = AsyncMock()

    session_manager = MagicMock()
    session_manager.get_last_response.return_value = ""
    session_manager.kill_session = AsyncMock()
    session_manager._get_session.return_value = MagicMock(cwd=CWD)

    message_queue = MagicMock()
    message_queue.enqueue = MagicMock()

    bot = MagicMock()
    bot.send_message = AsyncMock()

    runner = TaskQueueRunner(
        beads_queue=beads,
        session_manager=session_manager,
        message_queue=message_queue,
        bot=bot,
    )
    runner.set_qmode(CHANNEL, True)
    return runner, beads, session_manager, message_queue


def _task(id_="bd-aaa1", title="Do X") -> BeadsTask:
    return BeadsTask(id=id_, title=title, priority=1, status="open")


# --- try_start_next ---


async def test_try_start_next_noop_when_qmode_off():
    runner, beads, _, mq = _make_runner()
    runner.set_qmode(CHANNEL, False)

    await runner.try_start_next(CHANNEL)

    beads.has_in_progress.assert_not_called()
    assert not mq.enqueue.called


async def test_try_start_next_noop_when_in_progress_exists():
    runner, beads, _, mq = _make_runner(has_in_progress=True)

    await runner.try_start_next(CHANNEL)

    beads.get_next.assert_not_called()
    assert not mq.enqueue.called


async def test_try_start_next_noop_when_queue_empty():
    runner, _beads, _, mq = _make_runner(has_in_progress=False, next_task=None)

    await runner.try_start_next(CHANNEL)

    assert not mq.enqueue.called


async def test_try_start_next_claims_and_enqueues_task():
    task = _task()
    runner, beads, _, mq = _make_runner(has_in_progress=False, next_task=task)

    await runner.try_start_next(CHANNEL)

    beads.claim_task.assert_called_once_with(CWD, task.id)
    assert mq.enqueue.called
    kwargs = mq.enqueue.call_args[1]
    assert kwargs["source"] == "task_queue"
    assert kwargs["task_id"] == task.id


async def test_try_start_next_prompt_contains_task_id_and_markers():
    task = _task(id_="bd-xyz9", title="Deploy service")
    runner, _beads, _, mq = _make_runner(has_in_progress=False, next_task=task)

    await runner.try_start_next(CHANNEL)

    prompt = mq.enqueue.call_args[0][1]
    assert "bd-xyz9" in prompt
    assert "bd close bd-xyz9" in prompt
    assert "[WAITING_FOR_INPUT]" in prompt
