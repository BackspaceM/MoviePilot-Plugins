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
    assert len(CFRouterOS.plugin_version.split(".")) == 3 and all(p.isdigit() for p in CFRouterOS.plugin_version.split("."))
    assert "GITHUB_USER" not in CFRouterOS.plugin_icon
    commands = {item["cmd"] for item in CFRouterOS.get_command()}
    assert commands == {"/cfrouteros_run", "/cfrouteros_check",
                        "/cfrouteros_pool", "/cfrouteros_stop"}


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
        "/run", "/check", "/status", "/live", "/logs", "/refresh_pool",
        "/clear_cache", "/clear_logs", "/stop",
    }
    # v1.2 默认零 DoH 依赖，且带第三方 IP 池入口
    assert defaults["check_mode"] == "all"
    assert defaults["check_fallback"] is True
    assert defaults["cidr_source"] == "official+custom"
    assert "pool_urls" in defaults and "pool_text" in defaults
    assert defaults["official_v4_url"] == cfopt.CF_V4_LIST_URL


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
    assert "运行日志" not in str(page)


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


# ---------------------------------------------------------------------------
# v1.2.0：去 DoH 依赖、第三方 IP 池、逐站点实时日志
# ---------------------------------------------------------------------------

def test_parse_ip_pool_text_formats():
    parsed = cfopt.parse_ip_pool("""
# 第三方维护的池
104.16.0.1
104.16.0.2:443 12ms
104.16.0.3, 104.17.1.1; 104.18.2.2
104.16.0.0/13
[2606:4700::1]:443
1.1.1.1# CF DNS
192.168.1.1
bad-token
""")
    assert "104.16.0.1" in parsed["v4"] and "104.16.0.2" in parsed["v4"]
    assert "104.17.1.1" in parsed["v4"] and "1.1.1.1" in parsed["v4"]
    assert "2606:4700::1" in parsed["v6"]
    assert "104.16.0.0/13" in parsed["cidrs"]
    assert "192.168.1.1" not in parsed["ips"]


def test_parse_ip_pool_json_formats():
    assert cfopt.parse_ip_pool('["104.16.0.9", "104.16.0.10"]')["ips"] == [
        "104.16.0.9", "104.16.0.10",
    ]
    obj = cfopt.parse_ip_pool('{"data": [{"ip": "104.16.1.1", "ms": 12}, {"ip": "104.16.1.2"}]}')
    assert obj["ips"] == ["104.16.1.1", "104.16.1.2"]
    nested = cfopt.parse_ip_pool('{"ips": {"ipv4": ["172.64.0.1"], "ipv6": ["2606:4700::2"]}}')
    assert "172.64.0.1" in nested["ips"] and "2606:4700::2" in nested["ips"]
    assert cfopt.parse_ip_pool("")["ips"] == []


def test_parse_ip_pool_rejects_private_and_invalid():
    parsed = cfopt.parse_ip_pool("10.0.0.1\n127.0.0.1\n::1\n999.1.1.1\n104.16.0.5")
    assert parsed["ips"] == ["104.16.0.5"]


def test_classify_domain_default_is_independent_of_doh():
    nets = cfopt.build_networks()
    accepted, reason, _ = cfopt.classify_domain([], nets, mode="all")
    assert accepted and "未做 DNS 判定" in reason


def test_classify_domain_by_cloudflare_cdn_ip_and_fallback():
    nets = cfopt.build_networks()
    accepted, reason, cf = cfopt.classify_domain(
        ["8.8.8.8", "104.16.0.1"], nets, mode="system", fallback=False,
    )
    assert accepted and cf == ["104.16.0.1"] and "CDN IP" in reason
    skipped, reason, _ = cfopt.classify_domain(["8.8.8.8"], nets, mode="system", fallback=False)
    assert not skipped and "已跳过" in reason
    # 解析失败时按 fallback 决定，避免重演「全部域名被跳过」
    failed, reason, _ = cfopt.classify_domain([], nets, mode="doh", fallback=True)
    assert failed and "仍纳入" in reason
    failed2, reason2, _ = cfopt.classify_domain([], nets, mode="doh", fallback=False)
    assert not failed2 and "已跳过" in reason2


def test_build_candidate_pool_priority_and_strict():
    nets = cfopt.build_networks()
    pool = cfopt.build_candidate_pool(
        ["104.16.9.9"], ["104.16.8.8"], ["104.16.7.7"], 3,
    )
    assert pool == ["104.16.9.9", "104.16.7.7", "104.16.8.8"]
    strict = cfopt.build_candidate_pool(
        ["8.8.8.8", "104.16.9.9"], [], [], 5, networks=nets, strict_cf=True,
    )
    assert strict == ["104.16.9.9"]


def test_parse_doh_urls_uses_builtin_multi_source():
    assert cfopt.parse_doh_urls("") == list(cfopt.DEFAULT_DOH_URLS)
    urls = cfopt.parse_doh_urls("https://a.example/dns-query\nhttps://b.example/dns-query")
    assert len(urls) == 2
    assert cfopt.parse_doh_urls("not-a-url\n# 注释") == list(cfopt.DEFAULT_DOH_URLS)


def test_resolve_system_dns_uses_socket_getaddrinfo():
    original = socket.getaddrinfo
    socket.getaddrinfo = lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.16.0.1", 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.16.0.2", 0)),
    ]
    try:
        ips = asyncio.run(cfopt.resolve_system_dns("hdwing.org", timeout=1.0))
    finally:
        socket.getaddrinfo = original
    assert ips == ["104.16.0.1", "104.16.0.2"]


def _fake_sources(plugin, pool_ips=None, cidrs=None):
    """替换 IP 来源加载，避免测试访问网络。"""
    from app.plugins import cfrouteros as mod

    async def _load(http, force=False):
        networks = cfopt.build_networks()
        summary = {
            "networks": networks,
            "pool_ips": list(pool_ips or []),
            "pool_cidrs": list(cidrs or []),
            "sources": [{"name": "测试来源", "url": "", "ok": True,
                         "ips": len(pool_ips or []), "cidrs": 0, "error": "", "cached": False}],
            "official_ok": True,
            "source_mode": "builtin",
            "network_count": len(networks),
            "pool_count": len(pool_ips or []),
            "updated_text": "2026-11-10 00:00:00",
            "from_cache": False,
        }
        plugin.save_data("pool_sources", {k: v for k, v in summary.items() if k != "networks"})
        return summary

    original = plugin._load_ip_sources
    plugin._load_ip_sources = _load
    return original


def _fake_probes():
    from app.plugins import cfrouteros as mod

    async def _probe(ips, **kwargs):
        return [cfopt.ProbeResult(ip=ip, latency_ms=10.0 + index, ok=True)
                for index, ip in enumerate(ips)]

    original = mod.probe_many
    mod.probe_many = _probe
    return original


def test_default_mode_never_skips_domains_without_doh():
    """回归用户报告：DoH 不可达时所有域名都显示「跳过」。"""
    from app.plugins import cfrouteros as mod

    plugin = CFRouterOS()
    plugin.init_plugin({
        "enabled": True, "dry_run": True, "auto_sites": False,
        "domains": "hdwing.org\npt.example.com",
        "check_mode": "all", "check_fallback": True, "log_resolved_ips": False,
        "cidr_source": "builtin", "refresh_cidrs": False,
        "probe_count": 4, "probe_concurrency": 2, "probe_timeout": 0.2,
    })
    plugin._stop_event.clear()
    original_load = _fake_sources(plugin)
    original_probe = _fake_probes()
    try:
        result = asyncio.run(plugin._run_async("测试"))
    finally:
        plugin._load_ip_sources = original_load
        mod.probe_many = original_probe
    assert result["domains_total"] == 2
    assert result["domains_cf"] == 2
    assert all(row["state"] != "跳过" for row in result["domains"])
    assert result["best"] and result["best_latency"] > 0
    logs = [item["message"] for item in plugin.get_data("run_logs")]
    assert any("[站点" in line and "hdwing.org" in line for line in logs)
    assert any("[写入]" in line and "仅测速" in line for line in logs)
    assert any("[判定]" in line and "纳入 2" in line for line in logs)


def test_doh_failure_fallback_keeps_sites_eligible():
    """DoH 判定模式下多源全失败，解析为空仍按 fallback 纳入。"""
    from app.plugins import cfrouteros as mod

    async def _no_resolution(http, domains):
        return ({item: [] for item in domains}, {item: "解析失败" for item in domains})

    for fallback, expected in ((True, 1), (False, 0)):
        plugin = CFRouterOS()
        plugin.init_plugin({
            "enabled": True, "dry_run": True, "auto_sites": False,
            "domains": "hdwing.org", "check_mode": "doh",
            "check_fallback": fallback, "cidr_source": "builtin", "refresh_cidrs": False,
        })
        plugin._stop_event.clear()
        plugin._resolve_domains = _no_resolution
        original_load = _fake_sources(plugin)
        original_probe = _fake_probes()
        try:
            result = asyncio.run(plugin._run_async("测试"))
        finally:
            plugin._load_ip_sources = original_load
            mod.probe_many = original_probe
        assert result["domains_cf"] == expected


def test_run_logs_include_site_ips_best_ips_and_write_status():
    """逐站点日志应包含原始 IP、优选 IP 与 RouterOS 写入结果。"""
    from app.plugins import cfrouteros as mod

    plugin = CFRouterOS()
    plugin.init_plugin({
        "enabled": True, "dry_run": True, "auto_sites": False,
        "domains": "hdwing.org", "check_mode": "all", "log_resolved_ips": True,
        "cidr_source": "builtin", "refresh_cidrs": False,
        "probe_count": 3, "top_n": 2,
    })
    plugin._stop_event.clear()

    async def _dns(domain, **kwargs):
        return ["104.16.0.1", "104.16.0.2"]

    original_dns = mod.resolve_system_dns
    mod.resolve_system_dns = _dns
    original_load = _fake_sources(plugin)
    original_probe = _fake_probes()
    try:
        result = asyncio.run(plugin._run_async("测试"))
    finally:
        mod.resolve_system_dns = original_dns
        plugin._load_ip_sources = original_load
        mod.probe_many = original_probe
    logs = "\n".join(item["message"] for item in plugin.get_data("run_logs"))
    assert "104.16.0.1" in logs
    assert "[站点" in logs and "来源=系统DNS" in logs
    assert "[优选]" in logs and "[写入]" in logs
    row = result["domains"][0]
    assert row["resolved"] == ["104.16.0.1", "104.16.0.2"]
    assert len(row["addresses"]) == 2
    assert row["state"] == "跳过（仅测速）"
    assert "未写入" in row["message"]


def test_ip_pool_sources_merge_and_fallback_cache():
    class _Resp:
        def __init__(self, text):
            self.status_code = 200
            self.text = text

    class _Http:
        def __init__(self, mapping):
            self.mapping = mapping

        async def request(self, method, url, **kwargs):
            return _Resp(self.mapping[url]) if url in self.mapping else None

    plugin = CFRouterOS()
    plugin.init_plugin({
        "enabled": True, "cidr_source": "official+custom",
        "pool_urls": "https://pool.example/list.txt",
        "pool_text": "104.16.7.7",
        "refresh_cidrs": True,
    })
    mapping = {
        cfopt.CF_V4_LIST_URL: "104.16.0.0/13\n172.64.0.0/13",
        cfopt.CF_V6_LIST_URL: "2606:4700::/32",
        "https://pool.example/list.txt": "104.16.9.9\n104.16.9.10\n104.16.100.0/24",
    }
    summary = asyncio.run(plugin._load_ip_sources(_Http(mapping), force=True))
    assert summary["official_ok"] is True
    assert "104.16.9.9" in summary["pool_ips"] and "104.16.7.7" in summary["pool_ips"]
    assert "104.16.100.0/24" in summary["pool_cidrs"]
    sources = plugin.get_data("pool_sources")["sources"]
    assert any(item["url"] == "https://pool.example/list.txt" and item["ok"] for item in sources)
    assert "networks" not in plugin.get_data("pool_sources")

    # 全部源拉取失败时使用缓存与内置段，不清空池
    failed = asyncio.run(plugin._load_ip_sources(_Http({}), force=True))
    assert "104.16.9.9" in failed["pool_ips"]
    assert failed["official_ok"] is False
    assert failed["network_count"] > 0


def test_refresh_pool_api_forces_fetch_even_when_auto_off():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True, "refresh_cidrs": False, "cidr_source": "builtin"})
    response = plugin.api_refresh_pool()
    assert response.success is True
    assert "网段" in response.message
    assert "networks" not in (response.data or {})
    logs = "\n".join(item["message"] for item in plugin.get_data("run_logs"))
    assert "手动刷新完成" in logs


def test_live_progress_and_status_api():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True})
    plugin._update_live(stage="测速中", done=3, total=10, current="104.16.0.1")
    assert plugin.api_live().data["live"]["stage"] == "测速中"
    assert plugin.api_status().data["live"]["done"] == 3
    assert plugin.api_status().data["check_mode"] == "all"
    assert plugin.api_logs().data["live"]["total"] == 10


def test_exclude_domains_filters_auto_sites():
    plugin = CFRouterOS()
    plugin.init_plugin({
        "domains": "a.example.com\nb.example.com",
        "auto_sites": True, "exclude_domains": "b.example.com\nhdwing.org",
    })
    assert plugin._collect_domains() == ["a.example.com"]


def test_apply_domain_reports_written_addresses():
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
    result = asyncio.run(api.apply_domain(client, "hdwing.org", ["104.16.0.1"], records))
    assert result["written_addresses"] == ["104.16.0.1"]
    assert result["failed_addresses"] == []
    assert result["added"] == 1


def test_notification_includes_site_details():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True, "notify_mode": "全部"})
    plugin.messages.clear()
    result = {
        "trigger": "定时", "domains_total": 1, "domains_cf": 1, "probed": 4,
        "alive": 3, "written": 2, "message": "完成", "dry_run": False,
        "check_mode": "all",
        "best": [{"ip": "104.16.0.1", "latency_ms": 12.3}],
        "domains": [{
            "domain": "hdwing.org", "state": "已写入", "resolved": ["104.16.0.9"],
            "addresses": ["104.16.0.1"], "added": 2, "deleted": 2, "reason": "",
        }],
    }
    plugin._notify_result(result, 1.5)
    text = plugin.messages[-1]["text"]
    assert "站点明细" in text and "hdwing.org" in text
    assert "104.16.0.9" in text and "写入=是" in text
    assert "level" not in plugin.messages[-1]


def test_log_masks_url_credentials_and_tokens():
    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True, "router_password": "s3cr3t"})
    plugin._log("INFO", "拉取 https://user:pwd@host/x?token=abcd1234 口令 s3cr3t")
    content = plugin.get_data("run_logs")[0]["message"]
    assert "pwd" not in content and "abcd1234" not in content and "s3cr3t" not in content
    assert "***" in content


def test_doh_dead_sources_are_skipped_within_one_run():
    class _DeadHttp:
        def __init__(self):
            self.calls = 0

        async def request(self, *args, **kwargs):
            self.calls += 1
            raise OSError("unreachable")

    plugin = CFRouterOS()
    plugin.init_plugin({"enabled": True, "check_mode": "doh"})
    http = _DeadHttp()
    assert asyncio.run(plugin._resolve_domain(http, "hdwing.org")) == []
    first_calls = http.calls
    assert first_calls == len(cfopt.DEFAULT_DOH_URLS)
    # 已确认不可达的源本轮内不再重复请求，避免每个域名都等一轮超时
    assert asyncio.run(plugin._resolve_domain(http, "pt.example.com")) == []
    assert http.calls == first_calls
    assert len(plugin._doh_dead) == len(cfopt.DEFAULT_DOH_URLS)
