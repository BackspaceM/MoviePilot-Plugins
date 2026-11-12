#!/usr/bin/env python3
"""直接测绿联 NAS 守护的规则引擎和状态解析，不需要宿主或桩。"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
NAS_PATH = ROOT / "plugins.v3" / "ugreenguard" / "nas.py"
_spec = importlib.util.spec_from_file_location("ugreenguard_nas", NAS_PATH)
nas = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nas)

MISSING = nas.MISSING


def cfg(**overrides):
    base = {"water_threshold": 88, "temp_threshold": 52}
    base.update(overrides)
    return base


def full(**overrides):
    raw = {
        "ts": "2026-11-12 10:00:00",
        "volumes": [{"name": "volume1", "total": 2000, "used": 800, "percent": 40.0}],
        "disks": [{"name": "disk1", "temp": 40, "health": "正常", "smart_alert": False}],
        "containers": [
            {"name": "moviepilot", "status": "running", "restarts": 0},
            {"name": "qbittorrent", "status": "running", "restarts": 1},
        ],
        "ups": {"on_battery": False, "battery": 100},
        "system": {"cpu": 20.0, "mem": 45.0},
    }
    raw.update(overrides)
    return nas.parse_status(raw)


def rules(hits):
    return {h["rule"] for h in hits}


def actions(hits):
    return {h["action"] for h in hits}


class TestParseStatus(unittest.TestCase):
    def test_empty_raw_gives_full_schema_with_missing_markers(self):
        for raw in ({}, None, "x", [], 3):
            status = nas.parse_status(raw)
            self.assertEqual(set(status), {"ts", "volumes", "disks", "containers", "ups", "system"})
            self.assertEqual(status["ts"], MISSING)
            self.assertEqual(status["volumes"], [])
            self.assertEqual(status["disks"], [])
            self.assertEqual(status["containers"], [])
            self.assertEqual(status["ups"], {"on_battery": MISSING, "battery": MISSING})
            self.assertEqual(status["system"], {"cpu": MISSING, "mem": MISSING})

    def test_partial_entries_mark_missing_fields(self):
        status = nas.parse_status({
            "volumes": [{"name": "vol1"}],
            "disks": [{"name": "disk1"}],
            "containers": [{"name": "moviepilot", "restarts": 3}],
            "ups": {"battery": 90},
        })
        vol = status["volumes"][0]
        self.assertEqual(vol["name"], "vol1")
        self.assertEqual(vol["total"], MISSING)
        self.assertEqual(vol["used"], MISSING)
        self.assertEqual(vol["percent"], MISSING)
        disk = status["disks"][0]
        self.assertEqual(disk["temp"], MISSING)
        self.assertEqual(disk["health"], MISSING)
        self.assertEqual(disk["smart_alert"], MISSING)
        self.assertEqual(status["containers"][0]["status"], MISSING)
        self.assertEqual(status["containers"][0]["restarts"], 3)
        self.assertEqual(status["ups"]["on_battery"], MISSING)
        self.assertEqual(status["ups"]["battery"], 90)

    def test_coerces_strings_and_bools(self):
        status = nas.parse_status({
            "volumes": [{"name": "vol1", "percent": "95", "total": "1000", "used": "950"}],
            "disks": [{"name": "disk1", "temp": "60.5", "smart_alert": "true"}],
            "ups": {"on_battery": "false", "battery": "88"},
        })
        self.assertEqual(status["volumes"][0]["percent"], 95)
        self.assertEqual(status["volumes"][0]["total"], 1000)
        self.assertEqual(status["disks"][0]["temp"], 60.5)
        self.assertIs(status["disks"][0]["smart_alert"], True)
        self.assertIs(status["ups"]["on_battery"], False)
        self.assertEqual(status["ups"]["battery"], 88)

    def test_bad_items_are_skipped(self):
        status = nas.parse_status({
            "volumes": ["oops", 1, None, {"name": "vol1", "percent": 10}],
            "disks": {"not": "a list"},
        })
        self.assertEqual(len(status["volumes"]), 1)
        self.assertEqual(status["volumes"][0]["percent"], 10)
        self.assertEqual(status["disks"], [])

    def test_missing_bool_is_not_false(self):
        self.assertEqual(nas._as_bool(None), MISSING)
        self.assertEqual(nas._as_bool("未上报"), MISSING)
        self.assertIs(nas._as_bool("yes"), True)
        self.assertIs(nas._as_bool(0), False)
        self.assertEqual(nas._as_num(True), MISSING)
        self.assertEqual(nas._as_num("abc"), MISSING)


class TestEvaluate(unittest.TestCase):
    def test_hit_fields_are_stable(self):
        hits = nas.evaluate(full(), cfg())
        self.assertTrue(hits)
        for hit in hits:
            self.assertEqual(set(hit), {"rule", "level", "detail", "action"})
            self.assertIsInstance(hit["detail"], str)

    def test_empty_status_never_resumes(self):
        hits = nas.evaluate(nas.parse_status({}), cfg())
        self.assertNotIn("resume_download", actions(hits))
        self.assertIn("no_data", rules(hits))
        self.assertEqual(hits[0]["action"], "none")

    def test_partial_data_never_resumes(self):
        status = nas.parse_status({"volumes": [{"name": "vol1", "percent": 40}]})
        hits = nas.evaluate(status, cfg())
        self.assertNotIn("resume_download", actions(hits))
        self.assertIn("no_data", rules(hits))

    def test_all_normal_resumes(self):
        hits = nas.evaluate(full(), cfg())
        self.assertEqual(actions(hits), {"resume_download"})
        self.assertIn("resume", rules(hits))

    def test_volume_threshold_boundary(self):
        self.assertNotIn("volume_high", rules(nas.evaluate(full(volumes=[{"name": "v", "percent": 87.9}]), cfg())))
        self.assertIn("volume_high", rules(nas.evaluate(full(volumes=[{"name": "v", "percent": 88}]), cfg())))
        hit = nas.evaluate(full(volumes=[{"name": "v", "percent": 95}]), cfg())[0]
        self.assertEqual(hit["action"], "pause_download")
        self.assertEqual(hit["level"], "danger")
        self.assertNotIn("volume_high", rules(nas.evaluate(full(volumes=[{"name": "v", "percent": 90}]),
                                                          cfg(water_threshold=95))))
        self.assertIn("volume_high", rules(nas.evaluate(full(volumes=[{"name": "v", "percent": 95}]),
                                                       cfg(water_threshold=95))))

    def test_worst_volume_is_used(self):
        status = full(volumes=[
            {"name": "small", "percent": 30},
            {"name": "big", "percent": 97},
        ])
        hit = next(h for h in nas.evaluate(status, cfg()) if h["rule"] == "volume_high")
        self.assertIn("big", hit["detail"])

    def test_disk_temp_and_smart(self):
        self.assertIn("disk_risk", rules(nas.evaluate(full(disks=[{"name": "d", "temp": 52,
                                                                    "smart_alert": False}]), cfg())))
        self.assertNotIn("disk_risk", rules(nas.evaluate(full(disks=[{"name": "d", "temp": 51.9,
                                                                      "smart_alert": False}]), cfg())))
        hit = next(h for h in nas.evaluate(full(disks=[{"name": "d", "temp": 30,
                                                                  "smart_alert": True}]), cfg())
                   if h["rule"] == "disk_risk")
        self.assertEqual(hit["action"], "pause_download")
        self.assertIn("SMART", hit["detail"])
        self.assertNotIn("disk_risk", rules(nas.evaluate(full(disks=[{"name": "d", "temp": MISSING,
                                                                      "smart_alert": MISSING}]), cfg())))

    def test_custom_temp_threshold(self):
        status = full(disks=[{"name": "d", "temp": 58, "smart_alert": False}])
        self.assertNotIn("disk_risk", rules(nas.evaluate(status, cfg(temp_threshold=60))))
        self.assertIn("disk_risk", rules(nas.evaluate(status, cfg(temp_threshold=55))))

    def test_container_down_only_notifies(self):
        status = full(containers=[{"name": "moviepilot", "status": "exited", "restarts": 2}])
        hit = next(h for h in nas.evaluate(status, cfg()) if h["rule"] == "container_down")
        self.assertEqual(hit["action"], "notify")
        self.assertEqual(hit["level"], "warn")
        self.assertIn("moviepilot", hit["detail"])

    def test_running_container_is_fine(self):
        self.assertNotIn("container_down", rules(nas.evaluate(full(), cfg())))

    def test_missing_container_status_is_not_a_false_alarm(self):
        status = full(containers=[{"name": "moviepilot", "status": MISSING}])
        self.assertNotIn("container_down", rules(nas.evaluate(status, cfg())))

    def test_custom_watch_list(self):
        status = full(containers=[
            {"name": "moviepilot", "status": "exited"},
            {"name": "jellyfin", "status": "exited"},
        ])
        self.assertIn("container_down", rules(nas.evaluate(status, cfg(watch_containers=["jellyfin"]))))
        self.assertNotIn("container_down", rules(nas.evaluate(status, cfg(watch_containers=[]))))

    def test_ups_on_battery(self):
        hit = next(h for h in nas.evaluate(full(ups={"on_battery": True, "battery": 42}), cfg())
                   if h["rule"] == "ups_battery")
        self.assertEqual(hit["action"], "pause_download")
        self.assertIn("42", hit["detail"])
        self.assertNotIn("ups_battery", rules(nas.evaluate(full(ups={"on_battery": MISSING}), cfg())))

    def test_alerts_suppress_resume(self):
        hits = nas.evaluate(full(volumes=[{"name": "v", "percent": 95}]), cfg())
        self.assertIn("pause_download", actions(hits))
        self.assertNotIn("resume_download", actions(hits))

    def test_several_rules_hit_together(self):
        status = full(
            volumes=[{"name": "v", "percent": 93}],
            disks=[{"name": "d", "temp": 60, "smart_alert": True}],
            containers=[{"name": "emby", "status": "restarting"}],
            ups={"on_battery": True, "battery": 50},
        )
        self.assertEqual(rules(nas.evaluate(status, cfg())),
                         {"volume_high", "disk_risk", "container_down", "ups_battery"})

    def test_cfg_is_not_mutated(self):
        config = cfg()
        snapshot = dict(config)
        nas.evaluate(full(volumes=[{"name": "v", "percent": 95}]), config)
        self.assertEqual(config, snapshot)


def main():
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n合计 {total} 个用例，通过 {total - failed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
