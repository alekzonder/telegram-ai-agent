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


@router.message(Command("qskip"))
async def handle_qskip(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    cwd = task_queue_runner.get_cwd(key)

    task = await task_queue_runner._beads_queue.get_next(cwd)
    if task is None:
        await message.answer("Нет задач для пропуска.")
        return

    await task_queue_runner._beads_queue.close_task(cwd, task.id)
    await message.answer(f"Задача пропущена: [{task.id}] {task.title[:60]}")


@router.message(Command("qclear"))
async def handle_qclear(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    cwd = task_queue_runner.get_cwd(key)

    tasks = await task_queue_runner._beads_queue.list_tasks(cwd)
    open_tasks = [t for t in tasks if t.status == "open"]
    for task in open_tasks:
        await task_queue_runner._beads_queue.close_task(cwd, task.id)

    await message.answer(f"Очередь очищена ({len(open_tasks)} задач закрыто).")


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


@router.message(Command("qnext"))
async def handle_qnext(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    await task_queue_runner.try_start_next(key)
    await message.answer("Следующая задача запущена.")
