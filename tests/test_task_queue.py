from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from telegram_bot.core.config import Settings
from telegram_bot.core.services.claude import SessionManager
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
    await asyncio.sleep(0.1)
    assert len(completed) == 1
    assert completed[0] == (key, "task_queue", "t1")


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


# ---------------------------------------------------------------------------
# TaskQueue tests
# ---------------------------------------------------------------------------

from telegram_bot.core.services.task_queue import Task, TaskQueue, TaskQueueState  # noqa: E402


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


def test_task_queue_restart_resets_running_to_pending(tmp_path):
    path = tmp_path / "q.json"
    q1 = TaskQueue(path)
    t = q1.add("task A")
    q1.mark_running(t.id)
    # Simulate restart
    q2 = TaskQueue(path)
    assert q2.list_pending()[0].status == "pending"
