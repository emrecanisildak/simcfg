"""Editör sunucu API'si testleri (HTTP katmanı olmadan, Api sınıfı üzerinden)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from simcfg.server import EDITOR_DIR, Api  # noqa: E402
from test_simcfg import TempWorkspace  # noqa: E402


class ApiTests(unittest.TestCase):
    def test_editor_assets_exist(self):
        for f in ("index.html", "app.js", "style.css"):
            self.assertTrue((EDITOR_DIR / f).is_file(), f)

    def test_catalog_and_view(self):
        api = Api(ROOT)
        cat = api.catalog()
        self.assertIn("PlatformTemplate", cat["catalog"]["documents"])
        self.assertIn("munitions", cat["catalog"]["palettes"])
        v = api.doc("demo.platform.fighter_mk2")
        self.assertEqual(v["index"], 0)
        self.assertAlmostEqual(v["effective"]["systems"]["radar"]["parameters"]["maxRange"], 120 * 1852)
        self.assertIn("demo.platform.fighter_mk2_ew", v["children"])
        self.assertTrue(api.doc("no.such.doc")["missing"])

    def test_check_does_not_write(self):
        with TempWorkspace() as t:
            api = Api(t.dir)
            f = t.dir / "packages/demo/platforms/fighters.json"
            before = f.read_text(encoding="utf-8")
            raw = api.doc("demo.platform.fighter_mk2")["raw"]
            raw["spec"]["systems"]["radar"]["parameters"] = {"maxTracks": 0}
            r = api.check("demo.platform.fighter_mk2", raw)
            self.assertIn("value.range", {d["code"] for d in r["diagnostics"] if d.get("doc") == "demo.platform.fighter_mk2"})
            self.assertEqual(f.read_text(encoding="utf-8"), before)

    def test_save_into_array_file_keeps_siblings(self):
        with TempWorkspace() as t:
            api = Api(t.dir)
            raw = api.doc("demo.platform.fighter_mk2")["raw"]
            raw["spec"]["systems"]["chaff"] = {"fitted": False}
            r = api.save("demo.platform.fighter_mk2", raw)
            data = json.loads((t.dir / "packages/demo/platforms/fighters.json").read_text(encoding="utf-8"))
            self.assertEqual(data[0]["spec"]["systems"]["chaff"], {"fitted": False})
            self.assertEqual([d["id"] for d in data[1:]], ["demo.platform.fighter_mk2_export", "demo.platform.mixin.aft_dispensers",
                                                          "demo.platform.fighter_mk2_ew", "demo.platform.sam_battery"])
            self.assertNotIn("chaff", r["view"]["effective"]["systems"])

    def test_save_rejects_duplicate_rename(self):
        with TempWorkspace() as t:
            api = Api(t.dir)
            raw = api.doc("demo.platform.fighter_mk2")["raw"]
            raw["id"] = "demo.platform.sam_battery"
            with self.assertRaises(ValueError):
                api.save("demo.platform.fighter_mk2", raw)

    def test_derive_and_compile(self):
        with TempWorkspace() as t:
            api = Api(t.dir)
            r = api.derive("core.munition.generic_mrm", "demo.munition.mrm_er", "MRM-ER", False)
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["file"], "packages/demo/munitions/mrm_er.json")
            self.assertFalse(api.derive("core.munition.generic_mrm", "demo.munition.mrm_er", None, False)["ok"])
            c = api.compile("demo.scenario.strait_patrol")
            self.assertEqual([d for d in c["diagnostics"] if d["severity"] == "error"], [])
            self.assertIn("core.munition.generic_sam_mr", c["compiled"]["munitions"])


if __name__ == "__main__":
    unittest.main()
