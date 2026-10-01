"""The socket server: accept loop -> thread pool -> per-connection request loop."""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from email.utils import formatdate
from typing import Callable

from .errors import ConnectionClosed, HTTPError
from .request import Limits, Request, SocketReader, parse_request
from .response import Response
from .static import FileBody
from .threadpool import ThreadPool

log = logging.getLogger("rawhttp")
access_log = logging.getLogger("rawhttp.access")

SERVER_NAME = "rawhttp/1.0"
App = Callable[[Request], Response]


class HTTPServer:
    def __init__(
        self,
        app: App,
        host: str = "127.0.0.1",
        port: int = 8080,
        workers: int = 16,
        max_queue: int = 256,
        keep_alive_timeout: float = 5.0,
        request_timeout: float = 30.0,
        max_requests_per_connection: int = 1000,
        limits: Limits | None = None,
        backlog: int = 128,
    ):
        """
        :param keep_alive_timeout: max idle wait for the first byte of a request.
        :param request_timeout: max total time to receive one full request
            (headers + body) once it has started; defeats slowloris.
        """
        self.app = app
        self.host = host
        self.port = port
        self.workers = workers
        self.max_queue = max_queue
        self.keep_alive_timeout = keep_alive_timeout
        self.request_timeout = request_timeout
        self.max_requests_per_connection = max_requests_per_connection
        self.limits = limits or Limits()
        self.backlog = backlog

        self.pool: ThreadPool | None = None
        # Writes 503s off the acceptor thread so slow/hostile peers can't stall accept().
        self._reject_pool: ThreadPool | None = None
        self._sock: socket.socket | None = None
        self._shutdown = threading.Event()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._idle_lock = threading.Lock()
        self._idle: set[socket.socket] = set()

    # -- lifecycle -----------------------------------------------------------

    def bind(self) -> None:
        """Create the listening socket. Separate from serve_forever() so that
        callers using port 0 can read the real port before serving."""
        if self._sock is not None:
            return
        sock = socket.socket(socket.AF_INET6 if ":" in self.host else socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                # On Windows SO_REUSEADDR lets other processes steal the port; use the exclusive variant.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(self.backlog)
        except OSError:
            sock.close()  # e.g. port in use: don't leak the socket
            raise
        # A short accept timeout lets the loop notice shutdown requests (and Ctrl+C on Windows).
        sock.settimeout(0.5)
        self.port = sock.getsockname()[1]
        self._sock = sock

    def serve_forever(self) -> None:
        self.bind()
        assert self._sock is not None
        self.pool = ThreadPool(self.workers, self.max_queue, name="http-worker")
        self._reject_pool = ThreadPool(2, max_queue=64, name="http-reject")
        self._ready.set()
        log.info("serving on http://%s:%d with %d workers", self.host, self.port, self.workers)
        try:
            while not self._shutdown.is_set():
                try:
                    conn, addr = self._sock.accept()
                except socket.timeout:
                    continue
                except OSError as e:
                    if self._shutdown.is_set():
                        break
                    # Transient per-connection failures (peer reset before we
                    # accepted, EMFILE when out of file descriptors, ...) must
                    # not take the whole server down. Back off briefly instead.
                    log.warning("accept() failed: %s", e)
                    time.sleep(0.05)
                    continue
                try:
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:  # peer already gone
                    conn.close()
                    continue
                if not self.pool.submit(self._handle_connection, conn, addr):
                    if not self._reject_pool.submit(self._reject_overloaded, conn, addr):
                        conn.close()  # even the reject queue is full: just drop it
        finally:
            self._shutdown.set()  # also covers KeyboardInterrupt escaping accept()
            self._sock.close()
            self._close_idle_connections()
            self._reject_pool.shutdown(wait=True, timeout=2)
            self.pool.shutdown(wait=True, timeout=self.keep_alive_timeout + 1)
            self._stopped.set()
            log.info("server stopped")

    def start_background(self) -> threading.Thread:
        """Bind and serve on a daemon thread (handy for tests and embedding)."""
        self.bind()
        t = threading.Thread(target=self.serve_forever, name="http-acceptor", daemon=True)
        t.start()
        # Return only once the pools exist, so callers can use server.pool safely.
        self._ready.wait()
        return t

    def shutdown(self, timeout: float | None = 10.0) -> None:
        """Stop accepting, close idle keep-alive connections, let in-flight
        requests finish, then return."""
        self._shutdown.set()
        self._close_idle_connections()
        self._stopped.wait(timeout)

    def __enter__(self) -> "HTTPServer":
        self.start_background()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()

    def _close_idle_connections(self) -> None:
        with self._idle_lock:
            for conn in self._idle:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _reject_overloaded(self, conn: socket.socket, addr: tuple) -> None:
        """Runs on the reject pool: the worker queue is full, so shed load."""
        try:
            conn.settimeout(1.0)
            resp = Response.error(503, "server is overloaded, try again shortly")
            resp.headers["Retry-After"] = "1"
            resp.headers["Connection"] = "close"
            self._write_response(conn, resp, None)
            log.warning("rejected %s:%s with 503 (queue full)", *addr[:2])
        except OSError:
            pass
        finally:
            _lingering_close(conn, 0.5)

    # -- per-connection loop (runs on a pool worker) -------------------------

    def _handle_connection(self, conn: socket.socket, addr: tuple) -> None:
        conn.settimeout(self.keep_alive_timeout)
        reader = SocketReader(conn)
        served = 0
        try:
            while not self._shutdown.is_set():
                with self._idle_lock:
                    self._idle.add(conn)
                consumed_before = reader.consumed
                reader.begin_request(self.request_timeout)
                try:
                    request = parse_request(reader, self.limits, addr[:2])
                except ConnectionClosed:
                    return
                except socket.timeout:
                    if reader.has_buffered_data or reader.consumed > consumed_before:  # stalled mid-request
                        self._send_error(conn, HTTPError(408, "request timed out"), addr)
                        _lingering_close(conn, 1.0)
                    return
                except HTTPError as e:
                    self._send_error(conn, e, addr)
                    _lingering_close(conn, 1.0)
                    return
                finally:
                    with self._idle_lock:
                        self._idle.discard(conn)

                served += 1
                start = time.perf_counter()
                response = self._dispatch(request)
                keep_alive = (
                    request.keep_alive
                    and served < self.max_requests_per_connection
                    and not self._shutdown.is_set()
                    and (response.headers.get("Connection") or "").lower() != "close"
                )
                sent, keep_alive = self._write_response(conn, response, request, keep_alive)
                self._log_access(request, response, sent, time.perf_counter() - start)
                if not keep_alive:
                    return
        except (ConnectionError, socket.timeout, OSError) as e:
            log.debug("connection %s:%s dropped: %s", addr[0], addr[1], e)
        except Exception:
            # E.g. a streaming body generator raised mid-response. Headers are
            # already out, so the only honest signal left is to drop the connection.
            log.exception("error while serving %s:%s; closing connection", addr[0], addr[1])
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, request: Request) -> Response:
        try:
            response = self.app(request)
            if not isinstance(response, Response):
                raise TypeError(f"app returned {type(response).__name__}, expected Response")
            return response
        except HTTPError as e:
            return self._error_response(e)
        except Exception:
            log.exception("unhandled error handling %s %s", request.method, request.target)
            return Response.error(500)

    @staticmethod
    def _error_response(e: HTTPError) -> Response:
        resp = Response.error(e.status, e.message)
        for name, value in e.headers.items():
            resp.headers[name] = value
        return resp

    def _send_error(self, conn: socket.socket, e: HTTPError, addr: tuple) -> None:
        resp = self._error_response(e)
        resp.headers["Connection"] = "close"
        try:
            self._write_response(conn, resp, None)
        except OSError:
            pass
        access_log.info('%s - "-" %d (%s)', addr[0], e.status, e.message)

    # -- writing ---------------------------------------------------------------

    def _write_response(
        self,
        conn: socket.socket,
        response: Response,
        request: Request | None,
        keep_alive: bool = False,
    ) -> tuple[int, bool]:
        """Finalize headers, write the response, return (body_bytes, keep_alive)."""
        headers = response.headers
        status = response.status
        body = response.body
        head_only = request is not None and request.method == "HEAD"
        bodiless_status = status < 200 or status in (204, 304)
        http10 = request is not None and request.version == "HTTP/1.0"
        chunked = False

        headers.setdefault("Server", SERVER_NAME)
        headers.setdefault("Date", formatdate(usegmt=True))

        if bodiless_status:
            del headers["Content-Length"]
            del headers["Transfer-Encoding"]
        elif isinstance(body, bytes):
            headers["Content-Length"] = str(len(body))
        elif "Content-Length" not in headers:
            # Streaming body of unknown length.
            if http10:
                keep_alive = False  # length is delimited by closing the connection
            elif not head_only:
                chunked = True
                headers["Transfer-Encoding"] = "chunked"

        if keep_alive:
            if http10:
                headers["Connection"] = "keep-alive"
            headers["Keep-Alive"] = f"timeout={int(self.keep_alive_timeout)}"
        else:
            headers["Connection"] = "close"

        head = response.serialize_head()
        sent = 0
        try:
            if head_only or bodiless_status:
                conn.sendall(head)
            elif isinstance(body, bytes):
                conn.sendall(head + body)
                sent = len(body)
            elif isinstance(body, FileBody):
                conn.sendall(head)
                if body.length:
                    sent = conn.sendfile(body.file, body.offset, body.length)
                if sent != body.length:  # file shrank after stat(): framing is broken
                    log.warning("short file body (%d of %d bytes); closing connection", sent, body.length)
                    keep_alive = False
            else:
                declared = None if chunked else _declared_length(headers)
                conn.sendall(head)
                for chunk in body:
                    if not chunk:
                        continue
                    if chunked:
                        conn.sendall(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    else:
                        if declared is not None and sent + len(chunk) > declared:
                            # Never write past Content-Length: the excess would be
                            # parsed by the client as the start of the next response.
                            chunk = chunk[: declared - sent]
                        conn.sendall(chunk)
                    sent += len(chunk)
                    if declared is not None and sent >= declared:
                        break
                if chunked:
                    conn.sendall(b"0\r\n\r\n")
                elif declared is not None and sent != declared:
                    log.warning("body length %d != Content-Length %d; closing connection", sent, declared)
                    keep_alive = False
        finally:
            response.close()
        return sent, keep_alive

    @staticmethod
    def _log_access(request: Request, response: Response, sent: int, elapsed: float) -> None:
        client = request.client_address[0] if request.client_address else "-"
        access_log.info(
            '%s - "%s %s %s" %d %d %.1fms [%s]',
            client,
            request.method,
            request.target,
            request.version,
            response.status,
            sent,
            elapsed * 1000,
            threading.current_thread().name,
        )


def _declared_length(headers) -> int | None:
    value = headers.get("Content-Length")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _lingering_close(conn: socket.socket, timeout: float, max_bytes: int = 256 * 1024) -> None:
    """Close after an early response without losing it.

    If we close() while unread request bytes sit in the kernel receive buffer
    (or are still arriving), the OS sends a TCP RST that can destroy the
    response before the client reads it. Half-close our side, then drain
    input briefly until the client closes too.
    """
    try:
        conn.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + timeout
        drained = 0
        while drained < max_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            conn.settimeout(remaining)
            data = conn.recv(16384)
            if not data:
                break
            drained += len(data)
    except OSError:
        pass
    finally:
        conn.close()
