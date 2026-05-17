"""Tests for TaskQueueRunner with BeadsQueue."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from telegram_bot.core.services.message_queue import QueueItem
from telegram_bot.core.services.task_queue import (
    BeadsQueue,
    BeadsTask,
    TaskQueueRunner,
    TaskQueueState,
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
    beads.reset_task = AsyncMock()

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
        qmode_enabled=True,
    )
    return runner, beads, session_manager, message_queue


def _task(id_="bd-aaa1", title="Do X") -> BeadsTask:
    return BeadsTask(id=id_, title=title, priority=1, status="open")


# --- try_start_next ---


async def test_try_start_next_noop_when_qmode_off():
    runner, beads, _, mq = _make_runner()
    runner.set_qmode(False)

    await runner.try_start_next(CHANNEL)

    beads.has_in_progress.assert_not_called()
    assert not mq.enqueue.called


async def test_try_start_next_noop_when_paused_by_user():
    runner, beads, _, mq = _make_runner()
    runner.set_state(CHANNEL, TaskQueueState.PAUSED_BY_USER)

    await runner.try_start_next(CHANNEL)

    beads.has_in_progress.assert_not_called()
    assert not mq.enqueue.called


async def test_try_start_next_noop_when_in_progress_exists():
    runner, beads, _, mq = _make_runner(has_in_progress=True)

    await runner.try_start_next(CHANNEL)

    beads.get_next.assert_not_called()
    assert not mq.enqueue.called


async def test_try_start_next_sets_idle_when_queue_empty():
    runner, beads, _, mq = _make_runner(has_in_progress=False, next_task=None)

    await runner.try_start_next(CHANNEL)

    assert runner.get_state(CHANNEL) == TaskQueueState.IDLE
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
    runner, beads, _, mq = _make_runner(has_in_progress=False, next_task=task)

    await runner.try_start_next(CHANNEL)

    prompt = mq.enqueue.call_args[0][1]
    assert "bd-xyz9" in prompt
    assert "bd close bd-xyz9" in prompt
    assert "[TASK_COMPLETE]" in prompt


async def test_try_start_next_sets_running_state():
    task = _task()
    runner, _, _, _ = _make_runner(has_in_progress=False, next_task=task)

    await runner.try_start_next(CHANNEL)

    assert runner.get_state(CHANNEL) == TaskQueueState.RUNNING


# --- on_item_done ---


async def test_on_item_done_task_complete_starts_next():
    task = _task()
    runner, beads, sm, mq = _make_runner(has_in_progress=False, next_task=_task("bd-bbb2", "Do Y"))
    runner._current_task_titles[CHANNEL] = task.title
    sm.get_last_response.return_value = "All done. [TASK_COMPLETE]"

    item = _make_item(source="task_queue", task_id=task.id)
    await runner.on_item_done(CHANNEL, item)

    # Bot does NOT call bd close — Claude does it
    beads.close_task.assert_not_called()
    assert mq.enqueue.called  # next task started


async def test_on_item_done_waiting_for_input_pauses():
    task = _task()
    runner, beads, sm, mq = _make_runner()
    runner._current_task_titles[CHANNEL] = task.title
    sm.get_last_response.return_value = "Need more info. [WAITING_FOR_INPUT]"

    item = _make_item(source="task_queue", task_id=task.id)
    await runner.on_item_done(CHANNEL, item)

    beads.close_task.assert_called_once_with(CWD, task.id)
    assert runner.get_state(CHANNEL) == TaskQueueState.PAUSED_AWAITING_HUMAN
    assert not mq.enqueue.called


async def test_on_item_done_question_heuristic_pauses():
    task = _task()
    runner, beads, sm, _ = _make_runner()
    runner._current_task_titles[CHANNEL] = task.title
    sm.get_last_response.return_value = "Which approach: A or B?"

    item = _make_item(source="task_queue", task_id=task.id)
    await runner.on_item_done(CHANNEL, item)

    beads.close_task.assert_called_once_with(CWD, task.id)
    assert runner.get_state(CHANNEL) == TaskQueueState.PAUSED_AWAITING_HUMAN


async def test_on_item_done_no_marker_starts_next():
    task = _task()
    runner, beads, sm, mq = _make_runner(has_in_progress=False, next_task=_task("bd-bbb2", "Do Y"))
    runner._current_task_titles[CHANNEL] = task.title
    sm.get_last_response.return_value = "Completed successfully."

    item = _make_item(source="task_queue", task_id=task.id)
    await runner.on_item_done(CHANNEL, item)

    assert mq.enqueue.called


async def test_on_item_done_empty_response_resets_task():
    task = _task()
    runner, beads, sm, mq = _make_runner()
    runner._current_task_titles[CHANNEL] = task.title
    sm.get_last_response.return_value = ""

    item = _make_item(source="task_queue", task_id=task.id)
    await runner.on_item_done(CHANNEL, item)

    beads.reset_task.assert_called_once_with(CWD, task.id)
    assert runner.get_state(CHANNEL) == TaskQueueState.IDLE
    assert not mq.enqueue.called


async def test_on_item_done_user_source_clears_awaiting_human():
    task = _task()
    runner, _, _, mq = _make_runner(has_in_progress=False, next_task=task)
    runner.set_state(CHANNEL, TaskQueueState.PAUSED_AWAITING_HUMAN)

    item = _make_item(source="user")
    await runner.on_item_done(CHANNEL, item)

    assert mq.enqueue.called


async def test_on_item_done_user_source_noop_when_not_paused():
    runner, _, _, mq = _make_runner()
    runner.set_state(CHANNEL, TaskQueueState.IDLE)

    item = _make_item(source="user")
    await runner.on_item_done(CHANNEL, item)

    assert not mq.enqueue.called
