"""Case-insensitive, order-preserving, multi-valued HTTP header container."""

from __future__ import annotations

from typing import Iterable, Iterator, Mapping


class Headers:
    """Stores header fields as (name, value) pairs.

    Lookups are case-insensitive (``Content-Type`` == ``content-type``), the
    original casing and order are preserved for serialization, and repeated
    fields (e.g. multiple ``Set-Cookie``) are supported via :meth:`add`.
    """

    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, str] | Iterable[tuple[str, str]] | None = None):
        self._items: list[tuple[str, str]] = []
        if items is None:
            return
        pairs = items.items() if isinstance(items, Mapping) else items
        for name, value in pairs:
            self.add(name, value)

    @staticmethod
    def _check(name: str, value: str) -> None:
        # Guard against response splitting / header injection.
        if any(c in name for c in "\r\n:") or not name.strip():
            raise ValueError(f"invalid header name: {name!r}")
        if "\r" in value or "\n" in value or "\x00" in value:
            raise ValueError(f"invalid header value for {name!r}")
        try:
            # Fail here, in the handler (-> clean 500), not later while the
            # server is serializing the response mid-write.
            name.encode("ascii")
            value.encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError(f"header {name!r} is not encodable; percent-encode it") from None

    def add(self, name: str, value: str) -> None:
        value = str(value)
        self._check(name, value)
        self._items.append((name, value))

    def get(self, name: str, default: str | None = None) -> str | None:
        lname = name.lower()
        for k, v in self._items:
            if k.lower() == lname:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        lname = name.lower()
        return [v for k, v in self._items if k.lower() == lname]

    def setdefault(self, name: str, value: str) -> str:
        existing = self.get(name)
        if existing is None:
            self.add(name, value)
            return str(value)
        return existing

    def pop(self, name: str, default: str | None = None) -> str | None:
        value = self.get(name, default)
        self.__delitem__(name)
        return value

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def __getitem__(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise KeyError(name)
        return value

    def __setitem__(self, name: str, value: str) -> None:
        self.__delitem__(name)
        self.add(name, value)

    def __delitem__(self, name: str) -> None:
        lname = name.lower()
        self._items = [(k, v) for k, v in self._items if k.lower() != lname]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __iter__(self) -> Iterator[str]:
        return (k for k, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"Headers({self._items!r})"
