"""Veri güdümlü birim kataloğu (UnitCatalog dokümanlarından kurulur)."""
from __future__ import annotations

import re
from typing import Any

QTY_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*([A-Za-z%/][A-Za-z0-9_%/^.]*)\s*$")


class UnitError(ValueError):
    pass


class UnitCatalog:
    def __init__(self) -> None:
        # unit -> (dimension, factor, offset) ; taban = v * factor + offset
        self.units: dict[str, tuple[str, float, float]] = {}
        self.dimensions: dict[str, str] = {}

    def add_spec(self, spec: dict) -> list[str]:
        problems = []
        for dim, d in (spec.get("dimensions") or {}).items():
            self.dimensions[dim] = d["base"]
            for unit, f in d.get("units", {}).items():
                factor, offset = (f, 0.0) if isinstance(f, (int, float)) else (f["factor"], f.get("offset", 0.0))
                if unit in self.units and self.units[unit][0] != dim:
                    problems.append(f"'{unit}' birimi hem '{self.units[unit][0]}' hem '{dim}' boyutunda tanımlı")
                self.units[unit] = (dim, float(factor), float(offset))
            if d["base"] not in d.get("units", {}):
                problems.append(f"'{dim}' boyutunun taban birimi '{d['base']}' birim listesinde yok")
        return problems

    def knows(self, unit: str) -> bool:
        return unit in self.units

    def dimension(self, unit: str) -> str | None:
        u = self.units.get(unit)
        return u[0] if u else None

    def convert(self, value: float, src: str, dst: str) -> float:
        if src == dst:
            return float(value)
        if src not in self.units:
            raise UnitError(f"bilinmeyen birim '{src}'")
        if dst not in self.units:
            raise UnitError(f"bilinmeyen birim '{dst}'")
        ds, fs, os_ = self.units[src]
        dd, fd, od = self.units[dst]
        if ds != dd:
            raise UnitError(f"'{src}' ({ds}) -> '{dst}' ({dd}) boyutlar uyumsuz")
        base = value * fs + os_
        return (base - od) / fd

    @staticmethod
    def parse(value: Any) -> tuple[float, str | None] | None:
        """Sayı -> (v, None); "40 nmi" / {"value":40,"unit":"nmi"} -> (40, 'nmi'); aksi halde None."""
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value), None
        if isinstance(value, str):
            m = QTY_RE.match(value)
            return (float(m.group(1)), m.group(2)) if m else None
        if isinstance(value, dict) and set(value) == {"value", "unit"} and isinstance(value["value"], (int, float)):
            return float(value["value"]), str(value["unit"])
        return None

    def to_unit(self, value: Any, unit: str | None) -> float:
        """Birimli değeri hedef birime çevirir; hedef birim yoksa birimsiz sayı bekler."""
        parsed = self.parse(value)
        if parsed is None:
            raise UnitError(f"sayısal/birimli değer bekleniyordu: {value!r}")
        v, u = parsed
        if u is None:
            return v
        if unit is None:
            raise UnitError(f"bu alan birimsiz; '{u}' birimi kullanılamaz")
        return self.convert(v, u, unit)
