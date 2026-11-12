"""绿联 NAS 守护的状态采集、解析与规则判断，尽量只依赖标准库。

SSH 这一段只用一个复合 shell 探针拿数据，不在远端写文件、不改配置；
解析全在本地做，没读到的段落一律标「未上报」。
"""

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

TIMEOUT = 8
MISSING = "未上报"
DEFAULT_CONTAINERS = ["moviepilot", "qbittorrent", "transmission", "emby", "plex"]

# df 里这些都不是真磁盘，别混进存储卷
_PSEUDO_FS = {
    "tmpfs", "devtmpfs", "proc", "sysfs", "devpts", "cgroup", "cgroup2",
    "securityfs", "pstore", "mqueue", "hugetlbfs", "debugfs", "tracefs",
    "configfs", "fusectl", "binfmt_misc", "none", "udev", "ramfs",
}
_PARAMIKO_REASON = "容器里没有 paramiko，在 MoviePilot 容器执行 pip install paramiko 后重启插件"
_PROBE_TIMEOUT = 15
_ACTION_TIMEOUT = 15
_MAX_OUTPUT = 200000
_SECTION_RE = re.compile(r"^===UGN:([a-z]+)===\s*$")
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 远程动作只允许单条简单命令；危险动词按词边界匹配，避免把 warm / add 误判成 rm / dd
_DANGEROUS_WORDS = (
    r"\brm\b", r"\bmkfs\w*\b", r"\bdd\b", r"\bshutdown\b", r"\breboot\b",
    r"\bpoweroff\b", r"\bhalt\b", r"\bchmod\b", r"\bchown\b", r"\bkill\b",
    r"\bpkill\b", r"\bcurl\b", r"\bwget\b", r"\beval\b", r"\bexec\b",
    r"\bsystemctl\s+stop\b", r"\bservice\s+stop\b", r"\binit\s+0\b",
)
_DANGEROUS_CHARS = ("`", "$(", ">", "|", ";", "&&", "||", "\n", "\\", ":(){")
_SAFE_CMD_RE = re.compile(r"^[\w\s./:=@'\"{},\[\]-]+$", re.UNICODE)

# paramiko 连接复用：秒级巡检不能每次都重新握手
_SESSION_LOCK = threading.RLock()
_SESSION: Dict[str, Any] = {"client": None, "key": "", "transport": ""}
_LAST_SSH_ERROR: Dict[str, str] = {"text": ""}


def _now_text() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_text(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and text != MISSING


def _as_num(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return MISSING
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text or text == MISSING:
        return MISSING
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return MISSING


def _as_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if value is None:
        return MISSING
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on", "是", "真"):
        return True
    if text in ("false", "0", "no", "off", "否", "假"):
        return False
    return MISSING


def _as_text(value: Any) -> str:
    if value is None:
        return MISSING
    text = str(value).strip()
    return text or MISSING


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def parse_status(raw: Any) -> Dict[str, Any]:
    """把任意来源的 JSON 整理成固定 schema，缺的字段一律标未上报。"""
    raw = raw if isinstance(raw, dict) else {}

    volumes: List[Dict[str, Any]] = []
    for item in _as_list(raw.get("volumes")):
        if not isinstance(item, dict):
            continue
        volumes.append({
            "name": _as_text(item.get("name")),
            "total": _as_num(item.get("total")),
            "used": _as_num(item.get("used")),
            "percent": _as_num(item.get("percent")),
        })

    disks: List[Dict[str, Any]] = []
    for item in _as_list(raw.get("disks")):
        if not isinstance(item, dict):
            continue
        disks.append({
            "name": _as_text(item.get("name")),
            "temp": _as_num(item.get("temp")),
            "health": _as_text(item.get("health")),
            "smart_alert": _as_bool(item.get("smart_alert")),
        })

    containers: List[Dict[str, Any]] = []
    for item in _as_list(raw.get("containers")):
        if not isinstance(item, dict):
            continue
        containers.append({
            "name": _as_text(item.get("name")),
            "status": _as_text(item.get("status")),
            "restarts": _as_num(item.get("restarts")),
        })

    ups = raw.get("ups") if isinstance(raw.get("ups"), dict) else {}
    system = raw.get("system") if isinstance(raw.get("system"), dict) else {}
    return {
        "ts": _as_text(raw.get("ts")),
        "volumes": volumes,
        "disks": disks,
        "containers": containers,
        "ups": {
            "on_battery": _as_bool(ups.get("on_battery")),
            "battery": _as_num(ups.get("battery")),
        },
        "system": {
            "cpu": _as_num(system.get("cpu")),
            "mem": _as_num(system.get("mem")),
        },
    }


def parse_df(text: Any) -> List[Dict[str, Any]]:
    """解析 df -kP 的 POSIX 输出，只留真实分区，产出和状态桥统一 schema 的 volumes。"""
    volumes: List[Dict[str, Any]] = []
    seen = set()
    for line in str(text or "").splitlines():
        # 最多切 5 刀，挂载点里带空格也能整段留住
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        fs, blocks, used, available, _capacity, mount = parts
        name = fs.strip()
        if name.lower() == "filesystem" or name.lower() in _PSEUDO_FS:
            continue
        try:
            total_kb = int(blocks)
            used_kb = int(used)
            avail_kb = int(available)
        except (TypeError, ValueError):
            continue
        if total_kb <= 0 or name in seen:
            continue
        seen.add(name)
        # 单位统一成字节，和容器自检的 shutil.disk_usage 对齐
        denom = used_kb + avail_kb
        percent = round(used_kb / denom * 100.0, 1) if denom > 0 else round(used_kb / total_kb * 100.0, 1)
        volumes.append({
            "name": mount.strip() or name,
            "total": total_kb * 1024,
            "used": used_kb * 1024,
            "percent": percent,
        })
    return volumes


def _scrub(text: Any, cfg: Optional[Dict[str, Any]] = None) -> str:
    """报错一律先脱敏：密码、私钥整段和每一行都不能进日志或页面。"""
    out = str(text or "")
    for key in ("ssh_password", "ssh_key"):
        secret = str((cfg or {}).get(key) or "").strip()
        if not secret:
            continue
        out = out.replace(secret, "***")
        for line in secret.splitlines():
            line = line.strip()
            if len(line) > 8:
                out = out.replace(line, "***")
    return out[:400]


def paramiko_state() -> Dict[str, Any]:
    """只查依赖在不在，延迟导入，缺了也不抛，交给 collect 降级。"""
    try:
        import paramiko  # noqa: F401
        return {"available": True, "reason": ""}
    except Exception:
        return {"available": False, "reason": _PARAMIKO_REASON}


def ssh_cli_state() -> Dict[str, Any]:
    """容器自带的 ssh 命令能不能用；只能走私钥，密码交给 paramiko。"""
    path = shutil.which("ssh") or ""
    return {"available": bool(path), "path": path,
            "reason": "" if path else "容器里也没有 ssh 命令"}


def transport_state(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """选一条能用的传输：paramiko 优先，其次 ssh 命令 + 私钥。"""
    pm = paramiko_state()
    cli = ssh_cli_state()
    has_key = bool(str(cfg.get("ssh_key") or "").strip())
    has_password = bool(str(cfg.get("ssh_password") or ""))
    if pm["available"]:
        transport = "paramiko" + ("（私钥）" if has_key else "")
        note = "私钥登录" if has_key else ""
        available, reason = True, ""
    elif cli["available"] and has_key:
        transport, note = "ssh 命令（私钥）", "paramiko 未安装，改走容器自带的 ssh 命令"
        available, reason = True, ""
    elif cli["available"]:
        transport, note = "", "ssh 命令只能走私钥，请填 SSH 私钥或安装 paramiko"
        available, reason = False, note
    else:
        transport, note = "", pm["reason"]
        available, reason = False, pm["reason"]
    return {
        "available": available,
        "transport": transport,
        "note": note,
        "reason": reason,
        "paramiko": pm["available"],
        "paramiko_reason": pm["reason"],
        "cli": cli["available"],
        "password": has_password,
    }


def ssh_info(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """给页面看的 SSH 状态，只查依赖和主机，不联网、不碰密码。"""
    state = paramiko_state()
    tp = transport_state(cfg)
    return {
        "available": tp["available"],
        "reason": state["reason"],
        "transport": tp["transport"],
        "transport_note": tp["note"],
        "paramiko": state["available"],
        "host": str(cfg.get("ssh_host") or "").strip(),
        "used": False,
        "note": "",
    }


def _ssh_port(cfg: Dict[str, Any]) -> int:
    try:
        port = int(str(cfg.get("ssh_port") or 22).strip())
    except (TypeError, ValueError):
        return 22
    return port if 0 < port < 65536 else 22


def _session_key(cfg: Dict[str, Any]) -> str:
    """配置变了就换连接；哈希里不保留任何明文凭据。"""
    parts = (
        str(cfg.get("ssh_host") or "").strip(),
        _ssh_port(cfg),
        str(cfg.get("ssh_user") or "").strip(),
        str(cfg.get("ssh_password") or ""),
        str(cfg.get("ssh_key") or ""),
        str(cfg.get("ssh_sudo") or ""),
    )
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def close_session() -> None:
    """配置变更、插件卸载或进程退出时关掉复用连接。"""
    with _SESSION_LOCK:
        client = _SESSION.get("client")
        _SESSION["client"] = None
        _SESSION["key"] = ""
        _SESSION["transport"] = ""
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _pkey_from_text(key_text: str) -> Any:
    """依次试三种常见私钥格式，失败就明确报错，不静默退化成无认证。"""
    import io
    import paramiko

    for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            return cls.from_private_key(io.StringIO(key_text))
        except Exception:
            continue
    return None


def _new_paramiko_client(cfg: Dict[str, Any]) -> Tuple[Any, str]:
    """建一条新连接；错误文本已经脱敏。"""
    try:
        import paramiko
    except Exception as exc:
        return None, _scrub(f"paramiko 加载失败：{type(exc).__name__} {exc}", cfg)
    client = paramiko.SSHClient()
    # 内网 NAS 的主机指纹常常重签，默认不校验；需要严格校验可在 NAS 上配 known_hosts
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: Dict[str, Any] = {
        "hostname": str(cfg.get("ssh_host") or "").strip(),
        "port": _ssh_port(cfg),
        "username": str(cfg.get("ssh_user") or "").strip(),
        "timeout": TIMEOUT,
        "banner_timeout": TIMEOUT,
        "auth_timeout": TIMEOUT,
        "look_for_keys": False,
        "allow_agent": False,
    }
    key_text = str(cfg.get("ssh_key") or "").strip()
    if key_text:
        pkey = _pkey_from_text(key_text)
        if pkey is None:
            return None, "SSH 私钥格式不对，支持 RSA / Ed25519 / ECDSA 的 PEM 文本"
        kwargs["pkey"] = pkey
    else:
        kwargs["password"] = str(cfg.get("ssh_password") or "")
    try:
        client.connect(**kwargs)
        return client, ""
    except Exception as exc:
        try:
            client.close()
        except Exception:
            pass
        return None, _scrub(f"{type(exc).__name__}: {exc}", cfg)


def _client(cfg: Dict[str, Any]) -> Tuple[Any, str]:
    """同一份配置复用连接；断了就重连一次。"""
    key = _session_key(cfg)
    with _SESSION_LOCK:
        client = _SESSION.get("client")
        if client is not None and _SESSION.get("key") == key:
            try:
                transport = client.get_transport()
                if transport is not None and transport.is_active():
                    return client, ""
            except Exception:
                pass
        close_session()
        client, error = _new_paramiko_client(cfg)
        if client is not None:
            _SESSION["client"] = client
            _SESSION["key"] = key
            _SESSION["transport"] = "paramiko"
        return client, error


def _key_file(cfg: Dict[str, Any]) -> Tuple[str, str]:
    """ssh 命令行只认文件，把私钥写成一个 0600 的临时文件。"""
    text = str(cfg.get("ssh_key") or "").strip()
    if not text:
        return "", "没有填 SSH 私钥"
    try:
        fd, path = tempfile.mkstemp(prefix="ugn_ssh_", suffix=".key")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text if text.endswith("\n") else text + "\n")
        os.chmod(path, 0o600)
        return path, ""
    except Exception as exc:
        return "", _scrub(f"私钥临时文件写不了：{type(exc).__name__} {exc}", cfg)


def _run_remote(cfg: Dict[str, Any], command: str,
                timeout: Optional[int] = None) -> Tuple[bool, str, str]:
    """在 NAS 上跑一条命令，返回 (是否成功, 标准输出, 脱敏后的错误)。"""
    host = str(cfg.get("ssh_host") or "").strip()
    if not host:
        return False, "", "还没填 SSH 地址"
    limit = timeout or TIMEOUT
    pm = paramiko_state()
    if pm["available"]:
        client, error = _client(cfg)
        if client is None:
            return False, "", error or "SSH 登录失败"
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=limit)
            out = stdout.read().decode("utf-8", "replace")[:_MAX_OUTPUT]
            err = stderr.read().decode("utf-8", "replace")
            code = stdout.channel.recv_exit_status()
            text = _scrub(err.strip(), cfg)
            if code not in (0, None):
                return False, out, text or f"远端命令退出码 {code}"
            return True, out, text
        except Exception as exc:
            close_session()
            return False, "", _scrub(f"{type(exc).__name__}: {exc}", cfg)

    cli = ssh_cli_state()
    if not cli["available"]:
        return False, "", pm["reason"]
    key_path, key_error = _key_file(cfg)
    if not key_path:
        return False, "", key_error or pm["reason"]
    args = [
        cli["path"], "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null", "-o", f"ConnectTimeout={min(limit, 8)}",
        "-o", "IdentitiesOnly=yes", "-i", key_path,
        "-p", str(_ssh_port(cfg)),
        f"{str(cfg.get('ssh_user') or '').strip()}@{host}",
        command,
    ]
    try:
        done = subprocess.run(args, capture_output=True, timeout=limit, check=False)
        out = done.stdout.decode("utf-8", "replace")[:_MAX_OUTPUT]
        err = _scrub(done.stderr.decode("utf-8", "replace").strip(), cfg)
        if done.returncode != 0:
            return False, out, err or f"ssh 退出码 {done.returncode}"
        return True, out, err
    except Exception as exc:
        return False, "", _scrub(f"{type(exc).__name__}: {exc}", cfg)
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            pass


def probe_script() -> str:
    """一次连接拿全部状态：df / 负载 / 内存 / 容器 / 盘温 / 健康 / UPS。

    每条命令都带兜底，某个工具不存在只会让对应段落空着，不会中断整个探针。
    """
    return r"""
echo '===UGN:meta==='
id -u 2>/dev/null || echo 0
uname -s 2>/dev/null || echo unknown
echo '===UGN:df==='
df -kP 2>/dev/null || true
echo '===UGN:cpu==='
cat /proc/loadavg 2>/dev/null || true
nproc 2>/dev/null || echo 1
echo '===UGN:mem==='
grep -E '^(MemTotal|MemAvailable|MemFree):' /proc/meminfo 2>/dev/null || true
echo '===UGN:docker==='
{ docker ps --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null || sudo -n docker ps --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null || true; } | head -60
echo '===UGN:temp==='
for f in /sys/class/hwmon/hwmon*/temp*_input; do [ -r "$f" ] || continue; printf '%s %s\n' "$f" "$(cat "$f" 2>/dev/null)"; done
for d in /dev/sd? /dev/sd?? /dev/nvme?n1; do [ -e "$d" ] || continue; smartctl -A "$d" 2>/dev/null | grep -i 'temperature' | head -2 | sed "s|^|$d |" || sudo -n smartctl -A "$d" 2>/dev/null | grep -i 'temperature' | head -2 | sed "s|^|$d |" || true; done
echo '===UGN:health==='
for d in /dev/sd? /dev/sd?? /dev/nvme?n1; do [ -e "$d" ] || continue; smartctl -H "$d" 2>/dev/null | grep -iE 'SMART overall-health|SMART Health Status' | head -1 | sed "s|^|$d |" || true; done
echo '===UGN:ups==='
for u in $(upsc -l 2>/dev/null | head -3); do upsc "$u" 2>/dev/null | grep -E '^(ups.status|battery.charge):' | sed "s|^|$u |"; done
apcaccess status 2>/dev/null | grep -iE '^(STATUS|BATTCHG)' | head -3 || true
echo '===UGN:end==='
"""


def _sections(text: Any) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    current = ""
    for line in str(text or "").splitlines():
        match = _SECTION_RE.match(line.strip())
        if match:
            current = match.group(1)
            out.setdefault(current, [])
        elif current and current != "end":
            out[current].append(line)
    return out


def _smart_temp(line: str) -> Any:
    """smartctl 的两种常见温度格式都认，读不出就返回未上报。"""
    for pattern in (r"Temperature:\s*(\d{1,3})", r"-\s+(\d{1,3})(?:\s|\(|$)",
                    r"(\d{1,3})\s*Celsius"):
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            value = int(match.group(1))
            if 0 < value < 130:
                return float(value)
    return MISSING


def parse_probe(text: Any, cfg: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把复合探针输出还原成统一 schema + 每段采集是否成功。"""
    sections = _sections(text)
    meta_lines = sections.get("meta") or []
    uid = MISSING
    if meta_lines and str(meta_lines[0]).strip().isdigit():
        uid = int(str(meta_lines[0]).strip())
    kernel = _as_text(meta_lines[1]) if len(meta_lines) > 1 else MISSING

    volumes = parse_df("\n".join(sections.get("df") or []))
    cpu: Any = MISSING
    mem: Any = MISSING
    cpu_lines = sections.get("cpu") or []
    if cpu_lines:
        try:
            load = float(str(cpu_lines[0]).split()[0])
            cores = int(str(cpu_lines[1]).split()[0]) if len(cpu_lines) > 1 else 1
            cores = max(1, cores)
            cpu = round(min(100.0, load / cores * 100.0), 1)
        except (ValueError, IndexError):
            pass
    mem_lines = sections.get("mem") or []
    meminfo: Dict[str, float] = {}
    for line in mem_lines:
        match = re.match(r"^(MemTotal|MemAvailable|MemFree):\s+(\d+)", line.strip())
        if match:
            meminfo[match.group(1)] = float(match.group(2))
    total = meminfo.get("MemTotal") or 0
    avail = meminfo.get("MemAvailable", meminfo.get("MemFree"))
    if total > 0 and avail is not None:
        mem = round(max(0.0, (total - avail) / total * 100.0), 1)

    containers: List[Dict[str, Any]] = []
    for line in sections.get("docker") or []:
        parts = line.strip().split("|", 2)
        if len(parts) < 2 or not parts[0]:
            continue
        state = parts[1].strip().lower()
        status_text = parts[2].strip() if len(parts) > 2 else ""
        # 老版本 docker 的 .State 可能为空，退回按 .Status 的 Up / Exited 判断
        if state not in ("running", "exited", "paused", "restarting", "created", "removing", "dead"):
            if state.startswith("up") or status_text.lower().startswith("up"):
                state = "running"
            elif state.startswith("exited") or status_text.lower().startswith("exited"):
                state = "exited"
            else:
                state = state or status_text.lower() or MISSING
        containers.append({"name": parts[0].strip(),
                           "status": state or MISSING,
                           "restarts": MISSING})

    disks: Dict[str, Dict[str, Any]] = {}
    for line in sections.get("temp") or []:
        tokens = line.strip().split()
        if len(tokens) < 2:
            continue
        if tokens[0].startswith("/sys/class/hwmon/"):
            try:
                number = float(tokens[-1])
            except ValueError:
                continue
            name = (os.path.basename(os.path.dirname(tokens[0])) + "/" +
                    os.path.basename(tokens[0]).replace("_input", ""))
            temp = number / 1000.0 if number > 1000 else number
        elif tokens[0].startswith("/dev/"):
            name = os.path.basename(tokens[0])
            temp = _smart_temp(line)
        else:
            continue
        if not _is_num(temp) or not (0 < float(temp) < 130):
            continue
        entry = disks.setdefault(name, {"name": name, "temp": MISSING,
                                        "health": MISSING, "smart_alert": MISSING})
        if not _is_num(entry["temp"]) or float(temp) > float(entry["temp"]):
            entry["temp"] = float(temp)
    for line in sections.get("health") or []:
        tokens = line.strip().split()
        if len(tokens) < 2 or not tokens[0].startswith("/dev/"):
            continue
        name = os.path.basename(tokens[0])
        upper = line.upper()
        entry = disks.setdefault(name, {"name": name, "temp": MISSING,
                                        "health": MISSING, "smart_alert": MISSING})
        if "FAILED" in upper or "FAIL" in upper:
            entry["smart_alert"] = True
            entry["health"] = "异常"
        elif "PASSED" in upper or " OK" in upper:
            entry["smart_alert"] = False
            entry["health"] = "正常"

    on_battery: Any = MISSING
    battery: Any = MISSING
    for line in sections.get("ups") or []:
        upper = line.upper()
        if "UPS.STATUS:" in upper or "STATUS" in upper:
            if "OB" in upper or "ONBATT" in upper or "DISCHRG" in upper:
                on_battery = True
            elif "OL" in upper or "ONLINE" in upper:
                on_battery = False
        match = re.search(r"(?:battery\.charge|BATTCHG)\s*:?\s*(\d{1,3}(?:\.\d+)?)", line, re.IGNORECASE)
        if match:
            battery = float(match.group(1))

    reported = {
        "volumes": bool(volumes),
        "cpu": _is_num(cpu),
        "mem": _is_num(mem),
        "containers": bool(containers),
        "disks": bool(disks),
        "ups": isinstance(on_battery, bool) or _is_num(battery),
        "uid": uid,
        "kernel": kernel,
        "transport": str((cfg or {}).get("_transport") or ""),
    }
    raw = {
        "ts": _now_text(),
        "volumes": volumes,
        "disks": list(disks.values()),
        "containers": containers,
        "ups": {"on_battery": on_battery, "battery": battery},
        "system": {"cpu": cpu, "mem": mem},
        "ugn_probe": reported,
    }
    return raw, reported


def _read_ssh(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """SSH 跑一次复合探针；连不上才返回 None 让 collect 换下一路。"""
    host = str(cfg.get("ssh_host") or "").strip()
    if not host:
        return None
    tp = transport_state(cfg)
    if not tp["available"]:
        _LAST_SSH_ERROR["text"] = tp["reason"]
        return None
    ok, out, error = _run_remote(cfg, probe_script(), timeout=_PROBE_TIMEOUT)
    if not ok:
        _LAST_SSH_ERROR["text"] = error or "SSH 探针没跑通"
        return None
    probe_cfg = dict(cfg)
    probe_cfg["_transport"] = tp["transport"]
    raw, _meta = parse_probe(out, probe_cfg)
    _LAST_SSH_ERROR["text"] = ""
    return raw


def ssh_diagnostics(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """「测试连接」用的分步报告：地址 → 端口 → 依赖 → 登录 → 探针 → 各段数据。"""
    host = str(cfg.get("ssh_host") or "").strip()
    port = _ssh_port(cfg)
    user = str(cfg.get("ssh_user") or "").strip()
    tp = transport_state(cfg)
    report: Dict[str, Any] = {
        "ok": False, "checked_at": _now_text(), "host": host, "port": port,
        "user": user, "transport": tp["transport"], "steps": [],
        "sections": {}, "summary": "", "hint": "",
    }

    def step(name: str, ok: bool, detail: str) -> None:
        report["steps"].append({"name": name, "ok": bool(ok),
                                "detail": _scrub(detail, cfg)})

    step("地址", bool(host), host or "请先填 SSH 地址")
    if not host:
        report["summary"] = "没填 SSH 地址"
        report["hint"] = "在设置里填 NAS 的 IP 或内网域名，再点一次测试连接"
        return report
    try:
        sock = socket.create_connection((host, port), timeout=4)
        try:
            sock.close()
        except Exception:
            pass
        step("端口", True, f"{host}:{port} 能连上")
    except Exception as exc:
        step("端口", False, f"{host}:{port} 连不上：{type(exc).__name__} {exc}")
        report["summary"] = "SSH 端口连不上"
        report["hint"] = "确认 NAS 已开启 SSH、端口没填错，MP 容器能访问到这个地址"
        return report
    step("传输", tp["available"], tp["transport"] or tp["reason"])
    if not tp["available"]:
        report["summary"] = "缺少可用的 SSH 传输"
        report["hint"] = tp["reason"] or _PARAMIKO_REASON
        return report
    ok, out, error = _run_remote(cfg, "echo UGN_OK", timeout=TIMEOUT)
    step("登录", ok and "UGN_OK" in out,
         "账号认证通过" if ok and "UGN_OK" in out else (error or "账号或密码不对"))
    if not (ok and "UGN_OK" in out):
        report["summary"] = "SSH 登录没通过"
        report["hint"] = "检查账号、密码或私钥；绿联 NAS 需要在控制面板里开启 SSH 服务"
        return report
    ok, out, error = _run_remote(cfg, probe_script(), timeout=_PROBE_TIMEOUT)
    if not ok:
        step("探针", False, error or "远端命令执行失败")
        report["summary"] = "登录成功，但状态探针没跑通"
        report["hint"] = "看上面探针的报错，通常是 shell 或权限限制"
        return report
    _raw, reported = parse_probe(out, cfg)
    report["sections"] = reported
    got = [name for name in ("volumes", "cpu", "mem", "containers", "disks", "ups") if reported.get(name)]
    step("探针", True, "读到：" + ("、".join(got) if got else "没有可用段落"))
    report["ok"] = True
    report["summary"] = f"连接正常（{tp['transport']}），读到 {len(got)}/6 段状态"
    miss = [name for name in ("volumes", "cpu", "mem", "containers", "disks", "ups") if not reported.get(name)]
    report["hint"] = ("没读到的段落会在页面上标未上报；盘温和 UPS 通常需要 smartctl / NUT 或 root 权限"
                      if miss else "各段状态都读到了")
    return report


def validate_command(command: Any) -> Tuple[bool, str]:
    """远程动作的安全门禁：只放行一条简单命令，默认拒绝一切危险片段。"""
    text = str(command or "").strip()
    if not text:
        return False, "命令为空"
    if len(text) > 300:
        return False, "命令超过 300 字，太长了"
    check = text.lower()
    if check.startswith("sudo -n "):
        check = check[len("sudo -n "):]
    elif "sudo " in check or "su " in check:
        return False, "不允许提权命令"
    for pattern in _DANGEROUS_WORDS:
        match = re.search(pattern, check, re.IGNORECASE)
        if match:
            return False, f"含受限片段「{match.group(0).strip()}」"
    for bad in _DANGEROUS_CHARS:
        if bad in check:
            return False, f"含受限片段「{bad.strip() or '换行'}」"
    if not _SAFE_CMD_RE.fullmatch(text):
        return False, "只允许单条简单命令，不要带特殊符号"
    return True, ""


def restart_command(cfg: Dict[str, Any], name: Any) -> str:
    """容器名严格白名单化后再拼命令，杜绝拼接注入。"""
    text = str(name or "").strip()
    if not _CONTAINER_NAME_RE.fullmatch(text):
        return ""
    prefix = "sudo -n " if _to_bool_like(cfg.get("ssh_sudo")) else ""
    return f"{prefix}docker restart {text}"


def _to_bool_like(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("true", "1", "yes", "on", "是")


def run_remote_action(cfg: Dict[str, Any], command: Any,
                      timeout: int = _ACTION_TIMEOUT) -> Dict[str, Any]:
    """给自动处置用：校验 → 执行 → 截断输出 → 脱敏。"""
    valid, reason = validate_command(command)
    if not valid:
        return {"ok": False, "output": "", "error": reason, "command": str(command or "")}
    ok, out, error = _run_remote(cfg, str(command).strip(), timeout=timeout)
    return {
        "ok": ok,
        "output": _scrub(out.strip()[:300], cfg),
        "error": _scrub(error, cfg),
        "command": str(command).strip(),
    }


def _fmt_num(value: Any) -> str:
    return f"{value:.1f}" if isinstance(value, float) else str(value)


def _label(value: Any) -> str:
    return str(value) if _is_text(value) else MISSING


def _threshold(cfg: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(cfg.get(key))
    except (TypeError, ValueError):
        return default


def _watch_list(cfg: Dict[str, Any]) -> List[str]:
    raw = cfg.get("watch_containers")
    if raw is None:
        raw = DEFAULT_CONTAINERS
    if isinstance(raw, str):
        items = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        items = list(DEFAULT_CONTAINERS)
    return [x.lower() for x in items]


def _has_data(status: Dict[str, Any]) -> bool:
    for vol in status.get("volumes") or []:
        if isinstance(vol, dict) and any(_is_num(vol.get(k)) for k in ("total", "used", "percent")):
            return True
    for disk in status.get("disks") or []:
        if isinstance(disk, dict) and (_is_num(disk.get("temp"))
                                       or isinstance(disk.get("smart_alert"), bool)
                                       or _is_text(disk.get("health"))):
            return True
    for container in status.get("containers") or []:
        if isinstance(container, dict) and (_is_text(container.get("status"))
                                            or _is_num(container.get("restarts"))):
            return True
    ups = status.get("ups") or {}
    if isinstance(ups.get("on_battery"), bool) or _is_num(ups.get("battery")):
        return True
    system = status.get("system") or {}
    return _is_num(system.get("cpu")) or _is_num(system.get("mem"))


def _fully_reported(status: Dict[str, Any]) -> bool:
    """四个会触发暂停的段都报齐了，才敢说全部正常、去恢复下载。"""
    volumes = [v for v in (status.get("volumes") or []) if isinstance(v, dict)]
    if not any(_is_num(v.get("percent")) for v in volumes):
        return False
    disks = [d for d in (status.get("disks") or []) if isinstance(d, dict)]
    if not any(_is_num(d.get("temp")) or isinstance(d.get("smart_alert"), bool)
               or _is_text(d.get("health")) for d in disks):
        return False
    containers = [c for c in (status.get("containers") or []) if isinstance(c, dict)]
    if not any(_is_text(c.get("status")) for c in containers):
        return False
    ups = status.get("ups") or {}
    return isinstance(ups.get("on_battery"), bool) or _is_num(ups.get("battery"))


def _missing_segments(status: Dict[str, Any]) -> List[str]:
    """列出还没上报的段落，给「按已上报项恢复」的文案用。"""
    labels: List[str] = []
    volumes = [v for v in (status.get("volumes") or []) if isinstance(v, dict)]
    if not any(_is_num(v.get("percent")) for v in volumes):
        labels.append("存储卷")
    disks = [d for d in (status.get("disks") or []) if isinstance(d, dict)]
    if not any(_is_num(d.get("temp")) or isinstance(d.get("smart_alert"), bool)
               or _is_text(d.get("health")) for d in disks):
        labels.append("磁盘")
    containers = [c for c in (status.get("containers") or []) if isinstance(c, dict)]
    if not any(_is_text(c.get("status")) for c in containers):
        labels.append("容器")
    ups = status.get("ups") or {}
    if not (isinstance(ups.get("on_battery"), bool) or _is_num(ups.get("battery"))):
        labels.append("UPS")
    return labels


def evaluate(status: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """看一眼状态，返回命中的规则；没数据时只报未上报，不装作一切正常。"""
    water = _threshold(cfg, "water_threshold", 88)
    temp_limit = _threshold(cfg, "temp_threshold", 52)
    cpu_limit = _threshold(cfg, "cpu_threshold", 90)
    mem_limit = _threshold(cfg, "mem_threshold", 95)
    resume_scope = str(cfg.get("resume_scope") or "严格").strip()
    watched = _watch_list(cfg)
    hits: List[Dict[str, Any]] = []

    worst = None
    for vol in status.get("volumes") or []:
        if not isinstance(vol, dict):
            continue
        percent = vol.get("percent")
        if _is_num(percent) and (worst is None or float(percent) > float(worst["percent"])):
            worst = vol
    if worst and float(worst["percent"]) >= water:
        hits.append({
            "rule": "volume_high",
            "level": "danger",
            "detail": f"磁盘占用空间达到 {_fmt_num(worst.get('percent'))}%（卷 {_label(worst.get('name'))}），超过 {int(water)}%",
            "action": "pause_download",
        })

    risky = []
    for disk in status.get("disks") or []:
        if not isinstance(disk, dict):
            continue
        temp = disk.get("temp")
        hot = _is_num(temp) and float(temp) >= temp_limit
        if hot or disk.get("smart_alert") is True:
            risky.append((disk, hot))
    if risky:
        parts = []
        for disk, hot in risky[:3]:
            seg = _label(disk.get("name"))
            if hot:
                seg += f" {_fmt_num(disk.get('temp'))}℃（阈值 {int(temp_limit)}℃）"
            if disk.get("smart_alert") is True:
                seg += " SMART 告警"
            parts.append(seg)
        more = f" 等共 {len(risky)} 块" if len(risky) > 3 else ""
        hits.append({
            "rule": "disk_risk",
            "level": "danger",
            "detail": "磁盘异常：" + "；".join(parts) + more,
            "action": "pause_download",
        })

    down: List[str] = []
    targets: List[str] = []
    for container in status.get("containers") or []:
        if not isinstance(container, dict):
            continue
        name = str(container.get("name") or "")
        if not any(word in name.lower() for word in watched):
            continue
        state = container.get("status")
        if _is_text(state) and str(state).strip().lower() != "running":
            down.append(f"{name}（{state}）")
            targets.append(name)
    if down:
        more = f" 等 {len(down)} 个" if len(down) > 4 else ""
        hits.append({
            "rule": "container_down",
            "level": "warn",
            "detail": "关注容器没在运行：" + "、".join(down[:4]) + more,
            "action": "notify",
            "targets": targets,
        })

    ups = status.get("ups") or {}
    if ups.get("on_battery") is True:
        battery = ups.get("battery")
        detail = "UPS 已切到电池供电"
        if _is_num(battery):
            detail += f"，剩余 {_fmt_num(battery)}%"
        hits.append({
            "rule": "ups_battery",
            "level": "danger",
            "detail": detail,
            "action": "pause_download",
        })

    system = status.get("system") if isinstance(status.get("system"), dict) else {}
    for key, label, limit, rule in (
        ("cpu", "CPU 负载", cpu_limit, "cpu_high"),
        ("mem", "内存占用", mem_limit, "mem_high"),
    ):
        value = system.get(key)
        if _is_num(value) and float(value) >= limit:
            hits.append({
                "rule": rule,
                "level": "warn",
                "detail": f"{label}达到 {_fmt_num(value)}%（阈值 {int(limit)}%）",
                "action": "notify",
            })

    if not hits:
        missing = _missing_segments(status)
        if _fully_reported(status):
            hits.append({
                "rule": "resume",
                "level": "ok",
                "detail": "各项状态都在正常范围",
                "action": "resume_download",
            })
        elif resume_scope == "已上报" and "存储卷" not in missing and _has_data(status):
            hits.append({
                "rule": "resume",
                "level": "ok",
                "detail": "已上报的各项都在正常范围；" + "、".join(missing) + " 未上报，不参与判断",
                "action": "resume_download",
            })
        else:
            detail = ("有些状态还没上报，先不判断，也不会自动恢复下载"
                      if _has_data(status) else "状态还没上报，暂不判断，也不会自动恢复下载")
            hits.append({
                "rule": "no_data",
                "level": "info",
                "detail": detail,
                "action": "none",
            })
    return hits


def _read_bridge(url: str) -> Any:
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = resp.read().decode("utf-8", "replace")
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_status_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _candidate_paths() -> List[str]:
    paths: List[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            if value.startswith("/") and len(value) < 300:
                paths.append(value)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)

    try:
        from app.sdk.config import settings
        for name in ("DOWNLOAD_PATH", "LIBRARY_PATHS", "MEDIA_PATH"):
            collect(getattr(settings, name, None))
    except Exception:
        pass
    if not paths:
        paths = ["/"]

    result: List[str] = []
    seen = set()
    for path in paths:
        try:
            real = os.path.realpath(path)
            if real in seen or not os.path.isdir(real):
                continue
            seen.add(real)
            result.append(real)
        except OSError:
            continue
    return result or ["/"]


def _local_system() -> Dict[str, Any]:
    cpu: Any = MISSING
    mem: Any = MISSING
    try:
        load = os.getloadavg()[0]
        cores = os.cpu_count() or 1
        cpu = round(min(100.0, load / cores * 100.0), 1)
    except (OSError, AttributeError):
        pass
    try:
        total = available = None
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total = float(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = float(line.split()[1]) * 1024
                if total and available:
                    break
        if total:
            mem = round((total - (available or 0)) / total * 100.0, 1)
    except OSError:
        pass
    return {"cpu": cpu, "mem": mem}


def local_raw() -> Dict[str, Any]:
    """桥和状态文件都拿不到时，用容器内看得到的目录算一份磁盘占用空间。"""
    volumes: List[Dict[str, Any]] = []
    seen = set()
    for path in _candidate_paths():
        try:
            usage = shutil.disk_usage(path)
            stat = os.stat(path)
        except OSError:
            continue
        if stat.st_dev in seen or usage.total <= 0:
            continue
        seen.add(stat.st_dev)
        volumes.append({
            "name": os.path.basename(path.rstrip("/")) or "/",
            "total": usage.total,
            "used": usage.used,
            "percent": round(usage.used / usage.total * 100.0, 1),
        })
    return {"ts": _now_text(), "volumes": volumes, "system": _local_system()}


def service_counts() -> Dict[str, Any]:
    out: Dict[str, Any] = {"media": MISSING, "download": MISSING}
    try:
        from app.sdk.services import MediaServerHelper, DownloaderHelper

        def count(helper: Any) -> int:
            configs = helper().get_configs() or {}
            if isinstance(configs, dict):
                return len([v for v in configs.values() if v])
            if isinstance(configs, (list, tuple)):
                return len(configs)
            return 0

        out["media"] = count(MediaServerHelper)
        out["download"] = count(DownloaderHelper)
    except Exception:
        pass
    return out


def collect(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """按 source_mode 选路，自动是 SSH → 状态桥 → 本地文件 → 容器自检，取到哪层算哪层。"""
    mode = str(cfg.get("source_mode") or "自动").strip()
    if mode not in ("自动", "状态桥", "本地文件", "SSH", "容器自检"):
        mode = "自动"

    ssh = ssh_info(cfg)
    bridge = str(cfg.get("bridge_url") or "").strip()
    status_file = str(cfg.get("status_file") or "").strip()

    order = {
        "自动": ("ssh", "bridge", "file", "local"),
        "SSH": ("ssh", "local"),
        "状态桥": ("bridge", "local"),
        "本地文件": ("file", "local"),
        "容器自检": ("local",),
    }[mode]

    raw: Any = None
    probe: Any = None
    source = MISSING
    services = None
    for step in order:
        if step == "ssh":
            if not ssh["host"]:
                continue
            if not ssh["available"]:
                ssh["note"] = ssh["reason"] or ssh.get("transport_note") or ""
                continue
            raw = _read_ssh(cfg)
            if raw is not None:
                source = "SSH"
                ssh["used"] = True
                ssh["transport"] = str(ssh.get("transport") or "")
                probe = raw.get("ugn_probe") if isinstance(raw, dict) else None
                break
            ssh["note"] = _LAST_SSH_ERROR.get("text") or "SSH 探针没跑通，已降级"
        elif step == "bridge":
            if not bridge:
                continue
            raw = _read_bridge(bridge)
            if raw is not None:
                source = "状态桥"
                break
        elif step == "file":
            if not status_file:
                continue
            raw = _read_status_file(status_file)
            if raw is not None:
                source = "本地文件"
                break
        elif step == "local":
            raw = local_raw()
            source = "容器自检"
            services = service_counts()
            break

    if raw is None:
        raw = {}
        source = MISSING
    return {
        "status": parse_status(raw),
        "source": source,
        "services": services,
        "ssh": ssh,
        "probe": probe,
        "source_mode": mode,
    }
