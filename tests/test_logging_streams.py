"""Ordinary logging goes to stdout; only WARNING and above goes to stderr.

Railway files a log line's severity by the stream it arrived on, so with
everything on stderr -- which is what `logging.basicConfig` does -- every
routine `INFO` line in production was labelled `error`, and filtering the
deploy's logs for errors returned the whole log. See
`app.py::build_log_handlers`.

The handlers are built and exercised here rather than installed on the root
logger: doing the latter inside a test would take pytest's own log capture
away from every test that ran afterwards.
"""

from __future__ import annotations

import io
import logging

import app


def _emit(level: int, message: str) -> tuple[str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    record = logging.LogRecord(
        name="wanas.test", level=level, pathname=__file__, lineno=1,
        msg=message, args=(), exc_info=None,
    )
    for handler in app.build_log_handlers(stdout, stderr):
        if record.levelno >= handler.level:
            handler.handle(record)
    return stdout.getvalue(), stderr.getvalue()


def test_info_goes_to_stdout_only():
    out, err = _emit(logging.INFO, "a routine line")
    assert "a routine line" in out
    assert err == ""


def test_debug_goes_to_stdout_only():
    out, err = _emit(logging.DEBUG, "a noisy line")
    assert "a noisy line" in out
    assert err == ""


def test_warning_goes_to_stderr_only():
    out, err = _emit(logging.WARNING, "rejected a webhook with a bad signature")
    assert "rejected a webhook with a bad signature" in err
    # Not on both: a line in two places is a line counted twice.
    assert out == ""


def test_error_goes_to_stderr_only():
    out, err = _emit(logging.ERROR, "something broke")
    assert "something broke" in err
    assert out == ""


def test_the_two_handlers_are_the_only_ones_installed():
    """Replaced, never appended -- otherwise every line is written twice."""
    handlers = app.build_log_handlers(io.StringIO(), io.StringIO())
    assert len(handlers) == 2
    assert handlers[1].level == logging.WARNING
