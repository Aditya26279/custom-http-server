# rawhttp

An HTTP/1.1 server built from scratch in pure Python, on top of **raw sockets**.

rawhttp parses requests straight off the socket, routes them to handlers, serves static files and runs everything on a hand-written thread pool. It does not use `http.server`, `socketserver`, `concurrent.futures` or `queue`. The only standard-library helpers are generic ones: `urllib.parse` for percent-decoding, `mimetypes`, `email.utils` for HTTP dates, and `json`.

It has no dependencies and is a learning project. It isn't meant to replace a production server (see [Limitations](#limitations)).

## Features

- **HTTP/1.1 request parsing** over raw sockets: `Content-Length` and chunked bodies, `Expect: 100-continue`, keep-alive, and pipelining
- **Routing** with typed path parameters (`/users/<int:id>`), automatic `HEAD` and `OPTIONS`, and correct `404` / `405` + `Allow` responses
- **Static files** with MIME types, `ETag` / `Last-Modified` caching (`304`), byte ranges (`206`), `index.html` for directories, and `sendfile()`
- **Thread pool** with a bounded queue. When it's full, the server sheds load with `503 Retry-After` instead of falling over
- **Streaming responses** with chunked transfer encoding
- **Hardened** against path traversal, request smuggling, slowloris, header injection and open redirects (see [Security](#security))
- **Graceful shutdown**: in-flight requests finish, idle connections close
- **45 end-to-end tests** that drive a real server over real sockets

## Requirements

- Python **3.10+**
- No third-party packages

## Quick start

```bash
git clone <your-repo-url> rawhttp
cd rawhttp
python app.py --port 8080
```

Open <http://127.0.0.1:8080> for the demo page. It has buttons that call the API, including one that fires five 1-second requests at once so you can watch the thread pool run them in parallel.

To serve any folder as a static site:

```bash
python -m rawhttp --dir ./public --port 8080 --workers 16
```

| Option | Default | Description |
|---|---|---|
| `--host` | `127.0.0.1` | Interface to bind. Use `0.0.0.0` to accept connections from other machines |
| `--port` | `8080` | TCP port |
| `--workers` | `16` | Number of worker threads |
| `--dir` | `.` | Directory to serve (`python -m rawhttp` only) |

Press `Ctrl+C` to stop the server gracefully.

## Demo API

`app.py` serves these endpoints, with the static site in `public/` as a fallback:

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/hello?name=Ada` | Greeting as JSON |
| `GET` | `/api/users` | List users |
| `GET` | `/api/users/<id>` | One user, or `404` |
| `POST` | `/api/users` | Create a user from `{"name": "..."}` (1–100 characters). Returns `201` + `Location` |
| `GET` `POST` `PUT` | `/api/echo` | Echoes the method, path, query, headers, body and the worker thread that handled it |
| `GET` | `/api/slow?seconds=1` | Sleeps 0–10 s. Use it to see concurrency |
| `GET` | `/api/stream` | Streams five chunks with chunked encoding |
| `GET` | `/api/stats` | Thread pool stats: workers, active, queued, completed |

```bash
curl -i http://127.0.0.1:8080/api/hello?name=curl
```

```bash
curl -i -X POST -H "Content-Type: application/json" -d '{"name":"Grace"}' http://127.0.0.1:8080/api/users
```

```bash
curl -N http://127.0.0.1:8080/api/stream
```

## Using it as a library

```python
from rawhttp import HTTPError, HTTPServer, Response, Router

app = Router()

@app.get("/users/<int:user_id>")
def get_user(req, user_id):
    if user_id != 1:
        raise HTTPError(404, "no such user")
    return {"id": user_id}                      # dict / list  -> JSON

@app.post("/echo")
def echo(req):
    return Response(req.body, content_type=req.content_type or "text/plain")

@app.get("/created")
def created(req):
    return "made it", 201, {"X-Extra": "1"}     # (body, status, headers)

@app.get("/numbers")
def numbers(req):
    return Response((f"{i}\n".encode() for i in range(5)))  # streamed, chunked

app.mount_static("/", "public")                 # mount last so API routes win

HTTPServer(app, "0.0.0.0", 8080, workers=16).serve_forever()
```

**Routes:** `@app.get`, `@app.post`, `@app.put`, `@app.delete`, or `@app.route(pattern, methods=[...])`.

**Path converters:** `<name>` (one segment), `<int:n>`, `<float:x>`, `<path:p>` (may contain `/`).

**Request:** `req.method`, `req.path`, `req.query`, `req.arg("k", default)`, `req.headers`, `req.body`, `req.text`, `req.json()`, `req.form()`, `req.client_address`.

**Return values:**

| Handler returns | Response |
|---|---|
| `Response(...)` | As-is (`Response.text/html/json/redirect/error` helpers available) |
| `str` | `text/html` |
| `bytes` | `application/octet-stream` |
| `dict` / `list` | JSON |
| `None` | `204 No Content` |
| `(body, status)` / `(body, status, headers)` | Body converted as above |
| Generator / iterable of `bytes` | Streamed: chunked on HTTP/1.1, close-delimited on HTTP/1.0 |

Raise `HTTPError(status, message, headers)` from anywhere to send an error page. Any other exception becomes a logged `500`.

For tests or embedding, `server.start_background()` serves on a daemon thread and returns once the server is ready. `server.shutdown()` stops it. Pass `port=0` to get a free port, which you can then read from `server.port`.

## Configuration

`HTTPServer(app, host, port, ...)`:

| Parameter | Default | Description |
|---|---|---|
| `workers` | `16` | Worker threads |
| `max_queue` | `256` | Connections waiting for a worker. Beyond this, clients get `503` |
| `keep_alive_timeout` | `5.0` | Seconds an idle connection may wait for its next request |
| `request_timeout` | `30.0` | Seconds to receive one full request once it has started (anti-slowloris) |
| `max_requests_per_connection` | `1000` | Close a keep-alive connection after this many requests |
| `limits` | `Limits()` | Request size limits, see below |
| `backlog` | `128` | `listen()` backlog |

`Limits(...)`:

| Field | Default | Exceeded → |
|---|---|---|
| `max_request_line` | 8190 bytes | `414` |
| `max_header_line` | 8190 bytes | `431` |
| `max_header_count` | 100 | `431` |
| `max_header_bytes` | 64 KiB | `431` |
| `max_body_bytes` | 10 MiB | `413` |

`app.mount_static(prefix, directory, ...)` options: `index="index.html"`, `max_age=0` (seconds of `Cache-Control: max-age`, `0` means `no-cache`), `allow_dotfiles=False`, `chunk_size=65536`.

## Project structure

```
rawhttp/
  server.py       accept loop, per-connection keep-alive loop, response writer
  request.py      buffered socket reader + HTTP/1.x request parser
  response.py     Response object, status codes, serialization
  headers.py      case-insensitive, multi-value header container
  router.py       path patterns with typed params, 404 / 405 / OPTIONS
  static.py       static files: traversal-safe, MIME, ETag/304, Range/206
  threadpool.py   fixed worker pool on Condition + deque, bounded queue
  errors.py       HTTPError and internal exceptions
  __main__.py     `python -m rawhttp` static file server CLI
app.py            demo application (JSON API + static site)
public/           demo static site
tests/            end-to-end tests over real sockets
```

## How it works

```
             +--------------------+   submit(conn)    +----------------------+
 clients --> | acceptor thread    | ----------------> | bounded task queue   |
             | accept() loop      |   queue full?     +----------+-----------+
             +--------------------+   -> 503 (reject             |
                                         pool) + close     N worker threads
                                                                  |
                     +--------------------------------------------v-------+
                     | per connection: loop { parse_request -> router ->  |
                     |   handler -> write response } while keep-alive     |
                     +----------------------------------------------------+
```

1. **Accept:** one thread runs `accept()` and hands each connection to the pool. If the queue is full, a small separate pool sends `503 Retry-After`, so a slow client can't stall `accept()`.
2. **Parse** (`request.py`): a `SocketReader` buffers `recv()` data and reads line by line with hard length limits, keeping leftover bytes for pipelined requests. The parser checks the request line and headers, then reads the body by `Content-Length` or chunked encoding.
3. **Route** (`router.py`): patterns compile to regexes. If a path matches but the method doesn't, the router returns `405` with an `Allow` header.
4. **Respond** (`server.py`): the server sets the framing headers (`Content-Length`, chunked, or close) and keep-alive headers, then writes the response, using `sendfile()` for files. The connection loops for the next request until it closes or times out.

## Security

| Threat | Defense |
|---|---|
| Path traversal | Paths are resolved with `realpath` and must stay under the root. Blocks `..`, `%2e%2e`, `%5c`, drive letters, outbound symlinks and dotfiles |
| Windows device names | `CON`, `NUL`, `COM1`, `nul.txt` and names with trailing dots or spaces are refused |
| Request smuggling | Rejects `Transfer-Encoding` together with `Content-Length`, conflicting or non-ASCII `Content-Length`, repeated `Transfer-Encoding` headers, `Transfer-Encoding` on HTTP/1.0, and duplicate or invalid `Host` |
| Slowloris | Per-request deadline (`request_timeout`) on top of the idle timeout. Stalled clients get `408` |
| Oversized input | Line, header and body limits (`413` / `414` / `431`), and a cap on blank lines before the request |
| Header injection | CR, LF and NUL, and non-Latin-1 header values, are rejected when the header is set |
| Open redirect | Directory redirects collapse leading slashes, so `//evil.com` can't become a `Location` |
| Response desync | A body never goes past `Content-Length`. If a body comes up short, the connection is closed, not reused |
| Lost error responses | Lingering close: the server half-closes and drains input, so a TCP RST can't wipe out the response |
| MIME sniffing | Static files are sent with `X-Content-Type-Options: nosniff` |

## Testing

```bash
python -m unittest discover -s tests -t . -v
```

The suite starts real servers on free ports and covers routing, request bodies, keep-alive and pipelining, malformed input, static files, concurrency, overload (`503`), the thread pool, and a regression test for each security fix.

## Limitations

- **Each open connection occupies a worker**, including idle keep-alive time (bounded by `keep_alive_timeout`). With 16 workers, the 17th concurrent keep-alive client waits in the queue. A slow *reader* can also hold a worker for as long as a download takes. The next step would be a `selectors`-based event loop that hands only ready requests to the pool.
- No TLS, HTTP/2, multipart form parsing or compression. Put it behind a reverse proxy for anything public.
- Python's GIL caps CPU-bound throughput. I/O-bound handlers still scale across workers. A local load test reached about 4,500–5,000 req/s on small keep-alive responses.
