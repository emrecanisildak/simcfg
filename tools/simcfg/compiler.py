"""Örnekleme (instantiation) ve senaryo derleme.

Derlenmiş çıktı motorun tek girdisidir: tüm kalıtım çözülmüş, birimler tanımlayıcı birimlerine çevrilmiş,
varsayılanlar uygulanmış, roller mount'lara bağlanmış ve kullanılan tip katalogları eklenmiştir.

Katman sırası (soldan sağa, sağdaki kazanır)
-------------------------------------------
Platform   : PlatformTemplate kalıtım zinciri  ->  senaryo varlığı `overrides`
Sistem     : SystemTemplate zinciri  ->  platform mount.parameters/initialState  ->  varlık overrides.systems.<mount>
Davranış   : Behavior zinciri  ->  platform eki (parameters/rules)  ->  senaryo `behaviorOverrides`
             (davranışın soy zincirindeki her id için, genelden özele)  ->  varlık overrides.behaviors.<ek>
Senaryo    : Scenario kalıtım zinciri (varyantlar)
"""
from __future__ import annotations

import copy
import math
from typing import Any

from .checker import Checker, EntityInfo, RoleInfo, ScenarioIndex, Scope
from .diagnostics import Diagnostics, jp
from .merge import MISSING, MergeError, is_directive, merge, merge_all
from .workspace import API_VERSION, Workspace, suggest

COMPILED_API = "simcfg/compiled/v1"
CATALOG_KINDS = ("SystemType", "PlatformType", "MunitionType", "EventType", "ConditionType", "ActionType", "Maneuver",
                 "Enumeration", "AreaType")


class Builder:
    def __init__(self, ws: Workspace, diags: Diagnostics) -> None:
        self.ws = ws
        self.d = diags
        self.ck = Checker(ws, diags)
        self._global_events: set[str] | None = None

    # ------------------------------------------------------------------ yardımcılar
    def _merge(self, base: Any, patch: Any, path: str) -> Any:
        try:
            r = merge(base, patch, path)
            return {} if r is MISSING else r
        except MergeError as e:
            self.d.error("merge", str(e), path)
            return base

    def global_events(self) -> set[str]:
        """Kaynağı scenario/behavior olan olaylar her varlığa ulaşır."""
        if self._global_events is None:
            self._global_events = set()
            for doc in self.ws.by_kind("EventType"):
                r = self.ws.resolve(doc.id)
                if r and r.spec.get("source") in ("scenario", "behavior"):
                    self._global_events.add(doc.id)
        return self._global_events

    def _emits(self, type_id: str | None) -> set[str]:
        r = self.ws.resolve(type_id) if type_id else None
        return set((r.spec.get("emits") if r else None) or [])

    def _placement(self, p: Any, path: str) -> Any:
        if not isinstance(p, dict):
            return p
        out: dict = {}
        if "offset" in p:
            out["offset"] = {k: self.ck.quantity(v, "m", jp(path, "offset", k)) for k, v in p["offset"].items()}
        if "orientation" in p:
            out["orientation"] = {k: self.ck.quantity(v, "deg", jp(path, "orientation", k)) for k, v in p["orientation"].items()}
        return out

    # ------------------------------------------------------------------ sistem
    def build_system(self, key: str, mount: dict, path: str, *, level: str, allowed: list[str] | None,
                     override: dict | None = None) -> dict | None:
        tid = mount.get("template")
        if not tid:
            self.d.error("mount.template", f"'{key}' mount'unda 'template' yok", path)
            return None
        tr = self.ws.resolve_kind(tid, "SystemTemplate", jp(path, "template"), allow_abstract=False)
        if tr is None:
            return None
        st = self.ws.resolve_kind(tr.spec.get("systemType"), "SystemType", jp(path, "template"), allow_abstract=False,
                                  what="SystemType (şablonun systemType alanı)")
        if st is None:
            return None
        if allowed and not any(self.ws.is_a(st.id, a) for a in allowed):
            self.d.error("mount.not_allowed", f"'{st.id}' bu platform tipine monte edilemez (izinli: {', '.join(allowed)})", path)
        if override and isinstance(override.get("parameters"), dict):
            self.ck.check_override_levels(st.spec.get("parameters"), override["parameters"], level, jp(path, "parameters"))
        scope = Scope(level=level)
        raw_params = self._merge(tr.spec.get("parameters") or {}, mount.get("parameters") or {}, jp(path, "parameters"))
        params = self.ck.values(st.spec.get("parameters"), raw_params, jp(path, "parameters"), scope)
        raw_state = self._merge(tr.spec.get("initialState") or {}, mount.get("initialState") or {}, jp(path, "initialState"))
        state = self.ck.values(st.spec.get("state"), raw_state, jp(path, "initialState"), scope, sources={"parameters": params})
        out = {
            "template": tid,
            "lineage": tr.linear,
            "systemType": st.id,
            "typeLineage": st.linear,
            "category": st.spec.get("category"),
            "implementation": st.spec.get("implementation"),
            "parameters": params,
            "initialState": state,
        }
        if "label" in mount:
            out["label"] = mount["label"]
        if "placement" in mount:
            out["placement"] = self._placement(mount["placement"], jp(path, "placement"))
        return out

    # ------------------------------------------------------------------ rol bağlama
    def bind_roles(self, requires: dict, bindings: dict, systems: dict[str, dict], path: str) -> dict[str, RoleInfo]:
        roles: dict[str, RoleInfo] = {}
        for role, req in (requires or {}).items():
            need = req.get("systemType")
            optional = bool(req.get("optional"))
            unbound = RoleInfo(need, None, optional, bound=False)
            if role in bindings:
                mk = bindings[role]
                if mk is None:
                    if not optional:
                        self.d.error("role.unbound", f"zorunlu rol '{role}' null'a bağlanamaz", jp(path, "bindings", role))
                    roles[role] = unbound
                    continue
                if mk not in systems:
                    if optional:
                        self.d.info("role.unbound", f"opsiyonel rol '{role}' -> '{mk}' mount'u yok/takılı değil; ilgili kurallar devre dışı", jp(path, "bindings", role))
                    else:
                        self.d.error("role.unbound", f"rol '{role}' -> '{mk}' mount'u yok veya takılı değil (fitted: false){suggest(mk, systems)}", jp(path, "bindings", role))
                    roles[role] = unbound
                    continue
                if not self.ws.is_a(systems[mk]["systemType"], need):
                    self.d.error("role.type", f"rol '{role}' '{need}' gerektirir; '{mk}' mount'u '{systems[mk]['systemType']}'", jp(path, "bindings", role))
                roles[role] = RoleInfo(systems[mk]["systemType"], mk, optional, True)
                continue
            if role in systems and self.ws.is_a(systems[role]["systemType"], need):
                roles[role] = RoleInfo(systems[role]["systemType"], role, optional, True)
                continue
            cands = [k for k, s in systems.items() if self.ws.is_a(s["systemType"], need)]
            if len(cands) == 1:
                roles[role] = RoleInfo(systems[cands[0]]["systemType"], cands[0], optional, True)
            elif len(cands) > 1:
                self.d.error("role.ambiguous", f"rol '{role}' ({need}) için birden fazla aday mount: {', '.join(cands)}. 'bindings' ile seçin", path)
                roles[role] = unbound
            else:
                if optional:
                    self.d.info("role.unbound", f"opsiyonel rol '{role}' ({need}) için uygun sistem yok; ilgili kurallar devre dışı", path)
                else:
                    self.d.error("role.missing", f"davranış '{role}' rolü için '{need}' tipinde bir sistem gerektiriyor; platformda yok", path)
                roles[role] = unbound
        for b in bindings or {}:
            if b not in (requires or {}):
                self.d.warning("role.unknown_binding", f"davranış '{b}' adlı bir rol beyan etmiyor{suggest(b, requires or {})}", jp(path, "bindings", b))
        return roles

    # ------------------------------------------------------------------ davranış
    def build_behavior(self, key: str, base_att: dict, ent_att: dict | None, path: str, *, ptype: str | None,
                       systems: dict[str, dict], emitted: set[str] | None, level: str,
                       scenario: ScenarioIndex | None, global_overrides: dict | None) -> dict | None:
        ent_att = ent_att or {}
        bid = ent_att.get("behavior") or base_att.get("behavior")
        if not bid:
            self.d.error("behavior.missing", f"'{key}' ekinde 'behavior' kimliği yok", path)
            return None
        br = self.ws.resolve_kind(bid, "Behavior", jp(path, "behavior"), allow_abstract=False)
        if br is None:
            return None
        globs = [(gid, (global_overrides or {})[gid]) for gid in br.linear if gid in (global_overrides or {})]
        enabled = True
        for layer in (base_att, *(g for _, g in globs), ent_att):
            if isinstance(layer.get("enabled"), bool):
                enabled = layer["enabled"]
        if not enabled:
            return {"behavior": bid, "enabled": False}
        spec = br.spec
        req = spec.get("requires") or {}
        if req.get("platformType") and ptype and not self.ws.is_a(ptype, req["platformType"]):
            self.d.error("behavior.platform", f"'{bid}' davranışı '{req['platformType']}' platform tipi gerektirir (bu platform: {ptype})", path)
        bindings = self._merge(base_att.get("bindings") or {}, ent_att.get("bindings") or {}, jp(path, "bindings"))
        roles = self.bind_roles(req.get("systems") or {}, bindings, systems, path)

        pdescs = spec.get("parameters") or {}
        param_layers = [base_att.get("parameters"), *(g.get("parameters") for _, g in globs), ent_att.get("parameters")]
        if level == "instance":
            for gid, g in globs:
                self.ck.check_override_levels(pdescs, g.get("parameters"), "instance", f"spec.behaviorOverrides.{gid}.parameters")
            self.ck.check_override_levels(pdescs, ent_att.get("parameters"), "instance", jp(path, "parameters"))
        if level == "instance" and ent_att.get("behavior") and base_att.get("behavior") and ent_att["behavior"] != base_att["behavior"]:
            self.d.info("behavior.swapped", f"'{key}' eki '{base_att['behavior']}' yerine '{ent_att['behavior']}' kullanıyor", path)
        raw_params = merge_all([p for p in param_layers if p])
        base_scope = Scope(level=level, scenario=scenario, defer_scenario_refs=scenario is None)
        params = self.ck.values(pdescs, raw_params, jp(path, "parameters"), base_scope)
        variables = self.ck.values(spec.get("variables"), {}, jp(path, "variables"), base_scope)

        rules = copy.deepcopy(spec.get("rules") or {})
        for label, layer in [("platform", base_att.get("rules")), *((f"behaviorOverrides.{gid}", g.get("rules")) for gid, g in globs),
                             ("entity", ent_att.get("rules"))]:
            if not layer:
                continue
            for rk in layer:
                if rk not in rules and not is_directive(layer[rk]) and "trigger" not in (layer[rk] or {}):
                    self.d.error("rule.unknown", f"'{bid}' davranışında '{rk}' kuralı yok{suggest(rk, rules)} ({label} katmanı). Yeni kural ise trigger/actions verin", jp(path, "rules", rk))
            rules = self._merge(rules, layer, jp(path, "rules"))

        rule_scope = Scope(params=pdescs, variables=spec.get("variables") or {}, roles=roles, platform_type=ptype,
                           scenario=scenario, allow_expr=True, level=level, defer_scenario_refs=scenario is None,
                           emitted=emitted)
        out_rules = {}
        for rk, rule in rules.items():
            s = rule_scope.with_(used_roles=set())
            cr = self.ck.rule(rk, rule, jp(path, "rules", rk), s)
            if cr is None:
                continue
            unbound = sorted(r for r in s.used_roles if r in roles and not roles[r].bound)
            if unbound and cr.get("enabled", True):
                cr["enabled"] = False
                cr["disabledReason"] = f"bağlanmamış opsiyonel rol(ler): {', '.join(unbound)}"
            never = cr.pop("_neverFires", None)
            if never and cr.get("enabled", True):
                self.d.warning("rule.never_fires", f"'{cr['trigger']['event']}' olayını bu platformda yayan bir sistem/platform yok; kural tetiklenmeyecek", never)
            out_rules[rk] = cr
        return {
            "behavior": bid,
            "lineage": br.linear,
            "enabled": True,
            "bindings": {r: ri.mount for r, ri in roles.items()},
            "parameters": params,
            "variables": variables,
            "arbitration": {"policy": "priority", "preemption": True, **(spec.get("arbitration") or {})},
            "rules": out_rules,
        }

    # ------------------------------------------------------------------ platform
    def effective_platform_spec(self, template_id: str, patch: dict | None, path: str) -> tuple[Any, dict, dict] | None:
        tr = self.ws.resolve(template_id)
        if tr is None:
            return None
        base = tr.spec
        patch = patch or {}
        spec = self._merge(base, {k: v for k, v in patch.items() if k != "behaviors"}, path)
        return tr, base, spec

    def entity_info(self, template_id: str, patch: dict | None, side: str | None) -> EntityInfo | None:
        """Ön geçiş: diğer varlıkların ifadelerde (entities.X...) kullanılabilmesi için hafif bilgi."""
        eff = self.effective_platform_spec(template_id, patch, "")
        if eff is None:
            return None
        _, _, spec = eff
        roles = {}
        for mk, m in (spec.get("systems") or {}).items():
            if not isinstance(m, dict) or m.get("fitted", True) is False:
                continue
            tr = self.ws.resolve(m.get("template") or "")
            roles[mk] = RoleInfo(tr.spec.get("systemType") if tr else None, mk)
        return EntityInfo(spec.get("platformType"), roles, side)

    def build_platform(self, template_id: str, path: str, *, patch: dict | None = None, level: str = "template",
                       scenario: ScenarioIndex | None = None, global_overrides: dict | None = None,
                       require_complete: bool = True) -> dict | None:
        tr = self.ws.resolve_kind(template_id, "PlatformTemplate", path if level == "instance" else None,
                                  allow_abstract=level == "template")
        if tr is None:
            return None
        patch = patch or {}
        if not isinstance(patch, dict):
            self.d.error("overrides.invalid", "overrides bir nesne olmalı", path)
            patch = {}
        _, base, spec = self.effective_platform_spec(template_id, patch, jp(path, "overrides"))
        # Hata yolları: şablon + varlık yaması birleşmiş efektif görünüm üzerinden raporlanır.
        ppath = path
        pt = self.ws.resolve_kind(spec.get("platformType"), "PlatformType", jp(ppath, "platformType"), allow_abstract=False)
        if pt is None:
            return None
        if level == "instance":
            self.ck.check_override_levels(pt.spec.get("parameters"), patch.get("parameters"), level, jp(path, "overrides", "parameters"))
        scope = Scope(level=level)
        params = self.ck.values(pt.spec.get("parameters"), spec.get("parameters"), jp(ppath, "parameters"), scope,
                                check_required=require_complete)
        allowed = pt.spec.get("allowedSystemTypes")
        systems: dict[str, dict] = {}
        unfitted: list[str] = []
        for mk, m in (spec.get("systems") or {}).items():
            if not isinstance(m, dict):
                self.d.error("mount.invalid", "mount bir nesne olmalı", jp(ppath, "systems", mk))
                continue
            if m.get("fitted", True) is False:
                unfitted.append(mk)
                continue
            ov = (patch.get("systems") or {}).get(mk) if level == "instance" else None
            s = self.build_system(mk, m, jp(ppath, "systems", mk), level=level, allowed=allowed,
                                  override=ov if isinstance(ov, dict) else None)
            if s:
                systems[mk] = s
        emitted = self._emits(pt.id) | self.global_events()
        for s in systems.values():
            emitted |= self._emits(s["systemType"])

        behaviors: dict[str, dict] = {}
        base_b = base.get("behaviors") or {}
        ent_b = patch.get("behaviors") or {}
        for bk in list(dict.fromkeys([*base_b, *ent_b])):
            ent_att = ent_b.get(bk)
            if is_directive(ent_att):
                continue  # $delete: varlık seviyesinde davranış kaldırıldı
            b = self.build_behavior(bk, base_b.get(bk) or {}, ent_att, jp(path, "behaviors", bk),
                                    ptype=pt.id, systems=systems, emitted=emitted, level=level,
                                    scenario=scenario, global_overrides=global_overrides)
            if b:
                behaviors[bk] = b

        raw_state = spec.get("initialState") or {}
        state = self.ck.values(pt.spec.get("state"), raw_state, jp(ppath, "initialState"), scope, sources={"parameters": params})
        return {
            "template": template_id,
            "lineage": tr.linear,
            "platformType": pt.id,
            "typeLineage": pt.linear,
            "domain": pt.spec.get("domain"),
            "implementation": pt.spec.get("implementation"),
            "parameters": params,
            "initialState": state,
            "systems": systems,
            "unfittedSystems": unfitted,
            "behaviors": behaviors,
            "appearance": spec.get("appearance") or {},
        }

    # ------------------------------------------------------------------ senaryo
    def compile_scenario(self, scenario_id: str) -> dict | None:
        sr = self.ws.resolve_kind(scenario_id, "Scenario", None, allow_abstract=False)
        if sr is None:
            if scenario_id not in self.ws.docs:
                self.d.error("ref.unknown", f"bilinmeyen senaryo '{scenario_id}'")
            return None
        with self.d.scope(doc=scenario_id, file=sr.doc.location):
            return self._compile(sr)

    def _compile(self, sr) -> dict:
        spec = sr.spec
        P = "spec"
        sides = spec.get("sides") or {}
        areas = spec.get("areas") or {}
        routes = spec.get("routes") or {}
        entities = dict(spec.get("entities") or {})
        generated, units_out = self.expand_units(spec.get("units") or {}, sides, jp(P, "units"))
        for ek, e in generated.items():
            if ek in entities:
                self.d.error("unit.key_collision", f"birlikten üretilen '{ek}' anahtarı açık bir varlıkla çakışıyor", e.get("_path"))
            else:
                entities[ek] = e
        idx = ScenarioIndex(
            sides=set(sides),
            areas={k: (a.get("type") if isinstance(a, dict) else None) for k, a in areas.items()},
            routes=set(routes),
            entities={k: None for k in entities},
        )
        out: dict = {
            "apiVersion": COMPILED_API,
            "sourceApiVersion": API_VERSION,
            "scenario": {"id": sr.id, "lineage": sr.linear, "version": sr.doc.raw.get("version"), "meta": sr.meta},
        }
        # zaman
        t = spec.get("time") or {}
        out["time"] = {
            "start": t.get("start"),
            "duration": self.ck.quantity(t.get("duration", 3600), "s", jp(P, "time", "duration")),
            "timeStep": self.ck.quantity(t.get("timeStep", 0.1), "s", jp(P, "time", "timeStep")),
            "seed": t.get("seed", 0),
        }
        out["environment"] = spec.get("environment") or {}

        # taraflar
        for sk, s in sides.items():
            for other, rel in (s.get("relations") or {}).items():
                if other not in sides:
                    self.d.error("side.unknown", f"bilinmeyen taraf '{other}'{suggest(other, sides)}", jp(P, "sides", sk, "relations", other))
                elif (sides[other].get("relations") or {}).get(sk, rel) != rel:
                    self.d.warning("side.asymmetric", f"'{sk}'->'{other}' {rel}, ters yön farklı", jp(P, "sides", sk, "relations", other))
        out["sides"] = sides

        out["areas"] = {k: self._area(k, a, jp(P, "areas", k), idx) for k, a in areas.items()}
        out["routes"] = {k: self._route(k, r, jp(P, "routes", k), idx) for k, r in routes.items()}

        # ön geçiş: varlık bilgileri
        for ek, e in entities.items():
            if isinstance(e, dict) and isinstance(e.get("platform"), str):
                idx.entities[ek] = self.entity_info(e["platform"], e.get("overrides"), e.get("side"))

        gov = spec.get("behaviorOverrides") or {}
        used_behaviors: set[str] = set()
        out_entities = {}
        for ek, e in entities.items():
            e = dict(e)
            epath = e.pop("_path", None) or jp(P, "entities", ek)
            unit_info = e.pop("_unit", None)
            ce = self._entity(ek, e, epath, idx, gov)
            if ce and unit_info:
                ce["unit"] = unit_info
            if ce:
                out_entities[ek] = ce
                for b in ce["behaviors"].values():
                    used_behaviors.update(self.ws.resolve(b["behavior"]).linear if self.ws.resolve(b["behavior"]) else [])
        out["entities"] = out_entities

        for bid in gov:
            if self.ws.resolve_kind(bid, "Behavior", jp(P, "behaviorOverrides", bid)) and bid not in used_behaviors:
                self.d.warning("behaviorOverrides.unused", f"'{bid}' bu senaryoda hiçbir varlıkta kullanılmıyor", jp(P, "behaviorOverrides", bid))

        groups = spec.get("groups") or {}
        for gk, g in groups.items():
            for i, m in enumerate(g.get("members") or []):
                if m not in entities:
                    self.d.error("group.member", f"bilinmeyen varlık '{m}'{suggest(m, entities)}", jp(P, "groups", gk, "members", i))
        out["groups"] = groups

        vdescs = spec.get("variables") or {}
        self.ck.check_descriptor_map(vdescs, jp(P, "variables"))
        out["variables"] = self.ck.values(vdescs, {}, jp(P, "variables"), Scope(level="instance", scenario=idx))
        rscope = Scope(params={}, variables=vdescs, roles=None, scenario=idx, allow_expr=True, level="instance", no_self=True)
        out["rules"] = {}
        for rk, rule in (spec.get("rules") or {}).items():
            cr = self.ck.rule(rk, rule, jp(P, "rules", rk), rscope.with_(used_roles=set()))
            if cr:
                out["rules"][rk] = cr

        out["units"] = units_out
        out["munitions"] = self.munitions_section(out_entities)
        out["catalog"] = self.catalog_section()
        return out

    def munitions_section(self, entities: dict) -> dict:
        """Yüklü mühimmatların birimleri normalize edilmiş tam tanımları (motor bunları doğrudan kullanır)."""
        used: set[str] = set()
        for e in entities.values():
            for s in e["systems"].values():
                for st in ((s["parameters"].get("loadout") or {}).values() if isinstance(s["parameters"].get("loadout"), dict) else []):
                    if isinstance(st, dict) and isinstance(st.get("munition"), str):
                        used.add(st["munition"])
        out = {}
        for mid in sorted(used):
            mr = self.ws.resolve(mid)
            mt = self.ws.resolve(mr.spec.get("munitionType", "")) if mr else None
            if not mr or not mt:
                continue
            out[mid] = {
                "lineage": mr.linear,
                "munitionType": mt.id,
                "typeLineage": mt.linear,
                "implementation": mt.spec.get("implementation"),
                "parameters": self.ck.values(mt.spec.get("parameters"), mr.spec.get("parameters"), f"munitions.{mid}", Scope()),
            }
        return out

    def _area(self, key: str, a: dict, path: str, idx: ScenarioIndex) -> dict:
        at = self.ws.resolve_kind(a.get("type"), "AreaType", jp(path, "type"), allow_abstract=False)
        g = copy.deepcopy(a.get("geometry") or {})
        shape = g.get("shape")
        if at and at.spec.get("allowedShapes") and shape not in at.spec["allowedShapes"]:
            self.d.error("area.shape", f"'{at.id}' alanı '{shape}' şeklini desteklemiyor (izinli: {', '.join(at.spec['allowedShapes'])})", jp(path, "geometry", "shape"))
        for f in ("radius", "minRadius", "width", "semiMajor", "semiMinor"):
            if f in g:
                g[f] = self.ck.quantity(g[f], "m", jp(path, "geometry", f))
                if isinstance(g[f], (int, float)) and g[f] <= 0 and f != "minRadius":
                    self.d.error("area.geometry", f"{f} > 0 olmalı", jp(path, "geometry", f))
        for f in ("startBearing", "endBearing", "orientation"):
            if f in g:
                g[f] = self.ck.quantity(g[f], "deg", jp(path, "geometry", f))
        geo = {"type": "geoPoint"}
        if "center" in g:
            g["center"] = self.ck.value(geo, g["center"], jp(path, "geometry", "center"), Scope())
        if "points" in g:
            g["points"] = [self.ck.value(geo, p, jp(path, "geometry", "points", i), Scope()) for i, p in enumerate(g["points"])]
        if shape == "sector" and isinstance(g.get("minRadius"), (int, float)) and isinstance(g.get("radius"), (int, float)) and g["minRadius"] >= g["radius"]:
            self.d.error("area.geometry", "minRadius < radius olmalı", jp(path, "geometry"))
        out: dict = {"type": a.get("type"), "typeLineage": at.linear if at else [], "geometry": g, "active": a.get("active", True)}
        if "label" in a:
            out["label"] = a["label"]
        if "altitude" in a:
            alt = dict(a["altitude"])
            for f in ("min", "max"):
                if f in alt:
                    alt[f] = self.ck.quantity(alt[f], "m", jp(path, "altitude", f))
            if isinstance(alt.get("min"), (int, float)) and isinstance(alt.get("max"), (int, float)) and alt["min"] > alt["max"]:
                self.d.error("area.altitude", "altitude.min > altitude.max", jp(path, "altitude"))
            alt.setdefault("reference", "MSL")
            out["altitude"] = alt
        for i, s in enumerate(a.get("sides") or []):
            if s not in idx.sides:
                self.d.error("side.unknown", f"bilinmeyen taraf '{s}'", jp(path, "sides", i))
        out["sides"] = a.get("sides") or []
        out["properties"] = self.ck.values(at.spec.get("properties") if at else {}, a.get("properties") or {},
                                           jp(path, "properties"), Scope(level="instance", scenario=idx))
        return out

    def _route(self, key: str, r: dict, path: str, idx: ScenarioIndex) -> dict:
        scope = Scope(params={}, scenario=idx, allow_expr=True, level="instance", no_self=False)
        wps = []
        ids = set()
        for i, w in enumerate(r.get("waypoints") or []):
            wp = jp(path, "waypoints", i)
            o: dict = {"position": self.ck.value({"type": "geoPoint"}, w.get("position"), jp(wp, "position"), Scope())}
            if "id" in w:
                if w["id"] in ids:
                    self.d.error("route.waypoint", f"tekrarlanan waypoint id '{w['id']}'", jp(wp, "id"))
                ids.add(w["id"])
                o["id"] = w["id"]
            for f, u in (("altitude", "m"), ("speed", "m/s"), ("hold", "s")):
                if f in w:
                    o[f] = self.ck.quantity(w[f], u, jp(wp, f))
            if "actions" in w:
                o["actions"] = [self.ck.invocation(a, jp(wp, "actions", j), scope) for j, a in enumerate(w["actions"])]
            wps.append(o)
        return {"label": r.get("label"), "loop": bool(r.get("loop", False)), "waypoints": wps}

    def _entity(self, key: str, e: dict, path: str, idx: ScenarioIndex, gov: dict) -> dict | None:
        if not isinstance(e, dict):
            return None
        if e.get("side") not in idx.sides:
            self.d.error("side.unknown", f"bilinmeyen taraf '{e.get('side')}'{suggest(e.get('side'), idx.sides)}", jp(path, "side"))
        plat = self.build_platform(e.get("platform"), path, patch=e.get("overrides"), level="instance",
                                   scenario=idx, global_overrides=gov, require_complete=True)
        if plat is None:
            if e.get("platform") not in self.ws.docs:
                pass  # resolve_kind zaten raporladı
            return None
        state = dict(plat["initialState"])
        init = e.get("initial") or {}
        pt = self.ws.resolve(plat["platformType"])
        sdescs = (pt.spec.get("state") if pt else None) or {}
        sc = Scope(level="instance", scenario=idx)
        init_state = {k: init[k] for k in ("position", "altitude", "heading", "speed") if k in init}
        init_state.update(init.get("state") or {})
        state.update(self.ck.values(sdescs, init_state, jp(path, "initial"), sc, apply_defaults=False, check_required=False))
        if "route" in init:
            rk = init["route"]
            if rk not in idx.routes:
                self.d.error("ref.scenario", f"senaryoda '{rk}' rotası yok{suggest(rk, idx.routes)}", jp(path, "initial", "route"))
        if "position" not in state and "route" not in init:
            self.d.error("entity.position", "başlangıç konumu yok: initial.position veya initial.route verin", jp(path, "initial"))
        info = idx.entities.get(key)
        pscope = Scope(params={}, roles=info.roles if info else None, platform_type=plat["platformType"],
                       scenario=idx, allow_expr=True, level="instance")
        plan = [self.ck.invocation(a, jp(path, "plan", i), pscope.with_(used_roles=set())) for i, a in enumerate(e.get("plan") or [])]
        out = {
            "side": e.get("side"),
            "enabled": e.get("enabled", True),
            **{k: e[k] for k in ("label", "callsign", "tags") if k in e},
            **plat,
            "initialState": state,
            "plan": plan,
        }
        if "route" in init:
            out["initialRoute"] = init["route"]
        return out

    # ------------------------------------------------------------------ birlik açma
    def expand_units(self, instances: dict, sides: dict, path: str) -> tuple[dict, dict]:
        """Senaryo birlik örneklerini formasyon konumlarıyla varlıklara açar."""
        ents: dict = {}
        units: dict = {}
        for uk, inst in instances.items():
            ip = jp(path, uk)
            if not isinstance(inst, dict) or inst.get("enabled", True) is False:
                continue
            ut = self.ws.resolve_kind(inst.get("template"), "UnitTemplate", jp(ip, "template"), allow_abstract=False)
            if ut is None:
                continue
            if inst.get("side") not in sides:
                self.d.error("side.unknown", f"bilinmeyen taraf '{inst.get('side')}'{suggest(inst.get('side'), sides)}", jp(ip, "side"))
            pos = self.ck.value({"type": "geoPoint"}, inst.get("position"), jp(ip, "position"), Scope())
            heading = self.ck.quantity(inst.get("heading", 0), "deg", jp(ip, "heading"))
            if not isinstance(pos, dict) or not isinstance(heading, (int, float)):
                continue
            formation = self._merge(ut.spec.get("formation") or {}, inst.get("formation") or {}, jp(ip, "formation"))
            before = set(ents)
            self._expand(ut, uk, uk, None, inst, pos, float(heading), formation, inst.get("callsign") or uk.upper(), ents, units, ip, 0)
            produced = set(ents) - before
            for ok in (inst.get("overrides") or {}):
                if ok not in produced:
                    self.d.error("unit.override_key", f"'{uk}' birliği '{ok}' adlı bir varlık üretmiyor{suggest(ok, produced)}. Üretilenler: {', '.join(sorted(produced))}",
                                 jp(ip, "overrides", ok))
        return ents, units

    def _expand(self, ut, key: str, root: str, parent: str | None, inst: dict, pos: dict, heading: float, formation: dict,
                callsign: str, ents: dict, units: dict, path: str, depth: int) -> None:
        if depth > 8:
            self.d.error("unit.depth", "birlik iç içeliği 8 seviyeyi aştı (döngü?)", path)
            return
        spacing = self.ck.quantity(formation.get("spacing", 50), "m", jp(path, "formation", "spacing"))
        spacing = spacing if isinstance(spacing, (int, float)) else 50.0
        ftype = formation.get("type", "line")
        slots: list[tuple] = []
        for mk, m in (ut.spec.get("members") or {}).items():
            n = int(m.get("count", 1))
            slots += [("member", mk, i, n, m) for i in range(n)]
        for sk, sub in (ut.spec.get("subunits") or {}).items():
            n = int(sub.get("count", 1))
            slots += [("sub", sk, i, n, sub) for i in range(n)]
        lead_slot = next((sl for sl in slots if sl[0] == "member" and sl[4].get("leader")), next((sl for sl in slots if sl[0] == "member"), None))
        name = lambda base, i, n: f"{base}_{i + 1}" if n > 1 else base
        leader_key = f"{key}_{name(lead_slot[1], lead_slot[2], lead_slot[3])}" if lead_slot else None
        rec = {"template": ut.id, "lineage": ut.linear, "echelon": ut.spec.get("echelon"), "side": inst.get("side"),
               "parent": parent, "members": [], "subunits": [], "leader": leader_key, "formation": {"type": ftype, "spacing": spacing}}
        units[key] = rec
        plan_for = inst.get("planFor", "all")
        for idx, (kind, sk, i, n, d) in enumerate(slots):
            x, y = formation_offset(ftype, idx, len(slots), spacing)
            p = offset_geo(pos, heading, x, y)
            if kind == "member":
                ek = f"{key}_{name(sk, i, n)}"
                if not d.get("platform"):
                    self.d.error("unit.member", f"'{sk}' üyesinde platform yok", jp(path, "members", sk))
                    continue
                patch = merge_all([d.get("overrides"), inst.get("allMembers"), (inst.get("overrides") or {}).get(ek)])
                initial = {"position": p, "heading": heading, **{k: v for k, v in (inst.get("initial") or {}).items()}}
                e = {"platform": d["platform"], "side": inst.get("side"), "callsign": f"{callsign} {idx + 1}", "initial": initial,
                     "_path": f"{path} → {ek}",
                     "_unit": {"instance": key, "root": root, "template": ut.id, "member": sk, "role": d.get("role"),
                               "leader": ek == leader_key, "leaderEntity": leader_key}}
                if patch:
                    e["overrides"] = patch
                if inst.get("plan") and (plan_for == "all" or (ek == leader_key and parent is None)):
                    e["plan"] = copy.deepcopy(inst["plan"])
                ents[ek] = e
                rec["members"].append(ek)
            else:
                sut = self.ws.resolve_kind(d.get("unit"), "UnitTemplate", jp(path, "subunits", sk, "unit"), allow_abstract=False)
                if sut is None:
                    continue
                sub_key = f"{key}_{name(sk, i, n)}"
                self._expand(sut, sub_key, root, key, inst, p, heading, sut.spec.get("formation") or {}, f"{callsign}-{idx + 1}",
                             ents, units, jp(path, "subunits", sk), depth + 1)
                rec["subunits"].append(sub_key)

    # ------------------------------------------------------------------ katalog
    def catalog_section(self) -> dict:
        cat: dict = {}
        for kind in CATALOG_KINDS:
            items = {}
            for doc in sorted(self.ws.by_kind(kind), key=lambda d: d.id):
                r = self.ws.resolve(doc.id)
                if r and not r.abstract:
                    items[doc.id] = {"lineage": r.linear, "spec": r.spec}
            cat[kind] = items
        return cat


def formation_offset(ftype: str, i: int, n: int, s: float) -> tuple[float, float]:
    """Formasyon içi yerel ofset (x: sağ, y: ileri), metre. Lider i=0."""
    if ftype in ("column", "file", "trail"):
        return 0.0, -i * s
    if ftype in ("line", "wall"):
        return (i - (n - 1) / 2) * s, 0.0
    if ftype in ("wedge", "vee"):
        if i == 0:
            return 0.0, 0.0
        side = -1 if i % 2 else 1
        rank = (i + 1) // 2
        return side * rank * s, (-1 if ftype == "wedge" else 1) * rank * s
    if ftype == "echelon_right":
        return i * s, -i * s
    if ftype == "echelon_left":
        return -i * s, -i * s
    if ftype in ("diamond", "box"):
        pat = [(0, 0), (-s, -s), (s, -s), (0, -2 * s)] if ftype == "diamond" else [(0, 0), (s, 0), (0, -s), (s, -s)]
        bx, by = pat[i % 4]
        return float(bx), float(by - (i // 4) * 2.5 * s)
    cols = max(1, math.ceil(math.sqrt(n)))  # spread / bilinmeyen
    return (i % cols) * s, -(i // cols) * s


def offset_geo(pos: dict, heading_deg: float, x: float, y: float) -> dict:
    h = math.radians(heading_deg)
    east = x * math.cos(h) + y * math.sin(h)
    north = -x * math.sin(h) + y * math.cos(h)
    lat = pos["lat"] + north / 111320.0
    lon = pos["lon"] + east / (111320.0 * math.cos(math.radians(pos["lat"])))
    out = {"lat": round(lat, 6), "lon": round(lon, 6)}
    if "alt" in pos:
        out["alt"] = pos["alt"]
    return out
