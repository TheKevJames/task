"""Failures reported to whoever ran a command."""


class TaskError(Exception):
    """A user-facing failure, reported without a traceback."""
