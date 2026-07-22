#!/usr/bin/env python3
# ================================================
# LSI MegaRAID 每分钟数据采集器
# 通过 cron 每分钟运行，采集磁盘温度/状态/错误计数/SMART等
# 数据写入 $DATA_DIR/YYYY-MM-DD/
# ================================================

from __future__ import annotations

import json
import os
import subprocess
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

# 引入本地邮件报警模块
import lsi_alert

# ---- 配置 ----
PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))

# 优先使用项目目录下的 storcli64，回退到系统默认路径
LOCAL_STORCLI = PROJECT_ROOT / "storcli64"
STORCLI = os.environ.get(
    "STORCLI_PATH",
    str(LOCAL_STORCLI) if LOCAL_STORCLI.exists() else "/usr/local/bin/storcli64",
)
CONTROLLER = os.environ.get("LSI_CONTROLLER", "/c0")

# ---- 工具函数 ----


def run_storcli(cmd: str, timeout: int = 45) -> dict | None:
    full_cmd = f"sudo {STORCLI} {cmd}"
    try:
        result = subprocess.run(
            full_cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout.strip()
        json_start = output.find("{")
        if json_start == -1:
            return None
        return json.loads(output[json_start:])
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(
            f"[{datetime.now():%H:%M:%S}] storcli error: {cmd} — {e}", file=sys.stderr
        )
        return None


def parse_temperature(text: str) -> int | None:
    if not text:
        return None
    match = re.search(r"(\d+)\s*C", str(text))
    return int(match.group(1)) if match else None


def is_success(data: dict | None) -> bool:
    if not data:
        return False
    for ctrl in data.get("Controllers", []):
        if ctrl.get("Command Status", {}).get("Status") == "Success":
            return True
    return False


# ---- 磁盘采集 ----


def collect_disks() -> list[dict]:
    data = run_storcli(f"{CONTROLLER}/eall/sall show all J")
    if not is_success(data):
        return []

    disks = []
    try:
        resp = data["Controllers"][0].get("Response Data", {})

        # 第一遍：收集摘要
        summaries: dict[str, dict] = {}
        for key, val in resp.items():
            if key.startswith("Drive /c") and isinstance(val, list) and len(val) > 0:
                if isinstance(val[0], dict):
                    summaries[key] = val[0]

        # 第二遍：处理详细条目
        for key, detail in resp.items():
            m = re.match(r"Drive /c\d+/e(\d+)/s(\d+)", key)
            if not m:
                continue
            if not isinstance(detail, dict):
                continue

            eid = int(m.group(1))
            slot = int(m.group(2))
            summary_key = f"Drive /c0/e{eid}/s{slot}"
            summary = summaries.get(summary_key, {})

            state_key = f"Drive /c0/e{eid}/s{slot} State"
            state = detail.get(state_key, {})

            temp = parse_temperature(state.get("Drive Temperature", ""))
            if temp is None:
                temp = _find_temperature(detail)

            disks.append(
                {
                    "eid": eid,
                    "slot": slot,
                    "did": summary.get("DID", ""),
                    "dg": summary.get("DG", ""),
                    "model": summary.get("Model", "").strip(),
                    "state": summary.get("State", "").strip(),
                    "size": summary.get("Size", "").strip(),
                    "intf": summary.get("Intf", "").strip(),
                    "med": summary.get("Med", "").strip(),
                    "temperature": temp if temp is not None else "",
                    "media_error": _to_int(state.get("Media Error Count", 0)),
                    "other_error": _to_int(state.get("Other Error Count", 0)),
                    "predictive_failure": _to_int(
                        state.get("Predictive Failure Count", 0)
                    ),
                    "smart_alert": str(
                        state.get("S.M.A.R.T alert flagged by drive", "No")
                    ).strip(),
                    "shield_counter": _to_int(state.get("Shield Counter", 0)),
                }
            )

    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] disk parse error: {e}", file=sys.stderr)

    return disks


def _to_int(val) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _find_temperature(obj, depth: int = 0) -> int | None:
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "temperature" in k.lower() and isinstance(v, str):
                t = parse_temperature(v)
                if t is not None:
                    return t
            result = _find_temperature(v, depth + 1)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_temperature(item, depth + 1)
            if result is not None:
                return result
    return None


# ---- 控制器 / VD / BBU ----


def collect_controller() -> dict | None:
    data = run_storcli(f"{CONTROLLER} show J")
    if not is_success(data):
        return None

    try:
        resp = data["Controllers"][0].get("Response Data", {})

        model = resp.get("Product Name", "").strip()
        fw_version = resp.get("FW Version", "").strip()

        vd_list = resp.get("VD LIST", [])
        num_vds = resp.get("Virtual Drives", len(vd_list))
        num_disks = resp.get("Physical Drives", len(resp.get("PD LIST", [])))

        health = "Optimal"
        vd_states = []
        for vd in vd_list:
            state = vd.get("State", "Optl")
            vd_states.append(state)
            if state not in ("Optl", "Optimal", "Opt"):
                health = state

        cv_list = resp.get("Cachevault_Info", [])
        bbu_temp_val = None
        bbu_state = ""
        bbu_model = ""
        if cv_list:
            cv = cv_list[0]
            bbu_model = cv.get("Model", "").strip()
            bbu_state = cv.get("State", "").strip()
            bbu_temp_val = parse_temperature(cv.get("Temp", ""))

        return {
            "model": model,
            "fw_version": fw_version,
            "health": health,
            "num_vds": num_vds,
            "num_disks": num_disks,
            "bbu_model": bbu_model,
            "bbu_state": bbu_state,
            "bbu_temperature": bbu_temp_val if bbu_temp_val is not None else "",
            "vd_states": "|".join(vd_states),
        }
    except Exception as e:
        print(
            f"[{datetime.now():%H:%M:%S}] controller parse error: {e}", file=sys.stderr
        )
    return None


def collect_vds() -> list[dict]:
    data = run_storcli(f"{CONTROLLER} show J")
    if not is_success(data):
        return []
    try:
        vd_list = data["Controllers"][0].get("Response Data", {}).get("VD LIST", [])
        return [
            {
                "dg_vd": vd.get("DG/VD", ""),
                "type": vd.get("TYPE", ""),
                "state": vd.get("State", ""),
                "size": vd.get("Size", ""),
                "name": vd.get("Name", ""),
            }
            for vd in vd_list
        ]
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] VD parse error: {e}", file=sys.stderr)
    return []


# ---- 磁盘详细属性 ----


def collect_disk_attributes() -> list[dict]:
    data = run_storcli(f"{CONTROLLER}/eall/sall show all J")
    if not is_success(data):
        return []

    result = []
    try:
        resp = data["Controllers"][0].get("Response Data", {})

        summaries: dict[str, dict] = {}
        for key, val in resp.items():
            if key.startswith("Drive /c") and isinstance(val, list) and len(val) > 0:
                if isinstance(val[0], dict):
                    summaries[key] = val[0]

        for key, detail in resp.items():
            m = re.match(r"Drive /c\d+/e(\d+)/s(\d+)", key)
            if not m or not isinstance(detail, dict):
                continue
            eid = int(m.group(1))
            slot = int(m.group(2))
            summary_key = f"Drive /c0/e{eid}/s{slot}"
            summary = summaries.get(summary_key, {})

            sn = ""
            fw_rev = ""
            dev_speed = ""
            link_speed = ""

            for sub_val in detail.values():
                if not isinstance(sub_val, dict):
                    continue
                sn = sn or str(sub_val.get("SN", "")).strip()
                fw_rev = fw_rev or str(sub_val.get("Firmware Revision", "")).strip()
                dev_speed = dev_speed or str(sub_val.get("Device Speed", "")).strip()
                link_speed = link_speed or str(sub_val.get("Link Speed", "")).strip()

            result.append(
                {
                    "eid": eid,
                    "slot": slot,
                    "did": summary.get("DID", ""),
                    "sn": sn,
                    "fw_rev": fw_rev,
                    "dev_speed": dev_speed,
                    "link_speed": link_speed,
                }
            )

    except Exception as e:
        print(
            f"[{datetime.now():%H:%M:%S}] attribute parse error: {e}", file=sys.stderr
        )

    return result


# ---- SMART 数据 (smartctl) ----

SMARTCTL = os.environ.get("SMARTCTL_PATH", "/usr/sbin/smartctl")


def collect_smart(dids: list[int]) -> list[dict]:
    """通过 smartctl 采集 SMART 属性，兼容 SATA (ATA) 和 SAS (SCSI) 磁盘"""
    result = []
    for did in dids:
        try:
            cmd = f"sudo {SMARTCTL} -a -d megaraid,{did} /dev/sda"
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=20
            )
            output = proc.stdout

            smart = {
                "did": did,
                "reallocated": 0,
                "pending": 0,
                "uncorrectable": 0,
                "reported_uncorrectable": 0,
                "command_timeout": 0,
                "power_on_hours": 0,
                "smart_temp": None,
            }

            # SAS (SCSI) 路径
            for line in output.split("\n"):
                uline = line.upper()
                if "ELEMENTS IN GROWN DEFECT LIST" in uline:
                    m = re.search(r":\s*(\d+)", line)
                    if m:
                        smart["reallocated"] = int(m.group(1))
                elif "NON-MEDIUM ERROR COUNT" in uline:
                    m = re.search(r":\s*(\d+)", line)
                    if m:
                        smart["uncorrectable"] = int(m.group(1))
                elif "NUMBER OF HOURS POWERED UP" in uline:
                    m = re.search(r"(\d+(?:\.\d+)?)\s*$", line.strip())
                    if m:
                        smart["power_on_hours"] = int(float(m.group(1)))
                elif "CURRENT DRIVE TEMPERATURE" in uline:
                    m = re.search(r"(\d+)\s*C", line)
                    if m:
                        smart["smart_temp"] = int(m.group(1))

            # ATA 属性表路径（fallback）
            if smart["power_on_hours"] == 0:
                _parse_ata_table(output, smart)

            result.append(smart)

        except Exception as e:
            print(
                f"[{datetime.now():%H:%M:%S}] smartctl DID={did} error: {e}",
                file=sys.stderr,
            )
            result.append({"did": did, "reallocated": -1})

    return result


def _parse_ata_table(output: str, smart: dict):
    in_table = False
    for line in output.split("\n"):
        if "ID#" in line and "ATTRIBUTE_NAME" in line:
            in_table = True
            continue
        if in_table and line.strip() == "":
            in_table = False
            continue
        if not in_table:
            continue
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            aid = int(parts[0])
            raw = int(parts[-1])
        except ValueError:
            continue
        if aid == 5:
            smart["reallocated"] = max(smart.get("reallocated", 0), raw)
        elif aid == 187:
            smart["reported_uncorrectable"] = raw
        elif aid == 188:
            smart["command_timeout"] = raw
        elif aid == 197:
            smart["pending"] = raw
        elif aid == 198:
            smart["uncorrectable"] = max(smart.get("uncorrectable", 0), raw)
        elif aid == 9:
            smart["power_on_hours"] = max(smart.get("power_on_hours", 0), raw)
        elif aid == 194 and smart["smart_temp"] is None:
            smart["smart_temp"] = raw


# ---- 系统信息 ----


def collect_system_info() -> dict:
    info = {}
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().strip().split()
            info["load_1m"] = float(parts[0])
            info["load_5m"] = float(parts[1])
            info["load_15m"] = float(parts[2])
    except Exception:
        info["load_1m"] = info["load_5m"] = info["load_15m"] = -1

    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    info["mem_total_kb"] = int(line.split()[1])
                elif "MemAvailable" in line:
                    info["mem_avail_kb"] = int(line.split()[1])
    except Exception:
        info["mem_total_kb"] = info["mem_avail_kb"] = -1

    return info


# ---- 巡读 / 一致性检查 ----


def _parse_props(props_list: list[dict]) -> dict:
    result = {}
    for item in props_list:
        result[item.get("Ctrl_Prop", "")] = item.get("Value", "")
    return result


def collect_patrol_read() -> dict | None:
    data = run_storcli(f"{CONTROLLER} show patrolread J")
    if not is_success(data):
        return None
    try:
        props = (
            data["Controllers"][0]
            .get("Response Data", {})
            .get("Controller Properties", [])
        )
        p = _parse_props(props)
        return {
            "pr_mode": p.get("PR Mode", ""),
            "pr_delay": p.get("PR Execution Delay", ""),
            "pr_iterations": _to_int(p.get("PR iterations completed", 0)),
            "pr_next": p.get("PR Next Start time", ""),
            "pr_state": p.get("PR Current State", ""),
            "pr_max_concurrent": _to_int(p.get("PR MaxConcurrentPd", 1)),
        }
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] patrol read error: {e}", file=sys.stderr)
    return None


def collect_consistency_check() -> dict | None:
    data = run_storcli(f"{CONTROLLER} show cc J")
    if not is_success(data):
        return None
    try:
        props = (
            data["Controllers"][0]
            .get("Response Data", {})
            .get("Controller Properties", [])
        )
        p = _parse_props(props)
        return {
            "cc_mode": p.get("CC Operation Mode", ""),
            "cc_delay": p.get("CC Execution Delay", ""),
            "cc_next": p.get("CC Next Starttime", ""),
            "cc_state": p.get("CC Current State", ""),
            "cc_iterations": _to_int(p.get("CC Number of iterations", 0)),
            "cc_vd_completed": _to_int(p.get("CC Number of VD completed", 0)),
        }
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] CC error: {e}", file=sys.stderr)
    return None


# ---- CSV 写入 ----


def write_csv(dir_path: Path, filename: str, fieldnames: list[str], rows: list[dict]):
    dir_path.mkdir(parents=True, exist_ok=True)
    filepath = dir_path / filename
    file_exists = filepath.exists() and filepath.stat().st_size > 0
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_csv_once(
    dir_path: Path, filename: str, fieldnames: list[str], rows: list[dict]
):
    dir_path.mkdir(parents=True, exist_ok=True)
    filepath = dir_path / filename
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---- 主入口 ----


def main():
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    date_dir = BASE_DIR / now.strftime("%Y-%m-%d")
    minute = now.minute

    disk_fields = [
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
    ctrl_fields = [
        "timestamp",
        "model",
        "fw_version",
        "health",
        "num_vds",
        "num_disks",
        "bbu_model",
        "bbu_state",
        "bbu_temperature",
        "vd_states",
    ]
    vd_fields = ["timestamp", "dg_vd", "type", "state", "size", "name"]
    patrol_fields = [
        "timestamp",
        "pr_mode",
        "pr_delay",
        "pr_iterations",
        "pr_next",
        "pr_state",
        "pr_max_concurrent",
    ]
    cc_fields = [
        "timestamp",
        "cc_mode",
        "cc_delay",
        "cc_next",
        "cc_state",
        "cc_iterations",
        "cc_vd_completed",
    ]
    smart_fields = [
        "timestamp",
        "did",
        "reallocated",
        "pending",
        "uncorrectable",
        "reported_uncorrectable",
        "command_timeout",
        "power_on_hours",
        "smart_temp",
    ]
    attr_fields = [
        "timestamp",
        "eid",
        "slot",
        "did",
        "sn",
        "fw_rev",
        "dev_speed",
        "link_speed",
    ]
    sys_fields = [
        "timestamp",
        "load_1m",
        "load_5m",
        "load_15m",
        "mem_total_kb",
        "mem_avail_kb",
    ]

    # 磁盘（每分钟追加）
    disks = collect_disks()
    if disks:
        for d in disks:
            d["timestamp"] = timestamp
        write_csv(date_dir, "disks.csv", disk_fields, disks)
    else:
        write_csv(
            date_dir,
            "disks.csv",
            disk_fields,
            [{"timestamp": timestamp, "state": "N/A"}],
        )

    # 控制器（每分钟追加）
    ctrl = collect_controller()
    if ctrl:
        ctrl["timestamp"] = timestamp
    else:
        ctrl = {"timestamp": timestamp, "health": "N/A"}
    write_csv(date_dir, "controller.csv", ctrl_fields, [ctrl])

    # 故障邮件报警（基于本次采集结果）
    lsi_alert.check_and_alert(disks, ctrl)

    # 系统信息（每分钟追加）
    sys_info = collect_system_info()
    sys_info["timestamp"] = timestamp
    write_csv(date_dir, "system.csv", sys_fields, [sys_info])

    # VD（CSV 当天首次采集写入；状态变化告警每分钟检测）
    vds = collect_vds()
    if vds and not (date_dir / "vds.csv").exists():
        for v in vds:
            v["timestamp"] = timestamp
        write_csv_once(date_dir, "vds.csv", vd_fields, vds)

    # 磁盘状态变化 / VD 变化邮件告警（基于本次采集结果）
    lsi_alert.check_state_changes(disks, vds)

    # 磁盘属性（当天首次采集）
    if not (date_dir / "attributes.csv").exists():
        attrs = collect_disk_attributes()
        if attrs:
            for a in attrs:
                a["timestamp"] = timestamp
            write_csv_once(date_dir, "attributes.csv", attr_fields, attrs)

    # SMART（每 15 分钟采集一次）
    if minute % 15 == 0 and disks:
        dids = sorted(
            set(
                int(d["did"]) for d in disks if d.get("did") not in (None, "")
            )
        )
        if dids:
            smart_data = collect_smart(dids)
            for s in smart_data:
                s["timestamp"] = timestamp
            write_csv_once(date_dir, "smart.csv", smart_fields, smart_data)
            # SMART 关键属性 (5/187/188/197/198) 数值变化邮件告警
            lsi_alert.check_smart_attr_changes(smart_data)

    # 巡读 / CC（每次覆盖写入）
    pr = collect_patrol_read()
    if pr:
        pr["timestamp"] = timestamp
        write_csv_once(date_dir, "patrol.csv", patrol_fields, [pr])

    cc = collect_consistency_check()
    if cc:
        cc["timestamp"] = timestamp
        write_csv_once(date_dir, "consistency.csv", cc_fields, [cc])


if __name__ == "__main__":
    main()
