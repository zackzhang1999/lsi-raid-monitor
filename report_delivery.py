"""报表定时投递：把日报/周报/月报按计划发到邮箱。

为什么这件事值得单独做：报表的价值不在"能导出"，而在"不用人去点"。
一台放在机房的存储，没人会每天早上登录一次点导出——真正需要的是
周一早上打开邮箱就有一份上周的运行情况。落到产品上这是"运维闭环"的
最后一环：监控 -> 告警 -> 处理 -> 归档复盘。

三个设计取舍：

1. **按窗口键去重，而不是按"上次发送时间"**。daily 用 `2026-10-15`、
   weekly 用 ISO 周 `2026-W42`、monthly 用 `2026-10`。这样服务在 08:00
   前重启、或者一整天没开，只会在启动后补发一次，而不是重发或漏发。

2. **失败要能重试，但不能每分钟重试**。发送失败时记下 `retry_after`，
   10 分钟后才再试一次，同时把失败登记进告警中心——静默失败的定时任务
   是最难被发现的一类故障。

3. **启用时先"占位"当前窗口**。否则下午三点勾上启用，会立刻收到一份
   当天的日报；那是配置动作，不是订阅动作。想立刻验证请用"立即发送"。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import lsi_alert
import report_mgr

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
CONFIG_FILE = BASE_DIR / "report_schedule.json"

PERIODS = ("day", "week", "month")
PERIOD_LABEL = {"day": "日报", "week": "周报", "month": "月报"}
# 周期名（day/week/month）与配置里的键（daily/weekly/monthly）不是同一个词。
# 混用会让 due_periods 永远读到空配置、一封信都发不出去，所以显式映射一次。
CONFIG_KEY = {"day": "daily", "week": "weekly", "month": "monthly"}

# 发送失败后隔多久再试，避免配置错误时每分钟发一次
RETRY_MINUTES = 10
# 投递记录最多留多少条
HISTORY_MAX = 50

DEFAULT_CONFIG = {
    "enabled": False,
    # 留空时回落到告警收件人，避免"两处都要填"的重复配置
    "recipients": "",
    "daily": {"enabled": True, "time": "08:00"},
    # isoweekday: 1=周一 ... 7=周日
    "weekly": {"enabled": True, "weekday": 1, "time": "08:30"},
    "monthly": {"enabled": False, "day": 1, "time": "09:00"},
    "last_sent": {},
    "retry_after": {},
    "history": [],
}


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝，避免调用方改到默认值
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for key in DEFAULT_CONFIG:
                if key in saved:
                    cfg[key] = saved[key]
    except Exception as e:
        print(f"[report_delivery] config load error: {e}")
    return cfg


def save_config(new_cfg: dict) -> dict:
    """保存配置。启用状态从关到开时，把当前窗口记成"已发送"，避免立刻补发。"""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    old = load_config()
    # 必须在写回之前把旧状态抄下来：下面 cfg = old 是同一个对象，
    # 一旦把新值写进去，old 也被改了，"从关到开"这个边沿就再也检测不到。
    was_enabled = bool(old.get("enabled"))
    cfg = old
    for key in ("enabled", "recipients", "daily", "weekly", "monthly"):
        if key in new_cfg:
            cfg[key] = new_cfg[key]
    for key in ("last_sent", "retry_after", "history"):
        if key in new_cfg:
            cfg[key] = new_cfg[key]

    if new_cfg.get("enabled") and not was_enabled:
        now = datetime.now()
        sent = dict(cfg.get("last_sent") or {})
        for period in PERIODS:
            sent[period] = window_key(period, now)
        cfg["last_sent"] = sent
        cfg["retry_after"] = {}

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# ---- 时间窗口 ----


def window_key(period: str, now: datetime) -> str:
    if period == "day":
        return now.strftime("%Y-%m-%d")
    if period == "week":
        iso = now.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if period == "month":
        return now.strftime("%Y-%m")
    return ""


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    # 严格按 HH:MM 收口：'7:5' 也"能解析"，但存进去之后对不上
    # <input type="time"> 回填的值，界面上会看不出问题却永远不发信。
    t = str(s or "").strip()
    if len(t) != 5 or t[2] != ":" or not (t[:2] + t[3:]).isdigit():
        return None
    try:
        hh, mm = t.split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return None


def _month_last_day(now: datetime) -> int:
    nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (nxt - timedelta(days=1)).day


def valid_time(s: str) -> bool:
    """供接口层校验 HH:MM；跨模块调用公开名，不直接引私有函数。"""
    return _parse_hhmm(s) is not None


def due_periods(now: datetime, cfg: dict) -> list[str]:
    """返回当前到点且本窗口还没发过的周期。"""
    if not cfg.get("enabled"):
        return []
    last = cfg.get("last_sent") or {}
    retry = cfg.get("retry_after") or {}
    out = []

    for period in PERIODS:
        spec = cfg.get(CONFIG_KEY[period]) or {}
        if not spec.get("enabled"):
            continue
        hhmm = _parse_hhmm(spec.get("time"))
        if not hhmm:
            continue
        # 到点判定：当天该触发的时刻已过
        if (now.hour, now.minute) < hhmm:
            continue

        if period == "week":
            if now.isoweekday() != _to_int(spec.get("weekday"), 1):
                continue
        elif period == "month":
            # 配置 31 号但当月只有 30 天时，落到当月最后一天，而不是永不触发
            day = min(max(1, _to_int(spec.get("day"), 1)), _month_last_day(now))
            if now.day != day:
                continue

        key = window_key(period, now)
        if last.get(period) == key:
            continue
        ra = retry.get(period)
        if ra:
            try:
                if now < datetime.fromisoformat(str(ra)):
                    continue
            except ValueError:
                pass
        out.append(period)
    return out


# ---- 发送 ----


def _recipients(cfg: dict) -> list[str]:
    own = [r.strip() for r in str(cfg.get("recipients") or "").split(",") if r.strip()]
    if own:
        return own
    alert_cfg = lsi_alert.load_config()
    return [
        r.strip()
        for r in str(alert_cfg.get("alert_email_to") or "").split(",")
        if r.strip()
    ]


def _plain_summary(report: dict, period: str) -> str:
    """纯文本版本。有些邮件客户端拦 HTML，正文不能只有一个 HTML 部分。"""
    m = report["meta"]
    raid = report.get("raid") or {}
    disks = report.get("disks") or {}
    alerts = report.get("alerts") or {}
    vds = raid.get("vds") or []
    degraded = [v for v in vds if not v.get("healthy")]
    worn = disks.get("worn") or []
    # worn 里只挑真正有坏道计数的，全是 0 的盘列出来反而是噪音
    flagged = [
        d
        for d in worn
        if d.get("reallocated") or d.get("pending") or d.get("uncorrectable")
    ]
    lines = [
        f"DataMax-LSI {PERIOD_LABEL.get(period, period)} — {m.get('host', '')}",
        f"统计区间：{m.get('label', '')}（{m.get('title', '')}）",
        f"生成时间：{m.get('generated_at', '')}",
        "",
        f"控制器：{len(report.get('health', {}).get('controllers') or [])} 张",
        f"虚拟盘：{len(vds)} 个"
        + (f"，其中 {len(degraded)} 个非最优" if degraded else "，全部正常"),
        f"物理盘：{disks.get('total', 0)} 块，有坏道计数 {len(flagged)} 块",
        f"告警：本区间新增 {alerts.get('opened', 0)} 条，恢复 {alerts.get('resolved', 0)} 条，"
        f"当前未恢复 {alerts.get('open_now', 0)} 条",
    ]
    if degraded:
        lines.append("")
        lines.append("需要关注的虚拟盘：")
        for v in degraded:
            lines.append(f"  - {v.get('dg_vd') or v.get('vd')}：{v.get('state')}")
    if flagged:
        lines.append("")
        lines.append("需要关注的物理盘：")
        for d in flagged[:10]:
            lines.append(
                f"  - {d.get('label') or ''} {d.get('model') or ''}："
                f"重映射 {d.get('reallocated', 0)} / 待定 {d.get('pending', 0)}"
                f" / 无法纠正 {d.get('uncorrectable', 0)}"
            )
    lines += ["", "详细内容见 HTML 版本，或登录控制台「运行报表」查看。"]
    return "\n".join(lines)


def send_report(
    period: str, cfg: dict | None = None, status: dict | None = None
) -> tuple[bool, str]:
    """生成并发送一份报表。返回 (是否成功, 说明)。"""
    cfg = cfg or load_config()
    recipients = _recipients(cfg)
    if not recipients:
        return False, "未配置收件人（报表收件人与告警收件人都为空）"

    alert_cfg = lsi_alert.load_config()
    sendmail = alert_cfg.get("sendmail_path") or "/usr/sbin/sendmail"
    if not (os.path.isfile(sendmail) and os.access(sendmail, os.X_OK)):
        return False, f"sendmail 不可用: {sendmail}"

    report = report_mgr.build_report(period, status)
    html = report_mgr.render_html(report)
    text = _plain_summary(report, period)

    host = socket.gethostname()
    m = EmailMessage()
    m["From"] = f"lsi-raid-monitor@{host}"
    m["To"] = ", ".join(recipients)
    m["Subject"] = (
        f"[DataMax-LSI] {host} {PERIOD_LABEL.get(period, period)} · "
        f"{report['meta'].get('label', '')}"
    )
    m.set_content(text)
    m.add_alternative(html, subtype="html")
    stamp = report["meta"].get("generated_at", "").replace(":", "").replace(" ", "-")
    try:
        m.add_attachment(
            report_mgr.render_csv(report).encode("utf-8-sig"),
            maintype="text",
            subtype="csv",
            filename=f"datamax-lsi-{period}-{stamp}.csv",
        )
    except Exception:
        pass

    try:
        proc = subprocess.run(
            [sendmail, "-t", "-oi"],
            input=m.as_string().encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        if proc.returncode == 0:
            return True, f"已发送至 {', '.join(recipients)}"
        return False, proc.stderr.decode("utf-8", "ignore").strip() or "sendmail 返回非零"
    except Exception as e:
        return False, str(e)


def _record(cfg: dict, period: str, ok: bool, msg: str, trigger: str) -> dict:
    entry = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "period": period,
        "label": PERIOD_LABEL.get(period, period),
        "ok": bool(ok),
        "message": msg,
        "trigger": trigger,
    }
    hist = list(cfg.get("history") or [])
    hist.insert(0, entry)
    cfg["history"] = hist[:HISTORY_MAX]
    return entry


def deliver(period: str, trigger: str = "定时", status: dict | None = None) -> dict:
    """执行一次投递并落库状态（成功/失败都记）。"""
    cfg = load_config()
    ok, msg = send_report(period, cfg, status)
    now = datetime.now()
    key = window_key(period, now)

    sent = dict(cfg.get("last_sent") or {})
    retry = dict(cfg.get("retry_after") or {})
    label = PERIOD_LABEL.get(period, period)
    if ok:
        sent[period] = key
        retry.pop(period, None)
        lsi_alert.log_event("info", f"{label}投递成功（{trigger}）：{msg}")
        try:
            lsi_alert._clear(f"reports:delivery:{period}")
        except Exception:
            pass
    else:
        # 失败不写 last_sent：下一轮（10 分钟后）会重试，而不是丢掉这份报表
        retry[period] = (now + timedelta(minutes=RETRY_MINUTES)).isoformat(timespec="seconds")
        lsi_alert.log_event("warning", f"{label}投递失败（{trigger}）：{msg}")
        try:
            lsi_alert._flag(
                f"reports:delivery:{period}",
                f"{label}邮件投递失败",
                f"原因：{msg}。系统会每 {RETRY_MINUTES} 分钟自动重试，"
                "请检查收件人配置与 MTA（sendmail）是否可用。",
                "warning",
            )
        except Exception:
            pass

    entry = _record(cfg, period, ok, msg, trigger)
    cfg["last_sent"] = sent
    cfg["retry_after"] = retry
    save_config(cfg)
    return {"ok": ok, "message": msg, "period": period, "entry": entry}


def run_due(now: datetime | None = None) -> list[dict]:
    """投递所有到点的周期。每轮只处理一个，避免同时涌出三封邮件。"""
    now = now or datetime.now()
    cfg = load_config()
    due = due_periods(now, cfg)
    if not due:
        return []
    return [deliver(due[0], trigger="定时")]


def schedule_view() -> dict:
    """给界面用：各周期的配置与最近一次投递情况。"""
    cfg = load_config()
    now = datetime.now()
    out = {}
    for period in PERIODS:
        spec = cfg.get(CONFIG_KEY[period]) or {}
        out[period] = {
            "enabled": bool(spec.get("enabled")),
            "time": spec.get("time", ""),
            "weekday": _to_int(spec.get("weekday"), 1),
            "day": _to_int(spec.get("day"), 1),
            "last_sent": (cfg.get("last_sent") or {}).get(period, ""),
            "window_key": window_key(period, now),
            "retry_after": (cfg.get("retry_after") or {}).get(period, ""),
            "valid_time": bool(_parse_hhmm(spec.get("time"))),
        }
    return {"config": cfg, "periods": out}
