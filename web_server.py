#!/usr/bin/env python3
# ================================================
# LSI MegaRAID 监控 Web 服务
# Flask 后端：读取 lsi_collectd.py 采集的 CSV 数据，提供监控/报警/磁盘/存储/用户 API
#
# 环境变量:
#   LSI_DATA_DIR     数据目录（默认 项目根/data）
#   LSI_WEB_HOST     监听地址（默认 0.0.0.0）
#   LSI_WEB_PORT     监听端口（默认 5200）
#   STORCLI_PATH     storcli64 路径
#   LSI_CONTROLLER   控制器（默认 /c0）
# ================================================

from __future__ import annotations

import csv
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
    session,
)

import lsi_alert
import storage_mgr
import user_mgr

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))

LOCAL_STORCLI = PROJECT_ROOT / "storcli64"
STORCLI = os.environ.get(
    "STORCLI_PATH",
    str(LOCAL_STORCLI) if LOCAL_STORCLI.exists() else "/usr/local/bin/storcli64",
)
CONTROLLER = os.environ.get("LSI_CONTROLLER", "/c0")
SMARTCTL = os.environ.get("SMARTCTL_PATH", "/usr/sbin/smartctl")


def _smart_base_device() -> str:
    """smartctl -d megaraid,N 需要一个透传基设备：优先 /dev/sda，
    不存在时用 megaraid 字符设备 /dev/bus/0，再退到任意 /dev/sdX。
    （部分系统 VD 从 /dev/sdb 开始编号，没有 /dev/sda。）"""
    if os.path.exists("/dev/sda"):
        return "/dev/sda"
    if os.path.exists("/dev/bus/0"):
        return "/dev/bus/0"
    for c in "bcdefghijklmnopqrstuvwxyz":
        if os.path.exists(f"/dev/sd{c}"):
            return f"/dev/sd{c}"
    return "/dev/sda"

COLLECTION_CONFIG_FILE = BASE_DIR / "collection_config.json"
COLLECTD = PROJECT_ROOT / "lsi_collectd.py"
VALID_INTERVALS = (1, 5, 15, 30, 60)

DISK_ACTIONS = {
    "online": "set online",
    "offline": "set offline",
    "good": "set good force",
    "jbod": "set jbod",
    "locate_start": "start locate",
    "locate_stop": "stop locate",
    "hotspare_global": "add hotsparedrive",
    "hotspare_dedicated": "add hotsparedrive",  # 专用热备，后面拼 DGs=N
    "hotspare_delete": "delete hotsparedrive",
}

# ---- 定位灯状态（storcli 无可靠回读接口，由本服务跟踪 locate_start/stop）----

LOCATE_STATE_FILE = BASE_DIR / ".locate_state.json"


def _load_locate_state() -> dict:
    try:
        if LOCATE_STATE_FILE.exists():
            data = json.loads(LOCATE_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): True for k, v in data.items() if v}
    except Exception:
        pass
    return {}


def _save_locate_state(state: dict) -> None:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        LOCATE_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _set_locate(eid: int, slot: int, on: bool) -> None:
    state = _load_locate_state()
    key = f"{eid}:{slot}"
    if on:
        state[key] = True
    else:
        state.pop(key, None)
    _save_locate_state(state)


# ---- 文件系统分区隐藏（按挂载点记录，全局生效）----

FS_HIDDEN_FILE = BASE_DIR / ".fs_hidden.json"


def _load_fs_hidden() -> set:
    try:
        if FS_HIDDEN_FILE.exists():
            data = json.loads(FS_HIDDEN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {str(m) for m in data}
    except Exception:
        pass
    return set()


def _save_fs_hidden(hidden: set) -> None:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        FS_HIDDEN_FILE.write_text(json.dumps(sorted(hidden)), encoding="utf-8")
    except Exception:
        pass

app = Flask(
    __name__,
    template_folder=str(PROJECT_ROOT / "web" / "templates"),
    static_folder=str(PROJECT_ROOT / "web" / "static"),
)


# ---- 会话密钥（持久化，避免重启后全员掉线）----


def _load_secret_key() -> bytes:
    key_file = BASE_DIR / ".secret_key"
    try:
        if key_file.exists():
            return key_file.read_bytes()
    except Exception:
        pass
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        key_file.write_bytes(key)
        os.chmod(key_file, 0o600)
    except Exception:
        pass
    return key


app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)


# ---- 内置采集线程 ----
# 每分钟触发 lsi_collectd.py（脚本内部按采集间隔门控，并用文件锁 + 分钟
# 标记去重，与外部 cron 并存时不会重复采集），使项目拷到新机器后无需
# 配置 cron 即可自动采集。设 LSI_DISABLE_COLLECTOR=1 可关闭。

_status_cache: dict = {"ts": 0.0, "data": None}


def _collect_once(extra_args: list[str] | None = None) -> None:
    try:
        subprocess.run(
            [sys.executable, str(COLLECTD)] + (extra_args or []),
            capture_output=True,
            timeout=110,
        )
        _status_cache.update(ts=0.0)
    except Exception:
        pass


def _collector_loop() -> None:
    # 首次部署/重启时若当天还没有数据，立即补一次采集，避免展示陈旧数据
    today_dir = BASE_DIR / datetime.now().strftime("%Y-%m-%d")
    if not (today_dir / "disks.csv").exists():
        _collect_once(["--force"])
    while True:
        now = datetime.now()
        time.sleep(61 - now.second - now.microsecond / 1e6)  # 对齐下一分钟
        _collect_once()


def _start_embedded_collector() -> None:
    if os.environ.get("LSI_DISABLE_COLLECTOR") == "1":
        return
    t = threading.Thread(target=_collector_loop, daemon=True, name="lsi-collector")
    t.start()


_start_embedded_collector()


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


# ---- 鉴权 ----


def auth_required() -> bool:
    return user_mgr.users_exist()


def current_role() -> str:
    if not auth_required():
        return "admin"
    return session.get("role") or ""


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if auth_required() and not session.get("username"):
            return jsonify(ok=False, error="未登录"), 401
        return f(*args, **kwargs)

    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if auth_required() and not session.get("username"):
            return jsonify(ok=False, error="未登录"), 401
        if current_role() != "admin":
            return jsonify(ok=False, error="需要管理员权限"), 403
        return f(*args, **kwargs)

    return wrapper


# ---- CSV 读取工具 ----


def _date_dirs() -> list[Path]:
    if not BASE_DIR.exists():
        return []
    return sorted(
        (p for p in BASE_DIR.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)),
        key=lambda p: p.name,
    )


def _latest_date_dir() -> Path | None:
    dirs = _date_dirs()
    return dirs[-1] if dirs else None


def _read_csv(path: Path) -> list[dict]:
    if not path or not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _to_int(val, default=None):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _fmt_bytes(val, base=1024) -> str:
    """把字节数格式化为可读容量（B/KB/MB/GB/TB/PB）。base 默认 1024。"""
    n = _to_int(val)
    if n is None or n <= 0:
        return ""
    size = float(n)
    units = ("B", "KB", "MB", "GB", "TB", "PB") if base == 1000 else ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if size < base:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= base
    return f"{size:.2f} EB"


# ---- 磁盘故障预测 ----
# 基于控制器计数器（PF/SMART 告警/介质错误）、SMART 关键属性
# （重映射/待定/无法纠正扇区）及其跨天历史趋势的规则化评估。
# 输出 level: ok / info / warn / crit，并附带判定原因，便于提前更换磁盘。

_PREDICT_LEVELS = {"ok": 0, "info": 1, "warn": 2, "crit": 3}


def _smart_history() -> dict:
    """按 did 汇总各日期目录中的 SMART 快照（每天一份），用于趋势判断"""
    hist: dict[str, list] = {}
    for date_dir in _date_dirs():
        for row in _read_csv(date_dir / "smart.csv"):
            did = str(row.get("did", ""))
            if did:
                hist.setdefault(did, []).append((date_dir.name, row))
    return hist


def _predict_disk(row: dict, hist_rows: list, temp_warn: int, temp_crit: int) -> dict:
    reasons = []
    level = "ok"

    def bump(lv: str, text: str):
        nonlocal level
        if _PREDICT_LEVELS[lv] > _PREDICT_LEVELS[level]:
            level = lv
        reasons.append({"level": lv, "text": text})

    latest = hist_rows[-1][1] if hist_rows else {}
    realloc = _to_int(latest.get("reallocated"), 0)
    pending = _to_int(latest.get("pending"), 0)
    uncor = max(
        _to_int(latest.get("uncorrectable"), 0),
        _to_int(latest.get("reported_uncorrectable"), 0),
    )
    cmd_timeout = _to_int(latest.get("command_timeout"), 0)
    poh = _to_int(latest.get("power_on_hours"), 0)

    # 控制器报告的盘状态优先级最高：Failed/UBad 盘往往已读不到 SMART，
    # 不能因计数器为空而显示"正常"
    state = str(row.get("state", "")).strip()
    if state in ("Failed", "UBad"):
        bump("crit", f"磁盘状态为 {state}，已脱离阵列无法工作，请立即更换")
    elif state in ("Offln", "Missing"):
        bump("warn", f"磁盘状态为 {state}，未在线")

    if _to_int(row.get("predictive_failure"), 0) > 0 or str(row.get("smart_alert")) == "Yes":
        bump("crit", "控制器已报告预测性故障（PF/SMART 告警），请尽快备份并更换磁盘")
    if pending > 0:
        bump("crit", f"存在 {pending} 个待定扇区（Current_Pending_Sector），介质故障前兆")
    if realloc >= 50:
        bump("crit", f"重映射扇区已达 {realloc} 个，介质劣化严重")
    elif realloc > 0:
        bump("warn", f"存在 {realloc} 个重映射扇区（Reallocated_Sector）")
    if uncor > 0:
        bump("warn", f"无法纠正错误累计 {uncor} 个")
    if cmd_timeout > 0:
        bump("info", f"命令超时累计 {cmd_timeout} 次")
    media_error = _to_int(row.get("media_error"), 0)
    if media_error > 0:
        bump("warn", f"控制器介质错误计数 {media_error}")
    temp = _to_int(row.get("temperature"))
    if temp is not None:
        if temp >= temp_crit:
            bump("warn", f"温度 {temp}°C 已达严重阈值，高温加速磁盘老化")
        elif temp >= temp_warn:
            bump("info", f"温度 {temp}°C 超过告警阈值")
    if poh >= 43800:
        bump("info", f"通电时长 {poh} 小时（超过 5 年），注意老化风险")

    # 趋势：与最早一份快照对比，关键计数器增长即升级
    if len(hist_rows) >= 2:
        first_day, first = hist_rows[0]
        last_day = hist_rows[-1][0]
        for field, name, lv in (
            ("pending", "待定扇区", "crit"),
            ("reallocated", "重映射扇区", "warn"),
            ("uncorrectable", "无法纠正错误", "warn"),
        ):
            delta = _to_int(latest.get(field), 0) - _to_int(first.get(field), 0)
            if delta > 0:
                bump(lv, f"{name}在 {first_day} ~ {last_day} 期间新增 {delta} 个，呈增长趋势")

    return {"level": level, "reasons": reasons}


def build_status() -> dict:
    now = time.time()
    if _status_cache["data"] is not None and now - _status_cache["ts"] < 15:
        return _status_cache["data"]

    date_dir = _latest_date_dir()
    disks_rows = _read_csv(date_dir / "disks.csv") if date_dir else []
    ctrl_rows = _read_csv(date_dir / "controller.csv") if date_dir else []
    vd_rows = _read_csv(date_dir / "vds.csv") if date_dir else []
    attr_rows = _read_csv(date_dir / "attributes.csv") if date_dir else []
    smart_rows = _read_csv(date_dir / "smart.csv") if date_dir else []
    patrol_rows = _read_csv(date_dir / "patrol.csv") if date_dir else []
    cc_rows = _read_csv(date_dir / "consistency.csv") if date_dir else []
    sys_rows = _read_csv(date_dir / "system.csv") if date_dir else []
    nvme_rows = _read_csv(date_dir / "nvme.csv") if date_dir else []

    # 每盘取最新一行
    latest_disks: dict[tuple, dict] = {}
    for row in disks_rows:
        key = (row.get("eid"), row.get("slot"))
        if row.get("state") == "N/A":
            continue
        latest_disks[key] = row

    attrs = {(a.get("eid"), a.get("slot")): a for a in attr_rows}
    smart_latest: dict[str, dict] = {}
    for s in smart_rows:
        smart_latest[s.get("did", "")] = s

    alert_cfg = lsi_alert.load_config()
    temp_warn = int(alert_cfg.get("temp_warn", 45))
    temp_crit = int(alert_cfg.get("temp_crit", 55))
    locate_state = _load_locate_state()
    smart_hist = _smart_history()

    physical_disks = []
    for (eid, slot), row in sorted(
        latest_disks.items(), key=lambda kv: (_to_int(kv[0][0], 0), _to_int(kv[0][1], 0))
    ):
        attr = attrs.get((eid, slot), {})
        smart = smart_latest.get(row.get("did", ""), {})
        temp = _to_int(row.get("temperature"))
        physical_disks.append(
            {
                "label": f"E{eid}:S{slot}",
                "eid": _to_int(eid, 0),
                "slot": _to_int(slot, 0),
                "did": _to_int(row.get("did")),
                "dg": row.get("dg", ""),
                "model": row.get("model", ""),
                "sn": attr.get("sn", ""),
                "fw_rev": attr.get("fw_rev", ""),
                "state": row.get("state", ""),
                "size": row.get("size", ""),
                "intf": row.get("intf", ""),
                "med": row.get("med", ""),
                "temperature": temp,
                "media_error": _to_int(row.get("media_error"), 0),
                "other_error": _to_int(row.get("other_error"), 0),
                "predictive_failure": _to_int(row.get("predictive_failure"), 0),
                "smart_alert": row.get("smart_alert", ""),
                "shield_counter": _to_int(row.get("shield_counter"), 0),
                "locate": f"{eid}:{slot}" in locate_state,
                "dev_speed": attr.get("dev_speed", ""),
                "link_speed": attr.get("link_speed", ""),
                "reallocated": _to_int(smart.get("reallocated"), 0),
                "pending": _to_int(smart.get("pending"), 0),
                "uncorrectable": _to_int(smart.get("uncorrectable"), 0),
                "power_on_hours": _to_int(smart.get("power_on_hours"), 0),
                "prediction": _predict_disk(
                    row,
                    smart_hist.get(str(row.get("did", "")), []),
                    temp_warn,
                    temp_crit,
                ),
            }
        )

    ctrl_row = ctrl_rows[-1] if ctrl_rows else {}
    controller = {
        "model": ctrl_row.get("model", ""),
        "fw": ctrl_row.get("fw_version", ""),
        "health": ctrl_row.get("health", ""),
        "num_disks": _to_int(ctrl_row.get("num_disks"), len(physical_disks)),
        "num_vds": _to_int(ctrl_row.get("num_vds"), len(vd_rows)),
        "roc_temp": _to_int(ctrl_row.get("roc_temp")),
        "bbu_model": ctrl_row.get("bbu_model", ""),
        "bbu_state": ctrl_row.get("bbu_state", ""),
        "bbu_temperature": _to_int(ctrl_row.get("bbu_temperature")),
    }

    virtual_disks = [
        {
            "dg_vd": v.get("dg_vd", ""),
            "type": v.get("type", ""),
            "state": v.get("state", ""),
            "size": v.get("size", ""),
            "name": v.get("name", ""),
        }
        for v in vd_rows
    ]

    # NVMe 直连盘（每分钟追加，按设备取最新一行）
    latest_nvme: dict[str, dict] = {}
    for row in nvme_rows:
        dev = str(row.get("device", "")).strip()
        if dev:
            latest_nvme[dev] = row
    nvme_disks = []
    for dev, row in sorted(latest_nvme.items()):
        nvme_disks.append(
            {
                "device": dev,
                "model": row.get("model", ""),
                "serial": row.get("serial", ""),
                "firmware": row.get("firmware", ""),
                "size": _fmt_bytes(row.get("size_bytes"), 1000),
                "used": _fmt_bytes(row.get("used_bytes"), 1000),
                "temperature": _to_int(row.get("temperature")),
                "available_spare": _to_int(row.get("available_spare")),
                "percentage_used": _to_int(row.get("percentage_used")),
                "critical_warning": str(row.get("critical_warning", "")).strip(),
                "power_on_hours": _to_int(row.get("power_on_hours")),
                "power_cycles": _to_int(row.get("power_cycles")),
                "unsafe_shutdowns": _to_int(row.get("unsafe_shutdowns")),
                "media_errors": _to_int(row.get("media_errors")),
            }
        )

    pr = patrol_rows[-1] if patrol_rows else {}
    cc = cc_rows[-1] if cc_rows else {}
    maintenance = {
        "patrol_read": {
            "mode": pr.get("pr_mode", ""),
            "state": pr.get("pr_state", ""),
            "next": pr.get("pr_next", ""),
            "iterations": _to_int(pr.get("pr_iterations"), 0),
        },
        "consistency_check": {
            "mode": cc.get("cc_mode", ""),
            "state": cc.get("cc_state", ""),
            "next": cc.get("cc_next", ""),
            "iterations": _to_int(cc.get("cc_iterations"), 0),
        },
    }

    sys_row = sys_rows[-1] if sys_rows else {}
    mem_total = _to_int(sys_row.get("mem_total_kb"), 0) or 0
    mem_avail = _to_int(sys_row.get("mem_avail_kb"), 0) or 0
    system = {
        "load": " ".join(
            sys_row.get(k, "") for k in ("load_1m", "load_5m", "load_15m")
        ).strip(),
        "memory": (
            f"{(mem_total - mem_avail) / 1048576:.1f}G / {mem_total / 1048576:.1f}G"
            if mem_total
            else ""
        ),
    }

    # 总体健康
    health = "ok"
    ctrl_health = controller.get("health", "")
    if ctrl_health and ctrl_health not in ("Optimal", "N/A"):
        health = "crit"
    for d in physical_disks:
        st = d["state"]
        t = d["temperature"]
        if st in ("Failed", "UBad") or d["smart_alert"] == "Yes" or (
            isinstance(t, int) and t >= temp_crit
        ):
            health = "crit"
            break
        if (
            st not in ("Onln", "UGood", "JBOD", "GHS")
            or (d["predictive_failure"] or 0) > 0
            or (d["media_error"] or 0) > 0
            or (d["reallocated"] or 0) > 0
            or (d["pending"] or 0) > 0
            or (isinstance(t, int) and t >= temp_warn)
        ):
            health = "warn" if health != "crit" else health
    if not date_dir or ctrl_health == "N/A":
        health = "unknown" if not date_dir else health

    timestamp = ctrl_row.get("timestamp") or (
        disks_rows[-1].get("timestamp") if disks_rows else ""
    )

    status = {
        "host": socket.gethostname(),
        "timestamp": timestamp,
        "health": health,
        "controller": controller,
        "virtual_disks": virtual_disks,
        "physical_disks": physical_disks,
        "nvme_disks": nvme_disks,
        "maintenance": maintenance,
        "system": system,
    }
    _status_cache.update(ts=now, data=status)
    return status


# ---- 页面 ----


@app.route("/")
def index():
    return render_template("index.html")


# ---- 认证 API ----


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = user_mgr.verify_password(username, password)
    if not role:
        lsi_alert.log_event("warning", f"登录失败: {username} ({request.remote_addr})")
        return jsonify(ok=False, error="用户名或口令错误"), 401
    session.clear()
    session["username"] = username
    session["role"] = role
    session.permanent = True
    lsi_alert.log_event("info", f"用户 {username} 登录")
    return jsonify(ok=True, username=username, role=role)


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    required = auth_required()
    return jsonify(
        auth_required=required,
        logged_in=bool(session.get("username")) if required else True,
        username=session.get("username", ""),
        role=current_role(),
    )


# ---- 监控 API ----


@app.get("/api/status")
@login_required
def api_status():
    return jsonify(build_status())


@app.get("/api/history")
@login_required
def api_history():
    hours = _to_int(request.args.get("hours"), 24)
    if hours not in (6, 24, 72):
        hours = 24
    since = datetime.now() - timedelta(hours=hours)

    series: dict[str, list] = {}
    for date_dir in _date_dirs():
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        if dir_date < since - timedelta(days=1):
            continue
        for row in _read_csv(date_dir / "disks.csv"):
            ts_str = row.get("timestamp", "")
            temp = _to_int(row.get("temperature"))
            if temp is None or row.get("state") == "N/A":
                continue
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts < since:
                continue
            label = f"E{row.get('eid')}:S{row.get('slot')}"
            series.setdefault(label, []).append(
                [int(ts.timestamp() * 1000), temp]
            )

    return jsonify(
        series=[
            {"label": label, "points": pts}
            for label, pts in sorted(series.items())
        ]
    )


@app.get("/api/events")
@login_required
def api_events():
    level = request.args.get("level", "all")
    page = max(_to_int(request.args.get("page"), 1) or 1, 1)
    page_size = min(max(_to_int(request.args.get("page_size"), 20) or 20, 1), 100)

    events = []
    events_file = lsi_alert.EVENTS_FILE
    if events_file.exists():
        try:
            with open(events_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if level != "all" and ev.get("level") != level:
                        continue
                    events.append(ev)
        except Exception:
            pass
    events.reverse()
    total = len(events)
    start = (page - 1) * page_size
    return jsonify(total=total, events=events[start : start + page_size])


# ---- 报警配置 API ----


@app.get("/api/alert_config")
@login_required
def api_alert_config_get():
    cfg = lsi_alert.load_config()
    return jsonify(
        enabled=lsi_alert.alert_enabled(cfg),
        sendmail_available=lsi_alert.sendmail_available(cfg),
        locked=lsi_alert.locked_fields(),
        config={
            "alert_email_to": cfg.get("alert_email_to", ""),
            "sendmail_path": cfg.get("sendmail_path", ""),
            "temp_warn": int(cfg.get("temp_warn", 45)),
            "temp_crit": int(cfg.get("temp_crit", 55)),
            "policies": lsi_alert.effective_policies(cfg),
        },
    )


@app.post("/api/alert_config")
@admin_required
def api_alert_config_save():
    data = request.get_json(silent=True) or {}
    cfg = {}
    if "alert_email_to" in data:
        cfg["alert_email_to"] = str(data["alert_email_to"]).strip()
    if "sendmail_path" in data:
        cfg["sendmail_path"] = str(data["sendmail_path"]).strip()
    for key in ("temp_warn", "temp_crit"):
        if key in data:
            val = _to_int(data[key])
            if val is None or not (0 <= val <= 100):
                return jsonify(ok=False, error=f"{key} 必须是 0-100 的整数"), 400
            cfg[key] = val
    if "policies" in data:
        pol_in = data["policies"]
        if not isinstance(pol_in, dict):
            return jsonify(ok=False, error="policies 必须是对象"), 400
        cfg["policies"] = {
            k: bool(pol_in.get(k, True)) for k in lsi_alert.DEFAULT_POLICIES
        }
    merged = lsi_alert.load_config()
    merged.update(cfg)
    if int(merged.get("temp_warn", 45)) > int(merged.get("temp_crit", 55)):
        return jsonify(ok=False, error="警告阈值不能高于临界阈值"), 400
    lsi_alert.save_config(cfg)
    lsi_alert.log_event("info", f"报警配置已更新（{session.get('username', 'system')}）")
    _status_cache.update(ts=0.0)
    return jsonify(ok=True)


@app.post("/api/alert_test")
@admin_required
def api_alert_test():
    ok, msg = lsi_alert.send_mail(
        "测试报警",
        f"这是一封来自 LSI RAID 监控的测试邮件。\n主机: {socket.gethostname()}\n时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
    )
    if ok:
        lsi_alert.log_event("info", "测试报警邮件发送成功")
        return jsonify(ok=True)
    return jsonify(ok=False, error=msg), 500


# ---- 采集控制 API ----


def _load_collection_config() -> dict:
    try:
        if COLLECTION_CONFIG_FILE.exists():
            with open(COLLECTION_CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if int(data.get("interval_minutes", 1)) in VALID_INTERVALS:
                return {"interval_minutes": int(data["interval_minutes"])}
    except Exception:
        pass
    return {"interval_minutes": 1}


@app.get("/api/collection_config")
@login_required
def api_collection_config_get():
    return jsonify(_load_collection_config())


@app.post("/api/collection_config")
@admin_required
def api_collection_config_save():
    data = request.get_json(silent=True) or {}
    interval = _to_int(data.get("interval_minutes"))
    if interval not in VALID_INTERVALS:
        return jsonify(ok=False, error=f"采集间隔仅支持 {VALID_INTERVALS} 分钟"), 400
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    with open(COLLECTION_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"interval_minutes": interval}, f)
    lsi_alert.log_event("info", f"采集间隔调整为 {interval} 分钟")
    return jsonify(ok=True)


@app.post("/api/collect_now")
@admin_required
def api_collect_now():
    try:
        proc = subprocess.run(
            [sys.executable, str(COLLECTD), "--force"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        _status_cache.update(ts=0.0)
        if proc.returncode == 0:
            lsi_alert.log_event("info", "手动采集完成")
            return jsonify(ok=True)
        return jsonify(ok=False, error=proc.stderr.strip()[-500:] or "采集失败"), 500
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="采集超时"), 500


@app.get("/api/export.csv")
@login_required
def api_export_csv():
    date_dir = _latest_date_dir()
    if not date_dir:
        return jsonify(ok=False, error="暂无数据"), 404
    csv_type = request.args.get("type", "disks")
    if not re.fullmatch(r"[a-z_]+", csv_type):
        return jsonify(ok=False, error="非法类型"), 400
    path = date_dir / f"{csv_type}.csv"
    if not path.exists():
        return jsonify(ok=False, error=f"{csv_type}.csv 不存在"), 404
    return send_file(
        path,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"lsi_{csv_type}_{date_dir.name}.csv",
    )


# ---- 磁盘操作 API ----


def _run_storcli(args: str, timeout: int = 45) -> tuple[bool, str]:
    cmd = f"sudo {STORCLI} {args}"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        output = proc.stdout.strip()
        json_start = output.find("{")
        if json_start != -1:
            try:
                data = json.loads(output[json_start:])
                for ctrl in data.get("Controllers", []):
                    status = ctrl.get("Command Status", {})
                    if status.get("Status") == "Success":
                        return True, "Success"
                    return False, status.get("Description", "storcli 返回失败")
            except json.JSONDecodeError:
                pass
        if proc.returncode == 0:
            return True, output[-500:] or "Success"
        return False, (proc.stderr.strip() or output[-500:]) or "storcli 执行失败"
    except FileNotFoundError:
        return False, f"storcli 不存在: {STORCLI}"
    except subprocess.TimeoutExpired:
        return False, "storcli 执行超时"
    except Exception as e:
        return False, str(e)


@app.post("/api/disk_action")
@admin_required
def api_disk_action():
    data = request.get_json(silent=True) or {}
    eid = _to_int(data.get("eid"))
    slot = _to_int(data.get("slot"))
    action = str(data.get("action", ""))
    if eid is None or slot is None or action not in DISK_ACTIONS:
        return jsonify(ok=False, error="参数非法"), 400
    cmd_suffix = DISK_ACTIONS[action]
    if action == "hotspare_dedicated":
        dg = _to_int(data.get("dg"))
        if dg is None:
            return jsonify(ok=False, error="请指定磁盘组(DG)编号"), 400
        cmd_suffix += f" DGs={dg}"
    ok, msg = _run_storcli(
        f"{CONTROLLER}/e{eid}/s{slot} {cmd_suffix}"
    )
    label = f"E{eid}:S{slot}"
    if ok:
        if action == "locate_start":
            _set_locate(eid, slot, True)
        elif action == "locate_stop":
            _set_locate(eid, slot, False)
        lsi_alert.log_event(
            "warning", f"磁盘操作: {label} {action}（{session.get('username', '')}）"
        )
        _status_cache.update(ts=0.0)
        return jsonify(ok=True)
    lsi_alert.log_event("error", f"磁盘操作失败: {label} {action} — {msg}")
    return jsonify(ok=False, error=msg), 500


@app.get("/api/disk_smart")
@login_required
def api_disk_smart():
    eid = _to_int(request.args.get("eid"))
    slot = _to_int(request.args.get("slot"))
    if eid is None or slot is None:
        return jsonify(error="参数非法"), 400
    status = build_status()
    did = None
    found = False
    for d in status["physical_disks"]:
        if d["eid"] == eid and d["slot"] == slot:
            found = True
            did = d.get("did")
            break
    if not found:
        return jsonify(error="未找到该磁盘"), 404

    # 先尝试 smartctl 透传（仅对已组阵列、有有效 DID 的盘可靠）
    if did is not None:
        try:
            proc = subprocess.run(
                f"sudo {SMARTCTL} -a -d megaraid,{did} {_smart_base_device()}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = proc.stdout or proc.stderr
            if output.strip():
                parsed = parse_smart_output(output)
                if parsed["attrs"] or parsed["scsi"]:
                    return jsonify(output=output, attrs=parsed["attrs"], scsi=parsed["scsi"])
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            pass

    # smartctl 无法获取结构化数据（如 UGood/JBOD 未组阵列盘），改用 storcli 读原始 SMART
    hex_str = _storcli_smart_hex(eid, slot)
    if hex_str:
        attrs = parse_smart_hex(hex_str)
        return jsonify(output=f"[storcli 原始 SMART 数据]\n{hex_str}", attrs=attrs, scsi={})
    return jsonify(error="无法获取该磁盘的 SMART 数据"), 500


# ---- 存储管理 API ----


@app.get("/api/storage/devices")
@login_required
def api_storage_devices():
    try:
        return jsonify(devices=storage_mgr.list_devices())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.post("/api/storage/mount")
@admin_required
def api_storage_mount():
    data = request.get_json(silent=True) or {}
    ok, msg = storage_mgr.mount_device(
        str(data.get("device", "")), str(data.get("mountpoint", ""))
    )
    lsi_alert.log_event(
        "warning" if ok else "error",
        f"存储挂载 {data.get('device')} → {data.get('mountpoint')}: {msg}",
    )
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/storage/umount")
@admin_required
def api_storage_umount():
    data = request.get_json(silent=True) or {}
    ok, msg = storage_mgr.umount_device(str(data.get("device", "")))
    lsi_alert.log_event(
        "warning" if ok else "error", f"存储卸载 {data.get('device')}: {msg}"
    )
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/storage/format")
@admin_required
def api_storage_format():
    data = request.get_json(silent=True) or {}
    device = str(data.get("device", ""))
    fs_type = str(data.get("fs_type", ""))
    ok, msg = storage_mgr.format_device(device, fs_type)
    lsi_alert.log_event(
        "warning" if ok else "error",
        f"存储格式化 {device} 为 {fs_type}: {msg}（{session.get('username', '')}）",
    )
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error=msg), 500)


# ---- NFS 共享管理 API ----


@app.get("/api/nfs/exports")
@login_required
def api_nfs_list():
    try:
        return jsonify(
            available=storage_mgr.nfs_available(),
            exports=storage_mgr.list_exports(),
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.post("/api/nfs/exports")
@admin_required
def api_nfs_add():
    data = request.get_json(silent=True) or {}
    path = str(data.get("path", "")).strip()
    host = str(data.get("host", "")).strip() or "*"
    options = data.get("options")
    if not isinstance(options, list):
        options = []
    ok, msg = storage_mgr.add_export(path, host, [str(o) for o in options])
    lsi_alert.log_event(
        "warning" if ok else "error",
        f"NFS 添加共享 {path} → {host}: {msg}（{session.get('username', '')}）",
    )
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/nfs/exports/delete")
@admin_required
def api_nfs_remove():
    data = request.get_json(silent=True) or {}
    path = str(data.get("path", "")).strip()
    host = str(data.get("host", "")).strip()
    if not path or not host:
        return jsonify(ok=False, error="参数非法"), 400
    ok, msg = storage_mgr.remove_export(path, host)
    lsi_alert.log_event(
        "warning" if ok else "error",
        f"NFS 移除共享 {path} → {host}: {msg}（{session.get('username', '')}）",
    )
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error=msg), 500)


# ---- 用户管理 API ----


@app.get("/api/users")
@admin_required
def api_users_list():
    return jsonify(users=user_mgr.list_users())


@app.post("/api/users")
@admin_required
def api_users_create():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    ok, msg = user_mgr.create_user(
        username, str(data.get("password", "")), str(data.get("role", "viewer"))
    )
    if ok:
        lsi_alert.log_event(
            "info", f"创建用户 {username}（{session.get('username', 'system')}）"
        )
        return jsonify(ok=True)
    return jsonify(ok=False, error=msg), 400


@app.delete("/api/users/<username>")
@admin_required
def api_users_delete(username: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        return jsonify(ok=False, error="非法用户名"), 400
    if username == session.get("username"):
        return jsonify(ok=False, error="不能删除当前登录用户"), 400
    ok, msg = user_mgr.delete_user(username)
    if ok:
        lsi_alert.log_event("warning", f"删除用户 {username}")
        return jsonify(ok=True)
    return jsonify(ok=False, error=msg), 400


@app.post("/api/users/<username>/password")
@admin_required
def api_users_reset_password(username: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        return jsonify(ok=False, error="非法用户名"), 400
    data = request.get_json(silent=True) or {}
    ok, msg = user_mgr.set_password(username, str(data.get("password", "")))
    if ok:
        lsi_alert.log_event("warning", f"重置用户 {username} 口令")
        return jsonify(ok=True)
    return jsonify(ok=False, error=msg), 400


# ---- 实时系统性能 ----

_DISK_NAME_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|hd[a-z]+)$")


def _read_cpu_times() -> tuple[int, int]:
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def _read_diskstats() -> dict:
    stats = {}
    with open("/proc/diskstats") as f:
        for line in f:
            parts = line.split()
            if len(parts) < 14 or not _DISK_NAME_RE.match(parts[2]):
                continue
            try:
                stats[parts[2]] = {
                    "reads": int(parts[3]),
                    "sectors_read": int(parts[5]),
                    "writes": int(parts[7]),
                    "sectors_written": int(parts[9]),
                    "ios_in_progress": int(parts[11]),
                }
            except ValueError:
                continue
    return stats


@app.get("/api/system/realtime")
@login_required
def api_system_realtime():
    try:
        total1, idle1 = _read_cpu_times()
        io1 = _read_diskstats()
        time.sleep(1)
        total2, idle2 = _read_cpu_times()
        io2 = _read_diskstats()
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

    cpu_percent = 0.0
    if total2 > total1:
        cpu_percent = round((1 - (idle2 - idle1) / (total2 - total1)) * 100, 1)

    io = []
    for name, s2 in sorted(io2.items()):
        s1 = io1.get(name)
        if not s1:
            continue
        io.append(
            {
                "name": name,
                "read_bps": (s2["sectors_read"] - s1["sectors_read"]) * 512,
                "write_bps": (s2["sectors_written"] - s1["sectors_written"]) * 512,
                "iops": (s2["reads"] - s1["reads"]) + (s2["writes"] - s1["writes"]),
                "ios_in_progress": s2["ios_in_progress"],
            }
        )

    mem_total = mem_avail = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable"):
                    mem_avail = int(line.split()[1])
    except Exception:
        pass

    load = []
    try:
        with open("/proc/loadavg") as f:
            load = [float(x) for x in f.read().split()[:3]]
    except Exception:
        pass

    uptime = 0.0
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
    except Exception:
        pass

    return jsonify(
        cpu_percent=cpu_percent,
        mem_total_kb=mem_total,
        mem_avail_kb=mem_avail,
        load=load,
        uptime_seconds=uptime,
        io=io,
    )


# ---- 文件系统用量（实时）----


def _fs_usage() -> list[dict]:
    rows = []
    seen = set()
    try:
        with open("/proc/mounts") as f:
            mounts = [line.split() for line in f if line.strip()]
    except Exception:
        return rows
    for parts in mounts:
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        if not device.startswith("/dev/") or mountpoint in seen:
            continue
        seen.add(mountpoint)
        try:
            st = os.statvfs(mountpoint)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        used = total - free
        rows.append(
            {
                "device": device,
                "mountpoint": mountpoint,
                "fstype": fstype,
                "size": total,
                "used": used,
                "avail": avail,
                "use_percent": round(used / total * 100, 1) if total else 0.0,
                "inode_total": st.f_files,
                "inode_used": st.f_files - st.f_ffree,
                "inode_use_percent": round(
                    (st.f_files - st.f_ffree) / st.f_files * 100, 1
                )
                if st.f_files
                else 0.0,
            }
        )
    return rows


@app.get("/api/storage/usage")
@login_required
def api_storage_usage():
    hidden = _load_fs_hidden()
    rows = _fs_usage()
    for r in rows:
        r["hidden"] = r["mountpoint"] in hidden
    return jsonify(filesystems=rows)


@app.post("/api/storage/visibility")
@admin_required
def api_storage_visibility():
    data = request.get_json(silent=True) or {}
    mountpoint = str(data.get("mountpoint", "")).strip()
    if not re.fullmatch(r"/[A-Za-z0-9/_.-]+", mountpoint or ""):
        return jsonify(ok=False, error="非法挂载点"), 400
    hidden = _load_fs_hidden()
    if data.get("hidden"):
        hidden.add(mountpoint)
    else:
        hidden.discard(mountpoint)
    _save_fs_hidden(hidden)
    return jsonify(ok=True, hidden=sorted(hidden))


@app.get("/api/fs_history")
@login_required
def api_fs_history():
    hours = _to_int(request.args.get("hours"), 24)
    if hours not in (6, 24, 72):
        hours = 24
    since = datetime.now() - timedelta(hours=hours)
    hidden = _load_fs_hidden()
    series: dict[str, list] = {}
    for date_dir in _date_dirs():
        try:
            if datetime.strptime(date_dir.name, "%Y-%m-%d") < since - timedelta(days=1):
                continue
        except ValueError:
            continue
        for row in _read_csv(date_dir / "fs.csv"):
            if row.get("mountpoint") in hidden:
                continue
            try:
                ts = datetime.strptime(row.get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts < since:
                continue
            label = f"{row.get('device')} ({row.get('mountpoint')})"
            try:
                pct = float(row.get("use_percent") or 0)
            except ValueError:
                continue
            series.setdefault(label, []).append(
                [int(ts.timestamp() * 1000), pct]
            )
    return jsonify(
        series=[{"label": k, "points": v} for k, v in sorted(series.items())]
    )


@app.get("/api/io_history")
@login_required
def api_io_history():
    hours = _to_int(request.args.get("hours"), 24)
    if hours not in (6, 24, 72):
        hours = 24
    since = datetime.now() - timedelta(hours=hours)

    samples: dict[str, list] = {}
    for date_dir in _date_dirs():
        try:
            if datetime.strptime(date_dir.name, "%Y-%m-%d") < since - timedelta(days=1):
                continue
        except ValueError:
            continue
        for row in _read_csv(date_dir / "io.csv"):
            try:
                ts = datetime.strptime(row.get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if ts < since:
                continue
            samples.setdefault(row.get("name", ""), []).append(
                (
                    ts,
                    _to_int(row.get("sectors_read"), 0),
                    _to_int(row.get("sectors_written"), 0),
                    _to_int(row.get("reads"), 0) + _to_int(row.get("writes"), 0),
                )
            )

    series = []
    for name, pts in sorted(samples.items()):
        pts.sort(key=lambda p: p[0])
        out = []
        for (t1, r1, w1, io1), (t2, r2, w2, io2) in zip(pts, pts[1:]):
            dt = (t2 - t1).total_seconds()
            if dt <= 0 or r2 < r1 or w2 < w1:
                continue
            out.append(
                [
                    int(t2.timestamp() * 1000),
                    round((r2 - r1) * 512 / 1024 / dt, 1),
                    round((w2 - w1) * 512 / 1024 / dt, 1),
                    round((io2 - io1) / dt, 1),
                ]
            )
        series.append({"label": name, "points": out})
    return jsonify(series=series)


# ---- SMART 解析 ----

_ATA_ATTR_RE = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+(0x[0-9a-fA-F]+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$"
)


def parse_smart_output(output: str) -> dict:
    attrs = []
    scsi = {}
    for line in output.splitlines():
        m = _ATA_ATTR_RE.match(line)
        if m:
            attrs.append(
                {
                    "id": int(m.group(1)),
                    "name": m.group(2),
                    "value": int(m.group(4)),
                    "worst": int(m.group(5)),
                    "thresh": int(m.group(6)),
                    "type": m.group(7),
                    "updated": m.group(8),
                    "raw": m.group(10).strip(),
                }
            )
            continue
        for label, key in (
            ("Current Drive Temperature", "temperature"),
            ("Accumulated power on time, hours:minutes", "power_on_time"),
            ("Elements in grown defect list", "grown_defects"),
            ("Non-medium error count", "non_medium_errors"),
            ("Manufactured in week", "manufactured"),
            ("Accumulated start-stop cycles", "start_stop_cycles"),
            ("Accumulated load-unload cycles", "load_unload_cycles"),
            ("Percentage used endurance indicator", "endurance_used_pct"),
        ):
            if label.upper() in line.upper():
                scsi[key] = line.split(":", 1)[-1].strip()
    return {"attrs": attrs, "scsi": scsi}


# ATA SMART 属性 ID → 名称映射（用于解析 storcli 返回的原始 SMART 字节串）
_ATA_SMART_ATTRS = {
    1: "Raw_Read_Error_Rate",
    2: "Throughput_Performance",
    3: "Spin_Up_Time",
    4: "Start_Stop_Count",
    5: "Reallocated_Sector_Ct",
    7: "Seek_Error_Rate",
    8: "Seek_Time_Performance",
    9: "Power_On_Hours",
    10: "Spin_Retry_Count",
    12: "Power_Cycle_Count",
    13: "Soft_Read_Error_Rate",
    170: "Available_Reservd_Space",
    171: "Program_Fail_Count",
    172: "Erase_Fail_Count",
    173: "Wear_Leveling_Count",
    174: "Unexpected_Power_Loss",
    175: "Program_Fail_Count_Chip",
    177: "Wear_Range_Delta",
    178: "Used_Rsvd_Blk_Cnt",
    179: "Used_Rsvd_Blk_Cnt_Tot",
    180: "Unused_Rsvd_Blk_Cnt",
    181: "Program_Fail_Cnt_Total",
    182: "Erase_Fail_Count",
    183: "SATA_Downshift_Count",
    184: "End-to-End_Error",
    185: "Head_Stability",
    186: "Induced_Op-Vibration",
    187: "Reported_Uncorrect",
    188: "Command_Timeout",
    189: "High_Fly_Writes",
    190: "Airflow_Temperature_Cel",
    191: "G-Sense_Error_Rate",
    192: "Power-Off_Retract_Count",
    193: "Load_Cycle_Count",
    194: "Temperature_Celsius",
    195: "Hardware_ECC_Recovered",
    196: "Reallocated_Event_Count",
    197: "Current_Pending_Sector",
    198: "Offline_Uncorrectable",
    199: "UDMA_CRC_Error_Count",
    200: "Multi_Zone_Error_Rate",
    201: "Soft_Read_Error_Rate",
    202: "Data_Address_Mark_Errs",
    203: "Run_Out_Cancel",
    204: "Soft_ECC_Correction",
    205: "Thermal_Asperity_Rate",
    206: "Flying_Height",
    207: "Spin_High_Current",
    208: "Spin_Buzz",
    209: "Offline_Seek_Performance",
    220: "Disk_Shift",
    221: "G-Sense_Error_Rate",
    222: "Loaded_Hours",
    223: "Load_Retry_Count",
    224: "Load_Friction",
    225: "Load_Unload_Retry_Count",
    226: "Load-In_Time",
    227: "Torque_Amplification_Count",
    228: "Power-Off_Retract_Count",
    229: "Head_Flying_Hours",
    230: "Life_Left_SSD",
    231: "Life_Left",
    232: "Available_Reservd_Space",
    233: "Media_Wearout_Indicator",
    234: "Average_Erase_Count",
    235: "Good_Block_Count",
    240: "Head_Flying_Hours",
    241: "Total_LBAs_Written",
    242: "Total_LBAs_Read",
    243: "Total_NAND_Writes",
    244: "Thermal_Throttle",
    245: "Timed_Workload_Media_Wear",
    246: "Timed_Workload_Host_Reads",
    247: "Timed_Workload_Timer",
}


def parse_smart_hex(hex_str: str) -> list[dict]:
    """解析 storcli `show smart` 返回的原始 ATA SMART 字节串（hex）为属性列表。

    ATA SMART Read Data 结构：前 2 字节为版本号，之后每 12 字节一个属性
    （ID/flags/value/worst/raw6），raw 为 6 字节小端。
    """
    attrs = []
    cleaned = re.sub(r"[^0-9a-fA-F]", "", hex_str or "")
    if len(cleaned) < 4:
        return attrs
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        return attrs
    for off in range(2, min(len(raw) - 11, 2 + 30 * 12), 12):
        aid = raw[off]
        if aid == 0:
            continue
        flags = raw[off + 1] | (raw[off + 2] << 8)
        value = raw[off + 3]
        worst = raw[off + 4]
        raw_val = int.from_bytes(raw[off + 5 : off + 11], "little")
        attrs.append(
            {
                "id": aid,
                "name": _ATA_SMART_ATTRS.get(aid, f"Unknown_Attribute_{aid}"),
                "value": value,
                "worst": worst,
                "thresh": 0,
                "type": "Pre-fail" if flags & 0x01 else "Old_age",
                "updated": "Always" if flags & 0x02 else "Offline",
                "raw": str(raw_val),
            }
        )
    return attrs


def _storcli_smart_hex(eid: int, slot: int) -> str | None:
    """通过 storcli 读取指定物理盘（含未组阵列的 UGood/JBOD 盘）的原始 SMART 数据。"""
    try:
        proc = subprocess.run(
            f"sudo {STORCLI} {CONTROLLER}/e{eid}/s{slot} show smart J",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = proc.stdout.strip()
        json_start = output.find("{")
        if json_start == -1:
            return None
        data = json.loads(output[json_start:])
        resp = data["Controllers"][0].get("Response Data", {})
        for val in resp.values():
            if isinstance(val, str) and val.strip():
                return val
    except Exception:
        pass
    return None


# ---- RAID 维护操作 ----

RAID_ACTIONS = {
    "patrolread": {
        "start": "{ctrl} start patrolread",
        "stop": "{ctrl} stop patrolread",
        "pause": "{ctrl} pause patrolread",
        "resume": "{ctrl} resume patrolread",
    },
    "cc": {
        "start": "{ctrl}/v{vd} start cc",
        "stop": "{ctrl}/v{vd} stop cc",
    },
    "vd_init": {
        "start": "{ctrl}/v{vd} start init",
        "stop": "{ctrl}/v{vd} stop init",
    },
    "vd_delete": {
        "delete": "{ctrl}/v{vd} del force",
    },
    "vd_import": {
        "import": "{ctrl}/fall import",
    },
}


@app.post("/api/raid_action")
@admin_required
def api_raid_action():
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", ""))
    action = str(data.get("action", ""))
    vd = _to_int(data.get("vd"))
    if target not in RAID_ACTIONS or action not in RAID_ACTIONS[target]:
        return jsonify(ok=False, error="非法操作"), 400
    if "{vd}" in RAID_ACTIONS[target][action] and vd is None:
        return jsonify(ok=False, error="缺少 VD 编号"), 400
    cmd = RAID_ACTIONS[target][action].format(ctrl=CONTROLLER, vd=vd if vd is not None else 0)
    ok, msg = _run_storcli(cmd)
    desc = f"RAID 操作: {target} {action}" + (f" (v{vd})" if vd is not None else "")
    if ok:
        lsi_alert.log_event("warning", f"{desc}（{session.get('username', '')}）")
        _status_cache.update(ts=0.0)
        return jsonify(ok=True)
    lsi_alert.log_event("error", f"{desc} 失败 — {msg}")
    return jsonify(ok=False, error=msg), 500


def _fill_vd_disks_by_dg(vds: list[dict]):
    """对成员盘列表为空的 VD（storcli 明细查询失败），按物理盘摘要中的
    DG 归属补充成员盘。DG/VD 形如 "2/1"，"/" 前为 DG 号。"""
    try:
        cmd = f"sudo {STORCLI} {CONTROLLER}/eall/sall show J"
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=45)
        output = proc.stdout.strip()
        json_start = output.find("{")
        if json_start == -1:
            return
        resp = json.loads(output[json_start:])["Controllers"][0].get("Response Data", {})
        by_dg: dict[str, list] = {}
        for val in resp.values():
            if not isinstance(val, list):
                continue
            for s in val:
                if not isinstance(s, dict) or "EID:Slt" not in s or "DG" not in s:
                    continue
                dg = str(s.get("DG", ""))
                by_dg.setdefault(dg, []).append(
                    {
                        "slot": s.get("EID:Slt", ""),
                        "did": s.get("DID", ""),
                        "state": s.get("State", ""),
                        "size": s.get("Size", ""),
                        "intf": s.get("Intf", ""),
                        "med": s.get("Med", ""),
                        "model": s.get("Model", ""),
                    }
                )
        for v in vds:
            dg = str(v.get("dg_vd", "")).split("/")[0]
            v["disks"] = by_dg.get(dg, [])
    except Exception:
        pass


@app.get("/api/vd_detail")
@login_required
def api_vd_detail():
    cmd = f"sudo {STORCLI} {CONTROLLER}/vall show all J"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=45)
        output = proc.stdout.strip()
        json_start = output.find("{")
        if json_start == -1:
            return jsonify(ok=False, error="storcli 无输出"), 500
        data = json.loads(output[json_start:])
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

    try:
        resp = data["Controllers"][0].get("Response Data", {})
        vds = []
        for key, val in resp.items():
            m = re.match(r"/c\d+/v(\d+)$", key)
            if not m or not isinstance(val, list) or not val:
                continue
            summary = val[0] if isinstance(val[0], dict) else {}
            vd_num = int(m.group(1))
            props = resp.get(f"VD{vd_num} Properties") or {}
            pds = resp.get(f"PDs for VD {vd_num}") or []
            vds.append(
                {
                    "vd": vd_num,
                    "dg_vd": summary.get("DG/VD", f"{vd_num}/{vd_num}"),
                    "type": summary.get("TYPE", ""),
                    "state": summary.get("State", ""),
                    "access": summary.get("Access", ""),
                    "cache": summary.get("Cache", ""),
                    "size": summary.get("Size", ""),
                    "name": summary.get("Name", ""),
                    "current_operation": props.get("Active Operations", "None"),
                    "os_device": props.get("OS Drive Name", ""),
                    "write_cache": props.get("Write Cache(initial setting)", ""),
                    "disks": [
                        {
                            "slot": p.get("EID:Slt", ""),
                            "did": p.get("DID", ""),
                            "state": p.get("State", ""),
                            "size": p.get("Size", ""),
                            "intf": p.get("Intf", ""),
                            "med": p.get("Med", ""),
                            "model": p.get("Model", ""),
                        }
                        for p in pds
                        if isinstance(p, dict)
                    ],
                }
            )
        # VD 降级/有盘 Failed 时 storcli 可能不返回该 VD 的明细（ErrCd 45），
        # 成员盘列表为空，改用物理盘的 DG 归属兜底
        missing = [v for v in vds if not v["disks"]]
        if missing:
            _fill_vd_disks_by_dg(missing)
        return jsonify(vds=sorted(vds, key=lambda v: v["vd"]))
    except Exception as e:
        return jsonify(ok=False, error=f"解析失败: {e}"), 500


_events_cache: dict = {"ts": 0.0, "data": None}


@app.get("/api/controller_events")
@login_required
def api_controller_events():
    lines = min(_to_int(request.args.get("lines"), 200) or 200, 1000)
    q = (request.args.get("q") or "").strip()
    now = time.time()
    if _events_cache["data"] is not None and now - _events_cache["ts"] < 60:
        text = _events_cache["data"]
    else:
        ok, msg = _run_storcli_text(f"{CONTROLLER} show events", timeout=90)
        if not ok:
            return jsonify(ok=False, error=msg), 500
        text = msg
        _events_cache.update(ts=now, data=text)
    all_lines = text.splitlines()
    # 剥离 storcli 输出尾部的状态栏样板
    for i, ln in enumerate(all_lines):
        if ln.startswith(("Description =", "Events =", "Controller Properties", "Status =", "CLI Version", "Operating system", "Controller =")):
            all_lines = all_lines[:i]
            break
    # 关键字筛选：大小写不敏感，空格分隔多个关键字，任一匹配即保留
    if q:
        keys = [k.lower() for k in q.split() if k]
        matched = [ln for ln in all_lines if any(k in ln.lower() for k in keys)]
    else:
        matched = all_lines
    tail = matched[-lines:]
    return jsonify(output="\n".join(tail), total_lines=len(matched))


def _run_storcli_text(args: str, timeout: int = 60) -> tuple[bool, str]:
    cmd = f"sudo {STORCLI} {args}"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            return True, proc.stdout
        return False, (proc.stderr.strip() or "storcli 无输出")
    except FileNotFoundError:
        return False, f"storcli 不存在: {STORCLI}"
    except subprocess.TimeoutExpired:
        return False, "storcli 执行超时"
    except Exception as e:
        return False, str(e)


# ---- 阵列卡报警（蜂鸣器）开关 ----

_alarm_cache: dict = {"ts": 0.0, "state": None}

ALARM_MODES = {"on": "打开", "silence": "临时关闭", "off": "永久关闭"}


def _read_ctrl_prop(prop: str) -> str | None:
    """读取控制器布尔属性（alarm / jbod 等），返回 ON/OFF 等大写值"""
    cmd = f"sudo {STORCLI} {CONTROLLER} show {prop} J"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        output = proc.stdout.strip()
        json_start = output.find("{")
        if json_start == -1:
            return None
        data = json.loads(output[json_start:])
        props = data["Controllers"][0]["Response Data"]["Controller Properties"]
        for p in props:
            if str(p.get("Ctrl_Prop", "")).strip().lower() == prop.lower():
                return str(p.get("Value", "")).strip().upper()
    except Exception:
        pass
    return None


@app.get("/api/controller_alarm")
@login_required
def api_controller_alarm_get():
    now = time.time()
    if _alarm_cache["state"] is not None and now - _alarm_cache["ts"] < 60:
        state = _alarm_cache["state"]
    else:
        state = _read_ctrl_prop("alarm")
        _alarm_cache.update(ts=now, state=state)
    return jsonify(ok=state is not None, alarm=state)


@app.post("/api/controller_alarm")
@admin_required
def api_controller_alarm_set():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip().lower()
    if mode not in ALARM_MODES:
        return jsonify(ok=False, error="非法模式"), 400
    ok, msg = _run_storcli_text(f"{CONTROLLER} set alarm={mode}", timeout=30)
    if ok and "Status = Success" in msg:
        lsi_alert.log_event(
            "warning", f"阵列卡报警已{ALARM_MODES[mode]}（{session.get('username', '')}）"
        )
        _alarm_cache.update(ts=0.0, state=None)
        return jsonify(ok=True)
    return jsonify(ok=False, error="storcli 执行失败" if ok else msg), 500


# ---- JBOD 模式开关 ----

_jbod_cache: dict = {"ts": 0.0, "state": None}

JBOD_MODES = {"on": "打开", "off": "关闭"}


@app.get("/api/controller_jbod")
@login_required
def api_controller_jbod_get():
    now = time.time()
    if _jbod_cache["state"] is not None and now - _jbod_cache["ts"] < 60:
        state = _jbod_cache["state"]
    else:
        state = _read_ctrl_prop("jbod")
        _jbod_cache.update(ts=now, state=state)
    return jsonify(ok=state is not None, jbod=state)


@app.post("/api/controller_jbod")
@admin_required
def api_controller_jbod_set():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip().lower()
    if mode not in JBOD_MODES:
        return jsonify(ok=False, error="非法模式"), 400
    ok, msg = _run_storcli_text(f"{CONTROLLER} set jbod={mode}", timeout=30)
    if ok and "Status = Success" in msg:
        lsi_alert.log_event(
            "warning", f"JBOD 模式已{JBOD_MODES[mode]}（{session.get('username', '')}）"
        )
        _jbod_cache.update(ts=0.0, state=None)
        return jsonify(ok=True)
    return jsonify(ok=False, error="storcli 执行失败" if ok else msg), 500


# ---- 日志打包下载 ----


@app.get("/api/logs/download")
@login_required
def api_logs_download():
    """收集控制器 alilog、控制器事件与平台事件日志，打包 zip 下载"""
    import io
    import zipfile

    files = {}
    ok, out = _run_storcli_text(f"{CONTROLLER} show alilog", timeout=180)
    files["alilog.txt"] = out if ok else f"alilog 获取失败: {out}"
    ok, out = _run_storcli_text(f"{CONTROLLER} show events", timeout=90)
    files["controller_events.txt"] = out if ok else f"控制器事件获取失败: {out}"
    try:
        files["app_events.jsonl"] = (
            lsi_alert.EVENTS_FILE.read_text(encoding="utf-8")
            if lsi_alert.EVENTS_FILE.exists()
            else ""
        )
    except Exception as e:
        files["app_events.jsonl"] = f"事件日志读取失败: {e}"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    buf.seek(0)
    fname = f"lsi-logs-{datetime.now():%Y%m%d-%H%M%S}.zip"
    lsi_alert.log_event("info", f"日志包已下载（{session.get('username', '')}）")
    return send_file(
        buf, mimetype="application/zip", as_attachment=True, download_name=fname
    )


# ---- 创建磁盘阵列 ----

# RAID 级别 -> (最少盘数, 额外约束)
RAID_LEVEL_RULES = {
    "0": (1, None),
    "1": (2, "exact2"),
    "5": (3, None),
    "6": (4, None),
    "10": (4, "even"),
    "50": (6, "mult3"),
}


@app.post("/api/raid/create")
@admin_required
def api_raid_create():
    data = request.get_json(silent=True) or {}
    level = str(data.get("level", ""))
    drives = data.get("drives")
    name = str(data.get("name", "")).strip()

    if level not in RAID_LEVEL_RULES:
        return jsonify(ok=False, error="非法 RAID 级别，支持 0/1/5/6/10/50"), 400
    if not isinstance(drives, list) or not (1 <= len(drives) <= 32):
        return jsonify(ok=False, error="磁盘数量非法"), 400

    n = len(drives)
    min_drives, extra = RAID_LEVEL_RULES[level]
    if n < min_drives:
        return jsonify(ok=False, error=f"RAID{level} 至少需要 {min_drives} 块盘"), 400
    if extra == "exact2" and n != 2:
        return jsonify(ok=False, error="RAID1 需要恰好 2 块盘（更多镜像对请用 RAID10）"), 400
    if extra == "even" and n % 2 != 0:
        return jsonify(ok=False, error="RAID10 需要偶数块盘"), 400
    if extra == "mult3" and n % 3 != 0:
        return jsonify(ok=False, error="RAID50 盘数需为 3 的倍数（每个 RAID5 子组 3 块盘）"), 400
    if name and not re.fullmatch(r"[\w .-]{1,15}", name):
        return jsonify(ok=False, error="阵列名称仅支持字母数字/空格/._-，最长 15 字符"), 400

    # 校验每块盘存在且处于未配置状态（UGood/JBOD）
    status = build_status()
    pd_map = {(d["eid"], d["slot"]): d for d in status["physical_disks"]}
    specs = []
    for dr in drives:
        eid = _to_int(dr.get("eid") if isinstance(dr, dict) else None)
        slot = _to_int(dr.get("slot") if isinstance(dr, dict) else None)
        if eid is None or slot is None:
            return jsonify(ok=False, error="磁盘参数非法"), 400
        pd = pd_map.get((eid, slot))
        if not pd:
            return jsonify(ok=False, error=f"磁盘 E{eid}:S{slot} 不存在"), 400
        state = pd.get("state", "")
        if state == "JBOD":
            # JBOD 盘需先转为 UGood
            ok, msg = _run_storcli(f"{CONTROLLER}/e{eid}/s{slot} set good force")
            if not ok:
                return jsonify(
                    ok=False, error=f"磁盘 E{eid}:S{slot} JBOD 转 UGood 失败: {msg}"
                ), 500
            lsi_alert.log_event("warning", f"磁盘 E{eid}:S{slot} JBOD → UGood（创建阵列前置）")
        elif state != "UGood":
            return jsonify(
                ok=False,
                error=f"磁盘 E{eid}:S{slot} 状态为 {state or '未知'}，仅未配置（UGood/JBOD）的磁盘可创建阵列",
            ), 400
        specs.append(f"{eid}:{slot}")

    if len(set(specs)) != len(specs):
        return jsonify(ok=False, error="磁盘列表有重复"), 400

    # storcli add vd 语法要求参数顺序为：r<level> [name=...] drives=... [PDperArray=...]
    cmd = f"{CONTROLLER} add vd r{level}"
    if name:
        cmd += f" name={name}"
    cmd += f" drives={','.join(specs)}"
    if level == "50":
        # RAID50 必须指定每个 RAID5 子组的盘数，固定为 3（盘数已校验为 3 的倍数）
        cmd += " pdperarray=3"
    ok, msg = _run_storcli(cmd)
    if ok:
        lsi_alert.log_event(
            "warning",
            f"创建阵列 RAID{level} drives={','.join(specs)}"
            + (f" 名称={name}" if name else "")
            + f"（{session.get('username', '')}）",
        )
        _status_cache.update(ts=0.0)
        return jsonify(ok=True)
    lsi_alert.log_event("error", f"创建阵列失败 RAID{level} drives={','.join(specs)} — {msg}")
    return jsonify(ok=False, error=msg), 500


# ---- 整盘初始化 / fstab 持久挂载 ----


def _blkid_uuid(device: str) -> str:
    try:
        proc = subprocess.run(
            ["blkid", "-s", "UUID", "-o", "value", device],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return proc.stdout.strip()
    except Exception:
        return ""


def _partition_path(device: str, index: int = 1) -> str:
    if re.search(r"\d$", device):
        return f"{device}p{index}"
    return f"{device}{index}"


def _is_system_device(device: str) -> bool:
    """设备（或其分区）持有系统挂载点则视为系统盘"""
    base = os.path.basename(device)
    try:
        with open("/proc/mounts") as f:
            for line in f:
                src = line.split()[0]
                if src.startswith("/dev/") and os.path.basename(src).startswith(base):
                    return True
    except Exception:
        pass
    return False


@app.post("/api/storage/init_disk")
@admin_required
def api_storage_init_disk():
    data = request.get_json(silent=True) or {}
    device = str(data.get("device", ""))
    fs_type = str(data.get("fs_type", ""))
    mountpoint = str(data.get("mountpoint", "")).strip()
    persist = bool(data.get("persist"))

    if not re.fullmatch(r"/dev/[A-Za-z0-9/_-]+", device or ""):
        return jsonify(ok=False, error="非法设备路径"), 400
    if fs_type not in storage_mgr.ALLOWED_FS:
        return jsonify(ok=False, error=f"仅支持 {', '.join(storage_mgr.ALLOWED_FS)}"), 400
    if _is_system_device(device):
        return jsonify(ok=False, error="系统盘不可初始化"), 400
    dev_info = storage_mgr._find_device(device)
    if not dev_info:
        return jsonify(ok=False, error="设备不存在"), 400
    if dev_info.get("type") != "disk":
        return jsonify(ok=False, error="仅支持整盘初始化"), 400
    for child in dev_info.get("children") or []:
        mounts = [m for m in (child.get("mountpoints") or []) if m]
        if child.get("mountpoint") or mounts:
            return jsonify(ok=False, error="设备分区已挂载，请先卸载"), 400
    if mountpoint and not re.fullmatch(r"/[A-Za-z0-9/_.-]+", mountpoint):
        return jsonify(ok=False, error="非法挂载点"), 400

    steps = []

    def step(cmd: list[str], label: str, timeout: int = 600) -> tuple[bool, str]:
        rc, out, err = storage_mgr._run(cmd, timeout=timeout)
        steps.append(f"{'OK' if rc == 0 else 'FAIL'} {label}")
        return rc == 0, err.strip() or out.strip()

    ok, msg = step(["parted", "-s", device, "mklabel", "gpt"], "创建 GPT 分区表")
    if not ok:
        return jsonify(ok=False, error=msg, steps=steps), 500
    ok, msg = step(
        ["parted", "-s", device, "mkpart", "primary", "0%", "100%"], "创建主分区"
    )
    if not ok:
        return jsonify(ok=False, error=msg, steps=steps), 500
    subprocess.run(["udevadm", "settle"], capture_output=True, timeout=30)
    time.sleep(1)

    part = _partition_path(device)
    if not os.path.exists(part):
        return jsonify(ok=False, error=f"分区设备未出现: {part}", steps=steps), 500
    ok, msg = step(["mkfs." + fs_type, "-F", part], f"格式化 {part} 为 {fs_type}")
    storage_mgr.invalidate_cache()
    if not ok:
        return jsonify(ok=False, error=msg, steps=steps), 500

    if mountpoint:
        os.makedirs(mountpoint, exist_ok=True)
        ok, msg = step(["mount", part, mountpoint], f"挂载到 {mountpoint}", timeout=60)
        storage_mgr.invalidate_cache()
        if not ok:
            return jsonify(ok=False, error=msg, steps=steps), 500
        if persist:
            ok, msg = _fstab_add(part, mountpoint, fs_type)
            steps.append(f"{'OK' if ok else 'FAIL'} 写入 /etc/fstab")
            if not ok:
                return jsonify(ok=False, error=msg, steps=steps), 500

    lsi_alert.log_event(
        "warning",
        f"整盘初始化 {device} → {fs_type}"
        + (f" 挂载 {mountpoint}" if mountpoint else "")
        + f"（{session.get('username', '')}）",
    )
    return jsonify(ok=True, partition=part, steps=steps)


def _fstab_backup() -> str:
    backup = "/etc/fstab.lsi-monitor.bak"
    try:
        with open("/etc/fstab") as src, open(backup, "w") as dst:
            dst.write(src.read())
    except Exception:
        pass
    return backup


def _fstab_add(device: str, mountpoint: str, fstype: str) -> tuple[bool, str]:
    uuid = _blkid_uuid(device)
    src = f"UUID={uuid}" if uuid else device
    try:
        with open("/etc/fstab") as f:
            lines = f.read().splitlines()
    except Exception as e:
        return False, f"读取 /etc/fstab 失败: {e}"
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and not line.strip().startswith("#") and parts[1] == mountpoint:
            return False, f"/etc/fstab 中已存在挂载点 {mountpoint}"
    _fstab_backup()
    try:
        with open("/etc/fstab", "a") as f:
            f.write(f"{src}\t{mountpoint}\t{fstype}\tdefaults\t0\t2\n")
    except Exception as e:
        return False, f"写入 /etc/fstab 失败: {e}"
    return True, "已写入 /etc/fstab"


def _fstab_remove(mountpoint: str) -> tuple[bool, str]:
    try:
        with open("/etc/fstab") as f:
            lines = f.read().splitlines()
    except Exception as e:
        return False, f"读取 /etc/fstab 失败: {e}"
    kept = [
        line
        for line in lines
        if not (
            len(line.split()) >= 2
            and not line.strip().startswith("#")
            and line.split()[1] == mountpoint
        )
    ]
    if len(kept) == len(lines):
        return False, f"/etc/fstab 中未找到挂载点 {mountpoint}"
    _fstab_backup()
    try:
        with open("/etc/fstab", "w") as f:
            f.write("\n".join(kept) + "\n")
    except Exception as e:
        return False, f"写入 /etc/fstab 失败: {e}"
    return True, "已从 /etc/fstab 移除"


@app.post("/api/storage/fstab")
@admin_required
def api_storage_fstab():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", ""))
    mountpoint = str(data.get("mountpoint", "")).strip()
    if not re.fullmatch(r"/[A-Za-z0-9/_.-]+", mountpoint or ""):
        return jsonify(ok=False, error="非法挂载点"), 400
    if action == "add":
        device = str(data.get("device", ""))
        fstype = str(data.get("fstype", ""))
        if not re.fullmatch(r"/dev/[A-Za-z0-9/_-]+", device or ""):
            return jsonify(ok=False, error="非法设备路径"), 400
        ok, msg = _fstab_add(device, mountpoint, fstype)
    elif action == "remove":
        ok, msg = _fstab_remove(mountpoint)
    else:
        return jsonify(ok=False, error="非法操作"), 400
    if ok:
        lsi_alert.log_event(
            "warning", f"fstab {action} {mountpoint}（{session.get('username', '')}）"
        )
        return jsonify(ok=True)
    return jsonify(ok=False, error=msg), 400


if __name__ == "__main__":
    host = os.environ.get("LSI_WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("LSI_WEB_PORT", "5200"))
    app.run(host=host, port=port, debug=False)
