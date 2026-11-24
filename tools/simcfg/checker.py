"""Parametre tanımlayıcı (descriptor) tabanlı değer doğrulama + ifade dili + aksiyon/kural doğrulama.

Tek bir yerde toplanır; hem kütüphane doğrulaması hem senaryo derlemesi aynı kodu kullanır.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field, replace
from typing import Any

from .diagnostics import Diagnostics, jp
from .units import UnitError
from .workspace import Workspace, suggest

PARAM_TYPES = {
    "number", "integer", "boolean", "string", "enum", "any",
    "ref", "systemRole", "entityRef", "areaRef", "routeRef", "sideRef",
    "geoPoint", "list", "map", "object", "table",
    "expression", "action", "actionList", "args",
}
SCENARIO_REF_TYPES = {"entityRef", "areaRef", "routeRef", "sideRef"}
NUMERIC = {"number", "integer"}
EXPR_KEYS = {"var", "param", "op", "cond", "all", "any", "not"}
LEVELS = {"template": 0, "instance": 1, "runtime": 2}

OP_ARITY: dict[str, tuple[int, int | None]] = {
    "eq": (2, 2), "ne": (2, 2), "lt": (2, 2), "le": (2, 2), "gt": (2, 2), "ge": (2, 2),
    "add": (2, None), "mul": (2, None), "min": (2, None), "max": (2, None),
    "sub": (2, 2), "div": (2, 2), "mod": (2, 2), "neg": (1, 1), "abs": (1, 1),
    "between": (3, 3), "in": (2, 2), "exists": (1, 1), "if": (3, 3),
}
COMPARE_OPS = {"eq", "ne", "lt", "le", "gt", "ge"}
ORDER_OPS = {"lt", "le", "gt", "ge"}
ARITH_OPS = {"add", "mul", "min", "max", "sub", "div", "mod", "neg", "abs"}

# Olayların hepsinde örtük olarak bulunan alanlar
IMPLICIT_EVENT_FIELDS = {"entity": {"type": "entityRef"}, "time": {"type": "number", "unit": "s"}}


def is_expr(v: Any) -> bool:
    return isinstance(v, dict) and bool(v) and bool(set(v) & EXPR_KEYS) and set(v) <= EXPR_KEYS | {"args", "params"}


# ---------------------------------------------------------------------- kapsam
@dataclass
class RoleInfo:
    system_type: str | None  # bağlı sistemin tipi (bağlı değilse beklenen tip)
    mount: str | None
    optional: bool = False
    bound: bool = True


@dataclass
class EntityInfo:
    platform_type: str | None
    roles: dict[str, RoleInfo] | None
    side: str | None


@dataclass
class ScenarioIndex:
    sides: set[str] = field(default_factory=set)
    areas: dict[str, str | None] = field(default_factory=dict)  # anahtar -> AreaType
    routes: set[str] = field(default_factory=set)
    entities: dict[str, EntityInfo | None] = field(default_factory=dict)


@dataclass
class Scope:
    """Bir değerin/ifadenin doğrulandığı bağlam."""

    params: dict | None = None  # {"param": x} için tanımlayıcılar (None = bilinmiyor, kontrol yok)
    variables: dict | None = None  # bb.* değişkenleri
    roles: dict[str, RoleInfo] | None = None  # systemRole çözümlemesi (None = bilinmiyor)
    platform_type: str | None = None  # self.* çözümlemesi
    event: str | None = None  # event.* çözümlemesi
    scenario: ScenarioIndex | None = None
    allow_expr: bool = False
    level: str = "template"
    defer_scenario_refs: bool = False  # şablon seviyesinde zorunlu senaryo referansları sonraya bırakılır
    no_self: bool = False  # senaryo seviyesi kurallar
    emitted: set[str] | None = None  # bu bağlamda yayılabilen olaylar (tetikleyici uyarısı için)
    used_roles: set[str] = field(default_factory=set)

    def with_(self, **kw: Any) -> "Scope":
        return replace(self, **kw)  # used_roles aynı nesne -> paylaşılır


# ---------------------------------------------------------------------- checker
class Checker:
    def __init__(self, ws: Workspace, diags: Diagnostics) -> None:
        self.ws = ws
        self.d = diags
        self._enum_cache: dict[str, list] = {}

    # ============================================================ descriptor doğrulama
    def check_descriptor_map(self, descs: Any, path: str, *, siblings_root: dict | None = None) -> None:
        if descs is None:
            return
        if not isinstance(descs, dict):
            self.d.error("descriptor.invalid", "parametre tanım haritası bir nesne olmalı", path)
            return
        for k, d in descs.items():
            self.check_descriptor(d, jp(path, k), siblings=descs, siblings_root=siblings_root)

    def check_descriptor(self, d: Any, path: str, *, siblings: dict | None = None, siblings_root: dict | None = None) -> None:
        if not isinstance(d, dict):
            self.d.error("descriptor.invalid", "tanımlayıcı bir nesne olmalı", path)
            return
        t = d.get("type")
        if t not in PARAM_TYPES:
            self.d.error("descriptor.type", f"bilinmeyen parametre tipi '{t}'{suggest(t, PARAM_TYPES)}", path)
            return
        unit = d.get("unit")
        if unit is not None and not self.ws.units.knows(unit) and unit != "1":
            self.d.error("descriptor.unit", f"bilinmeyen birim '{unit}'{suggest(unit, self.ws.units.units)}", path)
        if unit is not None and t not in NUMERIC | {"table"}:
            self.d.warning("descriptor.unit", f"'{t}' tipinde birim anlamsız", path)
        if "min" in d and "max" in d and d["min"] > d["max"]:
            self.d.error("descriptor.range", f"min ({d['min']}) > max ({d['max']})", path)
        if t == "enum":
            if not d.get("values") and not d.get("enumRef"):
                self.d.error("descriptor.enum", "enum için 'values' veya 'enumRef' gerekli", path)
            if d.get("enumRef"):
                self.ws.resolve_kind(d["enumRef"], "Enumeration", jp(path, "enumRef"))
        if t == "ref":
            rk = d.get("refKind")
            if rk and rk not in self.ws.kinds:
                self.d.error("descriptor.refKind", f"bilinmeyen refKind '{rk}'", jp(path, "refKind"))
        for ck, kind in (("systemType", "SystemType"), ("platformType", "PlatformType"), ("areaType", "AreaType"),
                         ("munitionType", "MunitionType")):
            cid = (d.get("constraint") or {}).get(ck)
            if cid:
                self.ws.resolve_kind(cid, kind, jp(path, "constraint", ck))
        if t in ("list", "map"):
            if "items" not in d:
                self.d.error("descriptor.items", f"'{t}' tipi için 'items' tanımı gerekli", path)
            else:
                self.check_descriptor(d["items"], jp(path, "items"))
        if t == "object":
            self.check_descriptor_map(d.get("fields") or {}, jp(path, "fields"))
        if t == "table":
            axes = d.get("axes")
            if not axes:
                self.d.error("descriptor.table", "table tipi için 'axes' gerekli", path)
            for i, ax in enumerate(axes or []):
                u = ax.get("unit")
                if u and not self.ws.units.knows(u):
                    self.d.error("descriptor.unit", f"bilinmeyen eksen birimi '{u}'", jp(path, "axes", i))
        if t == "args":
            src = d.get("argsOf")
            sd = (siblings or {}).get(src) if src else None
            if not src or sd is None:
                self.d.error("descriptor.args", f"args tipi için geçerli bir 'argsOf' kardeş parametresi gerekli (verilen: {src!r})", path)
            elif sd.get("type") not in ("ref", "systemRole"):
                self.d.error("descriptor.args", f"argsOf '{src}' tipi ref veya systemRole olmalı", path)
        df = d.get("defaultFrom")
        if df is not None:
            parts = str(df).split(".")
            if len(parts) != 2 or parts[0] != "parameters":
                self.d.error("descriptor.defaultFrom", "defaultFrom 'parameters.<ad>' biçiminde olmalı", path)
            elif siblings_root is not None and parts[1] not in siblings_root:
                self.d.error("descriptor.defaultFrom", f"'{parts[1]}' parametresi tanımlı değil", path)
        if "default" in d and t not in ("args", "action", "actionList"):
            self.value(d, d["default"], jp(path, "default"), Scope(level="template"), siblings={}, sibling_descs={})

    # ============================================================ enum değerleri
    def enum_values(self, d: dict, path: str | None = None) -> list | None:
        vals: list = []
        for v in d.get("values") or []:
            vals.append(v["value"] if isinstance(v, dict) else v)
        ref = d.get("enumRef")
        if ref:
            if ref not in self._enum_cache:
                r = self.ws.resolve_kind(ref, "Enumeration", path)
                self._enum_cache[ref] = [v["value"] if isinstance(v, dict) else v for v in (r.spec.get("values") or [])] if r else []
            vals.extend(self._enum_cache[ref])
        return vals

    # ============================================================ değer haritası
    def values(self, descs: dict | None, values: Any, path: str, scope: Scope, *,
               apply_defaults: bool = True, check_required: bool = True,
               sources: dict | None = None, forbid_readonly: bool = False) -> dict:
        descs = descs or {}
        if values is None:
            values = {}
        if not isinstance(values, dict):
            self.d.error("value.type", "nesne (anahtar -> değer) bekleniyordu", path)
            return {}
        out: dict = {}
        for k, v in values.items():
            if k not in descs:
                avail = ", ".join(sorted(descs)) or "-"
                self.d.error("param.unknown", f"bilinmeyen alan '{k}'{suggest(k, descs)}. Geçerli: {avail}", jp(path, k))
                out[k] = v
                continue
            d = descs[k]
            if forbid_readonly and d.get("readonly"):
                self.d.error("param.readonly", f"'{k}' salt okunur (motor tarafından güncellenir)", jp(path, k))
            out[k] = self.value(d, v, jp(path, k), scope, siblings=values, sibling_descs=descs)
        for k, d in descs.items():
            if k in out:
                continue
            if apply_defaults:
                if "default" in d:
                    out[k] = self.value(d, copy.deepcopy(d["default"]), jp(path, k), scope.with_(allow_expr=False),
                                        siblings=values, sibling_descs=descs)
                    continue
                if d.get("defaultFrom") and sources is not None:
                    src, _, name = d["defaultFrom"].partition(".")
                    if name in (sources.get(src) or {}):
                        out[k] = copy.deepcopy(sources[src][name])
                        continue
                if d.get("type") == "object" and d.get("fields"):
                    # Nesnenin kendisi verilmemiş: alan varsayılanlarından oluştur.
                    sub = self.values(d["fields"], {}, jp(path, k), scope.with_(allow_expr=False), check_required=False)
                    if sub:
                        out[k] = sub
                        continue
            if check_required and d.get("required"):
                if scope.defer_scenario_refs and d.get("type") in SCENARIO_REF_TYPES:
                    continue
                self.d.error("param.required", f"zorunlu alan '{k}' eksik", jp(path, k))
        return out

    def check_override_levels(self, descs: dict | None, overrides: Any, level: str, path: str) -> None:
        if not isinstance(overrides, dict):
            return
        for k in overrides:
            d = (descs or {}).get(k)
            if not d:
                continue
            allowed = d.get("overridableAt", "instance")
            if LEVELS.get(level, 0) > LEVELS.get(allowed, 1):
                self.d.error("param.override_level", f"'{k}' sadece '{allowed}' seviyesinde değiştirilebilir (burada: {level})", jp(path, k))

    # ============================================================ tek değer
    def value(self, d: dict, v: Any, path: str, scope: Scope, *, siblings: dict | None = None,
              sibling_descs: dict | None = None) -> Any:
        t = d.get("type")
        if v is None:
            if d.get("nullable") or t == "any":
                return None
            self.d.error("value.null", "null değere izin yok (nullable: true değil)", path)
            return v
        if is_expr(v) and t not in ("expression", "any"):
            if not scope.allow_expr:
                self.d.error("value.expr", "bu bağlamda ifade (expression) kullanılamaz; sabit değer verin", path)
                return v
            inferred = self.expr(v, path, scope)
            if not compatible(t, inferred):
                self.d.error("expr.type", f"ifade tipi '{inferred}', alan tipi '{t}' ile uyumsuz", path)
            if t == "systemRole" or t == "ref":
                return v
            return v
        handler = getattr(self, f"_v_{t}", None)
        if handler is None:
            return v
        return handler(d, v, path, scope, siblings or {}, sibling_descs or {})

    def _v_number(self, d, v, path, scope, sib, sdesc):
        if isinstance(v, bool):
            self.d.error("value.type", "sayı bekleniyordu, boolean verildi", path)
            return v
        try:
            x = self.ws.units.to_unit(v, d.get("unit"))
        except UnitError as e:
            self.d.error("value.unit", str(e), path)
            return v
        if d.get("type") == "integer":
            if not float(x).is_integer():
                self.d.error("value.type", f"tam sayı bekleniyordu: {v!r}", path)
                return v
            x = int(x)
        elif isinstance(x, float) and x.is_integer() and isinstance(v, int):
            x = v
        if "min" in d and x < d["min"]:
            self.d.error("value.range", f"{x} < min {d['min']}{' ' + d['unit'] if d.get('unit') else ''}", path)
        if "max" in d and x > d["max"]:
            self.d.error("value.range", f"{x} > max {d['max']}{' ' + d['unit'] if d.get('unit') else ''}", path)
        return x

    _v_integer = _v_number

    def _v_boolean(self, d, v, path, scope, sib, sdesc):
        if not isinstance(v, bool):
            self.d.error("value.type", f"boolean bekleniyordu: {v!r}", path)
        return v

    def _v_string(self, d, v, path, scope, sib, sdesc):
        if not isinstance(v, str):
            self.d.error("value.type", f"metin bekleniyordu: {v!r}", path)
            return v
        pat = d.get("pattern")
        if pat and not re.fullmatch(pat, v):
            self.d.error("value.pattern", f"'{v}' deseni ({pat}) karşılamıyor", path)
        return v

    def _v_enum(self, d, v, path, scope, sib, sdesc):
        allowed = self.enum_values(d, path)
        if allowed and v not in allowed:
            self.d.error("value.enum", f"'{v}' geçerli değil{suggest(v, [str(a) for a in allowed])}. Geçerli: {', '.join(map(str, allowed))}", path)
        return v

    def _v_ref(self, d, v, path, scope, sib, sdesc):
        kind = d.get("refKind")
        if not isinstance(v, str):
            self.d.error("value.type", "doküman kimliği (metin) bekleniyordu", path)
            return v
        if kind:
            r = self.ws.resolve_kind(v, kind, path, allow_abstract=bool(d.get("allowAbstract")))
        else:
            r = self.ws.resolve(v)
            if r is None:
                self.d.error("ref.unknown", f"bilinmeyen doküman '{v}'", path)
        for ck in ("systemType", "munitionType", "platformType"):
            need = (d.get("constraint") or {}).get(ck)
            if r and need and not self.ws.is_a(r.spec.get(ck), need):
                self.d.error("ref.constraint", f"'{v}' {ck} değeri '{r.spec.get(ck)}', '{need}' türevi değil", path)
        return v

    def _v_systemRole(self, d, v, path, scope, sib, sdesc):
        if not isinstance(v, str):
            self.d.error("value.type", "sistem rolü/mount adı (metin) bekleniyordu", path)
            return v
        if scope.roles is None:
            return v
        if v not in scope.roles:
            avail = ", ".join(sorted(scope.roles)) or "-"
            self.d.error("role.unknown", f"'{v}' bu bağlamda tanımlı bir sistem rolü/mount değil{suggest(v, scope.roles)}. Mevcut: {avail}", path)
            return v
        scope.used_roles.add(v)
        ri = scope.roles[v]
        need = (d.get("constraint") or {}).get("systemType")
        if need and ri.system_type and not self.ws.is_a(ri.system_type, need):
            # Henüz bir mount'a bağlanmamış (sadece beyan edilmiş) rol, daha genel bir tipse bağlamada uyabilir.
            declared_only = ri.mount is None and self.ws.is_a(need, ri.system_type)
            if not declared_only:
                self.d.error("role.constraint", f"'{v}' rolünün tipi '{ri.system_type}'; bu alan '{need}' (veya türevini) gerektirir", path)
        return v

    def _scenario_key(self, kind: str, collection: Any, v, path, scope) -> Any:
        if not isinstance(v, str):
            self.d.error("value.type", f"{kind} anahtarı (metin) bekleniyordu", path)
            return v
        if scope.scenario is not None and v not in collection(scope.scenario):
            self.d.error("ref.scenario", f"senaryoda '{v}' adlı {kind} yok{suggest(v, collection(scope.scenario))}", path)
        return v

    def _v_entityRef(self, d, v, path, scope, sib, sdesc):
        return self._scenario_key("varlık", lambda s: s.entities, v, path, scope)

    def _v_routeRef(self, d, v, path, scope, sib, sdesc):
        return self._scenario_key("rota", lambda s: s.routes, v, path, scope)

    def _v_sideRef(self, d, v, path, scope, sib, sdesc):
        return self._scenario_key("taraf", lambda s: s.sides, v, path, scope)

    def _v_areaRef(self, d, v, path, scope, sib, sdesc):
        self._scenario_key("alan", lambda s: s.areas, v, path, scope)
        need = (d.get("constraint") or {}).get("areaType")
        if need and scope.scenario and isinstance(v, str) and v in scope.scenario.areas:
            if not self.ws.is_a(scope.scenario.areas[v], need):
                self.d.error("ref.constraint", f"'{v}' alanı '{need}' türünde değil", path)
        return v

    def _v_geoPoint(self, d, v, path, scope, sib, sdesc):
        if not isinstance(v, dict) or "lat" not in v or "lon" not in v:
            self.d.error("value.type", "{lat, lon[, alt]} bekleniyordu", path)
            return v
        out = dict(v)
        for k, lo, hi in (("lat", -90, 90), ("lon", -180, 180)):
            if not isinstance(v[k], (int, float)) or not lo <= v[k] <= hi:
                self.d.error("value.range", f"{k} [{lo}, {hi}] aralığında olmalı", jp(path, k))
        if "alt" in v:
            out["alt"] = self.quantity(v["alt"], "m", jp(path, "alt"))
        extra = set(v) - {"lat", "lon", "alt"}
        if extra:
            self.d.error("value.type", f"geoPoint içinde bilinmeyen alan(lar): {', '.join(sorted(extra))}", path)
        return out

    def _v_list(self, d, v, path, scope, sib, sdesc):
        if not isinstance(v, list):
            self.d.error("value.type", "dizi bekleniyordu", path)
            return v
        if "minItems" in d and len(v) < d["minItems"]:
            self.d.error("value.range", f"en az {d['minItems']} eleman gerekli", path)
        if "maxItems" in d and len(v) > d["maxItems"]:
            self.d.error("value.range", f"en fazla {d['maxItems']} eleman olabilir", path)
        item = d.get("items") or {"type": "any"}
        return [self.value(item, x, jp(path, i), scope) for i, x in enumerate(v)]

    def _v_map(self, d, v, path, scope, sib, sdesc):
        if not isinstance(v, dict):
            self.d.error("value.type", "nesne (anahtar -> değer) bekleniyordu", path)
            return v
        item = d.get("items") or {"type": "any"}
        return {k: self.value(item, x, jp(path, k), scope) for k, x in v.items()}

    def _v_object(self, d, v, path, scope, sib, sdesc):
        return self.values(d.get("fields") or {}, v, path, scope)

    def _v_table(self, d, v, path, scope, sib, sdesc):
        axes = d.get("axes") or []
        if not isinstance(v, dict) or "breakpoints" not in v or "values" not in v:
            self.d.error("value.table", "{breakpoints: [[...] eksen başına], values: [...]} bekleniyordu", path)
            return v
        bps = v["breakpoints"]
        if not isinstance(bps, list) or len(bps) != len(axes):
            self.d.error("value.table", f"{len(axes)} eksen için {len(axes)} breakpoint dizisi gerekli", jp(path, "breakpoints"))
            return v
        for i, bp in enumerate(bps):
            if not isinstance(bp, list) or not bp or any(not isinstance(x, (int, float)) for x in bp):
                self.d.error("value.table", "breakpoint dizisi sayılardan oluşmalı", jp(path, "breakpoints", i))
                return v
            if any(b >= a for a, b in zip(bp[1:], bp[:-1])):
                self.d.error("value.table", "breakpoint'ler kesin artan olmalı", jp(path, "breakpoints", i))

        def check(arr: Any, dim: int, p: str) -> None:
            if dim == len(bps):
                if not isinstance(arr, (int, float)) or isinstance(arr, bool):
                    self.d.error("value.table", f"sayı bekleniyordu: {arr!r}", p)
                return
            if not isinstance(arr, list) or len(arr) != len(bps[dim]):
                self.d.error("value.table", f"boyut {dim}: {len(bps[dim])} eleman bekleniyordu", p)
                return
            for i, x in enumerate(arr):
                check(x, dim + 1, jp(p, i))

        check(v["values"], 0, jp(path, "values"))
        return v

    def _v_expression(self, d, v, path, scope, sib, sdesc):
        self.expr(v, path, scope.with_(allow_expr=True))
        return v

    def _v_action(self, d, v, path, scope, sib, sdesc):
        return self.invocation(v, path, scope)

    def _v_actionList(self, d, v, path, scope, sib, sdesc):
        if not isinstance(v, list):
            self.d.error("value.type", "aksiyon dizisi bekleniyordu", path)
            return v
        if "minItems" in d and len(v) < d["minItems"]:
            self.d.error("value.range", f"en az {d['minItems']} aksiyon gerekli", path)
        return [self.invocation(x, jp(path, i), scope) for i, x in enumerate(v)]

    def _v_args(self, d, v, path, scope, sib, sdesc):
        if not isinstance(v, dict):
            self.d.error("value.type", "argüman nesnesi bekleniyordu", path)
            return v
        src = d.get("argsOf")
        src_val = sib.get(src)
        src_desc = sdesc.get(src) or {}
        target_spec = None
        if isinstance(src_val, str):
            if src_desc.get("type") == "ref":
                r = self.ws.resolve(src_val)
                target_spec = r.spec if r else None
            elif src_desc.get("type") == "systemRole" and scope.roles is not None and src_val in scope.roles:
                st = scope.roles[src_val].system_type
                r = self.ws.resolve(st) if st else None
                target_spec = r.spec if r else None
        if target_spec is None:
            return v  # hedef çözülemedi (ifade/bilinmeyen) -> derinlemesine kontrol atlanır
        frm = d.get("argsFrom", "parameters")
        try:
            frm = frm.format(**{k: x for k, x in sib.items() if isinstance(x, str)})
        except KeyError:
            return v
        node: Any = target_spec
        for part in frm.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            self.d.error("args.target", f"'{src_val}' üzerinde '{frm}' tanımı yok", path)
            return v
        return self.values(node, v, path, scope.with_(allow_expr=scope.allow_expr), apply_defaults=False,
                           check_required=not d.get("partial"), forbid_readonly=frm.startswith("state"))

    def _v_any(self, d, v, path, scope, sib, sdesc):
        if is_expr(v) and scope.allow_expr:
            self.expr(v, path, scope)
        return v

    # ============================================================ birim
    def quantity(self, v: Any, unit: str, path: str) -> Any:
        if is_expr(v):
            return v
        try:
            return self.ws.units.to_unit(v, unit)
        except UnitError as e:
            self.d.error("value.unit", str(e), path)
            return v

    # ============================================================ ifadeler
    def expr(self, node: Any, path: str, scope: Scope) -> str:
        """İfadeyi doğrular, çıkarılan tipi döndürür ('number', 'boolean', 'string', 'any', ...)."""
        if isinstance(node, bool):
            return "boolean"
        if isinstance(node, (int, float)):
            return "number"
        if isinstance(node, str):
            return "string"
        if node is None:
            return "any"
        if isinstance(node, list):
            for i, x in enumerate(node):
                self.expr(x, jp(path, i), scope)
            return "list"
        if not isinstance(node, dict):
            self.d.error("expr.invalid", f"geçersiz ifade: {node!r}", path)
            return "any"
        if "var" in node:
            return self.var_type(node["var"], jp(path, "var"), scope)
        if "param" in node:
            name = node["param"]
            if scope.params is None:
                return "any"
            if name not in scope.params:
                self.d.error("expr.param", f"bilinmeyen parametre '{name}'{suggest(name, scope.params)}", path)
                return "any"
            return scope.params[name].get("type", "any")
        if "op" in node:
            return self._op(node, path, scope)
        if "cond" in node:
            ct = self.ws.resolve_kind(node["cond"], "ConditionType", jp(path, "cond"), allow_abstract=False)
            if ct:
                self.values(ct.spec.get("parameters"), node.get("params") or {}, jp(path, "params"),
                            scope.with_(allow_expr=True))
            return "boolean"
        for k in ("all", "any"):
            if k in node:
                items = node[k]
                if not isinstance(items, list) or not items:
                    self.d.error("expr.invalid", f"'{k}' boş olmayan bir dizi bekler", path)
                    return "boolean"
                for i, x in enumerate(items):
                    t = self.expr(x, jp(path, k, i), scope)
                    if not compatible("boolean", t):
                        self.d.error("expr.type", f"'{k}' elemanı boolean olmalı (bulunan: {t})", jp(path, k, i))
                return "boolean"
        if "not" in node:
            t = self.expr(node["not"], jp(path, "not"), scope)
            if not compatible("boolean", t):
                self.d.error("expr.type", f"'not' boolean bekler (bulunan: {t})", path)
            return "boolean"
        self.d.error("expr.invalid", f"tanınmayan ifade biçimi: anahtarlar {sorted(node)}", path)
        return "any"

    def _op(self, node: dict, path: str, scope: Scope) -> str:
        op = node.get("op")
        args = node.get("args")
        if op not in OP_ARITY:
            self.d.error("expr.op", f"bilinmeyen operatör '{op}'{suggest(op, OP_ARITY)}", path)
            return "any"
        if not isinstance(args, list):
            self.d.error("expr.op", f"'{op}' için 'args' dizisi gerekli", path)
            return "any"
        lo, hi = OP_ARITY[op]
        if len(args) < lo or (hi is not None and len(args) > hi):
            self.d.error("expr.arity", f"'{op}' {lo}{'' if hi == lo else '+' if hi is None else '-' + str(hi)} argüman bekler, {len(args)} verildi", path)
        types = [self.expr(a, jp(path, "args", i), scope) for i, a in enumerate(args)]
        if op in ARITH_OPS or op in ORDER_OPS or op == "between":
            for i, t in enumerate(types):
                if not compatible("number", t):
                    self.d.error("expr.type", f"'{op}' sayısal argüman bekler (args[{i}]: {t})", path)
        if op in COMPARE_OPS | {"between", "in", "exists"}:
            return "boolean"
        if op == "if":
            if types and not compatible("boolean", types[0]):
                self.d.error("expr.type", "'if' ilk argümanı boolean olmalı", path)
            return types[1] if len(types) > 1 else "any"
        return "number"

    def var_type(self, p: Any, path: str, scope: Scope) -> str:
        if not isinstance(p, str) or not p:
            self.d.error("expr.var", "değişken yolu metin olmalı", path)
            return "any"
        parts = p.split(".")
        root = parts[0]
        if root == "self":
            if scope.no_self:
                self.d.error("expr.var", "senaryo seviyesinde 'self' yok; 'entities.<anahtar>...' kullanın", path)
                return "any"
            return self._entity_path(parts[1:], scope.platform_type, scope.roles, path, scope, track=True)
        if root == "entities":
            if len(parts) < 2:
                self.d.error("expr.var", "'entities.<anahtar>' gerekli", path)
                return "any"
            if scope.scenario is None:
                return "any"
            key = parts[1]
            if key not in scope.scenario.entities:
                self.d.error("expr.var", f"senaryoda '{key}' varlığı yok{suggest(key, scope.scenario.entities)}", path)
                return "any"
            info = scope.scenario.entities[key]
            if info is None:
                return "any"
            return self._entity_path(parts[2:], info.platform_type, info.roles, path, scope, track=False)
        if root == "event":
            if scope.event is None:
                self.d.error("expr.var", "'event.*' sadece olay (event) tetiklemeli kurallarda kullanılabilir", path)
                return "any"
            if len(parts) < 2:
                return "any"
            ev = self.ws.resolve(scope.event)
            payload = {**IMPLICIT_EVENT_FIELDS, **((ev.spec.get("payload") if ev else None) or {})}
            if parts[1] not in payload:
                self.d.error("expr.var", f"'{scope.event}' olayında '{parts[1]}' alanı yok{suggest(parts[1], payload)}. Alanlar: {', '.join(sorted(payload))}", path)
                return "any"
            return payload[parts[1]].get("type", "any") if len(parts) == 2 else "any"
        if root == "bb":
            if len(parts) < 2:
                return "any"
            if scope.variables is None:
                return "any"
            if parts[1] not in scope.variables:
                self.d.error("expr.var", f"tanımsız değişken 'bb.{parts[1]}'{suggest(parts[1], scope.variables)}", path)
                return "any"
            return scope.variables[parts[1]].get("type", "any")
        if root == "param":
            return self.expr({"param": parts[1] if len(parts) > 1 else ""}, path, scope)
        if root == "scenario":
            if len(parts) == 2 and parts[1] in ("time", "elapsed"):
                return "number"
            self.d.error("expr.var", "geçerli: scenario.time, scenario.elapsed", path)
            return "any"
        self.d.error("expr.var", f"bilinmeyen kök '{root}'. Geçerli: self, entities, event, bb, param, scenario", path)
        return "any"

    def _entity_path(self, parts: list[str], ptype: str | None, roles: dict | None, path: str, scope: Scope,
                     track: bool) -> str:
        if not parts:
            return "entityRef"
        head = parts[0]
        if head in ("id", "side", "callsign"):
            return "string"
        if head == "params":
            if len(parts) < 2 or not ptype:
                return "any"
            pt = self.ws.resolve(ptype)
            descs = (pt.spec.get("parameters") if pt else None) or {}
            if parts[1] not in descs:
                self.d.error("expr.var", f"'{ptype}' platform tipinde '{parts[1]}' parametresi yok{suggest(parts[1], descs)}", path)
                return "any"
            return descs[parts[1]].get("type", "any")
        if head == "systems":
            if len(parts) < 2:
                self.d.error("expr.var", "'systems.<rol>' gerekli", path)
                return "any"
            role = parts[1]
            if roles is None:
                return "any"
            if role not in roles:
                self.d.error("expr.var", f"'{role}' sistem rolü/mount tanımlı değil{suggest(role, roles)}. Mevcut: {', '.join(sorted(roles)) or '-'}", path)
                return "any"
            if track:
                scope.used_roles.add(role)
            if len(parts) == 2:
                return "systemRole"
            section = parts[2]
            if section == "fitted":
                return "boolean"
            if section not in ("state", "params"):
                self.d.error("expr.var", "'systems.<rol>.' sonrası geçerli: state, params, fitted", path)
                return "any"
            st = self.ws.resolve(roles[role].system_type) if roles[role].system_type else None
            if st is None or len(parts) < 4:
                return "any"
            descs = st.spec.get("state" if section == "state" else "parameters") or {}
            if parts[3] not in descs:
                self.d.error("expr.var", f"'{st.id}' tipinde '{section}.{parts[3]}' yok{suggest(parts[3], descs)}", path)
                return "any"
            return descs[parts[3]].get("type", "any")
        if not ptype:
            return "any"
        pt = self.ws.resolve(ptype)
        descs = (pt.spec.get("state") if pt else None) or {}
        if head not in descs:
            self.d.error("expr.var", f"'{ptype}' platform durumunda '{head}' yok{suggest(head, descs)}. (parametreler için self.params.<ad>)", path)
            return "any"
        return descs[head].get("type", "any")

    # ============================================================ aksiyon çağrısı
    def invocation(self, inv: Any, path: str, scope: Scope) -> Any:
        if not isinstance(inv, dict) or "action" not in inv:
            self.d.error("action.invalid", "{action: <id>, params: {...}} bekleniyordu", path)
            return inv
        at = self.ws.resolve_kind(inv["action"], "ActionType", jp(path, "action"), allow_abstract=False)
        out = dict(inv)
        s = scope.with_(allow_expr=True)
        on = inv.get("on")
        if on is not None:
            if scope.scenario is None:
                self.d.error("action.on", "'on' sadece senaryo seviyesinde kullanılabilir", jp(path, "on"))
            elif on not in scope.scenario.entities:
                self.d.error("action.on", f"senaryoda '{on}' varlığı yok{suggest(on, scope.scenario.entities)}", jp(path, "on"))
            else:
                info = scope.scenario.entities[on]
                s = s.with_(roles=info.roles if info else None, platform_type=info.platform_type if info else None,
                            no_self=False, used_roles=set())
        if "when" in inv:
            t = self.expr(inv["when"], jp(path, "when"), s)
            if not compatible("boolean", t):
                self.d.error("expr.type", "'when' boolean olmalı", jp(path, "when"))
        if "timeout" in inv:
            out["timeout"] = self.quantity(inv["timeout"], "s", jp(path, "timeout"))
        if at:
            out["params"] = self.values(at.spec.get("parameters"), inv.get("params") or {}, jp(path, "params"), s)
        return out

    # ============================================================ kural
    def rule(self, key: str, rule: Any, path: str, scope: Scope) -> dict | None:
        if not isinstance(rule, dict):
            self.d.error("rule.invalid", "kural bir nesne olmalı", path)
            return None
        out: dict = {"enabled": rule.get("enabled", True), "priority": rule.get("priority", 0)}
        for k in ("label", "description", "maxFires", "tags", "ui"):
            if k in rule:
                out[k] = rule[k]
        trig = rule.get("trigger")
        rscope = scope.with_(event=None)
        if not isinstance(trig, dict) or not trig:
            self.d.error("rule.trigger", "tetikleyici (trigger) gerekli: event | every | at | edge", jp(path, "trigger"))
        elif "event" in trig:
            ev = self.ws.resolve_kind(trig["event"], "EventType", jp(path, "trigger", "event"))
            rscope = scope.with_(event=trig["event"] if ev else None)
            nt: dict = {"event": trig["event"]}
            if "filter" in trig:
                t = self.expr(trig["filter"], jp(path, "trigger", "filter"), rscope)
                if not compatible("boolean", t):
                    self.d.error("expr.type", "'filter' boolean olmalı", jp(path, "trigger", "filter"))
                nt["filter"] = trig["filter"]
            if ev and scope.emitted is not None and trig["event"] not in scope.emitted:
                # Uyarı kararını çağırana bırak: kural bağlanmamış opsiyonel rol yüzünden zaten kapatılabilir.
                out["_neverFires"] = jp(path, "trigger")
            out["trigger"] = nt
        elif "every" in trig:
            iv = self.quantity(trig["every"], "s", jp(path, "trigger", "every"))
            if isinstance(iv, (int, float)) and iv <= 0:
                self.d.error("rule.trigger", "periyot > 0 olmalı", jp(path, "trigger", "every"))
            out["trigger"] = {"every": iv, **({"offset": self.quantity(trig["offset"], "s", jp(path, "trigger", "offset"))} if "offset" in trig else {})}
        elif "at" in trig:
            out["trigger"] = {"at": self.quantity(trig["at"], "s", jp(path, "trigger", "at"))}
        elif "edge" in trig:
            if "condition" not in rule:
                self.d.error("rule.trigger", "'edge' tetikleyici bir 'condition' gerektirir", jp(path, "trigger"))
            out["trigger"] = {"edge": trig["edge"]}
        else:
            self.d.error("rule.trigger", f"bilinmeyen tetikleyici: {sorted(trig)}", jp(path, "trigger"))
        if "condition" in rule:
            t = self.expr(rule["condition"], jp(path, "condition"), rscope)
            if not compatible("boolean", t):
                self.d.error("expr.type", f"koşul boolean olmalı (bulunan: {t})", jp(path, "condition"))
            out["condition"] = rule["condition"]
        acts = rule.get("actions")
        if not isinstance(acts, list) or not acts:
            self.d.error("rule.actions", "en az bir aksiyon gerekli", jp(path, "actions"))
            acts = []
        out["actions"] = [self.invocation(a, jp(path, "actions", i), rscope) for i, a in enumerate(acts)]
        if "cooldown" in rule:
            out["cooldown"] = self.quantity(rule["cooldown"], "s", jp(path, "cooldown"))
        return out


def compatible(expected: str | None, actual: str | None) -> bool:
    if actual in (None, "any") or expected in (None, "any", "expression", "args"):
        return True
    if expected in NUMERIC:
        return actual in NUMERIC
    if expected == "boolean":
        return actual == "boolean"
    if expected in ("string", "enum"):
        return actual in ("string", "enum")
    if expected in SCENARIO_REF_TYPES | {"systemRole"}:
        return actual in (expected, "string")
    if expected == "geoPoint":
        return actual == "geoPoint"
    if expected == "list":
        return actual == "list"
    return True
