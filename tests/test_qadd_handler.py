"""Tests for /qadd handler and queue command router."""

from __future__ import annotations

from telegram_bot.core.handlers.task_queue_cmds import (
    _parse_qadd_text,
    router,
)


def test_parse_qadd_text_strips_command():
    assert _parse_qadd_text("/qadd do the thing") == "do the thing"
    assert _parse_qadd_text("/qadd   spaces  ") == "spaces"
    assert _parse_qadd_text("/qadd") == ""


def test_router_has_all_queue_commands():
    command_names: set[str] = set()
    for observer in router.message.handlers:
        for f in observer.filters:
            # FilterObject wraps the actual filter in .callback
            cb = getattr(f, "callback", f)
            if hasattr(cb, "commands"):
                command_names.update(cb.commands)
    expected = {"qadd", "qmode", "qlist", "qskip", "qclear", "qpause", "qresume", "qnext"}
    missing = expected - command_names
    assert not missing, f"Missing commands: {missing}"
