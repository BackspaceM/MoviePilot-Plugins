"""RouterOS 场景模板一键部署 —— 预览比对、批量下发、备份与失败回滚。

安全约定（与仓库其它 RouterOS 插件一致）：
1. 只新增带 ``MP-A2[...]`` 标签的条目，绝不修改/删除用户已有配置；
2. 部署前在 RouterOS 本机留 .backup，并把涉及分区的现状导出到 MP 插件数据目录；
3. 下发失败按「逆序删除本次新建条目」自动回滚，绝不整表覆盖；
4. 同一标签已存在则跳过（幂等），重复点部署不会堆积重复规则；
5. 防火墙类动作按 place-before 插到兜底 drop 之前，避免变成永不匹配的死规则。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .templates import (PLACE_APPEND, PLACE_BEFORE_DROP, PLACE_TOP,
                        DeployAction, actions_from_plan, now_text)

LogFunc = Optional[Callable[[str, str], None]]

# 会让 drop 规则「收窄」的匹配字段：出现任意一个就不算兜底 drop
_NARROW_KEYS = {
    "src-address", "dst-address", "src-address-list", "dst-address-list",
    "src-mac-address", "protocol", "port", "src-port", "dst-port",
    "in-interface", "out-interface", "connection-state", "connection-nat-state",
    "icmp-options", "tcp-flags", "limit", "content", "layer7-protocol",
    "ttl", "dscp", "packet-size", "p2p", "hotspot", "time", "distance",
    "address-list", "address-list-timeout", "jump-target", "to-addresses",
    "to-ports", "src-address-type", "dst-address-type", "connection-mark",
    "packet-mark", "routing-mark", "new-packet-mark", "new-connection-mark",
}
_IGNORE_KEYS = {
    ".id", "id", "action", "comment", "bytes", "packets", "disabled",
    "dynamic", "invalid", "log", "log-prefix", "place-before", "nextid",
}


def _truthy(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in ("true", "yes", "1", "on")


def is_broad_drop(row: Dict[str, Any]) -> bool:
    """判断是否为「兜底拒绝」：drop/reject 且无收窄条件（!LAN 之类排除型不算收窄）。"""
    if str(row.get("action") or "").lower() not in ("drop", "reject"):
        return False
    if _truthy(row.get("disabled")) or _truthy(row.get("dynamic")):
        return False
    for key, value in dict(row or {}).items():
        kl = str(key).lower()
        if kl in _IGNORE_KEYS or kl.startswith("."):
            continue
        text = str(value if value is not None else "").strip()
        if not text:
            continue
        if kl in ("in-interface-list", "out-interface-list") and text.startswith("!"):
            continue
        if kl in _NARROW_KEYS:
            return False
    return True


def _row_id(row: Dict[str, Any]) -> str:
    return str(row.get(".id") or row.get("id") or "")


def find_tagged_id(rows: Sequence[Dict[str, Any]], tag: str) -> str:
    """按备注标签找已存在条目 id（幂等检查 / 回滚降级定位）。"""
    if not tag:
        return ""
    for row in rows or []:
        if tag in str(row.get("comment") or ""):
            rid = _row_id(row)
            if rid:
                return rid
    return ""


def choose_place_before(action: DeployAction,
                        rows: Optional[Sequence[Dict[str, Any]]]) -> Tuple[str, str]:
    """按动作策略计算 place-before 目标（纯函数，便于单测）。

    返回 (place_before, 说明)。空串表示追加到分区末尾。
    """
    if action.placement == PLACE_APPEND or not action.orderable:
        return "", "追加到分区末尾"
    if not rows:
        return "", "分区为空或不可读，直接追加"
    chain = str(action.chain or action.payload.get("chain") or "")
    seq = [r for r in rows if str(r.get("chain") or "") == chain and not _truthy(r.get("dynamic"))]
    if not seq:
        return "", f"{chain} 链暂无规则，直接追加"

    if action.placement == PLACE_TOP:
        first = _row_id(seq[0])
        return (first, f"插到 {chain} 链首（{first} 之前）") if first else ("", "链首无可用 id，追加")

    if action.placement == PLACE_BEFORE_DROP:
        for row in seq:
            if is_broad_drop(row):
                rid = _row_id(row)
                if rid:
                    return rid, f"插到 {chain} 链兜底拒绝规则 {rid} 之前"
        return "", f"{chain} 链未发现兜底 drop，追加到末尾"

    return "", "未知插入策略，按追加处理"


async def refresh_plan(client: Any, http: Any, plan: Dict[str, Any],
                       log: LogFunc = None) -> Dict[str, Any]:
    """联网比对现状：把重复标签标成 exists（部署时跳过），并记录分区可读性。"""
    actions = actions_from_plan(plan)
    cache: Dict[str, Optional[List[Dict[str, Any]]]] = {}
    unreadable: List[str] = []
    exists_count = 0

    for action in actions:
        if action.section not in cache:
            try:
                cache[action.section] = await client.list_section(action.section, client=http)
            except Exception as err:  # noqa: BLE001
                cache[action.section] = None
                if log:
                    log("WARN", f"读取 {action.section_label} 失败：{err}")
        rows = cache[action.section]
        if rows is None:
            if action.section not in unreadable:
                unreadable.append(action.section)
            action.state = "pending"
            action.detail = "分区现状读取失败（权限或连接问题），部署时按新增处理"
            continue
        exists_id = find_tagged_id(rows, action.tag)
        if exists_id:
            action.state = "exists"
            action.detail = f"已存在（id {exists_id}），部署时将跳过，不重复创建"
            exists_count += 1
        else:
            action.state = "pending"
            action.detail = "将新增"

    out = dict(plan or {})
    out["actions"] = [a.to_dict() for a in actions]
    out["refreshed"] = now_text()
    out["unreadable"] = unreadable
    out["summary"] = dict(out.get("summary") or {})
    out["summary"]["exists"] = exists_count
    out["summary"]["to_write"] = len(actions) - exists_count
    if log:
        log("INFO", f"预览刷新：共 {len(actions)} 条，已存在 {exists_count} 条，"
                    f"待写入 {len(actions) - exists_count} 条；不可读分区 {len(unreadable)} 个")
    return out


async def rollback_entries(client: Any, http: Any,
                           created: Sequence[Dict[str, Any]],
                           log: LogFunc = None) -> Dict[str, Any]:
    """逆序删除本次新建条目（失败自动回滚与详情页手动回滚共用）。"""
    ok_count = 0
    errors: List[str] = []
    items = list(created or [])
    for item in reversed(items):
        section = str(item.get("section") or "")
        rid = str(item.get("id") or "")
        label = f"#{item.get('index', '?')} {section}/{rid or '未知id'}"
        if not rid:
            errors.append(f"{label} 缺少 id，无法自动回滚")
            continue
        ok, msg = await client.delete(section, rid, client=http)
        if ok:
            ok_count += 1
        else:
            errors.append(f"{label}：{msg}")
        if log:
            log("INFO" if ok else "WARN", f"【回滚】{msg}")
    return {
        "done": True, "total": len(items), "ok_count": ok_count,
        "fail_count": len(errors), "errors": errors[:20],
        "message": f"逆序回滚完成：成功 {ok_count} / {len(items)} 条",
    }


async def deploy_plan(client: Any, http: Any, plan: Dict[str, Any],
                      options: Optional[Dict[str, Any]] = None,
                      log: LogFunc = None,
                      snapshot_saver: Optional[Callable[[Dict[str, Any]], str]] = None) -> Dict[str, Any]:
    """执行一份已确认的计划：备份 → 快照 → 逐条下发 → 失败逆序回滚。

    options: auto_backup / require_backup / auto_rollback / dry_run / backup_name
    """
    opts = dict(options or {})
    dry_run = bool(opts.get("dry_run"))
    auto_backup = bool(opts.get("auto_backup", True))
    require_backup = bool(opts.get("require_backup", True))
    auto_rollback = bool(opts.get("auto_rollback", True))
    actions = actions_from_plan(plan)
    sections: List[str] = [str(s) for s in (plan.get("sections") or [])]
    if not sections:
        for a in actions:
            if a.section not in sections:
                sections.append(a.section)

    started = now_text()
    report: Dict[str, Any] = {
        "ok": False, "message": "", "started": started,
        "plan_created": str(plan.get("created") or ""),
        "total": len(actions), "written": 0, "skipped": 0,
        "failed": 0, "errors": [], "details": [],
        "created": [], "backup": {"ok": False, "message": "未执行"},
        "snapshot": {"path": "", "count": 0, "failed": []},
        "rollback": {"done": False, "ok_count": 0, "errors": []},
        "dry_run": dry_run, "conn": "",
    }
    if plan.get("blocked"):
        report["message"] = "计划存在校验错误，已拒绝部署：" + "；".join(
            str(e) for e in (plan.get("errors") or [])[:3])
        return report
    if not actions:
        report["message"] = "没有可部署的动作，请先在详情页点「生成/刷新预览」"
        return report

    conn_ok, conn_msg = await client.check(client=http)
    report["conn"] = conn_msg
    if log:
        log("INFO" if conn_ok else "WARN", f"连接检查：{conn_msg}")
    if not conn_ok:
        report["message"] = conn_msg
        return report

    # 1) RouterOS 本机系统备份
    if auto_backup and not dry_run:
        name = str(opts.get("backup_name") or
                   ("mp-a2-" + time.strftime("%Y%m%d-%H%M%S")))
        bok, bmsg = await client.save_backup(name, client=http)
        report["backup"] = {"ok": bok, "message": bmsg}
        if log:
            log("INFO" if bok else "WARN", f"【部署前备份】{bmsg}")
        if not bok and require_backup:
            report["message"] = "改动前备份失败，已中止部署（可在设置页关闭「备份失败必须中止」）：" + bmsg
            return report
    elif dry_run:
        report["backup"] = {"ok": True, "message": "试运行，未创建备份"}

    # 2) MP 侧结构化快照（人工对照用，回滚不依赖它）
    try:
        snap = await client.export_snapshot(sections, client=http)
        report["snapshot"] = {
            "path": "", "count": int(snap.get("count") or 0),
            "failed": list(snap.get("failed") or []),
        }
        if snapshot_saver:
            try:
                report["snapshot"]["path"] = str(snapshot_saver(snap) or "")
            except Exception as err:  # noqa: BLE001
                if log:
                    log("WARN", f"快照写盘失败（不影响部署）：{err}")
    except Exception as err:  # noqa: BLE001
        if log:
            log("WARN", f"快照导出失败（不影响部署）：{err}")

    # 3) 逐条下发
    cache: Dict[str, Optional[List[Dict[str, Any]]]] = {}
    created: List[Dict[str, Any]] = []
    for action in actions:
        if action.section not in cache:
            try:
                cache[action.section] = await client.list_section(action.section, client=http)
            except Exception:  # noqa: BLE001
                cache[action.section] = None
        rows = cache[action.section]

        if rows is not None and find_tagged_id(rows, action.tag):
            action.state = "skipped"
            action.detail = "标签已存在，跳过（幂等）"
            report["skipped"] += 1
            report["details"].append({"index": action.index, "state": "skipped",
                                      "message": action.detail, "tag": action.tag})
            if log:
                log("INFO", f"#{action.index} 跳过：{action.tag} 已存在")
            continue

        place, place_note = choose_place_before(action, rows)
        ok, msg, created_id = await client.create(action.section, action.payload,
                                                  place_before=place, client=http)
        if not created_id and ok and not dry_run and rows is not None:
            # 部分分区 PUT 不返回 id：重读分区按标签定位，保证可回滚
            try:
                fresh = await client.list_section(action.section, client=http)
                if fresh is not None:
                    cache[action.section] = fresh
                    created_id = find_tagged_id(fresh, action.tag)
            except Exception:  # noqa: BLE001
                pass
        if ok:
            report["written"] += 1
            action.state = "done"
            action.detail = msg + (f"；{place_note}" if place else "")
            if not dry_run:
                entry = {"index": action.index, "section": action.section,
                         "id": created_id, "tag": action.tag}
                created.append(entry)
                if created_id and rows is not None:
                    synthetic = dict(action.payload)
                    synthetic[".id"] = created_id
                    rows.append(synthetic)
        else:
            report["failed"] += 1
            action.state = "failed"
            action.detail = msg
            report["errors"].append(f"#{action.index} {action.tag}：{msg}")
        report["details"].append({"index": action.index, "state": action.state,
                                  "message": action.detail, "tag": action.tag,
                                  "command": action.command})
        if log:
            log("INFO" if ok else "WARN", f"#{action.index} {msg}｜{place_note}")
        if not ok and auto_rollback and not dry_run:
            break  # 遇错即停，随后逆序回滚本次新建条目

    # 4) 失败自动回滚（只删本次新建）
    if report["failed"] and auto_rollback and not dry_run and created:
        rb = await rollback_entries(client, http, created, log=log)
        report["rollback"] = {"done": True, "ok_count": rb["ok_count"],
                              "fail_count": rb["fail_count"], "errors": rb["errors"]}
        report["created"] = [] if rb["fail_count"] == 0 else created
        report["message"] = (f"部署失败并已自动回滚 {rb['ok_count']} 条："
                             + "；".join(report["errors"][:3]))
    else:
        report["created"] = created
        report["ok"] = report["failed"] == 0
        suffix = "（试运行，未实际改动）" if dry_run else ""
        report["message"] = (f"部署完成：写入 {report['written']} 条、跳过 {report['skipped']} 条、"
                             f"失败 {report['failed']} 条{suffix}")
        if report["failed"]:
            report["message"] += "；失败：" + "；".join(report["errors"][:3])

    report["finished"] = now_text()
    if log:
        log("INFO" if report["ok"] else "WARN", f"【部署结果】{report['message']}")
    return report
