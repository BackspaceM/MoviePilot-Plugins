#!/usr/bin/env python3
"""直接测绿联 NAS 守护的规则引擎、df 解析和术语文案，不需要宿主或桩。"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import types
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
                "source_mode": "自动", "ssh_host": "10.0.0.2", "ssh_password": "pw",
                "bridge_url": "http://x", "status_file": "/tmp/x.json",
            })
        self.assertEqual(result["source"], "SSH")
        self.assertIs(result["ssh"]["used"], True)

    def test_ssh_mode_only_reports_volumes(self):
        raw = {"ts": "x", "volumes": [{"name": "/volume1", "total": 1024, "used": 900, "percent": 87.9}]}
        with patch.object(nas, "paramiko_state", return_value={"available": True, "reason": ""}), \
             patch.object(nas, "_read_ssh", return_value=raw):
            result = nas.collect({"source_mode": "SSH", "ssh_host": "10.0.0.2",
                                  "ssh_password": "pw"})
        self.assertEqual(result["source"], "SSH")
        self.assertEqual(result["status"]["disks"], [])
        self.assertEqual(result["status"]["containers"], [])
        self.assertEqual(result["status"]["ups"]["on_battery"], MISSING)
        self.assertIn("no_data", rules(nas.evaluate(result["status"], cfg())))

    def test_ssh_failure_falls_back_to_local(self):
        with patch.object(nas, "paramiko_state", return_value={"available": True, "reason": ""}), \
             patch.object(nas, "_read_ssh", return_value=None), \
             patch.object(nas, "local_raw", return_value={"volumes": [{"name": "/", "percent": 5}]}):
            result = nas.collect({"source_mode": "SSH", "ssh_host": "10.0.0.2",
                                  "ssh_password": "pw"})
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


PROBE_SAMPLE = """===UGN:meta===
1000
Linux
===UGN:df===
Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/sdb1 3907003264 3600000000 307003264 93% /volume1
tmpfs 8192000 0 8192000 0% /dev/shm
===UGN:cpu===
2.40 1.80 1.20 2/500 1234
4
===UGN:mem===
MemTotal:        8192000 kB
MemAvailable:    2048000 kB
MemFree:         1000000 kB
===UGN:docker===
moviepilot|running|Up 3 days
qbittorrent|exited|Exited (1) 2 minutes ago
===UGN:temp===
/sys/class/hwmon/hwmon0/temp1_input 41000
/dev/sda 194 Temperature_Celsius 0x0022 100 100 000 Old_age Always - 48 (Min/Max 20/50)
/dev/nvme0n1 Temperature: 39 Celsius
===UGN:health===
/dev/sda SMART overall-health self-assessment test result: FAILED
===UGN:ups===
ups battery.charge: 88
ups ups.status: OL
===UGN:end===
"""


class TestProbeParsing(unittest.TestCase):
    def test_all_sections_are_parsed(self):
        raw, reported = nas.parse_probe(PROBE_SAMPLE, {"_transport": "paramiko"})
        status = nas.parse_status(raw)
        self.assertTrue(reported["volumes"])
        self.assertTrue(reported["cpu"])
        self.assertTrue(reported["mem"])
        self.assertTrue(reported["containers"])
        self.assertTrue(reported["disks"])
        self.assertTrue(reported["ups"])
        self.assertEqual(reported["uid"], 1000)
        self.assertEqual(reported["transport"], "paramiko")
        self.assertEqual(len(status["volumes"]), 1)
        self.assertEqual(status["volumes"][0]["name"], "/volume1")
        self.assertEqual(status["system"]["cpu"], 60.0)
        self.assertEqual(status["system"]["mem"], 75.0)
        self.assertEqual(status["ups"]["on_battery"], False)
        self.assertEqual(status["ups"]["battery"], 88.0)

    def test_disk_temp_and_smart_health(self):
        _raw, _meta = nas.parse_probe(PROBE_SAMPLE)
        disks = {d["name"]: d for d in nas.parse_status(_raw)["disks"]}
        self.assertEqual(disks["hwmon0/temp1"]["temp"], 41.0)
        self.assertEqual(disks["sda"]["temp"], 48.0)
        self.assertIs(disks["sda"]["smart_alert"], True)
        self.assertEqual(disks["sda"]["health"], "异常")
        self.assertEqual(disks["nvme0n1"]["temp"], 39.0)

    def test_containers_and_probe_schema(self):
        raw, _meta = nas.parse_probe(PROBE_SAMPLE)
        containers = nas.parse_status(raw)["containers"]
        self.assertEqual([c["name"] for c in containers], ["moviepilot", "qbittorrent"])
        self.assertEqual(containers[1]["status"], "exited")
        self.assertEqual(containers[1]["restarts"], MISSING)

    def test_old_docker_without_state_field(self):
        text = ("===UGN:docker===\nmoviepilot||Up 3 days\nqbittorrent||Exited (1) 2 minutes ago\n"
                "===UGN:end===")
        raw, _meta = nas.parse_probe(text)
        containers = nas.parse_status(raw)["containers"]
        self.assertEqual([c["status"] for c in containers], ["running", "exited"])
        hits = nas.evaluate(nas.parse_status(raw), cfg(watch_containers=["moviepilot", "qbittorrent"]))
        hit = next(h for h in hits if h["rule"] == "container_down")
        self.assertEqual(hit["targets"], ["qbittorrent"])

    def test_empty_or_broken_probe_is_harmless(self):
        for text in ("", None, "随便一行", "===UGN:df===\nnot a df line\n===UGN:end==="):
            raw, reported = nas.parse_probe(text)
            self.assertIsInstance(nas.parse_status(raw), dict)
            self.assertFalse(reported["volumes"])
            self.assertFalse(reported["ups"])

    def test_missing_sections_are_marked_not_faked(self):
        raw, reported = nas.parse_probe("===UGN:df===\n===UGN:end===")
        self.assertFalse(reported["cpu"])
        self.assertFalse(reported["containers"])
        self.assertFalse(reported["ups"])
        status = nas.parse_status(raw)
        self.assertEqual(status["ups"]["on_battery"], MISSING)
        self.assertEqual(status["system"]["cpu"], MISSING)

    def test_ups_on_battery_and_apcaccess(self):
        text = ("===UGN:ups===\nups ups.status: OB DISCHRG\nups battery.charge: 42\n"
                "STATUS : ONBATT\nBATTCHG : 41.0 Percent\n===UGN:end===")
        raw, reported = nas.parse_probe(text)
        status = nas.parse_status(raw)
        self.assertTrue(reported["ups"])
        self.assertIs(status["ups"]["on_battery"], True)

    def test_probe_script_is_one_readonly_command(self):
        script = nas.probe_script()
        for marker in ("===UGN:df===", "===UGN:cpu===", "===UGN:mem===",
                       "===UGN:docker===", "===UGN:temp===", "===UGN:health===",
                       "===UGN:ups===", "===UGN:end==="):
            self.assertIn(marker, script)
        for dangerous in ("rm -rf", "mkfs", "shutdown", "reboot", "dd if="):
            self.assertNotIn(dangerous, script)


class TestTransportAndDiagnostics(unittest.TestCase):
    def test_paramiko_missing_but_ssh_cli_plus_key_works(self):
        with patch.dict(sys.modules, {"paramiko": None}), \
             patch.object(nas, "ssh_cli_state", return_value={"available": True, "path": "/usr/bin/ssh", "reason": ""}):
            state = nas.transport_state({"ssh_key": "-----BEGIN KEY-----"})
        self.assertTrue(state["available"])
        self.assertIn("ssh 命令", state["transport"])
        self.assertFalse(state["paramiko"])

    def test_no_credentials_has_clear_reason(self):
        with patch.dict(sys.modules, {"paramiko": None}), \
             patch.object(nas, "ssh_cli_state", return_value={"available": True, "path": "/usr/bin/ssh", "reason": ""}):
            state = nas.transport_state({})
        self.assertFalse(state["available"])
        self.assertEqual(state["auth"], "未填")
        self.assertIn("没填", state["reason"])

    def test_paramiko_missing_but_password_uses_ssh_cli(self):
        with patch.dict(sys.modules, {"paramiko": None}), \
             patch.object(nas, "ssh_cli_state", return_value={
                 "available": True, "path": "/usr/bin/ssh", "sshpass": "", "reason": ""}):
            state = nas.transport_state({"ssh_password": "S3cr3tPW"})
        self.assertTrue(state["available"])
        self.assertEqual(state["auth"], "密码")
        self.assertEqual(state["preferred"], "ssh 命令")
        self.assertIn("askpass", state["transport"])

    def test_explicit_preference_is_honoured(self):
        with patch.object(nas, "paramiko_state", return_value={"available": True, "reason": ""}), \
             patch.object(nas, "ssh_cli_state", return_value={
                 "available": True, "path": "/usr/bin/ssh", "sshpass": "", "reason": ""}):
            forced = nas.transport_state({"ssh_password": "pw", "ssh_transport": "ssh 命令"})
        self.assertEqual(forced["preferred"], "ssh 命令")
        with patch.dict(sys.modules, {"paramiko": None}), \
             patch.object(nas, "ssh_cli_state", return_value={
                 "available": True, "path": "/usr/bin/ssh", "sshpass": "", "reason": ""}):
            blocked = nas.transport_state({"ssh_password": "pw", "ssh_transport": "paramiko"})
        self.assertFalse(blocked["available"])
        self.assertIn("paramiko", blocked["reason"])

    def test_private_key_wins_over_password(self):
        self.assertEqual(nas.auth_mode({"ssh_key": "KEY", "ssh_password": "pw"}), "私钥")
        self.assertEqual(nas.auth_mode({"ssh_password": "pw"}), "密码")
        self.assertEqual(nas.auth_mode({}), "未填")

    def test_ssh_info_never_leaks_credentials(self):
        cfg = {"ssh_host": "10.0.0.2", "ssh_password": "S3cr3tPW", "ssh_key": "KEYLINE-secrets"}
        info = nas.ssh_info(cfg)
        self.assertNotIn("S3cr3tPW", repr(info))
        self.assertNotIn("KEYLINE-secrets", repr(info))
        self.assertEqual(info["host"], "10.0.0.2")

    def test_diagnostics_without_host_does_not_touch_network(self):
        with patch.object(nas.socket, "create_connection", side_effect=AssertionError("不该联网")):
            report = nas.ssh_diagnostics({})
        self.assertFalse(report["ok"])
        self.assertIn("没填 SSH 地址", report["summary"])
        self.assertTrue(report["steps"])

    def test_diagnostics_reports_unreachable_port(self):
        with patch.object(nas.socket, "create_connection", side_effect=OSError("拒绝连接")):
            report = nas.ssh_diagnostics({"ssh_host": "10.0.0.2"})
        self.assertFalse(report["ok"])
        self.assertIn("端口连不上", report["summary"])
        self.assertFalse(report["steps"][1]["ok"])

    def test_diagnostics_reports_login_failure(self):
        with patch.object(nas.socket, "create_connection", return_value=None), \
             patch.object(nas, "transport_state", return_value={
                 "available": True, "transport": "paramiko", "note": "", "reason": "",
                 "paramiko": True, "paramiko_reason": "", "cli": False, "password": True}), \
             patch.object(nas, "_run_remote", return_value=(False, "", "认证失败 S3cr3tPW")):
            report = nas.ssh_diagnostics({"ssh_host": "10.0.0.2", "ssh_password": "S3cr3tPW"})
        self.assertFalse(report["ok"])
        self.assertIn("登录没通过", report["summary"])
        self.assertNotIn("S3cr3tPW", repr(report))
        self.assertIn("***", repr(report))

    def test_diagnostics_full_success_lists_sections(self):
        calls = []

        def fake_run(cfg, command, timeout=None):
            calls.append(command)
            if "echo UGN_OK" in command:
                return True, "UGN_OK\n", ""
            return True, PROBE_SAMPLE, ""

        with patch.object(nas.socket, "create_connection", return_value=None), \
             patch.object(nas, "transport_state", return_value={
                 "available": True, "transport": "paramiko", "note": "", "reason": "",
                 "paramiko": True, "paramiko_reason": "", "cli": False, "password": True}), \
             patch.object(nas, "_run_remote", side_effect=fake_run):
            report = nas.ssh_diagnostics({"ssh_host": "10.0.0.2"})
        self.assertTrue(report["ok"])
        self.assertEqual(len(calls), 2)
        self.assertIn("6/6", report["summary"])
        self.assertTrue(all(report["sections"][k] for k in
                            ("volumes", "cpu", "mem", "containers", "disks", "ups")))

    def test_read_ssh_sets_error_and_returns_none_when_transport_missing(self):
        with patch.object(nas, "transport_state", return_value={
                "available": False, "transport": "", "note": "", "reason": "没装 paramiko",
                "paramiko": False, "paramiko_reason": nas._PARAMIKO_REASON,
                "cli": False, "password": False}):
            self.assertIsNone(nas._read_ssh({"ssh_host": "10.0.0.2"}))
        self.assertIn("paramiko", nas._LAST_SSH_ERROR["text"])

    def test_read_ssh_returns_full_raw_from_probe(self):
        with patch.object(nas, "transport_state", return_value={
                "available": True, "transport": "paramiko", "note": "", "reason": "",
                "paramiko": True, "paramiko_reason": "", "cli": False, "password": True}), \
             patch.object(nas, "_run_remote", return_value=(True, PROBE_SAMPLE, "")):
            raw = nas._read_ssh({"ssh_host": "10.0.0.2"})
        self.assertIsInstance(raw, dict)
        self.assertTrue(raw["ugn_probe"]["volumes"])
        self.assertTrue(raw["disks"])

    def test_collect_exposes_probe_metadata(self):
        with patch.object(nas, "transport_state", return_value={
                "available": True, "transport": "paramiko", "note": "", "reason": "",
                "paramiko": True, "paramiko_reason": "", "cli": False, "password": True}), \
             patch.object(nas, "_run_remote", return_value=(True, PROBE_SAMPLE, "")):
            result = nas.collect({"source_mode": "SSH", "ssh_host": "10.0.0.2"})
        self.assertEqual(result["source"], "SSH")
        self.assertTrue(result["ssh"]["used"])
        self.assertTrue(result["probe"]["volumes"])
        self.assertEqual(result["status"]["ups"]["on_battery"], False)


class TestPasswordOverSshCli(unittest.TestCase):
    """没有 paramiko 时，系统 ssh 命令也必须能用密码，且密码不进 argv。"""

    def setUp(self):
        self.cfg = {"ssh_host": "10.0.0.2", "ssh_port": 22,
                    "ssh_user": "admin", "ssh_password": "S3cr3tPW"}

    @staticmethod
    def _cli(sshpass=""):
        return {"available": True, "path": "/usr/bin/ssh",
                "sshpass": sshpass, "reason": ""}

    @staticmethod
    def _clean(paths):
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_sshpass_password_only_in_env(self):
        with patch.object(nas, "ssh_cli_state", return_value=self._cli("/usr/bin/sshpass")):
            args, env, temp, error = nas._cli_plan(self.cfg, "df -kP", 8)
        self.assertEqual(error, "")
        self.assertEqual(args[:2], ["/usr/bin/sshpass", "-e"])
        self.assertEqual(env["SSHPASS"], "S3cr3tPW")
        self.assertNotIn("S3cr3tPW", repr(args))
        self.assertEqual(temp, [])

    def test_askpass_script_never_contains_password(self):
        with patch.object(nas, "ssh_cli_state", return_value=self._cli()):
            args, env, temp, error = nas._cli_plan(self.cfg, "df -kP", 8)
        try:
            self.assertEqual(error, "")
            self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
            self.assertNotIn("S3cr3tPW", repr(args))
            self.assertNotIn("S3cr3tPW", pathlib.Path(env["SSH_ASKPASS"]).read_text())
            self.assertEqual(len(temp), 2)
            pwfile = pathlib.Path(env["UGN_ASKPASS_FILE"])
            self.assertIn("S3cr3tPW", pwfile.read_text())
            self.assertEqual(pwfile.stat().st_mode & 0o777, 0o600)
            self.assertEqual(pathlib.Path(env["SSH_ASKPASS"]).stat().st_mode & 0o777, 0o700)
        finally:
            self._clean(temp)

    def test_private_key_plan_stays_batch(self):
        cfg = dict(self.cfg, ssh_key="-----BEGIN KEY-----\nABC\n-----END KEY-----")
        with patch.object(nas, "ssh_cli_state", return_value=self._cli()):
            args, env, temp, error = nas._cli_plan(cfg, "df -kP", 8)
        try:
            self.assertEqual(error, "")
            self.assertIn("-i", args)
            self.assertIn("BatchMode=yes", args)
            self.assertEqual(env, {})
            self.assertEqual(len(temp), 1)
        finally:
            self._clean(temp)

    def test_password_login_runs_and_cleans_temp_files(self):
        class Done:
            returncode = 0
            stdout = b"UGN_OK\n"
            stderr = b""

        with patch.dict(sys.modules, {"paramiko": None}), \
             patch.object(nas, "ssh_cli_state", return_value=self._cli()), \
             patch.object(nas.subprocess, "run", return_value=Done()) as run:
            ok, out, err = nas._run_remote(self.cfg, "echo UGN_OK", timeout=8)
        self.assertTrue(ok)
        self.assertIn("UGN_OK", out)
        kwargs = run.call_args[1]
        self.assertTrue(kwargs["start_new_session"])
        self.assertIn("SSH_ASKPASS", kwargs["env"])
        self.assertNotIn("S3cr3tPW", repr(run.call_args[0][0]))
        self.assertFalse(os.path.exists(kwargs["env"]["SSH_ASKPASS"]))
        self.assertFalse(os.path.exists(kwargs["env"]["UGN_ASKPASS_FILE"]))

    def test_password_is_scrubbed_from_cli_error(self):
        class Done:
            returncode = 255
            stdout = b""
            stderr = b"Permission denied (password S3cr3tPW)"

        with patch.dict(sys.modules, {"paramiko": None}), \
             patch.object(nas, "ssh_cli_state", return_value=self._cli()), \
             patch.object(nas.subprocess, "run", return_value=Done()):
            ok, _out, err = nas._run_remote(self.cfg, "echo UGN_OK", timeout=8)
        self.assertFalse(ok)
        self.assertNotIn("S3cr3tPW", err)
        self.assertIn("***", err)

    def test_paramiko_still_supports_password_directly(self):
        fake = types.SimpleNamespace()
        calls = {}

        def connect(**kwargs):
            calls.update(kwargs)

        class FakeClient:
            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, **kwargs):
                connect(**kwargs)

            def close(self):
                pass

        fake.SSHClient = FakeClient
        fake.RSAKey = fake.Ed25519Key = fake.ECDSAKey = object()
        fake.AutoAddPolicy = lambda: None
        with patch.dict(sys.modules, {"paramiko": fake}):
            client, error = nas._new_paramiko_client(self.cfg)
        self.assertEqual(error, "")
        self.assertIsNotNone(client)
        self.assertEqual(calls["password"], "S3cr3tPW")
        self.assertNotIn("pkey", calls)


class TestCommandGuard(unittest.TestCase):
    def test_safe_commands_allowed(self):
        for text in ("docker restart qbittorrent", "sudo -n docker restart qbittorrent",
                     "/volume1/scripts/notify.sh 磁盘满", "logger NAS告警"):
            ok, reason = nas.validate_command(text)
            self.assertTrue(ok, f"{text} -> {reason}")

    def test_dangerous_commands_rejected(self):
        for text in ("rm -rf /", "echo x; reboot", "echo x | sh", "echo x > /tmp/a",
                     "mkfs.ext4 /dev/sda", "echo $(whoami)", "echo `id`", "echo x && shutdown",
                     "dd if=/dev/zero of=/dev/sda", "sudo reboot", "echo a\necho b"):
            ok, _reason = nas.validate_command(text)
            self.assertFalse(ok, text)

    def test_long_command_rejected(self):
        self.assertFalse(nas.validate_command("a" * 400)[0])

    def test_container_name_is_whitelisted(self):
        self.assertEqual(nas.restart_command({}, "qbittorrent"), "docker restart qbittorrent")
        self.assertEqual(nas.restart_command({"ssh_sudo": True}, "emby"),
                         "sudo -n docker restart emby")
        for bad in ("a;rm -rf /", "", "foo bar", "../etc", "a" * 80, None):
            self.assertEqual(nas.restart_command({}, bad), "")

    def test_invalid_command_is_never_executed(self):
        with patch.object(nas, "_run_remote", side_effect=AssertionError("不该执行")) as run:
            result = nas.run_remote_action({}, "rm -rf /")
        self.assertFalse(result["ok"])
        self.assertFalse(run.called)

    def test_action_output_is_truncated_and_scrubbed(self):
        with patch.object(nas, "_run_remote", return_value=(True, "S3cr3tPW" + "x" * 900, "")):
            result = nas.run_remote_action({"ssh_password": "S3cr3tPW"}, "docker restart qbittorrent")
        self.assertTrue(result["ok"])
        self.assertNotIn("S3cr3tPW", repr(result))
        self.assertLessEqual(len(result["output"]), 300)


class TestNewRules(unittest.TestCase):
    def test_cpu_and_memory_hits(self):
        status = full(system={"cpu": 96.0, "mem": 97.0})
        hits = nas.evaluate(status, cfg())
        self.assertIn("cpu_high", rules(hits))
        self.assertIn("mem_high", rules(hits))
        self.assertTrue(all(h["action"] == "notify" for h in hits))

    def test_normal_cpu_and_memory_do_not_hit(self):
        self.assertNotIn("cpu_high", rules(nas.evaluate(full(), cfg())))
        self.assertNotIn("mem_high", rules(nas.evaluate(full(), cfg())))

    def test_container_down_has_restart_targets(self):
        status = full(containers=[{"name": "qbittorrent", "status": "exited"}])
        hit = next(h for h in nas.evaluate(status, cfg()) if h["rule"] == "container_down")
        self.assertEqual(hit["targets"], ["qbittorrent"])

    def test_resume_scope_partial_data(self):
        status = nas.parse_status({"volumes": [{"name": "v", "percent": 40}]})
        self.assertIn("resume_download", actions(nas.evaluate(status, cfg(resume_scope="已上报"))))
        self.assertIn("no_data", rules(nas.evaluate(status, cfg())))
        self.assertNotIn("resume_download", actions(nas.evaluate(status, cfg(resume_scope="严格"))))

    def test_resume_scope_never_resumes_with_no_data(self):
        self.assertNotIn("resume_download",
                         actions(nas.evaluate(nas.parse_status({}), cfg(resume_scope="已上报"))))

    def test_custom_cpu_threshold(self):
        status = full(system={"cpu": 70.0, "mem": 20.0})
        self.assertIn("cpu_high", rules(nas.evaluate(status, cfg(cpu_threshold=60))))
        self.assertNotIn("cpu_high", rules(nas.evaluate(status, cfg(cpu_threshold=90))))


class TestNewRenderBlocks(unittest.TestCase):
    def _view(self, **overrides):
        data = {
            "generated_at": "2026-11-12 10:00:00",
            "status": full(),
            "hits": [],
            "source": "SSH",
            "services": None,
            "ssh": {"available": True, "reason": "", "transport": "paramiko",
                    "transport_note": "", "paramiko": True, "host": "10.0.0.2",
                    "used": True, "note": ""},
            "probe": {"volumes": True, "cpu": True, "mem": True,
                      "containers": True, "disks": False, "ups": False},
            "source_mode": "SSH",
            "log": [],
            "series": [{"time": "10:00:0%d" % i, "volume": 40 + i, "cpu": 20 + i,
                        "mem": 45 + i, "temp": 40, "alerts": 0} for i in range(4)],
            "ssh_test": {"ok": True, "checked_at": "10:00:00", "summary": "连接正常",
                         "hint": "没读到的段落标未上报",
                         "steps": [{"name": "登录", "ok": True, "detail": "<img src=x onerror=alert(1)>"}]},
            "age_seconds": 4,
            "poll_seconds": 30,
            "paused": False,
            "mode": "试运行",
            "cfg": {"water_threshold": 88, "temp_threshold": 52, "watch_containers": []},
            "cooldown": 1800,
            "actions": {"mode": "试运行", "allow_remote_action": False,
                        "restart_containers": False, "alert_command": "", "resume_scope": "已上报"},
        }
        data.update(overrides)
        return data

    def test_live_block_has_sparkline_and_samples(self):
        html = renderer.render(self._view())
        self.assertIn("<svg", html)
        self.assertIn("实时巡检", html)
        self.assertIn("后台巡检正常", html)
        self.assertIn("4 帧", html)

    def test_stale_data_is_flagged(self):
        self.assertIn("采集可能停了", renderer.render(self._view(age_seconds=999)))

    def test_ssh_test_detail_is_escaped(self):
        html = renderer.render(self._view())
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;img", html)
        self.assertIn("分步诊断", html)

    def test_actions_block_escapes_command(self):
        dirty = "<img src=x onerror=alert(1)>"
        actions = self._view()["actions"]
        actions = dict(actions, alert_command=dirty, allow_remote_action=True)
        html = renderer.render(self._view(actions=actions))
        self.assertNotIn(dirty, html)
        self.assertIn("&lt;img", html)
        self.assertIn("已开启", html)

    def test_full_page_has_no_script_no_ai_words_no_water(self):
        html = renderer.render(self._view())
        for marker in ("<script", "onclick=", "onload=", "javascript:", "AI", "水位", "智能赋能"):
            self.assertNotIn(marker, html, marker)
        for section in ("实时巡检", "SSH 连接", "自动处置", "当前命中", "最近动作"):
            self.assertIn(section, html)


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
