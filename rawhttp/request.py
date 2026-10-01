"""Parsing HTTP/1.x requests directly from a socket.

Implements the parts of RFC 9112 a real server needs: request-line and header
parsing with size limits, Content-Length and chunked bodies,
``Expect: 100-continue``, and keep-alive semantics.
"""

from __future__ import annotations

import json
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ConnectionClosed, HTTPError, LineTooLong
from .headers import Headers

# RFC 9110 "token" characters, used for methods and header names.
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_VERSION_RE = re.compile(r"^HTTP/(\d)\.(\d)$")
_HEX_RE = re.compile(rb"^[0-9A-Fa-f]+$")
# ASCII digits only: str.isdigit() also accepts e.g. "²", which int() rejects.
_DIGITS_RE = re.compile(r"^[0-9]+$")
# Control characters and spaces are never valid inside a request-target.
_TARGET_BAD_RE = re.compile(r"[\x00-\x20\x7f]")
# reg-name / IPv4 / [IPv6] with optional port (RFC 3986 authority, no userinfo).
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-_~!$&'()*+,;=%]*(:[0-9]*)?$|^\[[0-9A-Fa-f:.]+\](:[0-9]*)?$")
_MAX_LEADING_EMPTY_LINES = 8


@dataclass
class Limits:
    max_request_line: int = 8190
    max_header_line: int = 8190
    max_header_count: int = 100
    max_header_bytes: int = 64 * 1024
    max_body_bytes: int = 10 * 1024 * 1024


class SocketReader:
    """Buffered reader over a raw socket (we can't use makefile() because we
    need precise control over line limits and leftover pipelined bytes)."""

    def __init__(self, sock: socket.socket, bufsize: int = 64 * 1024):
        self.sock = sock
        self.bufsize = bufsize
        self.buf = bytearray()
        self.consumed = 0  # total bytes handed to the parser so far
        self.idle_timeout = sock.gettimeout()
        self.request_timeout: float | None = None
        self.deadline: float | None = None

    def begin_request(self, request_timeout: float | None) -> None:
        """Arm the per-request clock.

        Waiting for the *first* byte of a request uses the socket's idle
        timeout. Once any byte arrives, the whole request (headers + body)
        must be received within ``request_timeout`` in total. Without this
        overall deadline a client could drip one byte just inside the idle
        timeout forever and pin a worker (slowloris).
        """
        self.request_timeout = request_timeout
        self.deadline = None
        self.sock.settimeout(self.idle_timeout)
        if self.buf and request_timeout is not None:  # pipelined bytes already here
            self.deadline = time.monotonic() + request_timeout

    def _fill(self) -> None:
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("request read deadline exceeded")
            if self.idle_timeout is not None:
                remaining = min(remaining, self.idle_timeout)
            self.sock.settimeout(remaining)
        data = self.sock.recv(self.bufsize)
        if not data:
            raise ConnectionClosed()
        if self.deadline is None and self.request_timeout is not None:
            self.deadline = time.monotonic() + self.request_timeout
        self.buf += data

    def readline(self, limit: int) -> bytes:
        """Read through the next LF. Raises LineTooLong past ``limit`` bytes."""
        start = 0
        while True:
            idx = self.buf.find(b"\n", start)
            if idx != -1:
                if idx + 1 > limit:
                    raise LineTooLong()
                line = bytes(self.buf[: idx + 1])
                del self.buf[: idx + 1]
                self.consumed += len(line)
                return line
            if len(self.buf) > limit:
                raise LineTooLong()
            start = len(self.buf)
            self._fill()

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        data = bytes(self.buf[:n])
        del self.buf[:n]
        self.consumed += n
        return data

    @property
    def has_buffered_data(self) -> bool:
        return bool(self.buf)


@dataclass
class Request:
    method: str
    target: str
    path: str
    query_string: str
    version: str
    headers: Headers
    body: bytes = b""
    client_address: tuple[str, int] | None = None
    path_params: dict[str, Any] = field(default_factory=dict)

    @property
    def query(self) -> dict[str, list[str]]:
        return parse_qs(self.query_string, keep_blank_values=True)

    def arg(self, name: str, default: str | None = None) -> str | None:
        """First value of a query-string parameter."""
        values = self.query.get(name)
        return values[0] if values else default

    @property
    def keep_alive(self) -> bool:
        conn = ",".join(self.headers.get_all("Connection")).lower()
        tokens = {t.strip() for t in conn.split(",")}
        if self.version == "HTTP/1.1":
            return "close" not in tokens
        return "keep-alive" in tokens

    @property
    def content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except RecursionError:  # e.g. "[[[[...]]]]" nested thousands deep
            raise HTTPError(400, "JSON body nested too deeply") from None
        except ValueError as e:
            raise HTTPError(400, f"invalid JSON body: {e}") from None

    def form(self) -> dict[str, list[str]]:
        return parse_qs(self.text, keep_blank_values=True)


def _strip_eol(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return line[:-2]
    return line[:-1] if line.endswith(b"\n") else line


def _parse_request_line(line: bytes) -> tuple[str, str, str]:
    try:
        text = _strip_eol(line).decode("ascii")
    except UnicodeDecodeError:
        raise HTTPError(400, "request line is not ASCII") from None
    parts = text.split(" ")
    if len(parts) != 3:
        raise HTTPError(400, "malformed request line")
    method, target, version = parts
    if not _TOKEN_RE.match(method):
        raise HTTPError(400, "invalid method")
    m = _VERSION_RE.match(version)
    if not m:
        raise HTTPError(400, "invalid HTTP version")
    if m.group(1) != "1":
        raise HTTPError(505, f"{version} not supported")
    if not target:
        raise HTTPError(400, "empty request target")
    if _TARGET_BAD_RE.search(target):
        raise HTTPError(400, "invalid character in request target")
    return method, target, version


def _parse_headers(reader: SocketReader, limits: Limits) -> Headers:
    headers = Headers()
    total = 0
    while True:
        try:
            line = reader.readline(limits.max_header_line)
        except LineTooLong:
            raise HTTPError(431, "header line too long") from None
        total += len(line)
        if total > limits.max_header_bytes:
            raise HTTPError(431, "headers too large")
        line = _strip_eol(line)
        if not line:
            return headers
        if line[:1] in (b" ", b"\t"):
            raise HTTPError(400, "obsolete line folding is not allowed")
        if len(headers) >= limits.max_header_count:
            raise HTTPError(431, "too many headers")
        name, sep, value = line.partition(b":")
        if not sep:
            raise HTTPError(400, "header line missing ':'")
        try:
            name_s = name.decode("ascii")
        except UnicodeDecodeError:
            raise HTTPError(400, "invalid header name") from None
        # No whitespace allowed between field name and colon (request smuggling vector).
        if not _TOKEN_RE.match(name_s):
            raise HTTPError(400, f"invalid header name {name_s!r}")
        value_s = value.strip(b" \t").decode("latin-1")
        if "\r" in value_s or "\n" in value_s or "\x00" in value_s:
            raise HTTPError(400, "invalid character in header value")
        headers.add(name_s, value_s)


def _parse_target(method: str, target: str) -> tuple[str, str]:
    """Return (decoded_path, raw_query_string)."""
    if target == "*":
        if method != "OPTIONS":
            raise HTTPError(400, "'*' target only valid for OPTIONS")
        return "*", ""
    if target.startswith(("http://", "https://")):  # absolute-form
        parts = urlsplit(target)
        raw_path, query = parts.path or "/", parts.query
    elif target.startswith("/"):  # origin-form
        raw_path, _, query = target.partition("?")
    else:
        raise HTTPError(400, "invalid request target")
    raw_path = raw_path.split("#", 1)[0]
    try:
        path = unquote(raw_path, errors="strict") if "%" in raw_path else raw_path
    except UnicodeDecodeError:
        raise HTTPError(400, "path is not valid percent-encoded UTF-8") from None
    if any(c < " " or c == "\x7f" for c in path):
        raise HTTPError(400, "control character in path")
    return path, query


def _content_length(headers: Headers) -> int | None:
    values = headers.get_all("Content-Length")
    if not values:
        return None
    # Repeated or comma-joined values are fine only if they all agree.
    candidates = {v.strip() for raw in values for v in raw.split(",")}
    if len(candidates) != 1:
        raise HTTPError(400, "conflicting Content-Length")
    value = candidates.pop()
    if not _DIGITS_RE.match(value):
        raise HTTPError(400, "invalid Content-Length")
    return int(value)


def _read_chunked(reader: SocketReader, limits: Limits) -> bytes:
    body = bytearray()
    while True:
        try:
            line = _strip_eol(reader.readline(limits.max_header_line))
        except LineTooLong:
            raise HTTPError(400, "chunk size line too long") from None
        size_token = line.split(b";", 1)[0].strip()  # ignore chunk extensions
        if not _HEX_RE.match(size_token):
            raise HTTPError(400, "invalid chunk size")
        size = int(size_token, 16)
        if size == 0:
            break
        if len(body) + size > limits.max_body_bytes:
            raise HTTPError(413, "request body too large")
        body += reader.read(size)
        if reader.read(2) != b"\r\n":
            raise HTTPError(400, "missing CRLF after chunk data")
    # Consume (and discard) any trailer fields.
    _parse_headers(reader, limits)
    return bytes(body)


def parse_request(
    reader: SocketReader,
    limits: Limits | None = None,
    client_address: tuple[str, int] | None = None,
) -> Request:
    """Read exactly one request from ``reader``.

    Raises ConnectionClosed on a clean EOF before any bytes of a new request,
    and HTTPError for anything malformed.
    """
    limits = limits or Limits()

    # RFC 9112 §2.2: ignore (a few) empty lines preceding the request-line.
    for _ in range(_MAX_LEADING_EMPTY_LINES + 1):
        try:
            line = reader.readline(limits.max_request_line)
        except LineTooLong:
            raise HTTPError(414, "request line too long") from None
        if _strip_eol(line):
            break
    else:
        raise HTTPError(400, "too many empty lines before request")

    method, target, version = _parse_request_line(line)
    headers = _parse_headers(reader, limits)

    hosts = headers.get_all("Host")
    if len(hosts) > 1:
        raise HTTPError(400, "multiple Host headers")
    if version == "HTTP/1.1" and not hosts:
        raise HTTPError(400, "missing Host header")
    if hosts and not _HOST_RE.match(hosts[0]):
        raise HTTPError(400, "invalid Host header")

    path, query = _parse_target(method, target)

    # Join repeated fields: a proxy may honour the last TE header while we'd
    # otherwise only look at the first, which is a request-smuggling vector.
    te_values = headers.get_all("Transfer-Encoding")
    transfer_encoding = ", ".join(te_values) if te_values else None
    content_length = _content_length(headers)
    if transfer_encoding is not None and version == "HTTP/1.0":
        raise HTTPError(400, "Transfer-Encoding is not allowed in HTTP/1.0")
    if transfer_encoding is not None and content_length is not None:
        # Both present is a classic request-smuggling setup; refuse it.
        raise HTTPError(400, "both Transfer-Encoding and Content-Length present")

    body_expected = transfer_encoding is not None or bool(content_length)
    if content_length is not None and content_length > limits.max_body_bytes:
        raise HTTPError(413, "request body too large")

    if (
        body_expected
        and version == "HTTP/1.1"
        and (headers.get("Expect") or "").lower() == "100-continue"
    ):
        reader.sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")

    body = b""
    if transfer_encoding is not None:
        codings = [c.strip().lower() for c in transfer_encoding.split(",")]
        if codings != ["chunked"]:
            raise HTTPError(501, f"unsupported Transfer-Encoding: {transfer_encoding}")
        body = _read_chunked(reader, limits)
    elif content_length:
        body = reader.read(content_length)

    return Request(
        method=method,
        target=target,
        path=path,
        query_string=query,
        version=version,
        headers=headers,
        body=body,
        client_address=client_address,
    )
