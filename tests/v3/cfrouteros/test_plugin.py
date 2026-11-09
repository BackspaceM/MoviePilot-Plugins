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
    assert CFRouterOS.plugin_version == "1.0.0"
    assert CFRouterOS.get_command()[0]["cmd"] == "/cfrouteros_run"


def test_form_and_api():
    plugin = CFRouterOS()
    plugin.init_plugin({})
    form, defaults = plugin.get_form()
    assert form[0]["component"] == "VForm"
    assert defaults["router_port"] == 443
    assert defaults["ttl"] == "10m"
    assert defaults["ip_version"] == "4"
    assert {item["path"] for item in plugin.get_api()} == {"/run", "/check", "/status", "/clear_cache"}


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
