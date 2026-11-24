"""Referans kural motoru: derlenmiş senaryoyu (simcfg/compiled/v1) yükler, olayları kurallara dağıtır,
öncelik/kanal hakemliği yapar ve aksiyonları çalıştırır.

Bu bir *çalıştırılabilir şartname*dir, simülasyon motoru değildir: kinematik/RF/güdüm fiziği yoktur.
Sistem komutlarının etkileri (sayaçlar, modlar, olay üretimi) sözleşmeyi göstermek için basitçe modellenmiştir.
Gerçek motor aynı tetik/koşul/hakemlik semantiğini uygulamalı; komut etkilerini kendi eklentileriyle gerçekleştirmelidir.

Sözleşme kuralları (motorlar için bağlayıcı)
--------------------------------------------
R1  Olay hedefleri: kaynağı 'scenario'/'behavior' olan olaylar tüm varlıklara + senaryo kurallarına; diğerleri
    sadece olayı üreten varlığa ve senaryo kurallarına gider.
R2  systemRole tipindeki payload alanları bir davranışa teslim edilirken mount adından o davranışın rol adına çevrilir
    (bağlı değilse mount adı kalır). Senaryo kurallarına mount adı gider.
R3  Her olaya örtük 'entity' (kaynak varlık) ve 'time' alanları eklenir.
R4  Kural uygunluğu sırası: enabled -> maxFires -> cooldown -> trigger.filter -> condition.
R5  Aynı anda uygun kurallar önceliğe göre (büyükten küçüğe), eşitlikte tanım sırasına göre çalışır.
    arbitration.policy='first' ise davranış başına sadece ilki çalışır.
R6  Kanal hakemliği: süreli (durative) aksiyon içeren kural, aksiyon kanallarını öncelik ile kilitler. Daha yüksek
    veya eşit öncelikli bir sahibin tuttuğu kanala ihtiyaç duyan kural engellenir; daha düşük öncelikli sahip
    preemption=true ise kesilir.
R7  'every' tetikleyici adım başına en fazla bir kez; 'at' tetikleyici (önceki, şimdiki] aralığında bir kez;
    'edge' tetikleyici başlangıç değeri false kabul edilerek her adımda değerlendirilir.
R8  Aksiyon parametreleri çağrı anında değerlendirilir; action/actionList/expression tipleri tembel kalır.
    composite gövdedeki {param} referansları aksiyonun değerlendirilmiş parametrelerine çözülür.
R9  systemRole değerleri davranış bağlamında bindings ile mount'a çevrilir; plan/senaryo/`on` bağlamında mount adıdır.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from . import geo
from .expr import evaluate, is_expr, truthy


# ============================================================================ çalışma anı nesneleri
class SystemRT:
    def __init__(self, mount: str, data: dict) -> None:
        self.mount = mount
        self.data = data
        self.params = data["parameters"]
        self.state = copy.deepcopy(data["initialState"])
        self.types = list(reversed(data["typeLineage"]))  # en özelden genele
        lo = self.params.get("loadout")
        self.inventory = {k: {"munition": v["munition"], "count": v["count"]} for k, v in lo.items()} if isinstance(lo, dict) else {}

    def is_a(self, t: str) -> bool:
        return t in self.types

    def ready(self) -> bool:
        return self.state.get("power", True) is not False and self.state.get("operational", True) is not False


@dataclass
class RuleRT:
    key: str
    data: dict
    enabled: bool
    order: int
    fires: int = 0
    last: float | None = None
    last_cond: bool = False


class BehaviorRT:
    def __init__(self, key: str, data: dict) -> None:
        self.key = key
        self.data = data
        self.params = copy.deepcopy(data["parameters"])
        self.vars = copy.deepcopy(data["variables"])
        self.bindings: dict[str, str | None] = data["bindings"]
        self.reverse = {m: r for r, m in self.bindings.items() if m}
        self.policy = data["arbitration"]["policy"]
        self.preemption = data["arbitration"]["preemption"]
        self.rules = {k: RuleRT(k, r, r["enabled"], i) for i, (k, r) in enumerate(data["rules"].items())}


class EntityRT:
    def __init__(self, key: str, data: dict, routes: dict) -> None:
        self.key = key
        self.data = data
        self.side = data["side"]
        self.domain = data.get("domain")
        self.state = copy.deepcopy(data["initialState"])
        if "position" not in self.state and data.get("initialRoute") in routes:
            wp = routes[data["initialRoute"]]["waypoints"][0]
            self.state["position"] = wp["position"]
            self.state.setdefault("altitude", wp.get("altitude", 0))
        self.systems = {m: SystemRT(m, s) for m, s in data["systems"].items()}
        self.behaviors = {k: BehaviorRT(k, b) for k, b in data["behaviors"].items() if b.get("enabled")}
        self.channels: dict[str, dict] = {}
        self.last_under_fire: float | None = None
        self.unit = data.get("unit")

    @property
    def position(self) -> dict | None:
        return self.state.get("position")

    @property
    def altitude(self) -> float:
        return self.state.get("altitude") or 0.0


# ============================================================================ değerlendirme bağlamı
class Ctx:
    def __init__(self, world: "World", entity: EntityRT | None = None, behavior: BehaviorRT | None = None,
                 event: dict | None = None, params: dict | None = None, owner: str = "") -> None:
        self.world, self.entity, self.behavior, self.event = world, entity, behavior, event
        self.params = params if params is not None else (behavior.params if behavior else {})
        self.owner = owner

    def with_(self, **kw: Any) -> "Ctx":
        c = Ctx(self.world, self.entity, self.behavior, self.event, self.params, self.owner)
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def mount_for(self, value: Any) -> Any:
        if self.behavior and value in self.behavior.bindings:
            return self.behavior.bindings[value]
        return value

    # EvalContext arayüzü
    def param(self, name: str) -> Any:
        return self.params.get(name)

    def condition(self, cond_id: str, params: dict) -> Any:
        return self.world.eval_condition(cond_id, params, self)

    def lookup(self, path: str) -> Any:
        parts = path.split(".")
        root = parts[0]
        if root == "self":
            return self._entity_path(self.entity, parts[1:], use_bindings=True)
        if root == "entities":
            return self._entity_path(self.world.entities.get(parts[1]) if len(parts) > 1 else None, parts[2:], False)
        if root == "event":
            return (self.event or {}).get(parts[1]) if len(parts) > 1 else self.event
        if root == "bb":
            store = self.behavior.vars if self.behavior else self.world.vars
            return store.get(parts[1]) if len(parts) > 1 else None
        if root == "param":
            return self.param(parts[1]) if len(parts) > 1 else None
        if root == "scenario":
            return self.world.time if len(parts) > 1 and parts[1] in ("time", "elapsed") else None
        return None

    def _entity_path(self, ent: EntityRT | None, parts: list[str], use_bindings: bool) -> Any:
        if ent is None:
            return None
        if not parts:
            return ent.key
        head = parts[0]
        if head == "id":
            return ent.key
        if head in ("side", "callsign"):
            return ent.side if head == "side" else ent.data.get("callsign")
        if head == "params":
            return ent.data["parameters"].get(parts[1]) if len(parts) > 1 else None
        if head == "systems":
            if len(parts) < 2:
                return None
            mount = self.mount_for(parts[1]) if use_bindings else parts[1]
            sys_ = ent.systems.get(mount) if mount else None
            if len(parts) == 2:
                return mount if sys_ else None
            if parts[2] == "fitted":
                return sys_ is not None
            if sys_ is None or len(parts) < 4:
                return None
            return (sys_.state if parts[2] == "state" else sys_.params).get(parts[3])
        return ent.state.get(head)


# ============================================================================ dünya
@dataclass
class Record:
    t: float
    type: str
    entity: str | None = None
    owner: str | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"t": round(self.t, 6), "type": self.type}
        if self.entity:
            d["entity"] = self.entity
        if self.owner:
            d["owner"] = self.owner
        d.update(self.detail)
        return d


class World:
    def __init__(self, compiled: dict) -> None:
        if compiled.get("apiVersion") != "simcfg/compiled/v1":
            raise ValueError(f"desteklenmeyen derlenmiş format: {compiled.get('apiVersion')}")
        self.c = compiled
        cat = compiled["catalog"]
        self.actions = {k: v["spec"] for k, v in cat.get("ActionType", {}).items()}
        self.conds = {k: v["spec"] for k, v in cat.get("ConditionType", {}).items()}
        self.events = {k: v["spec"] for k, v in cat.get("EventType", {}).items()}
        self.munitions = compiled.get("munitions", {})
        self.areas = compiled["areas"]
        self.routes = compiled["routes"]
        self.sides = compiled["sides"]
        self.time = 0.0
        self.rng = random.Random(compiled["time"].get("seed", 0))
        self.vars = copy.deepcopy(compiled["variables"])
        self.entities = {k: EntityRT(k, e, self.routes) for k, e in compiled["entities"].items() if e.get("enabled", True)}
        self.scenario_rules = {k: RuleRT(k, r, r["enabled"], i) for i, (k, r) in enumerate(compiled["rules"].items())}
        self.log: list[Record] = []
        self.cond_handlers: dict[str, Callable] = dict(DEFAULT_CONDITIONS)
        self.action_handlers: dict[str, Callable] = dict(DEFAULT_ACTIONS)
        self.command_handlers: dict[tuple[str, str], Callable] = dict(DEFAULT_COMMANDS)
        self._queue: list[tuple[str, str | None, dict]] = []
        self._dispatching = False
        self._prev = -1e-9

    # ------------------------------------------------------------------ kayıt
    def record(self, type_: str, ctx: Ctx | None = None, **detail: Any) -> None:
        self.log.append(Record(self.time, type_, ctx.entity.key if ctx and ctx.entity else None,
                               ctx.owner if ctx else None, detail))

    # ------------------------------------------------------------------ ilişkiler
    def relation(self, a: str | None, b: str | None) -> str:
        if a is None or b is None:
            return "neutral"
        if a == b:
            return "friendly"
        return (self.sides.get(a, {}).get("relations") or {}).get(b, "neutral")

    # ------------------------------------------------------------------ yaşam döngüsü
    def start(self) -> None:
        self.time = 0.0
        self.record("scenario.start")
        for ent in self.entities.values():
            if ent.data["plan"]:
                ctx = Ctx(self, ent, None, None, {}, owner="plan")
                self._run_actions(ent.data["plan"], ctx, priority=0, owner="plan", entity=ent, preemption=True)
        self.emit("core.event.scenario.started", None, {})
        self.advance(0.0)

    def advance(self, t: float) -> None:
        """Zamanı t'ye ilerlet: at/every/edge tetikleyicilerini değerlendir (R7)."""
        prev, self.time = self._prev, t
        cands = []
        for ent in self.entities.values():
            for b in ent.behaviors.values():
                for r in b.rules.values():
                    if self._timed_due(r, prev, t, Ctx(self, ent, b)):
                        cands.append((ent, b, r, Ctx(self, ent, b, owner=f"{b.key}.{r.key}")))
        for r in self.scenario_rules.values():
            if self._timed_due(r, prev, t, Ctx(self)):
                cands.append((None, None, r, Ctx(self, owner=f"scenario.{r.key}")))
        self._prev = t
        self._fire(cands, check_condition=True)
        self._drain()

    def _timed_due(self, r: RuleRT, prev: float, t: float, ctx: Ctx) -> bool:
        trig = r.data["trigger"]
        if "at" in trig:
            return prev < trig["at"] <= t
        if "every" in trig:
            off, iv = trig.get("offset", 0.0), trig["every"]
            if t < off:
                return False
            return prev < off or int((t - off) // iv) > int((prev - off) // iv)
        if "edge" in trig:
            if not r.enabled:
                return False
            cur = truthy(evaluate(r.data.get("condition", True), ctx))
            was, r.last_cond = r.last_cond, cur
            e = trig["edge"]
            return (cur and not was and e in ("rising", "both")) or (was and not cur and e in ("falling", "both"))
        return False

    def emit(self, event_id: str, entity_key: str | None, payload: dict) -> None:
        self._queue.append((event_id, entity_key, payload))
        if not self._dispatching:
            self._drain()

    def _drain(self) -> None:
        self._dispatching = True
        try:
            guard = 0
            while self._queue:
                guard += 1
                if guard > 10000:
                    raise RuntimeError("olay döngüsü: 10000 olay sınırı aşıldı")
                self._dispatch(*self._queue.pop(0))
        finally:
            self._dispatching = False

    def _dispatch(self, event_id: str, entity_key: str | None, payload: dict) -> None:
        spec = self.events.get(event_id, {})
        base = {**payload, "entity": entity_key, "time": self.time}
        self.record("event", Ctx(self, self.entities.get(entity_key)) if entity_key else None, event=event_id,
                    payload={k: v for k, v in payload.items()})
        if event_id == "core.event.platform.under_fire" and entity_key in self.entities:
            self.entities[entity_key].last_under_fire = self.time
        broadcast = spec.get("source") in ("scenario", "behavior") or entity_key is None
        targets = list(self.entities.values()) if broadcast else [self.entities[entity_key]] if entity_key in self.entities else []
        role_fields = [k for k, d in (spec.get("payload") or {}).items() if d.get("type") == "systemRole"]
        cands = []
        for ent in targets:
            for b in ent.behaviors.values():
                ev = dict(base)
                for f in role_fields:  # R2
                    if ev.get(f) in b.reverse:
                        ev[f] = b.reverse[ev[f]]
                for r in b.rules.values():
                    if r.data["trigger"].get("event") == event_id:
                        cands.append((ent, b, r, Ctx(self, ent, b, ev, owner=f"{b.key}.{r.key}")))
        for r in self.scenario_rules.values():
            if r.data["trigger"].get("event") == event_id:
                cands.append((None, None, r, Ctx(self, None, None, dict(base), {}, owner=f"scenario.{r.key}")))
        self._fire(cands, check_condition=True)

    # ------------------------------------------------------------------ kural ateşleme
    def _eligible(self, r: RuleRT, ctx: Ctx) -> bool:
        d = r.data
        if not r.enabled:
            return False
        if d.get("maxFires") is not None and r.fires >= d["maxFires"]:
            return False
        if d.get("cooldown") and r.last is not None and self.time - r.last < d["cooldown"]:
            return False
        trig = d["trigger"]
        if "filter" in trig and not truthy(evaluate(trig["filter"], ctx)):
            return False
        if "edge" in trig:
            return True  # koşul zaten kenar tespitinde değerlendirildi
        return truthy(evaluate(d["condition"], ctx)) if "condition" in d else True

    def _fire(self, cands: list, check_condition: bool) -> None:
        ok = [(e, b, r, c) for e, b, r, c in cands if self._eligible(r, c)]
        ok.sort(key=lambda x: (-x[2].data["priority"], x[2].order))  # R5
        done_first: set[int] = set()
        for ent, b, r, ctx in ok:
            if b is not None and b.policy == "first":
                if id(b) in done_first:
                    continue
                done_first.add(id(b))
            if not r.enabled:  # aynı dağıtımda önceki bir kural kapatmış olabilir
                continue
            self.record("rule.fire", ctx, rule=r.key, priority=r.data["priority"])
            fired = self._run_actions(r.data["actions"], ctx, priority=r.data["priority"], owner=ctx.owner,
                                      entity=ent, preemption=b.preemption if b else True)
            if fired:
                r.fires += 1
                r.last = self.time

    def _channels(self, inv: dict) -> tuple[set[str], bool]:
        spec = self.actions.get(inv.get("action"), {})
        chans, durative = set(spec.get("channels") or []), bool(spec.get("durative"))
        impl = spec.get("implementation") or {}
        nested: list = []
        if impl.get("type") == "composite":
            nested.append(impl["body"])
        p = inv.get("params") or {}
        for k, d in (spec.get("parameters") or {}).items():
            if d.get("type") == "action" and isinstance(p.get(k), dict):
                nested.append(p[k])
            elif d.get("type") == "actionList" and isinstance(p.get(k), list):
                nested.extend(p[k])
        for n in nested:
            c, du = self._channels(n)
            chans |= c
            durative = durative or du
        return chans, durative

    def _run_actions(self, actions: list, ctx: Ctx, *, priority: float, owner: str, entity: EntityRT | None,
                     preemption: bool) -> bool:
        if entity is not None:
            need: set[str] = set()
            claim = False
            for a in actions:
                c, du = self._channels(a)
                need |= c
                claim = claim or (du and bool(c))
            for ch in sorted(need):  # R6
                h = entity.channels.get(ch)
                if h and h["owner"] != owner:
                    if h["priority"] >= priority or not preemption:
                        self.record("rule.blocked", ctx, channel=ch, heldBy=h["owner"])
                        return False
            for ch in sorted(need):
                h = entity.channels.get(ch)
                if h and h["owner"] != owner:
                    self.record("channel.preempted", ctx, channel=ch, previous=h["owner"])
            if claim:
                for ch in need:
                    entity.channels[ch] = {"owner": owner, "priority": priority}
        for a in actions:
            if not self.execute(a, ctx) and a.get("onFailure", "abort") == "abort":
                break
        return True

    def complete(self, entity_key: str, channel: str) -> None:
        """Süreli bir aksiyonun bittiğini bildirir (gerçek motorda dinamik model bildirir)."""
        ent = self.entities.get(entity_key)
        if ent and channel in ent.channels:
            h = ent.channels.pop(channel)
            self.record("channel.released", Ctx(self, ent), channel=channel, owner_=h["owner"])

    # ------------------------------------------------------------------ aksiyon
    def execute(self, inv: dict, ctx: Ctx) -> bool:
        spec = self.actions.get(inv["action"])
        if spec is None:
            self.record("action.error", ctx, action=inv["action"], error="katalogda yok")
            return False
        if inv.get("on"):
            ent = self.entities.get(inv["on"])
            if ent is None:
                self.record("action.error", ctx, action=inv["action"], error=f"varlık yok: {inv['on']}")
                return False
            ctx = ctx.with_(entity=ent, behavior=None, params={})
        if "when" in inv and not truthy(evaluate(inv["when"], ctx)):
            self.record("action.skipped", ctx, action=inv["action"], reason="when=false")
            return True
        descs = spec.get("parameters") or {}
        params = {k: (v if descs.get(k, {}).get("type") in ("action", "actionList", "expression") else evaluate(v, ctx))
                  for k, v in (inv.get("params") or {}).items()}  # R8
        impl = spec.get("implementation") or {}
        kind = impl.get("type")
        if kind == "composite":
            self.record("action", ctx, action=inv["action"], params=_printable(params, descs))
            return self.execute(impl["body"], ctx.with_(params=params))
        if kind == "command":
            mount = ctx.mount_for(params.get(impl["system"]))
            args = {k: evaluate(v, ctx.with_(params=params)) for k, v in (impl.get("args") or {}).items()}
            self.record("action", ctx, action=inv["action"], params=_printable(params, descs))
            return self.command(ctx, mount, impl["command"], args)
        handler = self.action_handlers.get(impl.get("handler"))
        self.record("action", ctx, action=inv["action"], params=_printable(params, descs))
        if handler is None:
            return True  # motor eklentisi gerektiren native aksiyon: referans çalışma zamanında sadece kayıt
        return handler(self, ctx, params) is not False

    def command(self, ctx: Ctx, mount: Any, name: str, args: dict) -> bool:
        ent = ctx.entity
        sys_ = ent.systems.get(mount) if ent and isinstance(mount, str) else None
        if sys_ is None:
            self.record("command.failed", ctx, system=mount, command=name, error="sistem yok/takılı değil")
            return False
        if not sys_.ready() and name != "setPower":
            self.record("command.failed", ctx, system=mount, command=name, error="sistem enerjisiz/arızalı")
            return False
        for t in sys_.types:
            h = self.command_handlers.get((t, name))
            if h:
                before = copy.deepcopy(sys_.state)
                ok = h(self, ctx, sys_, args)
                diff = {k: [before.get(k), v] for k, v in sys_.state.items() if before.get(k) != v}
                self.record("command" if ok is not False else "command.failed", ctx, system=mount, command=name,
                            args=args, **({"stateChange": diff} if diff else {}))
                return ok is not False
        self.record("command", ctx, system=mount, command=name, args=args, note="eklentiye iletildi (referans etkisi yok)")
        return True

    # ------------------------------------------------------------------ koşul
    def eval_condition(self, cond_id: str, params: dict, ctx: Ctx) -> Any:
        spec = self.conds.get(cond_id)
        if spec is None:
            return False
        full = {k: d.get("default") for k, d in (spec.get("parameters") or {}).items() if "default" in d}
        full.update(params)
        impl = spec.get("implementation") or {}
        if impl.get("type") == "expression":
            return evaluate(impl["body"], ctx.with_(params=full))
        h = self.cond_handlers.get(impl.get("handler"))
        return h(self, ctx, full) if h else False

    # ------------------------------------------------------------------ yardımcı
    def snapshot(self) -> dict:
        return {
            "time": self.time,
            "vars": self.vars,
            "entities": {
                k: {
                    "state": e.state,
                    "systems": {m: {"state": s.state, **({"inventory": s.inventory} if s.inventory else {})} for m, s in e.systems.items()},
                    "behaviors": {bk: {"vars": b.vars, "rules": {rk: {"enabled": r.enabled, "fires": r.fires} for rk, r in b.rules.items()}}
                                  for bk, b in e.behaviors.items()},
                    "channels": e.channels,
                } for k, e in self.entities.items()
            },
        }


def _printable(params: dict, descs: dict) -> dict:
    out = {}
    for k, v in params.items():
        t = descs.get(k, {}).get("type")
        if t == "actionList":
            out[k] = [x.get("action", "?").split(".")[-1] for x in v] if isinstance(v, list) else v
        elif t == "action":
            out[k] = v.get("action", "?").split(".")[-1] if isinstance(v, dict) else v
        elif v is not None:
            out[k] = v
    return out


# ============================================================================ varsayılan handler'lar
def _target(world: World, ctx: Ctx, key: Any) -> EntityRT | None:
    if key is None:
        return ctx.entity
    return world.entities.get(key) if isinstance(key, str) else None


def c_in_area(w: World, ctx: Ctx, p: dict) -> bool:
    ent = _target(w, ctx, p.get("entity"))
    area = w.areas.get(p.get("area"))
    return bool(ent and area and geo.in_area(ent.position, ent.altitude, area))


def c_hostile_in_area(w: World, ctx: Ctx, p: dict) -> bool:
    area = w.areas.get(p.get("area"))
    if not area or not ctx.entity:
        return False
    n = sum(1 for e in w.entities.values()
            if e.state.get("alive", True) and w.relation(ctx.entity.side, e.side) == "hostile"
            and (p.get("domain", "any") == "any" or e.domain == p["domain"])
            and geo.in_area(e.position, e.altitude, area))
    return n >= p.get("minCount", 1)


def c_within_range(w: World, ctx: Ctx, p: dict) -> bool:
    t = _target(w, ctx, p.get("target"))
    s = ctx.entity
    if not (t and s and t.position and s.position):
        return False
    return geo.distance(s.position, t.position, s.altitude, t.altitude) <= p.get("range", 0)


def c_system_ready(w: World, ctx: Ctx, p: dict) -> bool:
    s = ctx.entity.systems.get(ctx.mount_for(p.get("system"))) if ctx.entity else None
    return bool(s and s.ready())


def c_cm_available(w: World, ctx: Ctx, p: dict) -> bool:
    s = ctx.entity.systems.get(ctx.mount_for(p.get("system"))) if ctx.entity else None
    return bool(s and s.ready() and s.state.get("armed", True) and (s.state.get("remaining") or 0) >= p.get("minCount", 1))


def c_random(w: World, ctx: Ctx, p: dict) -> bool:
    return w.rng.random() < p.get("probability", 0)


def _launcher(ctx: Ctx, p: dict) -> SystemRT | None:
    return ctx.entity.systems.get(ctx.mount_for(p.get("launcher"))) if ctx.entity else None


def c_has_munition(w: World, ctx: Ctx, p: dict) -> bool:
    s = _launcher(ctx, p)
    if not s:
        return False
    n = sum(st["count"] for st in s.inventory.values() if p.get("munition") in (None, st["munition"]))
    return n >= p.get("minCount", 1)


def c_target_in_weapon_range(w: World, ctx: Ctx, p: dict) -> bool:
    s, t, me = _launcher(ctx, p), _target(w, ctx, p.get("target")), ctx.entity
    if not (s and t and me and t.position and me.position):
        return False
    d = geo.distance(me.position, t.position, me.altitude, t.altitude)
    for st in s.inventory.values():
        if st["count"] > 0 and p.get("munition") in (None, st["munition"]):
            mp = (w.munitions.get(st["munition"]) or {}).get("parameters") or {}
            if mp.get("minRange", 0) <= d <= mp.get("maxRange", 0) and t.domain in (mp.get("targetDomains") or [t.domain]):
                return True
    return False


def c_target_identity(w: World, ctx: Ctx, p: dict) -> bool:
    t = _target(w, ctx, p.get("target"))
    if t is None or ctx.entity is None:
        return p.get("identity") == "unknown"
    rel = w.relation(ctx.entity.side, t.side)
    return {"friendly": "friend", "hostile": "hostile", "neutral": "neutral"}.get(rel) == p.get("identity")


DEFAULT_CONDITIONS: dict[str, Callable] = {
    "core.cond.in_area": c_in_area,
    "core.cond.hostile_in_area": c_hostile_in_area,
    "core.cond.within_range": c_within_range,
    "core.cond.system_ready": c_system_ready,
    "core.cond.cm_available": c_cm_available,
    "core.cond.random": c_random,
    "core.cond.has_munition": c_has_munition,
    "core.cond.target_in_weapon_range": c_target_in_weapon_range,
    "core.cond.target_identity": c_target_identity,
}


# ---- native aksiyonlar
def a_sequence(w: World, ctx: Ctx, p: dict) -> bool:
    for step in p.get("steps") or []:
        if not w.execute(step, ctx) and step.get("onFailure", "abort") == "abort":
            return False
    return True


def a_parallel(w: World, ctx: Ctx, p: dict) -> bool:
    res = [w.execute(b, ctx) for b in p.get("branches") or []]
    return all(res) if p.get("policy", "all") == "all" else any(res)


def a_repeat(w: World, ctx: Ctx, p: dict) -> bool:
    for _ in range(p.get("count") or 1):  # referans: sonsuz döngü yok
        if not w.execute(p["body"], ctx):
            return False
    return True


def a_set_variable(w: World, ctx: Ctx, p: dict) -> bool:
    (ctx.behavior.vars if ctx.behavior else w.vars)[p["name"]] = p.get("value")
    return True


def a_set_rule_enabled(w: World, ctx: Ctx, p: dict) -> bool:
    b = ctx.entity.behaviors.get(p["behavior"]) if p.get("behavior") and ctx.entity else ctx.behavior
    rules = b.rules if b else w.scenario_rules
    if p["rule"] not in rules:
        return False
    rules[p["rule"]].enabled = bool(p["enabled"])
    return True


def a_set_state(w: World, ctx: Ctx, p: dict) -> bool:
    s = ctx.entity.systems.get(ctx.mount_for(p.get("system"))) if ctx.entity else None
    if s is None:
        return False
    s.state.update(p.get("state") or {})
    return True


def a_system_command(w: World, ctx: Ctx, p: dict) -> bool:
    return w.command(ctx, ctx.mount_for(p.get("system")), p.get("command"), p.get("args") or {})


def a_set_kinematics(w: World, ctx: Ctx, p: dict) -> bool:
    for k in ("heading", "speed", "altitude"):
        if p.get(k) is not None:
            ctx.entity.state[k] = p[k]
    return True


def a_follow_route(w: World, ctx: Ctx, p: dict) -> bool:
    ctx.entity.state["activeRoute"] = p.get("route")
    return p.get("route") in w.routes


def a_emcon(w: World, ctx: Ctx, p: dict) -> bool:
    mode = {"silent": "standby", "full": "search"}.get(p.get("level"))
    for s in ctx.entity.systems.values():
        if mode and s.is_a("core.systemtype.radar"):
            s.state["mode"], s.state["emitting"] = mode, mode != "standby"
    return True


DEFAULT_ACTIONS: dict[str, Callable] = {
    "core.control.sequence": a_sequence,
    "core.control.parallel": a_parallel,
    "core.control.repeat": a_repeat,
    "core.control.wait": lambda w, c, p: True,
    "core.control.log": lambda w, c, p: True,
    "core.control.set_variable": a_set_variable,
    "core.control.set_rule_enabled": a_set_rule_enabled,
    "core.system.set_state": a_set_state,
    "core.system.command": a_system_command,
    "core.system.emcon": a_emcon,
    "core.movement.set_kinematics": a_set_kinematics,
    "core.movement.follow_route": a_follow_route,
}


# ---- sistem komutları (referans etkileri)
def k_set_power(w, ctx, s, a):
    s.state["power"] = bool(a.get("on"))


def k_set_mode(w, ctx, s, a):
    if a.get("mode") not in (s.params.get("supportedModes") or [a.get("mode")]):
        return False
    s.state["mode"] = a["mode"]
    s.state["emitting"] = a["mode"] not in ("off", "standby")


def k_dispense(w, ctx, s, a):
    if not s.state.get("armed", True):
        return False
    n = min(int(a.get("count") or 1), s.state.get("remaining") or 0)
    if n <= 0:
        return False
    s.state["remaining"] -= n
    w.emit("core.event.cm.dispensed", ctx.entity.key, {"system": s.mount, "count": n})
    if s.state["remaining"] == 0:
        w.emit("core.event.cm.empty", ctx.entity.key, {"system": s.mount})


def k_run_program(w, ctx, s, a):
    prog = (s.params.get("programs") or {}).get(a.get("program"))
    if not prog:
        return False
    return k_dispense(w, ctx, s, {"count": prog.get("burstCount", 1) * prog.get("salvoCount", 1)})


def k_set_armed(w, ctx, s, a):
    s.state["armed"] = bool(a.get("armed"))


IFF_CODE_FIELDS = {"1": "mode1Code", "2": "mode2Code", "3A": "mode3Code"}


def k_set_code(w, ctx, s, a):
    f = IFF_CODE_FIELDS.get(a.get("mode"))
    if not f:
        return False
    s.state[f] = a.get("code")


def k_interrogate(w, ctx, s, a):
    tgt = w.entities.get(a.get("target"))
    mode = a.get("mode", "3A")
    if tgt is None:
        return False
    tx = next((x for x in tgt.systems.values() if x.is_a("core.systemtype.iff") and x.ready() and x.state.get("transponderOn")), None)
    if tx is None:
        return True  # cevap yok
    secure = {"4": "mode4Enabled", "5": "mode5Enabled"}.get(mode)
    valid = bool(tx.state.get(secure)) and w.relation(ctx.entity.side, tgt.side) == "friendly" if secure else True
    w.emit("core.event.iff.reply", ctx.entity.key, {"system": s.mount, "target": tgt.key, "mode": mode,
                                                    "code": tx.state.get(IFF_CODE_FIELDS.get(mode, "mode3Code")), "valid": valid})


def k_set_master_arm(w, ctx, s, a):
    s.state["masterArm"] = bool(a.get("armed"))


def k_fire(w, ctx, s, a):
    if not s.state.get("masterArm"):
        return False
    tgt = w.entities.get(a.get("target"))
    if tgt is None:
        return False
    me = ctx.entity
    d = geo.distance(me.position, tgt.position, me.altitude, tgt.altitude) if me.position and tgt.position else None
    station = None
    for st in s.inventory.values():
        if st["count"] <= 0 or a.get("munition") not in (None, st["munition"]):
            continue
        mp = (w.munitions.get(st["munition"]) or {}).get("parameters") or {}
        if (d is None or mp.get("minRange", 0) <= d <= mp.get("maxRange", float("inf"))) and tgt.domain in (mp.get("targetDomains") or [tgt.domain]):
            station = st
            break
    if station is None:
        return False
    n = min(int(a.get("salvo") or 1), station["count"])
    station["count"] -= n
    mp = (w.munitions.get(station["munition"]) or {}).get("parameters") or {}
    for _ in range(n):
        w.emit("core.event.weapon.launched", me.key, {"system": s.mount, "munition": station["munition"], "target": tgt.key})
    # Hedefin RWR'ı atışı algılayabiliyorsa füze uyarısı (motorda bunu RF/MAWS modeli üretir)
    if mp.get("launchWarning") and me.position and tgt.position:
        rwr = next((x for x in tgt.systems.values() if x.is_a("core.systemtype.rwr") and x.ready() and x.params.get("launchDetection", True)), None)
        if rwr:
            w.emit("core.event.rwr.missile_launch", tgt.key, {"system": rwr.mount, "source": me.key, "bearing": round(geo.bearing(tgt.position, me.position), 1)})
    _effects_on_target(w, me, tgt, station["munition"], d)
    if all(st["count"] == 0 for st in s.inventory.values()):
        w.emit("core.event.weapon.out_of_ammo", me.key, {"system": s.mount})


DEFAULT_COMMANDS: dict[tuple[str, str], Callable] = {
    ("core.systemtype.base", "setPower"): k_set_power,
    ("core.systemtype.radar", "setMode"): k_set_mode,
    ("core.systemtype.cm_dispenser", "dispense"): k_dispense,
    ("core.systemtype.cm_dispenser", "runProgram"): k_run_program,
    ("core.systemtype.cm_dispenser", "setArmed"): k_set_armed,
    ("core.systemtype.iff", "setCode"): k_set_code,
    ("core.systemtype.iff", "interrogate"): k_interrogate,
    ("core.systemtype.weapon_launcher", "setMasterArm"): k_set_master_arm,
    ("core.systemtype.weapon_launcher", "fire"): k_fire,
}


# ============================================================================ senaryo betiği
def run_script(world: World, script: dict) -> list[Record]:
    """Betik: {"step": s, "until": s, "events": [{t, event, entity, payload} | {t, set} | {t, complete}]}"""
    entries = sorted(script.get("events") or [], key=lambda e: e["t"])
    step = float(script.get("step", 1.0))
    until = float(script.get("until", max([e["t"] for e in entries] + [0])))
    times = sorted({round(i * step, 9) for i in range(int(until / step) + 1)} | {float(e["t"]) for e in entries if e["t"] <= until})
    world.start()
    i = 0
    for t in times:
        if t > 0:
            world.time = t
        while i < len(entries) and entries[i]["t"] <= t:
            e = entries[i]
            i += 1
            if "event" in e:
                world.emit(e["event"], e.get("entity"), e.get("payload") or {})
            elif "set" in e:
                _apply_set(world, e["set"])
            elif "complete" in e:
                world.complete(e["complete"]["entity"], e["complete"]["channel"])
        if t > 0:
            world.advance(t)
    return world.log


def _apply_set(world: World, s: dict) -> None:
    ent = world.entities[s["entity"]]
    parts = s["path"].split(".")
    target: Any = ent.state
    if parts[0] == "systems":
        target = ent.systems[parts[1]].state
        parts = parts[3:] if len(parts) > 3 and parts[2] == "state" else parts[2:]
    elif parts[0] == "state":
        parts = parts[1:]
    for p in parts[:-1]:
        target = target.setdefault(p, {})
    target[parts[-1]] = s["value"]
    world.record("set", Ctx(world, ent), path=s["path"], value=s["value"])


def format_record(r: Record) -> str:
    d = r.detail
    who = f"{r.entity or 'scenario':10}"
    if r.type == "event":
        body = f"⚡ {d['event']}  {_kv(d.get('payload'))}"
    elif r.type == "rule.fire":
        body = f"▶ {r.owner}  (öncelik {d['priority']})"
    elif r.type == "rule.blocked":
        body = f"⛔ {r.owner} engellendi: '{d['channel']}' kanalı {d['heldBy']} elinde"
    elif r.type == "channel.preempted":
        body = f"↯ {r.owner} '{d['channel']}' kanalını {d['previous']}'dan aldı"
    elif r.type == "action":
        body = f"    · {d['action'].replace('core.action.', '')}({_kv(d.get('params'))})"
    elif r.type.startswith("command"):
        sc = d.get("stateChange")
        change = "  " + ", ".join(f"{k}: {a}→{b}" for k, (a, b) in sc.items()) if sc else ""
        fail = f"  ✗ {d.get('error', 'başarısız')}" if r.type == "command.failed" else ""
        body = f"      ↳ {d['system']}.{d['command']}{change}{fail}"
    elif r.type == "channel.released":
        body = f"□ '{d['channel']}' kanalı serbest ({d.get('owner_')} tamamlandı)"
    elif r.type == "action.skipped":
        body = f"    · {d['action'].replace('core.action.', '')} atlandı ({d.get('reason')})"
    elif r.type == "set":
        body = f"✎ {d['path']} = {d['value']}"
    else:
        body = f"{r.type} {_kv(d)}"
    return f"t={r.t:8.2f}  {who} {body}"


def _kv(d: Any) -> str:
    if not isinstance(d, dict):
        return str(d)
    return ", ".join(f"{k}={v}" for k, v in d.items() if v is not None)


# ============================================================================ genişletme: yeni etkileşimler
def _is_guided(w: World, munition: str) -> bool:
    return "core.munitiontype.guided_missile" in ((w.munitions.get(munition) or {}).get("typeLineage") or [])


def _find(ent: EntityRT, type_id: str, ready: bool = True) -> SystemRT | None:
    return next((x for x in ent.systems.values() if x.is_a(type_id) and (x.ready() or not ready)), None)


def _effects_on_target(w: World, shooter: EntityRT, tgt: EntityRT, munition: str, dist: float | None) -> None:
    """Atışın hedefte ürettiği algılanabilir etkiler (gerçek motorda fizik/sensör modelleri üretir)."""
    w.emit("core.event.platform.under_fire", tgt.key, {"by": shooter.key, "munition": munition, "distance": round(dist, 1) if dist else None})
    if _is_guided(w, munition) and shooter.position and tgt.position:
        maws = _find(tgt, "core.systemtype.maws")
        if maws:
            speed = ((w.munitions.get(munition) or {}).get("parameters") or {}).get("speed") or 1
            w.emit("core.event.mws.missile_approach", tgt.key, {"system": maws.mount, "source": shooter.key,
                                                                "bearing": round(geo.bearing(tgt.position, shooter.position), 1),
                                                                "timeToImpact": round((dist or 0) / speed, 1)})
    if tgt.position:
        _detonation(w, tgt.position, munition, shooter.key, exclude=tgt.key)


def _detonation(w: World, pos: dict, munition: str, by: str | None, exclude: str | None = None, radius: float = 1000.0) -> None:
    for e in w.entities.values():
        if e.key in (exclude, by) or not e.position:
            continue
        dist = geo.distance(pos, e.position)
        if dist <= radius:
            w.emit("core.event.weapon.detonation_nearby", e.key, {"position": pos, "distance": round(dist, 1), "munition": munition, "by": by})


def c_under_fire_recently(w: World, ctx: Ctx, p: dict) -> bool:
    e = ctx.entity
    return bool(e and e.last_under_fire is not None and w.time - e.last_under_fire <= p.get("within", 10))


def c_target_domain(w: World, ctx: Ctx, p: dict) -> bool:
    t = _target(w, ctx, p.get("target"))
    return bool(t and t.domain == p.get("domain"))


def c_nearest_hostile_within(w: World, ctx: Ctx, p: dict) -> bool:
    me = ctx.entity
    if not me or not me.position:
        return False
    for e in w.entities.values():
        if e is me or not e.position or w.relation(me.side, e.side) != "hostile":
            continue
        if p.get("domain", "any") != "any" and e.domain != p["domain"]:
            continue
        if geo.distance(me.position, e.position, me.altitude, e.altitude) <= p.get("range", 0):
            return True
    return False


def c_is_night(w: World, ctx: Ctx, p: dict) -> bool:
    start = w.c["time"].get("start") or ""
    try:
        hour = (int(start[11:13]) + int(start[14:16]) / 60 + w.time / 3600) % 24
    except ValueError:
        return False
    return hour < 6 or hour >= 19


def c_datalink_ok(w: World, ctx: Ctx, p: dict) -> bool:
    s = ctx.entity.systems.get(ctx.mount_for(p.get("system"))) if ctx.entity else None
    return bool(s and s.ready() and s.state.get("linkStatus", "ok") == "ok")


def c_cargo_has_space(w: World, ctx: Ctx, p: dict) -> bool:
    s = ctx.entity.systems.get(ctx.mount_for(p.get("system"))) if ctx.entity else None
    return bool(s and (s.params.get("seats", 0) - s.state.get("occupants", 0)) >= p.get("count", 1))


DEFAULT_CONDITIONS.update({
    "core.cond.under_fire_recently": c_under_fire_recently,
    "core.cond.target_domain": c_target_domain,
    "core.cond.nearest_hostile_within": c_nearest_hostile_within,
    "core.cond.line_of_sight": lambda w, c, p: True,          # arazi modeli motorda
    "core.cond.altitude_agl_below": lambda w, c, p: bool(c.entity) and c.entity.altitude < p.get("altitude", 0),
    "core.cond.is_night": c_is_night,
    "core.cond.datalink_ok": c_datalink_ok,
    "core.cond.cargo_has_space": c_cargo_has_space,
})


def _set(ctx: Ctx, **kv) -> bool:
    if ctx.entity:
        ctx.entity.state.update({k: v for k, v in kv.items() if v is not None})
    return True


def a_terminal_attack(w: World, ctx: Ctx, p: dict) -> bool:
    me, tgt = ctx.entity, w.entities.get(p.get("target"))
    if not me or not tgt:
        return False
    w.emit("core.event.platform.under_fire", tgt.key, {"by": me.key, "munition": me.data["template"], "distance": 0})
    if tgt.position:
        _detonation(w, tgt.position, me.data["template"], me.key, exclude=tgt.key)
    me.state["alive"] = False
    return True


def a_fire_at_position(w: World, ctx: Ctx, p: dict) -> bool:
    return True


def a_suppress(w: World, ctx: Ctx, p: dict) -> bool:
    return w.command(ctx, ctx.mount_for(p.get("launcher")), "fire", {"target": p.get("target"), "salvo": p.get("rounds", 1)})


DEFAULT_ACTIONS.update({
    "core.movement.hold": lambda w, c, p: _set(c, activeRoute=None),
    "core.movement.orbit": lambda w, c, p: _set(c, orbitCenter=p.get("center")),
    "core.movement.hover": lambda w, c, p: _set(c, hovering=True),
    "core.movement.land": lambda w, c, p: _set(c, onGround=True, hovering=False) and (w.emit("core.event.platform.landed", c.entity.key, {"position": c.entity.position}) or True),
    "core.movement.takeoff": lambda w, c, p: _set(c, onGround=False) and (w.emit("core.event.platform.took_off", c.entity.key, {}) or True),
    "core.movement.follow": lambda w, c, p: _set(c, following=p.get("target")),
    "core.movement.patrol_area": lambda w, c, p: _set(c, patrolArea=p.get("area")) and p.get("area") in w.areas,
    "core.movement.flee": lambda w, c, p: _set(c, disposition="panicked") if c.entity and "disposition" in c.entity.state else True,
    "core.movement.rtb": lambda w, c, p: _set(c, activeRoute=None, returningToBase=True),
    "core.movement.terrain_follow": lambda w, c, p: _set(c, terrainFollow=p.get("altitudeAgl") if p.get("enabled", True) else None),
    "core.movement.set_formation": lambda w, c, p: _set(c, formation=p.get("formation")),
    "core.movement.terminal_attack": a_terminal_attack,
    "core.lifeform.set_posture": lambda w, c, p: _set(c, posture=p.get("posture")),
    "core.lifeform.take_cover": lambda w, c, p: _set(c, inCover=True),
    "core.lifeform.mount": lambda w, c, p: _set(c, mounted=True),
    "core.weapon.suppress": a_suppress,
})


def k_point_at(w, ctx, s, a):
    s.state["trackTarget"] = a.get("target") if a.get("track", True) else None
    s.state["mode"] = "track" if a.get("target") and a.get("track", True) else s.state.get("mode")


def k_designate(w, ctx, s, a):
    tgt = w.entities.get(a.get("target"))
    if tgt is None:
        return False
    s.state.update({"lasing": True, "designatedTarget": tgt.key, **({"code": a["code"]} if a.get("code") else {})})
    lwr = _find(tgt, "core.systemtype.lwr")
    if lwr and ctx.entity.position and tgt.position:
        w.emit("core.event.lwr.laser_warning", tgt.key, {"system": lwr.mount, "source": ctx.entity.key, "laserType": "designator",
                                                          "bearing": round(geo.bearing(tgt.position, ctx.entity.position), 1)})


def k_stop_designating(w, ctx, s, a):
    s.state.update({"lasing": False, "designatedTarget": None})


def k_jam(on: bool):
    def h(w, ctx, s, a):
        s.state["active"] = on
        if on and a.get("technique"):
            s.state["technique"] = a["technique"]
    return h


def k_send_message(w, ctx, s, a):
    me = ctx.entity
    if a.get("to"):
        targets = [w.entities.get(a["to"])]
    else:  # ağdaki dost telsizler
        targets = [e for e in w.entities.values() if e is not me and w.relation(me.side, e.side) == "friendly"]
    for t in targets:
        rx = _find(t, "core.systemtype.radio") if t else None
        if rx and rx.state.get("net", "default") == s.state.get("net", "default"):
            w.emit("core.event.comms.message_received", t.key, {"system": rx.mount, "from": me.key, "message": a.get("message"),
                                                                "priority": a.get("priority", "routine")})


def k_fire_at(w, ctx, s, a):
    if not s.state.get("masterArm"):
        return False
    st = next((x for x in s.inventory.values() if x["count"] > 0 and a.get("munition") in (None, x["munition"])), None)
    if st is None:
        return False
    n = min(int(a.get("rounds") or 1), st["count"])
    st["count"] -= n
    w.emit("core.event.weapon.launched", ctx.entity.key, {"system": s.mount, "munition": st["munition"], "target": None})
    _detonation(w, a["position"], st["munition"], ctx.entity.key)


def k_unload(w, ctx, s, a):
    n = s.state.get("occupants", 0)
    s.state["occupants"] = 0
    w.emit("core.event.cargo.unloaded", ctx.entity.key, {"system": s.mount, "count": n})


def k_load(w, ctx, s, a):
    if s.state.get("occupants", 0) >= s.params.get("seats", 0):
        return False
    s.state["occupants"] = s.state.get("occupants", 0) + 1
    tgt = w.entities.get(a.get("entity"))
    if tgt:
        tgt.state["mounted"] = True
        w.emit("core.event.platform.mounted", tgt.key, {"carrier": ctx.entity.key})
    w.emit("core.event.cargo.loaded", ctx.entity.key, {"system": s.mount, "entity": a.get("entity")})


DEFAULT_COMMANDS.update({
    ("core.systemtype.eo_ir", "pointAt"): k_point_at,
    ("core.systemtype.eo_ir", "setMode"): lambda w, c, s, a: s.state.update({"mode": a.get("mode")}),
    ("core.systemtype.laser_designator", "designate"): k_designate,
    ("core.systemtype.laser_designator", "stopDesignating"): k_stop_designating,
    ("core.systemtype.jammer", "startJamming"): k_jam(True),
    ("core.systemtype.jammer", "stopJamming"): k_jam(False),
    ("core.systemtype.radio", "sendMessage"): k_send_message,
    ("core.systemtype.radio", "setNet"): lambda w, c, s, a: s.state.update({"net": a.get("net")}),
    ("core.systemtype.weapon_launcher", "fireAt"): k_fire_at,
    ("core.systemtype.weapon_launcher", "ceaseFire"): lambda w, c, s, a: True,
    ("core.systemtype.cargo_bay", "load"): k_load,
    ("core.systemtype.cargo_bay", "unload"): k_unload,
    ("core.systemtype.aps", "setArmed"): lambda w, c, s, a: s.state.update({"armed": bool(a.get("armed"))}),
})
