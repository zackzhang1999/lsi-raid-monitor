"""容量趋势与写满预测。

"磁盘还剩多少空间"是每台存储都会被问到的问题，但真正有用的是
**"照这个速度，还有几天写满"**。只报一个使用率百分比，运维只能靠猜；
给出趋势和日期，才能提前扩容、提前归档、提前排变更窗口。

做法是每个文件系统对最近 N 天的已用量做一元线性回归：

    已用字节 ≈ a + b × 距今天数

然后按 b（日均增长）外推到 90% 与 100% 两个水位。为了让结论可用，
有三件事必须处理：

1. **伪文件系统要排除**。/snap、/proc、tmpfs 这类挂载的"已用量"要么
   恒定要么乱跳，混进来会把趋势算成一堆噪音。
2. **清理动作要处理**。运维删掉一批数据后使用量会掉下来，如果用全窗口
   拟合，斜率会被抬平甚至算成负增长，于是"明明快满了却说还很安全"。
   这里从**最后一次大幅下降之后**的样本开始拟合。
3. **样本太少不能装作有结论**。跨度不足 6 小时或样本少于 3 个时，
   只报当前使用率，不猜趋势——一个假预测比没有预测更危险。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import db
import report_mgr

# 拟合窗口与可信度门槛
DEFAULT_WINDOW_DAYS = 14
MIN_SPAN_HOURS = 6
MIN_SAMPLES = 3
# 相邻两次采样使用量下降超过这个比例，就认为发生过清理/归档
RESET_DROP_RATIO = 0.85

# 提前多久预警
DAYS_WARN = 30
DAYS_CRIT = 7
# 外推上限：超过十年就别给日期了。空文件系统上哪怕只涨了几 KB/天，
# 线性外推也能算出几十万天，硬套 timedelta 会直接溢出，而那个日期
# 对运维没有任何意义。
MAX_DAYS = 3650


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _fit(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """最小二乘拟合 y = a + b·x。返回 (a, b, r2)。

    r2 用来判断这条趋势可不可信：使用量本来就是锯齿状的（日志轮转、
    备份删除都会造成回落），r2 很低说明这个文件系统不是稳定增长型，
    那种情况下外推出来的日期没有意义，界面上要标出来。
    """
    n = len(points)
    if n < 2:
        return 0.0, 0.0, 0.0
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return sy / n, 0.0, 0.0
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    mean = sy / n
    ss_tot = sum((p[1] - mean) ** 2 for p in points)
    ss_res = sum((p[1] - (a + b * p[0])) ** 2 for p in points)
    r2 = 0.0 if ss_tot <= 1e-9 else max(0.0, 1 - ss_res / ss_tot)
    return a, b, r2


def _series(window_days: int) -> dict[str, list[dict]]:
    since = int((datetime.now() - timedelta(days=window_days)).timestamp())
    rows = db.history("fs", since)
    out: dict[str, list[dict]] = {}
    for r in rows:
        if not report_mgr.real_fs(r):
            continue
        mp = str(r.get("mountpoint") or "")
        if not mp:
            continue
        out.setdefault(mp, []).append(r)
    for mp in out:
        out[mp].sort(key=lambda x: _to_int(x.get("_ts_epoch")))
    return out


def _trim_reset(items: list[dict]) -> tuple[list[dict], str]:
    """从最后一次"大幅回落"之后开始保留样本。返回 (样本, 说明)。"""
    last_reset = 0
    for i in range(1, len(items)):
        prev = _to_int(items[i - 1].get("used_kb"))
        cur = _to_int(items[i].get("used_kb"))
        if prev > 0 and cur < prev * RESET_DROP_RATIO:
            last_reset = i
    if last_reset:
        when = datetime.fromtimestamp(_to_int(items[last_reset].get("_ts_epoch")))
        return items[last_reset:], f"检测到清理动作，自 {when:%Y-%m-%d %H:%M} 起拟合"
    return items, ""


def forecast_one(items: list[dict], now: datetime | None = None) -> dict:
    now = now or datetime.now()
    trimmed, note = _trim_reset(items)
    last = trimmed[-1] if trimmed else (items[-1] if items else None)
    if not last:
        return {}

    mp = str(last.get("mountpoint") or "")
    size = _to_int(last.get("size_kb")) * 1024
    used = _to_int(last.get("used_kb")) * 1024
    ts_last = _to_int(last.get("_ts_epoch"))
    out = {
        "mountpoint": mp,
        "device": str(last.get("device") or ""),
        "fstype": str(last.get("fstype") or ""),
        "size": size,
        "used": used,
        "avail": max(0, size - used),
        "use_percent": round(used / size * 100, 1) if size else 0.0,
        "samples": len(trimmed),
        "note": note,
        "span_hours": 0.0,
        "growth_per_day": None,
        "days_to_80": None,
        "days_to_90": None,
        "days_to_full": None,
        "eta_90": "",
        "eta_full": "",
        "r2": None,
        "trend": "unknown",
    }
    if len(trimmed) < MIN_SAMPLES or not size or used <= 0:
        return out

    t0 = _to_int(trimmed[0].get("_ts_epoch"))
    span_hours = (ts_last - t0) / 3600.0
    out["span_hours"] = round(span_hours, 1)
    if span_hours < MIN_SPAN_HOURS:
        return out

    # x 用"距首个样本的天数"，避免大数相减导致精度损失
    pts = [((_to_int(r.get("_ts_epoch")) - t0) / 86400.0, float(_to_int(r.get("used_kb")) * 1024)) for r in trimmed]
    a, b, r2 = _fit(pts)
    x_last = (ts_last - t0) / 86400.0
    out["r2"] = round(r2, 3)
    out["growth_per_day"] = round(b)

    if b <= 0 or round(b) <= 0:
        # 增长量四舍五入到 0 字节就是"基本不涨"，再外推只会给出误导性的日期
        out["trend"] = "flat"
        out["growth_per_day"] = 0
        return out
    out["trend"] = "growing"

    used_now = a + b * x_last
    for label, ratio in (("days_to_80", 0.8), ("days_to_90", 0.9), ("days_to_full", 1.0)):
        target = size * ratio
        if used_now >= target:
            out[label] = 0
            continue
        days = (target - used_now) / b
        out[label] = int(round(days)) if days <= MAX_DAYS else None
    if out["days_to_90"] is not None:
        out["eta_90"] = (now + timedelta(days=out["days_to_90"])).strftime("%Y-%m-%d")
    if out["days_to_full"] is not None:
        out["eta_full"] = (now + timedelta(days=out["days_to_full"])).strftime("%Y-%m-%d")
    return out


def forecast(window_days: int = DEFAULT_WINDOW_DAYS) -> dict:
    series = _series(window_days)
    items = [forecast_one(v) for v in series.values()]
    items = [i for i in items if i]
    items.sort(key=lambda x: (x["days_to_90"] if x["days_to_90"] is not None else 10**9))
    return {
        "window_days": window_days,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
        "risk": [i for i in items if i.get("days_to_90") is not None and i["days_to_90"] <= DAYS_WARN],
    }


def check_alerts(limit_days: int = DAYS_WARN) -> None:
    """对"预计在 limit_days 天内到 90%"的文件系统告警。"""
    import alert_center
    import lsi_alert

    data = forecast()
    active = set()
    for it in data["items"]:
        d90 = it.get("days_to_90")
        if d90 is None or d90 > limit_days:
            continue
        key = f"capacity:{it['mountpoint']}"
        active.add(key)
        # r2 低说明增长不平滑，日期仅供参考，措辞上要说清楚
        caution = (
            "近期使用量波动较大，该预测仅供参考。"
            if (it.get("r2") or 0) < 0.5
            else ""
        )
        level = "error" if d90 <= DAYS_CRIT else "warning"
        lsi_alert._flag(
            key,
            f"{it['mountpoint']} 预计 {d90} 天后达到 90% 容量",
            f"当前使用率 {it['use_percent']}%，日均增长 {_human(it.get('growth_per_day'))}，"
            f"预计 {it.get('eta_90')} 达到 90%、{it.get('eta_full')} 写满。{caution}",
            level,
        )
    try:
        for row in alert_center.list_alerts(status="open", limit=500):
            key = str(row.get("key") or "")
            if key.startswith("capacity:") and key not in active:
                lsi_alert._clear(key)
    except Exception:
        pass


def _human(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit, div in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} B"
