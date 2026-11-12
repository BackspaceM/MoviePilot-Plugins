"""绿联 NAS 守护的状态采集、解析与规则判断，尽量只依赖标准库。"""

import datetime as _dt
import json
import os
import shutil
import urllib.request
from typing import Any, Dict, List, Optional

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


def paramiko_state() -> Dict[str, Any]:
    """只查依赖在不在，延迟导入，缺了也不抛，交给 collect 降级。"""
    try:
        import paramiko  # noqa: F401
        return {"available": True, "reason": ""}
    except Exception:
        return {"available": False, "reason": _PARAMIKO_REASON}


def ssh_info(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """给页面看的 SSH 状态，只查依赖和主机，不联网、不碰密码。"""
    state = paramiko_state()
    return {
        "available": state["available"],
        "reason": state["reason"],
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


def _read_ssh(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """SSH 跑 df -kP，只上报存储卷；连不上就返回 None 让 collect 换下一路。"""
    host = str(cfg.get("ssh_host") or "").strip()
    if not host:
        return None
    try:
        import paramiko  # 延迟导入，别让宿主因为缺依赖回滚插件安装
    except Exception:
        return None
    client = None
    try:
        client = paramiko.SSHClient()
        # 不校验主机指纹方便内网 NAS，代价是中间人风险，自己掂量
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            host,
            port=_ssh_port(cfg),
            username=str(cfg.get("ssh_user") or "").strip(),
            password=str(cfg.get("ssh_password") or ""),
            timeout=TIMEOUT,
        )
        _stdin, stdout, _stderr = client.exec_command("df -kP", timeout=TIMEOUT)
        volumes = parse_df(stdout.read().decode("utf-8", "replace"))
        if not volumes:
            return None
        # 只给 volumes，盘温、容器、UPS 一律不塞，页面会标未上报
        return {"ts": _now_text(), "volumes": volumes}
    except Exception:
        # 异常全吞，绝不把可能带密码的报错抛出去或写进日志
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


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


def evaluate(status: Dict[str, Any], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """看一眼状态，返回命中的规则；没数据时只报未上报，不装作一切正常。"""
    water = _threshold(cfg, "water_threshold", 88)
    temp_limit = _threshold(cfg, "temp_threshold", 52)
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

    down = []
    for container in status.get("containers") or []:
        if not isinstance(container, dict):
            continue
        name = str(container.get("name") or "")
        if not any(word in name.lower() for word in watched):
            continue
        state = container.get("status")
        if _is_text(state) and str(state).strip().lower() != "running":
            down.append(f"{name}（{state}）")
    if down:
        more = f" 等 {len(down)} 个" if len(down) > 4 else ""
        hits.append({
            "rule": "container_down",
            "level": "warn",
            "detail": "关注容器没在运行：" + "、".join(down[:4]) + more,
            "action": "notify",
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

    if not hits:
        if _fully_reported(status):
            hits.append({
                "rule": "resume",
                "level": "ok",
                "detail": "各项状态都在正常范围",
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
    source = MISSING
    services = None
    for step in order:
        if step == "ssh":
            if not ssh["host"]:
                continue
            if not ssh["available"]:
                ssh["note"] = ssh["reason"]
                continue
            raw = _read_ssh(cfg)
            if raw is not None:
                source = "SSH"
                ssh["used"] = True
                break
            ssh["note"] = "SSH 没读到 df -kP，已降级"
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
        "source_mode": mode,
    }
