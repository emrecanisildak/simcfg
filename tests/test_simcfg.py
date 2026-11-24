"""simcfg referans araç zinciri testleri.  Çalıştırma:  .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from simcfg import Builder, Diagnostics, Validator, Workspace  # noqa: E402
from simcfg.editor import derive  # noqa: E402
from simcfg.merge import MergeError, merge, record_provenance  # noqa: E402
from simcfg.units import UnitCatalog  # noqa: E402
from simcfg.workspace import HAVE_JSONSCHEMA, version_satisfies  # noqa: E402


def load(root: Path = ROOT) -> tuple[Workspace, Diagnostics]:
    d = Diagnostics()
    return Workspace(root, d).load(), d


def validate(root: Path = ROOT) -> Diagnostics:
    ws, d = load(root)
    Validator(ws, d).run()
    return d


class TempWorkspace:
    """Depoyu geçici dizine kopyalar; JSON dosyalarını değiştirmeye izin verir."""

    def __enter__(self) -> "TempWorkspace":
        self.dir = Path(tempfile.mkdtemp(prefix="simcfg-test-"))
        for name in ("schemas", "packages"):
            shutil.copytree(ROOT / name, self.dir / name)
        shutil.copy(ROOT / "simcfg.workspace.json", self.dir)
        return self

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def edit(self, rel: str, fn) -> None:
        p = self.dir / rel
        data = json.loads(p.read_text(encoding="utf-8"))
        fn(data)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def write(self, rel: str, data) -> None:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def codes(d: Diagnostics, severity: str = "error") -> set[str]:
    return {x.code for x in d.items if x.severity == severity}


# ---------------------------------------------------------------------------------------------
class MergeTests(unittest.TestCase):
    def test_deep_merge_and_directives(self):
        base = {"a": {"x": 1, "y": 2}, "l": [1, 2], "k": 5, "m": {"p": 1}}
        patch = {"a": {"y": 3, "z": 4}, "l": {"$append": [3]}, "k": {"$delete": True}, "m": {"$replace": {"q": 2}}}
        self.assertEqual(merge(base, patch), {"a": {"x": 1, "y": 3, "z": 4}, "l": [1, 2, 3], "m": {"q": 2}})
        self.assertEqual(base["l"], [1, 2], "girdi değiştirilmemeli")

    def test_prepend_and_array_replace(self):
        self.assertEqual(merge({"l": [2]}, {"l": {"$prepend": [1]}}), {"l": [1, 2]})
        self.assertEqual(merge({"l": [1, 2]}, {"l": [9]}), {"l": [9]})

    def test_bad_directive(self):
        with self.assertRaises(MergeError):
            merge({"a": 1}, {"a": {"$append": [1]}})

    def test_provenance(self):
        prov: dict = {}
        record_provenance({"a": {"x": 1, "y": 2}}, "base", prov)
        record_provenance({"a": {"y": 3}, "b": 1}, "child", prov)
        record_provenance({"a": {"$replace": {"z": 1}}}, "grandchild", prov)
        self.assertEqual(prov, {"a": "grandchild", "b": "child"})


class UnitTests(unittest.TestCase):
    def setUp(self):
        ws, _ = load()
        self.u = ws.units

    def test_conversions(self):
        self.assertAlmostEqual(self.u.to_unit("40 nmi", "m"), 74080)
        self.assertAlmostEqual(self.u.to_unit({"value": 1, "unit": "kft"}, "ft"), 1000)
        self.assertAlmostEqual(self.u.to_unit("10 GHz", "Hz"), 1e10)
        self.assertAlmostEqual(self.u.to_unit("-30 dBW", "dBm"), 0)  # offset dönüşümü
        self.assertAlmostEqual(self.u.to_unit("25 degC", "K"), 298.15)
        self.assertEqual(self.u.to_unit(5, "m"), 5)

    def test_dimension_mismatch(self):
        with self.assertRaises(ValueError):
            self.u.to_unit("5 kg", "m")
        self.assertIsNone(UnitCatalog.parse("abc"))


class VersionTests(unittest.TestCase):
    def test_ranges(self):
        self.assertTrue(version_satisfies("1.4.2", "^1.0.0"))
        self.assertFalse(version_satisfies("2.0.0", "^1.0.0"))
        self.assertTrue(version_satisfies("1.2.9", "~1.2.0"))
        self.assertFalse(version_satisfies("1.3.0", "~1.2.0"))
        self.assertTrue(version_satisfies("3.0.0", ">=1.0.0"))
        self.assertTrue(version_satisfies("0.1.0", "*"))


# ---------------------------------------------------------------------------------------------
class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws, cls.d = load()
        cls.vd = Diagnostics()
        Validator(Workspace(ROOT, cls.vd).load(), cls.vd).run()

    def test_repository_is_valid(self):
        self.assertEqual([x.format() for x in self.vd.errors], [])
        self.assertEqual([x.format() for x in self.vd.warnings if x.code != "schema.unavailable"], [])

    def test_mixin_linearization(self):
        r = self.ws.resolve("demo.platform.fighter_mk2_ew")
        self.assertEqual(r.linear, [
            "core.platform.air.generic_fixed_wing", "core.platform.air.generic_fighter",
            "demo.platform.fighter_mk2", "demo.platform.mixin.aft_dispensers", "demo.platform.fighter_mk2_ew"])
        self.assertIn("chaff_aft", r.spec["systems"])
        self.assertEqual(r.spec["systems"]["radar"]["template"], "demo.system.radar.fcr_long_range")

    def test_cumulative_emits(self):
        rwr = self.ws.resolve("core.systemtype.rwr")
        for ev in ("core.event.system.failed", "core.event.sensor.detection", "core.event.rwr.lock"):
            self.assertIn(ev, rwr.spec["emits"])

    def test_is_a(self):
        self.assertTrue(self.ws.is_a("core.systemtype.chaff_dispenser", "core.systemtype.cm_dispenser"))
        self.assertFalse(self.ws.is_a("core.systemtype.radar", "core.systemtype.cm_dispenser"))

    def test_append_directive_in_template(self):
        r = self.ws.resolve("core.system.iff.generic_combined")
        self.assertEqual(r.spec["parameters"]["supportedModes"], ["1", "2", "3A", "C", "4", "5"])


class CompileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ws, d = load()
        b = Builder(ws, d)
        cls.day = b.compile_scenario("demo.scenario.strait_patrol")
        cls.night = b.compile_scenario("demo.scenario.strait_patrol_night")
        cls.d = d

    def test_no_errors(self):
        self.assertEqual([x.format() for x in self.d.errors], [])

    def test_units_and_defaults(self):
        v = self.day["entities"]["viper_1"]
        self.assertAlmostEqual(v["parameters"]["maxSpeed"], 1450 / 3.6)
        self.assertAlmostEqual(v["parameters"]["ceiling"], 50000 * 0.3048)
        self.assertEqual(v["initialState"]["fuel"], 4100.0, "defaultFrom parameters.fuelCapacity")
        self.assertAlmostEqual(v["systems"]["radar"]["parameters"]["maxRange"], 120 * 1852)
        self.assertEqual(v["systems"]["radar"]["parameters"]["noiseFigure"], 5, "SystemType varsayılanı")
        self.assertEqual(v["systems"]["chaff"]["initialState"]["remaining"], 30, "defaultFrom capacity")

    def test_entity_overrides(self):
        v1, v2 = self.day["entities"]["viper_1"], self.day["entities"]["viper_2"]
        self.assertEqual(v1["systems"]["iff"]["initialState"]["mode3Code"], "4521")
        self.assertEqual(v2["systems"]["chaff"]["initialState"]["remaining"], 12)
        self.assertEqual(v1["systems"]["flare"]["parameters"]["capacity"], 60)
        self.assertIn("patrol", v1["behaviors"])
        self.assertFalse(v2["behaviors"]["self_defense"]["rules"]["notch_without_flares"]["enabled"])

    def test_behavior_layer_order(self):
        sd = self.day["entities"]["viper_1"]["behaviors"]["self_defense"]["parameters"]
        self.assertEqual(sd["chaffBurst"], 5, "platform eki (fighter_mk2)")
        self.assertEqual(sd["breakG"], 7, "senaryo behaviorOverrides")

    def test_fitted_and_disabled(self):
        a = self.day["entities"]["airliner_1"]
        self.assertEqual(sorted(a["unfittedSystems"]), ["flare", "rwr"])
        self.assertNotIn("rwr", a["systems"])
        self.assertFalse(a["behaviors"]["self_defense"]["enabled"])

    def test_role_binding(self):
        self.assertEqual(self.day["entities"]["sam_1"]["behaviors"]["air_defense"]["bindings"],
                         {"radar": "search_radar", "iff": "iff", "launcher": "launcher"})

    def test_night_variant(self):
        n = self.night
        self.assertNotIn("airliner_1", n["entities"])
        self.assertIn("bandit_2", n["entities"])
        self.assertEqual(n["time"]["start"], "2026-06-01T22:00:00Z")
        self.assertEqual(n["environment"]["weather"]["visibility"], "5 km")
        self.assertEqual(n["environment"]["weather"]["cloudBase"], "3000 ft", "derin birleştirme")
        self.assertEqual(n["rules"]["phase_2_red_push"]["trigger"], {"at": 1500.0})
        self.assertNotIn("civil_in_mez_alert", n["rules"])
        v2 = n["entities"]["viper_2"]
        self.assertEqual(v2["template"], "demo.platform.fighter_mk2_ew")
        sd = v2["behaviors"]["self_defense"]
        self.assertEqual(sd["behavior"], "demo.behavior.aggressive_self_defense")
        self.assertEqual(sd["parameters"]["chaffBurst"], 3, "türev davranışa üst davranış override'ı uygulanır")
        self.assertEqual(sd["rules"]["flare_and_break_on_launch"]["priority"], 110)
        self.assertNotIn("magazine_empty", sd["rules"])
        self.assertFalse(n["entities"]["sam_1"]["behaviors"]["air_defense"]["rules"]["identify_contact"]["enabled"])

    def test_optional_role_disables_rule(self):
        rules = self.night["entities"]["bandit_2"]["behaviors"]["self_defense"]["rules"]
        self.assertFalse(rules["chaff_on_lock"]["enabled"])
        self.assertIn("chaff", rules["chaff_on_lock"]["disabledReason"])
        self.assertTrue(rules["flare_and_break_on_launch"]["enabled"])

    def test_catalog_included(self):
        self.assertIn("core.action.dispense_chaff", self.day["catalog"]["ActionType"])
        self.assertNotIn("core.systemtype.base", self.day["catalog"]["SystemType"], "soyutlar dahil edilmez")


# ---------------------------------------------------------------------------------------------
class ErrorDetectionTests(unittest.TestCase):
    def test_parameter_errors(self):
        with TempWorkspace() as t:
            def f(d):
                d[0]["spec"]["parameters"].update({"maxRang": 5, "peakPower": "8 km", "maxTracks": 0})
            t.edit("packages/demo/systems/systems.json", f)
            c = codes(validate(t.dir))
            self.assertTrue({"param.unknown", "value.unit", "value.range"} <= c, c)

    def test_expression_errors(self):
        with TempWorkspace() as t:
            def f(d):
                r = d["spec"]["rules"]["jink_on_track"]
                r["condition"] = {"all": [{"op": "gt", "args": [{"var": "self.systems.rwr.state.nope"}]},
                                          {"var": "event.bearingg"}, {"param": "unknownParam"}]}
                r["actions"].append({"action": "core.action.dispense_flare", "params": {"system": "radar"}})
            t.edit("packages/demo/behaviors/aggressive_self_defense.json", f)
            c = codes(validate(t.dir))
            self.assertTrue({"expr.arity", "expr.var", "expr.param", "role.unknown"} <= c, c)

    def test_scenario_errors(self):
        with TempWorkspace() as t:
            def f(d):
                e = d["spec"]["entities"]
                e["bandit_1"]["plan"][0]["params"]["route"] = "nope"
                e["viper_1"]["overrides"]["systems"]["iff"]["initialState"]["mode3Code"] = "9999"
                e["viper_1"]["side"] = "green"
                e["viper_2"]["overrides"]["behaviors"]["self_defense"]["rules"]["ghost"] = {"enabled": False}
                d["spec"]["rules"]["phase_2_red_push"]["actions"].append(
                    {"action": "core.action.set_system_state", "on": "bandit_1",
                     "params": {"system": "radar", "state": {"emitting": True}}})
                d["spec"]["rules"]["phase_2_red_push"]["actions"].append(
                    {"action": "core.action.log", "params": {"message": {"var": "self.speed"}}})
            t.edit("packages/demo/scenarios/strait_patrol.json", f)
            c = codes(validate(t.dir))
            self.assertTrue({"ref.scenario", "value.pattern", "side.unknown", "rule.unknown", "param.readonly", "expr.var"} <= c, c)

    def test_required_role_and_cycle(self):
        with TempWorkspace() as t:
            def f(d):
                d[1]["spec"]["systems"]["rwr"] = {"fitted": False}
                d[2]["extends"] = "demo.platform.fighter_mk2_ew"
            t.edit("packages/demo/platforms/fighters.json", f)
            d = validate(t.dir)
            self.assertTrue({"role.missing", "extends.cycle"} <= codes(d), codes(d))
            self.assertIn("rule.never_fires", codes(d, "warning"))

    def test_override_level(self):
        with TempWorkspace() as t:
            t.edit("packages/core/system-types/radar.json",
                   lambda d: d["spec"]["parameters"]["frequency"].update({"overridableAt": "template"}))
            t.edit("packages/demo/scenarios/strait_patrol.json",
                   lambda d: d["spec"]["entities"]["viper_1"]["overrides"]["systems"].update(
                       {"radar": {"parameters": {"frequency": "9.5 GHz"}}}))
            self.assertIn("param.override_level", codes(validate(t.dir)))

    def test_mount_not_allowed_and_abstract(self):
        with TempWorkspace() as t:
            t.edit("packages/core/platform-types/platform_types.json",
                   lambda d: d[2]["spec"].update({"allowedSystemTypes": ["core.systemtype.sensor"]}))
            t.edit("packages/demo/scenarios/strait_patrol.json",
                   lambda d: d["spec"]["entities"]["bandit_1"].update({"platform": "core.platform.air.generic_fixed_wing"}))
            c = codes(validate(t.dir))
            self.assertTrue({"mount.not_allowed", "ref.abstract"} <= c, c)

    def test_envelope_and_namespace(self):
        with TempWorkspace() as t:
            t.write("packages/demo/extra/bad.json", [
                {"apiVersion": "simcfg/v2", "kind": "Enumeration", "id": "demo.enum.x", "spec": {"values": ["a"]}},
                {"apiVersion": "simcfg/v1", "kind": "Enumeration", "id": "core.enum.stolen", "spec": {"values": ["a"]}},
                {"apiVersion": "simcfg/v1", "kind": "Widget", "id": "demo.widget.x", "spec": {}},
                {"apiVersion": "simcfg/v1", "kind": "Enumeration", "id": "demo.enum.dup", "spec": {"values": ["a", "a"]}},
            ])
            c = codes(validate(t.dir))
            expected = {"doc.apiVersion", "doc.namespace", "doc.kind", "enum.duplicate"}
            if HAVE_JSONSCHEMA:
                expected.add("schema")
            self.assertTrue(expected <= c, c)

    def test_missing_dependency_visibility(self):
        with TempWorkspace() as t:
            t.edit("packages/demo/simpkg.json", lambda d: d["spec"].update({"dependencies": {"core": "^2.0.0"}}))
            self.assertIn("package.dependency", codes(validate(t.dir)))


class EditorTests(unittest.TestCase):
    def test_derive_creates_valid_document(self):
        with TempWorkspace() as t:
            ws, d = load(t.dir)
            p = derive(ws, d, "core.platform.air.generic_fighter", "demo.platform.trainer", name="Trainer")
            self.assertIsNotNone(p)
            self.assertTrue(p.exists())
            vd = validate(t.dir)
            self.assertEqual([x.format() for x in vd.errors], [])
            ws2, _ = load(t.dir)
            self.assertEqual(ws2.resolve("demo.platform.trainer").linear[-2], "core.platform.air.generic_fighter")
            self.assertIsNone(derive(ws2, Diagnostics(), "core.platform.air.generic_fighter", "demo.platform.trainer"))


if __name__ == "__main__":
    unittest.main()
