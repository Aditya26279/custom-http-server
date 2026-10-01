"""URL routing with typed path parameters.

    router = Router()

    @router.get("/users/<int:user_id>")
    def show_user(req, user_id): ...

Supported converters: ``str`` (default, one segment), ``int``, ``float`` and
``path`` (may contain slashes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .errors import HTTPError
from .request import Request
from .response import Response

Handler = Callable[..., Any]

_PARAM_RE = re.compile(r"<(?:(?P<conv>[a-z]+):)?(?P<name>[A-Za-z_]\w*)>")
_CONVERTERS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "str": (r"[^/]+", str),
    # [0-9], not \d: \d also matches non-ASCII digits such as Arabic-Indic.
    "int": (r"[0-9]+", int),
    "float": (r"[0-9]+(?:\.[0-9]+)?", float),
    "path": (r".*", str),
}


@dataclass
class Route:
    pattern: str
    regex: re.Pattern[str]
    converters: dict[str, Callable[[str], Any]]
    methods: frozenset[str]
    handler: Handler

    def match(self, path: str) -> dict[str, Any] | None:
        m = self.regex.fullmatch(path)
        if not m:
            return None
        try:
            return {k: self.converters[k](v) for k, v in m.groupdict().items()}
        except ValueError:
            return None


def compile_pattern(pattern: str) -> tuple[re.Pattern[str], dict[str, Callable[[str], Any]]]:
    if not pattern.startswith("/"):
        raise ValueError(f"route pattern must start with '/': {pattern!r}")
    regex, converters, pos = "", {}, 0
    for m in _PARAM_RE.finditer(pattern):
        conv = m.group("conv") or "str"
        if conv not in _CONVERTERS:
            raise ValueError(f"unknown converter {conv!r} in {pattern!r}")
        name = m.group("name")
        if name in converters:
            raise ValueError(f"duplicate parameter {name!r} in {pattern!r}")
        part_re, fn = _CONVERTERS[conv]
        regex += re.escape(pattern[pos : m.start()]) + f"(?P<{name}>{part_re})"
        converters[name] = fn
        pos = m.end()
    regex += re.escape(pattern[pos:])
    return re.compile(regex), converters


def to_response(result: Any) -> Response:
    """Normalize whatever a handler returned into a Response."""
    if isinstance(result, Response):
        return result
    if result is None:
        return Response(b"", 204)
    if isinstance(result, tuple):
        body, status, *rest = result
        resp = to_response(body)
        resp.status = status
        for name, value in (rest[0] if rest else {}).items():
            resp.headers[name] = value
        return resp
    if isinstance(result, str):
        return Response.html(result)
    if isinstance(result, (bytes, bytearray)):
        return Response(bytes(result), content_type="application/octet-stream")
    if isinstance(result, (dict, list)):
        return Response.json(result)
    raise TypeError(f"handler returned unsupported type {type(result).__name__}")


class Router:
    def __init__(self) -> None:
        self.routes: list[Route] = []

    def add_route(self, pattern: str, handler: Handler, methods: Iterable[str] = ("GET",)) -> None:
        regex, converters = compile_pattern(pattern)
        methods = frozenset(m.upper() for m in methods)
        self.routes.append(Route(pattern, regex, converters, methods, handler))

    def route(self, pattern: str, methods: Iterable[str] = ("GET",)) -> Callable[[Handler], Handler]:
        def decorator(fn: Handler) -> Handler:
            self.add_route(pattern, fn, methods)
            return fn

        return decorator

    def get(self, pattern: str) -> Callable[[Handler], Handler]:
        return self.route(pattern, ["GET"])

    def post(self, pattern: str) -> Callable[[Handler], Handler]:
        return self.route(pattern, ["POST"])

    def put(self, pattern: str) -> Callable[[Handler], Handler]:
        return self.route(pattern, ["PUT"])

    def delete(self, pattern: str) -> Callable[[Handler], Handler]:
        return self.route(pattern, ["DELETE"])

    def mount_static(self, prefix: str, directory: str, **options: Any) -> None:
        """Serve files from ``directory`` under URL ``prefix``."""
        from .static import StaticFiles  # local import avoids a cycle

        handler = StaticFiles(directory, **options)
        prefix = "/" + prefix.strip("/")
        methods = ("GET", "HEAD")
        if prefix == "/":
            self.add_route("/<path:filepath>", handler, methods)
        else:
            self.add_route(prefix, lambda req: Response.redirect(prefix + "/", 301), methods)
            self.add_route(prefix + "/<path:filepath>", handler, methods)

    def __call__(self, request: Request) -> Response:
        """Dispatch a request: 404 if no pattern matches, 405 if the path
        matches but the method does not."""
        if request.path == "*":  # "OPTIONS * HTTP/1.1": server-wide capabilities
            methods = {m for r in self.routes for m in r.methods}
            if "GET" in methods:
                methods.add("HEAD")
            return Response(b"", 204, headers={"Allow": ", ".join(sorted(methods | {"OPTIONS"}))})

        allowed: set[str] = set()
        for route in self.routes:
            params = route.match(request.path)
            if params is None:
                continue
            methods = set(route.methods)
            if "GET" in methods:
                methods.add("HEAD")  # HEAD is GET without a body; server strips it.
            if request.method in methods:
                request.path_params = params
                return to_response(route.handler(request, **params))
            allowed |= methods

        if not allowed:
            raise HTTPError(404, f"no route for {request.path}")
        allow = ", ".join(sorted(allowed | {"OPTIONS"}))
        if request.method == "OPTIONS":
            return Response(b"", 204, headers={"Allow": allow})
        raise HTTPError(405, f"{request.method} not allowed", headers={"Allow": allow})
