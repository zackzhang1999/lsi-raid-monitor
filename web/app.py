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
import secrets
import subprocess
import threading
import time
from functools import wraps
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

from flask import Flask, jsonify, render_template, request, Response, session

# 引入项目根目录，复用 lsi_report 的数据读取逻辑
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import lsi_report
import lsi_alert
import storage_mgr
import user_mgr

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
COLLECTION_CONFIG_FILE = BASE_DIR / "collection_config.json"
COLLECTION_INTERVALS = {5, 30, 60}
DEFAULT_COLLECTION_INTERVAL = 30
_collection_lock = threading.Lock()
_scheduler_wakeup = threading.Event()
_scheduler_started = False

# ---- 认证配置 ----
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")


def _load_secret_key() -> str:
    """优先取环境变量，否则用持久化的随机密钥文件。"""
    env_key = os.environ.get("WEB_SECRET_KEY")
    if env_key:
        return env_key
    key_file = BASE_DIR / ".secret_key"
    try:
        if key_file.exists():
            return key_file.read_text().strip()
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        key_file.write_text(key)
        os.chmod(key_file, 0o600)
        return key
    except OSError:
        return secrets.token_hex(32)


app.secret_key = _load_secret_key()

# 无用户时按旧版 WEB_PASSWORD 迁移出一个 admin 用户
user_mgr.ensure_bootstrap(WEB_PASSWORD)


def auth_enabled() -> bool:
    return user_mgr.users_exist()


def current_role() -> str | None:
    """当前会话角色；未启用认证时视为 admin（无限制）。"""
    if not auth_enabled():
        return "admin"
    return session.get("role")


def require_admin(view):
    """危险/写操作仅管理员可用；普通用户返回 403，未登录返回 401。"""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not auth_enabled():
            return view(*args, **kwargs)
        if not session.get("user"):
            return jsonify({"success": False, "error": "unauthorized"}), 401
        if session.get("role") != "admin":
            return jsonify({"success": False, "error": "需要管理员权限"}), 403
        return view(*args, **kwargs)

    return wrapper


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
                # summary 的 state 是 24h 内出现过的状态集合，实时视图应显示最新状态
                "state": current.get("state") or d.get("state"),
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


def get_collection_config() -> dict:
    interval = DEFAULT_COLLECTION_INTERVAL
    if COLLECTION_CONFIG_FILE.exists():
        try:
            with open(COLLECTION_CONFIG_FILE, "r", encoding="utf-8") as f:
                interval = int(json.load(f).get("interval_minutes", interval))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    if interval not in COLLECTION_INTERVALS:
        interval = DEFAULT_COLLECTION_INTERVAL
    return {"interval_minutes": interval}


def save_collection_config(interval_minutes: int):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    with open(COLLECTION_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"interval_minutes": interval_minutes}, f, ensure_ascii=False, indent=2)


def trigger_collection() -> dict:
    if not _collection_lock.acquire(blocking=False):
        return {"success": False, "busy": True, "error": "采集任务正在运行"}

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
    finally:
        _collection_lock.release()


def _collection_scheduler():
    while True:
        result = trigger_collection()
        if not result.get("success") and not result.get("busy"):
            print(
                f"[{_now():%Y-%m-%d %H:%M:%S}] automatic collection failed: "
                f"{result.get('stderr') or result.get('error') or 'unknown error'}",
                file=sys.stderr,
            )
        interval = get_collection_config()["interval_minutes"]
        _scheduler_wakeup.wait(interval * 60)
        _scheduler_wakeup.clear()


def start_collection_scheduler():
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(
        target=_collection_scheduler,
        name="lsi-collection-scheduler",
        daemon=True,
    ).start()


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
            # 立即回读最新状态，并后台触发一次采集刷新 CSV，
            # 使界面数秒内显示操作后的真实状态而不是等下个采集周期
            response["current_state"] = _read_disk_state(storcli, controller, eid, slot)
            try:
                storage_mgr.invalidate_cache()
            except Exception:
                pass
            threading.Thread(target=_post_op_collection, daemon=True).start()
        return response
    except Exception as e:
        return {"success": False, "error": str(e), "command": full_cmd}


def _read_disk_state(storcli: str, controller: str, eid: int, slot: int) -> str | None:
    """操作后立即从 storcli 回读磁盘当前状态，失败返回 None。"""
    try:
        result = subprocess.run(
            ["sudo", storcli, f"{controller}/e{eid}/s{slot}", "show", "J"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout or "{}")
        info = data["Controllers"][0]["Response Data"]["Drive Information"]
        for entry in info:
            if entry.get("EID:Slt") == f"{eid}:{slot}":
                return entry.get("State")
    except Exception:
        pass
    return None


def _post_op_collection() -> None:
    """磁盘操作后稍等控制器状态稳定，再触发一次完整采集。"""
    time.sleep(3)
    result = trigger_collection()
    if not result.get("success") and not result.get("busy"):
        print(
            f"[{_now():%Y-%m-%d %H:%M:%S}] post-operation collection failed: "
            f"{result.get('stderr') or result.get('error') or 'unknown error'}",
            file=sys.stderr,
        )


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
@require_admin
def api_disk_operate(eid: int, slot: int):
    data = request.get_json(silent=True) or {}
    action = data.get("action", "").strip().lower()
    return jsonify(operate_disk(eid, slot, action))


@app.route("/api/disk/<int:eid>/<int:slot>/smart")
def api_disk_smart(eid: int, slot: int):
    return jsonify(get_disk_smart(eid, slot))


@app.route("/api/collect", methods=["POST"])
@require_admin
def api_collect():
    return jsonify(trigger_collection())


@app.route("/api/collection/config")
def api_collection_config():
    return jsonify(get_collection_config())


@app.route("/api/collection/config", methods=["POST"])
@require_admin
def api_collection_config_update():
    data = request.get_json(silent=True) or {}
    try:
        interval = int(data.get("interval_minutes"))
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "采集周期无效"}), 400
    if interval not in COLLECTION_INTERVALS:
        return jsonify({"success": False, "error": "采集周期仅支持 5、30 或 60 分钟"}), 400
    try:
        save_collection_config(interval)
    except OSError as e:
        return jsonify({"success": False, "error": str(e)}), 500
    _scheduler_wakeup.set()
    return jsonify({"success": True, "interval_minutes": interval})


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
@require_admin
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
@require_admin
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


# ---- 认证 ----


@app.route("/api/auth/status")
def api_auth_status():
    role = current_role() if (session.get("user") or not auth_enabled()) else None
    return jsonify(
        {
            "auth_required": auth_enabled(),
            "logged_in": bool(session.get("user")) or not auth_enabled(),
            "username": session.get("user") or "",
            "role": role or "",
        }
    )


@app.route("/api/login", methods=["POST"])
def api_login():
    if not auth_enabled():
        return jsonify({"success": True})
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    role = user_mgr.verify(username, str(data.get("password", "")))
    if role:
        session["user"] = username
        session["role"] = role
        return jsonify({"success": True, "username": username, "role": role})
    return jsonify({"success": False, "error": "用户名或口令错误"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user", None)
    session.pop("role", None)
    return jsonify({"success": True})


# ---- 用户管理（仅管理员） ----


@app.route("/api/users")
@require_admin
def api_users_list():
    return jsonify({"success": True, "users": user_mgr.list_users()})


@app.route("/api/users", methods=["POST"])
@require_admin
def api_users_create():
    data = request.get_json(silent=True) or {}
    ok, msg = user_mgr.create_user(
        str(data.get("username", "")).strip(),
        str(data.get("password", "")),
        str(data.get("role", "viewer")).strip() or "viewer",
    )
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    append_event(
        {
            "type": "user_op",
            "level": "info",
            "message": f"管理员 {session.get('user')} 新建用户 {msg}（{data.get('role', 'viewer')}）",
        }
    )
    return jsonify({"success": True, "username": msg})


@app.route("/api/users/password", methods=["POST"])
@require_admin
def api_users_password():
    data = request.get_json(silent=True) or {}
    ok, msg = user_mgr.set_password(
        str(data.get("username", "")).strip(), str(data.get("password", ""))
    )
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    append_event(
        {
            "type": "user_op",
            "level": "info",
            "message": f"管理员 {session.get('user')} 重置了用户 {msg} 的口令",
        }
    )
    return jsonify({"success": True})


@app.route("/api/users/delete", methods=["POST"])
@require_admin
def api_users_delete():
    data = request.get_json(silent=True) or {}
    ok, msg = user_mgr.delete_user(
        str(data.get("username", "")).strip(), session.get("user", "")
    )
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    append_event(
        {
            "type": "user_op",
            "level": "warn",
            "message": f"管理员 {session.get('user')} 删除了用户 {msg}",
        }
    )
    return jsonify({"success": True})


def _dev_name(value) -> str:
    """统一设备参数：接受 'sdb1' 或 '/dev/sdb1'，返回裸设备名。"""
    name = str(value or "").strip()
    if name.startswith("/dev/"):
        name = name[len("/dev/"):]
    return name


# ---- 存储管理 ----


@app.route("/api/storage/disks")
def api_storage_disks():
    try:
        tree = storage_mgr.build_storage_tree()
        tree["success"] = True
        return jsonify(tree)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/storage/disk/<name>/smart")
def api_storage_disk_smart(name: str):
    try:
        return jsonify(storage_mgr.get_smart_any(name))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/storage/mount", methods=["POST"])
@require_admin
def api_storage_mount():
    data = request.get_json(silent=True) or {}
    device = _dev_name(data.get("device"))
    mountpoint = data.get("mountpoint") or None
    if mountpoint:
        mountpoint = str(mountpoint).strip()
    ok, result = storage_mgr.mount_device(device, mountpoint)
    _log_storage_op("挂载", device, ok, result)
    if ok:
        return jsonify({"success": True, **result})
    return jsonify({"success": False, "error": str(result)}), 400


@app.route("/api/storage/umount", methods=["POST"])
@require_admin
def api_storage_umount():
    data = request.get_json(silent=True) or {}
    device = _dev_name(data.get("device"))
    ok, result = storage_mgr.umount_device(device)
    _log_storage_op("卸载", device, ok, result)
    if ok:
        return jsonify({"success": True, **result})
    return jsonify({"success": False, "error": str(result)}), 400


@app.route("/api/storage/format", methods=["POST"])
@require_admin
def api_storage_format():
    data = request.get_json(silent=True) or {}
    device = _dev_name(data.get("device"))
    fstype = str(data.get("fstype", "")).strip()
    label = data.get("label") or None
    confirm_name = _dev_name(data.get("confirm_name"))
    ok, result = storage_mgr.format_device(device, fstype, label, confirm_name)
    _log_storage_op(f"格式化为 {fstype}", device, ok, result)
    if ok:
        return jsonify({"success": True, **result})
    return jsonify({"success": False, "error": str(result)}), 400


def _log_storage_op(op: str, device: str, ok: bool, result) -> None:
    detail = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    append_event(
        {
            "type": "storage_op",
            "level": "info" if ok else "warn",
            "message": f"存储操作[{op}] 设备 {device}: {'成功' if ok else '失败'} — {detail}",
        }
    )


# ---- 主入口 ----


if __name__ == "__main__":
    host = os.environ.get("FLASK_RUN_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_RUN_PORT", "5200"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_collection_scheduler()
    app.run(host=host, port=port, debug=debug)
