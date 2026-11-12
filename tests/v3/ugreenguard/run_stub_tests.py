#!/usr/bin/env python3
"""绿联 NAS 守护的离线桩测试，不连 MoviePilot 宿主。"""

from __future__ import annotations

import os
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_DIR = ROOT / "plugins.v3" / "ugreenguard"
sys.path.insert(0, str(ROOT / "tests" / "v3"))

import check_api_contract as cc  # noqa: E402

cc.install_stubs()
MODULE, PLUGIN_CLS, PLUGIN_ID = cc.load_plugin(PLUGIN_DIR)
NAS = MODULE.nas
RENDERER = MODULE.renderer


def full_raw(**overrides):
    raw = {
        "ts": "2026-11-12 10:00:00",
        "volumes": [
            {"name": "volume1", "total": 2000, "used": 1000, "percent": 50.0},
            {"name": "volume2", "total": 1000, "used": 300, "percent": 30.0},
        ],
        "disks": [{"name": "disk1", "temp": 40, "health": "正常", "smart_alert": False}],
        "containers": [
            {"name": "moviepilot", "status": "running", "restarts": 0},
            {"name": "qbittorrent", "status": "running", "restarts": 2},
        ],
        "ups": {"on_battery": False, "battery": 100},
        "system": {"cpu": 20.0, "mem": 45.0},
    }
    raw.update(overrides)
    return raw


def bridge_collected(raw):
    return {"status": NAS.parse_status(raw), "source": "状态桥", "services": None}


class TestContract(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()

    def test_plugin_id_and_metadata(self):
        self.assertEqual(PLUGIN_ID, "UGreenGuard")
        self.assertEqual(PLUGIN_CLS.plugin_name, "绿联 NAS 守护")
        self.assertEqual(PLUGIN_CLS.plugin_version, "2.1.0")
        self.assertEqual(PLUGIN_CLS.plugin_author, "BackspaceM")
        self.assertEqual(PLUGIN_CLS.plugin_config_prefix, "ugreenguard_")
        self.assertEqual(PLUGIN_CLS.plugin_color, "#6366F1")
        self.assertEqual(PLUGIN_CLS.system_version, ">=3.0.0")
        self.assertEqual(PLUGIN_CLS.plugin_order, 96)
        self.assertTrue(PLUGIN_CLS.plugin_icon.endswith("icons/ugreenguard.png"))

    def test_six_abstract_methods_kept(self):
        for name in ("init_plugin", "get_state", "get_api", "get_form", "get_page", "stop_service"):
            self.assertTrue(callable(getattr(PLUGIN_CLS, name, None)), name)

    def test_response_is_pydantic_model_with_strict_three_keys(self):
        from pydantic import BaseModel

        self.assertTrue(issubclass(MODULE.Response, BaseModel))
        self.assertEqual(set(MODULE.Response.model_fields), {"success", "message", "data"})
        result = MODULE.Response(success=False, message="具体原因：xxx", data=None)
        self.assertEqual(set(result.model_dump()), {"success", "message", "data"})
        self.assertIsInstance(result.message, str)

    def test_all_api_routes_can_register_in_fastapi(self):
        from fastapi import FastAPI
        from fastapi.routing import APIRoute

        app = FastAPI()
        for api in self.plugin.get_api():
            self.assertEqual(api.get("auth"), "bear")
            self.assertTrue(str(api["path"]).startswith("/"))
            self.assertTrue(callable(api["endpoint"]))
            route = dict(api)
            route.pop("auth", None)
            route.pop("allow_anonymous", None)
            route["path"] = f"/api/v1/plugin/{PLUGIN_ID}{route['path']}"
            route["dependencies"] = list(route.get("dependencies") or ())
            route.setdefault("route_class_override", APIRoute)
            app.router.add_api_route(**route, tags=["plugin"])
        self.assertEqual(len(app.routes), len(self.plugin.get_api()) + 4)

    def test_every_page_button_matches_declared_api(self):
        declared = {
            str(a["path"]).lstrip("/"): {str(m).upper() for m in (a.get("methods") or ["GET"])}
            for a in self.plugin.get_api()
        }
        prefix = f"plugin/{PLUGIN_ID}/"
        clicks = cc.collect_click_events(self.plugin.get_page())
        self.assertEqual(len(clicks), 7)
        for click in list(clicks) + cc.collect_page_clicks(PLUGIN_CLS()):
            self.assertTrue(click["api"].startswith(prefix), click["api"])
            action = click["api"][len(prefix):].split("?", 1)[0].split("/", 1)[0]
            self.assertIn(action, declared, click["api"])
            self.assertIn(click["method"].upper(), declared[action], click["api"])

    def test_button_text_is_toplevel_and_no_click_confirm(self):
        for item in self.plugin.get_page():
            if item.get("component") != "VBtn":
                continue
            self.assertIsInstance(item.get("text"), str)
            self.assertNotIn("text", item.get("props", {}))
            click = (item.get("events") or {}).get("click") or {}
            # 详情页忽略 events.click.confirm，二次确认不能靠它
            self.assertNotIn("confirm", click)
        # 口令走 params，后端还会校验
        clear_btn = next(i for i in self.plugin.get_page()
                         if i.get("component") == "VBtn" and i.get("text") == "清空日志")
        self.assertEqual(clear_btn["events"]["click"]["params"], {"confirm": "yes"})

    def test_api_returns_three_part_envelope(self):
        for name in ("api_snapshot", "api_check", "api_refresh", "api_resume",
                     "api_clear", "api_test_ssh", "api_test_notify"):
            result = getattr(self.plugin, name)()
            self.assertIsInstance(result, MODULE.Response)
            self.assertIsInstance(result.success, bool)
            self.assertIsInstance(result.message, str)
            self.assertEqual(set(result.model_dump()), {"success", "message", "data"})
        snap = self.plugin.api_snapshot()
        for key in ("status", "hits", "log", "source", "mode", "ssh", "source_mode",
                    "series", "age_seconds", "ssh_test", "probe", "actions", "poll_seconds",
                    "notify", "notify_enabled", "notify_services"):
            self.assertIn(key, snap.data)


class TestEndpointErrors(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()

    def test_every_endpoint_catches_exception_and_never_raises(self):
        cases = [
            ("api_snapshot", "_view", {}),
            ("api_check", "run_check", {}),
            ("api_resume", "_call_downloaders", {}),
            ("api_refresh", "_refresh_view", {}),
            ("api_clear", "save_data", {"confirm": "yes"}),
        ]
        # ssh_diagnostics / notify.send 在子模块上，单独注入异常
        for endpoint, target, exc in (
            ("api_test_ssh", NAS, "ssh_diagnostics"),
            ("api_test_notify", MODULE.notify, "send"),
        ):
            with patch.object(target, exc, side_effect=RuntimeError("炸了")):
                result = getattr(self.plugin, endpoint)()
            self.assertIs(result.success, False)
            self.assertIn("具体原因", result.message)
        for name, broken, kwargs in cases:
            with self.subTest(endpoint=name):
                with patch.object(self.plugin, broken, side_effect=RuntimeError("炸了")):
                    result = getattr(self.plugin, name)(**kwargs)
                self.assertIsInstance(result, MODULE.Response)
                self.assertIs(result.success, False)
                self.assertIsInstance(result.message, str)
                self.assertIn("具体原因", result.message)
                self.assertEqual(set(result.model_dump()), {"success", "message", "data"})

    def test_renderer_failure_is_also_caught(self):
        with patch.object(RENDERER, "render", side_effect=ValueError("渲染挂了")):
            result = self.plugin.api_check()
        self.assertIs(result.success, False)
        self.assertIn("具体原因", result.message)

    def test_wrong_confirm_token_stays_false(self):
        for token in ("", "no", "随便", None, 0):
            self.assertIs(self.plugin.api_clear(confirm=token).success, False)

    def test_corrupted_stored_data_never_raises(self):
        for value in ("text", 1, True, [{"x": 1}], {"status": 3, "hits": "bad"}):
            with self.subTest(value=value):
                for key in ("ugn_snapshot", "ugn_series", "ugn_ssh_test",
                            "ugn_notify", "ugn_log", "ugn_state"):
                    self.plugin.save_data(key, value)
                for name in ("api_snapshot", "api_refresh", "api_check",
                             "api_test_ssh", "api_test_notify"):
                    result = getattr(self.plugin, name)()
                    self.assertIsInstance(result, MODULE.Response)
                    self.assertIsInstance(result.message, str)
                    self.assertEqual(set(result.model_dump()), {"success", "message", "data"})

    def test_password_scrubbed_from_error_message(self):
        self.plugin.init_plugin({"ssh_password": "S3cr3tPW"})
        with patch.object(self.plugin, "_view", side_effect=RuntimeError("连不上 S3cr3tPW")):
            result = self.plugin.api_snapshot()
        self.assertIs(result.success, False)
        self.assertNotIn("S3cr3tPW", result.message)
        self.assertIn("***", result.message)


class TestClearTwoStage(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()

    def test_first_click_arms_second_click_clears(self):
        self.assertFalse(self.plugin.api_clear().success, "没口令不该清")
        self.assertFalse(self.plugin.api_clear(confirm="yes").success, "第一次点只是确认，不清")
        self.assertTrue(self.plugin.api_clear(confirm="yes").success, "再点一次才真清")

    def test_single_click_never_clears_existing_log(self):
        self.plugin.save_data("ugn_log", [{"time": "x", "level": "INFO", "message": "留着"}])
        self.plugin.api_clear(confirm="yes")
        self.assertEqual(len(self.plugin.get_data("ugn_log")), 1)

    def test_arm_expires(self):
        self.plugin.api_clear(confirm="yes")
        self.plugin.save_data("ugn_clear_arm", {"ts": 0})
        self.assertFalse(self.plugin.api_clear(confirm="yes").success)


class TestPageAndForm(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.page = self.plugin.get_page()

    def test_page_has_buttons_and_one_html_node(self):
        self.assertIsInstance(self.page, list)
        components = [item.get("component") for item in self.page]
        self.assertEqual(components.count("VBtn"), 7)
        html_nodes = [item for item in self.page if item.get("html")]
        self.assertEqual(len(html_nodes), 1)
        self.assertIn("绿联 NAS 守护", html_nodes[0]["html"])
        self.assertIn("ugn-grid", html_nodes[0]["html"])

    def test_html_has_no_inline_script(self):
        html = RENDERER.render(self.plugin.api_snapshot().data)
        for marker in ("<script", "onclick=", "onchange=", "onload=", "javascript:"):
            self.assertNotIn(marker, html, marker)

    def test_form_stays_compact_and_keeps_compat_defaults(self):
        form, defaults = PLUGIN_CLS().get_form()
        controls = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("component") in ("VSelect", "VSwitch", "VTextField", "VTextarea", "VFileInput"):
                    controls.append(node["component"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(form)
        self.assertEqual(len(controls), 18, f"设置项要精简，实际 {controls}")
        self.assertEqual(controls.count("VSwitch"), 4)
        self.assertEqual(controls.count("VTextField"), 6)
        self.assertEqual(controls.count("VTextarea"), 1)
        self.assertEqual(controls.count("VSelect"), 7)
        # 老配置键仍然留在 defaults 里，升级不会丢设置
        self.assertTrue({
            "enabled", "source_mode", "ssh_host", "ssh_port", "ssh_user", "ssh_password",
            "ssh_key", "ssh_sudo", "ssh_transport", "bridge_url", "status_file",
            "water_threshold", "temp_threshold", "cpu_threshold", "mem_threshold",
            "mode", "poll_seconds", "resume_scope", "interval_minutes",
            "watch_containers", "cooldown", "allow_remote_action",
            "restart_containers", "alert_command", "notify_enabled", "notify_channel",
        }.issubset(defaults))
        self.assertEqual(defaults["water_threshold"], 88)
        self.assertFalse(defaults["allow_remote_action"])
        self.assertEqual(defaults["mode"], "试运行")
        self.assertTrue(defaults["notify_enabled"])
        self.assertEqual(defaults["notify_channel"], "")
        self.assertEqual(defaults["ssh_transport"], "自动")

    def test_form_source_mode_options_and_password_field(self):
        form, _ = PLUGIN_CLS().get_form()
        models = {}

        def walk(node):
            if isinstance(node, dict):
                props = node.get("props") or {}
                if isinstance(props, dict) and props.get("model"):
                    models[props["model"]] = props
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(form)
        items = {item["value"] for item in models["source_mode"]["items"]}
        self.assertEqual(items, {"自动", "状态桥", "本地文件", "SSH", "容器自检"})
        self.assertEqual(models["ssh_password"].get("type"), "password")
        self.assertIn("不会写进日志或快照", models["ssh_password"]["hint"])
        self.assertIn("不需要私钥", models["ssh_password"]["hint"])
        self.assertEqual(models["water_threshold"]["label"], "磁盘占用到多少就暂停")
        self.assertEqual({item["value"] for item in models["ssh_transport"]["items"]},
                         {"自动", "paramiko", "ssh 命令"})
        self.assertIn("WebPush", {item["value"] for item in models["notify_channel"]["items"]})
        self.assertIn("Telegram", {item["value"] for item in models["notify_channel"]["items"]})
        self.assertIn("", {item["value"] for item in models["notify_channel"]["items"]})

    def test_get_service_registers_cron(self):
        services = PLUGIN_CLS().get_service()
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["id"], "UGreenGuard")
        self.assertIsNotNone(services[0]["trigger"])
        self.assertTrue(callable(services[0]["func"]))
        disabled = PLUGIN_CLS()
        disabled.init_plugin({"enabled": False})
        self.assertFalse(disabled.get_state())
        self.assertEqual(disabled.get_service(), [])


class TestWordingAndEscape(unittest.TestCase):
    def test_no_water_word_on_page(self):
        html = RENDERER.render(PLUGIN_CLS().api_snapshot().data)
        self.assertNotIn("水位", html)
        self.assertIn("磁盘占用空间", html)

    def test_form_has_no_water_word(self):
        form, _ = PLUGIN_CLS().get_form()
        self.assertNotIn("水位", repr(form))

    def test_dynamic_volume_name_is_escaped(self):
        dirty = "<img src=x onerror=alert(1)>"
        raw = full_raw()
        raw["volumes"][0]["name"] = dirty
        raw["volumes"][0]["percent"] = 95.0
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"mode": "试运行", "cooldown": 0})
        with patch.object(NAS, "collect", return_value=bridge_collected(raw)):
            view = plugin.run_check("转义")
        html = RENDERER.render(view)
        self.assertNotIn(dirty, html)
        self.assertIn("&lt;img", html)

    def test_ssh_host_is_escaped_and_password_never_in_page(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"source_mode": "SSH", "ssh_host": "<svg onload=alert(1)>",
                            "ssh_password": "S3cr3tPW"})
        html = RENDERER.render(plugin.api_snapshot().data)
        self.assertNotIn("<svg onload=alert(1)>", html)
        self.assertIn("&lt;svg", html)
        self.assertNotIn("S3cr3tPW", html)
        self.assertIn("paramiko", html)

    def test_no_ai_words_or_emoji_in_page(self):
        html = RENDERER.render(PLUGIN_CLS().api_snapshot().data)
        for word in ("AI", "智能赋能", "一键搞定", "全方位", "本插件旨在"):
            self.assertNotIn(word, html, word)


class TestSSH(unittest.TestCase):
    def test_paramiko_missing_is_reported_and_downgrades(self):
        raw = full_raw()
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"source_mode": "自动", "ssh_host": "10.0.0.2",
                            "ssh_password": "S3cr3tPW", "bridge_url": "http://x/status"})
        # 两条 SSH 实现都没有时，才该降级到状态桥
        with patch.dict(sys.modules, {"paramiko": None}), \
             patch.object(NAS, "ssh_cli_state", return_value={
                 "available": False, "path": "", "sshpass": "", "reason": "没有 ssh 命令"}), \
             patch.object(NAS, "_read_bridge", return_value=raw):
            result = NAS.collect(plugin._cfg())
        self.assertEqual(result["source"], "状态桥")
        self.assertIs(result["ssh"]["available"], False)
        self.assertEqual(result["ssh"]["reason"],
                         "容器里没有 paramiko，在 MoviePilot 容器执行 pip install paramiko 后重启插件")
        self.assertEqual(result["ssh"]["host"], "10.0.0.2")
        self.assertNotIn("S3cr3tPW", repr(result["ssh"]))

    def test_password_keeps_ssh_alive_without_paramiko(self):
        raw = full_raw()
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"source_mode": "自动", "ssh_host": "10.0.0.2",
                            "ssh_password": "S3cr3tPW"})
        with patch.dict(sys.modules, {"paramiko": None}), \
             patch.object(NAS, "ssh_cli_state", return_value={
                 "available": True, "path": "/usr/bin/ssh",
                 "sshpass": "/usr/bin/sshpass", "reason": ""}), \
             patch.object(NAS, "_read_ssh", return_value=raw):
            result = NAS.collect(plugin._cfg())
        self.assertEqual(result["source"], "SSH")
        self.assertIs(result["ssh"]["available"], True)
        self.assertEqual(result["ssh"]["auth"], "密码")
        self.assertIn("ssh 命令", result["ssh"]["transport"])
        self.assertNotIn("S3cr3tPW", repr(result["ssh"]))

    def test_paramiko_state_shape_when_missing(self):
        with patch.dict(sys.modules, {"paramiko": None}):
            state = NAS.paramiko_state()
        self.assertEqual(state, {
            "available": False,
            "reason": "容器里没有 paramiko，在 MoviePilot 容器执行 pip install paramiko 后重启插件",
        })

    def test_ssh_mode_marks_other_sections_missing(self):
        raw = {"ts": "x", "volumes": [{"name": "/volume1", "total": 1024, "used": 900, "percent": 90}]}
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"source_mode": "SSH", "ssh_host": "10.0.0.2",
                            "ssh_password": "S3cr3tPW", "mode": "试运行", "cooldown": 0})
        with patch.object(NAS, "paramiko_state", return_value={"available": True, "reason": ""}), \
             patch.object(NAS, "_read_ssh", return_value=raw):
            view = plugin.run_check("SSH 巡检")
        self.assertEqual(view["source"], "SSH")
        self.assertEqual(view["status"]["disks"], [])
        self.assertEqual(view["status"]["containers"], [])
        self.assertEqual(view["status"]["ups"]["on_battery"], NAS.MISSING)
        self.assertIn("volume_high", {h["rule"] for h in view["hits"]})
        self.assertNotIn("resume_download", {h["action"] for h in view["hits"]})

    def test_password_never_leaks_to_page_snapshot_or_log(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"source_mode": "SSH", "ssh_host": "10.0.0.2", "ssh_user": "admin",
                            "ssh_password": "S3cr3tPW", "mode": "试运行", "cooldown": 0})
        raw = {"ts": "x", "volumes": [{"name": "/volume1", "total": 1000, "used": 900, "percent": 90}]}
        with patch.object(NAS, "paramiko_state", return_value={"available": True, "reason": ""}), \
             patch.object(NAS, "_read_ssh", return_value=raw):
            view = plugin.run_check("SSH")
        html = RENDERER.render(view)
        blob = (html + repr(plugin.get_data("ugn_snapshot")) + repr(plugin.get_data("ugn_log"))
                + repr(plugin.get_data("ugn_state")) + repr(view))
        self.assertNotIn("S3cr3tPW", blob)
        self.assertEqual(view["source"], "SSH")

    def test_page_shows_source_paramiko_and_host(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"source_mode": "SSH", "ssh_host": "10.0.0.2"})
        ssh = {"available": False,
               "reason": "容器里没有 paramiko，在 MoviePilot 容器执行 pip install paramiko 后重启插件",
               "host": "10.0.0.2", "used": False, "note": ""}
        view = plugin._view(collected={"source": "SSH", "services": None, "ssh": ssh,
                                       "source_mode": "SSH"})
        html = RENDERER.render(view)
        self.assertIn("当前生效来源：SSH", html)
        self.assertIn("paramiko 不可用", html)
        self.assertIn("10.0.0.2", html)
        self.assertNotIn("S3cr3tPW", html)

    def test_parse_df_filters_proc_and_duplicates(self):
        text = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "proc 0 0 0 - /proc\n"
            "tmpfs 100 1 99 1% /run\n"
            "/dev/sda1 1000 400 600 40% /volume1\n"
            "/dev/sda1 1000 400 600 40% /volume1\n"
        )
        volumes = NAS.parse_df(text)
        self.assertEqual([v["name"] for v in volumes], ["/volume1"])
        self.assertEqual(volumes[0]["total"], 1000 * 1024)


class TestCollectAndInspect(unittest.TestCase):
    def test_local_fallback_is_used_without_bridge(self):
        plugin = PLUGIN_CLS()
        result = NAS.collect(plugin._cfg())
        self.assertEqual(result["source"], "容器自检")
        self.assertTrue(result["status"]["volumes"])
        self.assertIsInstance(result["services"], dict)
        volume = result["status"]["volumes"][0]
        self.assertIsInstance(volume["percent"], (int, float))
        self.assertEqual(result["status"]["disks"], [])
        self.assertEqual(result["status"]["containers"], [])
        self.assertEqual(result["status"]["ups"]["on_battery"], NAS.MISSING)
        self.assertIsInstance(result["ssh"], dict)

    def test_bridge_takes_priority_over_local(self):
        raw = full_raw()
        with patch.object(NAS, "_read_bridge", return_value=raw):
            result = NAS.collect({"bridge_url": "http://127.0.0.1:9999/status", "status_file": ""})
        self.assertEqual(result["source"], "状态桥")
        self.assertEqual(len(result["status"]["volumes"]), 2)

    def test_status_file_is_second_choice(self):
        raw = full_raw()
        with patch.object(NAS, "_read_bridge", return_value=None), \
             patch.object(NAS, "_read_status_file", return_value=raw):
            result = NAS.collect({"bridge_url": "http://bad", "status_file": "/tmp/ugn.json"})
        self.assertEqual(result["source"], "本地文件")

    def test_full_check_records_action_and_state(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"mode": "试运行", "cooldown": 0})
        raw = full_raw()
        raw["volumes"][0]["percent"] = 95.0
        with patch.object(NAS, "collect", return_value=bridge_collected(raw)):
            result = plugin.api_check()
        self.assertTrue(result.success)
        self.assertEqual(result.data["source"], "状态桥")
        self.assertIn("html", result.data)
        self.assertIn("volume_high", {h["rule"] for h in result.data["hits"]})
        self.assertTrue(result.data["log"])
        self.assertTrue(any("暂停" in entry["message"] for entry in result.data["log"]))
        self.assertTrue(any("磁盘占用空间" in entry["message"] for entry in result.data["log"]))
        self.assertTrue(plugin.get_data("ugn_state")["paused"])

    def test_debounce_and_cooldown(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"mode": "试运行", "cooldown": 99999})
        high = full_raw()
        high["volumes"][0]["percent"] = 95.0
        normal = full_raw()

        with patch.object(NAS, "collect", return_value=bridge_collected(high)):
            plugin.run_check("第一次告警")
        self.assertEqual(len(plugin.get_data("ugn_log")), 1)
        self.assertIn("volume_high", plugin.get_data("ugn_state")["last_alert"])

        # 状态没变，不该再记一笔
        with patch.object(NAS, "collect", return_value=bridge_collected(high)):
            plugin.run_check("状态没变")
        self.assertEqual(len(plugin.get_data("ugn_log")), 1)

        # 恢复正常记一笔
        with patch.object(NAS, "collect", return_value=bridge_collected(normal)):
            plugin.run_check("恢复正常")
        self.assertEqual(len(plugin.get_data("ugn_log")), 2)
        self.assertFalse(plugin.get_data("ugn_state")["paused"])

        # 冷却期内又告警，不该再执行
        with patch.object(NAS, "collect", return_value=bridge_collected(high)):
            plugin.run_check("冷却期内复发")
        self.assertEqual(len(plugin.get_data("ugn_log")), 2)

    def test_no_data_never_resumes(self):
        plugin = PLUGIN_CLS()
        plugin.init_plugin({"mode": "自动处置", "cooldown": 0})
        with patch.object(NAS, "collect", return_value=bridge_collected({})):
            view = plugin.run_check("桥返回空")
        self.assertEqual(view["hits"][0]["action"], "none")
        self.assertFalse(plugin.get_data("ugn_state")["paused"])

    def test_schema_shape(self):
        status = NAS.parse_status({})
        self.assertEqual(set(status), {"ts", "volumes", "disks", "containers", "ups", "system"})
        self.assertEqual(status["ts"], NAS.MISSING)
        self.assertEqual(status["ups"], {"on_battery": NAS.MISSING, "battery": NAS.MISSING})
        self.assertEqual(status["system"], {"cpu": NAS.MISSING, "mem": NAS.MISSING})


def down_raw(name="qbittorrent"):
    raw = full_raw()
    raw["containers"] = [{"name": name, "status": "exited"}]
    return raw


class TestLiveMonitoring(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()

    def test_service_uses_second_level_interval(self):
        captured = {}

        class FakeInterval:
            def __init__(self, seconds):
                captured["seconds"] = seconds

        fake = types.ModuleType("apscheduler.triggers.interval")
        fake.IntervalTrigger = FakeInterval
        self.plugin.init_plugin({"poll_seconds": 15})
        with patch.dict(sys.modules, {"apscheduler.triggers.interval": fake}):
            services = self.plugin.get_service()
        self.assertEqual(captured["seconds"], 15)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["id"], "UGreenGuard")

    def test_poll_seconds_are_clamped(self):
        for raw, expected in ((1, 5), (0, 5), (999, 300), ("abc", 30), (60, 60)):
            self.plugin.init_plugin({"poll_seconds": raw})
            self.assertEqual(self.plugin._cfg()["poll_seconds"], expected)

    def test_host_port_shorthand(self):
        self.plugin.init_plugin({"ssh_host": "10.0.0.2:2222"})
        cfg = self.plugin._cfg()
        self.assertEqual(cfg["ssh_host"], "10.0.0.2")
        self.assertEqual(cfg["ssh_port"], 2222)
        self.plugin.init_plugin({"ssh_host": "[::1]:2200"})
        self.assertEqual(self.plugin._cfg()["ssh_host"], "::1")
        self.assertEqual(self.plugin._cfg()["ssh_port"], 2200)
        self.plugin.init_plugin({"ssh_host": "10.0.0.2", "ssh_port": 2222})
        self.assertEqual(self.plugin._cfg()["ssh_port"], 2222)

    def test_scheduled_failure_is_logged_not_raised(self):
        with patch.object(self.plugin, "run_check", side_effect=RuntimeError("探针炸了")):
            self.plugin.run_scheduled()
        logs = self.plugin.get_data("ugn_log")
        self.assertTrue(logs)
        self.assertIn("实时巡检异常", logs[0]["message"])

    def test_series_and_freshness_are_saved(self):
        self.plugin.init_plugin({"mode": "试运行", "cooldown": 0})
        with patch.object(NAS, "collect", return_value=bridge_collected(full_raw())):
            self.plugin.run_check("第一次")
            view = self.plugin.run_check("第二次")
        series = self.plugin.get_data("ugn_series")
        self.assertEqual(len(series), 2)
        self.assertEqual(series[0]["source"], "状态桥")
        self.assertIsInstance(series[0]["volume"], float)
        self.assertIsInstance(view["age_seconds"], int)
        self.assertGreaterEqual(view["age_seconds"], 0)
        self.assertEqual(view["poll_seconds"], 30)

    def test_series_ring_is_bounded(self):
        self.plugin.init_plugin({"mode": "试运行", "cooldown": 0})
        with patch.object(NAS, "collect", return_value=bridge_collected(full_raw())):
            for _ in range(45):
                self.plugin.run_check("巡检")
        self.assertLessEqual(len(self.plugin.get_data("ugn_series")), 40)

    def test_stop_service_closes_ssh_session(self):
        with patch.object(NAS, "close_session") as close:
            self.plugin.stop_service()
        close.assert_called_once()


class TestAutoActions(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.plugin.init_plugin({"mode": "自动处置", "cooldown": 0})

    def test_remote_actions_off_by_default(self):
        with patch.object(NAS, "collect", return_value=bridge_collected(down_raw())), \
             patch.object(NAS, "run_remote_action", side_effect=AssertionError("不该远程执行")):
            self.plugin.run_check("容器挂了")
        logs = self.plugin.get_data("ugn_log")
        self.assertTrue(logs)
        self.assertIn("qbittorrent", logs[0]["message"])

    def test_restart_container_when_explicitly_enabled(self):
        self.plugin.init_plugin({"mode": "自动处置", "cooldown": 0,
                                 "allow_remote_action": True, "restart_containers": True})
        with patch.object(NAS, "collect", return_value=bridge_collected(down_raw())), \
             patch.object(NAS, "run_remote_action", return_value={
                 "ok": True, "output": "qbittorrent", "error": "",
                 "command": "docker restart qbittorrent"}) as run:
            self.plugin.run_check("容器挂了")
        self.assertEqual(run.call_args[0][1], "docker restart qbittorrent")
        self.assertIn("重启 qbittorrent", self.plugin.get_data("ugn_log")[0]["message"])

    def test_trial_mode_only_plans(self):
        self.plugin.init_plugin({"mode": "试运行", "cooldown": 0,
                                 "allow_remote_action": True, "restart_containers": True})
        with patch.object(NAS, "collect", return_value=bridge_collected(down_raw())), \
             patch.object(NAS, "run_remote_action", side_effect=AssertionError("不该远程执行")):
            self.plugin.run_check("容器挂了")
        self.assertIn("计划", self.plugin.get_data("ugn_log")[0]["message"])

    def test_pause_mode_only_reminds(self):
        self.plugin.init_plugin({"mode": "仅提醒", "cooldown": 0})
        high = full_raw()
        high["volumes"][0]["percent"] = 95.0
        with patch.object(NAS, "collect", return_value=bridge_collected(high)), \
             patch.object(NAS, "run_remote_action", side_effect=AssertionError("不该远程执行")), \
             patch.object(self.plugin, "_call_downloaders", side_effect=AssertionError("不该动下载器")):
            self.plugin.run_check("磁盘满")
        self.assertIn("仅提醒", self.plugin.get_data("ugn_log")[0]["message"])

    def test_dangerous_custom_command_is_never_sent(self):
        self.plugin.init_plugin({"mode": "自动处置", "cooldown": 0,
                                 "allow_remote_action": True, "alert_command": "rm -rf /"})
        high = full_raw()
        high["volumes"][0]["percent"] = 95.0
        with patch.object(NAS, "collect", return_value=bridge_collected(high)), \
             patch.object(NAS, "_run_remote", side_effect=AssertionError("不该下发危险命令")):
            self.plugin.run_check("磁盘满")
        logs = self.plugin.get_data("ugn_log")
        self.assertIn("含受限片段", logs[0]["message"])

    def test_sudo_fallback_after_permission_denied(self):
        self.plugin.init_plugin({"mode": "自动处置", "cooldown": 0,
                                 "allow_remote_action": True, "restart_containers": True})
        results = [
            {"ok": False, "output": "", "error": "permission denied", "command": "docker restart qbittorrent"},
            {"ok": True, "output": "ok", "error": "", "command": "sudo -n docker restart qbittorrent"},
        ]
        with patch.object(NAS, "collect", return_value=bridge_collected(down_raw())), \
             patch.object(NAS, "run_remote_action", side_effect=results) as run:
            self.plugin.run_check("容器挂了")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1][0][1], "sudo -n docker restart qbittorrent")
        self.assertIn("成功", self.plugin.get_data("ugn_log")[0]["message"])

    def test_password_and_key_never_reach_logs_or_snapshot(self):
        self.plugin.init_plugin({"mode": "自动处置", "cooldown": 0,
                                 "ssh_password": "S3cr3tPW", "ssh_key": "PRIVATE-KEY-LINE",
                                 "allow_remote_action": True})
        with patch.object(NAS, "run_remote_action", return_value={
                "ok": False, "output": "", "error": "S3cr3tPW PRIVATE-KEY-LINE", "command": "x"}):
            self.plugin._append_log("WARN", "连不上 S3cr3tPW PRIVATE-KEY-LINE")
        blob = repr(self.plugin.get_data("ugn_log"))
        self.assertNotIn("S3cr3tPW", blob)
        self.assertNotIn("PRIVATE-KEY-LINE", blob)


class TestSSHTestEndpoint(unittest.TestCase):
    def setUp(self):
        self.plugin = PLUGIN_CLS()

    def test_report_is_saved_and_returned(self):
        report = {"ok": True, "checked_at": "10:00:00", "host": "10.0.0.2", "port": 22,
                  "user": "admin", "transport": "paramiko", "summary": "连接正常",
                  "hint": "一切就绪", "steps": [{"name": "登录", "ok": True, "detail": "通过"}],
                  "sections": {"volumes": True}}
        with patch.object(NAS, "ssh_diagnostics", return_value=report):
            result = self.plugin.api_test_ssh()
        self.assertTrue(result.success)
        self.assertEqual(result.data["ssh_test"]["summary"], "连接正常")
        self.assertEqual(self.plugin.get_data("ugn_ssh_test"), report)
        self.assertIn("连接正常", result.data["html"])
        self.assertIn("连接测试", self.plugin.get_data("ugn_log")[0]["message"])

    def test_failed_report_is_logged_as_warning(self):
        with patch.object(NAS, "ssh_diagnostics", return_value={
                "ok": False, "summary": "SSH 端口连不上", "hint": "检查 SSH 服务",
                "steps": [], "sections": {}}):
            self.plugin.api_test_ssh()
        self.assertEqual(self.plugin.get_data("ugn_log")[0]["level"], "WARN")


class TestNotificationBridge(unittest.TestCase):
    """通知走 V3 宿主 post_message(channel, mtype, title, text)，试运行也要发。"""

    class Recorder:
        def __init__(self, fail_first=False, fail_all=False):
            self.calls = []
            self.fail_first = fail_first
            self.fail_all = fail_all

        def post_message(self, **kwargs):
            self.calls.append(kwargs)
            if self.fail_all or (self.fail_first and len(self.calls) == 1):
                raise TypeError("unexpected keyword argument")

    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.notify = MODULE.notify

    def test_message_kwargs_use_real_v3_enums(self):
        class FakeEnum:
            Plugin = "插件枚举"
            WebPush = "WebPush 枚举"

        with patch.object(self.notify, "enums", return_value=(FakeEnum, FakeEnum)):
            kwargs = self.notify.message_kwargs("WebPush")
        self.assertEqual(kwargs, {"mtype": "插件枚举", "channel": "WebPush 枚举"})

    def test_default_channel_only_sends_mtype(self):
        class FakeEnum:
            Plugin = "插件枚举"

        with patch.object(self.notify, "enums", return_value=(FakeEnum, FakeEnum)):
            kwargs = self.notify.message_kwargs("")
        self.assertEqual(kwargs, {"mtype": "插件枚举"})

    def test_unknown_channel_is_ignored(self):
        class FakeEnum:
            Plugin = "插件枚举"

        with patch.object(self.notify, "enums", return_value=(FakeEnum, FakeEnum)):
            kwargs = self.notify.message_kwargs("NotAChannel")
        self.assertEqual(kwargs, {"mtype": "插件枚举"})

    def test_enums_missing_falls_back_to_no_kwargs(self):
        with patch.object(self.notify, "enums", return_value=(None, None)):
            self.assertEqual(self.notify.message_kwargs("WebPush"), {})

    def test_send_passes_channel_and_mtype(self):
        recorder = self.Recorder()
        with patch.object(self.notify, "message_kwargs", return_value={
                "mtype": "插件", "channel": "Telegram"}):
            result = self.notify.send(recorder, "标题", "正文", "Telegram")
        self.assertTrue(result["ok"])
        self.assertEqual(recorder.calls[0]["mtype"], "插件")
        self.assertEqual(recorder.calls[0]["channel"], "Telegram")
        self.assertEqual(recorder.calls[0]["title"], "标题")
        self.assertEqual(result["channel_label"], "Telegram")

    def test_send_falls_back_to_plain_call(self):
        recorder = self.Recorder(fail_first=True)
        with patch.object(self.notify, "message_kwargs", return_value={"mtype": "插件"}):
            result = self.notify.send(recorder, "标题", "正文")
        self.assertTrue(result["ok"])
        self.assertEqual(len(recorder.calls), 2)
        self.assertEqual(set(recorder.calls[1]), {"title", "text"})
        self.assertIn("回退", result["via"])

    def test_send_reports_failure_and_missing_hook(self):
        recorder = self.Recorder(fail_all=True)
        with patch.object(self.notify, "message_kwargs", return_value={}):
            result = self.notify.send(recorder, "标题", "正文")
        self.assertFalse(result["ok"])
        self.assertIn("简版也失败", result["error"])
        self.assertFalse(self.notify.send(object(), "标题", "正文")["ok"])

    def test_services_never_raises(self):
        self.assertIsInstance(self.notify.services(), list)

    def test_test_notify_endpoint_saves_receipt(self):
        result = self.plugin.api_test_notify()
        self.assertTrue(result.success)
        self.assertTrue(result.data["notify_test"]["test"])
        self.assertEqual(self.plugin.get_data("ugn_notify")["test"], True)
        self.assertIn("通知测试", self.plugin.get_data("ugn_log")[0]["message"])
        self.assertIn("通知", result.data["html"])

    def test_trial_mode_sends_notification(self):
        """用户明确要求：试运行时也要发通知，用来验证通知模块。"""
        self.plugin.init_plugin({"mode": "试运行", "cooldown": 0})
        high = full_raw()
        high["volumes"][0]["percent"] = 95.0
        with patch.object(NAS, "collect", return_value=bridge_collected(high)), \
             patch.object(self.notify, "send", return_value={
                 "ok": True, "via": "post_message", "error": ""}) as send:
            self.plugin.run_check("试运行巡检")
        self.assertEqual(send.call_count, 1)
        self.assertIn("试运行", send.call_args[0][2])
        self.assertTrue(self.plugin.get_data("ugn_notify")["ok"])

    def test_remind_only_mode_sends_notification(self):
        self.plugin.init_plugin({"mode": "仅提醒", "cooldown": 0})
        high = full_raw()
        high["volumes"][0]["percent"] = 95.0
        with patch.object(NAS, "collect", return_value=bridge_collected(high)), \
             patch.object(self.notify, "send", return_value={
                 "ok": True, "via": "post_message", "error": ""}) as send:
            self.plugin.run_check("仅提醒巡检")
        self.assertEqual(send.call_count, 1)

    def test_notification_can_be_turned_off(self):
        self.plugin.init_plugin({"mode": "试运行", "cooldown": 0, "notify_enabled": False})
        high = full_raw()
        high["volumes"][0]["percent"] = 95.0
        with patch.object(NAS, "collect", return_value=bridge_collected(high)), \
             patch.object(self.notify, "send", side_effect=AssertionError("不该发通知")):
            self.plugin.run_check("关通知")
        self.assertEqual(self.plugin.get_data("ugn_notify"), None)

    def test_channel_setting_is_forwarded(self):
        self.plugin.init_plugin({"notify_channel": "WebPush"})
        result = self.plugin.api_test_notify()
        self.assertEqual(result.data["notify_test"]["channel"], "WebPush")
        self.assertEqual(self.plugin.get_data("ugn_notify")["channel_label"], "WebPush 浏览器推送")

    def test_failed_notification_shows_on_page(self):
        with patch.object(self.notify, "send", return_value={
                "ok": False, "error": "宿主没有 post_message", "via": ""}):
            result = self.plugin.api_test_notify()
        self.assertFalse(result.data["notify_test"]["ok"])
        self.assertEqual(self.plugin.get_data("ugn_log")[0]["level"], "ERROR")
        html = RENDERER.render(result.data)
        self.assertIn("调用失败", html)
        self.assertIn("宿主没有 post_message", html)

    def test_notify_block_escapes_dynamic_text(self):
        dirty = "<img src=x onerror=alert(1)>"
        view = self.plugin.api_snapshot().data
        view["notify"] = {"ok": False, "at": "10:00:00", "channel_label": dirty,
                          "via": "", "error": dirty, "test": True}
        view["notify_services"] = [{"name": dirty, "type": "微信", "state": "启用"}]
        html = RENDERER.render(view)
        self.assertNotIn(dirty, html)
        self.assertIn("&lt;img", html)
        for marker in ("<script", "onclick=", "onload=", "javascript:"):
            self.assertNotIn(marker, html, marker)


class TestRealHostSignature(unittest.TestCase):
    """宿主 _PluginBase.get_data(key=None, plugin_id=None) 没有 default 参数，
    数据不存在时返回 None；桩里的 get_data(key, default) 会把这个 bug 盖住。"""

    def setUp(self):
        self.plugin = PLUGIN_CLS()
        self.db = {}
        self.plugin.get_data = lambda key=None, plugin_id=None: self.db.get(key)
        self.plugin.save_data = lambda key, value, plugin_id=None: self.db.__setitem__(key, value)

    def test_all_endpoints_work_when_nothing_is_stored(self):
        for name, kwargs in (("api_snapshot", {}), ("api_check", {}),
                             ("api_test_ssh", {}), ("api_test_notify", {}),
                             ("api_resume", {}), ("api_refresh", {}),
                             ("api_clear", {"confirm": "yes"})):
            with self.subTest(name=name):
                result = getattr(self.plugin, name)(**kwargs)
                self.assertIsInstance(result, MODULE.Response)
                self.assertEqual(set(result.model_dump()), {"success", "message", "data"})

    def test_page_and_form_work_with_none_data(self):
        self.assertIsInstance(self.plugin.get_page(), list)
        self.assertIsInstance(self.plugin.get_form()[0], list)

    def test_run_check_with_none_data(self):
        self.plugin.init_plugin({"mode": "试运行", "cooldown": 0})
        with patch.object(NAS, "collect", return_value=bridge_collected(full_raw())):
            view = self.plugin.run_check("真实签名")
        self.assertIsInstance(view["series"], list)
        self.assertEqual(len(self.db["ugn_series"]), 1)


class TestButtonWording(unittest.TestCase):
    def test_buttons_are_short_and_plain(self):
        page = PLUGIN_CLS().get_page()
        labels = [item.get("text") for item in page if item.get("component") == "VBtn"]
        self.assertEqual(labels, ["立即巡检", "刷新页面", "测试连接", "测试通知",
                                  "查看快照", "恢复下载", "清空日志"])
        for label in labels:
            self.assertLessEqual(len(label), 5)
            for word in ("第一步", "三步走", "智能", "一键"):
                self.assertNotIn(word, label)


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
