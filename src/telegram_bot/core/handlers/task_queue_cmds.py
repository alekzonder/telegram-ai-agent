"""Task queue command handlers."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from telegram_bot.core.services.task_queue import TaskQueueRunner, TaskQueueState
from telegram_bot.core.services.topic_config import TopicConfig
from telegram_bot.core.types import channel_key

logger = logging.getLogger(__name__)
router = Router(name="task_queue")


def _parse_qadd_text(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


@router.message(Command("qadd"))
async def handle_qadd(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    cwd = task_queue_runner.get_cwd(key)

    raw_text = message.text or message.caption or ""
    task_text = _parse_qadd_text(raw_text)

    if not task_text:
        await message.answer("Укажи текст задачи: /qadd <текст задачи>")
        return

    task_id = await task_queue_runner._beads_queue.add_task(cwd, task_text)
    if task_id:
        await message.answer(f"Задача добавлена [{task_id}]: {task_text[:80]}")
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
    state = task_queue_runner.get_state(key)
    state_label = {
        TaskQueueState.IDLE: "готова",
        TaskQueueState.RUNNING: "выполняется",
        TaskQueueState.PAUSED_AWAITING_HUMAN: "ожидает ответа",
        TaskQueueState.PAUSED_BY_USER: "на паузе",
    }.get(state, state.value)

    if not tasks:
        await message.answer(f"Очередь пуста. Состояние: {state_label}.")
        return

    lines = [f"Очередь ({len(tasks)} задач, состояние: {state_label}):"]
    for task in tasks:
        marker = "⚙️" if task.status == "in_progress" else "•"
        preview = task.title[:60] + ("…" if len(task.title) > 60 else "")
        lines.append(f"{marker} [{task.id}] {preview}")
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

    task_queue_runner.set_state(key, TaskQueueState.IDLE)
    await message.answer(f"Очередь очищена ({len(open_tasks)} задач закрыто).")


@router.message(Command("qpause"))
async def handle_qpause(message: Message, task_queue_runner: TaskQueueRunner) -> None:
    key = channel_key(message)
    task_queue_runner.set_state(key, TaskQueueState.PAUSED_BY_USER)
    await message.answer("Очередь на паузе. /qresume для возобновления.")


@router.message(Command("qresume"))
async def handle_qresume(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    task_queue_runner.set_state(key, TaskQueueState.IDLE)
    await task_queue_runner.try_start_next(key)
    await message.answer("Очередь возобновлена.")


@router.message(Command("qnext"))
async def handle_qnext(
    message: Message,
    task_queue_runner: TaskQueueRunner,
) -> None:
    key = channel_key(message)
    if task_queue_runner.get_state(key) == TaskQueueState.PAUSED_AWAITING_HUMAN:
        task_queue_runner.set_state(key, TaskQueueState.IDLE)
    await task_queue_runner.try_start_next(key)
    await message.answer("Следующая задача запущена.")
