#!/usr/bin/env python3
# ================================================
# LSI MegaRAID 每分钟数据采集器
# 通过 cron 每分钟运行，采集磁盘温度/状态/错误计数/SMART等
# 数据写入 $DATA_DIR/YYYY-MM-DD/
# ================================================

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# 引入本地邮件报警模块
import lsi_alert
# SQLite 数据底座（与 CSV 双写，供长期趋势查询）
import db
# 控制器配置快照与漂移检测
import config_mgr
# 写缓存与缓存单元保护评估
import battery_mgr

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


# 控制器清单缓存：/api/vd_detail 等接口每次请求都会枚举控制器，
# 不加缓存会对 storcli 造成不必要的调用。
_CTRL_CACHE: dict = {"ts": 0.0, "ids": None}


def _controller_ids_from_overview(data: dict | None) -> list[int]:
    """从 `storcli show J` 的 System Overview 中取出控制器编号。"""
    try:
        rd = data["Controllers"][0].get("Response Data", {})
        ids = [
            int(item["Ctl"])
            for item in rd.get("System Overview", [])
            if isinstance(item, dict) and item.get("Ctl") not in (None, "")
        ]
    except Exception:
        ids = []
    return sorted(set(ids))


def list_controller_ids() -> list[int]:
    """枚举需要采集的 RAID 控制器编号。

    多卡机器上必须遍历全部控制器：只读 /c0 时，若磁盘全部挂在 /c1，页面会
    表现为"整机无盘"——这不是显示问题，而是取数取错了卡。
    显式设置 LSI_CONTROLLER 时退化为单卡模式，便于调试与老部署兼容。
    """
    env = os.environ.get("LSI_CONTROLLER", "").strip()
    if env:
        m = re.search(r"(\d+)", env)
        return [int(m.group(1))] if m else [0]
    now = time.time()
    if _CTRL_CACHE["ids"] is not None and now - _CTRL_CACHE["ts"] < 30:
        return list(_CTRL_CACHE["ids"])
    ids = _controller_ids_from_overview(run_storcli("show J", timeout=30))
    ids = ids or [0]
    _CTRL_CACHE.update(ts=now, ids=ids)
    return list(ids)


def controller_overview() -> dict[int, dict]:
    """控制器编号 -> System Overview 摘要（型号/健康/盘数），用于快速列表。"""
    data = run_storcli("show J", timeout=30)
    out: dict[int, dict] = {}
    try:
        rd = data["Controllers"][0].get("Response Data", {})
        for item in rd.get("System Overview", []):
            if not isinstance(item, dict) or item.get("Ctl") in (None, ""):
                continue
            out[int(item["Ctl"])] = {
                "model": str(item.get("Model", "")).strip(),
                "health": str(item.get("Hlth", "")).strip(),
                "num_disks": _to_int(item.get("PDs"), 0),
                "num_vds": _to_int(item.get("VDs"), 0),
                "bbu": str(item.get("BBU", "")).strip(),
            }
    except Exception:
        pass
    return out


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


def _response_data(data: dict | None) -> dict | None:
    """返回首个含 Response Data 的控制器响应。

    个别盘 Failed/掉线时 storcli 整体 Status 为 Failure（ErrCd 45），
    但其余盘的 Response Data 仍然完整，不应像 is_success 那样整体丢弃。
    """
    if not data:
        return None
    for ctrl in data.get("Controllers", []):
        resp = ctrl.get("Response Data")
        if resp:
            return resp
    return None


# ---- 磁盘采集 ----


def _collect_drive_bg_progress(
    cmd: str, ctrl_id: int = 0
) -> dict[tuple[int, int], dict]:
    """采集指定控制器各物理盘的后台操作进度（rebuild/copyback），返回
    {(eid, slot): {"progress": int|None, "eta": str}}"""
    data = run_storcli(f"/c{ctrl_id}/eall/sall {cmd} J")
    if not is_success(data):
        return {}
    result: dict[tuple[int, int], dict] = {}
    try:
        resp = data["Controllers"][0].get("Response Data", [])
        for item in resp:
            if not isinstance(item, dict):
                continue
            m = re.match(r"/c\d+/e(\d+)/s(\d+)", str(item.get("Drive-ID", "")))
            if not m:
                continue
            if str(item.get("Status", "")).lower() != "in progress":
                continue
            progress = item.get("Progress%")
            try:
                progress = int(progress)
            except (ValueError, TypeError):
                progress = None
            result[(int(m.group(1)), int(m.group(2)))] = {
                "progress": progress,
                "eta": str(item.get("Estimated Time Left", "")).strip(),
            }
    except Exception as e:
        print(
            f"[{datetime.now():%H:%M:%S}] {cmd} parse error: {e}", file=sys.stderr
        )
    return result


def collect_rebuild(ctrl_id: int = 0) -> dict[tuple[int, int], dict]:
    return _collect_drive_bg_progress("show rebuild", ctrl_id)


def collect_copyback(ctrl_id: int = 0) -> dict[tuple[int, int], dict]:
    return _collect_drive_bg_progress("show copyback", ctrl_id)


def collect_erase(ctrl_id: int = 0) -> dict[tuple[int, int], dict]:
    return _collect_drive_bg_progress("show erase", ctrl_id)


def collect_disks(ctrl_id: int = 0) -> list[dict]:
    data = run_storcli(f"/c{ctrl_id}/eall/sall show all J")
    resp = _response_data(data)
    if not resp:
        return []

    rebuild_map = collect_rebuild(ctrl_id)
    copyback_map = collect_copyback(ctrl_id)
    erase_map = collect_erase(ctrl_id)

    disks = []
    # 正在后台操作（erase/rebuild 等）的盘会从 show all 列表中消失，
    # 且仅在 Command Status 的 Detailed Status 里报 "in progress" 错误；
    # 记录下来供 _append_failed_drives 区分"忙"与真正的 Failed。
    busy_drives: set[tuple[int, int]] = set(erase_map) | set(rebuild_map) | set(copyback_map)

    try:
        # 第一遍：收集摘要
        summaries: dict[str, dict] = {}
        for key, val in resp.items():
            if key.startswith("Drive /c") and isinstance(val, list) and len(val) > 0:
                if isinstance(val[0], dict):
                    summaries[key] = val[0]

        # 第二遍：处理详细条目
        for key, detail in resp.items():
            m = re.match(r"Drive /c(\d+)/e(\d+)/s(\d+)", key)
            if not m:
                continue
            if not isinstance(detail, dict):
                continue

            ctrl = m.group(1)
            eid = int(m.group(2))
            slot = int(m.group(3))
            summary_key = f"Drive /c{ctrl}/e{eid}/s{slot}"
            summary = summaries.get(summary_key, {})

            state_key = f"Drive /c{ctrl}/e{eid}/s{slot} State"
            state = detail.get(state_key, {})

            temp = parse_temperature(state.get("Drive Temperature", ""))
            if temp is None:
                temp = _find_temperature(detail)

            rb = rebuild_map.get((eid, slot), {})
            cb = copyback_map.get((eid, slot), {})
            er = erase_map.get((eid, slot), {})
            disks.append(
                {
                    "ctrl": int(ctrl),
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
                    "rebuild_progress": rb.get("progress", ""),
                    "rebuild_eta": rb.get("eta", ""),
                    "copyback_progress": cb.get("progress", ""),
                    "copyback_eta": cb.get("eta", ""),
                    "erase_progress": er.get("progress", ""),
                    "erase_eta": er.get("eta", ""),
                }
            )

    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] disk parse error: {e}", file=sys.stderr)

    _append_failed_drives(data, disks, busy_drives)
    return disks


def _query_single_drive(ctrl: str, eid: int, slot: int) -> dict:
    """单盘回查：show /cX/eX/sX 对已脱离 show all 列表的盘（UBad/Failed）
    仍能返回完整摘要（State/Model/Size/温度等），用于补记时还原真实状态。

    ctrl 为控制器编号（正则捕获组，不带 /c 前缀）。"""
    try:
        data = run_storcli(f"/c{ctrl}/e{eid}/s{slot} show J")
        if not data:
            return {}
        rd = data["Controllers"][0].get("Response Data", {})
        rows = rd.get("Drive Information", [])
        if rows and isinstance(rows[0], dict):
            return rows[0]
    except Exception:
        pass
    return {}


def _append_failed_drives(
    data: dict | None, disks: list[dict], busy_drives: set[tuple[int, int]] = frozenset()
):
    """Failed/UBad/掉线的盘在 show all 的 Response Data 中没有条目，只在
    Command Status 的 Detailed Status 里以 ErrCd 45 等形式出现；补一条记录
    避免故障盘从页面上"消失"。

    - 正在执行 erase 等后台操作的盘也会短暂地从列表中消失（ErrCd 255
      "Drive erase is in progress"），盘本身并未故障，补记为对应的状态名。
    - 其余消失的盘逐盘回查单盘信息还原真实状态（UBad 等）；
      回查失败才兜底记为 Failed。"""
    try:
        detailed = (
            data["Controllers"][0].get("Command Status", {}).get("Detailed Status", [])
        )
        present = {(d["eid"], d["slot"]) for d in disks}
        for entry in detailed:
            if not isinstance(entry, dict) or entry.get("Status") == "Success":
                continue
            m = re.match(r"/c(\d+)/e(\d+)/s(\d+)", str(entry.get("Drive", "")))
            if not m:
                continue
            ctrl, eid, slot = m.group(1), int(m.group(2)), int(m.group(3))
            if (eid, slot) in present:
                continue
            errmsg = str(entry.get("ErrMsg", ""))
            # 后台操作进行中导致的临时消失：不算 Failed
            if (eid, slot) in busy_drives or "in progress" in errmsg.lower():
                state = (
                    "Erase"
                    if "erase" in errmsg.lower()
                    else "Rebuild"
                    if "rebuild" in errmsg.lower()
                    else "Copyback"
                    if "copyback" in errmsg.lower()
                    else "BgOp"
                )
                summary: dict = {}
            else:
                # 逐盘回查还原真实状态（UBad/Offln/……）
                summary = _query_single_drive(ctrl, eid, slot)
                state = str(summary.get("State", "")).strip() or "Failed"
            temperature = parse_temperature(summary.get("Drive Temperature", ""))
            disks.append(
                {
                    "ctrl": int(ctrl),
                    "eid": eid,
                    "slot": slot,
                    "did": summary.get("DID", ""),
                    "dg": summary.get("DG", ""),
                    "model": str(summary.get("Model", "")).strip(),
                    "state": state,
                    "size": str(summary.get("Size", "")).strip(),
                    "intf": str(summary.get("Intf", "")).strip(),
                    "med": str(summary.get("Med", "")).strip(),
                    "temperature": temperature if temperature is not None else "",
                    "media_error": 0,
                    "other_error": 0,
                    "predictive_failure": 0,
                    "smart_alert": "",
                    "shield_counter": 0,
                    "rebuild_progress": "",
                    "rebuild_eta": "",
                    "copyback_progress": "",
                    "copyback_eta": "",
                    "erase_progress": "",
                    "erase_eta": "",
                }
            )
    except Exception:
        pass


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


def collect_controller(ctrl_id: int = 0) -> dict | None:
    data = run_storcli(f"/c{ctrl_id} show all J")
    if not is_success(data):
        return None

    try:
        resp = data["Controllers"][0].get("Response Data", {})

        # show all 输出中型号/固件在 Basics/Version 子表
        model = resp.get("Basics", {}).get("Model", "").strip()
        fw_version = resp.get("Version", {}).get("Firmware Version", "").strip()

        vd_list = resp.get("VD LIST", [])
        num_vds = resp.get("Virtual Drives", len(vd_list))
        num_disks = resp.get("Physical Drives", len(resp.get("PD LIST", [])))

        # 阵列卡（ROC）温度
        hwcfg = resp.get("HwCfg", {})
        roc_temp = _to_int(hwcfg.get("ROC temperature(Degree Celsius)", "")) \
            if hwcfg.get("ROC temperature(Degree Celsius)") not in (None, "") else ""

        # 缓存单元是否存在。这个字段决定写缓存能不能安全开启：
        # 没有它时固件会把写缓存临时禁用，配置成写回也只会是"临时禁用"
        # 状态——判缓存保护是否有效，必须看这里而不是只看 bbu_state。
        bbu_present = str(hwcfg.get("BBU", "")).strip()
        cachevault_flash = str(hwcfg.get("CacheVault Flash Size", "")).strip()
        onboard_mem_mb = _to_int(hwcfg.get("On Board Memory Size", 0))

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
            "ctrl": ctrl_id,
            "model": model,
            "fw_version": fw_version,
            "health": health,
            "num_vds": num_vds,
            "num_disks": num_disks,
            "roc_temp": roc_temp,
            "bbu_model": bbu_model,
            "bbu_state": bbu_state,
            "bbu_temperature": bbu_temp_val if bbu_temp_val is not None else "",
            "bbu_present": bbu_present,
            "cachevault_flash": cachevault_flash,
            "onboard_memory_mb": onboard_mem_mb,
            "vd_states": "|".join(vd_states),
        }
    except Exception as e:
        print(
            f"[{datetime.now():%H:%M:%S}] controller parse error: {e}", file=sys.stderr
        )
    return None


def collect_vds(ctrl_id: int = 0) -> list[dict]:
    data = run_storcli(f"/c{ctrl_id} show J")
    if not is_success(data):
        return []
    try:
        vd_list = data["Controllers"][0].get("Response Data", {}).get("VD LIST", [])
        return [
            {
                "ctrl": ctrl_id,
                "dg_vd": vd.get("DG/VD", ""),
                "type": vd.get("TYPE", ""),
                "state": vd.get("State", ""),
                "size": vd.get("Size", ""),
                "name": vd.get("Name", ""),
                # 写缓存码（如 RWB / RWTD）。缓存保护评估要靠它判断
                # "写回是否真的生效"，否则只能再单独跑一次 storcli。
                "cache": vd.get("Cache", ""),
            }
            for vd in vd_list
        ]
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] VD parse error: {e}", file=sys.stderr)
    return []


def collect_controller_raw(ctrl_id: int = 0) -> dict | None:
    """控制器配置原文（供配置快照归一化使用）。

    与 collect_controller 用同一条命令但返回原始结构：那里只取展示需要的
    摘要字段，配置比对需要的是全量（阵列布局、开关、盘归属）。
    """
    data = run_storcli(f"/c{ctrl_id} show all J", timeout=60)
    return data if is_success(data) else None


# ---- 磁盘详细属性 ----


def collect_foreign_config(ctrl_id: int = 0) -> dict:
    """检测控制器上是否存在外来阵列配置（Foreign Configuration）。

    从其它机器/背板迁移过来的盘带有原机的阵列元数据时，storcli 将其视为
    "外来配置"：盘状态 UGood/UBad、部分场景下 DG 列显示 F。
    返回 {"present": bool, "count": int, "description": str}。
    """
    data = run_storcli(f"/c{ctrl_id} /fall show J", timeout=30)
    if not data:
        return {"present": False, "count": 0, "description": "查询失败"}
    try:
        cs = data["Controllers"][0].get("Command Status", {})
        desc = str(cs.get("Description", ""))
        # 无外来配置时 storcli 仍返回 Success，但 Description 提示 "Couldn't find"
        if "couldn" in desc.lower() and "foreign" in desc.lower():
            return {"present": False, "count": 0, "description": desc}
        if cs.get("Status") != "Success":
            return {"present": False, "count": 0, "description": desc or "查询失败"}
        # 有外来配置时统计个数（Description 形如 "X foreign configurations found"）
        m = re.search(r"(\d+)\s+foreign", desc, re.IGNORECASE)
        count = int(m.group(1)) if m else 1
        return {"present": True, "count": count, "description": desc}
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] foreign config parse error: {e}", file=sys.stderr)
        return {"present": False, "count": 0, "description": "解析失败"}


def collect_disk_attributes(ctrl_id: int = 0) -> list[dict]:
    data = run_storcli(f"/c{ctrl_id}/eall/sall show all J")
    resp = _response_data(data)
    if not resp:
        return []

    result = []
    try:
        summaries: dict[str, dict] = {}
        for key, val in resp.items():
            if key.startswith("Drive /c") and isinstance(val, list) and len(val) > 0:
                if isinstance(val[0], dict):
                    summaries[key] = val[0]

        for key, detail in resp.items():
            m = re.match(r"Drive /c(\d+)/e(\d+)/s(\d+)", key)
            if not m or not isinstance(detail, dict):
                continue
            ctrl = m.group(1)
            eid = int(m.group(2))
            slot = int(m.group(3))
            summary_key = f"Drive /c{ctrl}/e{eid}/s{slot}"
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
                    "ctrl": int(ctrl),
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


def collect_smart(disks: list[dict]) -> list[dict]:
    """通过 smartctl 采集 SMART 属性，兼容 SATA (ATA) 和 SAS (SCSI) 磁盘。

    smartctl 透传偶发失败（INQUIRY failed）时，回退到 storcli 读取原始
    SMART hex，避免 power_on_hours 等字段被错误地记为 0。
    """
    result = []
    for d in disks:
        did = d.get("did")
        eid = d.get("eid")
        slot = d.get("slot")
        disk_ctrl = _to_int(d.get("ctrl"))
        if did in (None, ""):
            continue
        try:
            did_int = int(did)
        except (ValueError, TypeError):
            continue
        try:
            cmd = (
                f"sudo {SMARTCTL} -a -d megaraid,{did_int} "
                f"{_smart_base_device(disk_ctrl)}"
            )
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=20
            )
            output = proc.stdout

            smart = {
                "ctrl": disk_ctrl,
                "did": did_int,
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

            # smartctl 透传失败（未解析出通电时长）时，回退 storcli 原始 SMART
            if smart["power_on_hours"] == 0 and eid is not None and slot is not None:
                hex_str = _storcli_smart_hex(disk_ctrl, int(eid), int(slot))
                if hex_str:
                    raws = _parse_smart_hex_raw(hex_str)
                    smart["reallocated"] = max(smart["reallocated"], raws.get(5, 0))
                    smart["reported_uncorrectable"] = raws.get(187, 0)
                    smart["command_timeout"] = raws.get(188, 0)
                    smart["pending"] = raws.get(197, 0)
                    smart["uncorrectable"] = max(
                        smart["uncorrectable"], raws.get(198, 0)
                    )
                    smart["power_on_hours"] = max(
                        smart["power_on_hours"], raws.get(9, 0)
                    )
                    if smart["smart_temp"] is None:
                        smart["smart_temp"] = raws.get(194)

            result.append(smart)

        except Exception as e:
            print(
                f"[{datetime.now():%H:%M:%S}] smartctl DID={did} error: {e}",
                file=sys.stderr,
            )
            result.append({"ctrl": disk_ctrl, "did": did_int, "reallocated": -1})

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
            # RAW_VALUE 在第 10 列；其后可能跟 "(Min/Max ...)" 等附加说明
            raw = int(parts[9])
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


def _storcli_smart_hex(ctrl_id: int, eid: int, slot: int) -> str | None:
    """storcli 读取物理盘原始 SMART hex（smartctl 透传失败时的兜底）。"""
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


def _parse_smart_hex_raw(hex_str: str) -> dict[int, int]:
    """解析 storcli 原始 ATA SMART hex，返回 {属性ID: raw值}。

    ATA SMART Read Data：前 2 字节版本号，之后每 12 字节一个属性
    （ID/flags/value/worst/raw6），raw 为 6 字节小端。
    """
    result: dict[int, int] = {}
    cleaned = re.sub(r"[^0-9a-fA-F]", "", hex_str or "")
    if len(cleaned) < 4:
        return result
    try:
        raw = bytes.fromhex(cleaned)
    except ValueError:
        return result
    for off in range(2, min(len(raw) - 11, 2 + 30 * 12), 12):
        aid = raw[off]
        if aid == 0:
            continue
        raw_val = int.from_bytes(raw[off + 5 : off + 11], "little")
        result[aid] = raw_val
    return result


# ---- NVMe 磁盘（直连 PCIe/U.2，不经 MegaRAID）----


def _read_nvme_smart(path: str) -> dict:
    """通过 smartctl 读取单块 NVMe 盘的关键 SMART/健康字段。"""
    smart: dict = {}
    try:
        proc = subprocess.run(
            f"sudo {SMARTCTL} -a -d nvme {path}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        output = proc.stdout or proc.stderr
    except Exception:
        return smart

    for line in output.splitlines():
        uline = line.upper()
        if "TEMPERATURE:" in uline and "SENSOR" not in uline:
            m = re.search(r"(\d+)\s*Celsius", line)
            if m:
                smart["temperature"] = int(m.group(1))
        elif "AVAILABLE SPARE:" in uline:
            m = re.search(r"(\d+)\s*%", line)
            if m:
                smart["available_spare"] = int(m.group(1))
        elif "PERCENTAGE USED:" in uline:
            m = re.search(r"(\d+)\s*%", line)
            if m:
                smart["percentage_used"] = int(m.group(1))
        elif "CRITICAL WARNING:" in uline:
            smart["critical_warning"] = line.split(":", 1)[-1].strip()
        elif "POWER ON HOURS:" in uline:
            m = re.search(r":\s*([\d,]+)", line)
            if m:
                smart["power_on_hours"] = int(m.group(1).replace(",", ""))
        elif "POWER CYCLES:" in uline:
            m = re.search(r":\s*([\d,]+)", line)
            if m:
                smart["power_cycles"] = int(m.group(1).replace(",", ""))
        elif "UNSAFE SHUTDOWNS:" in uline:
            m = re.search(r":\s*([\d,]+)", line)
            if m:
                smart["unsafe_shutdowns"] = int(m.group(1).replace(",", ""))
        elif "MEDIA AND DATA INTEGRITY ERRORS:" in uline:
            m = re.search(r":\s*([\d,]+)", line)
            if m:
                smart["media_errors"] = int(m.group(1).replace(",", ""))
    return smart


def collect_nvme() -> list[dict]:
    """采集 NVMe 直连盘：nvme list 提供身份/容量，smartctl 提供健康字段。"""
    rows: list[dict] = []
    try:
        proc = subprocess.run(
            "nvme list -o json",
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        data = json.loads(proc.stdout or "{}")
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] nvme list error: {e}", file=sys.stderr)
        return rows

    for dev in data.get("Devices", []):
        path = str(dev.get("DevicePath", "")).strip()
        if not path:
            continue
        smart = _read_nvme_smart(path)
        rows.append(
            {
                "device": path,
                "model": str(dev.get("ModelNumber", "")).strip(),
                "serial": str(dev.get("SerialNumber", "")).strip(),
                "firmware": str(dev.get("Firmware", "")).strip(),
                "size_bytes": dev.get("PhysicalSize", 0),
                "used_bytes": dev.get("UsedBytes", 0),
                "temperature": smart.get("temperature", ""),
                "available_spare": smart.get("available_spare", ""),
                "percentage_used": smart.get("percentage_used", ""),
                "critical_warning": smart.get("critical_warning", ""),
                "power_on_hours": smart.get("power_on_hours", ""),
                "power_cycles": smart.get("power_cycles", ""),
                "unsafe_shutdowns": smart.get("unsafe_shutdowns", ""),
                "media_errors": smart.get("media_errors", ""),
            }
        )
    return rows


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


# ---- IO 性能计数（原始计数器，速率由读取方按相邻样本差值计算）----

_DISK_NAME_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|xvd[a-z]+|hd[a-z]+)$")


def collect_io() -> list[dict]:
    rows = []
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 14:
                    continue
                name = parts[2]
                if not _DISK_NAME_RE.match(name):
                    continue
                try:
                    rows.append(
                        {
                            "name": name,
                            "reads": int(parts[3]),
                            "sectors_read": int(parts[5]),
                            "ms_read": int(parts[6]),
                            "writes": int(parts[7]),
                            "sectors_written": int(parts[9]),
                            "ms_write": int(parts[10]),
                            "ios_in_progress": int(parts[11]),
                            # 第 13/14 列是"IO 忙时长"与"加权 IO 忙时长"。
                            # 前 12 列只够算 IOPS 和带宽，缺了这两列就算不出
                            # 利用率与队列深度——而这两个才是判断"磁盘是不是
                            # 到瓶颈了"的依据。
                            "ms_io": int(parts[12]),
                            "weighted_ms_io": int(parts[13]),
                        }
                    )
                except ValueError:
                    continue
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] io collect error: {e}", file=sys.stderr)
    return rows


# ---- 文件系统用量 ----


def collect_fs() -> list[dict]:
    rows = []
    seen = set()
    try:
        with open("/proc/mounts") as f:
            mounts = [line.split() for line in f if line.strip()]
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] fs collect error: {e}", file=sys.stderr)
        return rows

    for parts in mounts:
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        if not device.startswith("/dev/"):
            continue
        if mountpoint in seen:
            continue
        seen.add(mountpoint)
        try:
            st = os.statvfs(mountpoint)
        except OSError:
            continue
        total_kb = st.f_blocks * st.f_frsize // 1024
        avail_kb = st.f_bavail * st.f_frsize // 1024
        used_kb = total_kb - st.f_bfree * st.f_frsize // 1024
        use_pct = round(used_kb / total_kb * 100, 1) if total_kb else 0.0
        rows.append(
            {
                "device": device,
                "mountpoint": mountpoint,
                "fstype": fstype,
                "size_kb": total_kb,
                "used_kb": used_kb,
                "avail_kb": avail_kb,
                "use_percent": use_pct,
            }
        )
    return rows


# ---- 巡读 / 一致性检查 ----


def _parse_props(props_list: list[dict]) -> dict:
    result = {}
    for item in props_list:
        result[item.get("Ctrl_Prop", "")] = item.get("Value", "")
    return result


def collect_patrol_read(ctrl_id: int = 0) -> dict | None:
    data = run_storcli(f"/c{ctrl_id} show patrolread J")
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
            "ctrl": ctrl_id,
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


def collect_consistency_check(ctrl_id: int = 0) -> dict | None:
    data = run_storcli(f"/c{ctrl_id} show cc J")
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
            "ctrl": ctrl_id,
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


def _migrate_csv_header(filepath: Path, fieldnames: list[str]):
    """采集字段增加时，把已有 CSV 重写为新表头（旧列数据保留，新列留空）"""
    try:
        with open(filepath, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] csv header migrate error: {e}", file=sys.stderr)


def write_csv(dir_path: Path, filename: str, fieldnames: list[str], rows: list[dict]):
    dir_path.mkdir(parents=True, exist_ok=True)
    filepath = dir_path / filename
    file_exists = filepath.exists() and filepath.stat().st_size > 0
    if file_exists:
        try:
            with open(filepath, encoding="utf-8") as f:
                header = f.readline().strip().split(",")
            if header and header != fieldnames:
                _migrate_csv_header(filepath, fieldnames)
        except Exception:
            pass
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

LOCK_FILE = BASE_DIR / ".collectd.lock"
MARKER_FILE = BASE_DIR / ".last_collect"


def _acquire_lock():
    """非阻塞排他锁：cron 与 Web 内置采集线程同时触发时只允许一个运行"""
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        fd = open(LOCK_FILE, "w", encoding="utf-8")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        return None


def _already_collected(minute_key: str) -> bool:
    """同一分钟只采集一次（cron 与内置线程可能都会触发）"""
    try:
        return MARKER_FILE.read_text(encoding="utf-8").strip() == minute_key
    except Exception:
        return False


def _should_run(now: datetime) -> bool:
    """按 Web 端配置的采集间隔决定是否执行本次采集。

    cron 仍每分钟触发；data/collection_config.json 中的
    interval_minutes ∈ {1,5,15,30,60} 决定实际采集频率。
    """
    cfg_file = BASE_DIR / "collection_config.json"
    try:
        if cfg_file.exists():
            with open(cfg_file, encoding="utf-8") as f:
                interval = int(json.load(f).get("interval_minutes", 1))
            if interval in (5, 15, 30, 60):
                return now.minute % interval == 0
    except Exception:
        pass
    return True


def main():
    now = datetime.now()
    force = "--force" in sys.argv
    # --quick（Web 操作后即时刷新）：与 --force 一样绕过门控与分钟去重，
    # 但跳过 SMART 等耗时采集，只刷新磁盘/控制器/VD 等关键状态
    quick = "--quick" in sys.argv
    # --force（Web“立即采集”）绕过间隔门控与分钟去重
    if not force and not quick and not _should_run(now):
        return
    lock_fd = _acquire_lock()
    if lock_fd is None:
        return  # 另一个实例（cron 或内置线程）正在采集
    try:
        minute_key = now.strftime("%Y-%m-%d %H:%M")
        if not force and not quick and _already_collected(minute_key):
            return
        _run_collection(now, force, quick)
        MARKER_FILE.write_text(minute_key, encoding="utf-8")
    finally:
        lock_fd.close()


def _run_collection(now: datetime, force: bool = False, quick: bool = False):
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    date_dir = BASE_DIR / now.strftime("%Y-%m-%d")
    minute = now.minute

    disk_fields = [
        "timestamp",
        "ctrl",
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
        "rebuild_progress",
        "rebuild_eta",
        "copyback_progress",
        "copyback_eta",
        "erase_progress",
        "erase_eta",
    ]
    ctrl_fields = [
        "timestamp",
        "ctrl",
        "model",
        "fw_version",
        "health",
        "num_vds",
        "num_disks",
        "roc_temp",
        "bbu_model",
        "bbu_state",
        "bbu_temperature",
        "bbu_present",
        "cachevault_flash",
        "onboard_memory_mb",
        "vd_states",
        "foreign_present",
        "foreign_count",
        "foreign_desc",
    ]
    vd_fields = ["timestamp", "ctrl", "dg_vd", "type", "state", "size", "name", "cache"]
    patrol_fields = [
        "timestamp",
        "ctrl",
        "pr_mode",
        "pr_delay",
        "pr_iterations",
        "pr_next",
        "pr_state",
        "pr_max_concurrent",
    ]
    cc_fields = [
        "timestamp",
        "ctrl",
        "cc_mode",
        "cc_delay",
        "cc_next",
        "cc_state",
        "cc_iterations",
        "cc_vd_completed",
    ]
    smart_fields = [
        "timestamp",
        "ctrl",
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
        "ctrl",
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
    io_fields = [
        "timestamp",
        "name",
        "reads",
        "sectors_read",
        "ms_read",
        "writes",
        "sectors_written",
        "ms_write",
        "ios_in_progress",
        "ms_io",
        "weighted_ms_io",
    ]
    fs_fields = [
        "timestamp",
        "device",
        "mountpoint",
        "fstype",
        "size_kb",
        "used_kb",
        "avail_kb",
        "use_percent",
    ]
    nvme_fields = [
        "timestamp",
        "device",
        "model",
        "serial",
        "firmware",
        "size_bytes",
        "used_bytes",
        "temperature",
        "available_spare",
        "percentage_used",
        "critical_warning",
        "power_on_hours",
        "power_cycles",
        "unsafe_shutdowns",
        "media_errors",
    ]

    # 控制器清单：多卡机器必须逐张采集，只读 /c0 会漏掉整张卡
    controller_ids = list_controller_ids()

    # 磁盘（每分钟追加）
    disks = []
    for cid in controller_ids:
        disks.extend(collect_disks(cid))
    if disks:
        for d in disks:
            d["timestamp"] = timestamp
        write_csv(date_dir, "disks.csv", disk_fields, disks)
        db.insert_rows("disks", disks)
    else:
        write_csv(
            date_dir,
            "disks.csv",
            disk_fields,
            [{"timestamp": timestamp, "state": "N/A"}],
        )

    # 控制器（每分钟追加，每张卡一行）
    ctrls = []
    for cid in controller_ids:
        ctrl = collect_controller(cid)
        if not ctrl:
            ctrl = {"ctrl": cid, "model": "", "fw_version": "", "health": "N/A"}
        ctrl["timestamp"] = timestamp
        # 外来阵列配置状态（供 Web 显示 F 标记与"载入外部配置"入口）
        fc = collect_foreign_config(cid)
        ctrl["foreign_present"] = 1 if fc.get("present") else 0
        ctrl["foreign_count"] = fc.get("count", 0)
        ctrl["foreign_desc"] = fc.get("description", "")
        ctrls.append(ctrl)
    write_csv(date_dir, "controller.csv", ctrl_fields, ctrls)
    db.insert_rows("controller", ctrls)

    # 故障邮件报警（基于本次采集结果）
    lsi_alert.check_and_alert(disks, ctrls)
    # 跨控制器一致性（固件/型号）：不属于任何一块盘，必须由采集侧主动检查
    lsi_alert.check_consistency(ctrls)

    # 控制器配置快照：配置不会自己变，变了就是有人动过手或换了卡。
    # 首次只建基线，之后有差异才留档并告警。
    if not quick:
        for cid in controller_ids:
            try:
                raw = collect_controller_raw(cid)
                if not raw:
                    continue
                cfg = config_mgr.normalize(raw)
                change = config_mgr.observe(cid, cfg)
                if change and change.get("changes"):
                    lsi_alert.check_config_drift(cid, change)
            except Exception as e:
                print(
                    f"[{datetime.now():%H:%M:%S}] config snapshot error (C{cid}): {e}",
                    file=sys.stderr,
                )

    # 系统信息（每分钟追加）
    sys_info = collect_system_info()
    sys_info["timestamp"] = timestamp
    write_csv(date_dir, "system.csv", sys_fields, [sys_info])
    db.insert_rows("system", [sys_info])

    # IO 性能计数（每分钟追加原始计数器）
    io_rows = collect_io()
    if io_rows:
        for r in io_rows:
            r["timestamp"] = timestamp
        write_csv(date_dir, "io.csv", io_fields, io_rows)
        db.insert_rows("io", io_rows)

    # 文件系统用量（每分钟追加）
    fs_rows = collect_fs()
    if fs_rows:
        for r in fs_rows:
            r["timestamp"] = timestamp
        write_csv(date_dir, "fs.csv", fs_fields, fs_rows)
        db.insert_rows("fs", fs_rows)

    # NVMe 磁盘（每分钟追加）
    nvme_rows = collect_nvme()
    if nvme_rows:
        for r in nvme_rows:
            r["timestamp"] = timestamp
        write_csv(date_dir, "nvme.csv", nvme_fields, nvme_rows)
        db.insert_rows("nvme", nvme_rows)

    # VD（每轮追加最新快照，确保创建/删除阵列后页面即时刷新；状态变化告警每分钟检测）
    vds = []
    for cid in controller_ids:
        vds.extend(collect_vds(cid))
    if vds:
        for v in vds:
            v["timestamp"] = timestamp
        write_csv(date_dir, "vds.csv", vd_fields, vds)
        db.insert_rows("vds", vds)

    # 磁盘状态变化 / VD 变化邮件告警（基于本次采集结果）
    lsi_alert.check_state_changes(disks, vds)

    # 缓存保护评估：写缓存开着但缓存单元失效会丢数据，这是存储里最安静的
    # 一类风险——阵列状态全是 Optimal，出问题时才知道。
    if not quick:
        try:
            assessments = []
            for c in ctrls:
                cid = c.get("ctrl")
                assessments.append(
                    battery_mgr.evaluate(
                        c,
                        [
                            {
                                "ctrl": v.get("ctrl"),
                                "dg_vd": v.get("dg_vd"),
                                "state": v.get("state"),
                                "write_cache_raw": v.get("cache"),
                            }
                            for v in vds
                            if v.get("ctrl") == cid
                        ],
                    )
                )
            battery_mgr.check_alerts(assessments)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] cache protection check error: {e}", file=sys.stderr)

    # 磁盘属性（当天首次采集）
    if not (date_dir / "attributes.csv").exists():
        attrs = []
        for cid in controller_ids:
            attrs.extend(collect_disk_attributes(cid))
        if attrs:
            for a in attrs:
                a["timestamp"] = timestamp
            write_csv_once(date_dir, "attributes.csv", attr_fields, attrs)
            db.insert_rows("attrs", attrs)

    # SMART（每 15 分钟采集一次；--force 或当天 smart.csv 缺失时立即补采，
    # 避免服务重启/跨天后页面上通电时长等 SMART 字段长时间为 0）
    # --quick 模式跳过 SMART（逐盘透传耗时长，操作后刷新不需要）
    smart_file_missing = not (date_dir / "smart.csv").exists()
    if disks and not quick and (force or minute % 15 == 0 or smart_file_missing):
        smart_data = collect_smart(disks)
        if smart_data:
            for s in smart_data:
                s["timestamp"] = timestamp
            write_csv_once(date_dir, "smart.csv", smart_fields, smart_data)
            db.insert_rows("smart", smart_data)
            # SMART 关键属性 (5/187/188/197/198) 数值变化邮件告警
            lsi_alert.check_smart_attr_changes(smart_data)

    # 巡读 / CC（每次覆盖写入，每张卡一行）
    pr_rows = []
    for cid in controller_ids:
        pr = collect_patrol_read(cid)
        if pr:
            pr["timestamp"] = timestamp
            pr_rows.append(pr)
    if pr_rows:
        write_csv_once(date_dir, "patrol.csv", patrol_fields, pr_rows)
        db.insert_rows("patrol", pr_rows)

    cc_rows = []
    for cid in controller_ids:
        cc = collect_consistency_check(cid)
        if cc:
            cc["timestamp"] = timestamp
            cc_rows.append(cc)
    if cc_rows:
        write_csv_once(date_dir, "consistency.csv", cc_fields, cc_rows)
        db.insert_rows("consistency", cc_rows)


if __name__ == "__main__":
    main()
