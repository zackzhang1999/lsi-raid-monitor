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
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    Response,
    send_file,
    session,
    redirect,
)

import lsi_alert
import lsi_collectd
import storage_mgr
import bcache_mgr
import bcache_alerts
import user_mgr
import db
import pam_auth
import task_engine
import alert_center
import report_mgr
import license_mgr
import host_mgr
import rbac
import metrics_mgr
import backup_mgr
import config_mgr
import wizard_mgr
import battery_mgr
import report_delivery
import hardware_mgr
import capacity_mgr
import perf_mgr
import ses_mgr
import maint_mgr
import io_diag_mgr
import io_bench_mgr
import ctrl_policy_mgr
import data_backup_mgr

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))

VERSION = "1.10.26"
AUTH_MODE = os.environ.get("LSI_AUTH_MODE", "pam")

LOCAL_STORCLI = PROJECT_ROOT / "storcli64"
STORCLI = os.environ.get(
    "STORCLI_PATH",
    str(LOCAL_STORCLI) if LOCAL_STORCLI.exists() else "/usr/local/bin/storcli64",
)
CONTROLLER = os.environ.get("LSI_CONTROLLER", "/c0")
SMARTCTL = os.environ.get("SMARTCTL_PATH", "/usr/sbin/smartctl")

# ---- 登录防爆破：按来源 IP 在时间窗内限制失败次数 ----
LOGIN_WINDOW_SECONDS = 300
LOGIN_MAX_ATTEMPTS = 10
_login_attempts: defaultdict[str, list] = defaultdict(list)


def _login_block_seconds(ip: str) -> int:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    if len(attempts) < LOGIN_MAX_ATTEMPTS:
        return 0
    oldest = min(attempts)
    return int(LOGIN_WINDOW_SECONDS - (now - oldest)) + 1


def _record_login_failure(ip: str) -> None:
    _login_attempts[ip].append(time.time())


def _clear_login_failures(ip: str) -> None:
    _login_attempts.pop(ip, None)


def _smart_base_device(ctrl_id: int | None = None) -> str:
    """smartctl -d megaraid,N 需要一个透传基设备。

    多控制器时优先使用控制器专属的 /dev/bus/{ctrl}——两个控制器的 DID
    命名空间互不相通，共用 /dev/sda 会读到错误的那张卡。
    该节点不存在时退回 /dev/sda，再退到任意 /dev/sdX
    （部分系统 VD 从 /dev/sdb 开始编号，没有 /dev/sda）。"""
    if ctrl_id is not None and os.path.exists(f"/dev/bus/{ctrl_id}"):
        return f"/dev/bus/{ctrl_id}"
    if os.path.exists("/dev/sda"):
        return "/dev/sda"
    if os.path.exists("/dev/bus/0"):
        return "/dev/bus/0"
    for c in "bcdefghijklmnopqrstuvwxyz":
        if os.path.exists(f"/dev/sd{c}"):
            return f"/dev/sd{c}"
    return "/dev/sda"

COLLECTION_CONFIG_FILE = BASE_DIR / "collection_config.json"
HOTSPARE_CONFIG_FILE = BASE_DIR / "hotspare_config.json"
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
    "copyback_start": "start copyback",  # 后面拼 target=e:s
    "copyback_stop": "stop copyback",
    "erase_stop": "stop erase",
}

# 只有定位灯属于日常运维权限；其余磁盘操作都会改变盘或阵列的角色
OPERATE_DISK_ACTIONS = {"locate_start", "locate_stop"}

# ---- 定位灯状态（storcli 无可靠回读接口，由本服务跟踪 locate_start/stop）----

LOCATE_STATE_FILE = BASE_DIR / ".locate_state.json"


def _load_locate_state() -> dict:
    try:
        if LOCATE_STATE_FILE.exists():
            data = json.loads(LOCATE_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out = {}
                for k, v in data.items():
                    if not v:
                        continue
                    key = str(k)
                    # 旧版只有 "eid:slot" 两段。旧版只采集 /c0，
                    # 因此这些记录归属控制器 0，升级时补上前缀，
                    # 避免已点亮的定位灯在页面上凭空熄灭。
                    if key.count(":") == 1:
                        key = f"0:{key}"
                    out[key] = True
                return out
    except Exception:
        pass
    return {}


def _save_locate_state(state: dict) -> None:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        LOCATE_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def _set_locate(eid: int, slot: int, on: bool, ctrl_id: int = 0) -> None:
    """定位灯键必须含控制器：两张卡的同槽位盘定位灯是两盏不同的灯。"""
    state = _load_locate_state()
    key = f"{ctrl_id}:{eid}:{slot}"
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
    SESSION_COOKIE_SECURE=os.environ.get("LSI_COOKIE_SECURE", "0") == "1",
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


def _refresh_after_action() -> None:
    """磁盘/RAID 操作成功后同步触发一轮快速采集（--quick 跳过 SMART），
    让 /api/status 立即反映最新状态，页面无需等待下一轮定时采集。"""
    try:
        subprocess.run(
            [sys.executable, str(COLLECTD), "--quick"],
            capture_output=True,
            timeout=60,
        )
    except Exception:
        pass
    _status_cache.update(ts=0.0)


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


def _start_task_worker() -> None:
    """启动异步任务 worker。任务必须串行执行：同一张控制器上并行下发
    两个 storcli 写命令会互相干扰，这是硬件层面的约束。"""
    task_engine.start_worker()


def _start_host_poller() -> None:
    """启动多机采集线程。传入本机摘要的取数函数，避免模块间循环依赖。"""

    def _local():
        return agent_summary(build_status())

    host_mgr.start_poller(_local)


def _bcache_alert_loop() -> None:
    while True:
        try:
            if os.environ.get("LSI_DISABLE_BCACHE_ALERT") != "1":
                bcache_alerts.evaluate()
        except Exception:
            pass
        time.sleep(60)


def _start_bcache_alert_loop() -> None:
    t = threading.Thread(target=_bcache_alert_loop, daemon=True, name="bcache-alert")
    t.start()


def _sample_bcache_trend() -> None:
    """每分钟把各 bcache 设备的 hour/day 两档 cache_hit_ratio 写入 SQLite。"""
    try:
        info = bcache_mgr.status()
        devices = info.get("devices") or []
    except Exception:
        return
    if not devices:
        return

    def ratio_of(period_stats, period):
        try:
            return int((period_stats.get(period) or {}).get("cache_hit_ratio", -1))
        except (TypeError, ValueError):
            return -1

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for d in devices:
        name = d.get("name", "")
        if not name:
            continue
        try:
            stats = bcache_mgr.device_stats(name)
        except Exception:
            continue
        period_stats = stats.get("stats") or {}
        hour_ratio = ratio_of(period_stats, "hour")
        day_ratio = ratio_of(period_stats, "day")
        if hour_ratio < 0 or day_ratio < 0:
            continue
        rows.append({
            "timestamp": ts,
            "device": name,
            "hour_hit_ratio": hour_ratio,
            "day_hit_ratio": day_ratio,
        })
    if rows:
        try:
            db.insert_rows("bcache_trend", rows, src="live")
        except Exception:
            pass


def _bcache_trend_loop() -> None:
    while True:
        try:
            if os.environ.get("LSI_DISABLE_BCACHE_TREND") != "1":
                _sample_bcache_trend()
        except Exception:
            pass
        time.sleep(60)


def _start_bcache_trend_loop() -> None:
    t = threading.Thread(target=_bcache_trend_loop, daemon=True, name="bcache-trend")
    t.start()


def _report_delivery_loop() -> None:
    """每分钟检查一次是否有到点的报表要发。

    判断"到点"的是 report_delivery.due_periods()，它按窗口键去重，
    所以这里的轮询只是触发信号，重复触发不会重复发送。
    """
    while True:
        try:
            if os.environ.get("LSI_DISABLE_REPORT_DELIVERY") != "1":
                report_delivery.run_due()
        except Exception as exc:
            print(f"[report_delivery] {exc}", file=sys.stderr)
        time.sleep(60)


def _start_report_delivery_loop() -> None:
    t = threading.Thread(
        target=_report_delivery_loop, daemon=True, name="report-delivery"
    )
    t.start()


def _analysis_loop() -> None:
    """周期性把"分析型"结论送进告警中心：硬件环境、性能、容量。

    这三件事的采样成本差很多——读一次 BMC 要 2~3 秒，而容量预测要扫
    十几天的样本——所以用不同的周期跑在同一个线程里，而不是各开一个
    线程各自 sleep，那样在服务重启后会同时开火、抢 CPU。
    """
    tick = 0
    while True:
        try:
            if os.environ.get("LSI_DISABLE_ANALYSIS") != "1":
                if tick % 5 == 0:  # 每 5 分钟
                    try:
                        hardware_mgr.check_alerts()
                    except Exception as exc:
                        print(f"[hardware] {exc}", file=sys.stderr)
                    try:
                        perf_mgr.check_alerts()
                    except Exception as exc:
                        print(f"[perf] {exc}", file=sys.stderr)
                if tick % 60 == 0:  # 每 60 分钟
                    try:
                        capacity_mgr.check_alerts()
                    except Exception as exc:
                        print(f"[capacity] {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"[analysis] {exc}", file=sys.stderr)
        tick += 1
        time.sleep(60)


def _data_backup_loop() -> None:
    tick = 0
    while True:
        try:
            if os.environ.get("LSI_DISABLE_DATA_BACKUP") != "1":
                data_backup_mgr.run_due()
                # 每 24 小时检查一次是否需要做恢复演练（内部按 7 天判定）
                if tick % 1440 == 0:
                    data_backup_mgr.run_drill(7)
        except Exception as exc:
            print(f"[data_backup] {exc}", file=sys.stderr)
        tick += 1
        time.sleep(60)


def _start_data_backup_loop() -> None:
    t = threading.Thread(target=_data_backup_loop, daemon=True, name="data-backup-sched")
    t.start()


def _start_analysis_loop() -> None:
    t = threading.Thread(target=_analysis_loop, daemon=True, name="analysis")
    t.start()


def _maintenance_loop() -> None:
    """每分钟检查一次维护计划是否到点。"""
    while True:
        try:
            if os.environ.get("LSI_DISABLE_MAINTENANCE") != "1":
                maint_mgr.run_due()
        except Exception as exc:
            print(f"[maintenance] {exc}", file=sys.stderr)
        time.sleep(60)


def _start_maintenance_loop() -> None:
    t = threading.Thread(target=_maintenance_loop, daemon=True, name="maintenance")
    t.start()


# 启动时先把历史 CSV 幂等迁移进 SQLite，再按配置执行保留清理
try:
    db.migrate_from_csv()
except Exception as exc:  # SQLite 异常不应阻断 Web 服务启动
    print(f"[db] init error: {exc}", file=sys.stderr)

# 暂存的恢复要在其它一切之前应用：先把数据换好，后面的迁移与清理
# 才是作用于恢复后的数据，否则会拿新策略去清理旧数据。
try:
    _restore_result = backup_mgr.apply_pending_restore()
    if _restore_result:
        print(f"[restore] {_restore_result}", file=sys.stderr)
        lsi_alert.log_event(
            "warning",
            f"已应用暂存的数据恢复：{_restore_result['restore'].get('file')}"
            f"（自动备份 {_restore_result.get('auto_backup') or '无'}）",
        )
except Exception as exc:
    print(f"[restore] error: {exc}", file=sys.stderr)

try:
    _ret = backup_mgr.apply_retention()
    if _ret.get("removed_samples") or _ret.get("removed_backups"):
        print(f"[retention] {_ret}", file=sys.stderr)
except Exception as exc:
    print(f"[retention] error: {exc}", file=sys.stderr)


_start_embedded_collector()
_start_bcache_alert_loop()
_start_bcache_trend_loop()
_start_report_delivery_loop()
_start_analysis_loop()
_start_data_backup_loop()
_start_maintenance_loop()
_start_task_worker()
_start_host_poller()


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if getattr(resp, "mimetype", None) == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---- 鉴权 ----


def auth_required() -> bool:
    if AUTH_MODE == "pam":
        return True
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
        if not rbac.has_perm(current_role(), "configure"):
            return jsonify(ok=False, error="需要管理员权限"), 403
        return f(*args, **kwargs)

    return wrapper


def perm_required(perm: str):
    """按权限矩阵校验，而不是按角色名硬编码。

    用权限而不是角色做判断，好处是将来调整某个角色的能力时只改 rbac.py
    一处，不需要在几十个路由里找 "!= admin"。
    """

    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if auth_required() and not session.get("username"):
                return jsonify(ok=False, error="未登录"), 401
            role = current_role()
            if not rbac.has_perm(role, perm):
                return (
                    jsonify(
                        ok=False,
                        error=f"当前角色（{rbac.role_label(role)}）没有「{rbac.PERM_LABELS.get(perm, perm)}」权限",
                        perm=perm,
                        role=rbac.normalize(role),
                    ),
                    403,
                )
            return fn(*args, **kwargs)

        return wrapper

    return deco


def requires_feature(name: str):
    """功能门禁。未授权时返回 402 并说明需要哪个版本。

    必须和其它装饰器定义在一起：这个函数会在模块导入期被用作装饰器，
    放到文件后半段的话，前面用了它的路由会在 import 时直接 NameError。
    """

    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if license_mgr.feature_enabled(name):
                return fn(*args, **kwargs)
            meta = license_mgr.FEATURES.get(name) or (name, "pro")
            need = license_mgr.EDITIONS.get(meta[1], {}).get("name", meta[1])
            cur = license_mgr.current()
            return (
                jsonify(
                    ok=False,
                    error=f"「{meta[0]}」需要{need}授权",
                    feature=name,
                    requires=meta[1],
                    edition=cur.get("edition"),
                    trial=cur.get("trial"),
                ),
                402,
            )

        return wrapper

    return deco


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


def _linear_slope(points: list[tuple[float, float]]) -> float:
    """对 (x, y) 序列做最小二乘线性拟合，返回斜率；不足两点返回 0。"""
    n = len(points)
    if n < 2:
        return 0.0
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denom = sum((x - mean_x) ** 2 for x, _ in points)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denom


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

    # 趋势：用每日 SMART 快照做线性斜率，关键计数器持续增长即升级
    if len(hist_rows) >= 3:
        span_days = len(hist_rows) - 1
        for field, name, lv in (
            ("pending", "待定扇区", "crit"),
            ("reallocated", "重映射扇区", "warn"),
            ("uncorrectable", "无法纠正错误", "warn"),
        ):
            pts = [
                (i, float(_to_int(hist_rows[i][1].get(field), 0)))
                for i in range(len(hist_rows))
            ]
            slope = _linear_slope(pts)
            if slope > 0:
                bump(lv, f"{name}近 {span_days} 天呈上升趋势，平均每天 +{slope:.2f}")

    return {"level": level, "reasons": reasons}


def _disk_life(d: dict) -> dict:
    """基于通电时长与 SMART 磨损的寿命估算，结果用于展示，不替代预测结论。"""
    poh = _to_int(d.get("power_on_hours"), 0)
    try:
        design_hours = int(os.environ.get("LSI_DISK_LIFE_HOURS", "43800"))
    except ValueError:
        design_hours = 43800
    design_hours = max(1, design_hours)
    used = min(100, round(poh / design_hours * 100)) if poh > 0 else 0
    remaining = max(0, 100 - used)
    level = (d.get("prediction") or {}).get("level", "ok")
    stage = "normal"
    if level == "crit" or remaining <= 0:
        stage = "critical"
    elif level == "warn" or used >= 80:
        stage = "aged"
    elif used >= 60:
        stage = "watch"
    return {
        "power_on_hours": poh,
        "design_life_hours": design_hours,
        "used_percent": used,
        "remaining_percent": remaining,
        "stage": stage,
    }


def _is_hotspare(state: str) -> bool:
    s = str(state or "").strip().upper()
    return s in ("GHS", "DHS", "UGHS", "UGOOD+HS") or s.endswith("HS")


def _consistency_report(controllers: list[dict]) -> dict:
    """跨控制器一致性检查：固件版本、型号是否统一。

    多卡机器上固件不一致会显著增加故障排查与厂商支持的难度，
    是商用存储体检报告里应该有的一项；单一控制器时无意义，直接通过。
    """
    if len(controllers) < 2:
        return {"ok": True, "issues": []}

    ordered = sorted(controllers, key=lambda c: _to_int(c.get("ctrl"), 0) or 0)
    issues = []

    def _field_values(field: str) -> dict:
        out = {}
        for c in ordered:
            v = str(c.get(field) or "").strip()
            if v:
                out[_to_int(c.get("ctrl"), 0) or 0] = v
        return out

    fws = _field_values("fw")
    if len(set(fws.values())) > 1:
        issues.append(
            {
                "level": "warn",
                "key": "fw_mismatch",
                "title": "控制器固件版本不一致",
                "detail": "、".join(f"C{k} = {v}" for k, v in sorted(fws.items())),
                "hint": "固件不一致会让故障排查与厂商支持变复杂，建议统一到同一版本。",
            }
        )

    models = _field_values("model")
    if len(set(models.values())) > 1:
        issues.append(
            {
                "level": "info",
                "key": "model_mixed",
                "title": "控制器型号不一致",
                "detail": "、".join(f"C{k} = {v}" for k, v in sorted(models.items())),
                "hint": "混合型号在扩容与故障替换时需要额外确认兼容性。",
            }
        )

    return {"ok": not issues, "issues": issues}


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

    # 快照必须只反映**一轮采集**。
    #
    # disks.csv / controller.csv 是每轮追加的，同一天里可能混着换卡前后的
    # 两组记录（旧卡 ctrl 1 + 新卡 ctrl 0）。不按轮次过滤的话，换硬件之后
    # 页面会把历史轮次里的设备一起报出来——显示 2 张卡 8 块盘，而机器上
    # 只有 1 张卡 4 块盘。监控系统显示不存在的设备，比不显示更危险。
    def _latest_round(rows: list[dict], ts_key: str = "timestamp") -> list[dict]:
        if not rows:
            return []
        stamps = [str(r.get(ts_key) or "") for r in rows]
        latest = max(stamps)
        if not latest:
            return rows
        return [r for r in rows if str(r.get(ts_key) or "") == latest]

    disks_rows = _latest_round(disks_rows)
    ctrl_rows = _latest_round(ctrl_rows)

    # 每盘取最新一行。键必须含控制器编号：两张 RAID 卡的槽位号会重复
    # （例如两张卡都有 E252:S0），只用 (eid, slot) 会让后一张卡的盘
    # 覆盖前一张，多卡机器上表现为"少了一半的盘"。
    latest_disks: dict[tuple, dict] = {}
    for row in disks_rows:
        if row.get("state") == "N/A":
            continue
        key = (
            _to_int(row.get("ctrl"), 0),
            _to_int(row.get("eid"), 0),
            _to_int(row.get("slot"), 0),
        )
        latest_disks[key] = row

    attrs = {
        (
            _to_int(a.get("ctrl"), 0),
            _to_int(a.get("eid"), 0),
            _to_int(a.get("slot"), 0),
        ): a
        for a in attr_rows
    }
    smart_latest: dict[str, dict] = {}
    for s in smart_rows:
        smart_latest[s.get("did", "")] = s

    alert_cfg = lsi_alert.load_config()
    temp_warn = int(alert_cfg.get("temp_warn", 45))
    temp_crit = int(alert_cfg.get("temp_crit", 55))
    locate_state = _load_locate_state()
    smart_hist = _smart_history()

    physical_disks = []
    for (ctrl_id, eid, slot), row in sorted(latest_disks.items()):
        attr = attrs.get((ctrl_id, eid, slot), {})
        smart = smart_latest.get(row.get("did", ""), {})
        temp = _to_int(row.get("temperature"))
        physical_disks.append(
            {
                "ctrl": ctrl_id,
                "label": f"E{eid}:S{slot}",
                "full_label": f"C{ctrl_id}:E{eid}:S{slot}",
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
                "rebuild_progress": _to_int(row.get("rebuild_progress")),
                "rebuild_eta": row.get("rebuild_eta", ""),
                "copyback_progress": _to_int(row.get("copyback_progress")),
                "copyback_eta": row.get("copyback_eta", ""),
                "erase_progress": _to_int(row.get("erase_progress")),
                "erase_eta": row.get("erase_eta", ""),
                "locate": f"{ctrl_id}:{eid}:{slot}" in locate_state,
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

    # vds.csv 每轮采集都追加快照，这里只取最新一轮（避免同一天新旧快照混排）
    latest_vd_ts = ""
    for row in vd_rows[::-1]:
        ts = str(row.get("timestamp", ""))
        if ts:
            latest_vd_ts = ts
            break
    if latest_vd_ts:
        vd_rows = [r for r in vd_rows if str(r.get("timestamp", "")) == latest_vd_ts]
    virtual_disks = [
        {
            "ctrl": _to_int(v.get("ctrl"), 0),
            "dg_vd": v.get("dg_vd", ""),
            "type": v.get("type", ""),
            "state": v.get("state", ""),
            "size": v.get("size", ""),
            "name": v.get("name", ""),
            "cache": v.get("cache", ""),
        }
        for v in vd_rows
    ]

    # 控制器视图：每张卡一份，供前端并列展示。多卡机器上单控制器视图
    # 只能看到一张卡，另一张卡的磁盘与阵列会整体从页面上消失。
    latest_ctrls: dict[int, dict] = {}
    for row in ctrl_rows:
        latest_ctrls[_to_int(row.get("ctrl"), 0)] = row
    if not latest_ctrls:
        latest_ctrls = {0: {}}

    def _ctrl_view(ctrl_id: int, row: dict) -> dict:
        disks_of_ctrl = [d for d in physical_disks if d["ctrl"] == ctrl_id]
        vds_of_ctrl = [v for v in virtual_disks if v["ctrl"] == ctrl_id]
        return {
            "ctrl": ctrl_id,
            "index": ctrl_id,
            "model": row.get("model", ""),
            "fw": row.get("fw_version", ""),
            "health": row.get("health", ""),
            "num_disks": _to_int(row.get("num_disks"), len(disks_of_ctrl)),
            "num_vds": _to_int(row.get("num_vds"), len(vds_of_ctrl)),
            "roc_temp": _to_int(row.get("roc_temp")),
            "bbu_model": row.get("bbu_model", ""),
            "bbu_state": row.get("bbu_state", ""),
            "bbu_temperature": _to_int(row.get("bbu_temperature")),
            "bbu_present": row.get("bbu_present", ""),
            "cachevault_flash": row.get("cachevault_flash", ""),
            "onboard_memory_mb": _to_int(row.get("onboard_memory_mb"), 0),
            "foreign_present": _to_int(row.get("foreign_present"), 0) == 1,
            "foreign_count": _to_int(row.get("foreign_count"), 0),
            "foreign_desc": row.get("foreign_desc", ""),
        }

    controllers = [_ctrl_view(cid, latest_ctrls[cid]) for cid in sorted(latest_ctrls)]
    # 兼容字段：单控制器入口取"盘最多的那张卡"，避免多卡机器上老前端
    # 默认指向一张空卡。
    controller = (
        max(controllers, key=lambda c: (c["num_disks"], -c["ctrl"]))
        if controllers
        else {}
    )

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

    # 巡读 / CC 每张卡各一行，逐卡取最新
    pr_by_ctrl: dict[int, dict] = {}
    for row in patrol_rows:
        pr_by_ctrl[_to_int(row.get("ctrl"), 0)] = row
    cc_by_ctrl: dict[int, dict] = {}
    for row in cc_rows:
        cc_by_ctrl[_to_int(row.get("ctrl"), 0)] = row

    def _maint_view(ctrl_id: int) -> dict:
        pr = pr_by_ctrl.get(ctrl_id, {})
        cc = cc_by_ctrl.get(ctrl_id, {})
        return {
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

    maintenance = _maint_view(controller.get("ctrl", 0))
    for c in controllers:
        c["maintenance"] = _maint_view(c["ctrl"])

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
    # 任意一张卡异常即判为异常：多卡机器上一张卡正常不能掩盖另一张
    for c in controllers:
        ch = str(c.get("health", "")).strip()
        if ch and ch not in ("Optimal", "Opt", "N/A"):
            health = "crit"
            break
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
    if not date_dir:
        health = "unknown"
    elif controllers and all(
        str(c.get("health", "")).strip() in ("", "N/A") for c in controllers
    ):
        health = "unknown"

    timestamp = ctrl_row.get("timestamp") or (
        disks_rows[-1].get("timestamp") if disks_rows else ""
    )

    status = {
        "host": socket.gethostname(),
        "timestamp": timestamp,
        "health": health,
        "consistency": _consistency_report(controllers),
        "controllers": controllers,
        # 缓存保护评估：写缓存是否真的生效、掉电保护是否有效。
        # 这是存储里最容易被忽略的数据安全项，放在状态接口里让所有界面都能用。
        "cache_protection": [
            battery_mgr.evaluate(
                c,
                [
                    {
                        "ctrl": v.get("ctrl"),
                        "dg_vd": v.get("dg_vd"),
                        "state": v.get("state"),
                        "write_cache_raw": v.get("cache"),
                        "write_cache_initial": (c.get("maintenance") or {}).get("vd_cache_initial", "")
                        or v.get("write_cache_initial", ""),
                    }
                    for v in virtual_disks
                    if _to_int(v.get("ctrl"), 0) == _to_int(c.get("ctrl"), 0)
                ],
            )
            for c in controllers
        ],
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
    return render_template("index.html", version=VERSION)


# ---- 健康检查（供负载均衡 / 监控系统 / systemd 探活）----


# ---- Prometheus 指标 ----


# ---- 数据备份 / 恢复 / 保留策略 ----


# ---- 控制器配置快照 ----


# ---- 换卡恢复向导 ----
#
# 诊断需要读控制器原始配置与外来配置，每次请求约 1-2 秒。加一层短缓存：
# 向导是显式触发的低频操作，但用户可能连点几次，没必要每次都问 storcli。

_recovery_cache: dict = {"ts": 0.0, "data": None, "key": ""}


@app.get("/api/recovery/diagnose")
@login_required
def api_recovery_diagnose():
    st = build_status()
    key = "|".join(
        str(c.get("ctrl")) + ":" + str(c.get("num_disks")) + ":" + str(c.get("num_vds"))
        for c in (st.get("controllers") or [])
    )
    now = time.time()
    if _recovery_cache["data"] is not None and _recovery_cache["key"] == key and now - _recovery_cache["ts"] < 20:
        return jsonify(diagnoses=_recovery_cache["data"], controllers=[c.get("ctrl") for c in st.get("controllers") or []])

    out = []
    for c in st.get("controllers") or []:
        cid = _to_int(c.get("ctrl"))
        if cid is None:
            continue
        curr = None
        fc = None
        try:
            raw = lsi_collectd.collect_controller_raw(cid)
            if raw:
                curr = config_mgr.normalize(raw)
        except Exception:
            pass
        try:
            fc = lsi_collectd.collect_foreign_config(cid)
        except Exception:
            fc = None
        try:
            repl = config_mgr.card_replacement(cid)
        except Exception:
            repl = None
        out.append(wizard_mgr.diagnose(cid, st, fc, curr=curr, replacement=repl))

    _recovery_cache.update(ts=now, data=out, key=key)
    return jsonify(
        diagnoses=out,
        controllers=[c.get("ctrl") for c in (st.get("controllers") or [])],
    )


def _config_snapshot_now(ctrl_id: int, operator: str, note: str = "") -> dict:
    raw = lsi_collectd.collect_controller_raw(ctrl_id)
    if not raw:
        raise RuntimeError("读取控制器配置失败")
    cfg = config_mgr.normalize(raw)
    return config_mgr.save_snapshot(ctrl_id, cfg, operator, note, "manual")


@app.get("/api/config/snapshots")
@login_required
def api_config_snapshots():
    ctrl_id = _to_int(request.args.get("ctrl"))
    if ctrl_id is None:
        c = build_status().get("controller") or {}
        ctrl_id = _to_int(c.get("ctrl")) or 0
    latest = config_mgr.latest_snapshot(ctrl_id)
    return jsonify(
        ctrl=ctrl_id,
        latest={"id": latest["id"], "ts": latest["ts"], "hash": latest["hash"],
                "reason": latest.get("reason"), "note": latest.get("note")}
        if latest
        else None,
        history=config_mgr.history(ctrl_id),
    )


@app.get("/api/config/snapshots/<int:snap_id>")
@login_required
def api_config_snapshot_detail(snap_id: int):
    s = config_mgr.get_snapshot(snap_id)
    if not s:
        return jsonify(ok=False, error="快照不存在"), 404
    return jsonify(snapshot=s)


@app.get("/api/config/diff")
@login_required
def api_config_diff():
    """比较两份快照。默认与上一份比较，也可指定两个 id。"""
    a_id = _to_int(request.args.get("a"))
    b_id = _to_int(request.args.get("b"))
    if b_id is None:
        ctrl_id = _to_int(request.args.get("ctrl")) or 0
        hist = config_mgr.history(ctrl_id, 2)
        if len(hist) < 2:
            return jsonify(ok=True, changes=[], note="快照不足两份，无法比对")
        a_id, b_id = hist[1]["id"], hist[0]["id"]
    a = config_mgr.get_snapshot(a_id or 0)
    b = config_mgr.get_snapshot(b_id or 0)
    if not a or not b:
        return jsonify(ok=False, error="快照不存在"), 404
    return jsonify(
        ok=True,
        a={"id": a["id"], "ts": a["ts"], "hash": a["hash"]},
        b={"id": b["id"], "ts": b["ts"], "hash": b["hash"]},
        changes=config_mgr.diff(a.get("data") or {}, b.get("data") or {}),
    )


@app.post("/api/config/snapshots")
@perm_required("operate")
def api_config_snapshot_take():
    """手动打一份配置快照。变更前后各打一份，是运维的基本习惯。"""
    data = request.get_json(silent=True) or {}
    ctrl_id = _to_int(data.get("ctrl"))
    if ctrl_id is None:
        c = build_status().get("controller") or {}
        ctrl_id = _to_int(c.get("ctrl")) or 0
    try:
        snap = _config_snapshot_now(ctrl_id, session.get("username", ""), str(data.get("note") or "")[:200])
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    lsi_alert.log_event(
        "info", f"手动记录 C{ctrl_id} 配置快照 #{snap['id']}（{session.get('username', '')}）"
    )
    return jsonify(ok=True, snapshot=snap)
#
# 备份是任何角色都应该能做的（出故障时谁都想先留一份），
# 但恢复是破坏性操作，必须 admin，并且走二次确认 + 自动备份。


@app.get("/api/backups")
@login_required
@perm_required("operate")
def api_backups():
    cfg = backup_mgr.load_config()
    return jsonify(
        backups=backup_mgr.list_backups(),
        config=cfg,
        usage=backup_mgr.data_usage(),
        preview=backup_mgr.preview_prune(cfg.get("retention_days") or 0),
        pending=backup_mgr.pending_restore(),
    )


@app.post("/api/backups")
@login_required
@perm_required("operate")
def api_backup_create():
    data = request.get_json(silent=True) or {}
    scope = "full" if str(data.get("scope")) == "full" else "config"
    note = str(data.get("note") or "").strip()[:200]
    try:
        meta = backup_mgr.create_backup(scope, note)
    except Exception as e:
        return jsonify(ok=False, error=f"备份失败: {e}"), 500
    lsi_alert.log_event(
        "info",
        f"创建{'完整' if scope == 'full' else '配置'}备份 {meta['file']}"
        f"（{session.get('username', '')}）",
    )
    return jsonify(ok=True, backup=meta)


@app.get("/api/backups/<path:name>/download")
@login_required
@perm_required("audit")
def api_backup_download(name: str):
    p = backup_mgr.backup_path(name)
    if not p:
        return jsonify(ok=False, error="备份不存在"), 404
    return send_file(str(p), as_attachment=True, download_name=p.name)


@app.post("/api/backups/<path:name>/remove")
@admin_required
def api_backup_remove(name: str):
    if not backup_mgr.delete_backup(name):
        return jsonify(ok=False, error="备份不存在"), 404
    return jsonify(ok=True)


@app.get("/api/backups/<path:name>/verify")
@login_required
@perm_required("operate")
def api_backup_verify(name: str):
    ok, reason, meta = backup_mgr.validate_backup(name)
    return jsonify(ok=ok, error=reason, manifest=meta)


@app.post("/api/backups/<path:name>/restore")
@admin_required
def api_backup_restore(name: str):
    """暂存恢复内容。真正应用发生在下次启动——
    运行中直接覆盖 SQLite 轻则读到半截，重则损坏文件。"""
    data = request.get_json(silent=True) or {}
    if not data.get("acknowledge"):
        return jsonify(ok=False, error="请先确认已知晓恢复会覆盖当前数据"), 400
    if str(data.get("confirm") or "").strip() != "RESTORE":
        return jsonify(ok=False, error='请输入 RESTORE 以确认'), 400
    ok, reason = backup_mgr.stage_restore(name, session.get("username", ""))
    if not ok:
        return jsonify(ok=False, error=reason), 400
    lsi_alert.log_event(
        "warning",
        f"已暂存数据恢复 {name}，将在下次启动时应用（{session.get('username', '')}）",
    )
    return jsonify(ok=True, pending=backup_mgr.pending_restore())


@app.post("/api/backups/restore/cancel")
@admin_required
def api_backup_restore_cancel():
    backup_mgr.cancel_restore()
    lsi_alert.log_event("warning", f"已取消暂存的数据恢复（{session.get('username', '')}）")
    return jsonify(ok=True)


@app.post("/api/backups/config")
@admin_required
def api_backup_config():
    data = request.get_json(silent=True) or {}
    cfg = backup_mgr.save_config(data)
    lsi_alert.log_event(
        "warning",
        f"更新保留策略：样本 {cfg['retention_days']} 天 · 备份保留 {cfg['keep_backups']} 份"
        f"（{session.get('username', '')}）",
    )
    return jsonify(
        ok=True,
        config=cfg,
        preview=backup_mgr.preview_prune(cfg.get("retention_days") or 0),
    )


@app.post("/api/backups/retention/apply")
@admin_required
def api_backup_retention_apply():
    result = backup_mgr.apply_retention()
    lsi_alert.log_event(
        "warning",
        f"执行数据清理：删除样本 {result['removed_samples']} 条、"
        f"备份 {result['removed_backups']} 份（{session.get('username', '')}）",
    )
    return jsonify(ok=True, result=result)


def _metrics_filesystems() -> list[dict]:
    """给指标导出用的实时文件系统用量。

    过滤规则与报表一致：伪文件系统（snap 只读镜像、tmpfs 等）不是存储容量，
    混进去会把真实数据盘的曲线压平。
    """
    out = []
    try:
        for rec in report_mgr._samples("fs", report_mgr.window("day")):
            if not report_mgr._is_real_fs(rec):
                continue
            mp = str(rec.get("mountpoint") or "")
            if not mp:
                continue
            out.append(
                {
                    "mountpoint": mp,
                    "device": str(rec.get("device") or ""),
                    "fstype": str(rec.get("fstype") or ""),
                    "size_bytes": _to_int(rec.get("size_kb")) * 1024,
                    "used_bytes": _to_int(rec.get("used_kb")) * 1024,
                    "use_percent": float(rec.get("use_percent") or 0),
                }
            )
    except Exception:
        pass
    # 同一挂载点只保留最后一条（_samples 已按时间升序）
    latest: dict[str, dict] = {}
    for r in out:
        latest[r["mountpoint"]] = r
    return list(latest.values())


def _metrics_bcache() -> dict | None:
    try:
        return {"cache_sets": bcache_mgr.bcache_devices()}
    except Exception:
        return None


def _metrics_authorized() -> bool:
    """指标接口的鉴权。

    默认复用采集令牌：Prometheus 是机器对机器访问，没有浏览器会话；
    让它和中心节点用同一份只读凭据，客户不用多管一套密钥。
    设 LSI_METRICS_PUBLIC=1 可放开（内网且已由网络层隔离时使用）。
    """
    if os.environ.get("LSI_METRICS_PUBLIC") == "1":
        return True
    tok = request.headers.get("X-Agent-Token") or ""
    if not tok:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    if not tok:
        tok = request.args.get("token") or ""
    return host_mgr.token_matches(tok)


@app.get("/metrics")
@requires_feature("metrics")
def metrics():
    if not _metrics_authorized():
        return Response("unauthorized\n", status=401, mimetype="text/plain")
    body = metrics_mgr.render(
        build_status(),
        alerts=alert_center.counts(),
        tasks=task_engine.stats(),
        filesystems=_metrics_filesystems(),
        bcache=_metrics_bcache(),
    )
    # 用 content_type 而不是 mimetype：Flask 会给 mimetype 再追加一次
    # charset，最终变成 "charset=utf-8; charset=utf-8"，严格的解析器会报错。
    return Response(body, content_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/api/healthz")
def api_healthz():
    marker = BASE_DIR / ".last_collect"
    last_collect = None
    age_seconds = None
    if marker.exists():
        try:
            last_collect = marker.read_text(encoding="utf-8").strip()
            age_seconds = int((datetime.now() - datetime.strptime(last_collect, "%Y-%m-%d %H:%M")).total_seconds())
        except Exception:
            last_collect = None
            age_seconds = None
    interval = _load_collection_config().get("interval_minutes", 1)
    max_age_seconds = (interval * 2 + 1) * 60
    healthy = age_seconds is None or age_seconds <= max_age_seconds
    try:
        usage = shutil.disk_usage(BASE_DIR)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        disk = {"total": None, "used": None, "free": None}
    payload = {
        "ok": healthy,
        "version": VERSION,
        "host": socket.gethostname(),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_collect": last_collect,
        "last_collect_age_seconds": age_seconds,
        "collector": {
            "interval_minutes": interval,
            "max_age_seconds": max_age_seconds,
        },
        "storcli": {
            "path": STORCLI,
            "available": bool(os.path.isfile(STORCLI)),
        },
        "data_dir": str(BASE_DIR),
        "disk": disk,
    }
    return jsonify(payload), 200 if healthy else 503


# ---- 认证 API ----


@app.post("/api/login")
def api_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    ip = request.remote_addr or "unknown"
    block = _login_block_seconds(ip)
    if block > 0:
        return jsonify(ok=False, error=f"尝试次数过多，请 {block} 秒后重试"), 429
    if AUTH_MODE == "pam":
        role = pam_auth.user_role(username) if pam_auth.authenticate(username, password) else None
    else:
        role = user_mgr.verify_password(username, password)
    if not role:
        _record_login_failure(ip)
        lsi_alert.log_event("warning", f"登录失败: {username} ({ip})")
        return jsonify(ok=False, error="用户名或口令错误"), 401
    _clear_login_failures(ip)
    session.clear()
    session["username"] = username
    session["role"] = role
    session.permanent = True
    lsi_alert.log_event("info", f"用户 {username} 登录")
    return jsonify(
        ok=True,
        username=username,
        role=role,
        version=VERSION,
        auth_mode=AUTH_MODE,
        manage_users=AUTH_MODE != "pam",
    )


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    required = auth_required()
    return jsonify(
        version=VERSION,
        auth_mode=AUTH_MODE,
        manage_users=AUTH_MODE != "pam",
        auth_required=required,
        logged_in=bool(session.get("username")) if required else True,
        username=session.get("username", ""),
        role=current_role(),
        role_label=rbac.role_label(current_role()),
        perms=rbac.perms_for(current_role()),
        rbac=rbac.describe(),
    )


# ---- 监控 API ----


@app.get("/api/status")
@login_required
def api_status():
    return jsonify(build_status())


@app.get("/api/disk_ledger")
@login_required
def api_disk_ledger():
    st = build_status()
    rows = []
    for d in st.get("physical_disks", []):
        first_seen, last_seen, count = db.disk_times(
            d.get("eid"), d.get("slot"), d.get("ctrl")
        )
        pred = d.get("prediction") or {}
        level = pred.get("level", "ok")
        advice = {
            "ok": "正常",
            "info": "关注",
            "warn": "建议安排更换",
            "crit": "立即更换",
        }.get(level, "正常")
        rows.append(
            {
                "ctrl": d.get("ctrl"),
                "label": d.get("label"),
                "full_label": d.get("full_label"),
                "eid": d.get("eid"),
                "slot": d.get("slot"),
                "model": d.get("model"),
                "sn": d.get("sn"),
                "fw_rev": d.get("fw_rev"),
                "state": d.get("state"),
                "temperature": d.get("temperature"),
                "power_on_hours": d.get("power_on_hours"),
                "reallocated": d.get("reallocated"),
                "pending": d.get("pending"),
                "uncorrectable": d.get("uncorrectable"),
                "predict_level": level,
                "predict_reasons": pred.get("reasons", []),
                "replace_advice": advice,
                "life": _disk_life(d),
                "first_seen": datetime.fromtimestamp(first_seen).strftime("%Y-%m-%d %H:%M") if first_seen else None,
                "last_seen": datetime.fromtimestamp(last_seen).strftime("%Y-%m-%d %H:%M") if last_seen else None,
                "sample_count": count,
            }
        )
    return jsonify(total=len(rows), disks=rows)


@app.get("/api/report/summary")
@login_required
def api_report_summary():
    st = build_status()
    disks = st.get("physical_disks", [])
    ctrl = st.get("controller", {})

    def wear_score(d):
        return (
            _to_int(d.get("reallocated"), 0) * 3
            + _to_int(d.get("pending"), 0) * 12
            + _to_int(d.get("uncorrectable"), 0) * 6
            + (_to_int(d.get("predictive_failure"), 0) * 50)
        )

    wear_top = sorted(disks, key=wear_score, reverse=True)[:8]
    lifetime_top = sorted(
        disks, key=lambda d: _to_int(d.get("power_on_hours"), 0), reverse=True
    )[:8]

    return jsonify(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        host=socket.gethostname(),
        version=VERSION,
        health=st.get("health"),
        controller={
            "model": ctrl.get("model"),
            "fw": ctrl.get("fw"),
            "health": ctrl.get("health"),
        },
        disks_total=len(disks),
        vds_total=len(st.get("virtual_disks", [])),
        wear_top=[
            {
                "ctrl": d.get("ctrl"),
                "label": d.get("label"),
                "full_label": d.get("full_label"),
                "model": d.get("model"),
                "reallocated": d.get("reallocated"),
                "pending": d.get("pending"),
                "uncorrectable": d.get("uncorrectable"),
                "predict_level": (d.get("prediction") or {}).get("level", "ok"),
                "wear_score": wear_score(d),
            }
            for d in wear_top
        ],
        lifetime_top=[
            {
                "ctrl": d.get("ctrl"),
                "label": d.get("label"),
                "full_label": d.get("full_label"),
                "model": d.get("model"),
                "power_on_hours": d.get("power_on_hours"),
                "power_on_days": round(_to_int(d.get("power_on_hours"), 0) / 24, 1),
                "life": _disk_life(d),
            }
            for d in lifetime_top
        ],
    )


def _load_hotspare_policy() -> dict:
    cfg = {"desired_global": 1, "auto_promote": False}
    try:
        if HOTSPARE_CONFIG_FILE.exists():
            data = json.loads(HOTSPARE_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if "desired_global" in data:
                    cfg["desired_global"] = max(0, min(24, int(data["desired_global"])))
                cfg["auto_promote"] = bool(data.get("auto_promote", False))
    except Exception:
        pass
    return cfg


@app.get("/api/hotspare_policy")
@login_required
def api_hotspare_policy_get():
    cfg = _load_hotspare_policy()
    st = build_status()
    disks = st.get("physical_disks", [])
    ghs = sum(1 for d in disks if str(d.get("state", "")).strip().upper() == "GHS")
    dhs = sum(1 for d in disks if str(d.get("state", "")).strip().upper() == "DHS")
    eligible = [
        {
            "eid": d.get("eid"),
            "slot": d.get("slot"),
            "label": d.get("label"),
            "model": d.get("model"),
        }
        for d in disks
        if str(d.get("state", "")).strip().upper() in ("UGOOD", "JBOD")
    ]
    return jsonify(
        config=cfg,
        global_count=ghs,
        dedicated_count=dhs,
        eligible=eligible,
    )


@app.post("/api/hotspare_policy")
@admin_required
def api_hotspare_policy_save():
    data = request.get_json(silent=True) or {}
    cfg = _load_hotspare_policy()
    if "desired_global" in data:
        try:
            cfg["desired_global"] = max(0, min(24, int(data["desired_global"])))
        except (ValueError, TypeError):
            return jsonify(ok=False, error="desired_global 必须是整数"), 400
    if "auto_promote" in data:
        cfg["auto_promote"] = bool(data["auto_promote"])
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    HOTSPARE_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    lsi_alert.log_event("info", f"热备策略已更新（{session.get('username', 'system')}）")
    return jsonify(ok=True, config=cfg)


@app.get("/api/history")
@login_required
def api_history():
    hours = _to_int(request.args.get("hours"), 24)
    if hours not in (6, 24, 72):
        hours = 24
    since = datetime.now() - timedelta(hours=hours)

    if db.has_data():
        series: dict[str, list] = {}
        for row in db.history("disks", int(since.timestamp())):
            temp = _to_int(row.get("temperature"))
            if temp is None or row.get("state") == "N/A":
                continue
            label = f"E{row.get('eid')}:S{row.get('slot')}"
            series.setdefault(label, []).append([int(row["_ts_epoch"] * 1000), temp])
        return jsonify(
            series=[{"label": label, "points": pts} for label, pts in sorted(series.items())]
        )

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


# ---- 任务中心 API ----


# ---- 告警中心 API ----


# ---- 报表中心 API ----


# ---- 授权 API ----
#
# ---- 多机管理 API ----
#
# 节点侧只提供只读摘要，且不受授权门禁限制：被管节点是否授权是它自己的事，
# 中心连不上它就完全失去监控能力，这个代价不对等。真正收费的是中心侧能力。


def agent_summary(st: dict) -> dict:
    """节点对外暴露的摘要。只带中心列表页需要的信息，
    不返回磁盘明细——那属于节点自己的界面，中心不需要也不该拿。"""
    disks = st.get("physical_disks") or []
    temps = [d.get("temperature") for d in disks if isinstance(d.get("temperature"), int)]
    bad = [
        d.get("full_label") or d.get("label")
        for d in disks
        if str(d.get("state") or "") not in ("Onln", "UGood", "JBOD", "GHS", "DHS")
        or str(d.get("smart_alert") or "") == "Yes"
        or (isinstance(d.get("temperature"), int) and d.get("temperature") >= 55)
    ]
    try:
        a_counts = alert_center.counts()
    except Exception:
        a_counts = {}
    try:
        t_stats = task_engine.stats()
    except Exception:
        t_stats = {}
    return {
        "product": "DataMax-LSI",
        "version": VERSION,
        "host": st.get("host") or "",
        "timestamp": st.get("timestamp") or "",
        "health": st.get("health") or "unknown",
        "cache_protection_severity": _worst_cache_severity(st),
        # 只有未处理告警需要送到中心：已确认与已恢复的留在节点本地即可，
        # 中心列表要的是"现在需要处理什么"，不是全部历史。
        "alerts": [
            {
                "id": a.get("id"),
                "key": a.get("key"),
                "level": a.get("level"),
                "title": a.get("title"),
                "detail": (a.get("detail") or "")[:300],
                "status": a.get("status"),
                "count": a.get("count"),
                "first_seen": a.get("first_seen"),
                "last_seen": a.get("last_seen"),
            }
            for a in alert_center.list_alerts("active", 50)
        ],
        "alerts_active_count": int(alert_center.counts().get("active", 0) or 0),
        "controllers": [
            {
                "ctrl": c.get("ctrl"),
                "model": c.get("model"),
                "fw": c.get("fw"),
                "health": c.get("health"),
                "num_disks": c.get("num_disks"),
                "num_vds": c.get("num_vds"),
                "roc_temp": c.get("roc_temp"),
            }
            for c in (st.get("controllers") or [])
        ],
        "disks_total": len(disks),
        "disks_bad": bad[:12],
        "disks_bad_count": len(bad),
        "max_temp": max(temps) if temps else None,
        "vds_total": len(st.get("virtual_disks") or []),
        "vds_degraded": sum(
            1
            for v in (st.get("virtual_disks") or [])
            if str(v.get("state") or "").lower() not in ("optl", "optimal")
        ),
        "alerts_active": int(a_counts.get("active", 0) or 0),
        "alerts_open": int(a_counts.get("open", 0) or 0),
        "tasks_active": int(t_stats.get("active", 0) or 0),
        "consistency_ok": bool((st.get("consistency") or {}).get("ok", True)),
    }


def _agent_token_ok() -> bool:
    tok = request.headers.get("X-Agent-Token") or request.args.get("token") or ""
    return host_mgr.token_matches(tok)


def _worst_cache_severity(st: dict) -> str:
    order = {"ok": 0, "warn": 1, "crit": 2}
    worst = "ok"
    for cp in st.get("cache_protection") or []:
        s = str(cp.get("severity") or "ok")
        if order.get(s, 0) > order.get(worst, 0):
            worst = s
    return worst


@app.get("/api/agent/summary")
def api_agent_summary():
    """供中心节点拉取的只读摘要。用独立令牌鉴权，不使用会话——
    中心是机器对机器访问，没有浏览器会话可用。"""
    if not _agent_token_ok():
        return jsonify(ok=False, error="采集令牌不正确"), 401
    return jsonify(agent_summary(build_status()))


@app.get("/api/agent/token")
@admin_required
def api_agent_token():
    return jsonify(
        ok=True,
        token=host_mgr.agent_token(),
        host=socket.gethostname(),
        remote_ack=host_mgr.remote_ack_enabled(),
    )


@app.post("/api/agent/remote_ack")
@admin_required
def api_agent_remote_ack_setting():
    """是否允许中心节点代为确认本节点的告警。默认关闭。"""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    host_mgr.set_remote_ack(enabled)
    lsi_alert.log_event(
        "warning",
        f"{'已允许' if enabled else '已禁止'}中心节点代为确认告警（{session.get('username', '')}）",
    )
    return jsonify(ok=True, remote_ack=enabled)


@app.post("/api/agent/alerts/<int:alert_id>/ack")
def api_agent_alert_ack(alert_id: int):
    """供中心节点代为确认告警。

    这是采集令牌唯一的写操作，且必须由本节点显式开启。确认只改告警状态，
    不触碰任何阵列或磁盘配置——把令牌的权限扩到"改数据"就超出了它的设计。
    """
    if not _agent_token_ok():
        return jsonify(ok=False, error="采集令牌不正确"), 401
    if not host_mgr.remote_ack_enabled():
        return jsonify(ok=False, error="本节点未允许中心代为确认告警"), 403
    a = alert_center.acknowledge(alert_id, "center")
    if not a:
        return jsonify(ok=False, error="告警不存在或已恢复"), 404
    lsi_alert.log_event("info", f"告警 #{alert_id} 已由中心节点代为确认")
    return jsonify(ok=True, alert=a)


@app.post("/api/agent/token/rotate")
@admin_required
def api_agent_token_rotate():
    tok = host_mgr.rotate_agent_token()
    lsi_alert.log_event("warning", f"采集令牌已重置（{session.get('username', '')}）")
    return jsonify(ok=True, token=tok)


@app.get("/api/hosts")
@login_required
@requires_feature("multihost")
def api_hosts():
    return jsonify(hosts=host_mgr.list_hosts(), stats=host_mgr.summary_stats(), last=host_mgr.last_poll())


@app.post("/api/hosts")
@admin_required
@requires_feature("multihost")
def api_host_add():
    data = request.get_json(silent=True) or {}
    try:
        h = host_mgr.add_host(
            data.get("name"), data.get("address"), data.get("token"),
            data.get("note"), data.get("group_name", ""),
        )
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    lsi_alert.log_event(
        "warning", f"新增被管主机 {h.get('name')}（{session.get('username', '')}）"
    )
    return jsonify(ok=True, host=h)


@app.post("/api/hosts/<int:host_id>")
@admin_required
@requires_feature("multihost")
def api_host_update(host_id: int):
    data = request.get_json(silent=True) or {}
    h = host_mgr.update_host(
        host_id,
        **{k: data[k] for k in data
           if k in ("name", "address", "token", "note", "enabled", "group_name")},
    )
    if not h:
        return jsonify(ok=False, error="主机不存在"), 404
    return jsonify(ok=True, host=h)


@app.post("/api/hosts/<int:host_id>/remove")
@admin_required
@requires_feature("multihost")
def api_host_remove(host_id: int):
    if not host_mgr.remove_host(host_id):
        return jsonify(ok=False, error="主机不存在，或本机条目不可删除"), 400
    lsi_alert.log_event("warning", f"移除被管主机 #{host_id}（{session.get('username', '')}）")
    return jsonify(ok=True)


@app.post("/api/hosts/refresh")
@login_required
@requires_feature("multihost")
def api_hosts_refresh():
    """立即采集一轮。列表里手动点刷新时不该等下一个轮询周期。"""
    results = host_mgr.poll_all(agent_summary(build_status()))
    return jsonify(ok=True, results=results, hosts=host_mgr.list_hosts())


@app.post("/api/hosts/bulk")
@perm_required("configure")
@requires_feature("multihost")
def api_hosts_bulk():
    """批量启用 / 停用 / 设分组。

    几十台机器逐个点很折磨，而且"停用"本身就是为了批量操作——比如整批
    下线一个机房的节点。单次上限 200 台，避免误把全部节点一起改掉。
    """
    data = request.get_json(silent=True) or {}
    action = str(data.get("action") or "")
    ids = [int(x) for x in (data.get("ids") or [])][:200]
    if not ids:
        return jsonify(ok=False, error="没有选中任何节点"), 400
    if action not in ("enable", "disable", "group"):
        return jsonify(ok=False, error="不支持的动作"), 400
    changed = 0
    for hid in ids:
        try:
            if action == "group":
                host_mgr.update_host(hid, group_name=str(data.get("group_name") or ""))
            else:
                host_mgr.update_host(hid, enabled=(action == "enable"))
            changed += 1
        except Exception:
            continue
    label = {"enable": "启用", "disable": "停用", "group": "修改分组"}[action]
    lsi_alert.log_event(
        "warning", f"批量{label} {changed} 个被管节点（{session.get('username', '')}）"
    )
    return jsonify(ok=True, changed=changed, hosts=host_mgr.list_hosts())


@app.get("/api/hosts/<int:host_id>/history")
@login_required
@requires_feature("multihost")
def api_host_history(host_id: int):
    return jsonify(history=host_mgr.history(host_id))


@app.get("/api/hosts/alerts")
@login_required
@requires_feature("multihost")
def api_hosts_alerts():
    """多机统一告警视图：所有节点的未处理告警汇成一张表。"""
    items = host_mgr.unified_alerts()
    return jsonify(
        alerts=items,
        stats={
            "total": len(items),
            "error": sum(1 for a in items if a.get("level") == "error"),
            "warning": sum(1 for a in items if a.get("level") == "warning"),
            "offline_hosts": sum(1 for a in items if a.get("offline")),
        },
    )


@app.post("/api/hosts/<int:host_id>/alerts/<int:alert_id>/ack")
@login_required
@requires_feature("multihost")
@perm_required("operate")
def api_host_alert_ack(host_id: int, alert_id: int):
    """代节点确认告警。

    需要该节点显式允许（默认禁止）——采集令牌的承诺是只读，
    扩权必须由被管节点自己决定，不能由中心单方面施加。
    """
    h = host_mgr.get_host(host_id)
    if not h:
        return jsonify(ok=False, error="主机不存在"), 404
    if h.get("local"):
        a = alert_center.acknowledge(alert_id, session.get("username", "") or "center")
        if not a:
            return jsonify(ok=False, error="告警不存在或已恢复"), 404
        # 必须先把状态缓存打掉再重采：build_status 有 15 秒缓存，
        # 确认后立刻采集会拿到确认前的旧告警列表，界面看起来"点了没反应"。
        _status_cache.update(ts=0.0)
        host_mgr.poll_all(agent_summary(build_status()))
        return jsonify(ok=True)
    token = host_mgr.get_host_token(host_id)
    if not token:
        return jsonify(ok=False, error="该主机未配置采集令牌"), 400
    ok, reason = host_mgr.remote_ack(h.get("address") or "", token, alert_id)
    if not ok:
        return jsonify(ok=False, error=reason), 502
    lsi_alert.log_event(
        "info", f"代为确认 {h.get('name')} 的告警 #{alert_id}（{session.get('username', '')}）"
    )
    _status_cache.update(ts=0.0)
    host_mgr.poll_all(agent_summary(build_status()))
    return jsonify(ok=True)


@app.get("/api/audit/export")
@login_required
@perm_required("audit")
def api_audit_export():
    """导出审计记录：操作任务、告警生命周期与应用事件。

    审计角色存在的意义就是这份导出——合规审查要能拿到"谁在什么时候
    做了什么"的完整记录，而不是只能在页面上翻。
    """
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["DataMax-LSI 审计导出", socket.gethostname(),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    w.writerow([])

    w.writerow(["运维任务"])
    w.writerow(["ID", "操作", "目标", "状态", "操作者", "开始", "结束",
                "尝试次数", "命令", "结果说明"])
    for t in task_engine.list_tasks(500, "all"):
        w.writerow([
            t.get("id"), t.get("title"), t.get("target"), t.get("status"),
            t.get("operator"), t.get("started_at") or "", t.get("finished_at") or "",
            t.get("attempt"), t.get("command") or "", t.get("message") or "",
        ])
    w.writerow([])

    w.writerow(["告警记录"])
    w.writerow(["ID", "标识", "级别", "标题", "状态", "触发次数",
                "首次出现", "最近出现", "确认人", "确认时间", "恢复时间"])
    for a in alert_center.list_alerts("all", 500):
        w.writerow([
            a.get("id"), a.get("key"), a.get("level"), a.get("title"),
            a.get("status"), a.get("count"), a.get("first_seen"), a.get("last_seen"),
            a.get("acknowledged_by") or "", a.get("acknowledged_at") or "",
            a.get("resolved_at") or "",
        ])
    w.writerow([])

    w.writerow(["应用事件"])
    w.writerow(["时间", "级别", "内容"])
    try:
        lines = lsi_alert.EVENTS_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    for ln in lines[-2000:]:
        try:
            e = json.loads(ln)
            w.writerow([e.get("timestamp"), e.get("level"), e.get("message")])
        except ValueError:
            continue

    data = buf.getvalue().encode("utf-8-sig")
    return send_file(
        __import__("io").BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"datamax_audit_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
    )
#
# 门禁只挡"增值能力"，绝不挡客户自己硬件产生的数据。
# 客户必须永远能看到自己的阵列、磁盘和告警——把客户的生产数据
# 扣在授权后面，既留不住客户，也在故障时把责任揽到自己身上。


@app.get("/license-signer")
def license_signer_page():
    """授权签发台（厂商侧工具）。

    页面本身完全在浏览器里跑，服务端只负责把这份 HTML 发出去——
    签名私钥永远不经过这台服务器，也不会被写进任何日志。
    放在这里只是为了"任意设备打开就能用"；即使客户管理员打开它，
    没有私钥也签不出任何东西。
    """
    if auth_required() and not session.get("username"):
        return redirect("/")
    if not rbac.has_perm(current_role(), "configure"):
        return "需要管理员权限", 403
    path = PROJECT_ROOT / "tools" / "license-signer.html"
    if not path.exists():
        return "签发台文件缺失（tools/license-signer.html）", 404
    return send_file(str(path), mimetype="text/html")


@app.get("/api/license")
@login_required
def api_license_status():
    return jsonify(license=license_mgr.status())


@app.post("/api/license")
@admin_required
def api_license_install():
    """安装离线授权文件。校验失败即拒绝写入。"""
    raw = ""
    if request.files.get("file"):
        raw = request.files["file"].read().decode("utf-8", errors="replace")
    else:
        raw = str((request.get_json(silent=True) or {}).get("license") or "")
    if not raw.strip():
        return jsonify(ok=False, error="请提供授权文件内容"), 400
    ok, reason = license_mgr.install_license(raw)
    if not ok:
        lsi_alert.log_event("warning", f"授权安装被拒绝：{reason}")
        return jsonify(ok=False, error=reason), 400
    lsi_alert.log_event("warning", f"授权已安装（{session.get('username', '')}）")
    return jsonify(ok=True, license=license_mgr.status())


@app.post("/api/license/remove")
@admin_required
def api_license_remove():
    license_mgr.remove_license()
    lsi_alert.log_event("warning", f"授权已移除（{session.get('username', '')}）")
    return jsonify(ok=True, license=license_mgr.status())


@app.get("/api/reports/summary")
@login_required
@requires_feature("reports")
def api_reports_summary():
    period = (request.args.get("period") or "day").strip()
    return jsonify(report=report_mgr.build_report(period, build_status()))


@app.get("/api/reports/schedule")
@login_required
@requires_feature("reports")
def api_reports_schedule_get():
    view = report_delivery.schedule_view()
    alert_cfg = lsi_alert.load_config()
    return jsonify(
        **view,
        alert_email_to=alert_cfg.get("alert_email_to", ""),
        sendmail_path=alert_cfg.get("sendmail_path", ""),
    )


@app.post("/api/reports/schedule")
@perm_required("configure")
@requires_feature("reports")
def api_reports_schedule_save():
    """保存定时投递配置。

    时间用 HH:MM 字符串、星期用 1-7（周一=1），校验放在这里而不是前端，
    因为前端可以被绕过——写坏一条时间配置的后果是"报表永远不发"，
    而且不会有任何报错，属于最难排查的一类问题。
    """
    data = request.get_json(silent=True) or {}
    cfg = report_delivery.load_config()

    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
    if "recipients" in data:
        cfg["recipients"] = str(data["recipients"] or "").strip()

    for period, keys in (
        ("daily", ("enabled", "time")),
        ("weekly", ("enabled", "time", "weekday")),
        ("monthly", ("enabled", "time", "day")),
    ):
        incoming = data.get(period)
        if not isinstance(incoming, dict):
            continue
        spec = dict(cfg.get(period) or {})
        for k in keys:
            if k not in incoming:
                continue
            if k == "enabled":
                spec["enabled"] = bool(incoming[k])
            elif k == "time":
                t = str(incoming[k] or "").strip()
                if not report_delivery.valid_time(t):
                    return jsonify(ok=False, error=f"{period} 的时间格式应为 HH:MM"), 400
                spec["time"] = t
            elif k == "weekday":
                try:
                    wd = int(incoming[k])
                except (TypeError, ValueError):
                    return jsonify(ok=False, error="星期必须是 1-7 的整数"), 400
                if not (1 <= wd <= 7):
                    return jsonify(ok=False, error="星期必须是 1-7 的整数"), 400
                spec["weekday"] = wd
            elif k == "day":
                try:
                    d = int(incoming[k])
                except (TypeError, ValueError):
                    return jsonify(ok=False, error="日期必须是 1-31 的整数"), 400
                if not (1 <= d <= 31):
                    return jsonify(ok=False, error="日期必须是 1-31 的整数"), 400
                spec["day"] = d
        cfg[period] = spec

    saved = report_delivery.save_config(cfg)
    lsi_alert.log_event(
        "info",
        f"报表定时投递配置已更新（{session.get('username', 'system')}）："
        + ("已启用" if saved.get("enabled") else "已停用"),
    )
    return jsonify(ok=True, **report_delivery.schedule_view())


@app.post("/api/reports/send")
@perm_required("configure")
@requires_feature("reports")
def api_reports_send():
    """立即投递一份报表，用于验证收件人与 MTA 是否配通。"""
    data = request.get_json(silent=True) or {}
    period = str(data.get("period") or "day").strip()
    if period not in report_delivery.PERIODS:
        return jsonify(ok=False, error="未知的报表周期"), 400
    user = session.get("username", "system")
    res = report_delivery.deliver(period, trigger=f"手动（{user}）", status=build_status())
    return jsonify(**res, history=report_delivery.load_config().get("history", [])[:20])


@app.get("/api/reports/export")
@login_required
@requires_feature("reports")
def api_reports_export():
    """导出报表。

    HTML 是自包含单文件（样式内联、无外部依赖），可直接打印或另存为 PDF；
    CSV 用 utf-8-sig 编码，Excel 打开中文不会乱码。
    """
    import io

    period = (request.args.get("period") or "day").strip()
    fmt = (request.args.get("format") or "html").strip().lower()
    rep = report_mgr.build_report(period, build_status())
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    label = rep["meta"].get("label") or "report"

    if fmt == "csv":
        data = report_mgr.render_csv(rep).encode("utf-8-sig")
        return send_file(
            io.BytesIO(data),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"datamax_{label}_{stamp}.csv",
        )

    html = report_mgr.render_html(rep).encode("utf-8")
    if request.args.get("download"):
        return send_file(
            io.BytesIO(html),
            mimetype="text/html",
            as_attachment=True,
            download_name=f"datamax_{label}_{stamp}.html",
        )
    # 直接返回 HTML 便于浏览器打印 / 另存为 PDF
    return Response(html, mimetype="text/html")


@app.get("/api/alerts")
@login_required
def api_alerts():
    status = (request.args.get("status") or "open").strip()
    limit = min(max(_to_int(request.args.get("limit"), 100) or 100, 1), 500)
    return jsonify(
        alerts=alert_center.list_alerts(status, limit),
        counts=alert_center.counts(),
        silences=alert_center.active_silences(),
    )


@app.post("/api/alerts/<int:alert_id>/ack")
@login_required
@requires_feature("alert_closure")
@perm_required("operate")
def api_alert_ack(alert_id: int):
    """确认告警：表示"我看到了，正在处理"，不等于问题已经消失。"""
    a = alert_center.acknowledge(alert_id, session.get("username", "") or "unknown")
    if not a:
        return jsonify(ok=False, error="告警不存在或已恢复"), 404
    lsi_alert.log_event("info", f"告警 #{alert_id} 已确认（{session.get('username', '')}）")
    return jsonify(ok=True, alert=a)


@app.post("/api/alerts/ack_all")
@login_required
@requires_feature("alert_closure")
@perm_required("operate")
def api_alerts_ack_all():
    n = alert_center.acknowledge_all(session.get("username", "") or "unknown")
    lsi_alert.log_event("info", f"批量确认 {n} 条告警（{session.get('username', '')}）")
    return jsonify(ok=True, acknowledged=n)


@app.post("/api/alerts/silence")
@perm_required("operate")
@requires_feature("alert_closure")
def api_alert_silence():
    """维护窗口：静默期内照常记录告警，但不发送邮件/Webhook。

    静默的语义是"别打扰人"，不是"假装没发生"——告警照旧出现在页面上，
    窗口结束后也不会补发历史通知。
    """
    data = request.get_json(silent=True) or {}
    scope = str(data.get("scope") or "all").strip() or "all"
    minutes = _to_int(data.get("minutes"), 60) or 60
    reason = str(data.get("reason") or "").strip()
    s = alert_center.add_silence(scope, minutes, reason, session.get("username", ""))
    lsi_alert.log_event(
        "warning",
        f"已开启维护窗口 {scope}（{minutes} 分钟）{reason}（{session.get('username', '')}）",
    )
    return jsonify(ok=True, silence=s)


@app.post("/api/alerts/silence/<int:silence_id>/remove")
@perm_required("operate")
@requires_feature("alert_closure")
def api_alert_silence_remove(silence_id: int):
    ok = alert_center.remove_silence(silence_id)
    if not ok:
        return jsonify(ok=False, error="维护窗口不存在"), 404
    lsi_alert.log_event("info", f"已结束维护窗口 #{silence_id}（{session.get('username', '')}）")
    return jsonify(ok=True)


@app.get("/api/tasks")
@login_required
def api_tasks():
    limit = min(max(_to_int(request.args.get("limit"), 50) or 50, 1), 200)
    status = (request.args.get("status") or "all").strip()
    return jsonify(tasks=task_engine.list_tasks(limit, status), stats=task_engine.stats())


@app.get("/api/tasks/<int:task_id>")
@login_required
def api_task_detail(task_id: int):
    t = task_engine.get_task(task_id)
    if not t:
        return jsonify(ok=False, error="任务不存在"), 404
    return jsonify(task=t)


@app.post("/api/tasks/<int:task_id>/retry")
@admin_required
def api_task_retry(task_id: int):
    """重试失败任务。只有注册时声明为幂等的类型才允许，其余会被拒绝。"""
    try:
        t = task_engine.retry(task_id, session.get("username", ""))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    lsi_alert.log_event(
        "warning",
        f"任务 #{task_id} 重新提交为 #{t.get('id')}（{session.get('username', '')}）",
    )
    return jsonify(ok=True, task=t)


@app.get("/api/alert_config")
@login_required
def api_alert_config_get():
    cfg = lsi_alert.load_config()
    return jsonify(
        enabled=lsi_alert.alert_enabled(cfg),
        sendmail_available=lsi_alert.sendmail_available(cfg),
        webhook_configured=lsi_alert.webhook_enabled(cfg),
        locked=lsi_alert.locked_fields(),
        config={
            "alert_email_to": cfg.get("alert_email_to", ""),
            "sendmail_path": cfg.get("sendmail_path", ""),
            "webhook_url": cfg.get("webhook_url", ""),
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
    if "webhook_url" in data:
        cfg["webhook_url"] = str(data["webhook_url"]).strip()
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


@app.post("/api/webhook_test")
@admin_required
def api_webhook_test():
    ok, msg = lsi_alert.send_webhook(
        "测试报警",
        f"这是一条来自 LSI RAID 监控的测试消息。\n主机: {socket.gethostname()}\n时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
        "info",
    )
    if ok:
        lsi_alert.log_event("info", "测试 Webhook 发送成功")
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


def _resolve_ctrl(payload: dict, default: int | None = None) -> int:
    """解析本次操作作用于哪张 RAID 控制器。

    多卡机器上"打哪张卡"必须显式传递：默认落到 /c0 时，如果目标盘实际
    挂在 /c1，命令会作用在另一张卡上——轻则报 "No drive found"，重则
    对同槽位的另一块盘执行了 offline/erase。所以这里不做静默兜底，
    只在前端确实没传时退到"盘最多的那张卡"。
    """
    cid = _to_int(payload.get("ctrl"))
    if cid is not None and cid >= 0:
        return cid
    if default is not None:
        return default
    try:
        c = build_status().get("controller") or {}
        got = _to_int(c.get("ctrl"))
        if got is not None:
            return got
    except Exception:
        pass
    ids = lsi_collectd.list_controller_ids()
    return ids[0] if ids else 0


def _ctrl_for_disk(eid: int, slot: int, payload: dict) -> int:
    """定位某块盘所在的控制器。

    前端未传 ctrl 时不做"默认 /c0"这种兜底：多卡机器上两张卡可能有
    完全相同的 (eid, slot)，猜错就会对另一块盘执行 offline/erase。
    改为从当前状态反查这块盘真实挂在哪张卡上。
    """
    cid = _to_int(payload.get("ctrl"))
    if cid is not None and cid >= 0:
        return cid
    try:
        for d in build_status().get("physical_disks", []):
            if _to_int(d.get("eid")) == eid and _to_int(d.get("slot")) == slot:
                got = _to_int(d.get("ctrl"))
                if got is not None:
                    return got
    except Exception:
        pass
    return 0


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


# ---- 异步任务的执行体 ----


def _storcli_text_error(text: str) -> str:
    """从 storcli 的文本输出判断命令是否真的失败，失败时返回原因。

    storcli 即使命令失败也以退出码 0 结束——实测 `/c0/v99 stop cc` 返回 0，
    只在输出里写 "Status = Failure" 和明细表中的 Failed 行。仅凭退出码判定
    会把失败记成成功，而任务台账里"失败被记成功"比没有台账更糟：
    它会让人相信一次根本没生效的操作已经完成了。
    """
    if not text:
        return ""

    def _desc() -> str:
        m = re.search(r"^\s*Description\s*=\s*(.+?)\s*$", text, re.M)
        d = m.group(1).strip() if m else ""
        return d if d and d.lower() != "none" else ""

    def _detail_reason() -> str:
        # 明细表里的失败行，形如：
        #   99 -         Failed   255 Invalid VD number
        m = re.search(r"\bFailed\b\s+(\d+)\s+(.+?)\s*$", text, re.M)
        return m.group(2).strip() if m else ""

    if re.search(r"^\s*Status\s*=\s*Failure\s*$", text, re.M | re.I):
        # storcli 的 Description 常是 "None"，真正的原因在明细表里
        return _desc() or _detail_reason() or "storcli 返回失败"

    m = re.search(r"^\s*Status Code\s*=\s*(\d+)", text, re.M | re.I)
    if m and int(m.group(1)) != 0:
        return _desc() or _detail_reason() or f"storcli 状态码 {m.group(1)}"

    detail = _detail_reason()
    if detail:
        return detail

    return ""


#
# 所有存储写操作共用同一个执行器：参数里带着已经拼好的 storcli 参数串，
# 处理器只负责执行、回报进度与结果。参数校验留在 API 层完成——那时看到的
# 是最新状态；任务真正执行时状态可能已经变了，所以校验必须前置，
# 执行阶段只做"重跑一次不会更糟"的动作。


def _register_storcli_task(
    kind: str,
    title: str,
    retryable: bool = False,
    timeout: int = 120,
    refresh: bool = True,
):
    @task_engine.register(kind, title, retryable=retryable)
    def _handler(params, progress):
        ctrl_id = _to_int(params.get("ctrl"), 0) or 0
        # 前置状态转换（如 JBOD → UGood），失败即整体失败
        for spec in params.get("pre_good") or []:
            parts = str(spec).split(":")
            if len(parts) < 2:
                continue
            eid, slot = parts[-2], parts[-1]
            ok, msg = _run_storcli(f"/c{ctrl_id}/e{eid}/s{slot} set good force")
            if not ok:
                return False, f"磁盘 E{eid}:S{slot} 转 UGood 失败: {msg}", msg
            progress(15, f"E{eid}:S{slot} 已转为 UGood")

        args = str(params.get("args") or "").strip()
        if not args:
            return False, "任务参数缺失", ""
        progress(30, "执行中…")
        ok, msg = _run_storcli_text(args, timeout=timeout)
        # storcli 失败时退出码仍为 0，成功与否必须看输出内容
        reason = _storcli_text_error(msg)
        if not ok and not reason:
            tail = [ln for ln in (msg or "").strip().splitlines() if ln.strip()]
            reason = tail[-1] if tail else "执行失败"
        if ok and not reason:
            progress(90, "刷新状态…")
            if refresh:
                _refresh_after_action()
            progress(100, "完成")
            return True, "完成", msg
        reason = reason or "执行失败"
        return False, reason, f"{reason}\n\n{msg}"

    return _handler


_register_storcli_task("raid_create", "创建磁盘阵列", retryable=False, timeout=180)
_register_storcli_task("raid_expand", "动态扩容虚拟磁盘", retryable=False, timeout=300)
_register_storcli_task("disk_erase", "安全擦除磁盘", retryable=False, timeout=180)
_register_storcli_task("vd_delete", "删除虚拟磁盘", retryable=False)
_register_storcli_task("vd_init", "初始化虚拟磁盘", retryable=False, timeout=300)
_register_storcli_task("foreign_import", "导入外来阵列配置", retryable=True)
_register_storcli_task("cc_start", "启动一致性检查", retryable=True)
_register_storcli_task("cc_stop", "停止一致性检查", retryable=True)
_register_storcli_task("patrolread_start", "启动巡读", retryable=True)
_register_storcli_task("patrolread_stop", "停止巡读", retryable=True)
_register_storcli_task("patrolread_pause", "暂停巡读", retryable=True)
_register_storcli_task("patrolread_resume", "恢复巡读", retryable=True)
_register_storcli_task("ctrl_policy_set", "修改控制器策略", retryable=True)
_register_storcli_task("vd_policy_set", "修改虚拟盘策略", retryable=True)
_register_storcli_task("phy_reset", "复位 PHY 错误计数", retryable=True)


@task_engine.register("smart_test", "磁盘 SMART 自检", retryable=True)
def _task_smart_test(params, progress):
    """对控制器下的物理盘发起 SMART 自检。

    盘挂在 MegaRAID 后面时没有自己的 /dev 节点，必须用
    `smartctl -d megaraid,<did> <透传基设备>` 才够得着。发起自检只是
    让盘在后台自己跑，结果要稍后用「磁盘 SMART」查看。
    """
    ctrl_id = _to_int(params.get("ctrl"), 0) or 0
    mode = str(params.get("mode") or "short")
    if mode not in ("short", "long", "conveyance", "offline"):
        return False, f"不支持的自检模式：{mode}", ""

    base = _smart_base_device(ctrl_id)
    disks = [d for d in build_status().get("physical_disks", []) if _to_int(d.get("ctrl"), 0) == ctrl_id]
    if not disks:
        return False, "该控制器下没有可自检的物理盘", ""

    lines, failed = [], []
    for i, d in enumerate(disks):
        did = d.get("did")
        label = d.get("full_label") or d.get("label") or f"did={did}"
        progress(int(i / len(disks) * 90), f"发起自检 {label}")
        if did is None:
            failed.append(label)
            lines.append(f"{label}: 无有效 DID，跳过")
            continue
        try:
            proc = subprocess.run(
                [SMARTCTL, "-t", mode, "-d", f"megaraid,{did}", base],
                capture_output=True,
                text=True,
                timeout=90,
            )
            lines.append(f"$ smartctl -t {mode} -d megaraid,{did} {base}\n{(proc.stdout or '').strip()}\n{(proc.stderr or '').strip()}".strip())
            # smartctl 用位掩码返回：bit2(4) 表示部分 SMART 指令不支持，
            # 但自检本身已经发起，这种情况不该算失败。
            if proc.returncode not in (0, 4):
                failed.append(label)
        except Exception as e:
            failed.append(label)
            lines.append(f"{label}: {e}")

    output = "\n\n".join(lines)
    if failed:
        return False, f"{len(failed)}/{len(disks)} 块盘发起自检失败：{', '.join(failed)}", output
    progress(100, "完成")
    return True, f"已对 {len(disks)} 块盘发起 SMART {mode} 自检", output


def _maint_build(plan: dict) -> dict:
    """把维护计划翻译成 storcli / smartctl 命令与任务参数。

    命令模板留在 web_server，而不是让 maint_mgr 自己拼——RAID_ACTIONS
    是命令的唯一出处，抄一份到别处迟早会与这里不一致。
    """
    action = str(plan.get("action") or "")
    params = plan.get("params") or {}
    ctrl_id = _to_int(params.get("ctrl"), 0) or 0

    if action in ("patrolread_start", "patrolread_stop", "cc_start", "cc_stop"):
        target, verb = action.rsplit("_", 1)
        vd = _to_int(params.get("vd"), 0) or 0
        tmpl = RAID_ACTIONS[target][verb]
        cmd = tmpl.format(ctrl=f"/c{ctrl_id}", vd=vd)
        desc = f"C{ctrl_id} {RAID_ACTION_LABEL.get(target, target)}"
        if target == "cc":
            desc += f" (v{vd})"
        return {
            "kind": action,
            "params": {"ctrl": ctrl_id, "vd": vd, "args": cmd, "target": target, "action": verb},
            "target": desc,
            "command": f"storcli {cmd}",
        }

    if action in ("smart_short", "smart_long"):
        kind = "short" if action == "smart_short" else "long"
        return {
            "kind": "smart_test",
            # 不指定具体设备时对该控制器下的全部物理盘发起：
            # 计划任务没人盯着，逐个挑盘反而容易漏掉新增的盘。
            "params": {"ctrl": ctrl_id, "mode": kind},
            "target": f"C{ctrl_id} 全部物理盘",
            "command": f"smartctl -t {kind} -d megaraid,<did> <base>",
        }

    raise ValueError(f"不支持的维护动作：{action}")


maint_mgr.set_builder(_maint_build)

# 瞬时操作：同步执行后写一条终态记录，让审计时间线完整
task_engine.register_info("disk_action", "磁盘操作")
task_engine.register_info("controller_alarm", "阵列卡蜂鸣器")
task_engine.register_info("controller_jbod", "JBOD 模式")
task_engine.register_info("disk_erase_start", "磁盘擦除")

# RAID 操作 → 任务类型 与 中文名
# 属于日常运维的 RAID 目标：只扫描、不改结构
OPERATE_RAID_TARGETS = {"patrolread", "cc"}

RAID_ACTION_LABEL = {
    "patrolread": "巡读",
    "cc": "一致性检查",
    "vd_init": "虚拟盘初始化",
    "vd_delete": "删除虚拟盘",
    "vd_import": "导入外来配置",
}

TASK_KIND_BY_ACTION = {
    ("patrolread", "start"): "patrolread_start",
    ("patrolread", "stop"): "patrolread_stop",
    ("patrolread", "pause"): "patrolread_pause",
    ("patrolread", "resume"): "patrolread_resume",
    ("cc", "start"): "cc_start",
    ("cc", "stop"): "cc_stop",
    ("vd_init", "start"): "vd_init",
    ("vd_delete", "delete"): "vd_delete",
    ("vd_import", "import"): "foreign_import",
}


@app.post("/api/disk_action")
@perm_required("operate")
def api_disk_action():
    data = request.get_json(silent=True) or {}
    eid = _to_int(data.get("eid"))
    slot = _to_int(data.get("slot"))
    action = str(data.get("action", ""))
    if eid is None or slot is None or action not in DISK_ACTIONS:
        return jsonify(ok=False, error="参数非法"), 400
    # 磁盘操作里只有定位灯属于日常运维：它只点亮指示灯，不改动任何数据。
    # online/offline/good/jbod/热备/回拷都会改变阵列成员或盘的角色，
    # 一次误点就可能触发重建甚至数据不可访问，必须留在变更权限内。
    if action not in OPERATE_DISK_ACTIONS and not rbac.has_perm(current_role(), "configure"):
        return (
            jsonify(
                ok=False,
                error=f"当前角色（{rbac.role_label(current_role())}）没有「变更配置」权限",
                perm="configure",
            ),
            403,
        )
    cmd_suffix = DISK_ACTIONS[action]
    if action == "hotspare_dedicated":
        dg = _to_int(data.get("dg"))
        if dg is None:
            return jsonify(ok=False, error="请指定磁盘组(DG)编号"), 400
        cmd_suffix += f" DGs={dg}"
    if action == "copyback_start":
        teid = _to_int(data.get("target_eid"))
        tslot = _to_int(data.get("target_slot"))
        if teid is None or tslot is None:
            return jsonify(ok=False, error="请指定 copyback 目标盘(e:s)"), 400
        cmd_suffix += f" target={teid}:{tslot}"
    ctrl_id = _ctrl_for_disk(eid, slot, data)
    label = f"C{ctrl_id}:E{eid}:S{slot}"
    ok, msg = _run_storcli(
        f"/c{ctrl_id}/e{eid}/s{slot} {cmd_suffix}"
    )
    if ok:
        if action == "locate_start":
            _set_locate(eid, slot, True, ctrl_id)
        elif action == "locate_stop":
            _set_locate(eid, slot, False, ctrl_id)
        lsi_alert.log_event(
            "warning", f"磁盘操作: {label} {action}（{session.get('username', '')}）"
        )
        task_engine.record_instant(
            "disk_action",
            {"ctrl": ctrl_id, "eid": eid, "slot": slot, "action": action},
            ok=True,
            target=label,
            command=f"storcli /c{ctrl_id}/e{eid}/s{slot} {cmd_suffix}",
            operator=session.get("username", ""),
        )
        _refresh_after_action()
        return jsonify(ok=True)
    lsi_alert.log_event("error", f"磁盘操作失败: {label} {action} — {msg}")
    task_engine.record_instant(
        "disk_action",
        {"ctrl": ctrl_id, "eid": eid, "slot": slot, "action": action},
        ok=False,
        message=msg,
        target=label,
        command=f"storcli /c{ctrl_id}/e{eid}/s{slot} {cmd_suffix}",
        operator=session.get("username", ""),
    )
    return jsonify(ok=False, error=msg), 500


@app.post("/api/disk_erase")
@admin_required
def api_disk_erase():
    """启动磁盘安全擦除（storcli /ex/sx start erase）。

    需要显式输入槽位标识 + 勾选"我已知晓"双重确认，防止误操作；
    erase 为后台任务，进度经采集链路（show erase）展示在磁盘列表。
    """
    data = request.get_json(silent=True) or {}
    eid = _to_int(data.get("eid"))
    slot = _to_int(data.get("slot"))
    confirm_label = str(data.get("confirm", "")).strip()
    acknowledged = bool(data.get("acknowledge"))
    if eid is None or slot is None:
        return jsonify(ok=False, error="参数非法"), 400
    ctrl_id = _ctrl_for_disk(eid, slot, data)
    # 兼容三种输入格式："E134:0"、"E134:S0"，以及带控制器的 "C1:E134:S0"
    if confirm_label not in (
        f"E{eid}:{slot}",
        f"E{eid}:S{slot}",
        f"C{ctrl_id}:E{eid}:S{slot}",
    ):
        return jsonify(ok=False, error=f"请输入正确的盘位标识（如 \"E{eid}:S{slot}\"）"), 400
    if not acknowledged:
        return jsonify(ok=False, error="请勾选确认已知晓数据将被彻底销毁"), 400

    pattern = str(data.get("pattern", "simple")).strip().lower()
    if pattern not in (
        "simple",
        "normal",
        "thorough",
        "standard",
        "threepass",
        "crypto",
    ):
        return jsonify(ok=False, error="擦除模式非法"), 400

    label = f"C{ctrl_id}:E{eid}:S{slot}"
    args = f"/c{ctrl_id}/e{eid}/s{slot} start erase {pattern}"
    # 擦除是不可逆的高危操作：走任务化，留下操作者、命令与返回码。
    # 盘上的擦除进度由采集链路（show erase）继续反映在磁盘列表里。
    task = task_engine.submit(
        "disk_erase",
        {"ctrl": ctrl_id, "eid": eid, "slot": slot, "pattern": pattern, "args": args},
        target=f"{label} · erase {pattern}",
        command=f"storcli {args}",
        operator=session.get("username", ""),
    )
    lsi_alert.log_event(
        "warning",
        f"磁盘擦除: {label} 已提交 erase（{pattern}）（{session.get('username', '')}）",
    )
    return jsonify(ok=True, task=task)


@app.get("/api/foreign_config")
@admin_required
def api_foreign_config():
    """查询控制器上的外来阵列配置（Foreign Configuration）状态。

    外来配置来源：从其它机器/背板迁移过来的盘带有原机阵列元数据。
    storcli 将这些盘显示为 UGood/UBad（部分场景 DG 列显示 F），
    导入（import）后原阵列/VD 会在本控制器上恢复。
    """
    ctrl_id = _resolve_ctrl(request.args.to_dict())
    data = lsi_collectd.run_storcli(f"/c{ctrl_id} /fall show J", timeout=30)
    if not data:
        return jsonify(ok=False, error="storcli 查询失败"), 500
    try:
        cs = data["Controllers"][0].get("Command Status", {})
        desc = str(cs.get("Description", ""))
        present = not ("couldn" in desc.lower() and "foreign" in desc.lower())
        m = re.search(r"(\d+)\s+foreign", desc, re.IGNORECASE)
        count = int(m.group(1)) if (present and m) else (1 if present else 0)
        return jsonify(
            ok=True, ctrl=ctrl_id, present=present, count=count, description=desc
        )
    except Exception as e:
        return jsonify(ok=False, error=f"解析失败: {e}"), 500


@app.post("/api/foreign_import")
@admin_required
def api_foreign_import():
    """导入外来阵列配置（storcli /cX/fall import）。

    将迁移盘上原有的阵列元数据导回控制器，恢复其原 DG/VD 结构。
    导入后相关盘从 UGood/UBad 变为 Onln（或 Dgrd 等），由原配置决定。
    高危操作：若控制器上已有同编号 DG 可能冲突；导入前请确认来源盘合法。
    """
    data = request.get_json(silent=True) or {}
    if not data.get("acknowledge"):
        return jsonify(ok=False, error="请先确认导入风险"), 400
    ctrl_id = _resolve_ctrl(data)
    args = f"/c{ctrl_id} /fall import"
    task = task_engine.submit(
        "foreign_import",
        {"ctrl": ctrl_id, "args": args},
        target=f"C{ctrl_id} 导入外来阵列配置",
        command=f"storcli {args}",
        operator=session.get("username", ""),
    )
    lsi_alert.log_event(
        "warning", f"C{ctrl_id} 外来配置导入已提交（{session.get('username', '')}）"
    )
    return jsonify(ok=True, ctrl=ctrl_id, task=task)


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
    ctrl_id = _to_int(request.args.get("ctrl"))
    if ctrl_id is None:
        ctrl_id = _ctrl_for_disk(eid, slot, {})
    for d in status["physical_disks"]:
        if (
            d["eid"] == eid
            and d["slot"] == slot
            and _to_int(d.get("ctrl"), 0) == ctrl_id
        ):
            found = True
            did = d.get("did")
            break
    if not found:
        return jsonify(error="未找到该磁盘"), 404

    # 先尝试 smartctl 透传（仅对已组阵列、有有效 DID 的盘可靠）
    if did is not None:
        try:
            proc = subprocess.run(
                f"sudo {SMARTCTL} -a -d megaraid,{did} {_smart_base_device(ctrl_id)}",
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
    hex_str = _storcli_smart_hex(ctrl_id, eid, slot)
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


@app.post("/api/nfs/install")
@admin_required
def api_nfs_install():
    ok, msg = storage_mgr.install_nfs()
    lsi_alert.log_event(
        "warning" if ok else "error",
        f"NFS 服务安装：{msg}（{session.get('username', '')}）",
    )
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.get("/api/bcache/status")
@login_required
def api_bcache_status():
    return jsonify(bcache_mgr.status())


@app.post("/api/bcache/prepare")
@admin_required
def api_bcache_prepare():
    ok, msg = bcache_mgr.prepare()
    if ok:
        lsi_alert.log_event("info", f"bcache 环境准备完成（{session.get('username', '')}）")
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.get("/api/bcache/devices")
@login_required
def api_bcache_devices():
    return jsonify(devices=bcache_mgr.block_devices())


@app.post("/api/bcache/create")
@admin_required
def api_bcache_create():
    data = request.get_json(silent=True) or {}
    cache_path = str(data.get("cache_path", "")).strip()
    backing_path = str(data.get("backing_path", "")).strip()
    if not bool(data.get("acknowledge")):
        return jsonify(ok=False, error="请勾选确认两块设备可被清空"), 400
    ok, msg, dev = bcache_mgr.create_bcache(cache_path, backing_path)
    if ok:
        lsi_alert.log_event("warning", f"bcache 创建：缓存={cache_path} backing={backing_path} → {dev}（{session.get('username', '')}）")
    return jsonify(ok=ok, message=msg, device=dev) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/mode")
@admin_required
def api_bcache_mode():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    mode = str(data.get("mode", "")).strip()
    if not re.fullmatch(r"bcache\d+", name):
        return jsonify(ok=False, error="非法设备名"), 400
    ok, msg = bcache_mgr.set_mode(name, mode)
    lsi_alert.log_event("info" if ok else "error", f"bcache {name} 模式切换 {mode}: {msg}")
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/detach")
@admin_required
def api_bcache_detach():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not re.fullmatch(r"bcache\d+", name):
        return jsonify(ok=False, error="非法设备名"), 400
    ok, msg = bcache_mgr.detach(name)
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/stop")
@admin_required
def api_bcache_stop():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not re.fullmatch(r"bcache\d+", name):
        return jsonify(ok=False, error="非法设备名"), 400
    ok, msg = bcache_mgr.stop(name)
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/mount")
@admin_required
def api_bcache_mount():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    mountpoint = str(data.get("mountpoint", "")).strip() or None
    if not re.fullmatch(r"bcache\d+", name):
        return jsonify(ok=False, error="非法设备名"), 400
    ok, msg = bcache_mgr.mount_bcache(name, mountpoint)
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/umount")
@admin_required
def api_bcache_umount():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not re.fullmatch(r"bcache\d+", name):
        return jsonify(ok=False, error="非法设备名"), 400
    ok, msg = bcache_mgr.umount_bcache(name)
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.get("/api/bcache/stats")
@login_required
def api_bcache_stats():
    device = str(request.args.get("device", "")).strip()
    if not re.fullmatch(r"bcache\d+", device):
        return jsonify(ok=False, error="非法设备名"), 400
    return jsonify(bcache_mgr.device_stats(device))


@app.get("/api/bcache/trend")
@login_required
def api_bcache_trend():
    device = str(request.args.get("device", "")).strip()
    if not re.fullmatch(r"bcache\d+", device):
        return jsonify(ok=False, error="非法设备名"), 400
    try:
        hours = max(1, min(72, int(request.args.get("hours", "26"))))
    except (TypeError, ValueError):
        hours = 26
    since = int(time.time()) - hours * 3600
    points = []
    try:
        for row in db.history("bcache_trend", since):
            if str(row.get("device", "")) != device:
                continue
            try:
                points.append({
                    "ts": int(row["_ts_epoch"]) * 1000,
                    "hour": int(row["hour_hit_ratio"]),
                    "day": int(row["day_hit_ratio"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
    except Exception:
        points = []
    points.sort(key=lambda p: p["ts"])
    return jsonify(device=device, hours=hours, points=points)


@app.post("/api/bcache/writeback")
@admin_required
def api_bcache_writeback():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not re.fullmatch(r"bcache\d+", name):
        return jsonify(ok=False, error="非法设备名"), 400
    ok, msg = bcache_mgr.trigger_writeback(name)
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/erase")
@admin_required
def api_bcache_erase():
    data = request.get_json(silent=True) or {}
    dev_path = str(data.get("device_path", "")).strip()
    confirm = str(data.get("confirm", "")).strip()
    if not bool(data.get("acknowledge")):
        return jsonify(ok=False, error="请勾选确认操作风险"), 400
    if confirm != dev_path:
        return jsonify(ok=False, error="确认文本与设备路径不一致"), 400
    ok, msg = bcache_mgr.erase_superblock(dev_path)
    if ok:
        lsi_alert.log_event("warning", f"bcache 超级块擦除：{dev_path}（{session.get('username', '')}）")
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.get("/api/bcache/alerts")
@login_required
def api_bcache_alerts_get():
    return jsonify(config=bcache_alerts.load_config())


@app.post("/api/bcache/alerts")
@admin_required
def api_bcache_alerts_save():
    data = request.get_json(silent=True) or {}
    cfg = bcache_alerts.load_config()
    try:
        if "enabled" in data:
            cfg["enabled"] = bool(data["enabled"])
        for k, lo, hi in (("cache_available_warn", 1, 100), ("dirty_stuck_minutes", 1, 1440),
                          ("hit_drop_points", 1, 100)):
            if k in data:
                val = int(data[k])
                if not (lo <= val <= hi):
                    return jsonify(ok=False, error=f"{k} 超出范围"), 400
                cfg[k] = val
        if "backlog_bytes" in data:
            val = int(data["backlog_bytes"])
            if val <= 0:
                return jsonify(ok=False, error="backlog_bytes 必须为正数"), 400
            cfg["backlog_bytes"] = val
    except (ValueError, TypeError):
        return jsonify(ok=False, error="参数必须是整数"), 400
    bcache_alerts.save_config(cfg)
    lsi_alert.log_event("info", f"bcache 告警配置已更新（{session.get('username', 'system')}）")
    return jsonify(ok=True, config=cfg)


@app.get("/api/bcache/gc")
@login_required
def api_bcache_gc():
    uuid = str(request.args.get("uuid", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return jsonify(ok=False, error="非法缓存集 UUID"), 400
    return jsonify(bcache_mgr.gc_status(uuid))


@app.post("/api/bcache/gc/trigger")
@admin_required
def api_bcache_gc_trigger():
    data = request.get_json(silent=True) or {}
    uuid = str(data.get("uuid", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return jsonify(ok=False, error="非法缓存集 UUID"), 400
    ok, msg = bcache_mgr.trigger_gc(uuid)
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/cache_unregister")
@admin_required
def api_bcache_cache_unregister():
    data = request.get_json(silent=True) or {}
    uuid = str(data.get("uuid", "")).strip()
    confirm = str(data.get("confirm", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return jsonify(ok=False, error="非法缓存集 UUID"), 400
    if not bool(data.get("acknowledge")):
        return jsonify(ok=False, error="请勾选风险确认"), 400
    if confirm != uuid:
        return jsonify(ok=False, error="确认文本与缓存集 UUID 不一致"), 400
    ok, msg = bcache_mgr.unregister_cset(uuid)
    if ok:
        lsi_alert.log_event("warning", f"bcache 缓存集注销 {uuid}（{session.get('username', '')}）")
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.get("/api/bcache/tunables")
@login_required
def api_bcache_tunables():
    device = str(request.args.get("device", "")).strip()
    if not re.fullmatch(r"bcache\d+", device):
        return jsonify(ok=False, error="非法设备名"), 400
    return jsonify(bcache_mgr.tunables(device))


@app.post("/api/bcache/tune")
@admin_required
def api_bcache_tune():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    settings = data.get("settings")
    if not re.fullmatch(r"bcache\d+", name):
        return jsonify(ok=False, error="非法设备名"), 400
    if not isinstance(settings, dict) or not settings:
        return jsonify(ok=False, error="settings 不能为空"), 400
    results = []
    for key, value in settings.items():
        ok, msg = bcache_mgr.set_tunable(name, key, value)
        if not ok:
            return jsonify(ok=False, error=f"{key}: {msg}"), 400
        results.append(msg)
    lsi_alert.log_event("info", f"bcache {name} 比例调节：{', '.join(results)}（{session.get('username', 'system')}）")
    return jsonify(ok=True, message="；".join(results))


@app.post("/api/bcache/reattach")
@admin_required
def api_bcache_reattach():
    data = request.get_json(silent=True) or {}
    backing_path = str(data.get("backing_path", "")).strip()
    cset_uuid = str(data.get("cset_uuid", "")).strip()
    if not re.fullmatch(r"/dev/(sd[a-z]+|nvme\d+n\d+)$", backing_path):
        return jsonify(ok=False, error="非法 backing 设备路径"), 400
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", cset_uuid):
        return jsonify(ok=False, error="非法缓存集 UUID"), 400
    ok, msg, dev = bcache_mgr.reattach(backing_path, cset_uuid)
    if ok:
        lsi_alert.log_event("warning", f"bcache 重新绑定 {backing_path} → {cset_uuid}（{session.get('username', '')}）")
    return jsonify(ok=ok, message=msg, device=dev) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/recover")
@admin_required
def api_bcache_recover():
    ok, msg = bcache_mgr.recover()
    if ok:
        lsi_alert.log_event("warning", f"bcache 缓存恢复：{msg}（{session.get('username', '')}）")
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/destroy_cache")
@admin_required
def api_bcache_destroy_cache():
    data = request.get_json(silent=True) or {}
    cache_path = str(data.get("cache_path", "")).strip()
    confirm = str(data.get("confirm", "")).strip()
    if not re.fullmatch(r"/dev/(sd[a-z]+|nvme\d+n\d+)$", cache_path):
        return jsonify(ok=False, error="非法缓存盘路径"), 400
    if not bool(data.get("acknowledge")):
        return jsonify(ok=False, error="请勾选风险确认"), 400
    if confirm != cache_path:
        return jsonify(ok=False, error="确认文本与设备路径不一致"), 400
    ok, msg = bcache_mgr.destroy_cache(cache_path)
    if ok:
        lsi_alert.log_event("warning", f"bcache 缓存盘销毁并清空：{cache_path}（{session.get('username', '')}）")
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.get("/api/bcache/emergency")
@login_required
def api_bcache_emergency():
    return jsonify(bcache_mgr.emergency_status())


@app.post("/api/bcache/emergency/mount")
@admin_required
def api_bcache_emergency_mount():
    data = request.get_json(silent=True) or {}
    backing_path = str(data.get("backing_path", "")).strip()
    if not re.fullmatch(r"/dev/(sd[a-z]+|nvme\d+n\d+)$", backing_path):
        return jsonify(ok=False, error="非法 backing 路径"), 400
    ok, msg, mp = bcache_mgr.emergency_mount(backing_path)
    return (jsonify(ok=True, message=msg, mountpoint=mp), 200) if ok else (jsonify(ok=False, error=msg), 500)


@app.post("/api/bcache/emergency/unmount")
@admin_required
def api_bcache_emergency_unmount():
    data = request.get_json(silent=True) or {}
    backing_path = str(data.get("backing_path", "")).strip()
    ok, msg = bcache_mgr.emergency_unmount(backing_path)
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 500)


# ---- 用户管理 API ----


@app.get("/api/users")
@admin_required
def api_users_list():
    if AUTH_MODE == "pam":
        return jsonify(users=[]), 200
    return jsonify(users=user_mgr.list_users())


@app.post("/api/users")
@admin_required
def api_users_create():
    if AUTH_MODE == "pam":
        return jsonify(ok=False, error="PAM 模式下用户由系统管理"), 501
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
    if AUTH_MODE == "pam":
        return jsonify(ok=False, error="PAM 模式下用户由系统管理"), 501
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
    if AUTH_MODE == "pam":
        return jsonify(ok=False, error="PAM 模式下用户由系统管理"), 501
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


@app.get("/api/enclosure")
@login_required
def api_enclosure():
    """机箱背板（SES）真实槽位与指示灯状态，并与控制器视角比对。"""
    try:
        st = ses_mgr.status()
    except Exception as e:
        return jsonify(available=False, error=str(e), enclosures=[], totals={}, issues=[]), 200
    disks = build_status().get("physical_disks", [])
    st["issues"] = ses_mgr.compare(disks)
    return jsonify(st)


@app.get("/api/maintenance")
@login_required
def api_maintenance_get():
    return jsonify(maint_mgr.view())


@app.post("/api/maintenance")
@perm_required("configure")
def api_maintenance_save():
    data = request.get_json(silent=True) or {}
    cfg = maint_mgr.load_config()
    try:
        if "plans" in data:
            cfg["plans"] = maint_mgr.normalize(data["plans"])
        if "enabled" in data:
            cfg["enabled"] = bool(data["enabled"])
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    saved = maint_mgr.save_config(cfg)
    lsi_alert.log_event(
        "info",
        f"维护计划已更新（{session.get('username', 'system')}）："
        f"{len(saved.get('plans') or [])} 条计划，"
        + ("已启用" if saved.get("enabled") else "已停用"),
    )
    return jsonify(ok=True, **maint_mgr.view())


@app.post("/api/maintenance/run")
@perm_required("operate")
def api_maintenance_run():
    """立即执行一条计划，用于验证配置。"""
    data = request.get_json(silent=True) or {}
    res = maint_mgr.run_now(data.get("id"), trigger=f"手动（{session.get('username', 'system')}）")
    return jsonify(**res, **maint_mgr.view())


_policy_cache: dict = {"at": 0.0, "ctrl": None, "data": None}


def _read_ctrl_policies(ctrl: int, force: bool = False) -> dict:
    """逐条 `show <key> J` 读策略。

    17 条命令串行跑要 6~8 秒，所以做 120 秒缓存；改完策略会主动失效。
    不支持某条策略的卡返回空字符串，由前端标成「不支持」，不影响其它项。
    """
    import time as _t

    if (
        not force
        and _policy_cache["data"] is not None
        and _policy_cache["ctrl"] == ctrl
        and _t.time() - _policy_cache["at"] < 120
    ):
        return _policy_cache["data"]
    values = {}
    for key in ctrl_policy_mgr.CTRL_KEYS:
        try:
            ok, out = _run_storcli_text(f"/c{ctrl} show {key} J", timeout=20)
            if not ok or not out:
                values[key] = ""
                continue
            doc = json.loads(out)
            resp = (doc.get("Controllers") or [{}])[0].get("Response Data") or {}
            values[key] = ctrl_policy_mgr.parse_value(key, ctrl_policy_mgr.pick_value(resp))
        except Exception:
            values[key] = ""
    _policy_cache.update(at=_t.time(), ctrl=ctrl, data=values)
    return values


@app.get("/api/data_backup")
@login_required
def api_data_backup_status():
    cfg = data_backup_mgr.load_config()
    return jsonify(**data_backup_mgr.status(), history=cfg.get("history", [])[:20])


@app.post("/api/data_backup")
@perm_required("configure")
def api_data_backup_start():
    """启动一次数据备份（P0：全量 + 增量、本地/NFS/SSH 三种目的地）。"""
    job = request.get_json(silent=True) or {}
    ok, msg = data_backup_mgr.start(job)
    if ok:
        lsi_alert.log_event(
            "warning",
            f"数据备份已启动：{job.get('source')} → {job.get('target')}（{session.get('username', '')}）",
        )
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 400)


@app.get("/api/data_backup/schedule")
@login_required
def api_data_backup_schedule():
    cfg = data_backup_mgr.load_config()
    return jsonify(schedules=cfg.get("schedules", []), last_run=cfg.get("last_run", {}))


@app.post("/api/data_backup/schedule")
@perm_required("configure")
def api_data_backup_schedule_save():
    data = request.get_json(silent=True) or {}
    cfg = data_backup_mgr.load_config()
    sched = data.get("schedule") or {}
    if not isinstance(sched, dict) or not sched.get("job"):
        return jsonify(ok=False, error="缺少作业配置"), 400
    t = str(sched.get("time") or "")
    if len(t) != 5 or t[2] != ":":
        return jsonify(ok=False, error="时间格式应为 HH:MM"), 400
    if str(sched.get("freq") or "daily") not in ("daily", "weekly", "monthly"):
        return jsonify(ok=False, error="频率只能是 daily/weekly/monthly"), 400
    sched.setdefault("id", "bkp-1")
    sched["enabled"] = bool(sched.get("enabled", True))
    list_ = [x for x in (cfg.get("schedules") or []) if str(x.get("id")) != str(sched["id"])]
    list_.append(sched)
    cfg["schedules"] = list_
    data_backup_mgr.save_config(cfg)
    lsi_alert.log_event("info", f"定时备份已更新（{session.get('username', '')}）")
    return jsonify(ok=True, schedules=cfg["schedules"])


@app.get("/api/fs/browse")
@login_required
def api_fs_browse():
    """目录浏览器：只列一级子目录，不递归。

    path 必须是已存在的目录；越界/不存在都会回到根目录，避免报错打断浏览。
    返回 parent 便于"上一级"，is_mount 让前端能标出挂载点。
    """
    raw = str(request.args.get("path") or "/").strip() or "/"
    try:
        real = os.path.realpath(raw)
    except OSError:
        real = "/"
    if not os.path.isdir(real):
        real = "/"
    dirs = []
    try:
        with os.scandir(real) as it:
            for e in it:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    dirs.append({
                        "name": e.name,
                        "path": os.path.join(real, e.name),
                        "writable": os.access(e.path, os.W_OK),
                        "is_mount": os.path.ismount(e.path),
                    })
                except OSError:
                    continue
    except OSError as e:
        return jsonify(ok=False, error=str(e), path=real, parent="", dirs=[]), 200
    dirs.sort(key=lambda x: x["name"].lower())
    parent = os.path.dirname(real.rstrip("/")) or "/"
    return jsonify(ok=True, path=real, parent=parent, dirs=dirs[:500])


@app.get("/api/fs/paths")
@login_required
def api_fs_paths():
    """给备份表单用的路径候选：真实挂载点 + /mnt 下的一级子目录。

    不做任意目录递归——那在几十万文件的目录里会卡死服务。挂载点是备份
    最常选的目标，一级子目录覆盖"盘里再分目录"的常见情况，其余允许手输。
    """
    items = []
    try:
        out = subprocess.run(["findmnt", "-rn", "-o", "TARGET,FSTYPE"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            p = line.split()
            if len(p) < 2:
                continue
            tgt, fs = p[0], p[1]
            if tgt.startswith(("/proc", "/sys", "/dev", "/run", "/snap")):
                continue
            if not os.path.isdir(tgt):
                continue
            items.append({"path": tgt, "kind": "mount", "fstype": fs,
                          "writable": os.access(tgt, os.W_OK)})
    except Exception:
        pass
    for base in ("/mnt", "/srv", "/data"):
        try:
            for name in sorted(os.listdir(base))[:60]:
                full = os.path.join(base, name)
                if os.path.isdir(full) and not os.path.islink(full):
                    items.append({"path": full, "kind": "dir", "fstype": "",
                                  "writable": os.access(full, os.W_OK)})
        except OSError:
            continue
    return jsonify(paths=items[:200])


@app.get("/api/data_backup/archives")
@login_required
def api_data_backup_archives():
    dest = str(request.args.get("dest") or "").strip()
    if not dest:
        return jsonify(ok=False, error="缺少 dest"), 400
    return jsonify(ok=True, archives=data_backup_mgr.list_archives(dest))


@app.post("/api/data_backup/restore")
@perm_required("configure")
def api_data_backup_restore():
    """恢复（P1）。默认不覆盖，dry_run 只列将写入的条目。"""
    data = request.get_json(silent=True) or {}
    try:
        res = data_backup_mgr.restore(
            str(data.get("archive") or ""),
            str(data.get("target") or ""),
            str(data.get("prefix") or ""),
            bool(data.get("dry_run")),
        )
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    if not res.get("dry_run"):
        lsi_alert.log_event(
            "warning",
            f"数据恢复完成：{data.get('archive')} → {data.get('target')}（{session.get('username', '')}）",
        )
    return jsonify(ok=True, **res)


@app.post("/api/data_backup/restore_chain")
@perm_required("configure")
def api_data_backup_restore_chain():
    """增量链恢复：从全量基线起，按顺序应用每一份增量。"""
    data = request.get_json(silent=True) or {}
    try:
        res = data_backup_mgr.restore_chain(
            str(data.get("archive") or ""),
            str(data.get("target") or ""),
            str(data.get("prefix") or ""),
            bool(data.get("dry_run")),
        )
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    if not res.get("dry_run"):
        lsi_alert.log_event(
            "warning",
            f"增量链恢复完成：{len(res.get('chain') or [])} 份 → {data.get('target')}"
            f"（{session.get('username', '')}）",
        )
    return jsonify(ok=True, **res)


@app.post("/api/data_backup/drill")
@perm_required("operate")
def api_data_backup_drill():
    """立即做一次恢复演练：真恢复到临时目录、核对、删除。"""
    data = request.get_json(silent=True) or {}
    archive = str(data.get("archive") or "")
    try:
        res = data_backup_mgr.drill(archive)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    lsi_alert.log_event("info", f"恢复演练通过：{archive}（{res['files']} 文件）")
    return jsonify(ok=True, **res)


@app.get("/api/ctrl_policy")
@login_required
def api_ctrl_policy():
    ctrl = _resolve_ctrl(request.args)
    vds = []
    try:
        vds = [
            {"vd": v.get("dg_vd"), "name": v.get("name") or "", "state": v.get("state") or ""}
            for v in build_status().get("virtual_disks", [])
            if _to_int(v.get("ctrl"), 0) == ctrl
        ]
    except Exception:
        pass
    return jsonify(
        ctrl=ctrl,
        values=_read_ctrl_policies(ctrl),
        catalog=ctrl_policy_mgr.policy_catalog(),
        vds=vds,
    )


@app.post("/api/ctrl_policy")
@perm_required("configure")
def api_ctrl_policy_set():
    data = request.get_json(silent=True) or {}
    ctrl = _to_int(data.get("ctrl"), 0) or 0
    try:
        cmd = ctrl_policy_mgr.ctrl_cmd(ctrl, str(data.get("key") or ""), data.get("value"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    task = task_engine.submit(
        "ctrl_policy_set",
        {"ctrl": ctrl, "args": cmd},
        target=f"C{ctrl} 控制器策略",
        command=f"storcli {cmd}",
        operator=session.get("username", ""),
    )
    _policy_cache["at"] = 0
    lsi_alert.log_event(
        "warning", f"C{ctrl} 修改控制器策略：{cmd}（{session.get('username', '')}）"
    )
    return jsonify(ok=True, task=task)


@app.post("/api/vd_policy")
@perm_required("configure")
def api_vd_policy_set():
    data = request.get_json(silent=True) or {}
    ctrl = _to_int(data.get("ctrl"), 0) or 0
    vd = _to_int(data.get("vd"), None)
    if vd is None:
        return jsonify(ok=False, error="缺少 VD 编号"), 400
    try:
        cmd = ctrl_policy_mgr.vd_cmd(ctrl, vd, str(data.get("key") or ""), data.get("value"))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    task = task_engine.submit(
        "vd_policy_set",
        {"ctrl": ctrl, "vd": vd, "args": cmd},
        target=f"C{ctrl}/v{vd}",
        command=f"storcli {cmd}",
        operator=session.get("username", ""),
    )
    lsi_alert.log_event(
        "warning", f"C{ctrl}/v{vd} 修改虚拟盘策略：{cmd}（{session.get('username', '')}）"
    )
    return jsonify(ok=True, task=task)


@app.get("/api/phy_counters")
@login_required
def api_phy_counters():
    """PHY / 介质错误计数。

    这是"链路 vs 介质"的判据：PHY 的 CRC/丢码计数在涨是线缆/背板问题，
    介质错误在涨才是盘的问题。默认只看有盘的口，避免几秒的等待。
    """
    ctrl = _resolve_ctrl(request.args)
    rows = []
    try:
        disks = [
            d for d in build_status().get("physical_disks", [])
            if _to_int(d.get("ctrl"), 0) == ctrl
        ]
    except Exception:
        disks = []
    for d in disks:
        eid, slot = d.get("eid"), d.get("slot")
        item = {
            "label": d.get("full_label") or f"E{eid}:S{slot}",
            "eid": eid, "slot": slot, "model": d.get("model") or "",
            "phy": [], "err": "",
        }
        try:
            ok, out = _run_storcli_text(
                f"/c{ctrl}/e{eid}/s{slot} show phyerrorcounters J", timeout=20
            )
            if ok and out:
                doc = json.loads(out)
                resp = (doc.get("Controllers") or [{}])[0].get("Response Data") or {}
                item["phy"] = [
                    {"k": k, "v": v}
                    for k, v in ctrl_policy_mgr.flatten(resp)
                    if re.search(r"(error|crc|invalid|loss|reset)", str(k), re.I)
                ][:12]
            else:
                item["err"] = "读取失败"
        except Exception as e:
            item["err"] = str(e)[:80]
        rows.append(item)
    return jsonify(ctrl=ctrl, disks=rows)


@app.post("/api/phy_reset")
@perm_required("operate")
def api_phy_reset():
    data = request.get_json(silent=True) or {}
    ctrl = _to_int(data.get("ctrl"), 0) or 0
    eid = _to_int(data.get("eid"), None)
    slot = _to_int(data.get("slot"), None)
    if eid is None or slot is None:
        return jsonify(ok=False, error="缺少 eid/slot"), 400
    cmd = f"/c{ctrl}/e{eid}/s{slot} reset phyerrorcounters"
    task = task_engine.submit(
        "phy_reset", {"ctrl": ctrl, "args": cmd},
        target=f"C{ctrl} E{eid}:S{slot}",
        command=f"storcli {cmd}", operator=session.get("username", ""),
    )
    lsi_alert.log_event("warning", f"复位 PHY 错误计数 {eid}:{slot}（{session.get('username', '')}）")
    return jsonify(ok=True, task=task)


@app.get("/api/vpd")
@login_required
def api_vpd():
    """导出控制器 VPD（资产信息，售后换件时用）。"""
    ctrl = _resolve_ctrl(request.args)
    import io as _io
    import tempfile

    tmp = tempfile.NamedTemporaryFile(prefix="dmx-vpd-", suffix=".txt", delete=False)
    tmp.close()
    ok, out = _run_storcli_text(f"/c{ctrl} get vpd file={tmp.name}", timeout=30)
    try:
        with open(tmp.name, "rb") as f:
            blob = f.read()
    except OSError:
        blob = b""
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    if not blob:
        blob = (out or "").encode("utf-8", "replace")
    return send_file(
        _io.BytesIO(blob), mimetype="text/plain", as_attachment=True,
        download_name=f"datamax-vpd-c{ctrl}.txt",
    )


@app.get("/api/hardware")
@login_required
def api_hardware():
    """机箱传感器：风扇、温度、电压、机箱入侵。

    默认走 30 秒缓存——ipmitool 单次要 2~3 秒，每次请求都实采会让页面
    明显卡顿。`?refresh=1` 用于用户主动点"刷新"。
    """
    force = str(request.args.get("refresh", "")).strip() in ("1", "true", "yes")
    try:
        data = hardware_mgr.read_sensors(force=force)
        return jsonify(hardware_mgr.evaluate(data))
    except Exception as e:
        return jsonify(ok=False, error=str(e), severity="ok", groups={}, findings=[]), 200


@app.get("/api/perf/live")
@login_required
def api_perf_live():
    """实时速率。采样窗默认 1 秒，可用 ?interval= 调整（上限 3 秒）。"""

    def _f(name, default):
        try:
            return float(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    try:
        return jsonify(perf_mgr.live(_f("interval", 1.0)))
    except Exception as e:
        return jsonify(ok=False, error=str(e), rows=[], total={}), 200


@app.get("/api/io/diagnose")
@login_required
def api_io_diagnose():
    """「IO 很慢」的一键诊断。

    采样约 2 秒（设备两次计数求差 + 进程两次 /proc 计数求差），只在用户
    点按钮时跑，不做常驻轮询——诊断是有成本的，而且平时也没人看。
    """
    try:
        return jsonify(io_diag_mgr.diagnose(build_status()))
    except Exception as e:
        return jsonify(ok=False, error=str(e), findings=[], devices=[], processes=[]), 200


@app.get("/api/io/bench")
@login_required
def api_io_bench_status():
    """基准测试状态。附带可选的测试目标与 fio 是否可用。"""
    st = io_bench_mgr.status()
    return jsonify(
        **st,
        available=io_bench_mgr.fio_available(),
        targets=io_bench_mgr.targets(),
        modes=io_bench_mgr.modes(),
    )


@app.post("/api/io/bench")
@perm_required("configure")
def api_io_bench_start():
    """启动基准测试。

    会写盘，所以只允许管理员触发；所有安全校验都在 io_bench_mgr.validate
    里，接口层不做"再放宽一点"的处理。
    """
    data = request.get_json(silent=True) or {}
    ok, msg = io_bench_mgr.start(
        str(data.get("target") or ""),
        _to_int(data.get("size_mb"), 512),
        data,
        build_status(),
    )
    if ok:
        lsi_alert.log_event(
            "warning",
            f"基准测试已启动：{data.get('target')}（{session.get('username', '')}）",
        )
    return (jsonify(ok=True, message=msg), 200) if ok else (jsonify(ok=False, error=msg), 400)


@app.post("/api/io/bench/cancel")
@perm_required("operate")
def api_io_bench_cancel():
    io_bench_mgr.cancel()
    lsi_alert.log_event("warning", f"基准测试已取消（{session.get('username', '')}）")
    return jsonify(ok=True, **io_bench_mgr.status())


@app.get("/api/perf/history")
@login_required
def api_perf_history():
    hours = request.args.get("hours", "24")
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        hours = 24
    devices = [d for d in (request.args.get("devices", "") or "").split(",") if d]
    try:
        return jsonify(perf_mgr.series(hours, devices or None))
    except Exception as e:
        return jsonify(ok=False, error=str(e), series=[], devices=[]), 200


@app.get("/api/capacity/forecast")
@login_required
def api_capacity_forecast():
    try:
        days = int(request.args.get("days", capacity_mgr.DEFAULT_WINDOW_DAYS))
    except (TypeError, ValueError):
        days = capacity_mgr.DEFAULT_WINDOW_DAYS
    days = max(1, min(days, 365))
    try:
        return jsonify(capacity_mgr.forecast(days))
    except Exception as e:
        return jsonify(ok=False, error=str(e), items=[], risk=[]), 200


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

    if db.has_data():
        series: dict[str, list] = {}
        for row in db.history("fs", int(since.timestamp())):
            if row.get("mountpoint") in hidden:
                continue
            label = f"{row.get('device')} ({row.get('mountpoint')})"
            try:
                pct = float(row.get("use_percent") or 0)
            except (ValueError, TypeError):
                continue
            series.setdefault(label, []).append([int(row["_ts_epoch"] * 1000), pct])
        return jsonify(series=[{"label": k, "points": v} for k, v in sorted(series.items())])

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

    if db.has_data():
        samples: dict[str, list] = {}
        for row in db.history("io", int(since.timestamp())):
            samples.setdefault(row.get("name", ""), []).append(
                (
                    row["_ts_epoch"],
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
                dt = t2 - t1
                if dt <= 0 or r2 < r1 or w2 < w1:
                    continue
                out.append(
                    [
                        int(t2 * 1000),
                        round((r2 - r1) * 512 / 1024 / dt, 1),
                        round((w2 - w1) * 512 / 1024 / dt, 1),
                        round((io2 - io1) / dt, 1),
                    ]
                )
            series.append({"label": name, "points": out})
        return jsonify(series=series)

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


def _storcli_smart_hex(ctrl_id: int, eid: int, slot: int) -> str | None:
    """通过 storcli 读取指定物理盘（含未组阵列的 UGood/JBOD 盘）的原始 SMART 数据。"""
    try:
        proc = subprocess.run(
            f"sudo {STORCLI} /c{ctrl_id}/e{eid}/s{slot} show smart J",
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
@perm_required("operate")
def api_raid_action():
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", ""))
    action = str(data.get("action", ""))
    vd = _to_int(data.get("vd"))
    if target not in RAID_ACTIONS or action not in RAID_ACTIONS[target]:
        return jsonify(ok=False, error="非法操作"), 400
    # 巡读与一致性检查只读扫描数据，不改内容，属于日常运维；
    # 初始化/删除/导入外来配置会重建阵列结构，属于变更权限。
    if target not in OPERATE_RAID_TARGETS and not rbac.has_perm(current_role(), "configure"):
        return (
            jsonify(
                ok=False,
                error=f"当前角色（{rbac.role_label(current_role())}）没有「变更配置」权限",
                perm="configure",
            ),
            403,
        )
    if "{vd}" in RAID_ACTIONS[target][action] and vd is None:
        return jsonify(ok=False, error="缺少 VD 编号"), 400
    ctrl_id = _resolve_ctrl(data)
    cmd = RAID_ACTIONS[target][action].format(
        ctrl=f"/c{ctrl_id}", vd=vd if vd is not None else 0
    )
    desc = (
        f"C{ctrl_id} {RAID_ACTION_LABEL.get(target, target)}"
        + (f" (v{vd})" if vd is not None else "")
    )
    # 改为任务化：CC / 初始化这类操作可能跑几十分钟，同步等待会把
    # Web 请求线程占死，用户也看不到进度。
    kind = TASK_KIND_BY_ACTION.get((target, action))
    if kind:
        task = task_engine.submit(
            kind,
            {"ctrl": ctrl_id, "vd": vd, "args": cmd, "target": target, "action": action},
            target=desc,
            command=f"storcli {cmd}",
            operator=session.get("username", ""),
        )
        lsi_alert.log_event(
            "warning", f"{desc} 已提交（{session.get('username', '')}）"
        )
        return jsonify(ok=True, task=task)

    ok, msg = _run_storcli(cmd)
    if ok:
        lsi_alert.log_event("warning", f"{desc}（{session.get('username', '')}）")
        task_engine.record_instant(
            f"raid_{target}",
            {"ctrl": ctrl_id, "vd": vd, "args": cmd},
            ok=True,
            target=desc,
            command=f"storcli {cmd}",
            operator=session.get("username", ""),
        )
        _refresh_after_action()
        return jsonify(ok=True)
    lsi_alert.log_event("error", f"{desc} 失败 — {msg}")
    task_engine.record_instant(
        f"raid_{target}",
        {"ctrl": ctrl_id, "vd": vd, "args": cmd},
        ok=False,
        message=msg,
        target=desc,
        command=f"storcli {cmd}",
        operator=session.get("username", ""),
    )
    return jsonify(ok=False, error=msg), 500


def _fill_vd_disks_by_dg(vds: list[dict], ctrl_id: int = 0):
    """对成员盘列表为空的 VD（storcli 明细查询失败），按物理盘摘要中的
    DG 归属补充成员盘。DG/VD 形如 "2/1"，"/" 前为 DG 号。"""
    try:
        cmd = f"sudo {STORCLI} /c{ctrl_id}/eall/sall show J"
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


def _vd_migrate_status(ctrl_id: int = 0) -> dict[int, dict]:
    """读取指定控制器上各 VD 的迁移/扩容进度（Progress%、Status、ETA）。"""
    out: dict[int, dict] = {}
    try:
        ok, text = _run_storcli_text(f"/c{ctrl_id}/vall show migrate", timeout=60)
        if not ok:
            return out
        for line in text.splitlines():
            m = re.match(r"^\s*(\d+)\s+(\S+)\s+(\d+)\s+(.+?)\s*$", line)
            if not m:
                continue
            tail = m.group(4).strip()
            status = tail
            eta = ""
            for kw in ("In progress", "Completed", "Not in progress", "Aborted", "Paused"):
                if tail.startswith(kw):
                    status = kw
                    eta = tail[len(kw):].strip()
                    break
            out[int(m.group(1))] = {
                "operation": m.group(2),
                "progress": int(m.group(3)),
                "status": status,
                "eta": eta,
            }
    except Exception:
        pass
    return out


def _decode_write_cache(code: str) -> str:
    c = str(code or "").upper()
    if "AWB" in c:
        return "Always Write Back"
    if "WT" in c:
        return "Write Through"
    if "WB" in c:
        return "Write Back"
    return code or "—"


def _vd_detail_for_controller(ctrl_id: int) -> tuple[list[dict], str]:
    """读取单张控制器上的全部虚拟磁盘明细。返回 (vds, 错误信息)。"""
    cmd = f"sudo {STORCLI} /c{ctrl_id}/vall show all J"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=45)
        output = proc.stdout.strip()
        json_start = output.find("{")
        if json_start == -1:
            return [], "storcli 无输出"
        data = json.loads(output[json_start:])
    except Exception as e:
        return [], str(e)

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
            cache_code = summary.get("Cache", "")
            vds.append(
                {
                    "ctrl": ctrl_id,
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
                    "write_cache": _decode_write_cache(cache_code),
                    "write_cache_raw": cache_code,
                    "write_cache_initial": props.get("Write Cache(initial setting)", ""),
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
            _fill_vd_disks_by_dg(missing, ctrl_id)
        migrates = _vd_migrate_status(ctrl_id)
        for v in vds:
            mg = migrates.get(v["vd"])
            v["migrate_operation"] = mg.get("operation") if mg else None
            v["migrate_progress"] = mg.get("progress") if mg else None
            v["migrate_status"] = mg.get("status") if mg else None
            v["migrate_eta"] = mg.get("eta") if mg else None
        return vds, ""
    except Exception as e:
        return [], f"解析失败: {e}"


@app.get("/api/vd_detail")
@login_required
def api_vd_detail():
    """多控制器：逐张聚合，前端按 ctrl 分组并列展示。"""
    all_vds: list[dict] = []
    errors: list[str] = []
    for ctrl_id in lsi_collectd.list_controller_ids():
        vds, err = _vd_detail_for_controller(ctrl_id)
        if err:
            errors.append(f"C{ctrl_id}: {err}")
        all_vds.extend(vds)
    if not all_vds and errors:
        return jsonify(ok=False, error="；".join(errors)), 500
    return jsonify(
        vds=sorted(all_vds, key=lambda v: (v.get("ctrl", 0), v.get("vd", 0)))
    )


# 每张卡的控制器事件缓存（键为控制器编号）
_events_cache: dict = {"ts": {}, "data": {}}


@app.get("/api/controller_events")
@login_required
def api_controller_events():
    lines = min(_to_int(request.args.get("lines"), 200) or 200, 1000)
    q = (request.args.get("q") or "").strip()
    ctrl_id = _resolve_ctrl(request.args.to_dict())
    now = time.time()
    cached = _events_cache["data"].get(ctrl_id)
    if cached is not None and now - _events_cache["ts"].get(ctrl_id, 0) < 60:
        text = cached
    else:
        ok, msg = _run_storcli_text(f"/c{ctrl_id} show events", timeout=90)
        if not ok:
            return jsonify(ok=False, error=msg), 500
        text = msg
        _events_cache["data"][ctrl_id] = text
        _events_cache["ts"][ctrl_id] = now
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
        # 退出码非 0 时也要把 stdout 带回去：storcli 的失败原因写在
        # stdout（"Status = Failure" / "Description = ..."），只回 stderr
        # 会让调用方拿到"无输出"这种毫无信息量的原因。
        return False, (proc.stdout.strip() or proc.stderr.strip() or "storcli 无输出")
    except FileNotFoundError:
        return False, f"storcli 不存在: {STORCLI}"
    except subprocess.TimeoutExpired:
        return False, "storcli 执行超时"
    except Exception as e:
        return False, str(e)


# ---- 阵列卡报警（蜂鸣器）开关 ----

# 每张卡各自的蜂鸣器状态缓存（键为控制器编号）
_alarm_cache: dict = {"ts": {}, "state": {}}

ALARM_MODES = {"on": "打开", "silence": "临时关闭", "off": "永久关闭"}


def _read_ctrl_prop(prop: str, ctrl_id: int = 0) -> str | None:
    """读取指定控制器的布尔属性（alarm / jbod 等），返回 ON/OFF 等大写值。

    多卡机器上蜂鸣器和 JBOD 是每张卡各自的设置，不能读一张卡代表全部。"""
    cmd = f"sudo {STORCLI} /c{ctrl_id} show {prop} J"
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
    ctrl_id = _resolve_ctrl(request.args.to_dict())
    now = time.time()
    cached = _alarm_cache["state"].get(ctrl_id)
    if cached is not None and now - _alarm_cache["ts"].get(ctrl_id, 0) < 60:
        state = cached
    else:
        state = _read_ctrl_prop("alarm", ctrl_id)
        _alarm_cache["state"][ctrl_id] = state
        _alarm_cache["ts"][ctrl_id] = now
    return jsonify(ok=state is not None, ctrl=ctrl_id, alarm=state)


@app.post("/api/controller_alarm")
@admin_required
def api_controller_alarm_set():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip().lower()
    if mode not in ALARM_MODES:
        return jsonify(ok=False, error="非法模式"), 400
    ctrl_id = _resolve_ctrl(data)
    ok, msg = _run_storcli_text(f"/c{ctrl_id} set alarm={mode}", timeout=30)
    if ok and "Status = Success" in msg:
        lsi_alert.log_event(
            "warning",
            f"C{ctrl_id} 阵列卡报警已{ALARM_MODES[mode]}（{session.get('username', '')}）",
        )
        _alarm_cache["ts"][ctrl_id] = 0.0
        _alarm_cache["state"][ctrl_id] = None
        task_engine.record_instant(
            "controller_alarm",
            {"ctrl": ctrl_id, "mode": mode},
            ok=True,
            target=f"C{ctrl_id} 蜂鸣器 · {ALARM_MODES[mode]}",
            command=f"storcli /c{ctrl_id} set alarm={mode}",
            operator=session.get("username", ""),
        )
        return jsonify(ok=True, ctrl=ctrl_id)
    return jsonify(ok=False, error="storcli 执行失败" if ok else msg), 500


# ---- JBOD 模式开关 ----

# 每张卡各自的 JBOD 状态缓存（键为控制器编号）
_jbod_cache: dict = {"ts": {}, "state": {}}

JBOD_MODES = {"on": "打开", "off": "关闭"}


@app.get("/api/controller_jbod")
@login_required
def api_controller_jbod_get():
    ctrl_id = _resolve_ctrl(request.args.to_dict())
    now = time.time()
    cached = _jbod_cache["state"].get(ctrl_id)
    if cached is not None and now - _jbod_cache["ts"].get(ctrl_id, 0) < 60:
        state = cached
    else:
        state = _read_ctrl_prop("jbod", ctrl_id)
        _jbod_cache["state"][ctrl_id] = state
        _jbod_cache["ts"][ctrl_id] = now
    return jsonify(ok=state is not None, ctrl=ctrl_id, jbod=state)


@app.post("/api/controller_jbod")
@admin_required
def api_controller_jbod_set():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode", "")).strip().lower()
    if mode not in JBOD_MODES:
        return jsonify(ok=False, error="非法模式"), 400
    ctrl_id = _resolve_ctrl(data)
    ok, msg = _run_storcli_text(f"/c{ctrl_id} set jbod={mode}", timeout=30)
    if ok and "Status = Success" in msg:
        lsi_alert.log_event(
            "warning",
            f"C{ctrl_id} JBOD 模式已{JBOD_MODES[mode]}（{session.get('username', '')}）",
        )
        _jbod_cache["ts"][ctrl_id] = 0.0
        _jbod_cache["state"][ctrl_id] = None
        task_engine.record_instant(
            "controller_jbod",
            {"ctrl": ctrl_id, "mode": mode},
            ok=True,
            target=f"C{ctrl_id} JBOD · {JBOD_MODES[mode]}",
            command=f"storcli /c{ctrl_id} set jbod={mode}",
            operator=session.get("username", ""),
        )
        return jsonify(ok=True, ctrl=ctrl_id)
    return jsonify(ok=False, error="storcli 执行失败" if ok else msg), 500


# ---- 日志打包下载 ----


@app.get("/api/logs/download")
@login_required
@perm_required("audit")
def api_logs_download():
    """收集控制器 alilog、控制器事件与平台事件日志，打包 zip 下载"""
    import io
    import zipfile

    files = {}
    # 多控制器：逐张卡收集，文件名带控制器编号
    for cid in lsi_collectd.list_controller_ids():
        ok, out = _run_storcli_text(f"/c{cid} show alilog", timeout=180)
        files[f"c{cid}_alilog.txt"] = out if ok else f"C{cid} alilog 获取失败: {out}"
        ok, out = _run_storcli_text(f"/c{cid} show events", timeout=90)
        files[f"c{cid}_controller_events.txt"] = (
            out if ok else f"C{cid} 控制器事件获取失败: {out}"
        )
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
STORCLI_RAID_TYPES = ("r0", "r1", "r5", "r6", "r10", "r50", "r60")


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
    pd_map = {
        (_to_int(d.get("ctrl"), 0), _to_int(d["eid"]), _to_int(d["slot"])): d
        for d in status["physical_disks"]
    }
    specs = []
    ctrl_ids: set[int] = set()
    for dr in drives:
        eid = _to_int(dr.get("eid") if isinstance(dr, dict) else None)
        slot = _to_int(dr.get("slot") if isinstance(dr, dict) else None)
        if eid is None or slot is None:
            return jsonify(ok=False, error="磁盘参数非法"), 400
        ctrl_id = _to_int(dr.get("ctrl") if isinstance(dr, dict) else None)
        if ctrl_id is None:
            ctrl_id = _ctrl_for_disk(eid, slot, {})
        pd = pd_map.get((ctrl_id, eid, slot))
        if not pd:
            return jsonify(ok=False, error=f"磁盘 C{ctrl_id}:E{eid}:S{slot} 不存在"), 400
        state = pd.get("state", "")
        if state == "JBOD":
            # JBOD 盘需先转为 UGood
            ok, msg = _run_storcli(f"/c{ctrl_id}/e{eid}/s{slot} set good force")
            if not ok:
                return jsonify(
                    ok=False,
                    error=f"磁盘 C{ctrl_id}:E{eid}:S{slot} JBOD 转 UGood 失败: {msg}",
                ), 500
            lsi_alert.log_event(
                "warning",
                f"磁盘 C{ctrl_id}:E{eid}:S{slot} JBOD → UGood（创建阵列前置）",
            )
        elif state != "UGood":
            return jsonify(
                ok=False,
                error=f"磁盘 C{ctrl_id}:E{eid}:S{slot} 状态为 {state or '未知'}，仅未配置（UGood/JBOD）的磁盘可创建阵列",
            ), 400
        ctrl_ids.add(ctrl_id)
        specs.append(f"{eid}:{slot}")

    # 一个 RAID 阵列的成员盘必须挂在同一张控制器上——跨卡组阵列在
    # 硬件上不成立，storcli 会拒绝或产生半个阵列
    if len(ctrl_ids) > 1:
        cards = "、".join(f"C{c}" for c in sorted(ctrl_ids))
        return jsonify(
            ok=False,
            error=f"成员盘分布在多张 RAID 卡上（{cards}），一个阵列只能使用同一张卡上的磁盘",
        ), 400
    ctrl_id = next(iter(ctrl_ids)) if ctrl_ids else 0

    if len(set(specs)) != len(specs):
        return jsonify(ok=False, error="磁盘列表有重复"), 400

    # storcli add vd 语法要求参数顺序为：r<level> [name=...] drives=... [PDperArray=...]
    cmd = f"/c{ctrl_id} add vd r{level}"
    if name:
        cmd += f" name={name}"
    cmd += f" drives={','.join(specs)}"
    if level == "50":
        # RAID50 必须指定每个 RAID5 子组的盘数，固定为 3（盘数已校验为 3 的倍数）
        cmd += " pdperarray=3"
    # 校验已在上方完成（那时读到的是最新状态）；这里只登记任务，
    # 实际执行交给串行的 task worker，界面不再被初始化过程卡住。
    label = f"C{ctrl_id} RAID{level} drives={','.join(specs)}"
    if name:
        label += f" 名称={name}"
    task = task_engine.submit(
        "raid_create",
        {
            "ctrl": ctrl_id,
            "level": level,
            "name": name,
            "specs": specs,
            "args": cmd,
        },
        target=label,
        command=f"storcli {cmd}",
        operator=session.get("username", ""),
    )
    lsi_alert.log_event(
        "warning", f"{label} 已提交（{session.get('username', '')}）"
    )
    return jsonify(ok=True, ctrl=ctrl_id, task=task)


@app.post("/api/raid_expand")
@admin_required
def api_raid_expand():
    """在线扩容：向已有虚拟磁盘动态加入 UGood 盘（storcli start migrate option=add）。"""
    data = request.get_json(silent=True) or {}
    vd = _to_int(data.get("vd"))
    raid = str(data.get("raid", "")).strip().lower()
    drives = data.get("drives")
    if vd is None or raid not in STORCLI_RAID_TYPES:
        return jsonify(ok=False, error="参数非法"), 400
    if not isinstance(drives, list) or not (1 <= len(drives) <= 32):
        return jsonify(ok=False, error="请选择要加入阵列的磁盘"), 400

    # 扩容目标 VD 必须和加入的盘在同一张控制器上
    ctrl_id = _resolve_ctrl(data)
    status = build_status()
    pd_map = {
        (_to_int(d.get("ctrl"), 0), _to_int(d["eid"]), _to_int(d["slot"])): d
        for d in status["physical_disks"]
    }
    specs = []
    for dr in drives:
        eid = _to_int(dr.get("eid") if isinstance(dr, dict) else None)
        slot = _to_int(dr.get("slot") if isinstance(dr, dict) else None)
        if eid is None or slot is None:
            return jsonify(ok=False, error="磁盘参数非法"), 400
        disk_ctrl = _to_int(dr.get("ctrl") if isinstance(dr, dict) else None)
        if disk_ctrl is None:
            disk_ctrl = _ctrl_for_disk(eid, slot, {})
        if disk_ctrl != ctrl_id:
            return jsonify(
                ok=False,
                error=f"磁盘 C{disk_ctrl}:E{eid}:S{slot} 不在目标控制器 C{ctrl_id} 上",
            ), 400
        pd = pd_map.get((disk_ctrl, eid, slot))
        if not pd:
            return jsonify(ok=False, error=f"磁盘 C{disk_ctrl}:E{eid}:S{slot} 不存在"), 400
        state = pd.get("state", "")
        if state == "JBOD":
            ok, msg = _run_storcli(f"/c{ctrl_id}/e{eid}/s{slot} set good force")
            if not ok:
                return jsonify(
                    ok=False,
                    error=f"磁盘 C{ctrl_id}:E{eid}:S{slot} JBOD 转 UGood 失败: {msg}",
                ), 500
        elif state != "UGood":
            return jsonify(
                ok=False,
                error=f"磁盘 C{ctrl_id}:E{eid}:S{slot} 状态为 {state or '未知'}，仅 UGood 盘可加入扩容",
            ), 400
        specs.append(f"{eid}:{slot}")

    if len(set(specs)) != len(specs):
        return jsonify(ok=False, error="磁盘列表有重复"), 400

    cmd = f"/c{ctrl_id}/v{vd} start migrate type={raid} option=add drives={','.join(specs)}"
    # 在线扩容是长任务（可能数小时），必须任务化：同步等待会占死请求线程
    label = f"C{ctrl_id} VD{vd} 扩容：加入 {','.join(specs)} → RAID{raid}"
    task = task_engine.submit(
        "raid_expand",
        {"ctrl": ctrl_id, "vd": vd, "raid": raid, "specs": specs, "args": cmd},
        target=label,
        command=f"storcli {cmd}",
        operator=session.get("username", ""),
    )
    lsi_alert.log_event(
        "warning", f"{label} 已提交（{session.get('username', '')}）"
    )
    return jsonify(ok=True, ctrl=ctrl_id, task=task)


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
