from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from telegram_bot.core.config import Settings
from telegram_bot.core.services.bot_commands import build_bot_commands
from telegram_bot.core.services.claude import SessionManager
from telegram_bot.core.services.message_queue import MessageQueue, QueueItem
from telegram_bot.core.services.task_queue import TaskQueue, TaskQueueState
from telegram_bot.core.services.topic_config import TopicConfig


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


def test_task_queue_add_and_list(tmp_path):
    q = TaskQueue(tmp_path / "q.json")
    t1 = q.add("do X", priority=10)
    t2 = q.add("do Y", priority=5)
    tasks = q.list_pending()
    assert tasks[0].id == t2.id  # lower priority number = first
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
    q.add("task A", priority=10)
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


# ---------------------------------------------------------------------------
# Task 5: TopicConfig qmode tests
# ---------------------------------------------------------------------------


def test_topic_config_parses_qmode(tmp_path):
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{}", encoding="utf-8")
    path = tmp_path / "topic_config.json"
    path.write_text(
        json.dumps(
            {
                "topics": {
                    "99": {
                        "name": "T",
                        "type": "assistant",
                        "mode": "free",
                        "cwd": str(tmp_path),
                        "mcp_config": str(mcp),
                        "qmode": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    topic = TopicConfig(str(path), ".").get_topic(99)
    assert topic.qmode is True


def test_topic_config_qmode_defaults_false(tmp_path):
    from telegram_bot.core.services.topic_config import _default_topic

    assert _default_topic().qmode is False


async def test_topic_config_update_qmode(tmp_path):
    path = tmp_path / "topic_config.json"
    tc = TopicConfig(str(path), ".")
    ok = await tc.update_qmode(42, True)
    assert ok
    data = json.loads(path.read_text())
    assert data["topics"]["42"]["qmode"] is True


# ---------------------------------------------------------------------------
# Task 6: task-manager.md marker protocol
# ---------------------------------------------------------------------------


def test_task_manager_prompt_has_markers():
    prompt = Path("src/telegram_bot/prompts/task-manager.md").read_text(encoding="utf-8")
    assert "[TASK_COMPLETE]" in prompt
    assert "[WAITING_FOR_INPUT]" in prompt


# ---------------------------------------------------------------------------
# Task 9: Bot command menu
# ---------------------------------------------------------------------------


def test_queue_commands_in_bot_menu():
    names = {cmd.command for cmd in build_bot_commands("en")}
    for cmd in ("qmode", "qadd", "qlist", "qskip", "qclear", "qpause", "qresume", "qnext"):
        assert cmd in names, f"//{cmd} missing from bot menu"


# ---------------------------------------------------------------------------
# Task 10: __main__.py wiring
# ---------------------------------------------------------------------------


def test_main_imports_task_queue_router():
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")
    assert "task_queue_cmds" in source
    assert "task_queue_router" in source


def test_main_wires_task_queue_runner():
    source = Path("src/telegram_bot/__main__.py").read_text(encoding="utf-8")
    assert "TaskQueueRunner" in source
    assert "on_item_complete" in source
    assert 'dp["task_queue_runner"]' in source
