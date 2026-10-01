"""HTTP response objects and serialization."""

from __future__ import annotations

import json
from html import escape
from typing import Any, Iterable, Mapping

from .headers import Headers

STATUS_REASONS: dict[int, str] = {
    100: "Continue",
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    206: "Partial Content",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    304: "Not Modified",
    307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    413: "Content Too Large",
    414: "URI Too Long",
    415: "Unsupported Media Type",
    416: "Range Not Satisfiable",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}

# A body is either fully in memory, or an iterable of byte chunks streamed out.
Body = bytes | Iterable[bytes]


class Response:
    def __init__(
        self,
        body: str | bytes | Iterable[bytes] = b"",
        status: int = 200,
        headers: Headers | Mapping[str, str] | None = None,
        content_type: str | None = None,
    ):
        self.status = status
        self.headers = headers if isinstance(headers, Headers) else Headers(headers)
        if isinstance(body, str):
            body = body.encode("utf-8")
            content_type = content_type or "text/plain; charset=utf-8"
        elif isinstance(body, (bytearray, memoryview)):
            body = bytes(body)
        self.body: Body = body
        if content_type:
            self.headers["Content-Type"] = content_type

    @property
    def reason(self) -> str:
        return STATUS_REASONS.get(self.status, "Unknown")

    # -- convenience constructors -------------------------------------------

    @classmethod
    def text(cls, text: str, status: int = 200, **kw: Any) -> "Response":
        return cls(text, status, content_type="text/plain; charset=utf-8", **kw)

    @classmethod
    def html(cls, html: str, status: int = 200, **kw: Any) -> "Response":
        return cls(html, status, content_type="text/html; charset=utf-8", **kw)

    @classmethod
    def json(cls, data: Any, status: int = 200, **kw: Any) -> "Response":
        payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        return cls(payload, status, content_type="application/json", **kw)

    @classmethod
    def redirect(cls, location: str, status: int = 302) -> "Response":
        return cls(b"", status, headers={"Location": location})

    @classmethod
    def error(cls, status: int, message: str | None = None) -> "Response":
        reason = STATUS_REASONS.get(status, "Error")
        detail = f"<p>{escape(message)}</p>" if message else ""
        page = (
            f"<!doctype html><html><head><title>{status} {reason}</title></head>"
            f"<body><h1>{status} {reason}</h1>{detail}<hr><small>rawhttp</small></body></html>"
        )
        return cls.html(page, status)

    # -- serialization ---------------------------------------------------------

    def serialize_head(self) -> bytes:
        lines = [f"HTTP/1.1 {self.status} {self.reason}"]
        lines += [f"{name}: {value}" for name, value in self.headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    def close(self) -> None:
        """Release resources held by a streaming body (e.g. open files)."""
        close = getattr(self.body, "close", None)
        if callable(close):
            close()
