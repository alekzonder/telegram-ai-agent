"""Tests for TaskQueueRunner completion hook and state transitions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from telegram_bot.core.services.message_queue import QueueItem
from telegram_bot.core.services.task_queue import TaskQueue, TaskQueueRunner, TaskQueueState


def _make_item(source: str = "user", task_id: str | None = None) -> QueueItem:
    return QueueItem(
        entries=[(1, "prompt")],
        source_messages=[MagicMock()],
        source=source,
        task_id=task_id,
    )


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
    queue.add("do Y")
    sm.get_last_response.return_value = "Done. [TASK_COMPLETE]"

    item = _make_item(source="task_queue", task_id=task.id)
    await runner.on_item_done((1, 2), item)

    assert queue.state == TaskQueueState.RUNNING
    assert mq.enqueue.called


async def test_on_item_done_waiting_for_input_pauses(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    task = queue.add("do X")
    queue.mark_running(task.id)
    queue.set_state(TaskQueueState.RUNNING)
    sm.get_last_response.return_value = "[WAITING_FOR_INPUT]\nA or B?"

    item = _make_item(source="task_queue", task_id=task.id)
    await runner.on_item_done((1, 2), item)

    assert queue.state == TaskQueueState.PAUSED_AWAITING_HUMAN
    assert not mq.enqueue.called


async def test_on_item_done_question_heuristic_pauses(tmp_path):
    runner, queue, sm, _mq = _make_runner(tmp_path)
    task = queue.add("do X")
    queue.mark_running(task.id)
    queue.set_state(TaskQueueState.RUNNING)
    sm.get_last_response.return_value = "I need to know: approach A or B?"

    item = _make_item(source="task_queue", task_id=task.id)
    await runner.on_item_done((1, 2), item)

    assert queue.state == TaskQueueState.PAUSED_AWAITING_HUMAN


async def test_on_item_done_no_marker_starts_next(tmp_path):
    runner, queue, sm, mq = _make_runner(tmp_path)
    task = queue.add("do X")
    queue.mark_running(task.id)
    queue.set_state(TaskQueueState.RUNNING)
    queue.add("do Y")
    sm.get_last_response.return_value = "All done. Files updated."

    item = _make_item(source="task_queue", task_id=task.id)
    await runner.on_item_done((1, 2), item)

    assert mq.enqueue.called


async def test_on_item_done_user_source_clears_awaiting_human(tmp_path):
    runner, queue, _sm, mq = _make_runner(tmp_path)
    queue.set_state(TaskQueueState.PAUSED_AWAITING_HUMAN)
    queue.add("next task")

    item = _make_item(source="user")
    await runner.on_item_done((1, 2), item)

    assert mq.enqueue.called


async def test_try_start_next_noop_when_qmode_off(tmp_path):
    runner, queue, _sm, mq = _make_runner(tmp_path)
    runner.set_qmode(False)
    queue.add("task A")

    await runner.try_start_next((1, 2))
    assert not mq.enqueue.called


async def test_try_start_next_noop_when_paused_by_user(tmp_path):
    runner, queue, _sm, mq = _make_runner(tmp_path)
    queue.set_state(TaskQueueState.PAUSED_BY_USER)
    queue.add("task A")

    await runner.try_start_next((1, 2))
    assert not mq.enqueue.called


async def test_try_start_next_sets_idle_when_queue_empty(tmp_path):
    runner, queue, _sm, mq = _make_runner(tmp_path)
    queue.set_state(TaskQueueState.RUNNING)

    await runner.try_start_next((1, 2))
    assert queue.state == TaskQueueState.IDLE
    assert not mq.enqueue.called


async def test_try_start_next_enqueues_with_task_queue_source(tmp_path):
    runner, queue, _sm, mq = _make_runner(tmp_path)
    queue.add("my task")

    await runner.try_start_next((10, 20))

    assert mq.enqueue.called
    call_kwargs = mq.enqueue.call_args
    assert call_kwargs.kwargs.get("source") == "task_queue" or (
        len(call_kwargs.args) > 4 and False
    )  # kwargs path
    # Check via kwargs dict
    kwargs = mq.enqueue.call_args[1]
    assert kwargs["source"] == "task_queue"
    assert kwargs["task_id"] is not None
