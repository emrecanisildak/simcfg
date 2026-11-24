"""Referans çalışma zamanı + motor sözleşmesi testleri."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from simcfg import Builder, Diagnostics, Workspace  # noqa: E402
from simcfg.cli import TRACE_TYPES  # noqa: E402
from simcfg.runtime import DictContext, World, evaluate, run_script  # noqa: E402
from simcfg.workspace import HAVE_JSONSCHEMA  # noqa: E402

_CACHE: dict = {}


def compiled(sid: str) -> dict:
    if sid not in _CACHE:
        d = Diagnostics()
        ws = Workspace(ROOT, d).load()
        _CACHE[sid] = (Builder(ws, d).compile_scenario(sid), ws, d)
    return copy.deepcopy(_CACHE[sid][0])


def fired(world: World, entity: str | None = None) -> list[str]:
    return [r.owner for r in world.log if r.type == "rule.fire" and (entity is None or r.entity == entity)]


class StrictCtx(DictContext):
    def condition(self, cond_id, params):
        if cond_id == "test.must_not_be_called":
            raise AssertionError("kısa devre ihlali")
        return super().condition(cond_id, params)


class ExpressionConformance(unittest.TestCase):
    def test_vectors(self):
        data = json.loads((ROOT / "conformance/expressions.json").read_text(encoding="utf-8"))
        self.assertGreater(len(data["cases"]), 30)
        for case in data["cases"]:
            with self.subTest(case["name"]):
                c = case["context"]
                ctx = StrictCtx(c.get("vars"), c.get("params"), c.get("conds"))
                self.assertEqual(evaluate(case["expr"], ctx), case["expected"])
                if "expectCondCalls" in c:
                    self.assertEqual([[a, b] for a, b in ctx.cond_calls], c["expectCondCalls"])


class ContractTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema gerekli")
    def test_compiled_output_matches_schema(self):
        for sid in ("demo.scenario.strait_patrol", "demo.scenario.strait_patrol_night"):
            out = compiled(sid)
            ws, d = _CACHE[sid][1], Diagnostics()
            ws.d = d
            self.assertTrue(ws.schema_validate(out, "compiled-scenario.schema.json", sid), [x.format() for x in d.items])

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema gerekli")
    def test_schema_rejects_broken_output(self):
        out = compiled("demo.scenario.strait_patrol")
        del out["entities"]["viper_1"]["systems"]["radar"]["implementation"]
        out["rules"]["phase_2_red_push"]["trigger"] = {"every": 0}
        ws, d = _CACHE["demo.scenario.strait_patrol"][1], Diagnostics()
        ws.d = d
        self.assertFalse(ws.schema_validate(out, "compiled-scenario.schema.json", "x"))

    def test_rejects_unknown_api_version(self):
        out = compiled("demo.scenario.strait_patrol")
        out["apiVersion"] = "simcfg/compiled/v0"
        with self.assertRaises(ValueError):
            World(out)


class GoldenTrace(unittest.TestCase):
    def test_trace_matches_golden(self):
        script = json.loads((ROOT / "examples/scripts/strait_patrol_engagement.json").read_text(encoding="utf-8"))
        w = World(compiled(script["scenario"]))
        trace = [r.to_dict() for r in run_script(w, script) if r.type in TRACE_TYPES]
        golden = json.loads((ROOT / "conformance/traces/strait_patrol_engagement.trace.json").read_text(encoding="utf-8"))
        self.assertEqual(trace, golden, "referans semantiği değişti: izi gözden geçirip yeniden üretin (conformance/README.md)")


class RuleSemantics(unittest.TestCase):
    def world(self, sid="demo.scenario.strait_patrol") -> World:
        w = World(compiled(sid))
        w.start()
        return w

    def test_r2_role_translation(self):
        w = self.world()
        w.emit("core.event.sensor.detection", "sam_1", {"system": "search_radar", "target": "viper_1", "identity": "unknown"})
        self.assertIn("air_defense.identify_contact", fired(w, "sam_1"), "mount adı 'radar' rolüne çevrilmeli")

    def test_cooldown_and_bb_variable(self):
        w = self.world()
        for t in (10, 11, 15):
            w.time = t
            w.emit("core.event.rwr.lock", "viper_1", {"system": "rwr", "bearing": t})
        self.assertEqual(fired(w, "viper_1").count("self_defense.chaff_on_lock"), 2)
        self.assertEqual(w.entities["viper_1"].behaviors["self_defense"].vars["lastThreatBearing"], 15)
        self.assertEqual(w.entities["viper_1"].systems["chaff"].state["remaining"], 30 - 2 * 5)

    def test_preemption_and_blocking(self):
        w = self.world()
        w.emit("core.event.rwr.missile_launch", "viper_1", {"system": "rwr", "bearing": 90})
        self.assertEqual(w.entities["viper_1"].channels["movement"]["owner"], "self_defense.flare_and_break_on_launch")
        self.assertTrue(any(r.type == "channel.preempted" and r.detail["previous"] == "patrol.start_patrol" for r in w.log))
        w.entities["viper_1"].systems["flare"].state["remaining"] = 0
        w.time = 2
        w.emit("core.event.rwr.missile_launch", "viper_1", {"system": "rwr", "bearing": 90})
        blocked = [r for r in w.log if r.type == "rule.blocked"]
        self.assertEqual(blocked[-1].owner, "self_defense.notch_without_flares", "düşük öncelik kanalı alamaz")
        w.complete("viper_1", "movement")
        w.time = 3
        w.emit("core.event.rwr.missile_launch", "viper_1", {"system": "rwr", "bearing": 90})
        self.assertEqual(w.entities["viper_1"].channels["movement"]["owner"], "self_defense.notch_without_flares")

    def test_magazine_empty_event_chain(self):
        w = self.world()
        w.entities["viper_1"].systems["chaff"].state["remaining"] = 5
        w.emit("core.event.rwr.lock", "viper_1", {"system": "rwr", "bearing": 1})
        self.assertIn("self_defense.magazine_empty", fired(w, "viper_1"), "komut etkisi -> cm.empty -> kural")

    def test_scenario_at_rule_with_on(self):
        w = self.world()
        w.advance(900.0)
        self.assertIn("scenario.phase_2_red_push", fired(w))
        self.assertEqual(w.vars["phase"], 2)
        self.assertAlmostEqual(w.entities["bandit_1"].state["speed"], 520 * 1852 / 3600)
        self.assertEqual(w.entities["bandit_1"].systems["radar"].state["mode"], "search")
        w.advance(930.0)
        self.assertNotIn("scenario.red_emcon_until_phase2", fired(w), "phase=2 iken EMCON kuralı çalışmamalı")

    def test_edge_rising_and_falling(self):
        w = self.world()
        v = w.entities["viper_1"]
        v.state["position"] = {"lat": 39.9, "lon": 27.9}
        w.advance(1.0)
        v.state["position"] = {"lat": 40.6, "lon": 26.5}
        w.advance(2.0)
        sam = fired(w, "sam_1")
        self.assertEqual([x for x in sam if x.startswith("air_defense.radar")], ["air_defense.radar_up", "air_defense.radar_down"])
        self.assertEqual(w.entities["sam_1"].systems["search_radar"].state["mode"], "standby")

    def test_roe_layering_night(self):
        w = self.world("demo.scenario.strait_patrol_night")
        f = fired(w)
        self.assertNotIn("engage.arm_on_start", fired(w, "viper_1"), "senaryo geneli ROE=hold")
        self.assertIn("engage.arm_on_start", fired(w, "bandit_1"), "varlık seviyesi ROE=free kazanır")
        self.assertTrue(f)

    def test_unbound_optional_role_rule_never_fires(self):
        w = self.world("demo.scenario.strait_patrol_night")
        w.emit("core.event.rwr.lock", "bandit_2", {"system": "rwr", "bearing": 10})
        self.assertNotIn("self_defense.chaff_on_lock", fired(w, "bandit_2"))

    def test_weapon_inventory_and_out_of_ammo(self):
        w = self.world()
        sam = w.entities["sam_1"]
        w.entities["viper_1"].state["position"] = {"lat": 39.95, "lon": 27.75}
        w.advance(1.0)  # radar_up -> master arm
        for t in range(2, 80, 21):
            w.time = float(t)
            w.emit("core.event.sensor.detection", "sam_1", {"system": "search_radar", "target": "viper_1", "identity": "hostile"})
        self.assertEqual(sam.systems["launcher"].inventory["ready"]["count"], 0)
        self.assertTrue(any(r.type == "event" and r.detail["event"] == "core.event.weapon.out_of_ammo" for r in w.log))

    def test_composite_action_expands(self):
        w = World(compiled("demo.scenario.strait_patrol_night"))
        w.rng.random = lambda: 0.0  # core.cond.random her zaman true
        w.start()
        w.emit("core.event.rwr.new_threat", "viper_2", {"system": "rwr", "bearing": 45, "threatLevel": "track"})
        acts = [r.detail["action"] for r in w.log if r.type == "action" and r.entity == "viper_2"]
        i = acts.index("demo.action.defensive_reaction")
        self.assertEqual(acts[i + 1:i + 5], ["core.action.sequence", "core.action.dispense_chaff", "core.action.maneuver", "core.action.wait"])
        self.assertEqual(w.entities["viper_2"].systems["chaff"].state["remaining"], 12 - 2,
                         "composite {param chaffSystem} rolden mount'a; başlangıç 12 (gündüz senaryosundan miras)")


if __name__ == "__main__":
    unittest.main()
