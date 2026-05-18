"""Tests for /qadd, /qstatus handlers and queue command router."""

from __future__ import annotations

from telegram_bot.core.handlers.task_queue_cmds import (
    _parse_qadd_text,
    _parse_qstatus_args,
    router,
)


def test_parse_qadd_text_strips_command():
    assert _parse_qadd_text("/qadd do the thing") == ("do the thing", 2)
    assert _parse_qadd_text("/qadd   spaces  ") == ("spaces", 2)
    assert _parse_qadd_text("/qadd") == ("", 2)


def test_parse_qadd_text_with_priority_prefix():
    assert _parse_qadd_text("/qadd p0 Critical bug") == ("Critical bug", 0)
    assert _parse_qadd_text("/qadd p1 Important task") == ("Important task", 1)
    assert _parse_qadd_text("/qadd p4 Someday maybe") == ("Someday maybe", 4)


def test_parse_qadd_text_invalid_priority_treated_as_text():
    assert _parse_qadd_text("/qadd p5 oops") == ("p5 oops", 2)
    assert _parse_qadd_text("/qadd p-1 nope") == ("p-1 nope", 2)
    assert _parse_qadd_text("/qadd p9 too high") == ("p9 too high", 2)


def test_parse_qadd_text_priority_alone_is_treated_as_text():
    assert _parse_qadd_text("/qadd p0") == ("p0", 2)


def test_parse_qstatus_args_valid():
    assert _parse_qstatus_args("/qstatus bd-aaa1 open") == ("bd-aaa1", "open")
    assert _parse_qstatus_args("/qstatus bd-aaa1 in_progress") == ("bd-aaa1", "in_progress")
    assert _parse_qstatus_args("/qstatus bd-aaa1 closed") == ("bd-aaa1", "closed")


def test_parse_qstatus_args_invalid_status():
    assert _parse_qstatus_args("/qstatus bd-aaa1 unknown") == (None, None)


def test_parse_qstatus_args_missing_args():
    assert _parse_qstatus_args("/qstatus") == (None, None)
    assert _parse_qstatus_args("/qstatus bd-aaa1") == (None, None)


def test_router_has_all_queue_commands():
    command_names: set[str] = set()
    for observer in router.message.handlers:
        for f in observer.filters:
            cb = getattr(f, "callback", f)
            if hasattr(cb, "commands"):
                command_names.update(cb.commands)
    expected = {"qadd", "qmode", "qlist", "qstatus", "qpriority"}
    missing = expected - command_names
    assert not missing, f"Missing commands: {missing}"
    removed = {"qskip", "qclear", "qnext"} & command_names
    assert not removed, f"Commands should be removed: {removed}"
