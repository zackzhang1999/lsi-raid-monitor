#!/usr/bin/env python3
# ================================================
# LSI MegaRAID 邮件报警模块
# 被 lsi_collectd.py 每分钟调用，也被 web_server.py 用于读取/保存报警配置
#
# 配置文件: $LSI_DATA_DIR/alert_config.json
# 环境变量（优先级更高，设置后 Web 中对应字段锁定）:
#   ALERT_EMAIL_TO   报警收件人，多个用逗号分隔
#   SENDMAIL_PATH    sendmail 路径（默认 /usr/sbin/sendmail）
#   ALERT_TEMP_WARN  温度警告阈值 °C（默认 45）
#   ALERT_TEMP_CRIT  温度临界阈值 °C（默认 55）
# ================================================

from __future__ import annotations

import json
import os
import subprocess
import socket
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))

CONFIG_FILE = BASE_DIR / "alert_config.json"
STATE_FILE = BASE_DIR / ".alert_state.json"
EVENTS_FILE = BASE_DIR / "events.jsonl"

DEFAULT_CONFIG = {
    "alert_email_to": "",
    "sendmail_path": "/usr/sbin/sendmail",
    "temp_warn": 45,
    "temp_crit": 55,
}

# 环境变量 -> 配置字段
ENV_OVERRIDES = {
    "alert_email_to": "ALERT_EMAIL_TO",
    "sendmail_path": "SENDMAIL_PATH",
    "temp_warn": "ALERT_TEMP_WARN",
    "temp_crit": "ALERT_TEMP_CRIT",
}


# ---- 配置读写 ----


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for key in DEFAULT_CONFIG:
                if key in saved:
                    cfg[key] = saved[key]
    except Exception as e:
        print(f"[alert] config load error: {e}")
    # 环境变量覆盖
    for key, env in ENV_OVERRIDES.items():
        val = os.environ.get(env)
        if val is not None and val != "":
            if key in ("temp_warn", "temp_crit"):
                try:
                    val = int(val)
                except ValueError:
                    continue
            cfg[key] = val
    return cfg


def save_config(new_cfg: dict):
    """保存配置到 JSON；被环境变量锁定的字段不写入"""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = {}
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
    except Exception:
        cfg = {}
    for key in DEFAULT_CONFIG:
        if is_locked(key):
            continue
        if key in new_cfg:
            cfg[key] = new_cfg[key]
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def is_locked(key: str) -> bool:
    env = ENV_OVERRIDES.get(key)
    return bool(env and os.environ.get(env))


def locked_fields() -> dict:
    return {key: is_locked(key) for key in DEFAULT_CONFIG}


def sendmail_available(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    path = cfg.get("sendmail_path") or ""
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def alert_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    return bool(str(cfg.get("alert_email_to", "")).strip())


# ---- 事件日志（Web 事件页面读取同一文件）----


def log_event(level: str, message: str):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "message": message,
    }
    try:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[alert] event log error: {e}")


# ---- 邮件发送 ----


def send_mail(subject: str, body: str, cfg: dict | None = None) -> tuple[bool, str]:
    cfg = cfg or load_config()
    recipients = [
        r.strip() for r in str(cfg.get("alert_email_to", "")).split(",") if r.strip()
    ]
    if not recipients:
        return False, "未配置报警收件人"
    sendmail = cfg.get("sendmail_path") or "/usr/sbin/sendmail"
    if not (os.path.isfile(sendmail) and os.access(sendmail, os.X_OK)):
        return False, f"sendmail 不可用: {sendmail}"

    host = socket.gethostname()
    msg = (
        f"From: lsi-raid-monitor@{host}\n"
        f"To: {', '.join(recipients)}\n"
        f"Subject: [LSI RAID] {subject}\n"
        f"Content-Type: text/plain; charset=utf-8\n"
        f"\n{body}\n"
    )
    try:
        proc = subprocess.run(
            [sendmail, "-t", "-oi"],
            input=msg.encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return True, "发送成功"
        return False, proc.stderr.decode("utf-8", "ignore").strip() or "sendmail 失败"
    except Exception as e:
        return False, str(e)


def _alert(subject: str, body: str, level: str = "error"):
    """记录事件并按配置发送邮件"""
    log_event(level, f"{subject} — {body.splitlines()[0] if body else ''}")
    cfg = load_config()
    if not alert_enabled(cfg):
        return
    ok, err = send_mail(subject, body, cfg)
    if not ok:
        log_event("warning", f"报警邮件发送失败: {err}")


# ---- 状态快照（去重：只在状态变化时报警）----


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(state: dict):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


# ---- 采集器调用的检查入口 ----


def check_and_alert(disks: list[dict], ctrl: dict | None):
    """每分钟调用：温度阈值 / SMART 告警 / 预测性故障 / 控制器与 BBU 健康"""
    cfg = load_config()
    temp_warn = int(cfg.get("temp_warn", 45))
    temp_crit = int(cfg.get("temp_crit", 55))
    state = _load_state()
    flagged = state.setdefault("flagged", {})

    def flag_once(key: str, subject: str, body: str, level: str = "error"):
        if not flagged.get(key):
            flagged[key] = True
            _alert(subject, body, level)

    def unflag(key: str):
        flagged.pop(key, None)

    for d in disks or []:
        label = f"E{d.get('eid')}:S{d.get('slot')}"
        temp = d.get("temperature")
        if isinstance(temp, int):
            if temp >= temp_crit:
                flag_once(
                    f"temp_crit_{label}",
                    f"磁盘 {label} 温度临界 {temp}°C",
                    f"磁盘 {label} ({d.get('model', '')}) 温度 {temp}°C，超过临界阈值 {temp_crit}°C。",
                )
            elif temp >= temp_warn:
                flag_once(
                    f"temp_warn_{label}",
                    f"磁盘 {label} 温度偏高 {temp}°C",
                    f"磁盘 {label} ({d.get('model', '')}) 温度 {temp}°C，超过警告阈值 {temp_warn}°C。",
                    "warning",
                )
                unflag(f"temp_crit_{label}")
            else:
                unflag(f"temp_warn_{label}")
                unflag(f"temp_crit_{label}")

        if str(d.get("smart_alert", "")).strip() == "Yes":
            flag_once(
                f"smart_{label}",
                f"磁盘 {label} SMART 告警",
                f"磁盘 {label} ({d.get('model', '')}) SMART alert flagged by drive。",
            )
        else:
            unflag(f"smart_{label}")

        try:
            pf = int(d.get("predictive_failure") or 0)
        except (ValueError, TypeError):
            pf = 0
        if pf > 0:
            flag_once(
                f"pf_{label}",
                f"磁盘 {label} 预测性故障计数 {pf}",
                f"磁盘 {label} ({d.get('model', '')}) Predictive Failure Count = {pf}。",
            )
        else:
            unflag(f"pf_{label}")

    if ctrl:
        health = str(ctrl.get("health", "")).strip()
        if health and health not in ("Optimal", "N/A"):
            flag_once(
                "ctrl_health",
                f"控制器健康异常: {health}",
                f"控制器 {ctrl.get('model', '')} 健康状态为 {health}。",
            )
        else:
            unflag("ctrl_health")

        bbu = str(ctrl.get("bbu_state", "")).strip()
        if bbu and bbu not in ("Optimal", "Opt", "OK"):
            flag_once(
                "bbu_state",
                f"BBU/缓存单元异常: {bbu}",
                f"BBU {ctrl.get('bbu_model', '')} 状态为 {bbu}。",
            )
        else:
            unflag("bbu_state")

    _save_state(state)


def check_state_changes(disks: list[dict], vds: list[dict]):
    """每分钟调用：磁盘 / VD 状态变化告警"""
    state = _load_state()
    prev_disks = state.get("disk_states", {})
    prev_vds = state.get("vd_states", {})

    cur_disks = {}
    for d in disks or []:
        label = f"E{d.get('eid')}:S{d.get('slot')}"
        st = str(d.get("state", "")).strip()
        if st and st != "N/A":
            cur_disks[label] = st
    for label, st in cur_disks.items():
        old = prev_disks.get(label)
        if old is not None and old != st:
            _alert(
                f"磁盘 {label} 状态变化: {old} → {st}",
                f"磁盘 {label} 状态由 {old} 变为 {st}。",
                "error" if st not in ("Onln", "UGood", "JBOD") else "warning",
            )

    cur_vds = {}
    for v in vds or []:
        key = str(v.get("dg_vd", ""))
        st = str(v.get("state", "")).strip()
        if key and st:
            cur_vds[key] = st
    for key, st in cur_vds.items():
        old = prev_vds.get(key)
        if old is not None and old != st:
            _alert(
                f"虚拟磁盘 {key} 状态变化: {old} → {st}",
                f"虚拟磁盘 {key} 状态由 {old} 变为 {st}。",
                "error" if st not in ("Optl", "Optimal") else "warning",
            )

    state["disk_states"] = cur_disks
    state["vd_states"] = cur_vds
    _save_state(state)


def check_smart_attr_changes(smart_data: list[dict]):
    """SMART 采集后调用：关键属性 (5/187/188/197/198 等) 增长告警"""
    state = _load_state()
    prev = state.get("smart_attrs", {})
    cur = {}
    watch = [
        "reallocated",
        "pending",
        "uncorrectable",
        "reported_uncorrectable",
        "command_timeout",
    ]
    for s in smart_data or []:
        did = str(s.get("did", ""))
        if not did:
            continue
        cur[did] = {k: int(s.get(k) or 0) for k in watch}
        old = prev.get(did)
        if old:
            grown = [k for k in watch if cur[did][k] > int(old.get(k) or 0)]
            if grown:
                detail = ", ".join(
                    f"{k}: {old.get(k, 0)} → {cur[did][k]}" for k in grown
                )
                _alert(
                    f"磁盘 DID={did} SMART 关键属性增长",
                    f"磁盘 DID={did} SMART 属性变化：{detail}。",
                )
    state["smart_attrs"] = cur
    _save_state(state)
