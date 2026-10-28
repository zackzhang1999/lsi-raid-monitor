"""报表中心：把一个时间窗内的运行数据聚合成一份可交付的报告。

面向两类读者：

- 运维：这段时间有没有出过事、盘的健康趋势、容量还剩多少、做过哪些操作
- 管理层/客户：一张纸说清楚"这套存储这段时间运行得怎么样"

数据全部来自 SQLite 的 samples 表与 alerts / tasks 表，不做任何外部依赖，
导出的 HTML 是自包含单文件（内联样式），可直接打印或另存为 PDF。
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta
from pathlib import Path

import db

BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(Path(__file__).resolve().parent / "data")))

PERIODS = {
    "day": ("日报", 1),
    "week": ("周报", 7),
    "month": ("月报", 30),
}


def _to_int(v, default=0):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def window(period: str = "day", ref: datetime | None = None) -> dict:
    """计算报表时间窗。day = 当天；week/month = 截至当前的 7/30 天。"""
    period = period if period in PERIODS else "day"
    label, days = PERIODS[period]
    now = ref or datetime.now()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = start.strftime("%Y-%m-%d")
    else:
        start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        title = f"{start.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}"
    return {
        "period": period,
        "label": label,
        "days": days,
        "start": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end": now.strftime("%Y-%m-%d %H:%M:%S"),
        "start_epoch": int(start.timestamp()),
        "end_epoch": int(now.timestamp()),
        "title": title,
    }


def _samples(kind: str, win: dict) -> list[dict]:
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT ts, data FROM samples WHERE kind = ? AND ts_epoch >= ? AND ts_epoch <= ?"
        " ORDER BY ts_epoch",
        (kind, win["start_epoch"], win["end_epoch"]),
    ).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        d["_ts"] = r["ts"]
        out.append(d)
    return out


def _fmt_bytes(n, base=1024) -> str:
    n = _to_int(n)
    if n <= 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    v = float(n)
    while v >= base and i < len(units) - 1:
        v /= base
        i += 1
    return f"{v:.1f} {units[i]}" if i else f"{int(v)} B"


# 与存储容量无关的文件系统：snap 的只读镜像、内核伪文件系统等。
# 不过滤的话，一台普通 Ubuntu 上 20 个 squashfs 挂载会把容量报表
# 淹掉，真实的数据盘反而看不见。
_PSEUDO_FSTYPES = {
    "squashfs", "tmpfs", "devtmpfs", "devpts", "proc", "sysfs", "cgroup",
    "cgroup2", "overlay", "ramfs", "autofs", "debugfs", "tracefs",
    "securityfs", "pstore", "efivarfs", "bpf", "fusectl", "mqueue", "hugetlbfs",
}
_PSEUDO_PREFIXES = ("/snap/", "/proc", "/sys", "/dev", "/run/")


def _is_real_fs(rec: dict) -> bool:
    fstype = str(rec.get("fstype") or "").lower()
    if fstype in _PSEUDO_FSTYPES or fstype.startswith("fuse."):
        return False
    mp = str(rec.get("mountpoint") or "")
    if any(mp.startswith(p) for p in _PSEUDO_PREFIXES):
        return False
    dev = str(rec.get("device") or "")
    if dev.startswith("/dev/loop") or dev.startswith("tmpfs"):
        return False
    return bool(mp)


def real_fs(rec: dict) -> bool:
    """公开版：容量预测等模块要按同一套口径过滤伪文件系统，
    否则 /snap、/proc 这些只读挂载会混进容量报表，把趋势算成噪音。"""
    return _is_real_fs(rec)


# ---- 各分区 ----


def capacity_section(win: dict) -> dict:
    """文件系统容量的期初/期末对比与增长量。"""
    rows = [r for r in _samples("fs", win) if _is_real_fs(r)]
    by_mount: dict[str, list] = {}
    for r in rows:
        mp = str(r.get("mountpoint") or r.get("device") or "")
        if mp:
            by_mount.setdefault(mp, []).append(r)

    mounts = []
    for mp, items in sorted(by_mount.items()):
        items.sort(key=lambda x: str(x.get("_ts") or ""))
        first, last = items[0], items[-1]
        # 样本里的容量字段单位是 KB；_fmt_bytes 接受的是字节，
        # 少乘一次 1024 会让 291GB 显示成 291MB。
        used_first = _to_int(first.get("used_kb")) * 1024
        used_last = _to_int(last.get("used_kb")) * 1024
        size = _to_int(last.get("size_kb")) * 1024
        mounts.append(
            {
                "mountpoint": mp,
                "device": str(last.get("device") or ""),
                "fstype": str(last.get("fstype") or ""),
                "size": _fmt_bytes(size, 1024),
                "used": _fmt_bytes(used_last, 1024),
                "avail": _fmt_bytes(_to_int(last.get("avail_kb")) * 1024, 1024),
                "use_percent": round(float(last.get("use_percent") or 0), 1),
                "growth": _fmt_bytes(max(0, used_last - used_first), 1024)
                if used_last >= used_first
                else "-" + _fmt_bytes(used_first - used_last, 1024),
                "growth_bytes": used_last - used_first,
                "samples": len(items),
            }
        )

    # 容量趋势点，供前端画折线；按天聚合，避免几千个点
    trend: dict[str, dict] = {}
    for r in rows:
        day = str(r.get("_ts") or "")[:10]
        mp = str(r.get("mountpoint") or "")
        if not day or not mp:
            continue
        trend.setdefault(day, {})[mp] = _to_int(r.get("use_percent"))
    timeline = [
        {"date": d, "mounts": v} for d, v in sorted(trend.items())
    ]

    size_bytes = sum(_to_int(_last_val(by_mount, mp, "size_kb")) * 1024 for mp in by_mount)
    used_bytes = sum(_to_int(_last_val(by_mount, mp, "used_kb")) * 1024 for mp in by_mount)
    return {
        "mounts": mounts,
        "size": _fmt_bytes(size_bytes, 1024),
        "used": _fmt_bytes(used_bytes, 1024),
        "use_percent": round(used_bytes / size_bytes * 100, 1) if size_bytes else 0,
        "timeline": timeline,
    }


def _last_val(by_mount: dict, mp: str, field: str):
    items = by_mount.get(mp) or []
    if not items:
        return 0
    items = sorted(items, key=lambda x: str(x.get("_ts") or ""))
    return items[-1].get(field)


def raid_section(status: dict | None) -> dict:
    st = status or {}
    vds = st.get("virtual_disks") or []
    rows = []
    for v in vds:
        rows.append(
            {
                "ctrl": v.get("ctrl", 0),
                "dg_vd": v.get("dg_vd") or "",
                "name": v.get("name") or "",
                "type": v.get("type") or "",
                "size": v.get("size") or "",
                "state": v.get("state") or "",
                "healthy": str(v.get("state") or "").lower() in ("optl", "optimal"),
            }
        )
    degraded = [r for r in rows if not r["healthy"]]
    return {"vds": rows, "total": len(rows), "degraded": len(degraded)}


def disk_section(win: dict, status: dict | None) -> dict:
    st = status or {}
    disks = st.get("physical_disks") or []
    by_state: dict[str, int] = {}
    for d in disks:
        s = str(d.get("state") or "未知")
        by_state[s] = by_state.get(s, 0) + 1

    temps = [
        (d.get("temperature"), d.get("full_label") or d.get("label"))
        for d in disks
        if isinstance(d.get("temperature"), int)
    ]
    temps.sort(key=lambda x: x[0], reverse=True)

    def worn(d):
        return (
            _to_int(d.get("reallocated")) * 3
            + _to_int(d.get("pending")) * 12
            + _to_int(d.get("uncorrectable")) * 6
            + _to_int(d.get("predictive_failure")) * 50
        )

    worn_list = sorted(disks, key=worn, reverse=True)
    oldest = sorted(disks, key=lambda d: _to_int(d.get("power_on_hours")), reverse=True)

    # 周期内 SMART 关键属性的增长。关注的是"增量"而不是绝对值：
    # 一块盘有 3 个重映射不可怕，一周内新增 3 个才是要处理的信号。
    watched = ("reallocated", "pending", "uncorrectable")
    smart = _samples("smart", win)
    series: dict[str, dict] = {}
    for s in smart:
        key = f"C{s.get('ctrl', 0)}:D{s.get('did')}"
        slot = series.setdefault(key, {"first": {}, "last": {}})
        for f in watched:
            v = _to_int(s.get(f))
            slot["first"].setdefault(f, v)
            slot["last"][f] = v
    smart_growth = []
    for key, slot in series.items():
        delta = {f: slot["last"].get(f, 0) - slot["first"].get(f, 0) for f in watched}
        if any(v > 0 for v in delta.values()):
            smart_growth.append({"disk": key, **delta})
    smart_growth.sort(key=lambda x: (x["pending"], x["reallocated"]), reverse=True)

    return {
        "total": len(disks),
        "by_state": by_state,
        "avg_temp": round(sum(t[0] for t in temps) / len(temps), 1) if temps else None,
        "max_temp": {"value": temps[0][0], "disk": temps[0][1]} if temps else None,
        "worn": [
            {
                "label": d.get("full_label") or d.get("label"),
                "model": d.get("model"),
                "reallocated": _to_int(d.get("reallocated")),
                "pending": _to_int(d.get("pending")),
                "uncorrectable": _to_int(d.get("uncorrectable")),
                "level": (d.get("prediction") or {}).get("level", "ok"),
            }
            for d in worn_list[:8]
        ],
        "oldest": [
            {
                "label": d.get("full_label") or d.get("label"),
                "model": d.get("model"),
                "hours": _to_int(d.get("power_on_hours")),
                "days": round(_to_int(d.get("power_on_hours")) / 24, 1),
            }
            for d in oldest[:8]
        ],
        "smart_growth": smart_growth[:10],
    }


def alert_section(win: dict) -> dict:
    conn = db.get_conn()
    start, end = win["start"], win["end"]
    opened = conn.execute(
        "SELECT * FROM alerts WHERE first_seen >= ? AND first_seen <= ?"
        " ORDER BY first_seen DESC",
        (start, end),
    ).fetchall()
    resolved = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE resolved_at >= ? AND resolved_at <= ?",
        (start, end),
    ).fetchone()
    open_now = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE status != 'resolved'"
    ).fetchone()
    return {
        "opened": len(opened),
        "resolved": int(resolved["n"]) if resolved else 0,
        "open_now": int(open_now["n"]) if open_now else 0,
        "items": [
            {
                "title": r["title"],
                "level": r["level"],
                "detail": (r["detail"] or "")[:120],
                "first_seen": r["first_seen"],
                "status": r["status"],
                "count": int(r["count"] or 1),
            }
            for r in opened[:20]
        ],
    }


def task_section(win: dict) -> dict:
    conn = db.get_conn()
    start, end = win["start"], win["end"]
    rows = conn.execute(
        "SELECT * FROM tasks WHERE created_at >= ? AND created_at <= ?"
        " ORDER BY created_at DESC",
        (start, end),
    ).fetchall()
    failed = [r for r in rows if r["status"] == "failed"]
    return {
        "total": len(rows),
        "failed": len(failed),
        "items": [
            {
                "title": r["title"],
                "target": r["target"],
                "status": r["status"],
                "operator": r["operator"],
                "created_at": r["created_at"],
                "message": (r["message"] or "")[:120],
            }
            for r in rows[:30]
        ],
    }


def bcache_section(win: dict, status: dict | None) -> dict:
    rows = _samples("bcache_trend", win)
    by_dev: dict[str, list] = {}
    for r in rows:
        dev = str(r.get("device") or "")
        if dev:
            by_dev.setdefault(dev, []).append(r)
    devices = []
    for dev, items in sorted(by_dev.items()):
        items.sort(key=lambda x: str(x.get("_ts") or ""))
        hours = [x.get("hour_hit_ratio") for x in items if x.get("hour_hit_ratio") is not None]
        devices.append(
            {
                "device": dev,
                "latest_hour": items[-1].get("hour_hit_ratio"),
                "latest_day": items[-1].get("day_hit_ratio"),
                "avg_hour": round(sum(hours) / len(hours), 1) if hours else None,
                "min_hour": min(hours) if hours else None,
                "samples": len(items),
            }
        )
    return {"devices": devices}


def build_report(period: str = "day", status: dict | None = None) -> dict:
    win = window(period)
    st = status or {}
    return {
        "meta": {
            "host": st.get("host") or socket.gethostname(),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **win,
        },
        "health": {
            "overall": st.get("health") or "unknown",
            "controllers": st.get("controllers") or [],
            "consistency": st.get("consistency") or {"ok": True, "issues": []},
        },
        "capacity": capacity_section(win),
        "raid": raid_section(st),
        "disks": disk_section(win, st),
        "alerts": alert_section(win),
        "tasks": task_section(win),
        "bcache": bcache_section(win, st),
    }


# ---- HTML / CSV 导出 ----


def _e(v) -> str:
    s = "" if v is None else str(v)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_HEALTH_TEXT = {"ok": "正常", "warn": "警告", "crit": "严重", "unknown": "未知"}
_LEVEL_TEXT = {"error": "错误", "warning": "警告", "info": "信息"}
_STATE_TEXT = {"succeeded": "已完成", "failed": "失败", "running": "执行中", "queued": "排队中"}


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return '<p class="none">无数据</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_e(c)}</td>" for c in r) + "</tr>" for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(report: dict) -> str:
    """生成自包含的 HTML 报告（样式内联，无外部依赖，可直接打印或另存为 PDF）。"""
    m = report["meta"]
    h = report["health"]
    cap = report["capacity"]
    raid = report["raid"]
    disks = report["disks"]
    alerts = report["alerts"]
    tasks = report["tasks"]
    bc = report["bcache"]

    ctrl_rows = [
        [
            f"C{c.get('ctrl')}",
            c.get("model") or "",
            c.get("fw") or "",
            _HEALTH_TEXT.get(str(c.get("health") or "").lower(), c.get("health") or ""),
            f"{c.get('num_disks', 0)} / {c.get('num_vds', 0)}",
            f"{c.get('roc_temp')}°C" if c.get("roc_temp") is not None else "—",
        ]
        for c in h.get("controllers") or []
    ]
    issues = (h.get("consistency") or {}).get("issues") or []
    issue_html = (
        "<div class='issues'>"
        + "".join(
            f"<div class='issue {_e(i.get('level'))}'><strong>{_e(i.get('title'))}</strong>"
            f"<span>{_e(i.get('detail'))}</span><em>{_e(i.get('hint'))}</em></div>"
            for i in issues
        )
        + "</div>"
        if issues
        else ""
    )

    cap_rows = [
        [x["mountpoint"], x["device"], x["fstype"], x["size"], x["used"], x["avail"], f"{x['use_percent']}%", x["growth"]]
        for x in cap.get("mounts") or []
    ]
    vd_rows = [
        [f"C{v['ctrl']}", v["dg_vd"], v["name"], v["type"], v["size"], v["state"]]
        for v in raid.get("vds") or []
    ]
    state_rows = [[k, v] for k, v in sorted((disks.get("by_state") or {}).items())]
    worn_rows = [
        [
            d["label"],
            d["model"],
            d["reallocated"],
            d["pending"],
            d["uncorrectable"],
            {"ok": "正常", "info": "关注", "warn": "建议更换", "crit": "立即更换"}.get(d["level"], d["level"]),
        ]
        for d in disks.get("worn") or []
    ]
    old_rows = [[d["label"], d["model"], f"{d['days']} 天", d["hours"]] for d in disks.get("oldest") or []]
    growth_rows = [
        [g["disk"], g["reallocated"], g["pending"], g["uncorrectable"]]
        for g in disks.get("smart_growth") or []
    ]
    alert_rows = [
        [
            a["first_seen"],
            _LEVEL_TEXT.get(a["level"], a["level"]),
            a["title"],
            a["detail"],
            "已恢复" if a["status"] == "resolved" else ("已确认" if a["status"] == "acknowledged" else "未处理"),
        ]
        for a in alerts.get("items") or []
    ]
    task_rows = [
        [
            t["created_at"],
            t["title"],
            t["target"],
            _STATE_TEXT.get(t["status"], t["status"]),
            t["operator"],
            t["message"],
        ]
        for t in tasks.get("items") or []
    ]
    bc_rows = [
        [d["device"], f"{d['avg_hour']}%" if d["avg_hour"] is not None else "—",
         f"{d['min_hour']}%" if d["min_hour"] is not None else "—",
         f"{d['latest_hour']}%" if d["latest_hour"] is not None else "—",
         d["samples"]]
        for d in bc.get("devices") or []
    ]

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>DataMax-LSI {_e(m['label'])} · {_e(m['title'])}</title>
<style>
  :root {{ --ink:#131a24; --soft:#5d6a7b; --faint:#8b95a3; --line:#e2e7ee; --bg:#eef1f5;
           --ok:#14805d; --warn:#ad6100; --crit:#c84646; --brand:#2767e8; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:32px; background:var(--bg); color:var(--ink);
          font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif; font-size:13.5px; line-height:1.6; }}
  .page {{ max-width:1180px; margin:0 auto; background:#fff; border:1px solid var(--line); border-radius:16px; padding:36px 40px 44px; }}
  header {{ border-bottom:2px solid var(--ink); padding-bottom:16px; margin-bottom:24px; }}
  h1 {{ margin:0; font-size:22px; letter-spacing:-.02em; }}
  .sub {{ color:var(--soft); font-size:12.5px; margin-top:6px; }}
  .pill {{ display:inline-block; padding:2px 10px; border-radius:999px; font-size:11.5px; font-weight:600; }}
  .pill.ok {{ background:rgba(20,128,93,.12); color:var(--ok); }}
  .pill.warn {{ background:rgba(209,120,0,.12); color:var(--warn); }}
  .pill.crit {{ background:rgba(200,70,70,.12); color:var(--crit); }}
  .pill.unknown {{ background:#eceff4; color:var(--faint); }}
  h2 {{ font-size:15px; margin:30px 0 12px; padding-bottom:7px; border-bottom:1px solid var(--line); }}
  h2:first-of-type {{ margin-top:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
  th {{ text-align:left; font-size:10.5px; letter-spacing:.06em; text-transform:uppercase;
        color:var(--faint); padding:8px 9px; border-bottom:1px solid var(--line); font-weight:600; }}
  td {{ padding:8px 9px; border-bottom:1px solid var(--line); vertical-align:top; }}
  tbody tr:last-child td {{ border-bottom:none; }}
  .none {{ color:var(--faint); font-size:12.5px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }}
  .card {{ border:1px solid var(--line); border-radius:12px; padding:14px 16px; background:#fbfcfe; }}
  .card .k {{ font-size:10.5px; letter-spacing:.09em; text-transform:uppercase; color:var(--faint); }}
  .card .v {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:22px; font-weight:700; margin:6px 0 2px; }}
  .card .s {{ font-size:11.5px; color:var(--soft); }}
  .issues {{ display:flex; flex-direction:column; gap:8px; margin-bottom:14px; }}
  .issue {{ border:1px solid var(--line); border-left:3px solid var(--warn); border-radius:10px; padding:10px 13px; background:#fdfbf7; }}
  .issue.info {{ border-left-color:var(--brand); background:#f7faff; }}
  .issue strong {{ display:block; font-size:12.5px; }}
  .issue span {{ display:block; font-size:12px; color:var(--soft); }}
  .issue em {{ display:block; font-size:11.5px; color:var(--faint); font-style:normal; }}
  footer {{ margin-top:34px; padding-top:14px; border-top:1px solid var(--line); color:var(--faint); font-size:11.5px; }}
  @media print {{ body {{ background:#fff; padding:0; }} .page {{ border:none; border-radius:0; padding:0; max-width:none; }} }}
</style></head>
<body><div class="page">
  <header>
    <h1>DataMax-LSI 存储运行{_e(m['label'])}</h1>
    <div class="sub">
      主机 <strong>{_e(m['host'])}</strong> · 周期 {_e(m['title'])} ·
      生成于 {_e(m['generated_at'])} ·
      整体健康 <span class="pill {_e(h.get('overall'))}">{_e(_HEALTH_TEXT.get(h.get('overall'), h.get('overall')))}</span>
    </div>
  </header>

  {issue_html}

  <h2>控制器</h2>
  {_table(['控制器', '型号', '固件', '健康', '磁盘 / 阵列', 'ROC 温度'], ctrl_rows)}

  <h2>容量</h2>
  <div class="cards">
    <div class="card"><div class="k">已用容量</div><div class="v">{_e(cap.get('used'))}</div><div class="s">占 {_e(cap.get('use_percent'))}%</div></div>
    <div class="card"><div class="k">总容量</div><div class="v">{_e(cap.get('size'))}</div><div class="s">文件系统合计</div></div>
    <div class="card"><div class="k">虚拟磁盘</div><div class="v">{_e(raid.get('total'))}</div><div class="s">{_e(raid.get('degraded'))} 个非最优</div></div>
    <div class="card"><div class="k">物理磁盘</div><div class="v">{_e(disks.get('total'))}</div><div class="s">均温 {_e(disks.get('avg_temp'))}°C</div></div>
  </div>
  <h2>文件系统用量与增长</h2>
  {_table(['挂载点', '设备', '类型', '容量', '已用', '可用', '使用率', '周期内增长'], cap_rows)}

  <h2>RAID 阵列</h2>
  {_table(['控制器', 'DG/VD', '名称', '级别', '容量', '状态'], vd_rows)}

  <h2>磁盘健康</h2>
  <h3 style="font-size:13px;color:var(--soft);margin:14px 0 8px">状态分布</h3>
  {_table(['状态', '数量'], state_rows)}
  <h3 style="font-size:13px;color:var(--soft);margin:16px 0 8px">磨损排行</h3>
  {_table(['盘位', '型号', '重映射', '待定', '无法纠正', '预测'], worn_rows)}
  <h3 style="font-size:13px;color:var(--soft);margin:16px 0 8px">通电时长排行</h3>
  {_table(['盘位', '型号', '通电天数', '通电小时'], old_rows)}
  <h3 style="font-size:13px;color:var(--soft);margin:16px 0 8px">周期内 SMART 属性增长</h3>
  {_table(['磁盘', '重映射增量', '待定增量', '无法纠正增量'], growth_rows)}

  <h2>告警</h2>
  <div class="cards">
    <div class="card"><div class="k">周期内新增</div><div class="v">{_e(alerts.get('opened'))}</div><div class="s">条告警</div></div>
    <div class="card"><div class="k">周期内恢复</div><div class="v">{_e(alerts.get('resolved'))}</div><div class="s">条告警</div></div>
    <div class="card"><div class="k">当前未处理</div><div class="v">{_e(alerts.get('open_now'))}</div><div class="s">条告警</div></div>
  </div>
  <h3 style="font-size:13px;color:var(--soft);margin:16px 0 8px">告警明细</h3>
  {_table(['首次出现', '级别', '标题', '详情', '状态'], alert_rows)}

  <h2>运维操作</h2>
  {_table(['时间', '操作', '目标', '结果', '操作者', '说明'], task_rows)}

  <h2>SSD / NVMe 缓存加速</h2>
  {_table(['设备', '平均小时命中率', '最低小时命中率', '最新小时命中率', '采样点'], bc_rows)}

  <footer>本报告由 DataMax-LSI 自动生成 · 数据来源：本地 SQLite 历史样本 · 可在浏览器中直接打印或另存为 PDF</footer>
</div></body></html>"""


def render_csv(report: dict) -> str:
    """把报表导出为 CSV。多个分区用空行 + 分区标题分隔。"""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    m = report["meta"]
    w.writerow(["DataMax-LSI", m["label"], m["title"]])
    w.writerow(["主机", m["host"], "生成时间", m["generated_at"]])
    w.writerow([])

    w.writerow(["控制器"])
    w.writerow(["控制器", "型号", "固件", "健康", "磁盘数", "阵列数", "ROC温度"])
    for c in report["health"].get("controllers") or []:
        w.writerow([f"C{c.get('ctrl')}", c.get("model"), c.get("fw"), c.get("health"),
                    c.get("num_disks"), c.get("num_vds"), c.get("roc_temp")])
    w.writerow([])

    w.writerow(["文件系统"])
    w.writerow(["挂载点", "设备", "类型", "容量", "已用", "可用", "使用率%", "周期内增长"])
    for x in report["capacity"].get("mounts") or []:
        w.writerow([x["mountpoint"], x["device"], x["fstype"], x["size"], x["used"],
                    x["avail"], x["use_percent"], x["growth"]])
    w.writerow([])

    w.writerow(["虚拟磁盘"])
    w.writerow(["控制器", "DG/VD", "名称", "级别", "容量", "状态"])
    for v in report["raid"].get("vds") or []:
        w.writerow([f"C{v['ctrl']}", v["dg_vd"], v["name"], v["type"], v["size"], v["state"]])
    w.writerow([])

    w.writerow(["磁盘磨损排行"])
    w.writerow(["盘位", "型号", "重映射", "待定", "无法纠正", "预测"])
    for d in report["disks"].get("worn") or []:
        w.writerow([d["label"], d["model"], d["reallocated"], d["pending"], d["uncorrectable"], d["level"]])
    w.writerow([])

    w.writerow(["周期内告警"])
    w.writerow(["首次出现", "级别", "标题", "详情", "状态"])
    for a in report["alerts"].get("items") or []:
        w.writerow([a["first_seen"], a["level"], a["title"], a["detail"], a["status"]])
    w.writerow([])

    w.writerow(["运维操作"])
    w.writerow(["时间", "操作", "目标", "结果", "操作者", "说明"])
    for t in report["tasks"].get("items") or []:
        w.writerow([t["created_at"], t["title"], t["target"], t["status"], t["operator"], t["message"]])
    return buf.getvalue()
