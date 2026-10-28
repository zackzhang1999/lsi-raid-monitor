"""控制器配置快照与漂移检测。

阵列配置本身不会自己变——变了就一定是有人动了手，或者卡被换过。这两种
情况都需要立刻知道：

- 有人改了阵列设置：需要确认这是计划内的变更
- 卡被换过、配置丢失：需要尽快恢复，否则盘上的数据可能读不出来

所以这里做三件事：定期给配置拍快照、比对差异、把差异变成告警。

快照只保留**归一化的配置项**。温度、运行时长、错误计数这类每轮都在变的
值必须排除，否则每次快照的哈希都不同，漂移检测会变成永不停歇的误报——
一个永远在报警的检测等于没有检测。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

import db

BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
KEEP_SNAPSHOTS = int(os.environ.get("LSI_CONFIG_SNAPSHOTS", "60") or 60)


def _to_int(v, default=0):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _slug(s: str) -> str:
    """把 storcli 的属性名转成稳定的键名，避免空格与大小写差异导致误判。"""
    return re.sub(r"[^a-z0-9]+", "_", str(s or "").strip().lower()).strip("_")


def normalize(storcli_all: dict, storcli_summary: dict | None = None) -> dict:
    """把 storcli 的 `show all J` 输出归一化成可比对的配置结构。

    只取"配置身份"字段：型号、固件、控制器唯一标识、阵列布局、成员盘归属、
    控制器策略。运行态字段（温度、错误计数、进度）一律不要。

    注意 storcli 的字段名：`show all J` 里**没有** Controller Properties，
    配置分散在 Policies / Defaults / Boot 三个子对象里。读错键不会报错，
    只会安静地得到一份空配置——那等于配置漂移检测完全失效。
    """
    out = {
        "model": "",
        "firmware": "",
        "safe_id": "",
        "settings": {},
        "vds": [],
        "pds": [],
        "jbod": [],
    }
    try:
        ctrl0 = (storcli_all or {}).get("Controllers", [{}])[0]
        resp = ctrl0.get("Response Data", {}) or {}
    except Exception:
        return out

    out["model"] = str(resp.get("Basics", {}).get("Model", "")).strip()
    out["firmware"] = str(resp.get("Version", {}).get("Firmware Version", "")).strip()
    # Safe ID 是控制器的唯一标识：换卡后它一定会变，是"卡被换过"最直接的证据
    out["safe_id"] = str(resp.get("Safe ID", "")).strip()

    # 策略与默认值：最容易被人改掉、影响也最大的就是这些
    for section, prefix in (("Policies", ""), ("Defaults", "default_"), ("Boot", "boot_")):
        sec = resp.get(section)
        if not isinstance(sec, dict):
            continue
        for k, v in sec.items():
            if k == "Policies Table":
                # 速率类策略是列表，展开成扁平项
                for item in v or []:
                    if isinstance(item, dict) and item.get("Policy"):
                        out["settings"][_slug(item["Policy"])] = str(item.get("Current", "")).strip()
                continue
            if isinstance(v, (str, int, float, bool)):
                out["settings"][prefix + _slug(k)] = str(v).strip()

    # JBOD 盘单列一张表，不属于任何阵列，但它的存在本身就是配置状态
    for p in resp.get("JBOD LIST") or []:
        if isinstance(p, dict):
            out["jbod"].append(
                {
                    "slot": str(p.get("EID:Slt", "")).strip(),
                    "size": str(p.get("Size", "")).strip(),
                }
            )
    out["jbod"].sort(key=lambda x: x["slot"])

    # 虚拟盘：编号、名称、级别、容量、跨盘数、条带、写策略
    for vd in resp.get("VD LIST") or []:
        if not isinstance(vd, dict):
            continue
        out["vds"].append(
            {
                "id": str(vd.get("DG/VD", "")).strip(),
                "name": str(vd.get("Name", "")).strip(),
                "level": str(vd.get("TYPE", "")).strip(),
                "size": str(vd.get("Size", "")).strip(),
                "strip": str(vd.get("SC", "")).strip() if "SC" in vd else "",
            }
        )
    out["vds"].sort(key=lambda x: x["id"])

    # 物理盘归属：DG 变化意味着盘被移出/加入了阵列，是关键的配置变更
    for p in resp.get("PD LIST") or []:
        if not isinstance(p, dict):
            continue
        out["pds"].append(
            {
                "slot": str(p.get("EID:Slt", "")).strip(),
                "dg": str(p.get("DG", "")).strip(),
                "size": str(p.get("Size", "")).strip(),
                "intf": str(p.get("Intf", "")).strip(),
                "med": str(p.get("Med", "")).strip(),
            }
        )
    out["pds"].sort(key=lambda x: x["slot"])

    # 阵列组（DG）级别的条带与 RAID 级别
    dgs = []
    for dg in resp.get("TOPOLOGY") or []:
        if isinstance(dg, dict):
            dgs.append({k: str(v).strip() for k, v in dg.items() if isinstance(v, (str, int))})
    if dgs:
        out["topology"] = dgs
    return out


def is_meaningful(cfg: dict) -> bool:
    """判断归一化结果是否包含实质内容。

    storcli 输出结构变化或读取失败时，normalize 会返回一份空壳。把空壳存成
    快照，会让下一次真实采集被误判为"配置大变"，进而发出无法解释的告警。
    所以宁可不存，也不存一份自己都不相信的快照。
    """
    cfg = cfg or {}
    return bool(
        str(cfg.get("model") or "").strip()
        or str(cfg.get("safe_id") or "").strip()
        or cfg.get("settings")
        or cfg.get("vds")
        or cfg.get("pds")
    )


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(
        json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def save_snapshot(ctrl_id: int, cfg: dict, operator: str = "", note: str = "", reason: str = "auto") -> dict:
    conn = db.get_conn()
    h = config_hash(cfg)
    cur = conn.execute(
        "INSERT INTO ctrl_configs(ctrl, ts, ts_epoch, hash, data, operator, note, reason)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            int(ctrl_id),
            _now(),
            int(datetime.now().timestamp()),
            h,
            json.dumps(cfg, ensure_ascii=False, sort_keys=True),
            operator,
            note,
            reason,
        ),
    )
    conn.commit()
    conn.execute(
        "DELETE FROM ctrl_configs WHERE ctrl = ? AND id NOT IN"
        " (SELECT id FROM ctrl_configs WHERE ctrl = ? ORDER BY id DESC LIMIT ?)",
        (int(ctrl_id), int(ctrl_id), max(5, KEEP_SNAPSHOTS)),
    )
    conn.commit()
    return {"id": int(cur.lastrowid), "ctrl": ctrl_id, "hash": h, "ts": _now()}


def latest_snapshot(ctrl_id: int) -> dict | None:
    conn = db.get_conn()
    r = conn.execute(
        "SELECT * FROM ctrl_configs WHERE ctrl = ? ORDER BY id DESC LIMIT 1", (int(ctrl_id),)
    ).fetchone()
    return _row(r) if r else None


def _row(r) -> dict:
    d = dict(r)
    try:
        d["data"] = json.loads(d.get("data") or "{}")
    except (ValueError, TypeError):
        d["data"] = {}
    return d


def history(ctrl_id: int, limit: int = 50) -> list[dict]:
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, ctrl, ts, hash, operator, note, reason FROM ctrl_configs"
        " WHERE ctrl = ? ORDER BY id DESC LIMIT ?",
        (int(ctrl_id), max(1, min(int(limit), 200))),
    ).fetchall()
    return [dict(r) for r in rows]


def get_snapshot(snap_id: int) -> dict | None:
    conn = db.get_conn()
    r = conn.execute("SELECT * FROM ctrl_configs WHERE id = ?", (int(snap_id),)).fetchone()
    return _row(r) if r else None


# ---- 差异比对 ----


def diff(a: dict, b: dict) -> list[dict]:
    """比较两份归一化配置，返回差异列表。

    输出面向运维而不是程序员：每条差异说明**哪一项、从什么变成了什么**，
    而不是丢一个 JSON patch 让人自己解读。
    """
    a = a or {}
    b = b or {}
    out: list[dict] = []

    for field, label in (
        ("model", "控制器型号"),
        ("firmware", "固件版本"),
        ("safe_id", "控制器唯一标识"),
    ):
        if str(a.get(field) or "") != str(b.get(field) or ""):
            out.append(
                {
                    "kind": "field",
                    "key": field,
                    "label": label,
                    "from": a.get(field) or "—",
                    "to": b.get(field) or "—",
                    # Safe ID 变化意味着换了卡（或卡被重置），是最严重的一种
                    "impact": "high" if field in ("model", "safe_id") else "info",
                }
            )

    sa, sb = a.get("settings") or {}, b.get("settings") or {}
    for k in sorted(set(sa) | set(sb)):
        if str(sa.get(k, "")) != str(sb.get(k, "")):
            out.append(
                {
                    "kind": "setting",
                    "key": k,
                    "label": k,
                    "from": sa.get(k, "—"),
                    "to": sb.get(k, "—"),
                    "impact": "medium",
                }
            )

    va = {v["id"]: v for v in (a.get("vds") or [])}
    vb = {v["id"]: v for v in (b.get("vds") or [])}
    for vid in sorted(set(va) | set(vb)):
        if vid not in va:
            out.append({"kind": "vd_added", "key": vid, "label": f"虚拟盘 {vid}", "from": "—",
                        "to": f"{vb[vid].get('level', '')} {vb[vid].get('size', '')}".strip(), "impact": "high"})
        elif vid not in vb:
            out.append({"kind": "vd_removed", "key": vid, "label": f"虚拟盘 {vid}", "from":
                        f"{va[vid].get('level', '')} {va[vid].get('size', '')}".strip(), "to": "—", "impact": "high"})
        else:
            for f, lbl in (("level", "RAID 级别"), ("size", "容量"), ("name", "名称")):
                if str(va[vid].get(f, "")) != str(vb[vid].get(f, "")):
                    out.append({"kind": "vd_changed", "key": f"{vid}.{f}", "label": f"虚拟盘 {vid} {lbl}",
                                "from": va[vid].get(f, "—"), "to": vb[vid].get(f, "—"), "impact": "high"})

    pa = {p["slot"]: p for p in (a.get("pds") or [])}
    pb = {p["slot"]: p for p in (b.get("pds") or [])}
    for slot in sorted(set(pa) | set(pb)):
        if slot not in pa:
            out.append({"kind": "pd_added", "key": slot, "label": f"磁盘 {slot}", "from": "—",
                        "to": f"DG {pb[slot].get('dg', '')} {pb[slot].get('size', '')}".strip(), "impact": "medium"})
        elif slot not in pb:
            out.append({"kind": "pd_removed", "key": slot, "label": f"磁盘 {slot}",
                        "from": f"DG {pa[slot].get('dg', '')} {pa[slot].get('size', '')}".strip(),
                        "to": "—", "impact": "high"})
        elif str(pa[slot].get("dg", "")) != str(pb[slot].get("dg", "")):
            # DG 变化是"盘被移出/加入阵列"，是最需要人确认的一类变更
            out.append({"kind": "pd_dg", "key": slot, "label": f"磁盘 {slot} 阵列归属",
                        "from": f"DG {pa[slot].get('dg', '')}", "to": f"DG {pb[slot].get('dg', '')}",
                        "impact": "high"})
    return out


def compare_with_previous(ctrl_id: int, cfg: dict) -> dict | None:
    """与上一份快照比较。相同则返回 None，不同则返回差异详情。

    采集侧每轮调用一次：配置没变时什么都不做，变了才留档。
    这样快照历史里只有真正发生过的变更，翻起来是有信息量的。
    """
    prev = latest_snapshot(ctrl_id)
    if not prev:
        return None
    if prev.get("hash") == config_hash(cfg):
        return None
    changes = diff(prev.get("data") or {}, cfg)
    if not changes:
        # 哈希不同但结构化差异为空：说明差异只出现在未纳入比对的字段上，
        # 记下来但不当成配置漂移，避免给出无法解释的告警。
        return {"ctrl": ctrl_id, "changes": [], "hash_changed_only": True}
    return {
        "ctrl": ctrl_id,
        "changes": changes,
        "prev_id": prev.get("id"),
        "prev_ts": prev.get("ts"),
        "high": sum(1 for c in changes if c.get("impact") == "high"),
    }


def observe(ctrl_id: int, cfg: dict, operator: str = "", note: str = "") -> dict | None:
    """采集侧入口。

    - 没有历史快照：建立基线，不报变更（第一次见到配置不是"变更"）
    - 与上一份相同：什么都不做
    - 不同：留档并返回差异，交给调用方决定要不要告警
    """
    if not is_meaningful(cfg):
        # 提取失败时不写快照：存一份空配置会让下一次真实采集被误判为
        # "配置大变"，进而发出谁也解释不清的告警。
        return None
    prev = latest_snapshot(ctrl_id)
    if prev is None:
        save_snapshot(ctrl_id, cfg, operator, note or "基线快照", "baseline")
        return None
    if prev.get("hash") == config_hash(cfg):
        return None

    change = compare_with_previous(ctrl_id, cfg) or {"ctrl": ctrl_id, "changes": []}
    if not change.get("changes"):
        return None
    save_snapshot(ctrl_id, cfg, operator, note or "检测到配置变更", "drift")
    return change


def card_replacement(ctrl_id: int, lookback: int = 10) -> dict | None:
    """判断这台控制器是否被更换过。

    依据是快照历史里残留的漂移记录，而不是"最新快照与当前配置的差异"——
    漂移一旦被记录，最新快照就已经是新卡的配置了，拿它比对永远看不出
    换过卡。历史里的那条记录才是持久的证据。
    """
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM ctrl_configs WHERE ctrl = ? ORDER BY id DESC LIMIT ?",
        (int(ctrl_id), max(2, min(int(lookback), 60))),
    ).fetchall()
    if len(rows) < 2:
        return None
    # rows 是倒序的：从最新往回走，找第一个 safe_id 与更早记录不同的点
    items = [_row(r) for r in rows]
    for i in range(len(items) - 1):
        newer = (items[i].get("data") or {}).get("safe_id") or ""
        older = (items[i + 1].get("data") or {}).get("safe_id") or ""
        if newer and older and newer != older:
            d = diff(items[i + 1].get("data") or {}, items[i].get("data") or {})
            return {
                "ctrl": ctrl_id,
                "from_safe_id": older,
                "to_safe_id": newer,
                "from_model": (items[i + 1].get("data") or {}).get("model") or "",
                "to_model": (items[i].get("data") or {}).get("model") or "",
                "detected_at": items[i].get("ts"),
                "snapshot_id": items[i].get("id"),
                "changes": d,
            }
    return None
