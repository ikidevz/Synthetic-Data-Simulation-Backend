"""One error type for every request problem, with a machine-readable code.

`BatchError` (app/services/batch.py) subclasses this, so existing code that
raises or catches BatchError keeps working; a single handler in
app/api/error_handlers.py turns any ApiError into the standard
{"error": {"code", "message"}} envelope.
"""
from __future__ import annotations


class ApiError(Exception):
    """A request problem, with an error code and HTTP-style status."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
