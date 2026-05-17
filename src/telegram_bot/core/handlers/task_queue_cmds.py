"""Task queue command handlers."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from telegram_bot.core.services.task_queue import TaskQueueRunner, TaskQueueState
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.types import channel_key

logger = logging.getLogger(__name__)
router = Router(name="task_queue")


def _parse_qadd_text(text: str) -> str:
    """Strip '/qadd' prefix and return task text."""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _qadd_text_from_reply(msg: Message) -> str:
    """Extract text from a reply-to message."""
    if msg.text:
        return msg.text
    return msg.caption or ""


async def _download_media(bot: Bot, message: Message, file_cache_dir: str) -> list[str]:
    """Download photo or document. Returns list of local file paths."""
    paths: list[str] = []
    tmp_dir = Path(file_cache_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())

    if message.photo:
        photo = message.photo[-1]
        dest = tmp_dir / f"{ts}_{photo.file_unique_id}.jpg"
        try:
            await bot.download(photo.file_id, destination=dest)
            paths.append(str(dest))
        except Exception:
            logger.warning("Failed to download photo for task queue", exc_info=True)

    elif message.document:
        doc = message.document
        dest = tmp_dir / f"{ts}_{doc.file_unique_id}_{doc.file_name or 'file'}"
        try:
            await bot.download(doc.file_id, destination=dest)
            paths.append(str(dest))
        except Exception:
            logger.warning("Failed to download document for task queue", exc_info=True)

    return paths


@router.message(Command("qadd"))
async def handle_qadd(
    message: Message,
    bot: Bot,
    task_queue_runner: TaskQueueRunner,
    session_manager: object,
) -> None:
    queue = task_queue_runner._queue

    raw_text = message.text or message.caption or ""
    task_text = _parse_qadd_text(raw_text)
    media_paths: list[str] = []

    if message.photo or message.document:
        cache_dir: str = getattr(session_manager, "file_cache_dir", "/tmp/bot_cache")
        media_paths = await _download_media(bot, message, cache_dir)
        if not media_paths:
            await message.answer("Не удалось скачать файл. Задача не добавлена.")
            return

    elif message.reply_to_message is not None:
        reply = message.reply_to_message
        if reply.photo or reply.document:
            cache_dir = getattr(session_manager, "file_cache_dir", "/tmp/bot_cache")
            media_paths = await _download_media(bot, reply, cache_dir)
        if not task_text:
            task_text = _qadd_text_from_reply(reply)

    if not task_text and not media_paths:
        await message.answer("Укажи текст задачи: /qadd <текст задачи>")
        return

    queue.add(task_text, media_paths=media_paths)
    position = len(queue.list_pending())
    await message.answer(f"Задача #{position} добавлена: {task_text[:80]}")


@router.message(Command("qmode"))
async def handle_qmode(
    message: Message,
    task_queue_runner: TaskQueueRunner,
    topic_config: TopicConfig,
) -> None:
    key = channel_key(message)
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or parts[1].strip() not in {"on", "off"}:
        current = "on" if task_queue_runner._qmode_enabled else "off"
        await message.answer(f"Режим очереди: {current}\nИспользование: /qmode on|off")
        return

    enabled = parts[1].strip() == "on"
    task_queue_runner.set_qmode(enabled)

    thread_id = key[1]
    if thread_id is not None:
        await topic_config.update_qmode(thread_id, enabled)

    status = "включён" if enabled else "выключен"
    await message.answer(f"Режим очереди {status}.")


@router.message(Command("qlist"))
async def handle_qlist(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    queue = task_queue_runner._queue
    pending = queue.list_pending()
    state_label = {
        TaskQueueState.IDLE: "готова",
        TaskQueueState.RUNNING: "выполняется",
        TaskQueueState.PAUSED_AWAITING_HUMAN: "ожидает ответа",
        TaskQueueState.PAUSED_BY_USER: "на паузе",
    }.get(queue.state, queue.state.value)

    if not pending:
        await message.answer(f"Очередь пуста. Состояние: {state_label}.")
        return

    lines = [f"Очередь ({len(pending)} задач, состояние: {state_label}):"]
    for i, t in enumerate(pending, 1):
        preview = t.text[:60] + ("…" if len(t.text) > 60 else "")
        lines.append(f"{i}. {preview}")
    await message.answer("\n".join(lines))


@router.message(Command("qskip"))
async def handle_qskip(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    queue = task_queue_runner._queue
    skipped = queue.skip_next()
    if skipped:
        await message.answer(f"Задача пропущена: {skipped.text[:60]}")
    else:
        await message.answer("Нет задач для пропуска.")


@router.message(Command("qclear"))
async def handle_qclear(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    queue = task_queue_runner._queue
    count = queue.clear()
    queue.set_state(TaskQueueState.IDLE)
    await message.answer(f"Очередь очищена ({count} задач удалено).")


@router.message(Command("qpause"))
async def handle_qpause(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    task_queue_runner._queue.set_state(TaskQueueState.PAUSED_BY_USER)
    await message.answer("Очередь на паузе. /qresume для возобновления.")


@router.message(Command("qresume"))
async def handle_qresume(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    task_queue_runner._queue.set_state(TaskQueueState.IDLE)
    await task_queue_runner.try_start_next(key)
    await message.answer("Очередь возобновлена.")


@router.message(Command("qnext"))
async def handle_qnext(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    queue = task_queue_runner._queue
    if queue.state == TaskQueueState.PAUSED_AWAITING_HUMAN:
        queue.set_state(TaskQueueState.IDLE)
    await task_queue_runner.try_start_next(key)
    await message.answer("Следующая задача запущена.")
