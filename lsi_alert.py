#!/usr/bin/env python3
# ================================================
# LSI MegaRAID 故障邮件报警模块
# 调用服务器本地 sendmail 服务发送即时报警邮件
#
# 支持环境变量或 JSON 配置文件配置报警参数。
# 配置文件路径: $LSI_DATA_DIR/alert_config.json
#
# 环境变量 (优先级高于配置文件):
#   ALERT_EMAIL_TO      报警收件人，多个地址用逗号分隔
#   SENDMAIL_PATH       sendmail 路径 (默认 /usr/sbin/sendmail)
#   TEMP_WARN           温度警告阈值 (默认 45)
#   TEMP_CRIT           温度临界阈值 (默认 50)
#   LSI_DATA_DIR        数据目录 (默认 ./data)
# ================================================

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from email.header import Header
from email.mime.text import MIMEText

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
ALERT_STATE_FILE = BASE_DIR / "alert_state.json"
ALERT_CONFIG_FILE = BASE_DIR / "alert_config.json"

# 默认值
DEFAULT_SENDMAIL_PATH = "/usr/sbin/sendmail"
DEFAULT_TEMP_WARN = 45
DEFAULT_TEMP_CRIT = 50


# ---- 配置读写 ----


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def load_alert_config_file() -> dict:
    if not ALERT_CONFIG_FILE.exists():
        return {}
    try:
        with open(ALERT_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        print(f"[{_ts()}] alert config load error: {e}", file=sys.stderr)
        return {}


def save_alert_config(config: dict):
    """保存报警配置到 JSON 文件。"""
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        with open(ALERT_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[{_ts()}] alert config save error: {e}", file=sys.stderr)
        raise


def get_alert_config() -> dict:
    """
    返回最终生效的报警配置。优先级：环境变量 > 配置文件 > 默认值。
    """
    file_cfg = load_alert_config_file()

    email_to = _env_str("ALERT_EMAIL_TO", file_cfg.get("alert_email_to", ""))
    sendmail = _env_str(
        "SENDMAIL_PATH", file_cfg.get("sendmail_path", DEFAULT_SENDMAIL_PATH)
    )
    temp_warn = _env_int("TEMP_WARN", file_cfg.get("temp_warn", DEFAULT_TEMP_WARN))
    temp_crit = _env_int("TEMP_CRIT", file_cfg.get("temp_crit", DEFAULT_TEMP_CRIT))

    return {
        "alert_email_to": email_to,
        "sendmail_path": sendmail,
        "temp_warn": temp_warn,
        "temp_crit": temp_crit,
    }


# ---- 状态持久化 ----


def load_alert_state() -> dict:
    if not ALERT_STATE_FILE.exists():
        return {}
    try:
        with open(ALERT_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[{_ts()}] alert state load error: {e}", file=sys.stderr)
        return {}


def save_alert_state(state: dict):
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        with open(ALERT_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print(f"[{_ts()}] alert state save error: {e}", file=sys.stderr)


# ---- 邮件发送 ----


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def sendmail_available(sendmail_path: str | None = None) -> bool:
    path = sendmail_path or get_alert_config()["sendmail_path"]
    return shutil.which(path) is not None


def send_alert_email(
    subject: str, body: str, to_addresses: list[str], sendmail_path: str | None = None
) -> bool:
    """使用本地 sendmail 发送 UTF-8 纯文本邮件。"""
    path = sendmail_path or get_alert_config()["sendmail_path"]
    if not sendmail_available(path):
        print(f"[{_ts()}] sendmail not found: {path}", file=sys.stderr)
        return False

    to_header = ", ".join(to_addresses)
    # 用标准 MIME 构造邮件：Subject 按 RFC 2047 编码、正文 base64，
    # 使整封邮件为 7bit ASCII，避免投递时要求 SMTPUTF8 而被中继拒收
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to_header
    msg["Subject"] = Header(subject, "utf-8")
    message = msg.as_string()

    try:
        result = subprocess.run(
            [path, "-t"],
            input=message,
            text=True,
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            print(
                f"[{_ts()}] sendmail failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as e:
        print(f"[{_ts()}] sendmail error: {e}", file=sys.stderr)
        return False


# ---- 异常检测 ----


def _disk_state_ok(state: str | None) -> bool:
    return str(state).strip().lower() in (
        "onln",
        "online",
        "hotspare",
        "ugood",
        "optimal",
        "optl",
        "opt",
    )


def _controller_state_ok(state: str | None) -> bool:
    return str(state).strip().lower() in ("optimal", "optl", "opt")


def detect_issues(
    disks: list[dict], controller: dict | None, cfg: dict | None = None
) -> tuple[list[str], set[str]]:
    """
    返回 (问题描述列表, 当前激活的告警标识集合)。
    告警标识用于状态去重，避免同一故障每分钟重复发邮件。
    """
    cfg = cfg or get_alert_config()
    temp_warn = cfg.get("temp_warn", DEFAULT_TEMP_WARN)
    temp_crit = cfg.get("temp_crit", DEFAULT_TEMP_CRIT)

    issues: list[str] = []
    active_alerts: set[str] = set()

    # 控制器健康
    ctrl_health = controller.get("health", "N/A") if controller else "N/A"
    if not _controller_state_ok(ctrl_health):
        issues.append(f"控制器健康状态异常: {ctrl_health}")
        active_alerts.add("controller")

    for d in disks:
        eid = d.get("eid", "?")
        slot = d.get("slot", "?")
        label = f"E{eid}:S{slot}"

        # 磁盘状态
        state = str(d.get("state", "")).strip()
        if state and not _disk_state_ok(state):
            issues.append(f"物理磁盘 {label} 状态异常: {state}")
            active_alerts.add(f"disk:{eid}:{slot}")

        # 温度
        temp = d.get("temperature")
        if isinstance(temp, int):
            if temp >= temp_crit:
                issues.append(f"物理磁盘 {label} 温度达到临界值: {temp}°C")
                active_alerts.add(f"temp_crit:{eid}:{slot}")
            elif temp >= temp_warn:
                issues.append(f"物理磁盘 {label} 温度超过警告阈值: {temp}°C")
                active_alerts.add(f"temp_warn:{eid}:{slot}")

        # SMART 告警
        if str(d.get("smart_alert", "No")).strip().lower() == "yes":
            issues.append(f"物理磁盘 {label} SMART 告警被标记")
            active_alerts.add(f"smart:{eid}:{slot}")

        # 错误计数器
        me = d.get("media_error", 0) or 0
        oe = d.get("other_error", 0) or 0
        pf = d.get("predictive_failure", 0) or 0
        if me > 0 or oe > 0 or pf > 0:
            issues.append(
                f"物理磁盘 {label} 错误计数异常: "
                f"MediaError={me}, OtherError={oe}, PredictiveFailure={pf}"
            )
            active_alerts.add(f"errors:{eid}:{slot}")

    return issues, active_alerts


def _build_alert_body(
    timestamp: str,
    controller: dict | None,
    disks: list[dict],
    issues: list[str],
    cfg: dict | None = None,
) -> str:
    cfg = cfg or get_alert_config()
    host = os.uname().nodename
    lines = [
        f"LSI RAID 故障报警 — {host}",
        f"时间: {timestamp}",
        "",
        "检测到以下异常:",
    ]
    for issue in issues:
        lines.append(f"  - {issue}")

    lines.append("")
    lines.append(
        f"控制器健康: {controller.get('health', 'N/A') if controller else 'N/A'}"
    )
    if controller:
        lines.append(f"控制器型号: {controller.get('model', '—')}")
        lines.append(f"固件版本: {controller.get('fw_version', '—')}")

    lines.append("")
    lines.append("物理磁盘状态:")
    for d in disks:
        eid = d.get("eid", "?")
        slot = d.get("slot", "?")
        temp = d.get("temperature", "—")
        temp_str = f"{temp}°C" if isinstance(temp, int) else str(temp)
        lines.append(
            f"  E{eid}:S{slot} | {d.get('model', '—')} | "
            f"状态: {d.get('state', '—')} | 温度: {temp_str} | "
            f"ME={d.get('media_error', 0)} OE={d.get('other_error', 0)} "
            f"PF={d.get('predictive_failure', 0)} SMART={d.get('smart_alert', 'No')}"
        )

    lines.append("")
    lines.append(" thresholds:")
    lines.append(
        f"  温度警告: {cfg.get('temp_warn', DEFAULT_TEMP_WARN)}°C, "
        f"温度临界: {cfg.get('temp_crit', DEFAULT_TEMP_CRIT)}°C"
    )
    lines.append("")
    lines.append("本邮件由 lsi-raid-monitor 自动发送。")
    return "\n".join(lines)


# ---- 主入口 ----


def check_and_alert(disks: list[dict], controller: dict | None):
    """
    检查当前磁盘/控制器状态，若存在新的异常则立即发送报警邮件。
    同一异常在恢复前不会重复发送。
    """
    cfg = get_alert_config()
    alert_email_to = cfg.get("alert_email_to", "")
    sendmail_path = cfg.get("sendmail_path", DEFAULT_SENDMAIL_PATH)

    if not alert_email_to:
        return

    if not sendmail_available(sendmail_path):
        print(
            f"[{_ts()}] ALERT_EMAIL_TO is set but sendmail not found: {sendmail_path}",
            file=sys.stderr,
        )
        return

    issues, active_alerts = detect_issues(disks, controller, cfg)

    state = load_alert_state()
    prev_alerts: set[str] = set(state.get("active_alerts", []))

    # 只有当存在异常且异常集合发生变化（新增异常）时才发送邮件
    if not active_alerts:
        save_alert_state(
            {"active_alerts": [], "last_check": datetime.now().isoformat()}
        )
        return

    if active_alerts == prev_alerts:
        # 异常集合没有变化，不重复发邮件；只更新时间戳
        save_alert_state(
            {
                "active_alerts": sorted(active_alerts),
                "last_check": datetime.now().isoformat(),
            }
        )
        return

    # 有新增异常，发送邮件
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host = os.uname().nodename
    subject = f"[LSI RAID ALERT] {host} 检测到 {len(issues)} 项异常"
    body = _build_alert_body(timestamp, controller, disks, issues, cfg)

    recipients = [addr.strip() for addr in alert_email_to.split(",") if addr.strip()]
    if recipients and send_alert_email(subject, body, recipients, sendmail_path):
        print(f"[{_ts()}] Alert email sent to {', '.join(recipients)}")

    save_alert_state(
        {
            "active_alerts": sorted(active_alerts),
            "last_check": datetime.now().isoformat(),
        }
    )


# ---- SMART 关键属性变化监控 ----

# (采集字段名, SMART 属性 ID, 属性说明)
SMART_WATCH_ATTRS = [
    ("reallocated", 5, "Reallocated Sector Count 重映射扇区数"),
    ("reported_uncorrectable", 187, "Reported Uncorrectable Errors 不可恢复错误"),
    ("command_timeout", 188, "Command Timeout 命令超时"),
    ("pending", 197, "Current Pending Sector Count 待映射扇区数"),
    ("uncorrectable", 198, "Offline Uncorrectable 离线不可校正扇区数"),
]


def detect_smart_attr_changes(
    smart_rows: list[dict], prev_values: dict
) -> tuple[list[str], dict]:
    """
    对比上次采集值，返回 (变化描述列表, 本次基线 dict)。
    首次见到的磁盘只建立基线，不告警。
    """
    changes: list[str] = []
    current: dict = {}
    for row in smart_rows:
        did = row.get("did")
        # collect_smart 失败时以 {"did": did, "reallocated": -1} 占位，跳过
        if did is None or row.get("reallocated") == -1:
            continue
        key = str(did)
        cur = {}
        for field, aid, _name in SMART_WATCH_ATTRS:
            value = row.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                cur[str(aid)] = value
        current[key] = cur
        old = prev_values.get(key)
        if not isinstance(old, dict):
            continue
        for _field, aid, name in SMART_WATCH_ATTRS:
            aid_key = str(aid)
            if aid_key in cur and aid_key in old and cur[aid_key] != old[aid_key]:
                changes.append(
                    f"DID {did} SMART 属性 {aid} ({name}): "
                    f"{old[aid_key]} → {cur[aid_key]}"
                )
    return changes, current


def check_smart_attr_changes(smart_rows: list[dict]):
    """
    SMART 关键属性 (5/187/188/197/198) 数值一旦变化即发送邮件告警。
    基线存于 alert_state.json 的 smart_attr_values；发送失败时不更新基线，
    下次采集会重新检测，避免告警丢失。
    """
    cfg = get_alert_config()
    alert_email_to = cfg.get("alert_email_to", "")
    if not alert_email_to:
        return

    state = load_alert_state()
    prev_values = state.get("smart_attr_values") or {}
    changes, current = detect_smart_attr_changes(smart_rows, prev_values)

    if not changes:
        state["smart_attr_values"] = current
        save_alert_state(state)
        return

    sendmail_path = cfg.get("sendmail_path", DEFAULT_SENDMAIL_PATH)
    if not sendmail_available(sendmail_path):
        print(
            f"[{_ts()}] SMART 属性变化但 sendmail 不可用: {sendmail_path}",
            file=sys.stderr,
        )
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host = os.uname().nodename
    subject = f"[LSI RAID ALERT] {host} 检测到 {len(changes)} 项 SMART 属性变化"
    lines = [
        f"LSI RAID SMART 属性变化告警 — {host}",
        f"时间: {timestamp}",
        "",
        "以下 SMART 关键属性数值发生变化（可能预示磁盘劣化）:",
    ]
    lines += [f"  - {c}" for c in changes]
    lines += [
        "",
        "监控属性: 5 (重映射扇区), 187 (不可恢复错误), 188 (命令超时),",
        "          197 (待映射扇区), 198 (离线不可校正)",
        "",
        "本邮件由 lsi-raid-monitor 自动发送。",
    ]

    recipients = [addr.strip() for addr in alert_email_to.split(",") if addr.strip()]
    if recipients and send_alert_email(subject, "\n".join(lines), recipients, sendmail_path):
        print(f"[{_ts()}] SMART attr change alert sent to {', '.join(recipients)}")
        state["smart_attr_values"] = current
        save_alert_state(state)


# ---- 磁盘 / VD 状态变化监控 ----


def detect_state_changes(
    disks: list[dict],
    vds: list[dict],
    prev_disk_states: dict | None,
    prev_vd_states: dict | None,
) -> tuple[list[str], dict, dict]:
    """
    对比上次采集快照，返回 (变化描述列表, 当前磁盘快照, 当前VD快照)。
    首次采集（prev 为 None）只建立基线，不告警。
    本次采集为空的一侧（storcli 失败）不参与对比，也不覆盖基线。
    """
    cur_disks: dict = {}
    for d in disks or []:
        eid, slot = d.get("eid"), d.get("slot")
        if eid is None or slot is None:
            continue
        cur_disks[f"E{eid}:S{slot}"] = str(d.get("state", "")).strip() or "Unknown"

    cur_vds: dict = {}
    for v in vds or []:
        key = str(v.get("dg_vd", "")).strip()
        if key:
            cur_vds[key] = str(v.get("state", "")).strip() or "Unknown"

    changes: list[str] = []

    if prev_disk_states is not None and cur_disks:
        for label, st in cur_disks.items():
            if label not in prev_disk_states:
                changes.append(f"新增物理磁盘 {label}，当前状态: {st}")
            elif prev_disk_states[label] != st:
                changes.append(
                    f"物理磁盘 {label} 状态变化: {prev_disk_states[label]} → {st}"
                )
        for label, st in prev_disk_states.items():
            if label not in cur_disks:
                changes.append(f"物理磁盘 {label} 已移除（此前状态: {st}）")

    if prev_vd_states is not None and cur_vds:
        for key, st in cur_vds.items():
            if key not in prev_vd_states:
                changes.append(f"新增虚拟磁盘 VD {key}，当前状态: {st}")
            elif prev_vd_states[key] != st:
                changes.append(
                    f"虚拟磁盘 VD {key} 状态变化: {prev_vd_states[key]} → {st}"
                )
        for key, st in prev_vd_states.items():
            if key not in cur_vds:
                changes.append(f"虚拟磁盘 VD {key} 已移除（此前状态: {st}）")

    return changes, cur_disks, cur_vds


def check_state_changes(disks: list[dict], vds: list[dict]):
    """
    磁盘状态变化（Onln→Failed 等迁移、增删盘）与 VD 变化邮件告警。
    基线存于 alert_state.json 的 disk_states / vd_states；首次采集只建立
    基线不告警；发送失败时不更新基线，下次采集重新检测，避免告警丢失。
    """
    cfg = get_alert_config()
    alert_email_to = cfg.get("alert_email_to", "")
    if not alert_email_to:
        return

    state = load_alert_state()
    prev_disks = state.get("disk_states")
    prev_vds = state.get("vd_states")
    changes, cur_disks, cur_vds = detect_state_changes(
        disks, vds, prev_disks, prev_vds
    )

    if not changes:
        # 无变化：更新基线（采集为空的一侧保留旧基线）
        if cur_disks:
            state["disk_states"] = cur_disks
        if cur_vds:
            state["vd_states"] = cur_vds
        save_alert_state(state)
        return

    sendmail_path = cfg.get("sendmail_path", DEFAULT_SENDMAIL_PATH)
    if not sendmail_available(sendmail_path):
        print(
            f"[{_ts()}] 磁盘/VD 状态变化但 sendmail 不可用: {sendmail_path}",
            file=sys.stderr,
        )
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    host = os.uname().nodename
    subject = f"[LSI RAID ALERT] {host} 检测到 {len(changes)} 项磁盘/VD 状态变化"
    lines = [
        f"LSI RAID 磁盘/VD 状态变化告警 — {host}",
        f"时间: {timestamp}",
        "",
        "检测到以下磁盘 / 虚拟磁盘（VD）变化:",
    ]
    lines += [f"  - {c}" for c in changes]
    lines += [
        "",
        "当前物理磁盘状态:",
    ]
    for label in sorted(cur_disks):
        lines.append(f"  {label} | 状态: {cur_disks[label]}")
    if cur_vds:
        lines += ["", "当前虚拟磁盘（VD）状态:"]
        for key in sorted(cur_vds):
            lines.append(f"  VD {key} | 状态: {cur_vds[key]}")
    lines += [
        "",
        "本邮件由 lsi-raid-monitor 自动发送。",
    ]

    recipients = [addr.strip() for addr in alert_email_to.split(",") if addr.strip()]
    if recipients and send_alert_email(
        subject, "\n".join(lines), recipients, sendmail_path
    ):
        print(f"[{_ts()}] Disk/VD state change alert sent to {', '.join(recipients)}")
        if cur_disks:
            state["disk_states"] = cur_disks
        if cur_vds:
            state["vd_states"] = cur_vds
        save_alert_state(state)


if __name__ == "__main__":
    # 简单自测：读取环境变量并尝试发送一封测试邮件
    cfg = get_alert_config()
    test_to = cfg.get("alert_email_to", "")
    if not test_to:
        print("Usage: ALERT_EMAIL_TO=you@example.com python3 lsi_alert.py")
        sys.exit(1)
    host = os.uname().nodename
    ok = send_alert_email(
        f"[LSI RAID ALERT TEST] {host}",
        f"这是一封来自 {host} 的 LSI RAID Monitor 报警邮件测试。\n\n"
        "如果收到此邮件，说明本地 sendmail 配置正确。",
        [addr.strip() for addr in test_to.split(",") if addr.strip()],
    )
    sys.exit(0 if ok else 1)
