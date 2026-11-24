"""Tanı (diagnostic) toplama: tüm doğrulama katmanları buraya yazar."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    doc: str | None = None
    path: str | None = None
    file: str | None = None

    def format(self) -> str:
        where = []
        if self.doc:
            where.append(self.doc)
        if self.path:
            where.append(self.path)
        loc = " @ ".join(where)
        file = f" ({self.file})" if self.file else ""
        return f"{self.severity.upper():7} {self.code:24} {loc}{file}\n        {self.message}"

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


class Diagnostics:
    def __init__(self) -> None:
        self.items: list[Diagnostic] = []
        self._seen: set[tuple] = set()
        self._ctx: list[dict] = [{}]

    @contextmanager
    def scope(self, doc: str | None = None, file: str | None = None) -> Iterator[None]:
        cur = dict(self._ctx[-1])
        if doc is not None:
            cur["doc"] = doc
        if file is not None:
            cur["file"] = file
        self._ctx.append(cur)
        try:
            yield
        finally:
            self._ctx.pop()

    def add(self, severity: str, code: str, message: str, path: str | None = None,
            doc: str | None = None, file: str | None = None) -> None:
        ctx = self._ctx[-1]
        d = Diagnostic(severity, code, message, doc or ctx.get("doc"), path, file or ctx.get("file"))
        key = (d.severity, d.code, d.message, d.doc, d.path)
        if key in self._seen:
            return
        self._seen.add(key)
        self.items.append(d)

    def error(self, code: str, message: str, path: str | None = None, **kw) -> None:
        self.add("error", code, message, path, **kw)

    def warning(self, code: str, message: str, path: str | None = None, **kw) -> None:
        self.add("warning", code, message, path, **kw)

    def info(self, code: str, message: str, path: str | None = None, **kw) -> None:
        self.add("info", code, message, path, **kw)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.items if d.severity == "warning"]

    def has_errors(self) -> bool:
        return any(d.severity == "error" for d in self.items)

    def sorted(self) -> list[Diagnostic]:
        return sorted(self.items, key=lambda d: (SEVERITY_ORDER[d.severity], d.doc or "", d.path or ""))


def jp(base: str | None, *parts: object) -> str:
    """JSON yolu birleştir: jp('spec', 'systems', 'radar') -> spec.systems.radar ; int -> [i]"""
    out = base or ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out = f"{out}.{p}" if out else str(p)
    return out
