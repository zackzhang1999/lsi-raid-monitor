#!/usr/bin/env python3
# ================================================
# 存储管理模块：块设备枚举 / RAID 关联 / 挂载卸载 / 格式化 / SMART
# ================================================

from __future__ import annotations

import json
import os
import re
import subprocess
import time

import lsi_collectd

SMARTCTL_PATH = os.environ.get("SMARTCTL_PATH", "/usr/sbin/smartctl")

_LSBLK_CACHE_TTL = 10.0
_lsblk_cache = {"ts": 0.0, "data": []}

_NAME_RE = re.compile(r"^[a-zA-Z0-9/_-]+$")
ALLOWED_FSTYPES = ("ext4", "xfs")


# ---- lsblk 枚举 ----


def _normalize_node(node: dict) -> dict:
    mountpoints = node.get("mountpoints")
    if mountpoints is None:
        mp = node.get("mountpoint")
        mountpoints = [mp] if mp else []
    mountpoints = [m for m in mountpoints if m]

    try:
        size_bytes = int(node.get("size") or 0)
    except (ValueError, TypeError):
        size_bytes = 0

    return {
        "name": node.get("name"),
        "kname": node.get("kname") or node.get("name"),
        "path": node.get("path") or f"/dev/{node.get('name')}",
        "type": node.get("type"),
        "size_bytes": size_bytes,
        "model": (node.get("model") or "").strip() or None,
        "serial": (node.get("serial") or "").strip() or None,
        "wwn": node.get("wwn"),
        "tran": node.get("tran"),
        "rota": bool(node.get("rota")),
        "rm": bool(node.get("rm")),
        "pkname": node.get("pkname"),
        "fstype": node.get("fstype"),
        "label": node.get("label"),
        "uuid": node.get("uuid"),
        "mountpoints": mountpoints,
        "children": [_normalize_node(c) for c in node.get("children") or []],
    }


def list_block_devices() -> list[dict]:
    """运行 lsblk -J -O -b，返回磁盘→分区树（10 秒模块级缓存）。"""
    now = time.monotonic()
    if _lsblk_cache["data"] and now - _lsblk_cache["ts"] < _LSBLK_CACHE_TTL:
        return _lsblk_cache["data"]

    result = subprocess.run(
        ["lsblk", "-J", "-O", "-b"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(result.stdout or "{}")
    devices = [_normalize_node(d) for d in data.get("blockdevices", [])]
    _lsblk_cache["ts"] = now
    _lsblk_cache["data"] = devices
    return devices


def invalidate_cache():
    _lsblk_cache["ts"] = 0.0
    _lsblk_cache["data"] = []
    _tree_cache["ts"] = 0.0
    _tree_cache["data"] = None
    _storcli_cache["ts"] = 0.0
    _storcli_cache["data"] = None


def _all_nodes(devs: list[dict]) -> list[dict]:
    nodes = []
    for dev in devs:
        nodes.append(dev)
        nodes.extend(dev.get("children", []))
    return nodes


def find_device(name: str) -> dict | None:
    """按 name 在 lsblk 结果中查找设备节点（含分区），校验失败返回 None。"""
    if not name or not _NAME_RE.match(name):
        return None
    for node in _all_nodes(list_block_devices()):
        if node.get("name") == name:
            return node
    return None


# ---- 用量 / 系统盘 ----


def get_disk_usage(mountpoint: str | None) -> dict | None:
    """os.statvfs 统计挂载点用量；挂载点无效返回 None。"""
    if not mountpoint:
        return None
    try:
        st = os.statvfs(mountpoint)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    percent = round(used / total * 100, 1) if total > 0 else 0.0
    return {
        "mountpoint": mountpoint,
        "total": total,
        "used": used,
        "avail": avail,
        "percent": percent,
    }


def _system_disk_names() -> set:
    """根 / 所在设备及其父盘（pkname）下的所有节点名视为系统盘。"""
    root_src = None
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "/":
                    root_src = parts[0]
                    break
    except OSError:
        return set()
    if not root_src:
        return set()

    root_src = os.path.realpath(root_src)
    names = set()
    for dev in list_block_devices():
        paths = [dev.get("path")] + [c.get("path") for c in dev.get("children", [])]
        paths = [os.path.realpath(p) for p in paths if p]
        if root_src in paths or f"/dev/{root_src}" in paths:
            names.add(dev["name"])
            names.update(c["name"] for c in dev.get("children", []))
    return names


def is_system_disk(name: str) -> bool:
    """name 是系统盘（根分区所在盘）或其分区时返回 True。"""
    return name in _system_disk_names()


# ---- storcli 关联 ----


_STORCLI_CACHE_TTL = 60.0
_storcli_cache = {"ts": 0.0, "data": None}
_TREE_CACHE_TTL = 10.0
_tree_cache = {"ts": 0.0, "data": None}


def _collect_storcli() -> tuple[list, list, str]:
    """storcli 采集（60 秒缓存）；失败返回空数据。"""
    now = time.monotonic()
    if _storcli_cache["data"] is not None and now - _storcli_cache["ts"] < _STORCLI_CACHE_TTL:
        return _storcli_cache["data"]
    try:
        disks = lsi_collectd.collect_disks() or []
        attrs = lsi_collectd.collect_disk_attributes() or []
    except Exception:
        disks, attrs = [], []
    try:
        ctrl_model = (lsi_collectd.collect_controller() or {}).get("model") or ""
    except Exception:
        ctrl_model = ""
    _storcli_cache["ts"] = now
    _storcli_cache["data"] = (disks, attrs, ctrl_model)
    return disks, attrs, ctrl_model


_SIZE_UNITS = {"KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12, "PB": 1e15}


def _parse_size_text(text) -> int:
    """把 storcli 的 '3.638 TB' 之类容量字符串解析为字节数。"""
    m = re.match(r"^\s*([\d.]+)\s*([KMGTPE]B)\s*$", str(text or ""), re.I)
    if not m:
        return 0
    return int(float(m.group(1)) * _SIZE_UNITS.get(m.group(2).upper(), 1))


def _norm_model(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _looks_like_raid_vd(dev: dict, ctrl_model: str) -> bool:
    """RAID 虚拟盘（VD）在 OS 里显示为普通 /dev/sdX，
    其 MODEL 通常是 'MR9460-16i' 这类控制器型号缩写。"""
    if not ctrl_model or dev.get("type") != "disk":
        return False
    vd = _norm_model(dev.get("model"))
    if vd.startswith("mr"):
        vd = vd[2:]
    ctrl = _norm_model(ctrl_model)
    return bool(vd) and len(vd) >= 4 and vd in ctrl


def merge_with_storcli(devs: list[dict]) -> list[dict]:
    """
    关联 storcli PD 与 /dev 节点（按 serial，其次 wwn 匹配）。
    匹配不到 /dev 节点的 storcli PD（阵列成员盘对 OS 不可见）以
    合成节点（name 为 E<eid>:S<slot>、path=None）追加到列表末尾。
    storcli 调用失败时所有磁盘标记为未关联，不抛异常。
    """
    disks, attrs, ctrl_model = _collect_storcli()

    attr_by_es = {(a.get("eid"), a.get("slot")): a for a in attrs}
    pd_list = []
    for d in disks:
        attr = attr_by_es.get((d.get("eid"), d.get("slot")), {})
        pd_list.append(
            {
                "eid": d.get("eid"),
                "slot": d.get("slot"),
                "did": d.get("did"),
                "state": d.get("state"),
                "dg": d.get("dg"),
                "sn": str(attr.get("sn") or "").strip(),
                "model": d.get("model"),
                "size": d.get("size"),
                "intf": d.get("intf"),
                "med": d.get("med"),
                "temperature": d.get("temperature"),
            }
        )

    matched_pds: set[int] = set()

    def match_pd(dev: dict) -> dict | None:
        serial = (dev.get("serial") or "").strip()
        wwn = (dev.get("wwn") or "").strip()
        for key in (serial, wwn):
            if key:
                for pd in pd_list:
                    if pd["sn"] and pd["sn"] == key:
                        matched_pds.add(id(pd))
                        return pd
        return None

    system_names = _system_disk_names()
    for dev in devs:
        pd = match_pd(dev)
        if pd:
            dev["storcli"] = {
                "eid": pd["eid"],
                "slot": pd["slot"],
                "did": pd["did"],
                "state": pd["state"],
                "dg": pd["dg"],
            }
            dg_raw = pd.get("dg")
            dev["category"] = "jbod" if dg_raw is None or str(dg_raw).strip() == "-" else "raid-member"
        else:
            dev["storcli"] = None
            name = dev.get("name") or ""
            if dev.get("tran") == "nvme" or name.startswith("nvme"):
                dev["category"] = "nvme"
            elif _looks_like_raid_vd(dev, ctrl_model):
                dev["category"] = "raid-vd"
            else:
                dev["category"] = "direct-sata"
        dev["is_system"] = dev.get("name") in system_names

    # 阵列成员盘等对 OS 不可见的 storcli PD：以合成节点展示
    for pd in pd_list:
        if id(pd) in matched_pds:
            continue
        dg_raw = pd.get("dg")
        is_jbod = dg_raw is None or str(dg_raw).strip() == "-"
        devs.append(
            {
                "name": f"E{pd['eid']}:S{pd['slot']}",
                "kname": None,
                "path": None,
                "type": "disk",
                "size_bytes": _parse_size_text(pd.get("size")),
                "model": pd.get("model"),
                "serial": pd.get("sn") or None,
                "wwn": None,
                "tran": pd.get("intf"),
                "rota": pd.get("med") == "HDD",
                "rm": False,
                "pkname": None,
                "fstype": None,
                "label": None,
                "uuid": None,
                "mountpoints": [],
                "children": [],
                "storcli": {
                    "eid": pd["eid"],
                    "slot": pd["slot"],
                    "did": pd["did"],
                    "state": pd["state"],
                    "dg": pd["dg"],
                },
                "category": "jbod" if is_jbod else "raid-member",
                "is_system": False,
                "no_device": True,
            }
        )
    return devs


# ---- SMART ----


def _smartctl_cmd(name: str, storcli: dict | None, json_mode: bool) -> list:
    cmd = ["sudo", SMARTCTL_PATH, "-a"]
    if json_mode:
        cmd.append("-j")
    did = (storcli or {}).get("did")
    try:
        did_ok = did is not None and int(did) >= 0
    except (ValueError, TypeError):
        did_ok = False
    if did_ok:
        cmd += ["-d", f"megaraid,{int(did)}"]
        cmd.append("/dev/sda")
    else:
        cmd.append(f"/dev/{name}")
    return cmd


def _parse_smart_json(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    passed = None
    smart_status = data.get("smart_status")
    if isinstance(smart_status, dict):
        passed = smart_status.get("passed")

    temperature = None
    temp_obj = data.get("temperature")
    if isinstance(temp_obj, dict):
        temperature = temp_obj.get("current")
        # SCSI/RAID 虚拟盘常以 0 表示“未上报”，视为无数据
        if not temperature:
            temperature = None

    power_on_hours = None
    pot = data.get("power_on_time")
    if isinstance(pot, dict):
        power_on_hours = pot.get("hours")

    if passed is None and temperature is None and power_on_hours is None:
        return None

    if passed is True:
        health = "PASSED"
    elif passed is False:
        health = "FAILED"
    else:
        health = None

    return {
        "healthy": passed,
        "passed": passed,
        "health": health,
        "temperature": temperature,
        "power_on_hours": power_on_hours,
    }


def _run_smartctl(cmd: list) -> subprocess.CompletedProcess | None:
    try:
        # smartctl 对不支持的设备返回非零是常态，不检查返回码
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception:
        return None


def get_smart_full(name: str, storcli: dict | None = None) -> dict:
    """获取完整 SMART 输出：原始 `smartctl -a` 文本；另附 JSON 解析的摘要。"""
    if storcli is None and (not name or not _NAME_RE.match(name)):
        return {"success": False, "error": f"无效的设备名: {name}"}

    # 摘要走 JSON 模式解析（失败则为 None，不影响原文展示）
    summary = None
    result = _run_smartctl(_smartctl_cmd(name, storcli, json_mode=True))
    if result is not None and result.stdout:
        summary = _parse_smart_json(result.stdout)

    # 展示用原始文本格式
    cmd = _smartctl_cmd(name, storcli, json_mode=False)
    result = _run_smartctl(cmd)
    if result is None:
        return {"success": False, "error": "smartctl 执行失败", "command": " ".join(cmd)}
    if not result.stdout:
        return {
            "success": False,
            "error": (result.stderr or "smartctl 无输出").strip(),
            "command": " ".join(cmd),
        }
    return {
        "success": True,
        "command": " ".join(cmd),
        "output": result.stdout,
        "summary": summary,
    }


def get_smart_summary(name: str, storcli: dict | None = None) -> dict | None:
    """获取 SMART 摘要（healthy/temperature/power_on_hours），失败返回 None。"""
    if storcli is None and (not name or not _NAME_RE.match(name)):
        return None
    cmd = _smartctl_cmd(name, storcli, json_mode=True)
    result = _run_smartctl(cmd)
    if result is None or not result.stdout:
        return None
    return _parse_smart_json(result.stdout)


# ---- 存储树 ----


def build_storage_tree() -> dict:
    """汇总块设备、storcli 关联、用量与 SMART 摘要（10 秒缓存）。"""
    now = time.monotonic()
    if _tree_cache["data"] is not None and now - _tree_cache["ts"] < _TREE_CACHE_TTL:
        return _tree_cache["data"]

    devs = merge_with_storcli(list_block_devices())
    for dev in devs:
        mountpoint = dev["mountpoints"][0] if dev["mountpoints"] else None
        if mountpoint is None:
            for child in dev.get("children", []):
                if child["mountpoints"]:
                    mountpoint = child["mountpoints"][0]
                    break
        dev["usage"] = get_disk_usage(mountpoint)
        try:
            dev["smart_health"] = get_smart_summary(dev["name"], dev.get("storcli"))
        except Exception:
            dev["smart_health"] = None
    tree = {"disks": devs}
    _tree_cache["ts"] = now
    _tree_cache["data"] = tree
    return tree


def get_smart_any(name: str) -> dict:
    """完整 SMART：支持 /dev 节点名（如 sda）与 storcli 合成名（如 E65:S0）。"""
    node = find_merged_device(name)
    if node is not None:
        return get_smart_full(name, node.get("storcli"))
    m = re.match(r"^E(\d+):S(\d+)$", str(name or ""))
    if m:
        eid, slot = int(m.group(1)), int(m.group(2))
        disks, _, _ = _collect_storcli()
        for d in disks:
            if d.get("eid") == eid and d.get("slot") == slot:
                storcli = {"eid": eid, "slot": slot, "did": d.get("did")}
                return get_smart_full(name, storcli)
        return {"success": False, "error": f"未找到磁盘: {name}"}
    return {"success": False, "error": f"设备不存在或名称非法: {name}"}


# ---- 操作 ----


def _run_privileged(cmd: list, timeout: int = 120) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return False, str(e)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        return False, msg
    return True, (result.stdout or "").strip()


def mount_device(name: str, mountpoint: str | None = None) -> tuple[bool, dict | str]:
    """挂载设备；mountpoint 缺省 /mnt/<label 或 uuid前8位 或 name>。"""
    node = find_device(name)
    if node is None:
        return False, f"设备不存在或名称非法: {name}"
    if node["mountpoints"]:
        return False, f"设备已挂载于 {node['mountpoints'][0]}"

    if not mountpoint:
        ident = node.get("label") or (node.get("uuid") or "")[:8] or name
        mountpoint = f"/mnt/{ident}"
    mountpoint = os.path.normpath(mountpoint)
    if not mountpoint.startswith("/"):
        return False, f"挂载点必须是绝对路径: {mountpoint}"

    ok, msg = _run_privileged(["sudo", "mkdir", "-p", mountpoint])
    if not ok:
        return False, f"创建挂载点失败: {msg}"
    ok, msg = _run_privileged(["sudo", "mount", f"/dev/{name}", mountpoint])
    if not ok:
        return False, f"挂载失败: {msg}"

    invalidate_cache()
    return True, {"device": name, "mountpoint": mountpoint}


def umount_device(name: str) -> tuple[bool, dict | str]:
    """卸载设备；必须已挂载，拒绝系统盘。"""
    node = find_device(name)
    if node is None:
        return False, f"设备不存在或名称非法: {name}"
    if is_system_disk(name):
        return False, "拒绝卸载系统盘"
    if not node["mountpoints"]:
        return False, "设备未挂载"

    mountpoint = node["mountpoints"][0]
    ok, msg = _run_privileged(["sudo", "umount", mountpoint])
    if not ok:
        return False, f"卸载失败: {msg}"

    invalidate_cache()
    return True, {"device": name, "mountpoint": mountpoint}


def format_device(
    name: str, fstype: str, label: str | None, confirm_name: str
) -> tuple[bool, dict | str]:
    """格式化设备为 ext4/xfs；需 confirm_name == name 二次确认。"""
    if confirm_name != name:
        return False, "确认名称与设备名不一致，已取消"
    node = find_device(name)
    if node is None:
        return False, f"设备不存在或名称非法: {name}"
    if fstype not in ALLOWED_FSTYPES:
        return False, f"不支持的文件系统: {fstype}（仅支持 ext4/xfs）"
    if node["mountpoints"]:
        return False, f"设备已挂载于 {node['mountpoints'][0]}，请先卸载"
    for child in node.get("children", []):
        if child.get("mountpoints"):
            return False, (
                f"分区 /dev/{child['name']} 已挂载于 {child['mountpoints'][0]}，"
                "请先卸载该分区"
            )
    if is_system_disk(name):
        return False, "拒绝格式化系统盘"
    if (node.get("category") or (find_merged_device(name) or {}).get("category")) == "raid-member":
        return False, "拒绝格式化 RAID 成员盘"

    cmd = ["sudo", f"mkfs.{fstype}"]
    if fstype == "ext4":
        cmd.append("-F")
    if label:
        if not re.match(r"^[\w. -]{1,64}$", label):
            return False, f"非法的卷标: {label}"
        cmd += ["-L", label]
    cmd.append(f"/dev/{name}")

    ok, msg = _run_privileged(cmd, timeout=600)
    if not ok:
        return False, f"格式化失败: {msg}"

    invalidate_cache()
    return True, {"device": name, "fstype": fstype, "label": label or ""}


def find_merged_device(name: str) -> dict | None:
    """在 storcli 关联后的设备树中查找节点（含 category/storcli 信息）。"""
    if not name or not _NAME_RE.match(name):
        return None
    try:
        devs = merge_with_storcli(list_block_devices())
    except Exception:
        return None
    for node in _all_nodes(devs):
        if node.get("name") == name:
            return node
    return None
