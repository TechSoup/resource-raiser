"""Cooperative query deadline/cancellation propagated onto executor worker threads."""
import threading, time

_STATE = threading.local()


class QueryCancelled(RuntimeError):
    pass


def bind(cancel=None, deadline=None):
    _STATE.cancel, _STATE.deadline = cancel, deadline


def capture():
    return getattr(_STATE, "cancel", None), getattr(_STATE, "deadline", None)


def check():
    cancel, deadline = capture()
    if cancel is not None and cancel.is_set():
        raise QueryCancelled("query cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        if cancel is not None:
            cancel.set()
        raise QueryCancelled("query deadline exceeded")
