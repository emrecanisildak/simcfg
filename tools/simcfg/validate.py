"""Tür (kind) bazlı semantik doğrulama.

Yeni bir doküman türü eklendiğinde: schemas/v1/registry.json'a kaydedin ve burada
`@handler("YeniTur")` ile bir doğrulayıcı tanımlayın (opsiyonel; yoksa sadece şema doğrulaması yapılır).
"""
from __future__ import annotations

from typing import Callable

from .checker import RoleInfo, Scope, compatible
from .compiler import Builder
from .diagnostics import Diagnostics, jp
from .workspace import Resolved, Workspace

HANDLERS: dict[str, Callable[["Validator", Resolved], None]] = {}


def handler(kind: str):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn

    return deco


# Manevra segment alanları: ad -> (tip, birim)
SEGMENT_FIELDS = {
    "g": {"type": "number", "unit": "g", "min": 0},
    "headingChange": {"type": "number", "unit": "deg"},
    "heading": {"type": "number", "unit": "deg"},
    "angle": {"type": "number", "unit": "deg"},
    "altitudeChange": {"type": "number", "unit": "m"},
    "rate": {"type": "number", "unit": "m/s"},
    "speed": {"type": "number", "unit": "m/s"},
    "duration": {"type": "number", "unit": "s", "min": 0},
    "relative": {"type": "boolean"},
    "direction": {"type": "enum", "values": ["left", "right", "away", "toward", "shortest"]},
}
SEGMENT_REQUIRED = {
    "turn": ["headingChange"], "climb": ["altitudeChange"], "descend": ["altitudeChange"],
    "accelerate": ["speed"], "hold": ["duration"], "roll": ["angle"], "pull": [], "heading": ["heading"],
}


class Validator:
    def __init__(self, ws: Workspace, diags: Diagnostics) -> None:
        self.ws = ws
        self.d = diags
        self.b = Builder(ws, diags)
        self.ck = self.b.ck

    def run(self, compile_scenarios: bool = True) -> None:
        for doc in sorted(self.ws.docs.values(), key=lambda x: x.id):
            with self.d.scope(doc=doc.id, file=doc.location):
                r = self.ws.resolve(doc.id)
                if r is None:
                    continue
                kind_info = self.ws.kinds.get(r.kind) or {}
                if kind_info.get("schema"):
                    self.ws.schema_validate(r.as_document(), kind_info["schema"], doc.id)
                if doc.raw.get("deprecated"):
                    self.d.info("doc.deprecated", f"kullanımdan kaldırılmış: {doc.raw['deprecated']}")
                for parent in doc.extends:
                    p = self.ws.get(parent)
                    if p and p.raw.get("deprecated"):
                        self.d.warning("extends.deprecated", f"'{parent}' kullanımdan kaldırılmış", "extends")
                if r.kind == "Scenario" and not compile_scenarios:
                    continue
                fn = HANDLERS.get(r.kind)
                if fn:
                    fn(self, r)

    # ortak
    def _impl_required(self, r: Resolved) -> None:
        if not r.abstract and not r.spec.get("implementation"):
            self.d.error("type.implementation", "soyut olmayan tip için 'implementation' gerekli", "spec.implementation")

    def _emits(self, r: Resolved) -> None:
        for i, e in enumerate(r.spec.get("emits") or []):
            self.ws.resolve_kind(e, "EventType", jp("spec.emits", i))


@handler("Enumeration")
def _enum(v: Validator, r: Resolved) -> None:
    vals = [x["value"] if isinstance(x, dict) else x for x in r.spec.get("values") or []]
    if not vals and not r.abstract:
        v.d.error("enum.empty", "en az bir değer gerekli", "spec.values")
    dup = {x for x in vals if vals.count(x) > 1}
    if dup:
        v.d.error("enum.duplicate", f"tekrarlanan değer(ler): {', '.join(map(str, sorted(dup, key=str)))}", "spec.values")


@handler("SystemType")
def _system_type(v: Validator, r: Resolved) -> None:
    params = r.spec.get("parameters") or {}
    v.ck.check_descriptor_map(params, "spec.parameters")
    v.ck.check_descriptor_map(r.spec.get("state"), "spec.state", siblings_root=params)
    for ck_, c in (r.spec.get("commands") or {}).items():
        v.ck.check_descriptor_map(c.get("parameters"), jp("spec.commands", ck_, "parameters"))
    clash = set(params) & set(r.spec.get("state") or {})
    if clash:
        v.d.warning("type.name_clash", f"aynı ad hem parametre hem durum: {', '.join(sorted(clash))}", "spec")
    v._emits(r)
    v._impl_required(r)


@handler("PlatformType")
def _platform_type(v: Validator, r: Resolved) -> None:
    params = r.spec.get("parameters") or {}
    v.ck.check_descriptor_map(params, "spec.parameters")
    v.ck.check_descriptor_map(r.spec.get("state"), "spec.state", siblings_root=params)
    for i, st in enumerate(r.spec.get("allowedSystemTypes") or []):
        v.ws.resolve_kind(st, "SystemType", jp("spec.allowedSystemTypes", i))
    v._emits(r)
    v._impl_required(r)


@handler("EventType")
def _event_type(v: Validator, r: Resolved) -> None:
    v.ck.check_descriptor_map(r.spec.get("payload"), "spec.payload")


@handler("AreaType")
def _area_type(v: Validator, r: Resolved) -> None:
    v.ck.check_descriptor_map(r.spec.get("properties"), "spec.properties")


@handler("ConditionType")
def _condition_type(v: Validator, r: Resolved) -> None:
    params = r.spec.get("parameters") or {}
    v.ck.check_descriptor_map(params, "spec.parameters")
    impl = r.spec.get("implementation")
    if not impl:
        if not r.abstract:
            v.d.error("type.implementation", "'implementation' gerekli (native | expression)", "spec.implementation")
        return
    ptype = (r.spec.get("requires") or {}).get("platformType")
    if ptype:
        v.ws.resolve_kind(ptype, "PlatformType", "spec.requires.platformType")
    if impl.get("type") == "expression":
        t = v.ck.expr(impl.get("body"), "spec.implementation.body",
                      Scope(params=params, platform_type=ptype, allow_expr=True))
        if not compatible("boolean", t):
            v.d.error("expr.type", f"koşul gövdesi boolean döndürmeli (bulunan: {t})", "spec.implementation.body")


@handler("ActionType")
def _action_type(v: Validator, r: Resolved) -> None:
    params = r.spec.get("parameters") or {}
    v.ck.check_descriptor_map(params, "spec.parameters")
    impl = r.spec.get("implementation")
    if not impl:
        if not r.abstract:
            v.d.error("type.implementation", "'implementation' gerekli (native | command | composite)", "spec.implementation")
        return
    scope = Scope(params=params, allow_expr=True)
    if impl.get("type") == "command":
        pname = impl.get("system")
        pd = params.get(pname)
        if not pd or pd.get("type") != "systemRole":
            v.d.error("action.command", f"implementation.system '{pname}' systemRole tipinde bir parametre olmalı", "spec.implementation.system")
            return
        st_id = (pd.get("constraint") or {}).get("systemType")
        st = v.ws.resolve(st_id) if st_id else None
        if st is None:
            # Kısıt yoksa komut tüm sistemlerde (kök tiplerde) bulunmalı; derin kontrol yapılamaz.
            return
        cmd = (st.spec.get("commands") or {}).get(impl.get("command"))
        if cmd is None:
            v.d.error("action.command", f"'{st.id}' tipinde '{impl.get('command')}' komutu yok. Mevcut: {', '.join(sorted(st.spec.get('commands') or {})) or '-'}", "spec.implementation.command")
            return
        v.ck.values(cmd.get("parameters"), impl.get("args") or {}, "spec.implementation.args", scope, apply_defaults=False)
    elif impl.get("type") == "composite":
        v.ck.invocation(impl.get("body"), "spec.implementation.body", scope)


@handler("Maneuver")
def _maneuver(v: Validator, r: Resolved) -> None:
    params = r.spec.get("parameters") or {}
    v.ck.check_descriptor_map(params, "spec.parameters")
    scope = Scope(params=params, allow_expr=True)
    segs = r.spec.get("segments") or []
    if not segs and not r.abstract:
        v.d.error("maneuver.empty", "en az bir segment gerekli", "spec.segments")
    for i, seg in enumerate(segs):
        p = jp("spec.segments", i)
        st = seg.get("type")
        fields = {k: x for k, x in seg.items() if k not in ("type", "until") and not k.startswith("x-")}
        v.ck.values(SEGMENT_FIELDS, fields, p, scope, apply_defaults=False, check_required=False)
        for req in SEGMENT_REQUIRED.get(st, []):
            if req not in seg:
                v.d.error("maneuver.segment", f"'{st}' segmenti '{req}' alanı gerektirir", p)
        if "until" in seg:
            t = v.ck.expr(seg["until"], jp(p, "until"), scope)
            if not compatible("boolean", t):
                v.d.error("expr.type", "'until' boolean olmalı", jp(p, "until"))
        if st == "pull" and "until" not in seg and "duration" not in seg:
            v.d.warning("maneuver.segment", "'pull' segmenti için 'until' veya 'duration' önerilir", p)


@handler("Behavior")
def _behavior(v: Validator, r: Resolved) -> None:
    spec = r.spec
    params = spec.get("parameters") or {}
    v.ck.check_descriptor_map(params, "spec.parameters")
    v.ck.check_descriptor_map(spec.get("variables"), "spec.variables")
    req = spec.get("requires") or {}
    ptype = req.get("platformType")
    if ptype:
        v.ws.resolve_kind(ptype, "PlatformType", "spec.requires.platformType")
    roles = {}
    for role, rq in (req.get("systems") or {}).items():
        v.ws.resolve_kind(rq.get("systemType"), "SystemType", jp("spec.requires.systems", role, "systemType"))
        roles[role] = RoleInfo(rq.get("systemType"), None, bool(rq.get("optional")), True)
    if not spec.get("rules") and not r.abstract:
        v.d.warning("behavior.empty", "davranışta kural yok", "spec.rules")
    scope = Scope(params=params, variables=spec.get("variables") or {}, roles=roles, platform_type=ptype,
                  allow_expr=True, defer_scenario_refs=True)
    for rk, rule in (spec.get("rules") or {}).items():
        v.ck.rule(rk, rule, jp("spec.rules", rk), scope.with_(used_roles=set()))


@handler("SystemTemplate")
def _system_template(v: Validator, r: Resolved) -> None:
    st = v.ws.resolve_kind(r.spec.get("systemType"), "SystemType", "spec.systemType", allow_abstract=r.abstract)
    if st is None:
        return
    scope = Scope(level="template")
    params = v.ck.values(st.spec.get("parameters"), r.spec.get("parameters"), "spec.parameters", scope,
                         check_required=not r.abstract)
    v.ck.values(st.spec.get("state"), r.spec.get("initialState"), "spec.initialState", scope,
                sources={"parameters": params}, check_required=False)


@handler("PlatformTemplate")
def _platform_template(v: Validator, r: Resolved) -> None:
    v.b.build_platform(r.id, "spec", level="template", require_complete=not r.abstract)


@handler("Scenario")
def _scenario(v: Validator, r: Resolved) -> None:
    if not r.abstract:
        v.b.compile_scenario(r.id)


@handler("MunitionType")
def _munition_type(v: Validator, r: Resolved) -> None:
    v.ck.check_descriptor_map(r.spec.get("parameters"), "spec.parameters")
    v._impl_required(r)


@handler("Munition")
def _munition(v: Validator, r: Resolved) -> None:
    mt = v.ws.resolve_kind(r.spec.get("munitionType"), "MunitionType", "spec.munitionType", allow_abstract=r.abstract)
    if mt is None:
        return
    p = v.ck.values(mt.spec.get("parameters"), r.spec.get("parameters"), "spec.parameters", Scope(level="template"),
                    check_required=not r.abstract)
    if isinstance(p.get("minRange"), (int, float)) and isinstance(p.get("maxRange"), (int, float)) and p["minRange"] >= p["maxRange"]:
        v.d.error("munition.range", "minRange < maxRange olmalı", "spec.parameters.minRange")


@handler("UnitTemplate")
def _unit_template(v: Validator, r: Resolved) -> None:
    spec = r.spec
    if not spec.get("members") and not spec.get("subunits") and not r.abstract:
        v.d.error("unit.empty", "birlikte üye veya alt birlik yok", "spec")
    for key, enum_id, path in (("echelon", "core.enum.echelon", "spec.echelon"),
                               ("type", "core.enum.formation", "spec.formation.type")):
        val = spec.get("echelon") if key == "echelon" else (spec.get("formation") or {}).get("type")
        if val is not None and v.ws.get(enum_id):
            v.ck.value({"type": "enum", "enumRef": enum_id}, val, path, Scope())
    leaders = [k for k, m in (spec.get("members") or {}).items() if m.get("leader")]
    if len(leaders) > 1:
        v.d.error("unit.leader", f"birden fazla lider: {', '.join(leaders)}", "spec.members")
    for mk, m in (spec.get("members") or {}).items():
        p = v.ws.resolve_kind(m.get("platform"), "PlatformTemplate", jp("spec.members", mk, "platform"), allow_abstract=False)
        if p and m.get("overrides"):
            v.b.build_platform(m["platform"], jp("spec.members", mk), patch=m["overrides"], level="instance", require_complete=True)
    for sk, sub in (spec.get("subunits") or {}).items():
        v.ws.resolve_kind(sub.get("unit"), "UnitTemplate", jp("spec.subunits", sk, "unit"), allow_abstract=False)

    def walk(uid: str, stack: tuple) -> None:  # alt birlik döngüsü tespiti
        if uid in stack:
            v.d.error("unit.cycle", f"alt birlik döngüsü: {' -> '.join([*stack, uid])}", "spec.subunits")
            return
        u = v.ws.resolve(uid)
        for sub in ((u.spec.get("subunits") or {}).values() if u else []):
            if sub.get("unit") in v.ws.docs:
                walk(sub["unit"], (*stack, uid))
    walk(r.id, ())
