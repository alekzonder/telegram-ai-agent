"""Task queue command handlers."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from telegram_bot.core.services.task_queue import TaskQueueRunner
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.types import channel_key

logger = logging.getLogger(__name__)
router = Router(name="task_queue")

_VALID_STATUSES = {"open", "in_progress", "closed"}


def _parse_qadd_text(text: str) -> tuple[str, int]:
    """Return (task_text, priority). Priority defaults to 2 if not specified."""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return "", 2
    body = parts[1].strip()
    # Check for optional p<N> prefix (p0-p4 only, must be followed by a space and more text)
    if len(body) >= 3 and body[0] == "p" and body[1].isdigit() and body[2] == " ":
        priority = int(body[1])
        if 0 <= priority <= 4:
            return body[3:].strip(), priority
    return body, 2


def _parse_qstatus_args(text: str) -> tuple[str | None, str | None]:
    """Return (task_id, status) or (None, None) on invalid input."""
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        return None, None
    task_id = parts[1].strip()
    status = parts[2].strip()
    if status not in _VALID_STATUSES:
        return None, None
    return task_id, status


@router.message(Command("qadd"))
async def handle_qadd(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    cwd = task_queue_runner.get_cwd(key)

    raw_text = message.text or message.caption or ""
    task_text, priority = _parse_qadd_text(raw_text)

    if not task_text:
        await message.answer("Укажи текст задачи: /qadd [p0-p4] <текст задачи>")
        return

    task_id = await task_queue_runner._beads_queue.add_task(cwd, task_text, priority=priority)
    if task_id:
        await message.answer(f"Задача добавлена [{task_id}] [p{priority}]: {task_text[:80]}")
    else:
        await message.answer("Не удалось добавить задачу (bd вернул пустой ответ).")


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
        current = "on" if task_queue_runner.is_qmode(key) else "off"
        await message.answer(f"Режим очереди: {current}\nИспользование: /qmode on|off")
        return

    enabled = parts[1].strip() == "on"
    task_queue_runner.set_qmode(key, enabled)

    thread_id = key[1]
    if thread_id is not None:
        await topic_config.update_qmode(thread_id, enabled)

    status = "включён" if enabled else "выключен"
    await message.answer(f"Режим очереди {status}.")

    if enabled:
        await task_queue_runner.try_start_next(key)


@router.message(Command("qlist"))
async def handle_qlist(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    cwd = task_queue_runner.get_cwd(key)

    tasks = await task_queue_runner._beads_queue.list_tasks(cwd)
    qmode_label = "вкл" if task_queue_runner.is_qmode(key) else "выкл"

    if not tasks:
        await message.answer(f"Очередь пуста. qmode: {qmode_label}.")
        return

    lines = [f"Очередь ({len(tasks)} задач, qmode: {qmode_label}):"]
    for task in tasks:
        marker = "⚙️" if task.status == "in_progress" else "•"
        preview = task.title[:60] + ("…" if len(task.title) > 60 else "")
        lines.append(f"{marker} [{task.id}] [p{task.priority}] [{task.status}] {preview}")
    await message.answer("\n".join(lines))


@router.message(Command("qstatus"))
async def handle_qstatus(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    cwd = task_queue_runner.get_cwd(key)

    raw_text = message.text or ""
    task_id, status = _parse_qstatus_args(raw_text)

    if task_id is None or status is None:
        await message.answer("Использование: /qstatus <id> open|in_progress|closed")
        return

    await task_queue_runner._beads_queue.set_status(cwd, task_id, status)
    await message.answer(f"Статус [{task_id}] → {status}")

    if status == "open" and task_queue_runner.is_qmode(key):
        await task_queue_runner.try_start_next(key, silent=True)


@router.message(Command("qpriority"))
async def handle_qpriority(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    cwd = task_queue_runner.get_cwd(key)

    raw_text = message.text or ""
    parts = raw_text.split(maxsplit=2)
    if len(parts) < 3 or not parts[2].isdigit() or not (0 <= int(parts[2]) <= 4):
        await message.answer(
            "Использование: /qpriority <id> <0-4>\n(0=critical, 1=high, 2=medium, 3=low, 4=backlog)"
        )
        return

    task_id = parts[1].strip()
    priority = int(parts[2])
    await task_queue_runner._beads_queue.set_priority(cwd, task_id, priority)
    await message.answer(f"Приоритет задачи [{task_id}] установлен: p{priority}")
