#!/usr/bin/env python3
# ================================================
# LSI MegaRAID 健康报告 + 可选邮件发送
# 统计过去 24 小时数据，生成温度折线图并以 HTML 报告输出。
# 邮件发送为可选项：仅当 SMTP_USER 等环境变量配置后才会发送。
#
# 环境变量:
#   SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM / SMTP_TO
#   LSI_DATA_DIR   - 数据目录 (默认 /var/lib/lsi-monitor/data)
# ================================================

from __future__ import annotations

import csv
import os
import io
import sys
import base64
import smtplib
import shutil
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

# ---- 配置 ----
PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
CHART_DIR = BASE_DIR.parent / "charts"
CHART_DIR.mkdir(parents=True, exist_ok=True)

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.example.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_TO = os.environ.get("SMTP_TO", SMTP_FROM)

TEMP_WARN = int(os.environ.get("TEMP_WARN", "45"))
TEMP_CRIT = int(os.environ.get("TEMP_CRIT", "50"))

COLORS = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#7f7f7f",
    "#bcbd22",
    "#17becf",
    "#ff6384",
    "#36a2eb",
    "#cc65fe",
    "#ffce56",
    "#4bc0c0",
    "#9966ff",
]

CJK_FONT_CANDIDATES = [
    os.path.expanduser("~/.local/share/fonts/wqy-microhei.ttc"),
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
]


# ---- matplotlib ----


def setup_matplotlib():
    try:
        import matplotlib
    except ImportError:
        print(
            "[warning] matplotlib not installed; temperature chart will be skipped",
            file=sys.stderr,
        )
        return None, None

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm

    font_path = None
    for path in CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            font_path = path
            break
    if font_path:
        fm.fontManager.addfont(font_path)
        prop = fm.FontProperties(fname=font_path)
        font_name = prop.get_name()
        plt.rcParams["font.family"] = "sans-serif"
        sans_list = list(plt.rcParams.get("font.sans-serif", ["DejaVu Sans"]))
        if font_name not in sans_list:
            sans_list.insert(0, font_name)
        plt.rcParams["font.sans-serif"] = sans_list
        plt.rcParams["axes.unicode_minus"] = False
        return plt, prop
    else:
        plt.rcParams["font.family"] = "sans-serif"
        return plt, None


# ---- 时间窗口 ----


def _date_dirs_in_range(start: datetime, end: datetime) -> list[Path]:
    dirs = []
    d = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
    while d <= end_day:
        p = BASE_DIR / d.strftime("%Y-%m-%d")
        if p.exists():
            dirs.append(p)
        d += timedelta(days=1)
    return dirs


# ---- 数据读取 ----


def _read_csv_filtered(
    dir_paths: list[Path], filename: str, ts_min: str, ts_max: str
) -> list[dict]:
    rows = []
    for dp in dir_paths:
        fp = dp / filename
        if not fp.exists():
            continue
        with open(fp, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ts = row.get("timestamp", "")
                if ts_min <= ts <= ts_max:
                    rows.append(row)
    return rows


def read_disks(ts_min: str, ts_max: str) -> list[dict]:
    rows = _read_csv_filtered(
        _date_dirs_in_range(
            datetime.strptime(ts_min[:10], "%Y-%m-%d"),
            datetime.strptime(ts_max[:10], "%Y-%m-%d"),
        ),
        "disks.csv",
        ts_min,
        ts_max,
    )
    out = []
    for row in rows:
        if row.get("state", "").strip() == "N/A":
            continue
        try:
            row["temperature"] = (
                int(row["temperature"]) if row.get("temperature", "").strip() else None
            )
        except (ValueError, TypeError):
            row["temperature"] = None
        for fld in (
            "media_error",
            "other_error",
            "predictive_failure",
            "shield_counter",
        ):
            try:
                row[fld] = int(row.get(fld, 0) or 0)
            except (ValueError, TypeError):
                row[fld] = 0
        try:
            row["eid"] = int(row.get("eid", 0))
            row["slot"] = int(row.get("slot", 0))
        except (ValueError, TypeError):
            pass
        out.append(row)
    return out


def read_controller(ts_min: str, ts_max: str) -> dict | None:
    rows = _read_csv_filtered(
        _date_dirs_in_range(
            datetime.strptime(ts_min[:10], "%Y-%m-%d"),
            datetime.strptime(ts_max[:10], "%Y-%m-%d"),
        ),
        "controller.csv",
        ts_min,
        ts_max,
    )
    last = None
    for row in rows:
        if row.get("health", "").strip() not in ("N/A", ""):
            last = row
    if last:
        try:
            last["bbu_temperature"] = int(last.get("bbu_temperature", 0) or 0) or None
        except (ValueError, TypeError):
            last["bbu_temperature"] = None
        try:
            last["num_vds"] = int(last.get("num_vds", 0) or 0)
            last["num_disks"] = int(last.get("num_disks", 0) or 0)
        except (ValueError, TypeError):
            pass
    return last


def read_vds(date_dir: Path) -> list[dict]:
    fp = date_dir / "vds.csv"
    if not fp.exists():
        return []
    seen = set()
    rows = []
    with open(fp, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("state", "").strip() == "N/A":
                continue
            key = row.get("dg_vd", row.get("name", ""))
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return rows


def read_one(date_dir: Path, filename: str) -> dict | None:
    fp = date_dir / filename
    if not fp.exists():
        return None
    last = None
    with open(fp, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            last = row
    return last


def read_smart(date_dir: Path) -> dict[int, dict]:
    fp = date_dir / "smart.csv"
    if not fp.exists():
        return {}
    result = {}
    with open(fp, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            did = int(row.get("did", 0) or 0)
            for k in ("reallocated", "pending", "uncorrectable", "power_on_hours"):
                try:
                    row[k] = int(row.get(k, 0) or 0)
                except ValueError:
                    row[k] = 0
            try:
                row["smart_temp"] = (
                    int(row["smart_temp"])
                    if row.get("smart_temp", "").strip()
                    else None
                )
            except (ValueError, TypeError):
                row["smart_temp"] = None
            result[did] = row
    return result


def read_attributes(date_dir: Path) -> dict[tuple, dict]:
    fp = date_dir / "attributes.csv"
    if not fp.exists():
        return {}
    result = {}
    with open(fp, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                key = (int(row.get("eid", 0)), int(row.get("slot", 0)))
                result[key] = row
            except (ValueError, TypeError):
                pass
    return result


def read_system(date_dir: Path) -> dict | None:
    return read_one(date_dir, "system.csv")


# ---- 图表 ----


def make_temperature_chart(
    plt, font_prop, disks_data: list[dict], ctrl_data: dict | None, label_str: str
) -> str | None:
    if not disks_data:
        return None

    groups = defaultdict(list)
    for row in disks_data:
        groups[(row.get("eid"), row.get("slot"))].append(row)
    if not groups:
        return None

    fig, ax = plt.subplots(figsize=(14, 6))
    x_vals_all = []
    color_idx = 0

    for (eid, slot), rows in sorted(groups.items()):
        rows.sort(key=lambda r: r.get("timestamp", ""))
        times = [r.get("timestamp", "")[5:16] for r in rows]
        valid = [
            (t, r.get("temperature"))
            for t, r in zip(times, rows)
            if r.get("temperature") is not None
        ]
        if not valid:
            continue
        x_v = [v[0] for v in valid]
        y_v = [v[1] for v in valid]
        x_vals_all = x_v
        color = COLORS[color_idx % len(COLORS)]
        model = rows[0].get("model", "")[:20] if rows[0].get("model") else ""
        label = f"E{eid}:S{slot} ({model})" if model else f"E{eid}:S{slot}"
        ax.plot(
            x_v,
            y_v,
            color=color,
            linewidth=1.2,
            marker=".",
            markersize=1,
            label=label,
            alpha=0.85,
        )
        color_idx += 1

    if ctrl_data and ctrl_data.get("bbu_temperature"):
        ax.axhline(
            y=ctrl_data["bbu_temperature"],
            color="purple",
            linestyle=":",
            linewidth=1,
            alpha=0.7,
            label=f"BBU {ctrl_data.get('bbu_model','')} {ctrl_data['bbu_temperature']}C",
        )

    ax.axhline(
        y=TEMP_WARN,
        color="orange",
        linestyle="--",
        linewidth=1,
        alpha=0.7,
        label=f"WARN {TEMP_WARN}C",
    )
    ax.axhline(
        y=TEMP_CRIT,
        color="red",
        linestyle="--",
        linewidth=1,
        alpha=0.7,
        label=f"CRIT {TEMP_CRIT}C",
    )

    title = f"LSI RAID Disk Temperature — {label_str}"
    if font_prop:
        ax.set_title(title, fontproperties=font_prop, fontsize=14)
        ax.set_xlabel("Time", fontproperties=font_prop, fontsize=11)
        ax.set_ylabel("Temp (C)", fontproperties=font_prop, fontsize=11)
    else:
        ax.set_title(title, fontsize=14)
        ax.set_xlabel("Time", fontsize=11)
        ax.set_ylabel("Temp (C)", fontsize=11)

    ax.legend(loc="upper left", fontsize=8, ncol=2, framealpha=0.6)
    ax.grid(True, alpha=0.3)
    if x_vals_all and len(x_vals_all) > 30:
        step = len(x_vals_all) // 24
        tick_positions = list(range(0, len(x_vals_all), max(step, 1)))
        ax.set_xticks([x_vals_all[i] for i in tick_positions if i < len(x_vals_all)])
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=7)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


# ---- 摘要 ----


def build_summary(
    disks_data,
    ctrl_data,
    vds_data,
    patrol_data,
    cc_data,
    smart_data=None,
    attr_data=None,
    sys_data=None,
):
    if smart_data is None:
        smart_data = {}
    if attr_data is None:
        attr_data = {}
    summary = {
        "controller_model": "",
        "controller_fw": "",
        "controller_health": "No Data",
        "num_vds": 0,
        "num_disks": 0,
        "bbu_model": "",
        "bbu_state": "",
        "bbu_temperature": None,
        "vd_details": [],
        "disk_details": [],
        "has_warning": False,
        "has_critical": False,
        "has_offline": False,
        "has_smart_alert": False,
        "has_media_error": False,
        "has_smart_issue": False,
        "pr_next": "",
        "pr_state": "",
        "pr_mode": "",
        "pr_iterations": 0,
        "cc_next": "",
        "cc_state": "",
        "cc_mode": "",
        "cc_iterations": 0,
        "sys_load": "",
        "sys_mem": "",
    }

    if ctrl_data:
        summary["controller_model"] = ctrl_data.get("model", "")
        summary["controller_fw"] = ctrl_data.get("fw_version", "")
        summary["controller_health"] = ctrl_data.get("health", "No Data")
        summary["num_vds"] = ctrl_data.get("num_vds", 0)
        summary["num_disks"] = ctrl_data.get("num_disks", 0)
        summary["bbu_model"] = ctrl_data.get("bbu_model", "")
        summary["bbu_state"] = ctrl_data.get("bbu_state", "")
        summary["bbu_temperature"] = ctrl_data.get("bbu_temperature")
    if not summary["controller_health"]:
        summary["controller_health"] = "No Data"

    # VD
    for vd in vds_data:
        state = vd.get("state", "").strip()
        if state not in ("Optl", "Optimal", "Opt"):
            summary["has_warning"] = True
        summary["vd_details"].append(
            {
                "dg_vd": vd.get("dg_vd", ""),
                "type": vd.get("type", ""),
                "state": state,
                "size": vd.get("size", ""),
                "name": vd.get("name", ""),
            }
        )

    # 磁盘
    disk_groups = defaultdict(
        lambda: {
            "model": "",
            "dg": "",
            "temps": [],
            "states": set(),
            "max_temp": None,
            "media_error": 0,
            "other_error": 0,
            "predictive_failure": 0,
            "smart_alert": "No",
            "shield_counter": 0,
        }
    )
    for row in disks_data:
        key = (row.get("eid"), row.get("slot"))
        dg = disk_groups[key]
        dg["model"] = row.get("model", "")
        dg["dg"] = row.get("dg", "")
        temp = row.get("temperature")
        if temp is not None:
            dg["temps"].append(temp)
            if dg["max_temp"] is None or temp > dg["max_temp"]:
                dg["max_temp"] = temp
        state = row.get("state", "").strip()
        if state:
            dg["states"].add(state)
        dg["media_error"] = max(dg["media_error"], row.get("media_error", 0))
        dg["other_error"] = max(dg["other_error"], row.get("other_error", 0))
        dg["predictive_failure"] = max(
            dg["predictive_failure"], row.get("predictive_failure", 0)
        )
        dg["shield_counter"] = max(dg["shield_counter"], row.get("shield_counter", 0))
        if str(row.get("smart_alert", "No")).strip().lower() == "yes":
            dg["smart_alert"] = "Yes"

    for (eid, slot), dg in sorted(disk_groups.items()):
        min_t = min(dg["temps"]) if dg["temps"] else None
        max_t = dg["max_temp"]
        avg_t = round(sum(dg["temps"]) / len(dg["temps"]), 1) if dg["temps"] else None
        states = ", ".join(sorted(dg["states"])) if dg["states"] else "?"

        if max_t is not None and max_t >= TEMP_CRIT:
            summary["has_critical"] = True
        if max_t is not None and max_t >= TEMP_WARN:
            summary["has_warning"] = True
        if any(
            s.lower() in ("offline", "failed", "missing", "ubad") for s in dg["states"]
        ):
            summary["has_offline"] = True
        if dg["smart_alert"] == "Yes":
            summary["has_smart_alert"] = True
        if (
            dg["media_error"] > 0
            or dg["other_error"] > 0
            or dg["predictive_failure"] > 0
        ):
            summary["has_media_error"] = True

        attr = attr_data.get((eid, slot), {})
        sn = attr.get("sn", "")
        fw_rev = attr.get("fw_rev", "")
        dev_speed = attr.get("dev_speed", "")
        link_speed = attr.get("link_speed", "")

        did = _find_latest_did(disks_data, eid, slot)
        smart = smart_data.get(did, {}) if did is not None else {}
        realloc = smart.get("reallocated", 0)
        pending = smart.get("pending", 0)
        uncorrect = smart.get("uncorrectable", 0)
        poh = smart.get("power_on_hours", 0)
        smart_fail = realloc < 0

        if realloc > 0 or pending > 0 or uncorrect > 0:
            summary["has_smart_issue"] = True

        summary["disk_details"].append(
            {
                "label": f"E{eid}:S{slot}",
                "eid": eid,
                "slot": slot,
                "model": dg["model"],
                "dg": dg["dg"],
                "state": states,
                "temp_min": min_t,
                "temp_max": max_t,
                "temp_avg": avg_t,
                "media_error": dg["media_error"],
                "other_error": dg["other_error"],
                "predictive_failure": dg["predictive_failure"],
                "smart_alert": dg["smart_alert"],
                "shield_counter": dg["shield_counter"],
                "sn": sn,
                "fw_rev": fw_rev,
                "dev_speed": dev_speed,
                "link_speed": link_speed,
                "reallocated": realloc,
                "pending": pending,
                "uncorrectable": uncorrect,
                "power_on_hours": poh,
                "smart_fail": smart_fail,
            }
        )

    # 巡读
    if patrol_data:
        summary["pr_next"] = patrol_data.get("pr_next", "")
        summary["pr_state"] = patrol_data.get("pr_state", "")
        summary["pr_mode"] = patrol_data.get("pr_mode", "")
        summary["pr_iterations"] = patrol_data.get("pr_iterations", 0)

    # CC
    if cc_data:
        summary["cc_next"] = cc_data.get("cc_next", "")
        summary["cc_state"] = cc_data.get("cc_state", "")
        summary["cc_mode"] = cc_data.get("cc_mode", "")
        summary["cc_iterations"] = cc_data.get("cc_iterations", 0)

    # 系统信息
    if sys_data:
        try:
            ld = sys_data.get("load_1m", "")
            summary["sys_load"] = (
                f'{ld} / {sys_data.get("load_5m","")} / {sys_data.get("load_15m","")}'
            )
        except Exception:
            summary["sys_load"] = ""
        try:
            total_gb = int(sys_data.get("mem_total_kb", 0)) / (1024 * 1024)
            avail_gb = int(sys_data.get("mem_avail_kb", 0)) / (1024 * 1024)
            used_pct = round((1 - avail_gb / total_gb) * 100) if total_gb > 0 else 0
            summary["sys_mem"] = (
                f"{avail_gb:.1f}G avail / {total_gb:.1f}G total ({used_pct}% used)"
            )
        except Exception:
            summary["sys_mem"] = ""

    return summary


def _find_latest_did(disks_data: list[dict], eid: int, slot: int) -> int:
    """从 disks_data 中返回指定 (eid, slot) 最新的 did。"""
    latest = None
    for row in disks_data:
        if row.get("eid") == eid and row.get("slot") == slot:
            if latest is None or row.get("timestamp", "") > latest.get("timestamp", ""):
                latest = row
    if not latest:
        return 0
    try:
        return int(latest.get("did", 0) or 0)
    except (ValueError, TypeError):
        return 0


# ---- 文本摘要 ----


def build_text_summary(summary: dict, label_str: str) -> str:
    """生成可直接打印到终端的纯文本摘要。"""
    lines = []
    host = os.uname().nodename
    lines.append(f"LSI RAID Report — {host}")
    lines.append(f"Range: {label_str}")

    if (
        summary["has_critical"]
        or summary["has_offline"]
        or summary["has_smart_alert"]
        or summary["has_smart_issue"]
    ):
        status = "[ ERROR ]"
    elif summary["has_warning"] or summary["has_media_error"]:
        status = "[ WARN ]"
    elif summary["controller_health"] == "No Data":
        status = "[ NO DATA ]"
    else:
        status = "[ OK ]"
    lines.append(f"Status: {status}")
    lines.append("")

    lines.append("Controller:")
    lines.append(f"  Model: {summary['controller_model'] or '—'}")
    lines.append(f"  Firmware: {summary['controller_fw'] or '—'}")
    lines.append(f"  Health: {summary['controller_health']}")
    lines.append(f"  Physical Disks: {summary['num_disks']} drives")
    lines.append(f"  Virtual Disks: {summary['num_vds']} VDs")
    if summary["bbu_model"]:
        bt = f"{summary['bbu_temperature']}C" if summary["bbu_temperature"] else "—"
        lines.append(
            f"  BBU: {summary['bbu_model']} / {summary['bbu_state']} / {bt}"
        )
    if summary["pr_next"]:
        lines.append(
            f"  Patrol Read: Next {summary['pr_next']} | {summary['pr_state']} | "
            f"{summary['pr_mode']} | {summary['pr_iterations']} runs"
        )
    if summary["cc_next"]:
        lines.append(
            f"  Consistency Check: Next {summary['cc_next']} | {summary['cc_state']} | "
            f"{summary['cc_mode']} | {summary['cc_iterations']} runs"
        )
    if summary["sys_load"]:
        lines.append(f"  Load: {summary['sys_load']}")
    if summary["sys_mem"]:
        lines.append(f"  Memory: {summary['sys_mem']}")
    lines.append("")

    lines.append("Virtual Disks:")
    if summary["vd_details"]:
        lines.append(
            f"  {'DG/VD':<10} {'Name':<12} {'RAID':<8} {'Size':<10} State"
        )
        for vd in summary["vd_details"]:
            lines.append(
                f"  {vd['dg_vd']:<10} {vd['name']:<12} {vd['type']:<8} "
                f"{vd['size']:<10} {vd['state']}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    lines.append("Physical Disks:")
    if summary["disk_details"]:
        lines.append(
            f"  {'Slot':<10} {'Model':<20} {'State':<12} {'Temp C':<14} "
            f"{'STOR':<20} {'SMART':<20} POH"
        )
        for d in summary["disk_details"]:
            temp = (
                f"{d['temp_min']} / {d['temp_max']} / {d['temp_avg']}"
                if d["temp_min"] is not None
                else "—"
            )
            ep = (
                f"ME:{d['media_error']} OE:{d['other_error']} "
                f"PF:{d['predictive_failure']}"
            )
            smart = (
                f"R:{d['reallocated']} P:{d['pending']} U:{d['uncorrectable']}"
            )
            poh = d.get("power_on_hours", 0)
            poh_str = f"{poh}h ({poh // 24}d)" if poh > 0 else "—"
            lines.append(
                f"  {d['label']:<10} {d['model'][:18]:<20} {d['state']:<12} "
                f"{temp:<14} {ep:<20} {smart:<20} {poh_str}"
            )
    else:
        lines.append("  (none)")

    return "\n".join(lines)


# ---- HTML 邮件 ----


def build_html_email(
    summary: dict, chart_b64: str | None, label_str: str, inline_chart: bool = False
) -> str:

    if (
        summary["has_critical"]
        or summary["has_offline"]
        or summary["has_smart_alert"]
        or summary["has_smart_issue"]
    ):
        status = '<span style="color:#d00;font-weight:bold">[ ERROR ]</span>'
    elif summary["has_warning"] or summary["has_media_error"]:
        status = '<span style="color:#e68a00;font-weight:bold">[ WARN ]</span>'
    elif summary["controller_health"] == "No Data":
        status = '<span style="color:#888">[ NO DATA ]</span>'
    else:
        status = '<span style="color:#2a2">[ OK ]</span>'

    def _row(label, value, vc=None):
        c = f"color:{vc};" if vc else ""
        return f'<tr><td class="k">{label}</td><td style="{c}">{value}</td></tr>'

    def _state_color(s):
        return "#d00" if s not in ("Optimal", "Optl") else "inherit"

    ctrl_hc = _state_color(summary["controller_health"])

    ctrl_rows = ""
    ctrl_rows += _row("Model", summary["controller_model"], None)
    ctrl_rows += _row("Firmware", summary["controller_fw"], None)
    ctrl_rows += _row("Health", f'<b>{summary["controller_health"]}</b>', ctrl_hc)
    ctrl_rows += _row("Physical Disks", f'{summary["num_disks"]} drives', None)
    ctrl_rows += _row("Virtual Disks", f'{summary["num_vds"]} VDs', None)

    if summary["bbu_model"]:
        bs = _state_color(summary["bbu_state"])
        bt = f'{summary["bbu_temperature"]}C' if summary["bbu_temperature"] else "—"
        ctrl_rows += _row(
            "BBU", f'{summary["bbu_model"]} / <b>{summary["bbu_state"]}</b> / {bt}', bs
        )

    if summary["pr_next"]:
        ps = _state_color("Optimal" if "Active" in summary["pr_state"] else "Degraded")
        ctrl_rows += _row(
            "Patrol Read",
            f'Next {summary["pr_next"]} | {summary["pr_state"]} | {summary["pr_mode"]} | {summary["pr_iterations"]} runs',
            ps,
        )
    if summary["cc_next"]:
        cs = _state_color("Optimal" if "Active" in summary["cc_state"] else "Degraded")
        ctrl_rows += _row(
            "Consistency Check",
            f'Next {summary["cc_next"]} | {summary["cc_state"]} | {summary["cc_mode"]} | {summary["cc_iterations"]} runs',
            cs,
        )

    ctrl_rows += _row("Load", summary.get("sys_load", "—"), None)
    ctrl_rows += _row("Memory", summary.get("sys_mem", "—"), None)

    # VD table
    vd_rows = ""
    for vd in summary["vd_details"]:
        vs = _state_color(
            "Optimal" if vd["state"] in ("Optl", "Optimal") else "Degraded"
        )
        vd_rows += f'<tr><td>{vd["dg_vd"]}</td><td>{vd["name"]}</td><td>{vd["type"]}</td><td>{vd["size"]}</td><td style="color:{vs}"><b>{vd["state"]}</b></td></tr>'

    # Disk table
    disk_rows = ""
    for d in summary["disk_details"]:
        tmax = d["temp_max"] or 0
        tc = (
            "#d00"
            if tmax >= TEMP_CRIT
            else ("#e68a00" if tmax >= TEMP_WARN else "inherit")
        )
        ts = (
            f'{d["temp_min"]} / {d["temp_max"]} / <b style="color:{tc}">{d["temp_avg"]}C</b>'
            if d["temp_min"] is not None
            else "—"
        )

        ds = (
            "#d00"
            if any(
                s in d["state"].lower()
                for s in ("offline", "failed", "missing", "degraded")
            )
            else "inherit"
        )

        ep = []
        for lbl, key in [
            ("ME", "media_error"),
            ("OE", "other_error"),
            ("PF", "predictive_failure"),
        ]:
            v = d[key]
            ep.append(
                f'<span style="color:#d00;font-weight:bold">{lbl}:{v}</span>'
                if v > 0
                else f"{lbl}:0"
            )
        err_str = " ".join(ep)

        smart_sp = []
        for lbl, key in [
            ("Realloc", "reallocated"),
            ("Pending", "pending"),
            ("Uncorr", "uncorrectable"),
        ]:
            v = d.get(key, 0)
            if d.get("smart_fail"):
                smart_sp.append(f"{lbl}:?")
            elif v > 0:
                smart_sp.append(
                    f'<span style="color:#d00;font-weight:bold">{lbl}:{v}</span>'
                )
            else:
                smart_sp.append(f"{lbl}:0")
        smart_sector_str = " ".join(smart_sp)

        poh = d.get("power_on_hours", 0)
        if poh > 0:
            poh_str = f"{poh}h ({poh // 24}d)"
        elif d.get("smart_fail"):
            poh_str = "?"
        else:
            poh_str = "—"

        ss = "#d00" if d["smart_alert"] == "Yes" else "inherit"
        smart_str = f'<b style="color:{ss}">{d["smart_alert"]}</b>'

        sn = d.get("sn", "")[:12] if d.get("sn") else ""
        fw = d.get("fw_rev", "")

        disk_rows += f'<tr><td>{d["label"]}</td><td>{d["model"]}</td><td>{sn}</td><td>{fw}</td><td>{d["dg"]}</td><td style="color:{ds}"><b>{d["state"]}</b></td><td>{ts}</td><td>{err_str}</td><td>{smart_sector_str}</td><td>{poh_str}</td><td>{smart_str}</td></tr>'

    chart_html = ""
    if chart_b64:
        if inline_chart:
            chart_html = f'<div style="margin-top:18px"><img src="data:image/png;base64,{chart_b64}" style="max-width:100%;border:1px solid #ccc"></div>'
        else:
            chart_html = f'<div style="margin-top:18px"><img src="cid:temp_chart" style="max-width:100%;border:1px solid #ccc"></div>'

    host = os.uname().nodename

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: "Helvetica Neue", Arial, sans-serif; font-size:13px; color:#222; background:#eee; padding:18px }}
  .wrap {{ max-width:960px; margin:0 auto; background:#fff; border:1px solid #d0d0d0 }}
  .hdr {{ padding:14px 22px; border-bottom:2px solid #333 }}
  .hdr h1 {{ margin:0; font-size:18px; font-weight:600 }}
  .hdr p  {{ margin:4px 0 0; color:#666; font-size:12px }}
  .sec {{ padding:12px 22px; border-bottom:1px solid #eee }}
  .sec h2 {{ margin:0 0 8px; font-size:14px; font-weight:600; color:#333 }}
  table.detail {{ width:100%; border-collapse:collapse; font-size:12px }}
  table.detail td.k {{ width:100px; color:#666; padding:3px 10px 3px 0; vertical-align:top }}
  table.detail td {{ padding:3px 0 }}
  table.grid {{ width:100%; border-collapse:collapse; font-size:12px }}
  table.grid th {{ background:#555; color:#fff; padding:4px 6px; text-align:left; font-weight:500 }}
  table.grid td {{ padding:4px 6px; border-bottom:1px solid #e8e8e8 }}
  table.grid tr:hover td {{ background:#fafafa }}
  .ft {{ padding:10px 22px; color:#999; font-size:11px }}
</style>
</head>
<body>
<div class="wrap">

<div class="hdr">
  <h1>LSI RAID {host}</h1>
  <p>{label_str} &nbsp; Status: {status}</p>
</div>

<div class="sec">
  <h2>Controller</h2>
  <table class="detail">{ctrl_rows}</table>
</div>

<div class="sec">
  <h2>Virtual Disks</h2>
  <table class="grid">
    <tr><th>DG/VD</th><th>Name</th><th>RAID</th><th>Size</th><th>State</th></tr>
    {vd_rows}
  </table>
</div>

<div class="sec">
  <h2>Physical Disks</h2>
  <table class="grid">
    <tr><th>Slot</th><th>Model</th><th>SN</th><th>FW</th><th>DG</th><th>State</th><th>Temp C</th><th>STOR</th><th>SMART</th><th>POH</th><th>Alert</th></tr>
    {disk_rows}
  </table>
</div>

{chart_html}

<div class="ft">lsi-report.py &nbsp;|&nbsp; {datetime.now().strftime("%Y-%m-%d %H:%M")}</div>

</div>
</body>
</html>"""
    return html


def create_mime_message(
    html: str, chart_b64: str | None, label_str: str
) -> MIMEMultipart:
    msg = MIMEMultipart("related")
    msg["Subject"] = f"LSI RAID Report — {label_str}"
    msg["From"] = SMTP_FROM
    msg["To"] = SMTP_TO
    alt = MIMEMultipart("alternative")
    msg.attach(alt)
    alt.attach(MIMEText(html, "html", "utf-8"))
    if chart_b64:
        img = MIMEImage(base64.b64decode(chart_b64))
        img.add_header("Content-ID", "<temp_chart>")
        img.add_header("Content-Disposition", "inline", filename="temperature.png")
        msg.attach(img)
    return msg


def cleanup_old_data(retention_days: int = 30):
    if not BASE_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=retention_days)
    for entry in sorted(BASE_DIR.iterdir()):
        if not entry.is_dir():
            continue
        try:
            if datetime.strptime(entry.name, "%Y-%m-%d") < cutoff:
                shutil.rmtree(entry)
                print(f"[cleanup] removed: {entry}")
        except ValueError:
            pass


# ---- 主入口 ----


def main():
    now = datetime.now()

    if len(sys.argv) > 1:
        try:
            day = datetime.strptime(sys.argv[1], "%Y-%m-%d")
            start = day.replace(hour=0, minute=0, second=0)
            end = day.replace(hour=23, minute=59, second=59)
            label_str = f"{sys.argv[1]} (full day)"
        except ValueError:
            print(f"Invalid date: {sys.argv[1]} (expected YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)
    else:
        end = now
        start = now - timedelta(hours=24)
        label_str = f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}"

    ts_min = start.strftime("%Y-%m-%d %H:%M:%S")
    ts_max = end.strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{now:%H:%M:%S}] Range: {label_str}")

    disks_data = read_disks(ts_min, ts_max)
    ctrl_data = read_controller(ts_min, ts_max)

    today_dir = BASE_DIR / now.strftime("%Y-%m-%d")
    vds_data = read_vds(today_dir)
    patrol_data = read_one(today_dir, "patrol.csv")
    cc_data = read_one(today_dir, "consistency.csv")
    smart_data = read_smart(today_dir)
    attr_data = read_attributes(today_dir)
    sys_data = read_system(today_dir)

    if not disks_data and not ctrl_data:
        print("  No data collected")
        summary = {
            "controller_model": "",
            "controller_fw": "",
            "controller_health": "No Data",
            "num_vds": 0,
            "num_disks": 0,
            "bbu_model": "",
            "bbu_state": "",
            "bbu_temperature": None,
            "vd_details": [],
            "disk_details": [],
            "has_warning": False,
            "has_critical": False,
            "has_offline": False,
            "has_smart_alert": False,
            "has_media_error": False,
            "has_smart_issue": False,
            "pr_next": "",
            "pr_state": "",
            "pr_mode": "",
            "pr_iterations": 0,
            "cc_next": "",
            "cc_state": "",
            "cc_mode": "",
            "cc_iterations": 0,
            "sys_load": "",
            "sys_mem": "",
        }
        chart_b64 = None
    else:
        summary = build_summary(
            disks_data,
            ctrl_data,
            vds_data,
            patrol_data,
            cc_data,
            smart_data,
            attr_data,
            sys_data,
        )
        plt, font_prop = setup_matplotlib()
        if plt is None:
            chart_b64 = None
        else:
            chart_b64 = make_temperature_chart(
                plt, font_prop, disks_data, ctrl_data, label_str
            )

    # 直接输出文本摘要并保存 HTML 报告
    html = build_html_email(summary, chart_b64, label_str, inline_chart=True)
    report_path = CHART_DIR / f"report-{now.strftime('%Y-%m-%d-%H%M')}.html"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)

    text_summary = build_text_summary(summary, label_str)
    print()
    print(text_summary)
    print(f"\nHTML report saved to: {report_path}")

    # 邮件发送改为可选：仅当 SMTP_USER 配置时才发送
    if SMTP_USER:
        email_html = build_html_email(summary, chart_b64, label_str, inline_chart=False)
        msg = create_mime_message(email_html, chart_b64, label_str)
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.sendmail(SMTP_FROM, [SMTP_TO], msg.as_string())
            print(f"\nEmail sent to {SMTP_TO}")
        except Exception as e:
            print(f"\nEmail failed: {e}", file=sys.stderr)
    else:
        print("\nSMTP not configured (set SMTP_HOST / SMTP_USER / SMTP_PASS env vars)")
        print("Skipping email send.")

    cleanup_old_data()
    print("\nDone.")


if __name__ == "__main__":
    main()
