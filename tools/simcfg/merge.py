"""Katmanlı birleştirme (deep merge) ve yama direktifleri.

Kurallar
--------
* object + object  -> anahtar bazında derin birleştirme
* diğer her şey    -> yamadaki değer tabandakinin yerine geçer (diziler dahil)
* Direktifler (tek anahtarlı nesneler):
    {"$delete": true}      anahtarı kaldırır
    {"$replace": X}        derin birleştirme yapmadan X ile değiştirir
    {"$append":  [..]}     tabandaki diziye sona ekler
    {"$prepend": [..]}     tabandaki dizinin başına ekler
"""
from __future__ import annotations

import copy
from typing import Any

DELETE, REPLACE, APPEND, PREPEND = "$delete", "$replace", "$append", "$prepend"
DIRECTIVES = (DELETE, REPLACE, APPEND, PREPEND)


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "MISSING"


MISSING: Any = _Missing()


class MergeError(ValueError):
    pass


def is_directive(v: Any) -> bool:
    return isinstance(v, dict) and len(v) == 1 and next(iter(v)) in DIRECTIVES


def merge(base: Any, patch: Any, path: str = "") -> Any:
    """`base` üzerine `patch` uygular. Girdileri değiştirmez. MISSING dönerse anahtar silinmelidir."""
    if is_directive(patch):
        key, val = next(iter(patch.items()))
        if key == DELETE:
            if val is not True:
                raise MergeError(f"{path}: $delete değeri true olmalı")
            return MISSING
        if key == REPLACE:
            return merge(MISSING, val, path)
        if not isinstance(val, list):
            raise MergeError(f"{path}: {key} bir dizi bekler")
        items = [merge(MISSING, x, f"{path}[{i}]") for i, x in enumerate(val)]
        if base is MISSING or base is None:
            return items
        if not isinstance(base, list):
            raise MergeError(f"{path}: {key} sadece dizilere uygulanabilir (taban: {type(base).__name__})")
        return copy.deepcopy(base) + items if key == APPEND else items + copy.deepcopy(base)

    if isinstance(patch, dict):
        out = copy.deepcopy(base) if isinstance(base, dict) else {}
        for k, v in patch.items():
            r = merge(out.get(k, MISSING), v, f"{path}.{k}" if path else k)
            if r is MISSING:
                out.pop(k, None)
            else:
                out[k] = r
        return out

    if isinstance(patch, list):
        return [merge(MISSING, x, f"{path}[{i}]") for i, x in enumerate(patch)]

    return patch


def merge_all(layers: list[Any]) -> Any:
    out: Any = {}
    for layer in layers:
        if layer is None:
            continue
        out = merge(out, layer)
        if out is MISSING:
            out = {}
    return out


def record_provenance(patch: Any, origin: str, prov: dict[str, str], prefix: str = "") -> None:
    """Bir yama katmanının hangi yaprak yolları belirlediğini kaydeder (editörde 'şuradan miras' gösterimi için)."""

    def drop(pfx: str) -> None:
        for k in [k for k in prov if k == pfx or k.startswith(pfx + ".") or k.startswith(pfx + "[")]:
            del prov[k]

    if is_directive(patch):
        key = next(iter(patch))
        drop(prefix)
        if key != DELETE:
            prov[prefix] = origin
        return
    if isinstance(patch, dict) and patch:
        for k, v in patch.items():
            record_provenance(v, origin, prov, f"{prefix}.{k}" if prefix else k)
        return
    drop(prefix)
    prov[prefix] = origin
