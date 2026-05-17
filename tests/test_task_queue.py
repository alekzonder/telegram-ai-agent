from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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
