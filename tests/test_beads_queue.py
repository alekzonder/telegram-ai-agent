"""Tests for BeadsQueue CLI wrapper."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from telegram_bot.core.services.task_queue import BeadsQueue, BeadsTask

CWD = "/tmp/project"


@pytest.fixture()
def queue():
    return BeadsQueue()


async def test_get_next_returns_first_task(queue):
    tasks = [
        {"id": "bd-aaa1", "title": "Fix bug", "priority": 1, "status": "open"},
        {"id": "bd-bbb2", "title": "Add feature", "priority": 2, "status": "open"},
    ]
    with patch.object(queue, "_run", new=AsyncMock(return_value=json.dumps(tasks))):
        result = await queue.get_next(CWD)
    assert result == BeadsTask(id="bd-aaa1", title="Fix bug", priority=1, status="open")


async def test_get_next_returns_none_on_empty(queue):
    with patch.object(queue, "_run", new=AsyncMock(return_value="[]")):
        result = await queue.get_next(CWD)
    assert result is None


async def test_get_next_returns_none_on_empty_output(queue):
    with patch.object(queue, "_run", new=AsyncMock(return_value="")):
        result = await queue.get_next(CWD)
    assert result is None


async def test_has_in_progress_true(queue):
    tasks = [{"id": "bd-aaa1", "title": "Fix bug", "priority": 1, "status": "in_progress"}]
    with patch.object(queue, "_run", new=AsyncMock(return_value=json.dumps(tasks))):
        result = await queue.has_in_progress(CWD)
    assert result is True


async def test_has_in_progress_false(queue):
    with patch.object(queue, "_run", new=AsyncMock(return_value="[]")):
        result = await queue.has_in_progress(CWD)
    assert result is False


async def test_claim_task_calls_correct_args(queue):
    mock_run = AsyncMock(return_value="")
    with patch.object(queue, "_run", new=mock_run):
        await queue.claim_task(CWD, "bd-aaa1")
    mock_run.assert_called_once_with(CWD, "update", "bd-aaa1", "--status", "in_progress")


async def test_close_task_calls_correct_args(queue):
    mock_run = AsyncMock(return_value="")
    with patch.object(queue, "_run", new=mock_run):
        await queue.close_task(CWD, "bd-aaa1")
    mock_run.assert_called_once_with(CWD, "close", "bd-aaa1")


async def test_reset_task_calls_correct_args(queue):
    mock_run = AsyncMock(return_value="")
    with patch.object(queue, "_run", new=mock_run):
        await queue.reset_task(CWD, "bd-aaa1")
    mock_run.assert_called_once_with(CWD, "update", "bd-aaa1", "--status", "open")


async def test_add_task_calls_bd_q_and_returns_id(queue):
    mock_run = AsyncMock(return_value="bd-ccc3\n")
    with patch.object(queue, "_run", new=mock_run):
        result = await queue.add_task(CWD, "New task", priority=1)
    mock_run.assert_called_once_with(CWD, "q", "New task", "-p", "1")
    assert result == "bd-ccc3"


async def test_add_task_default_priority(queue):
    mock_run = AsyncMock(return_value="bd-ddd4\n")
    with patch.object(queue, "_run", new=mock_run):
        await queue.add_task(CWD, "Default priority task")
    mock_run.assert_called_once_with(CWD, "q", "Default priority task", "-p", "2")


async def test_list_tasks_parses_json(queue):
    tasks = [
        {"id": "bd-aaa1", "title": "Task A", "priority": 0, "status": "open"},
        {"id": "bd-bbb2", "title": "Task B", "priority": 2, "status": "in_progress"},
    ]
    with patch.object(queue, "_run", new=AsyncMock(return_value=json.dumps(tasks))):
        result = await queue.list_tasks(CWD)
    assert len(result) == 2
    assert result[0] == BeadsTask(id="bd-aaa1", title="Task A", priority=0, status="open")
    assert result[1] == BeadsTask(id="bd-bbb2", title="Task B", priority=2, status="in_progress")


async def test_list_tasks_returns_empty_on_empty_output(queue):
    with patch.object(queue, "_run", new=AsyncMock(return_value="")):
        result = await queue.list_tasks(CWD)
    assert result == []
