#!/usr/bin/env python3
"""直接测绿联 NAS 守护的规则引擎、df 解析和术语文案，不需要宿主或桩。"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
NAS_PATH = ROOT / "plugins.v3" / "ugreenguard" / "nas.py"
RENDER_PATH = ROOT / "plugins.v3" / "ugreenguard" / "renderer.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nas = _load("ugreenguard_nas", NAS_PATH)
renderer = _load("ugreenguard_renderer", RENDER_PATH)

MISSING = nas.MISSING

# 一行表头 + 真分区 + 一堆伪文件系统 + 一行重复设备 + 一个带空格的挂载点
DF_SAMPLE = """Filesystem 1024-blocks      Used Available Capacity Mounted on
/dev/md0       19275980   1234567  18041413       7% /
tmpfs           8192000         0   8192000       0% /dev/shm
devtmpfs        8184000         0   8184000       0% /dev
proc                   0         0         0        - /proc
/dev/sdb1     3907003264 2000000000 1907003264      52% /volume1
/dev/sdc1      976762584  100000000  876762584      11% /volume2
/dev/sdb1     3907003264 2000000000 1907003264      52% /mnt/bind1
/dev/mapper/cache  999999  500000  499999  51% /mnt/my data
"""


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


class TestParseDf(unittest.TestCase):
    def test_empty_and_odd_input(self):
        for text in ("", None, "   \n\n", "hello world", "Filesystem 1024-blocks Used Available Capacity Mounted on"):
            self.assertEqual(nas.parse_df(text), [], text)

    def test_header_and_pseudo_fs_are_filtered(self):
        volumes = nas.parse_df(DF_SAMPLE)
        names = [v["name"] for v in volumes]
        for bad in ("/dev/shm", "/dev", "/proc", "/mnt/bind1"):
            self.assertNotIn(bad, names, bad)
        for word in ("tmpfs", "devtmpfs", "proc"):
            self.assertNotIn(word, [v["name"] for v in volumes])

    def test_real_partitions_and_duplicate_removal(self):
        volumes = nas.parse_df(DF_SAMPLE)
        self.assertEqual(len(volumes), 4)
        self.assertEqual([v["name"] for v in volumes], ["/", "/volume1", "/volume2", "/mnt/my data"])
        # 重复设备只留第一条
        self.assertEqual(len([v for v in volumes if v["name"] == "/volume1"]), 1)

    def test_schema_and_units(self):
        vol = next(v for v in nas.parse_df(DF_SAMPLE) if v["name"] == "/volume1")
        self.assertEqual(set(vol), {"name", "total", "used", "percent"})
        self.assertEqual(vol["total"], 3907003264 * 1024)
        self.assertEqual(vol["used"], 2000000000 * 1024)
        self.assertIsInstance(vol["percent"], float)

    def test_percent_from_used_over_available_not_capacity(self):
        vol = next(v for v in nas.parse_df(DF_SAMPLE) if v["name"] == "/")
        # df 自己写 7%，按 Used/(Used+Available) 算应该是 6.4%
        self.assertAlmostEqual(vol["percent"], 6.4, places=1)
        self.assertNotEqual(vol["percent"], 7.0)

    def test_mount_point_with_spaces_kept_whole(self):
        vol = next(v for v in nas.parse_df(DF_SAMPLE) if v["name"] == "/mnt/my data")
        self.assertEqual(vol["total"], 999999 * 1024)

    def test_malformed_lines_are_skipped(self):
        text = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/sda1 abc 100 100 50% /a\n"
            "short line\n"
            "/dev/sda2 1000 400 600 40% /b\n"
            "0 0 0 0 0 /zero\n"
        )
        volumes = nas.parse_df(text)
        self.assertEqual([v["name"] for v in volumes], ["/b"])

    def test_output_fits_unified_status_schema(self):
        status = nas.parse_status({"volumes": nas.parse_df(DF_SAMPLE)})
        self.assertEqual(len(status["volumes"]), 4)
        self.assertEqual(status["disks"], [])
        self.assertEqual(status["ups"], {"on_battery": MISSING, "battery": MISSING})


class TestParamikoAndCollect(unittest.TestCase):
    def test_paramiko_missing_returns_explicit_state(self):
        with patch.dict(sys.modules, {"paramiko": None}):
            state = nas.paramiko_state()
        self.assertEqual(state, {
            "available": False,
            "reason": "容器里没有 paramiko，在 MoviePilot 容器执行 pip install paramiko 后重启插件",
        })

    def test_paramiko_state_never_raises(self):
        with patch.dict(sys.modules, {"paramiko": None}):
            self.assertIsInstance(nas.ssh_info({"ssh_host": "10.0.0.2"}), dict)
            self.assertEqual(nas._read_ssh({"ssh_host": "10.0.0.2"}), None)

    def test_auto_downgrades_when_paramiko_missing(self):
        raw = {"volumes": [{"name": "v", "percent": 10}]}
        with patch.dict(sys.modules, {"paramiko": None}), \
             patch.object(nas, "_read_bridge", return_value=raw), \
             patch.object(nas, "_read_status_file", return_value=None), \
             patch.object(nas, "local_raw", return_value={"volumes": []}):
            result = nas.collect({
                "source_mode": "自动", "ssh_host": "10.0.0.2",
                "bridge_url": "http://x/status", "status_file": "/tmp/x.json",
            })
        self.assertEqual(result["source"], "状态桥")
        self.assertIs(result["ssh"]["available"], False)
        self.assertIn("paramiko", result["ssh"]["reason"])

    def test_auto_order_ssh_then_bridge_then_file_then_local(self):
        raw = {"volumes": [{"name": "v", "percent": 10}]}
        with patch.object(nas, "paramiko_state", return_value={"available": True, "reason": ""}), \
             patch.object(nas, "_read_ssh", return_value=raw), \
             patch.object(nas, "_read_bridge", return_value={"volumes": [{"name": "b", "percent": 1}]}), \
             patch.object(nas, "_read_status_file", return_value={"volumes": [{"name": "f", "percent": 1}]}), \
             patch.object(nas, "local_raw", return_value={"volumes": [{"name": "l", "percent": 1}]}):
            result = nas.collect({
                "source_mode": "自动", "ssh_host": "10.0.0.2",
                "bridge_url": "http://x", "status_file": "/tmp/x.json",
            })
        self.assertEqual(result["source"], "SSH")
        self.assertIs(result["ssh"]["used"], True)

    def test_ssh_mode_only_reports_volumes(self):
        raw = {"ts": "x", "volumes": [{"name": "/volume1", "total": 1024, "used": 900, "percent": 87.9}]}
        with patch.object(nas, "paramiko_state", return_value={"available": True, "reason": ""}), \
             patch.object(nas, "_read_ssh", return_value=raw):
            result = nas.collect({"source_mode": "SSH", "ssh_host": "10.0.0.2"})
        self.assertEqual(result["source"], "SSH")
        self.assertEqual(result["status"]["disks"], [])
        self.assertEqual(result["status"]["containers"], [])
        self.assertEqual(result["status"]["ups"]["on_battery"], MISSING)
        self.assertIn("no_data", rules(nas.evaluate(result["status"], cfg())))

    def test_ssh_failure_falls_back_to_local(self):
        with patch.object(nas, "paramiko_state", return_value={"available": True, "reason": ""}), \
             patch.object(nas, "_read_ssh", return_value=None), \
             patch.object(nas, "local_raw", return_value={"volumes": [{"name": "/", "percent": 5}]}):
            result = nas.collect({"source_mode": "SSH", "ssh_host": "10.0.0.2"})
        self.assertEqual(result["source"], "容器自检")

    def test_explicit_modes_still_keep_three_original_paths(self):
        with patch.object(nas, "_read_bridge", return_value={"volumes": []}):
            self.assertEqual(nas.collect({"source_mode": "状态桥", "bridge_url": "http://x"})["source"], "状态桥")
        with patch.object(nas, "_read_status_file", return_value={"volumes": []}):
            self.assertEqual(nas.collect({"source_mode": "本地文件", "status_file": "/tmp/x"})["source"], "本地文件")
        self.assertEqual(nas.collect({"source_mode": "容器自检"})["source"], "容器自检")

    def test_invalid_mode_defaults_to_auto(self):
        self.assertEqual(nas.collect({"source_mode": "瞎写的"})["source_mode"], "自动")

    def test_ssh_port_parsing(self):
        self.assertEqual(nas._ssh_port({"ssh_port": "2222"}), 2222)
        self.assertEqual(nas._ssh_port({"ssh_port": "abc"}), 22)
        self.assertEqual(nas._ssh_port({"ssh_port": 99999}), 22)
        self.assertEqual(nas._ssh_port({}), 22)


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

    def test_volume_hit_uses_disk_usage_wording(self):
        hit = next(h for h in nas.evaluate(full(volumes=[{"name": "v", "percent": 95}]), cfg())
                   if h["rule"] == "volume_high")
        self.assertIn("磁盘占用空间达到", hit["detail"])
        self.assertNotIn("水位", hit["detail"])

    def test_internal_rule_id_stays_put(self):
        self.assertIn("volume_high", rules(nas.evaluate(full(volumes=[{"name": "v", "percent": 95}]), cfg())))

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


class TestRendererText(unittest.TestCase):
    def _view(self, **overrides):
        data = {
            "generated_at": "2026-11-12 10:00:00",
            "status": full(),
            "hits": [],
            "source": "容器自检",
            "services": None,
            "ssh": {"available": False, "reason": "容器里没有 paramiko", "host": "", "used": False, "note": ""},
            "source_mode": "自动",
            "log": [],
            "paused": False,
            "mode": "试运行",
            "cfg": {"water_threshold": 88, "temp_threshold": 52, "watch_containers": []},
            "cooldown": 1800,
        }
        data.update(overrides)
        return data

    def test_no_water_word_and_new_term_on_page(self):
        html = renderer.render(self._view())
        self.assertNotIn("水位", html)
        self.assertIn("磁盘占用空间", html)

    def test_dynamic_volume_name_is_escaped(self):
        status = full(volumes=[{"name": "<img src=x onerror=alert(1)>", "percent": 95}])
        html = renderer.render(self._view(status=status))
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;img", html)

    def test_ssh_host_is_escaped_and_password_not_shown(self):
        dirty = "<svg onload=alert(1)>"
        ssh = {"available": False, "reason": "容器里没有 paramiko，在 MoviePilot 容器执行 pip install paramiko 后重启插件",
               "host": dirty, "used": True, "note": ""}
        html = renderer.render(self._view(source="SSH", ssh=ssh))
        self.assertNotIn(dirty, html)
        self.assertIn("&lt;svg", html)
        self.assertIn("paramiko", html)
        self.assertIn("未上报", html)
        self.assertNotIn("ssh_password", html)

    def test_source_and_paramiko_state_shown(self):
        html = renderer.render(self._view(source="SSH", source_mode="SSH"))
        self.assertIn("当前生效来源：SSH", html)
        self.assertIn("paramiko 不可用", html)


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
