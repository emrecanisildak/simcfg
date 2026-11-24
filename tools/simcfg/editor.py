"""Model / davranış editörlerinin ihtiyaç duyduğu servisler.

* build_catalog : editörün form ve paletlerini üretebilmesi için tüm tiplerin çözülmüş hali + kalıtım ağacı
* describe      : bir şablonun her değerinin nereden geldiği (provenance) ile birlikte görünümü
* derive        : bir dokümandan yeni türev dokümanı iskeleti oluşturma ("Türet..." komutu)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .diagnostics import Diagnostics
from .workspace import API_VERSION, Workspace

CATALOG_API = "simcfg/catalog/v1"

SKELETONS: dict[str, dict] = {
    "SystemTemplate": {"parameters": {}, "initialState": {}},
    "PlatformTemplate": {"parameters": {}, "systems": {}, "behaviors": {}},
    "Behavior": {"parameters": {}, "rules": {}},
    "Scenario": {"entities": {}, "behaviorOverrides": {}, "rules": {}},
    "SystemType": {"parameters": {}, "state": {}},
    "PlatformType": {"parameters": {}, "state": {}},
    "ActionType": {"parameters": {}},
    "Maneuver": {"parameters": {}},
    "Munition": {"parameters": {}},
    "MunitionType": {"parameters": {}},
    "AreaType": {"properties": {}},
}


def build_catalog(ws: Workspace) -> dict:
    children = ws.children()
    docs: dict[str, list] = {}
    for doc in sorted(ws.docs.values(), key=lambda d: d.id):
        r = ws.resolve(doc.id)
        if r is None:
            continue
        docs.setdefault(doc.kind, []).append({
            "id": doc.id,
            "package": doc.package,
            "file": str(doc.location),
            "abstract": r.abstract,
            "extends": doc.extends,
            "lineage": r.linear,
            "children": sorted(children.get(doc.id, [])),
            "meta": r.meta,
            "spec": r.spec,
            "provenance": r.provenance,
        })

    def resolved(kind: str):
        for e in docs.get(kind, []):
            if not e["abstract"]:
                yield e

    palettes = {
        "actions": _group(resolved("ActionType"), lambda e: e["spec"].get("category") or "other"),
        "conditions": _group(resolved("ConditionType"), lambda e: e["spec"].get("category") or "other"),
        "events": _group(resolved("EventType"), lambda e: e["spec"].get("source") or "other"),
        "maneuvers": [e["id"] for e in resolved("Maneuver")],
        "munitions": _group(resolved("Munition"), lambda e: e["spec"].get("munitionType")),
        "systemTemplates": _group(resolved("SystemTemplate"), lambda e: e["spec"].get("systemType")),
        "platformTemplates": _group(resolved("PlatformTemplate"), lambda e: e["spec"].get("platformType")),
        "expressionOperators": ["eq", "ne", "lt", "le", "gt", "ge", "add", "sub", "mul", "div", "mod", "neg",
                                "min", "max", "abs", "between", "in", "exists", "if", "all", "any", "not"],
        "variableRoots": ["self", "self.params", "self.systems.<role>.state", "self.systems.<role>.params",
                          "entities.<key>", "event", "bb", "param", "scenario.time"],
    }
    return {
        "apiVersion": CATALOG_API,
        "packages": {p.id: {"version": p.raw.get("version"), "dependencies": p.spec.get("dependencies") or {}, "meta": p.meta}
                     for p in ws.packages.values()},
        "kinds": ws.kinds,
        "units": {dim: {"base": base, "units": sorted(u for u, (d, _, _) in ws.units.units.items() if d == dim)}
                  for dim, base in ws.units.dimensions.items()},
        "palettes": palettes,
        "documents": docs,
    }


def _group(items, keyfn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in items:
        out.setdefault(str(keyfn(e)), []).append(e["id"])
    return out


def describe(ws: Workspace, doc_id: str) -> str:
    r = ws.resolve(doc_id)
    if r is None:
        return f"'{doc_id}' bulunamadı"
    lines = [f"{r.id}  [{r.kind}{', abstract' if r.abstract else ''}]",
             f"  lineage: {' -> '.join(r.linear)}"]
    name = r.meta.get("name")
    if name:
        lines.append(f"  name: {name if isinstance(name, str) else name.get('tr') or name.get('en') or name}")

    def origin(path: str) -> str:
        # 1) değeri belirleyen en uzun ön ek (yaprak ya da $replace edilmiş alt ağaç)
        best = ""
        for p in r.provenance:
            if (path == p or path.startswith(p + ".")) and len(p) > len(best):
                best = p
        if best:
            return r.provenance[best]
        # 2) iç içe nesne: alt yolları belirleyen katmanlardan en türetilmiş olanı
        subs = {o for p, o in r.provenance.items() if p.startswith(path + ".")}
        if not subs:
            return "?"
        return max(subs, key=lambda o: r.linear.index(o) if o in r.linear else -1)

    def show_values(title: str, prefix: str, values: dict, descs: dict | None = None) -> None:
        if not values and not descs:
            return
        lines.append(f"  {title}:")
        keys = list(dict.fromkeys([*(descs or {}), *values]))
        for k in keys:
            d = (descs or {}).get(k, {})
            if k in values:
                val = json.dumps(values[k], ensure_ascii=False)
                src = origin(f"{prefix}.{k}")
            elif "default" in d:
                val, src = json.dumps(d["default"], ensure_ascii=False), "(default)"
            elif "defaultFrom" in d:
                val, src = f"<{d['defaultFrom']}>", "(defaultFrom)"
            else:
                val, src = ("<ZORUNLU>" if d.get("required") else "-"), ""
            if len(val) > 60:
                val = val[:57] + "..."
            unit = d.get("unit", "")
            lines.append(f"    {k:22} {val:40} {unit:6} {src}")

    spec = r.spec
    if r.kind == "SystemTemplate":
        st = ws.resolve(spec.get("systemType", ""))
        lines.append(f"  systemType: {spec.get('systemType')}")
        show_values("parameters", "parameters", spec.get("parameters") or {}, st.spec.get("parameters") if st else None)
        show_values("initialState", "initialState", spec.get("initialState") or {}, st.spec.get("state") if st else None)
    elif r.kind == "PlatformTemplate":
        pt = ws.resolve(spec.get("platformType", ""))
        lines.append(f"  platformType: {spec.get('platformType')}")
        show_values("parameters", "parameters", spec.get("parameters") or {}, pt.spec.get("parameters") if pt else None)
        lines.append("  systems:")
        for mk, m in (spec.get("systems") or {}).items():
            fitted = "" if m.get("fitted", True) else "  (fitted: false)"
            lines.append(f"    {mk:22} {m.get('template', '?'):40} {origin('systems.' + mk + '.template')}{fitted}")
            for pk, pv in (m.get("parameters") or {}).items():
                lines.append(f"      .{pk:20} {json.dumps(pv, ensure_ascii=False):40}        {origin(f'systems.{mk}.parameters.{pk}')}")
        lines.append("  behaviors:")
        for bk, b in (spec.get("behaviors") or {}).items():
            en = "" if b.get("enabled", True) else "  (disabled)"
            lines.append(f"    {bk:22} {b.get('behavior', '?'):40} {origin('behaviors.' + bk)}{en}")
            for pk, pv in (b.get("parameters") or {}).items():
                lines.append(f"      .{pk:20} {json.dumps(pv, ensure_ascii=False):40}        {origin(f'behaviors.{bk}.parameters.{pk}')}")
    elif r.kind == "Behavior":
        defaults = {k: d.get("default") for k, d in (spec.get("parameters") or {}).items() if "default" in d}
        show_values("parameters (defaults)", "parameters", {}, spec.get("parameters"))
        lines.append("  rules:")
        for rk, rule in sorted((spec.get("rules") or {}).items(), key=lambda kv: -kv[1].get("priority", 0)):
            trig = rule.get("trigger") or {}
            tdesc = next((f"{k}={v}" for k, v in trig.items() if k != "filter"), "?")
            en = "" if rule.get("enabled", True) else " (disabled)"
            acts = ", ".join(a.get("action", "?").split(".")[-1] for a in rule.get("actions") or [])
            lines.append(f"    [{rule.get('priority', 0):>4}] {rk:28} {tdesc:38} -> {acts}  {origin('rules.' + rk)}{en}")
        _ = defaults
    else:
        lines.append(json.dumps(spec, indent=2, ensure_ascii=False))
    return "\n".join(lines)


def derive(ws: Workspace, diags: Diagnostics, base_id: str, new_id: str, name: str | None = None,
           abstract: bool = False) -> Path | None:
    base = ws.get(base_id)
    if base is None:
        diags.error("derive.base", f"'{base_id}' bulunamadı")
        return None
    if new_id in ws.docs:
        diags.error("derive.exists", f"'{new_id}' zaten var: {ws.docs[new_id].location}")
        return None
    pkg_id = new_id.split(".")[0]
    pkg = ws.packages.get(pkg_id)
    if pkg is None or "." not in new_id:
        diags.error("derive.package", f"'{new_id}' için paket bulunamadı; kimlik '<paket>.<...>' biçiminde olmalı (paketler: {', '.join(ws.packages)})")
        return None
    if base.package not in ws.visible_packages(pkg_id):
        diags.error("derive.visibility", f"'{pkg_id}' paketi '{base.package}' paketine bağımlı değil (simpkg.json dependencies)")
        return None
    pkg_dir = ws.root / Path(pkg.file).parent
    kind_info = ws.kinds.get(base.kind) or {}
    target = pkg_dir / kind_info.get("dir", "") / f"{new_id.split('.')[-1]}.json"
    if target.exists():
        diags.error("derive.exists", f"dosya zaten var: {ws.rel(target)}")
        return None
    schema_rel = os.path.relpath(ws.schema_dir / kind_info.get("schema", "document.schema.json"), target.parent)
    doc: dict[str, Any] = {
        "$schema": Path(schema_rel).as_posix(),
        "apiVersion": API_VERSION,
        "kind": base.kind,
        "id": new_id,
        "extends": base_id,
        "version": "0.1.0",
    }
    if abstract:
        doc["abstract"] = True
    doc["meta"] = {"name": name or new_id.split(".")[-1]}
    doc["spec"] = SKELETONS.get(base.kind, {})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target
