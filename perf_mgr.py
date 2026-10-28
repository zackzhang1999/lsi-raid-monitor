"""块设备性能监控：IOPS、带宽、延迟、利用率、队列深度。

存储系统里"跑得慢"和"快要坏"是两类完全不同的故障，但现象都是"磁盘有问题"。
区分它们靠的是**延迟**和**利用率**：一块盘重映射数在涨但延迟平稳，那是寿命
问题；延迟从 0.5ms 涨到 40ms，那是路径（背板/线缆/控制器/缓存）问题。
只报 IOPS 和带宽看不出来，所以这里把 await 和 %util 提到一等位置。

数据来自 /proc/diskstats 的累计计数器，所有速率都必须**两次采样求差**：

    iops      = Δ(读次数 + 写次数) / Δt
    await(ms) = Δ(读耗时 + 写耗时) / Δ(读次数 + 写次数)   每次 IO 的平均耗时
    %util     = Δ(IO 忙时长) / Δt × 100
    队列深度   = Δ(加权 IO 忙时长) / Δt

几个必须注意的点：

* 计数器是**单调累计**的，重启后会归零。差值出现负数时必须丢弃这次样本，
  否则会在图上画出一个巨大的负值尖峰。
* 空闲设备的两个采样之间往往没有任何 IO，除数会是 0——这时延迟和利用率
  应该报 0 而不是报"无法计算"，否则整张表看起来都是坏数据。
* /proc/diskstats 里 loop / ram 这类虚拟设备要过滤掉，它们数量多且没有意义。
* 单次 1 秒采样的瞬时值抖动很大，所以**实时接口采样 1 秒、告警用最近若干
  分钟的平均值**，避免被一个尖峰触发。
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import db

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))

SECTOR = 512
DISKSTATS = Path("/proc/diskstats")
SYS_BLOCK = Path("/sys/block")

_NAME_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|bcache\d+|md\d+)$")

# 告警门限（对最近窗口的平均值判定，不用瞬时值）
ALERT_MIN_IOPS = 10
LAT_WARN_MS, LAT_CRIT_MS = 50.0, 200.0
UTIL_WARN = 95.0
ALERT_WINDOW_MIN = 10


def _read_diskstats() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        with open(DISKSTATS) as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        p = line.split()
        if len(p) < 14:
            continue
        name = p[2]
        if not _NAME_RE.match(name):
            continue
        try:
            out[name] = {
                "reads": int(p[3]),
                "sectors_read": int(p[5]),
                "ms_read": int(p[6]),
                "writes": int(p[7]),
                "sectors_written": int(p[9]),
                "ms_write": int(p[10]),
                "inflight": int(p[11]),
                "ms_io": int(p[12]),
                "weighted_ms_io": int(p[13]),
            }
        except ValueError:
            continue
    return out


def _delta(a: dict, b: dict, dt: float) -> dict:
    """由两次累计快照算出速率。计数器归零（重启）时返回空。"""
    if dt <= 0:
        return {}
    d = {}
    for k in (
        "reads",
        "sectors_read",
        "ms_read",
        "writes",
        "sectors_written",
        "ms_write",
        "ms_io",
        "weighted_ms_io",
    ):
        v = int(b.get(k, 0)) - int(a.get(k, 0))
        if v < 0:
            return {}
        d[k] = v
    ios = d["reads"] + d["writes"]
    ms = d["ms_read"] + d["ms_write"]
    return {
        "iops": ios / dt,
        "read_iops": d["reads"] / dt,
        "write_iops": d["writes"] / dt,
        "read_bps": d["sectors_read"] * SECTOR / dt,
        "write_bps": d["sectors_written"] * SECTOR / dt,
        # 没有任何 IO 时延迟无意义，报 0 而不是留空：
        # 表格里出现一片 "—" 会让人以为采集坏了。
        "latency_ms": (ms / ios) if ios else 0.0,
        "read_latency_ms": (d["ms_read"] / d["reads"]) if d["reads"] else 0.0,
        "write_latency_ms": (d["ms_write"] / d["writes"]) if d["writes"] else 0.0,
        "util_percent": min(100.0, d["ms_io"] / (dt * 1000.0) * 100.0),
        "queue_depth": d["weighted_ms_io"] / (dt * 1000.0),
        "inflight": int(b.get("inflight", 0)),
    }


# ---- 设备元信息：这是什么设备、上面挂着什么 ----


def _read_text(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def _bus_of(name: str) -> str:
    """设备挂在哪条总线上。

    这不是锦上添花的信息：一块 USB 盘做系统盘时，同样的负载下延迟会是
    SATA 盘的几十倍，而这从 IOPS / 吞吐上看不出来——只能从"它挂在 USB 上"
    这一条解释。诊断里单独拎出来说，比笼统地报"设备很忙"有用得多。
    """
    try:
        real = os.path.realpath(f"/sys/block/{name}/device")
    except OSError:
        return ""
    low = real.lower()
    if "/usb" in low:
        return "usb"
    if "nvme" in low:
        return "nvme"
    if "/ata" in low or "/sata" in low:
        return "sata"
    if "/virtio" in low:
        return "virtio"
    if "/host" in low or "/target" in low:
        return "scsi"
    return ""


def _mounts() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and p[0].startswith("/dev/"):
                    out.setdefault(os.path.basename(p[0]), []).append(p[1])
    except OSError:
        pass
    return out


def device_meta() -> dict[str, dict]:
    """每个块设备的角色、型号与挂载点。

    判断"这块 sdX 是阵列虚拟盘还是系统盘"靠的是 /sys/block/<dev>/device/model：
    MegaRAID 暴露出来的 VD 型号形如 MR9460-16i，据此归类。
    挂载点要同时看设备本身和它上面的 holder（例如 sda -> bcache0 -> /mnt）。
    """
    mounts = _mounts()
    out: dict[str, dict] = {}
    if not SYS_BLOCK.exists():
        return out
    for dev in sorted(SYS_BLOCK.iterdir()):
        name = dev.name
        if not _NAME_RE.match(name):
            continue
        vendor = _read_text(dev / "device" / "vendor")
        model = _read_text(dev / "device" / "model")
        try:
            size = int(_read_text(dev / "size")) * SECTOR
        except ValueError:
            size = 0

        holders = sorted(p.name for p in (dev / "holders").iterdir()) if (dev / "holders").exists() else []
        slaves = sorted(p.name for p in (dev / "slaves").iterdir()) if (dev / "slaves").exists() else []

        # 直接挂载要连分区一起看：/boot/efi 挂在 sdb1 上，只看 sdb 会漏掉，
        # 于是系统盘被误判成普通数据盘。
        mps = list(mounts.get(name, []))
        for other in mounts:
            if other != name and other.startswith(name) and other[len(name) : len(name) + 1].isdigit():
                mps.extend(mounts[other])
        # 被上层设备（bcache/md）接管时，如实说明它服务于谁，
        # 而不是把上层的挂载点算成自己也挂在那里。
        serves = [
            {"holder": h, "mounts": sorted(set(mounts.get(h, [])))}
            for h in holders
        ]

        blob = f"{vendor} {model}".upper()
        if name.startswith("bcache"):
            role = "bcache"
        elif name.startswith("nvme"):
            role = "nvme"
        elif name.startswith("md"):
            role = "md"
        elif "MR9" in blob or "MEGARAID" in blob or "AVAGO" in blob or "LSI" in blob:
            role = "vd"
        elif any(m == "/" or m.startswith("/boot") for m in mps):
            role = "system"
        else:
            role = "disk"
        out[name] = {
            "name": name,
            "role": role,
            "bus": _bus_of(name),
            "model": model or vendor or "",
            "size": size,
            "mounts": sorted(set(mps)),
            "holders": holders,
            "slaves": slaves,
            "serves": serves,
        }
    return out


def live(interval: float = 1.0) -> dict:
    """实时速率：两次采样求差。interval 决定灵敏度，1 秒对运维够用。"""
    a = _read_diskstats()
    t0 = time.time()
    time.sleep(max(0.2, min(interval, 3.0)))
    b = _read_diskstats()
    dt = time.time() - t0
    meta = device_meta()

    rows = []
    for name, cur in b.items():
        prev = a.get(name)
        if not prev:
            continue
        rate = _delta(prev, cur, dt)
        if not rate:
            continue
        rows.append({**rate, **meta.get(name, {"name": name, "role": "disk"})})

    rows.sort(key=lambda r: r["iops"], reverse=True)
    total = {
        "iops": sum(r["iops"] for r in rows),
        "read_bps": sum(r["read_bps"] for r in rows),
        "write_bps": sum(r["write_bps"] for r in rows),
    }
    busiest = max(rows, key=lambda r: r["util_percent"], default=None)
    slowest = None
    for r in rows:
        if r["iops"] >= ALERT_MIN_IOPS and (slowest is None or r["latency_ms"] > slowest["latency_ms"]):
            slowest = r
    return {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "interval": round(dt, 2),
        "rows": rows,
        "total": total,
        "busiest": busiest["name"] if busiest else "",
        "busiest_util": round(busiest["util_percent"], 1) if busiest else 0.0,
        "slowest": slowest["name"] if slowest else "",
        "slowest_latency": round(slowest["latency_ms"], 2) if slowest else 0.0,
    }


def series(hours: int = 24, devices: list[str] | None = None, limit_points: int = 400) -> dict:
    """历史速率序列。按相邻样本求差，输出每设备的时间序列。"""
    hours = max(1, min(int(hours or 24), 24 * 30))
    since = int((datetime.now() - timedelta(hours=hours)).timestamp())
    rows = db.history("io", since)
    if not rows:
        return {"hours": hours, "series": [], "devices": [], "legend": {}}

    # 同一时刻的多个设备样本聚成一张快照
    snaps: dict[int, dict[str, dict]] = {}
    for r in rows:
        ts = int(r.get("_ts_epoch") or 0)
        name = str(r.get("name") or "")
        if not ts or not name or not _NAME_RE.match(name):
            continue
        snaps.setdefault(ts, {})[name] = r

    stamps = sorted(snaps)
    out: dict[str, list[list[float]]] = {}
    for i in range(1, len(stamps)):
        prev_t, cur_t = stamps[i - 1], stamps[i]
        dt = cur_t - prev_t
        # 采样间隔异常（服务停机过久）时不要把中间那段 IO 平均进来
        if dt <= 0 or dt > 3600:
            continue
        prev, cur = snaps[prev_t], snaps[cur_t]
        for name, rec in cur.items():
            if name not in prev:
                continue
            rate = _delta(prev[name], rec, dt)
            if not rate:
                continue
            out.setdefault(name, []).append(
                [
                    cur_t,
                    round(rate["iops"], 2),
                    round(rate["read_bps"] / 1048576, 3),
                    round(rate["write_bps"] / 1048576, 3),
                    round(rate["latency_ms"], 2),
                    round(rate["util_percent"], 1),
                ]
            )

    keys = sorted(out, key=lambda n: len(out[n]), reverse=True)
    if devices:
        keep = set(devices)
        keys = [k for k in keys if k in keep]
    stride = max(1, (max((len(out[k]) for k in keys), default=0) // limit_points) + 1)
    return {
        "hours": hours,
        "devices": keys,
        # 每行形如 [时间, IOPS, 读MB/s, 写MB/s, 延迟ms, 利用率%]
        "series": [{"name": k, "points": out[k][::stride]} for k in keys],
    }


def check_alerts() -> None:
    """用最近 ALERT_WINDOW_MIN 分钟的平均值判定延迟与利用率。

    刻意不用瞬时值：一个 1 秒尖峰就告警的话，这套告警很快会被运维关掉，
    关掉之后连真故障也一起没了。
    """
    import alert_center
    import lsi_alert

    data = series(hours=1)
    since = int((datetime.now() - timedelta(minutes=ALERT_WINDOW_MIN)).timestamp())
    active = set()

    for s in data.get("series") or []:
        pts = [p for p in s["points"] if p[0] >= since]
        if len(pts) < 3:
            continue
        n = len(pts)
        avg_iops = sum(p[1] for p in pts) / n
        avg_lat = sum(p[4] for p in pts) / n
        avg_util = sum(p[5] for p in pts) / n
        name = s["name"]
        if avg_iops >= ALERT_MIN_IOPS and avg_lat >= LAT_WARN_MS:
            key = f"perf:latency:{name}"
            active.add(key)
            lsi_alert._flag(
                key,
                f"{name} 平均 IO 延迟 {avg_lat:.1f} ms",
                f"最近 {ALERT_WINDOW_MIN} 分钟平均 {avg_iops:.0f} IOPS、"
                f"利用率 {avg_util:.0f}%。延迟升高通常来自背板/线缆/控制器，"
                "而不是介质老化——先看利用率是否接近饱和。",
                "error" if avg_lat >= LAT_CRIT_MS else "warning",
            )
        if avg_util >= UTIL_WARN and avg_iops >= ALERT_MIN_IOPS:
            key = f"perf:util:{name}"
            active.add(key)
            lsi_alert._flag(
                key,
                f"{name} 利用率持续 {avg_util:.0f}%",
                f"最近 {ALERT_WINDOW_MIN} 分钟平均 {avg_iops:.0f} IOPS。"
                "磁盘接近饱和时延迟会非线性恶化，业务侧的感受就是越来越慢。",
                "warning",
            )

    try:
        for row in alert_center.list_alerts(status="open", limit=500):
            key = str(row.get("key") or "")
            if key.startswith("perf:") and key not in active:
                lsi_alert._clear(key)
    except Exception:
        pass
