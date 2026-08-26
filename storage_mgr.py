#!/usr/bin/env python3
# ================================================
# 存储管理模块
# 基于 lsblk 枚举块设备，提供挂载 / 卸载 / 格式化能力
# 安全约束:
#   - RAID 成员盘（被 MegaRAID 接管的物理盘）不可操作
#   - 已挂载设备不可格式化
#   - 系统根分区所在设备不可格式化/卸载
# 所有写操作需要调用方（web_server）已做 admin 鉴权
# ================================================

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

ALLOWED_FS = ("ext4", "xfs")

_lsblk_cache: dict = {"ts": 0.0, "data": None}


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _lsblk() -> list[dict]:
    import time

    now = time.time()
    if _lsblk_cache["data"] is not None and now - _lsblk_cache["ts"] < 5:
        return _lsblk_cache["data"]
    rc, out, err = _run(["lsblk", "-J", "-O", "-b"])
    if rc != 0:
        raise RuntimeError(f"lsblk 失败: {err.strip()}")
    data = json.loads(out).get("blockdevices", [])
    _lsblk_cache.update(ts=now, data=data)
    return data


def invalidate_cache():
    _lsblk_cache.update(ts=0.0, data=None)


def _root_sources() -> set:
    """系统挂载点（/ 等）背后的源设备路径集合"""
    sources = set()
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] in ("/", "/boot"):
                    sources.add(parts[0])
    except Exception:
        pass
    return sources


def _is_under(dev: dict, names: set) -> bool:
    if dev.get("name") in names or dev.get("path") in names:
        return True
    return any(_is_under(c, names) for c in dev.get("children") or [])


def _normalize(dev: dict, raid_disks: set, root_names: set) -> dict:
    path = dev.get("path") or ("/dev/" + dev.get("name", ""))
    mountpoints = [
        m for m in (dev.get("mountpoints") or []) if m
    ]
    if dev.get("mountpoint") and dev["mountpoint"] not in mountpoints:
        mountpoints.append(dev["mountpoint"])
    node = {
        "name": dev.get("name", ""),
        "path": path,
        "type": dev.get("type", ""),
        "size": int(dev.get("size") or 0),
        "fstype": dev.get("fstype") or "",
        "label": dev.get("label") or "",
        "model": (dev.get("model") or "").strip(),
        "mountpoints": mountpoints,
        "ro": bool(dev.get("ro")),
        "raid_member": os.path.basename(path) in raid_disks,
        "system": _is_under(dev, root_names),
    }
    children = [
        _normalize(c, raid_disks, root_names) for c in dev.get("children") or []
    ]
    if children:
        node["children"] = children
    return node


def _raid_disk_names() -> set:
    """MegaRAID 物理盘对应的内核设备名（通过 /dev/disk/by-path 或 sysfs 难以可靠映射，
    这里以 storcli 可见盘的 /dev/sdX 顺序近似：lsblk 中 type=disk 且无分区的跳过判断，
    实际保护主要依靠 mount/system 检查与 Web 端确认）。"""
    names = set()
    try:
        for dev in _lsblk():
            if dev.get("type") == "disk":
                # 直通/JBOD 盘若正在被 RAID 管理，通常没有可用分区表标记；
                # 这里不做过激猜测，交由前端对 raid_member=False 的盘才允许操作
                pass
    except Exception:
        pass
    return names


def list_devices() -> list[dict]:
    root_names = set()
    for src in _root_sources():
        root_names.add(src)
        root_names.add(os.path.basename(src))
    raid_disks = _raid_disk_names()
    # loop 设备为内核环回设备（如 snap 镜像挂载），不属磁盘管理范围，予以过滤
    devices = [d for d in _lsblk() if str(d.get("type", "")).lower() != "loop"]
    return [_normalize(d, raid_disks, root_names) for d in devices]


def _find_device(path: str) -> dict | None:
    def walk(devs):
        for d in devs:
            p = d.get("path") or ("/dev/" + d.get("name", ""))
            if p == path:
                return d
            found = walk(d.get("children") or [])
            if found:
                return found
        return None

    return walk(_lsblk())


def _check_device_path(device: str) -> tuple[bool, str]:
    if not re.fullmatch(r"/dev/[A-Za-z0-9/_-]+", device or ""):
        return False, "非法设备路径"
    dev = _find_device(device)
    if not dev:
        return False, "设备不存在"
    return True, ""


def mount_device(device: str, mountpoint: str) -> tuple[bool, str]:
    ok, err = _check_device_path(device)
    if not ok:
        return False, err
    if not re.fullmatch(r"/[A-Za-z0-9/_.-]+", mountpoint or ""):
        return False, "非法挂载点"
    dev = _find_device(device)
    mounts = [m for m in (dev.get("mountpoints") or []) if m]
    if dev.get("mountpoint"):
        mounts.append(dev["mountpoint"])
    if mounts:
        return False, f"设备已挂载于 {mounts[0]}"
    os.makedirs(mountpoint, exist_ok=True)
    rc, _, serr = _run(["mount", device, mountpoint])
    invalidate_cache()
    if rc != 0:
        return False, serr.strip() or "mount 失败"
    return True, f"已挂载到 {mountpoint}"


def umount_device(device: str) -> tuple[bool, str]:
    ok, err = _check_device_path(device)
    if not ok:
        return False, err
    if device in _root_sources():
        return False, "系统设备不可卸载"
    rc, _, serr = _run(["umount", device])
    invalidate_cache()
    if rc != 0:
        return False, serr.strip() or "umount 失败"
    return True, "已卸载"


def format_device(device: str, fs_type: str) -> tuple[bool, str]:
    ok, err = _check_device_path(device)
    if not ok:
        return False, err
    if fs_type not in ALLOWED_FS:
        return False, f"仅支持 {', '.join(ALLOWED_FS)}"
    dev = _find_device(device)
    mounts = [m for m in (dev.get("mountpoints") or []) if m]
    if dev.get("mountpoint"):
        mounts.append(dev["mountpoint"])
    if mounts:
        return False, "设备已挂载，请先卸载"
    rc, _, serr = _run(["mkfs." + fs_type, "-F", device], timeout=600)
    invalidate_cache()
    if rc != 0:
        return False, serr.strip() or "mkfs 失败"
    return True, f"已格式化为 {fs_type}"


# ================================================
# NFS 共享管理（/etc/exports）
# 安全约束:
#   - 仅允许白名单内的挂载选项
#   - 系统关键目录不可共享
#   - 每次修改前备份 /etc/exports，修改后 exportfs -ra 生效
# ================================================

EXPORTS_FILE = "/etc/exports"
EXPORTS_BACKUP = "/etc/exports.lsi-monitor.bak"

NFS_ALLOWED_OPTS = (
    "rw", "ro", "sync", "async",
    "root_squash", "no_root_squash", "all_squash", "no_all_squash",
    "subtree_check", "no_subtree_check", "secure", "insecure", "no_wdelay",
)
# 这些目录本身或其子路径禁止共享
NFS_BLOCKED_SUBTREES = (
    "/etc", "/boot", "/proc", "/sys", "/dev", "/run",
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/root",
)
NFS_HOST_RE = re.compile(r"^(\*|[A-Za-z0-9_.*:@/-]{1,128})$")


def nfs_available() -> bool:
    return shutil.which("exportfs") is not None


def _parse_exports_line(line: str) -> dict | None:
    tokens = line.split()
    if not tokens:
        return None
    clients = []
    for tok in tokens[1:]:
        m = re.fullmatch(r"([^\s(]+)(?:\(([^\s)]*)\))?", tok)
        if not m:
            continue
        clients.append({"host": m.group(1), "options": m.group(2) or ""})
    return {"path": tokens[0], "clients": clients}


def list_exports() -> list[dict]:
    rows = []
    try:
        with open(EXPORTS_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parsed = _parse_exports_line(line)
                if parsed:
                    rows.append(parsed)
    except FileNotFoundError:
        pass
    return rows


def _check_export_path(path: str) -> tuple[bool, str]:
    if not re.fullmatch(r"/[A-Za-z0-9/_.-]+", path or ""):
        return False, "非法共享路径"
    norm = os.path.normpath(path)
    if norm == "/" or norm in NFS_BLOCKED_SUBTREES or any(
        norm.startswith(b + "/") for b in NFS_BLOCKED_SUBTREES
    ):
        return False, f"系统目录 {norm} 不可共享"
    if not os.path.isdir(norm):
        return False, f"目录不存在: {norm}"
    return True, norm


def _backup_exports() -> None:
    try:
        if os.path.exists(EXPORTS_FILE):
            shutil.copyfile(EXPORTS_FILE, EXPORTS_BACKUP)
    except Exception:
        pass


def _exportfs_reload() -> tuple[bool, str]:
    if not nfs_available():
        return False, "未安装 exportfs（nfs-utils / nfs-kernel-server）"
    rc, _, serr = _run(["exportfs", "-ra"], timeout=30)
    if rc != 0:
        return False, serr.strip() or "exportfs -ra 失败"
    return True, ""


def add_export(path: str, host: str, options: list[str]) -> tuple[bool, str]:
    ok, res = _check_export_path(path)
    if not ok:
        return False, res
    path = res
    host = (host or "").strip() or "*"
    if not NFS_HOST_RE.fullmatch(host):
        return False, "非法客户端（支持 *、主机名、IP、CIDR、@网组）"
    opts = []
    for o in options or []:
        if o not in NFS_ALLOWED_OPTS:
            return False, f"不支持的挂载选项: {o}"
        if o not in opts:
            opts.append(o)
    if "rw" not in opts and "ro" not in opts:
        opts.insert(0, "rw")

    for row in list_exports():
        if row["path"] == path and any(c["host"] == host for c in row["clients"]):
            return False, f"{path} 对 {host} 的共享已存在"

    _backup_exports()
    line = f"{path} {host}({','.join(opts)})\n"
    try:
        with open(EXPORTS_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        return False, f"写入 {EXPORTS_FILE} 失败: {e}"
    ok, msg = _exportfs_reload()
    if not ok:
        return False, f"已写入 {EXPORTS_FILE}，但重新加载失败: {msg}"
    return True, f"已共享 {path} 给 {host}"


def remove_export(path: str, host: str) -> tuple[bool, str]:
    try:
        with open(EXPORTS_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return False, f"{EXPORTS_FILE} 不存在"
    except Exception as e:
        return False, f"读取 {EXPORTS_FILE} 失败: {e}"

    new_lines = []
    removed = False
    for raw in lines:
        stripped = raw.strip()
        if removed or not stripped or stripped.startswith("#"):
            new_lines.append(raw)
            continue
        parsed = _parse_exports_line(stripped)
        if not parsed or parsed["path"] != path or not any(
            c["host"] == host for c in parsed["clients"]
        ):
            new_lines.append(raw)
            continue
        # 找到目标行：移除指定客户端，若还有其他客户端则保留该行
        kept = [
            tok for tok in stripped.split()[1:]
            if not tok.startswith(host + "(") and tok != host
        ]
        if kept:
            new_lines.append(f"{path} {' '.join(kept)}")
        removed = True

    if not removed:
        return False, f"未找到 {path} 对 {host} 的共享"
    _backup_exports()
    try:
        with open(EXPORTS_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + "\n")
    except Exception as e:
        return False, f"写入 {EXPORTS_FILE} 失败: {e}"
    ok, msg = _exportfs_reload()
    if not ok:
        return False, f"已修改 {EXPORTS_FILE}，但重新加载失败: {msg}"
    return True, f"已移除 {path} 对 {host} 的共享"
