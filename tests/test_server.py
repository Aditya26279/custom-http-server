"""End-to-end tests: a real server on an ephemeral port, driven by raw sockets
and http.client."""

import http.client
import json
import os
import select
import shutil
import socket
import tempfile
import threading
import time
import unittest

from rawhttp import HTTPError, HTTPServer, Limits, Response, Router, ThreadPool


def raw(port: int, data: bytes, timeout: float = 3.0) -> bytes:
    """Send raw bytes, read until the server closes the connection."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(data)
        chunks = []
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def status_of(response: bytes) -> int:
    return int(response.split(b" ", 2)[1])


class ServerFixture(unittest.TestCase):
    """Starts one shared server (with a temp static dir) per test class."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        with open(os.path.join(cls.tmp, "index.html"), "w") as f:
            f.write("<h1>home</h1>")
        os.mkdir(os.path.join(cls.tmp, "docs"))
        with open(os.path.join(cls.tmp, "docs", "index.html"), "w") as f:
            f.write("docs")
        with open(os.path.join(cls.tmp, "data.bin"), "wb") as f:
            f.write(bytes(range(256)) * 4)  # 1024 bytes
        with open(os.path.join(cls.tmp, ".secret"), "w") as f:
            f.write("nope")
        os.mkdir(os.path.join(cls.tmp, "café"))
        with open(os.path.join(os.path.dirname(cls.tmp), "outside.txt"), "w") as f:
            f.write("outside")

        app = Router()

        @app.get("/hello/<name>")
        def hello(req, name):
            return Response.text(f"hi {name}")

        @app.get("/items/<int:item_id>")
        def item(req, item_id):
            return {"id": item_id, "type": type(item_id).__name__}

        @app.route("/echo", methods=["POST", "PUT"])
        def echo(req):
            return Response(req.body, content_type="application/octet-stream")

        @app.get("/boom")
        def boom(req):
            raise RuntimeError("kaboom")

        @app.get("/teapot")
        def teapot(req):
            raise HTTPError(403, "no tea")

        @app.get("/stream")
        def stream(req):
            return Response(iter([b"one,", b"two,", b"three"]))

        @app.get("/sleep")
        def sleep(req):
            time.sleep(float(req.arg("s", "0.5")))
            return {"worker": threading.current_thread().name}

        @app.get("/broken-stream")
        def broken_stream(req):
            def gen():
                yield b"partial"
                raise RuntimeError("generator failed")
            return Response(gen())

        @app.get("/overrun")
        def overrun(req):  # declares 3 bytes, yields 6
            return Response(iter([b"abc", b"def"]), headers={"Content-Length": "3"})

        @app.get("/short-file")
        def short_file(req):  # simulates a file truncated after stat()
            from rawhttp.static import FileBody
            f = open(os.path.join(cls.tmp, "data.bin"), "rb")
            return Response(FileBody(f, 0, 2000, 4096), headers={"Content-Length": "2000"})

        @app.get("/bad-header")
        def bad_header(req):
            return Response("x", headers={"X-Name": "café ☃"})

        app.mount_static("/static", cls.tmp)
        app.mount_static("/", cls.tmp)

        cls.server = HTTPServer(
            app, "127.0.0.1", 0, workers=8, keep_alive_timeout=2, request_timeout=3,
            limits=Limits(max_body_bytes=4096),
        )
        cls.server.start_background()
        cls.port = cls.server.port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        try:
            os.remove(os.path.join(os.path.dirname(cls.tmp), "outside.txt"))
        except OSError:
            pass

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()


class ServerTestCase(ServerFixture):
    # -- routing -------------------------------------------------------------

    def test_route_with_param(self):
        status, headers, body = self.request("GET", "/hello/world")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hi world")
        self.assertEqual(headers["Server"], "rawhttp/1.0")

    def test_int_converter(self):
        status, _, body = self.request("GET", "/items/42")
        self.assertEqual(json.loads(body), {"id": 42, "type": "int"})
        self.assertEqual(self.request("GET", "/items/abc")[0], 404)

    def test_percent_decoded_param(self):
        self.assertEqual(self.request("GET", "/hello/a%20b")[2], b"hi a b")

    def test_404_and_405(self):
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        status, headers, _ = self.request("DELETE", "/echo")
        self.assertEqual(status, 405)
        self.assertIn("POST", headers["Allow"])

    def test_options(self):
        status, headers, _ = self.request("OPTIONS", "/echo")
        self.assertEqual(status, 204)
        # GET/HEAD come from the catch-all "/" static mount.
        self.assertEqual(headers["Allow"], "GET, HEAD, OPTIONS, POST, PUT")

    def test_handler_errors(self):
        self.assertEqual(self.request("GET", "/boom")[0], 500)
        status, _, body = self.request("GET", "/teapot")
        self.assertEqual(status, 403)
        self.assertIn(b"no tea", body)

    def test_head_has_no_body(self):
        status, headers, body = self.request("HEAD", "/hello/x")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], "4")
        self.assertEqual(body, b"")

    # -- bodies ----------------------------------------------------------------

    def test_post_content_length(self):
        status, _, body = self.request("POST", "/echo", body=b"payload")
        self.assertEqual((status, body), (200, b"payload"))

    def test_chunked_request_body(self):
        req = (
            b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            b"5\r\nhello\r\n7;ext=1\r\n, world\r\n0\r\nTrailer: x\r\n\r\n"
        )
        resp = raw(self.port, req)
        self.assertEqual(status_of(resp), 200)
        self.assertTrue(resp.endswith(b"hello, world"))

    def test_chunked_response(self):
        status, headers, body = self.request("GET", "/stream")
        self.assertEqual(headers.get("Transfer-Encoding"), "chunked")
        self.assertEqual(body, b"one,two,three")

    def test_stream_to_http10_closes_connection(self):
        resp = raw(self.port, b"GET /stream HTTP/1.0\r\n\r\n")
        self.assertIn(b"Connection: close", resp)
        self.assertNotIn(b"Transfer-Encoding", resp)
        self.assertTrue(resp.endswith(b"one,two,three"))

    def test_body_too_large(self):
        self.assertEqual(self.request("POST", "/echo", body=b"x" * 5000)[0], 413)

    def test_expect_100_continue(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            s.sendall(b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n"
                      b"Expect: 100-continue\r\nConnection: close\r\n\r\n")
            self.assertEqual(s.recv(1024), b"HTTP/1.1 100 Continue\r\n\r\n")
            s.sendall(b"abc")
            self.assertTrue(s.recv(4096).endswith(b"abc"))

    # -- keep-alive / pipelining ---------------------------------------------

    def test_keep_alive_reuses_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        for name in ("a", "b", "c"):
            conn.request("GET", f"/hello/{name}")
            resp = conn.getresponse()
            self.assertEqual(resp.read(), f"hi {name}".encode())
            self.assertEqual(resp.getheader("Connection"), None)  # HTTP/1.1 default keep-alive
        conn.close()

    def test_pipelined_requests(self):
        req = b"GET /hello/1 HTTP/1.1\r\nHost: x\r\n\r\n"
        last = b"GET /hello/2 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        resp = raw(self.port, req + last)
        self.assertEqual(resp.count(b"HTTP/1.1 200 OK"), 2)
        self.assertIn(b"hi 1", resp)
        self.assertTrue(resp.endswith(b"hi 2"))

    # -- malformed input -------------------------------------------------------

    def test_malformed_requests(self):
        cases = {
            b"GARBAGE\r\n\r\n": 400,
            b"GET / HTTP/1.1\r\n\r\n": 400,  # missing Host
            b"GET / HTTP/2.0\r\nHost: x\r\n\r\n": 505,
            b"GET / HTTP/1.1\r\nHost: x\r\nBad Header: y\r\n\r\n": 400,
            b"GET / HTTP/1.1\r\nHost : x\r\n\r\n": 400,
            b"GET nope HTTP/1.1\r\nHost: x\r\n\r\n": 400,
            b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\nContent-Length: 4\r\n\r\nabcd": 400,
            b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\nTransfer-Encoding: chunked\r\n\r\n": 400,
            b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip\r\n\r\n": 501,
            b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\nHost: x\r\n\r\n": 414,
            b"GET / HTTP/1.1\r\nHost: x\r\nX: " + b"a" * 9000 + b"\r\n\r\n": 431,
        }
        for req, expected in cases.items():
            with self.subTest(req=req[:60]):
                self.assertEqual(status_of(raw(self.port, req)), expected)

    def test_idle_connection_closed_silently(self):
        start = time.monotonic()
        self.assertEqual(raw(self.port, b"", timeout=5), b"")
        self.assertLess(time.monotonic() - start, 4)

    def test_partial_request_times_out_with_408(self):
        resp = raw(self.port, b"GET / HTTP/1.1\r\nHost: x\r\n", timeout=5)
        self.assertEqual(status_of(resp), 408)

    # -- static files ------------------------------------------------------------

    def test_static_index_and_mime(self):
        status, headers, body = self.request("GET", "/static/")
        self.assertEqual((status, body), (200, b"<h1>home</h1>"))
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")

    def test_static_directory_redirect(self):
        status, headers, _ = self.request("GET", "/static/docs")
        self.assertEqual(status, 301)
        self.assertEqual(headers["Location"], "/static/docs/")
        self.assertEqual(self.request("GET", "/static")[0], 301)

    def test_static_binary_file(self):
        status, headers, body = self.request("GET", "/static/data.bin")
        self.assertEqual(status, 200)
        self.assertEqual(body, bytes(range(256)) * 4)
        self.assertEqual(headers["Content-Length"], "1024")

    def test_static_path_traversal_blocked(self):
        for path in ("/static/../outside.txt", "/static/%2e%2e/outside.txt",
                     "/static/..%2foutside.txt", "/static/..%5coutside.txt",
                     "/static/.secret", "/static/C:%5cWindows%5cwin.ini"):
            with self.subTest(path=path):
                resp = raw(self.port, f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
                self.assertEqual(status_of(resp), 404)
                self.assertNotIn(b"outside", resp.split(b"\r\n\r\n", 1)[1])

    def test_static_conditional_get(self):
        _, headers, _ = self.request("GET", "/static/data.bin")
        etag, last_mod = headers["ETag"], headers["Last-Modified"]
        status, _, body = self.request("GET", "/static/data.bin", headers={"If-None-Match": etag})
        self.assertEqual((status, body), (304, b""))
        status, _, _ = self.request("GET", "/static/data.bin", headers={"If-Modified-Since": last_mod})
        self.assertEqual(status, 304)
        status, _, _ = self.request("GET", "/static/data.bin", headers={"If-None-Match": '"other"'})
        self.assertEqual(status, 200)

    def test_static_range(self):
        status, headers, body = self.request("GET", "/static/data.bin", headers={"Range": "bytes=10-19"})
        self.assertEqual(status, 206)
        self.assertEqual(body, bytes(range(10, 20)))
        self.assertEqual(headers["Content-Range"], "bytes 10-19/1024")
        status, _, body = self.request("GET", "/static/data.bin", headers={"Range": "bytes=-4"})
        self.assertEqual(body, bytes([252, 253, 254, 255]))
        status, headers, _ = self.request("GET", "/static/data.bin", headers={"Range": "bytes=5000-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */1024")

    def test_static_missing(self):
        self.assertEqual(self.request("GET", "/static/missing.txt")[0], 404)

    # -- concurrency -----------------------------------------------------------

    def test_requests_run_concurrently(self):
        results = []

        def hit():
            results.append(json.loads(self.request("GET", "/sleep?s=0.5")[2])["worker"])

        threads = [threading.Thread(target=hit) for _ in range(6)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - start
        self.assertEqual(len(results), 6)
        self.assertLess(elapsed, 1.5, "6 x 0.5s requests should overlap on 8 workers")
        self.assertGreater(len(set(results)), 1)


class SecurityRegressionTest(ServerFixture):
    """Each test reproduces a bug found in review."""

    def get(self, target: bytes, extra: bytes = b"") -> bytes:
        return raw(self.port, b"GET " + target + b" HTTP/1.1\r\nHost: x\r\nConnection: close\r\n" + extra + b"\r\n")

    def test_bad_requests_get_a_response_not_a_dropped_socket(self):
        cases = {
            # Previously crashed the worker (no response at all):
            b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: \xb2\r\n\r\nab": 400,
            b"GET /%ff HTTP/1.1\r\nHost: x\r\n\r\n": 400,
            # Previously 500 (control chars reached the filesystem):
            b"GET /static/\x1b[31m HTTP/1.1\r\nHost: x\r\n\r\n": 400,
            b"GET /static/%1b HTTP/1.1\r\nHost: x\r\n\r\n": 400,
            # Request smuggling vectors, previously accepted:
            b"POST /echo HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
            b"Transfer-Encoding: gzip\r\n\r\n0\r\n\r\n": 501,
            b"POST /echo HTTP/1.0\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n": 400,
            b"GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n": 400,
            b"GET / HTTP/1.1\r\nHost: a/b\r\n\r\n": 400,
            b"\r\n" * 20 + b"GET / HTTP/1.1\r\nHost: x\r\n\r\n": 400,
        }
        for req, expected in cases.items():
            with self.subTest(req=req[:70]):
                self.assertEqual(status_of(raw(self.port, req)), expected)

    def test_valid_hosts_accepted(self):
        for host in (b"example.com", b"127.0.0.1:8080", b"[::1]:80", b"localhost"):
            with self.subTest(host=host):
                resp = raw(self.port, b"GET /hello/x HTTP/1.1\r\nHost: " + host + b"\r\nConnection: close\r\n\r\n")
                self.assertEqual(status_of(resp), 200)

    def test_no_open_redirect_via_double_slash(self):
        resp = self.get(b"//docs")
        self.assertEqual(status_of(resp), 301)
        self.assertIn(b"\r\nLocation: /docs/\r\n", resp)

    def test_redirect_with_non_ascii_path_is_percent_encoded(self):
        resp = self.get(b"/static/caf%C3%A9")
        self.assertEqual(status_of(resp), 301)
        self.assertIn(b"\r\nLocation: /static/caf%C3%A9/\r\n", resp)

    def test_windows_device_names_blocked(self):
        for name in (b"CON", b"nul.txt", b"com1.log", b"AUX"):
            with self.subTest(name=name):
                self.assertEqual(status_of(self.get(b"/static/" + name)), 404)

    def test_backwards_range_is_ignored(self):
        resp = self.get(b"/static/data.bin", b"Range: bytes=10-5\r\n")
        self.assertEqual(status_of(resp), 200)

    def test_options_star(self):
        resp = raw(self.port, b"OPTIONS * HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertEqual(status_of(resp), 204)
        self.assertIn(b"Allow: ", resp)

    def test_slowloris_is_cut_off(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
        start = time.monotonic()
        # Drip faster than keep_alive_timeout until the server answers. (Stop as
        # soon as it does: bytes sent after its close would trigger a TCP RST
        # that discards the unread 408.)
        while time.monotonic() - start < 8:
            if select.select([s], [], [], 0.5)[0]:
                break
            s.sendall(b"X")
        resp = b""
        try:
            while chunk := s.recv(4096):
                resp += chunk
        except OSError:
            pass
        s.close()
        self.assertLess(time.monotonic() - start, 6, "request_timeout=3 should end the drip")
        self.assertIn(b" 408 ", resp)

    def test_stream_error_truncates_and_closes(self):
        resp = self.get(b"/broken-stream")
        self.assertIn(b"partial", resp)
        self.assertFalse(resp.endswith(b"0\r\n\r\n"), "must not look like a complete chunked body")

    def test_body_never_exceeds_content_length(self):
        first = b"GET /overrun HTTP/1.1\r\nHost: x\r\n\r\n"
        second = b"GET /hello/z HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        resp = raw(self.port, first + second)
        body1, rest = resp.split(b"\r\n\r\n", 1)[1][:3], resp.split(b"\r\n\r\n", 1)[1][3:]
        self.assertEqual(body1, b"abc")
        self.assertTrue(rest.startswith(b"HTTP/1.1 200 OK"), rest[:40])

    def test_short_file_closes_instead_of_hanging(self):
        start = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as s:
            s.sendall(b"GET /short-file HTTP/1.1\r\nHost: x\r\n\r\n")  # keep-alive
            data = b""
            while chunk := s.recv(65536):
                data += chunk
        self.assertLess(time.monotonic() - start, 1.5, "server should close, not idle out")
        self.assertEqual(len(data.split(b"\r\n\r\n", 1)[1]), 1024)

    def test_unencodable_header_is_clean_500(self):
        self.assertEqual(status_of(self.get(b"/bad-header")), 500)


class DemoAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as demo
        cls.server = HTTPServer(demo.app, "127.0.0.1", 0, workers=4)
        cls.server.start_background()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def get_status(self, path, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request(method, path, body=body)
        status = conn.getresponse().status
        conn.close()
        return status

    def test_slow_validates_input(self):
        for q in ("nan", "-1", "abc", "11", "inf"):
            with self.subTest(q=q):
                self.assertEqual(self.get_status(f"/api/slow?seconds={q}"), 400)
        self.assertEqual(self.get_status("/api/slow?seconds=0"), 200)

    def test_create_user_validates_name(self):
        self.assertEqual(self.get_status("/api/users", "POST", json.dumps({"name": "x" * 101})), 422)
        self.assertEqual(self.get_status("/api/users", "POST", json.dumps({"name": "  "})), 422)
        self.assertEqual(self.get_status("/api/users", "POST", json.dumps({"name": "Grace"})), 201)

    def test_deeply_nested_json_is_400(self):
        # 20 KB of valid JSON nested past the recursion limit: used to be a 500.
        self.assertEqual(self.get_status("/api/users", "POST", "[" * 10_000 + "]" * 10_000), 400)


class OverloadTest(unittest.TestCase):
    def test_full_queue_returns_503(self):
        app = Router()
        release = threading.Event()

        @app.get("/block")
        def block(req):
            release.wait(5)
            return "done"

        server = HTTPServer(app, "127.0.0.1", 0, workers=1, max_queue=1)
        server.start_background()
        try:
            busy = []

            def wait_for(key, value):
                deadline = time.monotonic() + 3
                while server.pool.stats()[key] != value:
                    self.assertLess(time.monotonic(), deadline, f"pool never reached {key}={value}")
                    time.sleep(0.01)

            # One connection occupies the only worker, the next fills the queue.
            # Wait for each step: opening both at once races the worker's dequeue.
            for key in ("active", "queued"):
                s = socket.create_connection(("127.0.0.1", server.port))
                s.sendall(b"GET /block HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                busy.append(s)
                wait_for(key, 1)
            resp = raw(server.port, b"GET /block HTTP/1.1\r\nHost: x\r\n\r\n")
            self.assertEqual(status_of(resp), 503)
            self.assertIn(b"Retry-After: 1", resp)
        finally:
            release.set()
            for s in busy:
                s.close()
            server.shutdown()


class ThreadPoolTest(unittest.TestCase):
    def test_runs_all_tasks_and_drains_on_shutdown(self):
        pool = ThreadPool(4)
        done = []
        lock = threading.Lock()

        def task(i):
            time.sleep(0.01)
            with lock:
                done.append(i)

        for i in range(50):
            self.assertTrue(pool.submit(task, i))
        pool.shutdown(wait=True)
        self.assertEqual(sorted(done), list(range(50)))
        self.assertEqual(pool.stats()["completed"], 50)

    def test_bounded_queue_rejects(self):
        pool = ThreadPool(1, max_queue=1)
        gate = threading.Event()
        pool.submit(gate.wait)
        time.sleep(0.05)  # let the worker pick it up
        self.assertTrue(pool.submit(gate.wait))
        self.assertFalse(pool.submit(gate.wait))
        gate.set()
        pool.shutdown()

    def test_task_exception_does_not_kill_worker(self):
        pool = ThreadPool(1)
        ran = threading.Event()
        pool.submit(lambda: 1 / 0)
        pool.submit(ran.set)
        self.assertTrue(ran.wait(2))
        pool.shutdown()


if __name__ == "__main__":
    unittest.main()
