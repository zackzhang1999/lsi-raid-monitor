#!/usr/bin/env python3
# ================================================
# LSI RAID Monitor Web UI — Flask 后端
# ================================================

from __future__ import annotations

import os
import sys
import csv
import io
import json
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

from flask import Flask, jsonify, render_template, request, Response

# 引入项目根目录，复用 lsi_report 的数据读取逻辑
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import lsi_report
import lsi_alert

# ---- Flask 配置 ----
WEB_DIR = Path(__file__).resolve().parent
app = Flask(
    __name__,
    template_folder=str(WEB_DIR / "templates"),
    static_folder=str(WEB_DIR / "static"),
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
TEMP_WARN = int(os.environ.get("TEMP_WARN", "45"))
TEMP_CRIT = int(os.environ.get("TEMP_CRIT", "50"))

EVENTS_FILE = BASE_DIR / "events.jsonl"


# ---- 工具函数 ----


def _now() -> datetime:
    return datetime.now()


def _range(hours: int = 24) -> tuple[str, str]:
    end = _now()
    start = end - timedelta(hours=hours)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def _status_from_summary(summary: dict) -> str:
    if (
        summary.get("has_critical")
        or summary.get("has_offline")
        or summary.get("has_smart_alert")
        or summary.get("has_smart_issue")
    ):
        return "ERROR"
    if summary.get("has_warning") or summary.get("has_media_error"):
        return "WARN"
    if summary.get("controller_health") == "No Data":
        return "NO DATA"
    return "OK"


def _disk_current_state(disks_data: list[dict], eid: int, slot: int) -> dict:
    """从 disks_data 中返回指定磁盘最新的完整记录。"""
    latest = None
    for row in disks_data:
        if row.get("eid") == eid and row.get("slot") == slot:
            if latest is None or row.get("timestamp", "") > latest.get("timestamp", ""):
                latest = row
    return latest or {}


def _state_ok(state: str) -> bool:
    return str(state).strip().lower() in ("optimal", "optl", "opt")


def _disk_ok(state: str) -> bool:
    return str(state).strip().lower() in ("onln", "online", "hotspare", "ugood")


# ---- 健康评分 ----


def build_health_score(
    summary: dict,
    current_disk_details: list[dict],
    avg_temp: float | None,
    max_temp: int | None,
) -> dict:
    """
    计算 RAID 各维度健康评分（0-100）。
    规则：每个维度满足理想条件得 100 分，存在降级按权重扣减。
    """

    def score(value: bool) -> int:
        return 100 if value else 0

    # 控制器
    ctrl_health = summary.get("controller_health", "")
    ctrl_score = score(_state_ok(ctrl_health) or ctrl_health.lower() in ("ok",))

    # 虚拟磁盘
    vd_details = summary.get("vd_details", [])
    if vd_details:
        vd_ok_count = sum(1 for vd in vd_details if _state_ok(vd.get("state", "")))
        vd_score = int(vd_ok_count / len(vd_details) * 100)
    else:
        vd_score = 100 if summary.get("num_vds", 0) == 0 else 0

    # 物理磁盘
    disks = current_disk_details or []
    if disks:
        disk_ok_count = sum(1 for d in disks if _disk_ok(d.get("state", "")))
        disk_score = int(disk_ok_count / len(disks) * 100)
    else:
        disk_score = 100

    # 温度
    if max_temp is not None:
        if max_temp >= TEMP_CRIT:
            temp_score = 0
        elif max_temp >= TEMP_WARN:
            # WARN 到 CRIT 之间线性扣分
            temp_score = max(
                0,
                int(100 - (max_temp - TEMP_WARN) / (TEMP_CRIT - TEMP_WARN) * 50),
            )
        else:
            temp_score = 100
    else:
        temp_score = 100

    # SMART / 错误
    error_score = 100
    if summary.get("has_smart_alert"):
        error_score -= 40
    if summary.get("has_smart_issue"):
        error_score -= 30
    if summary.get("has_media_error"):
        error_score -= 20
    if summary.get("has_offline"):
        error_score -= 10
    error_score = max(0, error_score)

    # BBU
    bbu_state = summary.get("bbu_state", "")
    bbu_score = score(_state_ok(bbu_state) or bbu_state.lower() in ("ok", "healthy"))
    # 没有 BBU 时不扣分
    if not summary.get("bbu_model"):
        bbu_score = 100

    # 综合评分：加权平均
    overall = int(
        ctrl_score * 0.25
        + vd_score * 0.20
        + disk_score * 0.20
        + temp_score * 0.15
        + error_score * 0.15
        + bbu_score * 0.05
    )

    return {
        "overall": overall,
        "level": _health_level(overall),
        "details": {
            "controller": {"score": ctrl_score, "label": "控制器"},
            "virtual_disks": {"score": vd_score, "label": "虚拟磁盘"},
            "physical_disks": {"score": disk_score, "label": "物理磁盘"},
            "temperature": {"score": temp_score, "label": "温度"},
            "smart_errors": {"score": error_score, "label": "SMART/错误"},
            "bbu": {"score": bbu_score, "label": "BBU"},
        },
    }


def _health_level(score: int) -> str:
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 60:
        return "warning"
    return "critical"


# ---- 事件日志 ----


def load_events(limit: int = 100, offset: int = 0, level: str | None = None) -> list[dict]:
    """读取事件日志文件，按时间倒序返回，支持分页和级别筛选。"""
    events = []
    if not EVENTS_FILE.exists():
        return events
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    # 跳过演示/测试事件
                    if ev.get("type") == "test":
                        continue
                    if level and ev.get("level") != level:
                        continue
                    events.append(ev)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return events[offset : offset + limit] if limit else events[offset:]


def count_events(level: str | None = None) -> int:
    """统计事件总数。"""
    total = 0
    if not EVENTS_FILE.exists():
        return total
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    # 跳过演示/测试事件
                    if ev.get("type") == "test":
                        continue
                    if level and ev.get("level") != level:
                        continue
                    total += 1
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return total


def append_event(event: dict):
    """追加一条事件到日志文件。"""
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    event["timestamp"] = _now().strftime("%Y-%m-%d %H:%M:%S")
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def detect_events(
    prev_status: dict | None,
    curr_status: dict,
    curr_summary: dict,
    disks_data: list[dict],
) -> list[dict]:
    """
    对比前后状态，检测需要记录的事件。
    """
    events = []
    ts = _now().strftime("%Y-%m-%d %H:%M:%S")

    if not prev_status:
        # 首次运行只记录初始状态
        return events

    # 控制器健康变化
    prev_ctrl = prev_status.get("controller", {})
    curr_ctrl = curr_status.get("controller", {})
    if prev_ctrl.get("health") != curr_ctrl.get("health"):
        events.append(
            {
                "type": "controller",
                "level": "warning" if not _state_ok(curr_ctrl.get("health", "")) else "info",
                "message": f"控制器健康状态变化：{prev_ctrl.get('health', '—')} → {curr_ctrl.get('health', '—')}",
            }
        )

    # VD 状态变化
    prev_vds = {vd.get("dg_vd"): vd for vd in prev_status.get("virtual_disks", [])}
    for vd in curr_status.get("virtual_disks", []):
        key = vd.get("dg_vd")
        prev_state = prev_vds.get(key, {}).get("state")
        curr_state = vd.get("state")
        if prev_state and prev_state != curr_state:
            events.append(
                {
                    "type": "virtual_disk",
                    "level": "warning" if not _state_ok(curr_state) else "info",
                    "message": f"虚拟磁盘 {key} 状态变化：{prev_state} → {curr_state}",
                }
            )

    # 磁盘状态变化
    prev_disks = {d.get("label"): d for d in prev_status.get("physical_disks", [])}
    for d in curr_status.get("physical_disks", []):
        label = d.get("label")
        prev_state = prev_disks.get(label, {}).get("state")
        curr_state = d.get("state")
        if prev_state and prev_state != curr_state:
            level = "error" if not _disk_ok(curr_state) else "info"
            events.append(
                {
                    "type": "physical_disk",
                    "level": level,
                    "message": f"物理磁盘 {label} 状态变化：{prev_state} → {curr_state}",
                }
            )

    # 温度告警
    for d in curr_status.get("physical_disks", []):
        temp = d.get("temperature")
        if temp is None:
            continue
        label = d.get("label")
        if temp >= TEMP_CRIT:
            events.append(
                {
                    "type": "temperature",
                    "level": "error",
                    "message": f"物理磁盘 {label} 温度达到临界值：{temp}°C",
                }
            )
        elif temp >= TEMP_WARN:
            # 避免重复告警：只有上一次不是高温时才记录
            prev_temp = prev_disks.get(label, {}).get("temperature")
            if prev_temp is None or prev_temp < TEMP_WARN:
                events.append(
                    {
                        "type": "temperature",
                        "level": "warning",
                        "message": f"物理磁盘 {label} 温度超过警告阈值：{temp}°C",
                    }
                )

    # SMART / 错误告警
    if curr_summary.get("has_smart_alert") and not prev_status.get("has_smart_alert"):
        events.append(
            {
                "type": "smart",
                "level": "error",
                "message": "检测到 SMART 告警",
            }
        )
    if curr_summary.get("has_smart_issue") and not prev_status.get("has_smart_issue"):
        events.append(
            {
                "type": "smart",
                "level": "error",
                "message": "检测到 SMART 扇区错误",
            }
        )
    if curr_summary.get("has_media_error") and not prev_status.get("has_media_error"):
        events.append(
            {
                "type": "error_counter",
                "level": "warning",
                "message": "检测到媒体错误/预测性故障计数增加",
            }
        )

    return events


# ---- 状态构建 ----


def build_status() -> dict:
    now = _now()
    today_dir = BASE_DIR / now.strftime("%Y-%m-%d")
    ts_min, ts_max = _range(24)

    disks_data = lsi_report.read_disks(ts_min, ts_max)
    ctrl_data = lsi_report.read_controller(ts_min, ts_max)
    vds_data = lsi_report.read_vds(today_dir)
    patrol_data = lsi_report.read_one(today_dir, "patrol.csv")
    cc_data = lsi_report.read_one(today_dir, "consistency.csv")
    smart_data = lsi_report.read_smart(today_dir)
    attr_data = lsi_report.read_attributes(today_dir)
    sys_data = lsi_report.read_system(today_dir)

    if not disks_data and not ctrl_data:
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
    else:
        summary = lsi_report.build_summary(
            disks_data,
            ctrl_data,
            vds_data,
            patrol_data,
            cc_data,
            smart_data,
            attr_data,
            sys_data,
        )

    # 计算全局温度概况
    all_temps = [
        d.get("temperature")
        for d in disks_data
        if isinstance(d.get("temperature"), int)
    ]
    avg_temp = round(sum(all_temps) / len(all_temps), 1) if all_temps else None
    max_temp = max(all_temps) if all_temps else None

    # 使用最新一条磁盘记录替换 summary 中的 disk_details，以展示当前值
    current_disk_details = []
    for d in summary.get("disk_details", []):
        label = d.get("label", "")
        # 解析 E{eid}:S{slot}
        parts = label.replace("E", "").replace("S", "").split(":")
        if len(parts) == 2:
            try:
                eid, slot = int(parts[0]), int(parts[1])
            except ValueError:
                current_disk_details.append(d)
                continue
        else:
            current_disk_details.append(d)
            continue

        current = _disk_current_state(disks_data, eid, slot)
        current_disk_details.append(
            {
                **d,
                "temperature": current.get("temperature", d.get("temp_max")),
                "media_error": current.get("media_error", d.get("media_error")),
                "other_error": current.get("other_error", d.get("other_error")),
                "predictive_failure": current.get(
                    "predictive_failure", d.get("predictive_failure")
                ),
                "smart_alert": current.get("smart_alert", d.get("smart_alert")),
                "shield_counter": current.get("shield_counter", d.get("shield_counter")),
            }
        )

    # 健康评分
    health_score = build_health_score(
        summary, current_disk_details, avg_temp, max_temp
    )

    # 事件检测：读取上一次缓存的状态
    status_cache_file = BASE_DIR / ".web_status_cache.json"
    prev_status = None
    try:
        if status_cache_file.exists():
            with open(status_cache_file, "r", encoding="utf-8") as f:
                prev_status = json.load(f)
    except Exception:
        prev_status = None

    curr_status = {
        "controller": {
            "model": summary.get("controller_model", ""),
            "fw": summary.get("controller_fw", ""),
            "health": summary.get("controller_health", "No Data"),
            "num_disks": summary.get("num_disks", 0),
            "num_vds": summary.get("num_vds", 0),
            "bbu_model": summary.get("bbu_model", ""),
            "bbu_state": summary.get("bbu_state", ""),
            "bbu_temperature": summary.get("bbu_temperature"),
        },
        "virtual_disks": summary.get("vd_details", []),
        "physical_disks": [
            {
                "label": d.get("label"),
                "state": d.get("state"),
                "temperature": d.get("temperature"),
            }
            for d in current_disk_details
        ],
        "has_smart_alert": summary.get("has_smart_alert", False),
        "has_smart_issue": summary.get("has_smart_issue", False),
        "has_media_error": summary.get("has_media_error", False),
    }

    events = detect_events(prev_status, curr_status, summary, disks_data)
    for ev in events:
        append_event(ev)

    # 保存当前状态供下次对比
    try:
        with open(status_cache_file, "w", encoding="utf-8") as f:
            json.dump(curr_status, f, ensure_ascii=False)
    except Exception:
        pass

    return {
        "host": os.uname().nodename,
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": _status_from_summary(summary),
        "thresholds": {"warn": TEMP_WARN, "crit": TEMP_CRIT},
        "health_score": health_score,
        "temperature_overview": {
            "avg": avg_temp,
            "max": max_temp,
            "warn": TEMP_WARN,
            "crit": TEMP_CRIT,
        },
        "controller": curr_status["controller"],
        "virtual_disks": summary.get("vd_details", []),
        "physical_disks": current_disk_details,
        "maintenance": {
            "patrol_read": {
                "next": summary.get("pr_next", ""),
                "state": summary.get("pr_state", ""),
                "mode": summary.get("pr_mode", ""),
                "iterations": summary.get("pr_iterations", 0),
            },
            "consistency_check": {
                "next": summary.get("cc_next", ""),
                "state": summary.get("cc_state", ""),
                "mode": summary.get("cc_mode", ""),
                "iterations": summary.get("cc_iterations", 0),
                "vd_completed": summary.get("cc_vd_completed", 0),
            },
        },
        "system": {
            "load": summary.get("sys_load", ""),
            "memory": summary.get("sys_mem", ""),
        },
    }


# ---- 历史温度 ----


def build_history(hours: int = 24) -> dict:
    ts_min, ts_max = _range(hours)
    disks_data = lsi_report.read_disks(ts_min, ts_max)

    # 按 (eid, slot) 分组，保留每个时间戳的温度
    groups = defaultdict(list)
    for row in disks_data:
        eid = row.get("eid")
        slot = row.get("slot")
        temp = row.get("temperature")
        ts = row.get("timestamp", "")
        if temp is None or not ts:
            continue
        label = f"E{eid}:S{slot}"
        groups[label].append({"x": ts, "y": temp})

    # 对每个分组按时间排序
    datasets = []
    for label, points in sorted(groups.items()):
        points.sort(key=lambda p: p["x"])
        datasets.append({"label": label, "data": points})

    return {"datasets": datasets, "hours": hours}


# ---- 导出 ----


def export_csv(hours: int = 24) -> str:
    """导出过去 N 小时的 disks.csv 数据为 CSV 字符串。"""
    ts_min, ts_max = _range(hours)
    rows = lsi_report.read_disks(ts_min, ts_max)

    output = io.StringIO()
    fieldnames = [
        "timestamp",
        "eid",
        "slot",
        "did",
        "dg",
        "model",
        "state",
        "size",
        "intf",
        "med",
        "temperature",
        "media_error",
        "other_error",
        "predictive_failure",
        "smart_alert",
        "shield_counter",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


# ---- 采集 ----


def trigger_collection() -> dict:
    collector = PROJECT_ROOT / "lsi_collectd.py"
    try:
        result = subprocess.run(
            ["sudo", sys.executable, str(collector)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---- 磁盘操作 ----


DISK_ACTIONS = {
    "good": {
        "label": "设为 UGood",
        "cmd": "set good",
        "force": True,
        "description": "将磁盘标记为 Unconfigured Good（未配置良好）",
    },
    "online": {
        "label": "设为 Online",
        "cmd": "set online",
        "force": False,
        "description": "将磁盘设置为 Online 状态",
    },
    "offline": {
        "label": "设为 Offline",
        "cmd": "set offline",
        "force": False,
        "description": "将磁盘设置为 Offline 状态",
    },
    "jbod": {
        "label": "设为 JBOD",
        "cmd": "set jbod",
        "force": True,
        "description": "将磁盘设置为 JBOD 直通模式",
    },
}


def operate_disk(eid: int, slot: int, action: str) -> dict:
    if action not in DISK_ACTIONS:
        return {"success": False, "error": f"不支持的操作: {action}"}

    cfg = DISK_ACTIONS[action]
    controller = os.environ.get("LSI_CONTROLLER", "/c0")
    local_storcli = PROJECT_ROOT / "storcli64"
    storcli = os.environ.get(
        "STORCLI_PATH",
        str(local_storcli) if local_storcli.exists() else "/usr/local/bin/storcli64",
    )
    cmd_parts = ["sudo", storcli, f"{controller}/e{eid}/s{slot}", cfg["cmd"]]
    if cfg["force"]:
        cmd_parts.append("force")
    full_cmd = " ".join(cmd_parts)

    try:
        result = subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        success = result.returncode == 0 and "Status = Success" in result.stdout
        response = {
            "success": success,
            "returncode": result.returncode,
            "command": full_cmd,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if success:
            append_event(
                {
                    "type": "disk_operation",
                    "level": "info",
                    "message": f"磁盘 E{eid}:S{slot} 执行操作: {cfg['label']}",
                }
            )
        return response
    except Exception as e:
        return {"success": False, "error": str(e), "command": full_cmd}


def _find_did_for_disk(eid: int, slot: int) -> int | None:
    """从当天 disks.csv 中查找指定 (eid, slot) 最新的 DID。"""
    today_dir = BASE_DIR / _now().strftime("%Y-%m-%d")
    fp = today_dir / "disks.csv"
    if not fp.exists():
        return None
    latest = None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    reid = int(row.get("eid", 0))
                    rslot = int(row.get("slot", 0))
                except (ValueError, TypeError):
                    continue
                if reid == eid and rslot == slot:
                    if latest is None or row.get("timestamp", "") > latest.get(
                        "timestamp", ""
                    ):
                        latest = row
    except Exception:
        return None
    if not latest:
        return None
    try:
        return int(latest.get("did", 0) or 0)
    except (ValueError, TypeError):
        return None


def get_disk_smart(eid: int, slot: int) -> dict:
    """通过 smartctl 获取指定磁盘的完整 SMART 信息。"""
    did = _find_did_for_disk(eid, slot)
    if did is None or did <= 0:
        return {
            "success": False,
            "error": f"未找到磁盘 E{eid}:S{slot} 的 DID，可能尚未完成数据采集",
        }

    smartctl = os.environ.get("SMARTCTL_PATH", "/usr/sbin/smartctl")
    cmd = f"sudo {smartctl} -a -d megaraid,{did} /dev/sda"
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "success": result.returncode in (0, 4),
            "did": did,
            "command": cmd,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "command": cmd}


# ---- 路由 ----


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(build_status())


@app.route("/api/history")
def api_history():
    hours = request.args.get("hours", "24")
    try:
        hours = int(hours)
    except ValueError:
        hours = 24
    if hours < 1:
        hours = 1
    if hours > 168:
        hours = 168
    return jsonify(build_history(hours))


@app.route("/api/events")
def api_events():
    limit = request.args.get("limit", "50")
    offset = request.args.get("offset", "0")
    level = request.args.get("level", "")
    try:
        limit = int(limit)
    except ValueError:
        limit = 50
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500
    try:
        offset = int(offset)
    except ValueError:
        offset = 0
    if offset < 0:
        offset = 0
    level = level.strip() or None
    return jsonify({
        "events": load_events(limit=limit, offset=offset, level=level),
        "total": count_events(level=level),
        "limit": limit,
        "offset": offset,
    })


@app.route("/api/export/csv")
def api_export_csv():
    hours = request.args.get("hours", "24")
    try:
        hours = int(hours)
    except ValueError:
        hours = 24
    if hours < 1:
        hours = 1
    if hours > 168:
        hours = 168

    csv_data = export_csv(hours)
    filename = f"lsi-raid-disks-{_now().strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/api/disk/operations")
def api_disk_operations():
    return jsonify(
        {
            "actions": [
                {"key": k, "label": v["label"], "description": v["description"]}
                for k, v in DISK_ACTIONS.items()
            ]
        }
    )


@app.route("/api/disk/<int:eid>/<int:slot>/operate", methods=["POST"])
def api_disk_operate(eid: int, slot: int):
    data = request.get_json(silent=True) or {}
    action = data.get("action", "").strip().lower()
    return jsonify(operate_disk(eid, slot, action))


@app.route("/api/disk/<int:eid>/<int:slot>/smart")
def api_disk_smart(eid: int, slot: int):
    return jsonify(get_disk_smart(eid, slot))


@app.route("/api/collect", methods=["POST"])
def api_collect():
    return jsonify(trigger_collection())


# ---- 报警配置 ----


def _alert_config_response() -> dict:
    cfg = lsi_alert.get_alert_config()
    return {
        "enabled": bool(cfg.get("alert_email_to")),
        "recipients": [
            addr.strip()
            for addr in cfg.get("alert_email_to", "").split(",")
            if addr.strip()
        ],
        "sendmail_path": cfg.get("sendmail_path", lsi_alert.DEFAULT_SENDMAIL_PATH),
        "sendmail_available": lsi_alert.sendmail_available(
            cfg.get("sendmail_path", lsi_alert.DEFAULT_SENDMAIL_PATH)
        ),
        "temp_warn": cfg.get("temp_warn", lsi_alert.DEFAULT_TEMP_WARN),
        "temp_crit": cfg.get("temp_crit", lsi_alert.DEFAULT_TEMP_CRIT),
        # 标记哪些字段被环境变量锁定（Web 不能覆盖）
        "locked": {
            "alert_email_to": bool(os.environ.get("ALERT_EMAIL_TO")),
            "sendmail_path": bool(os.environ.get("SENDMAIL_PATH")),
            "temp_warn": bool(os.environ.get("TEMP_WARN")),
            "temp_crit": bool(os.environ.get("TEMP_CRIT")),
        },
    }


@app.route("/api/alert/config")
def api_alert_config():
    return jsonify(_alert_config_response())


@app.route("/api/alert/config", methods=["POST"])
def api_alert_config_update():
    data = request.get_json(silent=True) or {}

    # 被环境变量锁定的字段不能通过 Web 修改
    locked = {
        "alert_email_to": bool(os.environ.get("ALERT_EMAIL_TO")),
        "sendmail_path": bool(os.environ.get("SENDMAIL_PATH")),
        "temp_warn": bool(os.environ.get("TEMP_WARN")),
        "temp_crit": bool(os.environ.get("TEMP_CRIT")),
    }

    cfg = lsi_alert.load_alert_config_file()

    email_to = data.get("alert_email_to")
    if email_to is not None and not locked["alert_email_to"]:
        cfg["alert_email_to"] = str(email_to).strip()

    sendmail = data.get("sendmail_path")
    if sendmail is not None and not locked["sendmail_path"]:
        cfg["sendmail_path"] = str(sendmail).strip() or lsi_alert.DEFAULT_SENDMAIL_PATH

    temp_warn = data.get("temp_warn")
    if temp_warn is not None and not locked["temp_warn"]:
        try:
            cfg["temp_warn"] = int(temp_warn)
        except (ValueError, TypeError):
            pass

    temp_crit = data.get("temp_crit")
    if temp_crit is not None and not locked["temp_crit"]:
        try:
            cfg["temp_crit"] = int(temp_crit)
        except (ValueError, TypeError):
            pass

    try:
        lsi_alert.save_alert_config(cfg)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    append_event(
        {
            "type": "alert_config",
            "level": "info",
            "message": "报警邮件配置已更新",
        }
    )
    return jsonify({"success": True, "config": _alert_config_response()})


@app.route("/api/alert/test", methods=["POST"])
def api_alert_test():
    cfg = lsi_alert.get_alert_config()
    alert_email_to = cfg.get("alert_email_to", "")
    sendmail_path = cfg.get("sendmail_path", lsi_alert.DEFAULT_SENDMAIL_PATH)

    if not alert_email_to:
        return jsonify(
            {"success": False, "error": "ALERT_EMAIL_TO not configured"}
        ), 400
    if not lsi_alert.sendmail_available(sendmail_path):
        return jsonify(
            {"success": False, "error": f"sendmail not found: {sendmail_path}"}
        ), 400

    host = os.uname().nodename
    recipients = [addr.strip() for addr in alert_email_to.split(",") if addr.strip()]
    subject = f"[LSI RAID ALERT TEST] {host}"
    body = (
        f"这是一封来自 {host} 的 LSI RAID Monitor 报警邮件测试。\n\n"
        "如果收到此邮件，说明本地 sendmail 配置正确，即时报警功能可用。\n\n"
        f"sendmail 路径: {sendmail_path}\n"
        f"收件人: {', '.join(recipients)}\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    ok = lsi_alert.send_alert_email(subject, body, recipients, sendmail_path)
    if ok:
        append_event(
            {
                "type": "alert_test",
                "level": "info",
                "message": f"手动测试报警邮件已发送至 {', '.join(recipients)}",
            }
        )
        return jsonify({"success": True, "recipients": recipients})
    return jsonify({"success": False, "error": "sendmail command failed"}), 500


# ---- 主入口 ----


if __name__ == "__main__":
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5200"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
