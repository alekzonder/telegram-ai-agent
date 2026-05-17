"""TaskQueue — per-channel queue of planned tasks for autonomous execution."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


class TaskQueueState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED_AWAITING_HUMAN = "paused_awaiting_human"
    PAUSED_BY_USER = "paused_by_user"


@dataclass
class Task:
    id: str
    text: str
    priority: int
    status: Literal["pending", "running", "done", "skipped"]
    created_at: float
    media_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "priority": self.priority,
            "status": self.status,
            "created_at": self.created_at,
            "media_paths": self.media_paths,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(
            id=d["id"],
            text=d["text"],
            priority=d.get("priority", 100),
            status=d.get("status", "pending"),
            created_at=d.get("created_at", 0.0),
            media_paths=d.get("media_paths", []),
        )


class TaskQueue:
    """Per-channel persistent task queue."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._tasks: list[Task] = []
        self._state: TaskQueueState = TaskQueueState.IDLE
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._tasks = [Task.from_dict(t) for t in data.get("tasks", [])]
            # Reset stuck "running" tasks to "pending" on load (bot restart)
            for t in self._tasks:
                if t.status == "running":
                    t.status = "pending"
        except Exception:
            logger.warning("TaskQueue: corrupt queue file %s, starting empty", self._path)
            self._tasks = []

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"tasks": [t.to_dict() for t in self._tasks]}, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception:
            logger.warning("TaskQueue: failed to save %s", self._path, exc_info=True)

    @property
    def state(self) -> TaskQueueState:
        return self._state

    def set_state(self, state: TaskQueueState) -> None:
        self._state = state

    def add(self, text: str, *, priority: int = 100, media_paths: list[str] | None = None) -> Task:
        task = Task(
            id=str(uuid.uuid4()),
            text=text,
            priority=priority,
            status="pending",
            created_at=time.time(),
            media_paths=media_paths or [],
        )
        self._tasks.append(task)
        self._tasks.sort(key=lambda t: (t.priority, t.created_at))
        self.save()
        return task

    def list_pending(self) -> list[Task]:
        return [t for t in self._tasks if t.status == "pending"]

    def peek_next(self) -> Task | None:
        pending = self.list_pending()
        return pending[0] if pending else None

    def mark_running(self, task_id: str) -> None:
        for t in self._tasks:
            if t.id == task_id:
                t.status = "running"
                self.save()
                return

    def mark_done(self, task_id: str) -> None:
        for t in self._tasks:
            if t.id == task_id:
                t.status = "done"
                self.save()
                return

    def skip(self, task_id: str) -> None:
        for t in self._tasks:
            if t.id == task_id:
                t.status = "skipped"
                self.save()
                return

    def skip_next(self) -> Task | None:
        task = self.peek_next()
        if task:
            self.skip(task.id)
        return task

    def clear(self) -> int:
        count = len(self.list_pending())
        for t in self._tasks:
            if t.status == "pending":
                t.status = "skipped"
        self.save()
        return count
