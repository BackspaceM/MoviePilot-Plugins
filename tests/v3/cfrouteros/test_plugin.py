"""cfrouteros 插件离线用例：不访问外网，不依赖宿主实现。"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from app.plugins.cfrouteros import CFRouterOS
from app.plugins.cfrouteros import cfopt, routeros


# ---------------------------------------------------------------------------
# 网段与归属
# ---------------------------------------------------------------------------

def test_parse_cidrs_falls_back_to_builtin():
    # 空输入由 build_networks 回落内置段；parse_cidrs 只做解析
    assert len(cfopt.build_networks("")) == len(cfopt.DEFAULT_IPV4_CIDRS) + len(cfopt.DEFAULT_IPV6_CIDRS)
    networks = cfopt.parse_cidrs("104.16.0.0/13\nnot-a-cidr\n\n172.64.0.0/13")
    assert [str(item) for item in networks] == ["104.16.0.0/13", "172.64.0.0/13"]


def test_build_networks_merges_v4_and_v6():
    # 用户提供文本时以其为准，不再混入内置段
    nets = cfopt.build_networks("104.16.0.0/13", "2606:4700::/32")
    assert [str(item) for item in nets] == ["104.16.0.0/13", "2606:4700::/32"]
    assert any(item.version == 6 for item in nets)
    assert cfopt.build_networks(enabled=False) == []


def test_is_cloudflare_ip():
    nets = cfopt.build_networks()
    assert cfopt.is_cloudflare_ip("104.16.132.229", nets) is True
    assert cfopt.is_cloudflare_ip("172.64.0.1", nets) is True
    assert cfopt.is_cloudflare_ip("8.8.8.8", nets) is False
    assert cfopt.is_cloudflare_ip("not-an-ip", nets) is False
    assert cfopt.is_cloudflare_ip("2606:4700::1", nets) is True


def test_cloudflare_ips_keeps_order_and_dedupes():
    nets = cfopt.build_networks()
    got = cfopt.cloudflare_ips(["104.16.0.1", "8.8.8.8", "104.16.0.1", "172.64.0.9"], nets)
    assert got == ["104.16.0.1", "172.64.0.9"]


def test_sample_ips_stays_in_networks():
    nets = cfopt.build_networks()
    picked = cfopt.sample_ips(nets, 30, ipv4_only=True)
    assert len(picked) == 30
    assert len(set(picked)) == 30
    for ip in picked:
        assert cfopt.is_cloudflare_ip(ip, nets)
        assert ":" not in ip


# ---------------------------------------------------------------------------
# 域名与 DoH
# ---------------------------------------------------------------------------

def test_normalize_domains_strips_scheme_and_dedupes():
    raw = "https://HDWinG.org/torrents.php?id=1\npt.example.com:443\n  hdwing.org  \nnot_a_domain\n"
    assert cfopt.normalize_domains(raw) == ["hdwing.org", "pt.example.com"]


def test_normalize_domains_accepts_list():
    assert cfopt.normalize_domains(["a.example.com", "A.example.com"]) == ["a.example.com"]


def test_parse_doh_answer_reads_a_records_only():
    payload = {
        "Status": 0,
        "Answer": [
            {"name": "hdwing.org.", "type": 5, "data": "cf.example.net."},
            {"name": "hdwing.org.", "type": 1, "data": "104.16.0.1"},
            {"name": "hdwing.org.", "type": 1, "data": "172.64.0.2"},
            {"name": "hdwing.org.", "type": 1, "data": "bad-ip"},
        ],
    }
    assert cfopt.parse_doh_answer(payload) == ["104.16.0.1", "172.64.0.2"]
    assert cfopt.parse_doh_answer({}) == []
    assert cfopt.parse_doh_answer({"Answer": "bad"}) == []


# ---------------------------------------------------------------------------
# 探测与排序
# ---------------------------------------------------------------------------

def test_pick_fastest_sorts_and_limits():
    results = [
        cfopt.ProbeResult("1.1.1.1", 80.0, True),
        cfopt.ProbeResult("2.2.2.2", 20.0, True),
        cfopt.ProbeResult("3.3.3.3", 5.0, False),
        cfopt.ProbeResult("4.4.4.4", 50.0, True),
    ]
    best = cfopt.pick_fastest(results, 2)
    assert [item.ip for item in best] == ["2.2.2.2", "4.4.4.4"]
    assert cfopt.pick_fastest(results, 0) == []


def test_format_latency():
    assert cfopt.format_latency(0) == "-"
    assert cfopt.format_latency(12.34) == "12 ms"
    assert cfopt.format_latency(150.6) == "151 ms"


def test_probe_once_success_and_failure():
    async def _run():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            ok = await cfopt.probe_once("127.0.0.1", port=port, timeout=2)
            assert ok.ok is True
            assert ok.latency_ms >= 0
        finally:
            server.close()
            await server.wait_closed()
        # 端口已关闭，连接应被拒绝
        failed = await cfopt.probe_once("127.0.0.1", port=port, timeout=1)
        assert failed.ok is False
        assert failed.error

    asyncio.run(_run())


def test_probe_many_returns_all_results():
    async def _run():
        return await cfopt.probe_many([], port=443, timeout=0.5)

    assert asyncio.run(_run()) == []


# ---------------------------------------------------------------------------
# DNS static 记录
# ---------------------------------------------------------------------------

def test_build_record_fields():
    record = cfopt.build_record("hdwing.org", "104.16.0.1", "10m", "cfopt")
    assert record == {
        "name": "hdwing.org",
        "address": "104.16.0.1",
        "type": "A",
        "ttl": "10m",
        "comment": "cfopt:hdwing.org",
    }
    v6 = cfopt.build_record("hdwing.org", "2606:4700::1", "1h", "cfopt")
    assert v6["type"] == "AAAA"


def test_build_record_defaults():
    record = cfopt.build_record("a.example.com", "1.2.3.4")
    assert record["ttl"] == "10m"
    assert record["comment"] == "cfopt:a.example.com"


# ---------------------------------------------------------------------------
# RouterOS 客户端
# ---------------------------------------------------------------------------

def test_normalize_base_url():
    # 返回的 base 不含 /rest，路径由 DNS_STATIC_PATH 自带 /rest 前缀
    assert routeros.normalize_base_url("192.168.88.1") == "https://192.168.88.1"
    assert routeros.normalize_base_url("http://192.168.88.1:8080/rest") == "http://192.168.88.1:8080"
    assert routeros.normalize_base_url("192.168.88.1/", 80, False) == "http://192.168.88.1"
    assert routeros.normalize_base_url("") == ""
    assert routeros.normalize_base_url("192.168.88.1", 8728, False) == "http://192.168.88.1:8728"


def test_basic_auth_header():
    header = routeros.basic_auth_header("admin", "p@ss")
    assert header["Authorization"].startswith("Basic ")
    assert "Content-Type" not in header


def test_parse_records():
    payload = [
        {"\\.id": "*1A", "name": "hdwing.org", "address": "104.16.0.1", "type": "A",
         "ttl": "10m", "comment": "cfopt:hdwing.org", "disabled": "false"},
        {"name": "skip", "type": "CNAME"},
        "garbage",
    ]
    records = routeros.parse_records(payload)
    assert len(records) == 1
    assert records[0].id == "*1A"
    assert records[0].address == "104.16.0.1"
    assert routeros.parse_records({"a": 1}) == []


def test_extract_record_id():
    assert routeros.extract_record_id({"\\.id": "*2B"}) == "*2B"
    assert routeros.extract_record_id({"id": "x"}) == "x"
    assert routeros.extract_record_id(None) == ""


def test_describe_error():
    assert "认证失败" in routeros.describe_error(401, "")
    assert "认证失败" in routeros.describe_error(403, "")
    assert "v7" in routeros.describe_error(404, "")
    assert "参数" in routeros.describe_error(400, {"detail": "invalid ttl"})
    assert "内部错误" in routeros.describe_error(500, "")


class _FakeResponse:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False) if isinstance(body, (list, dict)) else str(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _FakeClient:
    """按 method+path 返回预置响应的假 HTTP 客户端。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        path = url.split("/rest", 1)[-1]
        key = (method, path.split("?", 1)[0])
        if key in self.routes:
            return _FakeResponse(200, self.routes[key])
        for (r_method, r_path), body in self.routes.items():
            if r_method == method and url.split("?", 1)[0].endswith(r_path):
                return _FakeResponse(200, body)
        return _FakeResponse(200, [])


def test_apply_domain_replaces_own_records_only():
    routes = {
        ("GET", "/ip/dns/static"): [
            {"\\.id": "*1", "name": "hdwing.org", "address": "104.16.0.9",
             "type": "A", "comment": "cfopt:hdwing.org"},
        ],
        ("PUT", "/ip/dns/static"): {"\\.id": "*9"},
    }
    client = _FakeClient(routes)
    api = routeros.RouterOSClient("https://192.168.88.1/rest", "admin", "pwd")
    records = [cfopt.build_record("hdwing.org", "104.16.0.1"), cfopt.build_record("hdwing.org", "104.16.0.2")]
    result = asyncio.run(api.apply_domain(client, "hdwing.org", ["104.16.0.1", "104.16.0.2"], records))
    assert result["deleted"] == 1
    assert result["added"] == 2
    assert result["addresses"] == ["104.16.0.1", "104.16.0.2"]
    assert result["errors"] == []


def test_check_reports_identity():
    client = _FakeClient({("GET", "/system/identity"): {"name": "RouterOS", "version": "7.14"}})
    api = routeros.RouterOSClient("https://192.168.88.1/rest", "admin", "pwd")
    ok, message = asyncio.run(api.check(client))
    assert ok is True
    assert "7.14" in message


# ---------------------------------------------------------------------------
# 插件契约
# ---------------------------------------------------------------------------

def test_plugin_metadata():
    assert CFRouterOS.plugin_author == "BackspaceM"
    assert CFRouterOS.plugin_config_prefix == "cfrouteros_"
    assert CFRouterOS.plugin_version == "1.1.0"
    assert "GITHUB_USER" not in CFRouterOS.plugin_icon
    commands = {item["cmd"] for item in CFRouterOS.get_command()}
    assert commands == {"/cfrouteros_run", "/cfrouteros_check", "/cfrouteros_stop"}


def test_form_and_api():
    plugin = CFRouterOS()
    plugin.init_plugin({})
    form, defaults = plugin.get_form()
    assert form[0]["component"] == "VForm"
    assert defaults["router_port"] == 443
    assert defaults["ttl"] == "10m"
    assert defaults["ip_version"] == "4"
    assert defaults["notify_mode"] == "全部"
    assert defaults["run_now"] is False
    assert "min_success" in defaults
    assert {item["path"] for item in plugin.get_api()} == {
        "/run", "/check", "/status", "/logs", "/clear_cache", "/clear_logs", "/stop"
    }


def test_service_only_when_enabled():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": False})
    assert plugin.get_service() == []
    plugin.init_plugin({"enabled": True, "cron": "30 4 * * *"})
    services = plugin.get_service()
    assert len(services) == 1
    assert services[0]["id"] == "CFRouterOS"
    plugin.init_plugin({"enabled": True, "cron": "bad cron"})
    assert plugin.get_service()[0]["id"] == "CFRouterOS"


def test_collect_domains_merges_sites_and_manual():
    plugin = CFRouterOS()
    plugin.init_plugin({
        "domains": "hdwing.org\nhttps://pt.example.com/",
        "auto_sites": True,
    })
    domains = plugin._collect_domains()
    assert "hdwing.org" in domains
    assert "pt.example.com" in domains
    assert domains.count("hdwing.org") == 1


def test_collect_domains_without_auto_sites():
    plugin = CFRouterOS()
    plugin.init_plugin({"domains": "a.example.com", "auto_sites": False})
    assert plugin._collect_domains() == ["a.example.com"]


def test_api_guards_without_config():
    plugin = CFRouterOS()
    plugin.init_plugin({})
    assert plugin.api_run().success is False
    assert plugin.api_check().success is False
    assert plugin.api_status().success is True
    assert plugin.api_clear_cache().success is True


def test_dry_run_result_not_written():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True, "dry_run": True, "auto_sites": False})
    assert plugin._dry_run is True
    # 无域名时直接返回，不触网
    result = asyncio.run(plugin._run_async("测试"))
    assert result["written"] == 0
    assert result["domains_total"] == 0


# ---------------------------------------------------------------------------
# 优化项回归：立即运行、日志、通知、可靠性
# ---------------------------------------------------------------------------

def _find_buttons(node):
    """递归收集详情页 JSON 中的 VBtn。"""
    found = []
    if isinstance(node, dict):
        if node.get("component") == "VBtn":
            found.append(node)
        for child in node.get("content") or []:
            found.extend(_find_buttons(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_find_buttons(child))
    return found


def test_detail_buttons_use_official_events_schema():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True})
    buttons = _find_buttons(plugin.get_page())
    assert len(buttons) >= 4
    by_text = {item["text"]: item for item in buttons}
    assert by_text["立即执行"]["events"]["click"] == {
        "api": "plugin/CFRouterOS/run", "method": "post",
    }
    assert by_text["检测路由器"]["events"]["click"]["api"] == "plugin/CFRouterOS/check"
    assert by_text["刷新页面"]["events"]["click"]["method"] == "get"
    # 旧写法 actions/VOn/handler 前端 PageRender 完全不识别
    assert all("actions" not in item for item in buttons)


def test_detail_page_has_buttons_even_when_disabled():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": False})
    page = plugin.get_page()
    assert _find_buttons(page)
    assert "运行日志" in str(page)


def test_run_now_triggers_once_and_resets_switch():
    plugin = CFRouterOS()
    calls = []
    plugin.update_config = lambda config, plugin_id=None: calls.append(("config", config.get("run_now"))) or True
    plugin.start_run = lambda **kwargs: calls.append(("run", kwargs)) or True
    plugin.init_plugin({"enabled": True, "run_now": True, "cron": "0 5 * * *"})
    assert ("config", False) in calls
    assert any(kind == "run" and kwargs.get("prefer_thread") is True
               for kind, kwargs in calls if isinstance(kwargs, dict))


def test_notify_modes_filter_success_and_failure():
    success = {
        "trigger": "定时", "domains_total": 1, "domains_cf": 1, "probed": 10,
        "alive": 8, "written": 4, "best": [], "message": "完成", "dry_run": False,
    }
    failure = dict(success, written=0, message="路由器连接失败")
    for mode, should_send in (("全部", True), ("仅异常", False), ("关闭", False)):
        plugin = CFRouterOS()
        plugin.init_plugin({"enabled": True, "notify_mode": mode})
        plugin.messages.clear()
        plugin._notify_result(success, 1.2)
        assert bool(plugin.messages) is should_send
    for mode, should_send in (("全部", True), ("仅异常", True), ("关闭", False)):
        plugin = CFRouterOS()
        plugin.init_plugin({"enabled": True, "notify_mode": mode})
        plugin.messages.clear()
        plugin._notify_result(failure, 1.2)
        assert bool(plugin.messages) is should_send
        if should_send:
            assert "level" not in plugin.messages[-1]  # V3 Message 不接受 level
            assert plugin.messages[-1]["title"].endswith("执行异常")


def test_notification_uses_mp_channel_and_user_route():
    from app.sdk.events import Event

    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True})
    plugin.messages.clear()
    plugin._send_message("标题", "正文", channel="telegram", userid="u1")
    assert plugin.messages[-1]["channel"] == "telegram"
    assert plugin.messages[-1]["userid"] == "u1"
    plugin.messages.clear()
    plugin.handle_plugin_action(Event("plugin_action", {
        "action": "cfrouteros_stop", "channel": "wechat", "user": "u1",
    }))
    assert plugin.messages[-1]["channel"] == "wechat"
    assert plugin.messages[-1]["userid"] == "u1"


def test_logs_persisted_masked_and_clearable():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True, "router_password": "s3cr3t"})
    plugin._log("INFO", "连接 s3cr3t 成功")
    logs = plugin.get_data("run_logs")
    assert logs[0]["level"] == "INFO"
    assert "s3cr3t" not in logs[0]["message"]
    assert "***" in logs[0]["message"]
    assert plugin.api_logs().data["logfile"] == "plugins/cfrouteros.log"
    assert plugin.api_clear_logs().success is True
    assert plugin.get_data("run_logs") == []


def test_new_api_and_instance_state_isolation():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True})
    assert plugin.api_status().data["notify_mode"] == "全部"
    assert plugin.api_stop().success is True
    assert plugin.api_logs().success is True
    other = CFRouterOS()
    assert plugin._state_lock is not other._state_lock
    assert plugin._stop_event is not other._stop_event


def test_resolve_domain_queries_aaaa_in_dual_stack():
    class _DohResponse:
        status_code = 200

        def json(self):
            return {"Answer": [{"type": 28, "data": "2606:4700::1"}]}

    class _DohClient:
        def __init__(self):
            self.types = []

        async def request(self, method, url, **kwargs):
            self.types.append((kwargs.get("params") or {}).get("type"))
            return _DohResponse()

    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True, "ip_version": "46"})
    client = _DohClient()
    assert asyncio.run(plugin._resolve_domain(client, "hdwing.org")) == ["2606:4700::1"]
    assert client.types == ["A", "AAAA"]

    plugin4 = CFRouterOS()
    plugin4.init_plugin({"enabled": True, "ip_version": "4"})
    client4 = _DohClient()
    asyncio.run(plugin4._resolve_domain(client4, "hdwing.org"))
    assert client4.types == ["A"]


def test_apply_domain_keeps_old_records_when_writes_fail():
    class _FailClient(_FakeClient):
        async def request(self, method, url, **kwargs):
            if method == "PUT":
                self.calls.append((method, url, kwargs.get("json")))
                return _FakeResponse(400, {"detail": "bad record"})
            return await super().request(method, url, **kwargs)

    routes = {
        ("GET", "/ip/dns/static"): [
            {"\\.id": "*1", "name": "hdwing.org", "address": "104.16.0.9",
             "type": "A", "comment": "cfopt:hdwing.org"},
        ],
    }
    client = _FailClient(routes)
    api = routeros.RouterOSClient("https://192.168.88.1/rest", "admin", "pwd")
    records = [cfopt.build_record("hdwing.org", "104.16.0.1")]
    result = asyncio.run(api.apply_domain(client, "hdwing.org", ["104.16.0.1"], records))
    assert result["added"] == 0
    assert result["deleted"] == 0
    assert not any(call[0] == "DELETE" for call in client.calls)
    assert "保留原有记录" in result["errors"][-1]


def test_apply_domain_writes_before_deleting():
    routes = {
        ("GET", "/ip/dns/static"): [
            {"\\.id": "*1", "name": "hdwing.org", "address": "104.16.0.9",
             "type": "A", "comment": "cfopt:hdwing.org"},
        ],
        ("PUT", "/ip/dns/static"): {"\\.id": "*9"},
    }
    client = _FakeClient(routes)
    api = routeros.RouterOSClient("https://192.168.88.1/rest", "admin", "pwd")
    records = [cfopt.build_record("hdwing.org", "104.16.0.1")]
    asyncio.run(api.apply_domain(client, "hdwing.org", ["104.16.0.1"], records))
    methods = [call[0] for call in client.calls]
    assert methods.index("PUT") < methods.index("DELETE")
