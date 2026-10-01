"""Demo application: JSON API routes + static site from ./public.

    python app.py --port 8080
"""

import argparse
import logging
import os
import threading
import time

from rawhttp import HTTPError, HTTPServer, Response, Router

app = Router()
server: HTTPServer | None = None

USERS = {1: {"id": 1, "name": "Ada"}, 2: {"id": 2, "name": "Linus"}}
_users_lock = threading.Lock()
MAX_USERS = 1000
MAX_NAME_LENGTH = 100


@app.get("/api/hello")
def hello(req):
    return {"message": f"Hello, {req.arg('name', 'world')}!"}


@app.get("/api/users")
def list_users(req):
    with _users_lock:
        return list(USERS.values())


@app.get("/api/users/<int:user_id>")
def get_user(req, user_id):
    with _users_lock:
        user = USERS.get(user_id)
    if user is None:
        raise HTTPError(404, f"user {user_id} not found")
    return user


@app.post("/api/users")
def create_user(req):
    data = req.json()
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise HTTPError(422, "expected JSON object with a string 'name'")
    name = data["name"].strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise HTTPError(422, f"'name' must be 1-{MAX_NAME_LENGTH} characters")
    with _users_lock:
        if len(USERS) >= MAX_USERS:  # in-memory store: cap it so clients can't exhaust RAM
            raise HTTPError(429, "user limit reached")
        uid = max(USERS, default=0) + 1
        USERS[uid] = {"id": uid, "name": name}
        user = USERS[uid]
    return user, 201, {"Location": f"/api/users/{uid}"}


@app.route("/api/echo", methods=["GET", "POST", "PUT"])
def echo(req):
    return {
        "method": req.method,
        "path": req.path,
        "query": req.query,
        "headers": dict(req.headers.items()),
        "body": req.text,
        "worker": threading.current_thread().name,
    }


@app.get("/api/slow")
def slow(req):
    """Sleeps to demonstrate that one slow request doesn't block others."""
    try:
        seconds = float(req.arg("seconds", "1"))
    except ValueError:
        raise HTTPError(400, "'seconds' must be a number") from None
    if not 0 <= seconds <= 10:  # also rejects nan (all comparisons with nan are False)
        raise HTTPError(400, "'seconds' must be between 0 and 10")
    time.sleep(seconds)
    return {"slept": seconds, "worker": threading.current_thread().name}


@app.get("/api/stream")
def stream(req):
    """Body of unknown length: sent with chunked transfer encoding."""
    def numbers():
        for i in range(1, 6):
            yield f"chunk {i}\n".encode()
            time.sleep(0.2)
    return Response(numbers(), content_type="text/plain; charset=utf-8")


@app.get("/api/stats")
def stats(req):
    return server.pool.stats() if server and server.pool else {}


# Static files last so API routes take precedence.
app.mount_static("/", os.path.join(os.path.dirname(os.path.abspath(__file__)), "public"))


def main() -> None:
    global server
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = HTTPServer(app, args.host, args.port, workers=args.workers)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
