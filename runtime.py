"""Exceptions shared by the event-loop-native query runtime."""


class QueryCancelled(RuntimeError):
    pass


class Refused(Exception):
    """An ordinary query cannot be answered; unlike SystemExit this never stops the process."""


class QueryBudgetExceeded(Refused):
    """A bounded query exhausted work, not wall-clock time or client cancellation."""
