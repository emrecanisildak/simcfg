"""Genişletilmiş kütüphane testleri: birlik açma, formasyon, yeni platform/sistem etkileşimleri."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from simcfg import Builder, Diagnostics, Validator, Workspace  # noqa: E402
from simcfg.cli import TRACE_TYPES  # noqa: E402
from simcfg.compiler import formation_offset, offset_geo  # noqa: E402
from simcfg.runtime import World, run_script  # noqa: E402
from test_simcfg import TempWorkspace, codes  # noqa: E402

_C: dict = {}


def compiled(sid="demo.scenario.combined_arms") -> dict:
    if sid not in _C:
        d = Diagnostics()
        _C[sid] = (Builder(Workspace(ROOT, d).load(), d).compile_scenario(sid), d)
    return json.loads(json.dumps(_C[sid][0]))


def fired(w: World, ent: str) -> list[str]:
    return [r.owner for r in w.log if r.type == "rule.fire" and r.entity == ent]


def events(w: World, ent: str) -> list[str]:
    return [r.detail["event"] for r in w.log if r.type == "event" and r.entity == ent]


class LibraryCoverage(unittest.TestCase):
    def test_platform_classes_present(self):
        ws = Workspace(ROOT, Diagnostics()).load()
        need = ["rotary_wing", "multirotor", "uav", "loitering_munition", "tracked_vehicle", "wheeled_vehicle",
                "lifeform", "surface_vessel", "structure"]
        for t in need:
            self.assertIsNotNone(ws.get("core.platformtype." + t), t)
        for p in ["rotary.generic_attack_helicopter", "uav.generic_male_uav", "uav.generic_quadcopter", "land.generic_mbt",
                  "land.generic_shorad_vehicle", "land.generic_lr_sam_site", "air.generic_light_aircraft", "life.generic_rifleman",
                  "life.generic_civilian", "sea.generic_frigate", "structure.generic_building"]:
            r = ws.resolve("core.platform." + p)
            self.assertIsNotNone(r, p)
            self.assertFalse(r.abstract, p)

    def test_whole_workspace_valid(self):
        d = Diagnostics()
        Validator(Workspace(ROOT, d).load(), d).run()
        self.assertEqual([x.format() for x in d.errors + d.warnings if x.code != "schema.unavailable"], [])


class UnitExpansion(unittest.TestCase):
    def test_counts_and_hierarchy(self):
        c = compiled()
        self.assertEqual(c["units"]["blue_mech"]["subunits"], ["blue_mech_squad_1", "blue_mech_squad_2", "blue_mech_squad_3"])
        self.assertEqual(c["units"]["blue_mech_squad_1_alpha"]["parent"], "blue_mech_squad_1")
        mech = [k for k in c["entities"] if k.startswith("blue_mech_")]
        self.assertEqual(len(mech), 3 + 3 * (1 + 2 * 4), "3 PMA + 3 manga x (lider + 2 tim x 4)")
        self.assertEqual(len([k for k in c["entities"] if k.startswith("civilians_person_")]), 12)

    def test_leader_callsign_and_overrides(self):
        c = compiled()
        lead, wing = c["entities"]["blue_tanks_lead"], c["entities"]["blue_tanks_wing_1"]
        self.assertTrue(lead["unit"]["leader"])
        self.assertEqual(wing["unit"]["leaderEntity"], "blue_tanks_lead")
        self.assertEqual((lead["callsign"], wing["callsign"]), ("TIGER 1", "TIGER 2"))
        self.assertEqual(c["entities"]["blue_tanks_wing_3"]["behaviors"]["combat"]["parameters"]["roe"], "tight", "allMembers yaması")
        fcp = c["entities"]["red_ad_fcp"]["behaviors"]["air_defense"]["parameters"]
        self.assertEqual(fcp["engagementArea"], "red_mez", "varlık anahtarına göre override")

    def test_formation_geometry(self):
        self.assertEqual(formation_offset("wedge", 0, 4, 100), (0.0, 0.0))
        self.assertEqual(formation_offset("wedge", 1, 4, 100), (-100, -100))
        self.assertEqual(formation_offset("wedge", 2, 4, 100), (100, -100))
        self.assertEqual(formation_offset("column", 3, 4, 50), (0.0, -150))
        self.assertEqual(formation_offset("line", 0, 3, 10), (-10.0, 0.0))
        p = offset_geo({"lat": 40.0, "lon": 30.0}, 90, 0, 1000)  # doğuya bakarken 1 km ileri
        self.assertAlmostEqual(p["lat"], 40.0, places=5)
        self.assertGreater(p["lon"], 30.0)

    def test_bad_override_key_and_cycle(self):
        with TempWorkspace() as t:
            t.edit("packages/demo/scenarios/combined_arms.json",
                   lambda d: d["spec"]["units"]["blue_tanks"].update({"overrides": {"blue_tanks_wing_9": {}}}))
            t.edit("packages/core/unit-templates/land.json",
                   lambda d: d[0]["spec"].update({"subunits": {"loop": {"unit": "core.unit.infantry_squad"}}}))
            d = Diagnostics()
            Validator(Workspace(t.dir, d).load(), d).run()
            self.assertTrue({"unit.override_key", "unit.cycle"} <= codes(d), codes(d))


class Interactions(unittest.TestCase):
    def world(self) -> World:
        w = World(compiled())
        w.start()
        return w

    def test_manpads_seen_by_maws_not_rwr(self):
        w = self.world()
        w.entities["blue_attack_lead"].state["position"] = {"lat": 39.68, "lon": 27.315}
        w.emit("core.event.sensor.detection", "red_manpads_gunner", {"system": "eyes", "target": "blue_attack_lead", "identity": "hostile"})
        ev = events(w, "blue_attack_lead")
        self.assertIn("core.event.mws.missile_approach", ev)
        self.assertNotIn("core.event.rwr.missile_launch", ev, "IR MANPADS RWR'da görünmez")
        self.assertIn("missile_defense.flare_on_approach", fired(w, "blue_attack_lead"))

    def test_infantry_cover_and_civilians_flee(self):
        w = self.world()
        w.entities["blue_tanks_lead"].state["position"] = {"lat": 39.685, "lon": 27.32}
        w.emit("core.event.sensor.detection", "blue_tanks_lead", {"system": "sight", "target": "red_squad_alpha_gunner", "identity": "hostile"})
        g = w.entities["red_squad_alpha_gunner"]
        self.assertEqual((g.state["posture"], g.state["inCover"]), ("prone", True))
        self.assertTrue(w.entities["civilians_person_3"].behaviors["civilian"].vars["panicked"])
        self.assertEqual(w.entities["civilians_person_3"].state["disposition"], "panicked")

    def test_roe_tight_does_not_engage_unknown(self):
        w = self.world()
        w.entities["blue_tanks_lead"].state["position"] = {"lat": 39.685, "lon": 27.32}
        w.emit("core.event.sensor.detection", "blue_tanks_lead", {"system": "sight", "target": "red_squad_alpha_gunner", "identity": "unknown"})
        self.assertNotIn("combat.engage_main", fired(w, "blue_tanks_lead"))

    def test_lz_unload_and_uav_lost_link(self):
        w = self.world()
        w.emit("core.event.platform.waypoint_reached", "blue_lift", {"waypointId": "lz"})
        self.assertEqual(w.entities["blue_lift"].systems["cabin"].state["occupants"], 0)
        w.emit("core.event.datalink.lost", "blue_uav", {"system": "c2"})
        self.assertTrue(w.entities["blue_uav"].state.get("returningToBase"))
        self.assertTrue(w.entities["blue_uav"].behaviors["lost_link"].vars["linkLost"])

    def test_laser_warning_smoke(self):
        w = self.world()
        w.emit("core.event.lwr.laser_warning", "blue_tanks_wing_1", {"system": "lwr", "laserType": "designator", "bearing": 10})
        self.assertEqual(w.entities["blue_tanks_wing_1"].systems["smoke"].state["remaining"], 8)

    def test_designation_triggers_target_lwr(self):
        w = self.world()
        w.entities["blue_attack_lead"].state["position"] = {"lat": 39.66, "lon": 27.29}
        w.emit("core.event.sensor.detection", "blue_attack_lead", {"system": "eo", "target": "red_ad_shorad_1", "identity": "hostile"})
        # ATGM menzili dışında: saldırı yok -> işaretleme yok; hedefi menzile al
        w.entities["red_ad_shorad_1"].state["position"] = {"lat": 39.69, "lon": 27.31}
        w.time = 20
        w.emit("core.event.sensor.detection", "blue_attack_lead", {"system": "eo", "target": "red_ad_shorad_1", "identity": "hostile"})
        self.assertIn("attack.lase_for_missile", fired(w, "blue_attack_lead"))

    def test_atc_divert_and_artillery(self):
        w = self.world()
        w.advance(300.0)
        self.assertIn("civil.divert_on_instruction", fired(w, "civ_ga"))
        w.advance(600.0)
        self.assertEqual(w.entities["red_artillery_gun_1"].systems["howitzer"].inventory["he"]["count"], 38)
        self.assertEqual(w.vars["phase"], 2)


class Golden(unittest.TestCase):
    def test_combined_arms_trace(self):
        script = json.loads((ROOT / "examples/scripts/combined_arms_engagement.json").read_text(encoding="utf-8"))
        w = World(compiled(script["scenario"]))
        trace = [r.to_dict() for r in run_script(w, script) if r.type in TRACE_TYPES]
        golden = json.loads((ROOT / "conformance/traces/combined_arms_engagement.trace.json").read_text(encoding="utf-8"))
        self.assertEqual(trace, golden)


if __name__ == "__main__":
    unittest.main()
