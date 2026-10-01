"""Exceptions shared across the server."""

from __future__ import annotations


class HTTPError(Exception):
    """Raise from anywhere (parser, router, handler) to send an error response."""

    def __init__(self, status: int, message: str | None = None, headers: dict[str, str] | None = None):
        super().__init__(message or str(status))
        self.status = status
        self.message = message
        self.headers = headers or {}


class ConnectionClosed(Exception):
    """The peer closed the connection (EOF) while we were reading."""


class LineTooLong(Exception):
    """A request line or header line exceeded the configured limit."""
