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
from urllib import request as urlrequest

import alert_center

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))

CONFIG_FILE = BASE_DIR / "alert_config.json"
STATE_FILE = BASE_DIR / ".alert_state.json"
EVENTS_FILE = BASE_DIR / "events.jsonl"

DEFAULT_CONFIG = {
    "alert_email_to": "",
    "sendmail_path": "/usr/sbin/sendmail",
    "webhook_url": "",
    "temp_warn": 45,
    "temp_crit": 55,
    # 告警持续未恢复时每隔多少分钟再提醒一次；0 = 不重复提醒
    "escalate_minutes": 0,
    "policies": {},
}

# 报警策略开关：哪些情况发送邮件（Web 端可配置，默认全开）
DEFAULT_POLICIES = {
    "temp_warn": True,          # 磁盘温度超过警告阈值
    "temp_crit": True,          # 磁盘温度超过临界阈值
    "smart_alert": True,        # 磁盘 SMART 告警
    "predictive_failure": True,  # 预测性故障（PF）计数 > 0
    "ctrl_health": True,        # 控制器健康异常
    "bbu_state": True,          # BBU/缓存单元异常
    "disk_state_change": True,  # 磁盘状态变化
    "vd_state_change": True,    # 虚拟磁盘状态变化
    "smart_attr_growth": True,  # SMART 关键属性增长
    "ctrl_consistency": True,   # 跨控制器固件/型号不一致
    "config_drift": True,       # 控制器配置发生变更
}

# 环境变量 -> 配置字段
ENV_OVERRIDES = {
    "alert_email_to": "ALERT_EMAIL_TO",
    "sendmail_path": "SENDMAIL_PATH",
    "webhook_url": "WEBHOOK_URL",
    "temp_warn": "ALERT_TEMP_WARN",
    "temp_crit": "ALERT_TEMP_CRIT",
    "escalate_minutes": "ALERT_ESCALATE_MINUTES",
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


def effective_policies(cfg: dict | None = None) -> dict:
    """合并默认策略与已保存配置，未知键忽略、缺失键默认开启"""
    cfg = cfg or load_config()
    pol = dict(DEFAULT_POLICIES)
    saved = cfg.get("policies")
    if isinstance(saved, dict):
        for k in DEFAULT_POLICIES:
            if k in saved:
                pol[k] = bool(saved[k])
    return pol


def policy_enabled(cfg: dict, name: str) -> bool:
    return effective_policies(cfg).get(name, True)


def locked_fields() -> dict:
    return {key: is_locked(key) for key in DEFAULT_CONFIG}


def sendmail_available(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    path = cfg.get("sendmail_path") or ""
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def alert_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    return bool(str(cfg.get("alert_email_to", "")).strip())


def webhook_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    return bool(str(cfg.get("webhook_url", "")).strip())


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
    # 用 EmailMessage 构建标准 MIME 邮件：主题按 RFC 2047 编码、正文自动
    # Content-Transfer-Encoding，避免原始 UTF-8 头部触发 SMTPUTF8 要求而被
    # 对端邮件服务器退信（dsn 5.6.7）
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = f"lsi-raid-monitor@{host}"
    m["To"] = ", ".join(recipients)
    m["Subject"] = f"[DataMax-LSI] {subject}"
    m.set_content(body)
    msg = m.as_string()
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


def send_webhook(subject: str, body: str, level: str = "error", cfg: dict | None = None) -> tuple[bool, str]:
    """向通用 Webhook 地址 POST 一条 JSON 报警，兼容钉钉/飞书/企业微信的自定义机器人
    请使用它们提供的「关键字/加签」地址以通过安全校验。"""
    cfg = cfg or load_config()
    url = str(cfg.get("webhook_url", "")).strip()
    if not url:
        return False, "未配置 Webhook 地址"
    payload = {
        "source": "lsi-raid-monitor",
        "host": socket.gethostname(),
        "level": level,
        "subject": subject,
        "message": body,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    req = urlrequest.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            status = resp.status
            resp.read(64)
        if 200 <= status < 300:
            return True, "发送成功"
        return False, f"Webhook 返回 HTTP {status}"
    except Exception as e:
        return False, str(e)


def _alert(subject: str, body: str, level: str = "error"):
    """记录事件并按配置发送邮件与 Webhook"""
    log_event(level, f"{subject} — {body.splitlines()[0] if body else ''}")
    cfg = load_config()
    if alert_enabled(cfg):
        ok, err = send_mail(subject, body, cfg)
        if not ok:
            log_event("warning", f"报警邮件发送失败: {err}")
    if webhook_enabled(cfg):
        ok, err = send_webhook(subject, body, level, cfg)
        if not ok:
            log_event("warning", f"报警 Webhook 发送失败: {err}")


def _flag(
    key: str,
    subject: str,
    body: str,
    level: str = "error",
    escalate_minutes: int = 0,
):
    """登记告警并按需通知。是否通知由告警中心判定（新告警 / 升级 / 静默）。"""
    alert, should_notify, silence = alert_center.raise_alert(
        key, level, subject, body, escalate_minutes=escalate_minutes
    )
    if should_notify:
        _alert(subject, body, level)
        alert_center.mark_notified(key)
    elif silence:
        # 静默窗口内照常记录，但不打扰人——"静默"不等于"假装没发生"
        log_event("info", f"[维护窗口静默] {subject}")
    return alert


def _clear(key: str):
    """条件消失：置为已恢复并发出恢复通知。"""
    prev = alert_center.resolve_alert(key)
    if prev:
        title = prev.get("title") or key
        _alert(
            f"已恢复: {title}",
            f"「{title}」于 {prev.get('first_seen', '')} 触发，现已恢复正常。",
            "info",
        )
    return prev


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


def _disk_key(d: dict) -> str:
    """磁盘唯一键。

    跨控制器必须带控制器编号：/c0/e252/s0 与 /c1/e252/s0 的槽位号会撞车，
    只用 E:S 做键会让状态变化告警张冠李戴。
    """
    ctrl = d.get("ctrl", "")
    prefix = f"C{ctrl}:" if ctrl not in ("", None) else ""
    return f"{prefix}E{d.get('eid')}:S{d.get('slot')}"


def _as_ctrl_list(ctrls) -> list[dict]:
    """兼容单控制器（dict）与多控制器（list）两种入参。"""
    if isinstance(ctrls, dict):
        return [ctrls] if ctrls else []
    return [c for c in (ctrls or []) if c]


def check_and_alert(disks: list[dict], ctrls):
    """每分钟调用：温度阈值 / SMART 告警 / 预测性故障 / 控制器与 BBU 健康"""
    cfg = load_config()
    temp_warn = int(cfg.get("temp_warn", 45))
    temp_crit = int(cfg.get("temp_crit", 55))
    state = _load_state()
    flagged = state.setdefault("flagged", {})

    escalate_minutes = int(cfg.get("escalate_minutes", 0) or 0)

    def flag_once(key: str, subject: str, body: str, level: str = "error"):
        """登记告警；是否需要通知由告警中心判定（新告警 / 升级 / 静默）。"""
        flagged[key] = True
        _flag(key, subject, body, level, escalate_minutes)

    def unflag(key: str):
        """条件消失：把告警置为已恢复，并发出恢复通知。"""
        flagged.pop(key, None)
        _clear(key)

    for d in disks or []:
        label = _disk_key(d)
        temp = d.get("temperature")
        # 告警 key 用层级结构：前缀即作用域，维护窗口可按盘 / 按卡前缀静默
        k_temp_crit = f"disk:{label}:temp_crit"
        k_temp_warn = f"disk:{label}:temp_warn"
        k_smart = f"disk:{label}:smart"
        k_pf = f"disk:{label}:pf"
        # 策略关闭时清除残留标记，重新启用后可按当前状态重新报警
        if not policy_enabled(cfg, "temp_crit"):
            unflag(k_temp_crit)
        if not policy_enabled(cfg, "temp_warn"):
            unflag(k_temp_warn)
        if isinstance(temp, int):
            if temp >= temp_crit and policy_enabled(cfg, "temp_crit"):
                flag_once(
                    k_temp_crit,
                    f"磁盘 {label} 温度临界 {temp}°C",
                    f"磁盘 {label} ({d.get('model', '')}) 温度 {temp}°C，超过临界阈值 {temp_crit}°C。",
                )
            elif temp >= temp_warn and policy_enabled(cfg, "temp_warn"):
                flag_once(
                    k_temp_warn,
                    f"磁盘 {label} 温度偏高 {temp}°C",
                    f"磁盘 {label} ({d.get('model', '')}) 温度 {temp}°C，超过警告阈值 {temp_warn}°C。",
                    "warning",
                )
                unflag(k_temp_crit)
            else:
                unflag(k_temp_warn)
                unflag(k_temp_crit)

        if not policy_enabled(cfg, "smart_alert"):
            unflag(k_smart)
        elif str(d.get("smart_alert", "")).strip() == "Yes":
            flag_once(
                k_smart,
                f"磁盘 {label} SMART 告警",
                f"磁盘 {label} ({d.get('model', '')}) SMART alert flagged by drive。",
            )
        else:
            unflag(k_smart)

        if not policy_enabled(cfg, "predictive_failure"):
            unflag(k_pf)
        else:
            try:
                pf = int(d.get("predictive_failure") or 0)
            except (ValueError, TypeError):
                pf = 0
            if pf > 0:
                flag_once(
                    k_pf,
                    f"磁盘 {label} 预测性故障计数 {pf}",
                    f"磁盘 {label} ({d.get('model', '')}) Predictive Failure Count = {pf}。",
                )
            else:
                unflag(k_pf)

    # 控制器健康 / BBU：每张卡独立判定与独立标记，多卡机器上
    # 一张卡正常不能掩盖另一张卡的异常。
    for ctrl in _as_ctrl_list(ctrls):
        cid = ctrl.get("ctrl", "")
        cid_key = cid if cid not in ("", None) else 0
        tag = f"C{cid}" if cid not in ("", None) else "控制器"
        k_health = f"controller:{cid_key}:health"
        k_bbu = f"controller:{cid_key}:bbu"

        health = str(ctrl.get("health", "")).strip()
        if not policy_enabled(cfg, "ctrl_health"):
            unflag(k_health)
        elif health and health not in ("Optimal", "Opt", "N/A"):
            flag_once(
                k_health,
                f"{tag} 健康异常: {health}",
                f"{tag} {ctrl.get('model', '')} 健康状态为 {health}。",
            )
        else:
            unflag(k_health)

        bbu = str(ctrl.get("bbu_state", "")).strip()
        if not policy_enabled(cfg, "bbu_state"):
            unflag(k_bbu)
        elif bbu and bbu not in ("Optimal", "Opt", "OK"):
            flag_once(
                k_bbu,
                f"{tag} BBU/缓存单元异常: {bbu}",
                f"{tag} BBU {ctrl.get('bbu_model', '')} 状态为 {bbu}。",
            )
        else:
            unflag(k_bbu)

    _save_state(state)


def check_consistency(ctrls) -> list[dict]:
    """跨控制器一致性检查：固件版本、型号是否统一。

    多卡机器上固件不一致会显著增加故障排查与厂商支持的成本，但这个问题
    不属于任何一块盘——只放在页面上展示的话，不打开页面就永远不会知道。
    所以由采集侧主动检查并纳入告警闭环。
    """
    cfg = load_config()
    lists = _as_ctrl_list(ctrls)
    if len(lists) < 2:
        # 控制器数量降到 1 张（例如换卡后只剩一张）时，之前基于多卡判定出的
        # 一致性问题已经不存在，必须清掉——否则那条"固件版本不一致"会永久
        # 挂在页面上，而它描述的两张卡早已不在同一台机器里。
        for key in ("system:consistency:fw_mismatch", "system:consistency:model_mixed"):
            _clear(key)
        return []

    def _values(*fields: str) -> dict:
        """按候选字段名取值。采集器输出的是 fw_version，而 build_status
        对外暴露的是 fw——两处字段名不同，只认一个会静默取不到值。"""
        out = {}
        for c in lists:
            v = ""
            for f in fields:
                v = str(c.get(f) or "").strip()
                if v:
                    break
            if v:
                cid = c.get("ctrl", "")
                out[cid if cid not in ("", None) else 0] = v
        return out

    issues = []
    fws = _values("fw", "fw_version")
    if len(set(fws.values())) > 1:
        detail = "、".join(f"C{k} = {v}" for k, v in sorted(fws.items(), key=lambda x: str(x[0])))
        issues.append(
            {
                "key": "system:consistency:fw_mismatch",
                "level": "warning",
                "title": "控制器固件版本不一致",
                "detail": detail,
                "hint": "固件不一致会让故障排查与厂商支持变复杂，建议统一到同一版本。",
            }
        )

    models = _values("model")
    if len(set(models.values())) > 1:
        detail = "、".join(f"C{k} = {v}" for k, v in sorted(models.items(), key=lambda x: str(x[0])))
        issues.append(
            {
                "key": "system:consistency:model_mixed",
                "level": "info",
                "title": "控制器型号不一致",
                "detail": detail,
                "hint": "混合型号在扩容与故障替换时需要额外确认兼容性。",
            }
        )

    seen = set()
    escalate = int(cfg.get("escalate_minutes", 0) or 0)
    if policy_enabled(cfg, "ctrl_consistency"):
        for it in issues:
            _flag(
                it["key"],
                it["title"],
                f"{it['detail']}。{it['hint']}",
                it["level"],
                escalate,
            )
            seen.add(it["key"])

    # 问题消失（或策略被关闭）时清掉残留告警，否则会永远挂在那里
    for key in ("system:consistency:fw_mismatch", "system:consistency:model_mixed"):
        if key not in seen:
            _clear(key)

    return issues


def check_config_drift(ctrl_id: int, change: dict) -> None:
    """控制器配置发生变更时告警。

    这是这套系统里最"值钱"的一类告警：阵列配置不会自己变，变了要么是
    计划内的变更（需要留档），要么是意外或换卡（需要立刻处理）。
    两种情况都需要人马上看到，而不是等下一次出故障时才发现。
    """
    cfg = load_config()
    if not policy_enabled(cfg, "config_drift"):
        _clear(f"controller:{ctrl_id}:config_drift")
        return

    changes = change.get("changes") or []
    if not changes:
        return
    high = [c for c in changes if c.get("impact") == "high"]
    lines = [
        f"{c.get('label')}: {c.get('from')} → {c.get('to')}" for c in changes[:12]
    ]
    detail = "；".join(lines)
    if len(changes) > 12:
        detail += f"（另有 {len(changes) - 12} 项）"
    level = "error" if high else "warning"
    _flag(
        f"controller:{ctrl_id}:config_drift",
        f"C{ctrl_id} 控制器配置发生变更（{len(changes)} 项）",
        f"{detail}。上次快照 {change.get('prev_ts', '')}。"
        f"若为计划内变更请记录；若非预期，请立即核查。",
        level,
        int(cfg.get("escalate_minutes", 0) or 0),
    )


def check_state_changes(disks: list[dict], vds: list[dict]):
    """每分钟调用：磁盘 / VD 状态变化告警"""
    cfg = load_config()
    disk_policy = policy_enabled(cfg, "disk_state_change")
    vd_policy = policy_enabled(cfg, "vd_state_change")
    state = _load_state()
    prev_disks = state.get("disk_states", {})
    prev_vds = state.get("vd_states", {})

    cur_disks = {}
    for d in disks or []:
        label = _disk_key(d)
        st = str(d.get("state", "")).strip()
        if st and st != "N/A":
            cur_disks[label] = st
    for label, st in cur_disks.items():
        old = prev_disks.get(label)
        if disk_policy and old is not None and old != st:
            _alert(
                f"磁盘 {label} 状态变化: {old} → {st}",
                f"磁盘 {label} 状态由 {old} 变为 {st}。",
                "error" if st not in ("Onln", "UGood", "JBOD") else "warning",
            )

    cur_vds = {}
    for v in vds or []:
        key = str(v.get("dg_vd", ""))
        cid = v.get("ctrl", "")
        if cid not in ("", None):
            key = f"C{cid}:{key}"
        st = str(v.get("state", "")).strip()
        if key and st:
            cur_vds[key] = st
    for key, st in cur_vds.items():
        old = prev_vds.get(key)
        if vd_policy and old is not None and old != st:
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
    growth_policy = policy_enabled(load_config(), "smart_attr_growth")
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
        # 带控制器编号做键：不同控制器的 DID 命名空间互不相通，
        # 只用 DID 有可能把两张卡的盘混成同一块。
        cid = s.get("ctrl", "")
        key = f"C{cid}:D{did}" if cid not in ("", None) else f"D{did}"
        cur[key] = {k: int(s.get(k) or 0) for k in watch}
        old = prev.get(key)
        if old:
            grown = [k for k in watch if cur[key][k] > int(old.get(k) or 0)]
            # 策略关闭时仍更新快照，避免重新启用后补报历史增长
            if grown and growth_policy:
                detail = ", ".join(
                    f"{k}: {old.get(k, 0)} → {cur[key][k]}" for k in grown
                )
                _alert(
                    f"磁盘 {key} SMART 关键属性增长",
                    f"磁盘 {key} SMART 属性变化：{detail}。",
                )
    state["smart_attrs"] = cur
    _save_state(state)
