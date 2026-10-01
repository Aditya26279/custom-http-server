"""Static file serving: safe path resolution, MIME types, caching validators
(ETag / Last-Modified -> 304) and single byte-range requests (206)."""

from __future__ import annotations

import mimetypes
import os
import re
import sys
from email.utils import formatdate, parsedate_to_datetime
from typing import BinaryIO, Iterator
from urllib.parse import quote

from .errors import HTTPError
from .request import Request
from .response import Response

_RANGE_RE = re.compile(r"^bytes=([0-9]*)-([0-9]*)$")

# Opening these on Windows reaches a device (console, printer, ...) regardless
# of directory or extension: "public/con.txt" is the console.
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{dev}{n}" for dev in ("COM", "LPT") for n in (*"123456789", "\u00b9", "\u00b2", "\u00b3")
}


def _is_windows_reserved(part: str) -> bool:
    stem = part.split(".", 1)[0].rstrip(" ").upper()
    return stem in _WINDOWS_RESERVED

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/wasm", ".wasm")


class FileBody:
    """Streams a slice of an open file. Exposes the file so the server can use
    ``socket.sendfile`` (zero-copy where the OS supports it)."""

    def __init__(self, file: BinaryIO, offset: int, length: int, chunk_size: int):
        self.file = file
        self.offset = offset
        self.length = length
        self.chunk_size = chunk_size

    def __iter__(self) -> Iterator[bytes]:
        self.file.seek(self.offset)
        remaining = self.length
        while remaining > 0:
            chunk = self.file.read(min(self.chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

    def close(self) -> None:
        self.file.close()


class StaticFiles:
    def __init__(
        self,
        directory: str,
        index: str = "index.html",
        chunk_size: int = 64 * 1024,
        max_age: int = 0,
        allow_dotfiles: bool = False,
    ):
        self.root = os.path.realpath(directory)
        if not os.path.isdir(self.root):
            raise ValueError(f"static directory does not exist: {directory}")
        self.index = index
        self.chunk_size = chunk_size
        self.max_age = max_age
        self.allow_dotfiles = allow_dotfiles

    def resolve(self, filepath: str) -> str | None:
        """Map a URL sub-path to a filesystem path inside ``root``, or None.

        Defends against traversal (``../``), absolute paths, Windows drive
        letters / backslashes, and (optionally) hidden dotfiles.
        """
        parts = [p for p in filepath.replace("\\", "/").split("/") if p not in ("", ".")]
        for part in parts:
            if part == ".." or ":" in part or any(c < " " for c in part):
                return None
            if part.startswith(".") and not self.allow_dotfiles:
                return None
            if sys.platform == "win32" and (_is_windows_reserved(part) or part[-1] in ". "):
                # Trailing dots/spaces are silently stripped by Windows, which
                # would let "secret.txt." alias "secret.txt".
                return None
        try:
            full = os.path.realpath(os.path.join(self.root, *parts))
            # realpath also resolves symlinks, so a link pointing outside root is rejected too.
            inside = os.path.commonpath([self.root, full]) == self.root
        except (OSError, ValueError):  # e.g. invalid filename, or a different drive
            return None
        return full if inside else None

    def __call__(self, request: Request, filepath: str = "") -> Response:
        full = self.resolve(filepath)
        if full is None:
            raise HTTPError(404, "not found")

        if os.path.isdir(full):
            if not request.path.endswith("/"):
                # Collapse leading slashes: "//evil.com" in Location is a
                # protocol-relative URL, i.e. an open redirect to another host.
                # Re-quote so non-ASCII/reserved chars are a valid header value.
                location = "/" + quote(request.path.lstrip("/"), safe="/!$&'()*+,;=:@-._~") + "/"
                if request.query_string:
                    location += "?" + request.query_string
                return Response.redirect(location, 301)
            full = os.path.join(full, self.index)

        try:
            st = os.stat(full)
        except (FileNotFoundError, NotADirectoryError):
            raise HTTPError(404, "not found") from None
        if not os.path.isfile(full):
            raise HTTPError(404, "not found")

        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        last_modified = formatdate(st.st_mtime, usegmt=True)
        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/json", "image/svg+xml"):
            content_type += "; charset=utf-8"

        headers = {
            "ETag": etag,
            "Last-Modified": last_modified,
            "Accept-Ranges": "bytes",
            "Cache-Control": f"public, max-age={self.max_age}" if self.max_age else "no-cache",
            "X-Content-Type-Options": "nosniff",
        }

        if self._not_modified(request, etag, int(st.st_mtime)):
            return Response(b"", 304, headers=headers)

        size = st.st_size
        start, end, status = 0, size - 1, 200
        byte_range = self._requested_range(request, etag, size)
        if byte_range is not None:
            start, end = byte_range
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        try:
            f = open(full, "rb")
        except PermissionError:
            raise HTTPError(403, "forbidden") from None
        length = max(0, end - start + 1)
        headers["Content-Length"] = str(length)
        body = FileBody(f, start, length, self.chunk_size)
        return Response(body, status, headers=headers, content_type=content_type)

    @staticmethod
    def _not_modified(request: Request, etag: str, mtime: int) -> bool:
        inm = request.headers.get("If-None-Match")
        if inm is not None:  # If-None-Match takes precedence (RFC 9110 §13.2.2)
            if inm.strip() == "*":
                return True
            tags = {t.strip().removeprefix("W/") for t in inm.split(",")}
            return etag in tags
        ims = request.headers.get("If-Modified-Since")
        if ims:
            try:
                return mtime <= int(parsedate_to_datetime(ims).timestamp())
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def _requested_range(request: Request, etag: str, size: int) -> tuple[int, int] | None:
        header = request.headers.get("Range")
        if not header or request.method not in ("GET", "HEAD"):
            return None
        if_range = request.headers.get("If-Range")
        if if_range is not None and if_range.strip() != etag:
            return None  # representation changed: send the whole thing
        m = _RANGE_RE.match(header.strip())
        if not m:
            return None  # multi-range / unknown units: ignore, send 200
        first, last = m.groups()
        if not first and not last:
            return None
        if not first:  # suffix range: last N bytes
            n = int(last)
            if n == 0:
                raise _unsatisfiable(size)
            return max(0, size - n), size - 1
        start = int(first)
        if last and int(last) < start:
            return None  # syntactically invalid range: ignore it (RFC 9110 §14.1.1)
        if start >= size:
            raise _unsatisfiable(size)
        end = min(int(last), size - 1) if last else size - 1
        return start, end


def _unsatisfiable(size: int) -> HTTPError:
    return HTTPError(416, "range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
