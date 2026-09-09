"""bcache 加速管理模块。

状态查看 + 环境准备是只读/非破坏性的；真正的 make-bcache / attach 等写盘
操作需要调用方（页面）在确认设备且系统就绪后执行，本模块暂只提供安全部分。
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as exc:
        return 127, "", str(exc)


def tools_available() -> bool:
    return bool(shutil.which("make-bcache")) and bool(shutil.which("bcache-super-show"))


def module_loaded() -> bool:
    try:
        rc, out, _ = _run(["lsmod"], timeout=10)
        if rc != 0:
            return False
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0] == "bcache":
                return True
    except Exception:
        pass
    return False


def os_family() -> str:
    ids: list[str] = []
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ID="):
                    ids.append(line.split("=", 1)[1].strip().strip('"'))
                elif line.startswith("ID_LIKE="):
                    ids.extend(x.strip().strip('"') for x in line.split("=", 1)[1].split())
    except Exception:
        pass
    joined = " ".join(ids).lower()
    if "debian" in joined or "ubuntu" in joined:
        return "debian"
    if any(k in joined for k in ("rhel", "fedora", "centos", "rocky", "almalinux")):
        return "rhel"
    return ""


def prepare() -> tuple[bool, str]:
    """安装 bcache-tools 并加载内核模块。只做环境准备，不触碰已有块设备。"""
    if tools_available() and module_loaded():
        return True, "bcache 工具与内核模块已就绪"
    family = os_family()
    if not family:
        return False, "无法识别系统发行版，请手动安装 bcache-tools"
    if not tools_available():
        if family == "debian":
            rc, out, err = _run(["apt-get", "update", "-y"], timeout=600)
            if rc != 0:
                return False, (err or out).strip()[-400:] or "apt-get update 失败"
            rc, out, err = _run(["apt-get", "install", "-y", "bcache-tools"], timeout=600)
        else:
            rc, out, err = _run(["dnf", "install", "-y", "bcache-tools"], timeout=600)
        if rc != 0:
            return False, (err or out).strip()[-400:] or "安装 bcache-tools 失败"
    if not module_loaded():
        rc, out, err = _run(["modprobe", "bcache"], timeout=30)
        if rc != 0:
            return False, (err or out).strip()[-300:] or "modprobe bcache 失败"
    if not tools_available() or not module_loaded():
        return False, "环境准备未完成，请检查内核模块与 bcache-tools"
    return True, "bcache 工具与内核模块已就绪"


def bcache_devices() -> list[dict]:
    """读取 /sys 下的 bcache 设备与缓存集信息（只读）。"""
    csets = []
    if os.path.isdir("/sys/fs/bcache"):
        for name in sorted(os.listdir("/sys/fs/bcache")):
            if re.fullmatch(r"[0-9a-fA-F-]{36}", name):
                base = os.path.join("/sys/fs/bcache", name)
                info = {
                    "uuid": name,
                    "cache_hits": _to_int(_read(os.path.join(base, "stats_total", "cache_hits"))),
                    "cache_misses": _to_int(_read(os.path.join(base, "stats_total", "cache_misses"))),
                    "cache_hit_ratio": _to_int(_read(os.path.join(base, "stats_total", "cache_hit_ratio"))),
                    "cache_bypass_hits": _to_int(_read(os.path.join(base, "stats_total", "cache_bypass_hits"))),
                    "cache_bypass_misses": _to_int(_read(os.path.join(base, "stats_total", "cache_bypass_misses"))),
                    "cache_available_percent": _to_int(_read(os.path.join(base, "cache_available_percent"))),
                    "root_usage_percent": _to_int(_read(os.path.join(base, "root_usage_percent"))),
                }
                cache_info: dict = {}
                cache_link = os.path.join(base, "cache0")
                cache_offline = False
                if os.path.islink(cache_link):
                    m = re.search(r"/block/([^/]+)/", os.readlink(cache_link))
                    if m:
                        dev = m.group(1)
                        cb = f"/sys/block/{dev}/bcache"
                        cache_info["device"] = f"/dev/{dev}"
                        if not os.path.isdir(cb):
                            cache_offline = True
                            for key in ("nbuckets", "bucket_size", "block_size", "discard"):
                                cache_info[key] = None
                        else:
                            for key in ("nbuckets", "bucket_size", "block_size", "discard"):
                                cache_info[key] = _read(os.path.join(cb, key))
                            ps = _read(os.path.join(cb, "priority_stats"))
                            if ps:
                                for pkey in ("Unused", "Clean", "Dirty", "Metadata"):
                                    pm = re.search(rf"{pkey}:\s*([0-9.]+)%", ps)
                                    if pm:
                                        cache_info["bucket_" + pkey.lower()] = pm.group(1)
                info["cache_offline"] = cache_offline
                info["cache_info"] = cache_info
                csets.append(info)
    devices = []
    for d in sorted(glob.glob("/sys/block/bcache[0-9]*"), key=len):
        name = os.path.basename(d)
        bdir = os.path.join(d, "bcache")
        info: dict = {
            "device": f"/dev/{name}",
            "state": None,
            "cache_mode": None,
            "label": None,
            "dirty_data": None,
            "running": None,
        }
        if os.path.isdir(bdir):
            info["state"] = _read(os.path.join(bdir, "state"))
            info["cache_mode"] = _read(os.path.join(bdir, "cache_mode"))
            info["label"] = _read(os.path.join(bdir, "label"))
            info["dirty_data"] = _read(os.path.join(bdir, "dirty_data"))
            info["running"] = _read(os.path.join(bdir, "running"))
        info["name"] = name
        info["mounted"] = _mountpoint(name)
        devices.append(info)
    return [{"cache_sets": csets, "devices": devices}]


def status() -> dict:
    data = {
        "available": tools_available(),
        "module_loaded": module_loaded(),
        "os_family": os_family(),
        "make_bcache": shutil.which("make-bcache"),
        "bcache_super_show": shutil.which("bcache-super-show"),
    }
    dev = bcache_devices()
    data.update(dev[0])
    return data


def block_devices() -> list[dict]:
    """列出系统可见的整块磁盘（排除 loop/系统盘等），供选择缓存盘/被加速盘。"""
    rc, out, err = _run(["lsblk", "-J", "-b", "-o",
                         "NAME,SIZE,TYPE,ROTA,FSTYPE,MOUNTPOINT,MODEL,TRAN,PATH"], timeout=20)
    if rc != 0:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    used = _used_bcache_devices()
    result = []
    for disk in data.get("blockdevices", []):
        name = str(disk.get("name", ""))
        if name.startswith(("loop", "ram", "zram", "sr", "md", "dm-", "fd")):
            continue
        children = disk.get("children") or []
        has_part = bool(children)
        path = disk.get("path") or f"/dev/{name}"
        direct_mount = disk.get("mountpoint")
        child_mounts = [str(c.get("mountpoint") or "") for c in children]
        child_fstype = [str(c.get("fstype") or "").lower() for c in children]
        system = direct_mount in ("/", "/boot", "/boot/efi") \
            or any(m in ("/", "/boot", "/boot/efi") for m in child_mounts) \
            or "swap" in child_fstype
        if system:
            continue
        result.append({
            "device": name,
            "path": path,
            "size": disk.get("size"),
            "rota": disk.get("rota"),
            "model": disk.get("model") or "",
            "tran": disk.get("tran") or "",
            "has_partitions": has_part,
            "fstype": disk.get("fstype") or "",
            "in_bcache": path in used,
            "mounted": bool(direct_mount),
            "bcache_role": _super_role(path),
        })
    return result


def _used_bcache_devices() -> set:
    used = set()
    base = "/sys/fs/bcache"
    if not os.path.isdir(base):
        return used
    for name in os.listdir(base):
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", name):
            continue
        for link in ("cache0", "bdev0"):
            p = os.path.join(base, name, link)
            if not os.path.islink(p):
                continue
            try:
                target = os.readlink(p)
            except Exception:
                continue
            m = re.search(r"/block/([^/]+)/", target)
            if m:
                used.add(f"/dev/{m.group(1)}")
    return used


def _cache_set_uuids() -> list[str]:
    if not os.path.isdir("/sys/fs/bcache"):
        return []
    return [n for n in os.listdir("/sys/fs/bcache")
            if re.fullmatch(r"[0-9a-fA-F-]{36}", n)]


def _mountpoint(name: str) -> str | None:
    rc, out, _ = _run(["findmnt", "-no", "TARGET", f"/dev/{name}"], timeout=15)
    if rc == 0 and out.strip():
        return out.strip()
    return None


def _release_old_bcache(paths: list[str]) -> tuple[bool, str]:
    """解除旧 bcache 占用：先拒绝仍属于运行中缓存集的盘，再 stop 被自动注册的 backing。"""
    used = _used_bcache_devices()
    for path in paths:
        if path in used:
            return False, f"{path} 属于运行中的缓存集，请先在页面停止/注销后再操作"
    for path in paths:
        bdir = f"/sys/block/{os.path.basename(path)}/bcache"
        for _ in range(20):
            if not os.path.isdir(bdir):
                break
            try:
                with open(os.path.join(bdir, "stop"), "w", encoding="utf-8") as f:
                    f.write("1")
            except Exception as exc:
                return False, f"停止 {path} 的 bcache 注册失败：{exc}"
            time.sleep(0.3)
        if os.path.isdir(bdir):
            return False, f"{path} 无法解除 bcache 注册（可能仍被挂载/使用）"
    return True, ""


def create_bcache(cache_path: str, backing_path: str) -> tuple[bool, str, str | None]:
    """make-bcache 创建缓存集。返回 (ok, message, bcache_device)。"""
    devs = block_devices()
    by_path = {d["path"]: d for d in devs}
    if cache_path not in by_path or backing_path not in by_path:
        return False, "缓存盘或被加速盘不在系统可见列表中，请刷新后重试", None
    if cache_path == backing_path:
        return False, "缓存盘与被加速盘不能是同一块", None
    if by_path[cache_path].get("in_bcache") or by_path[backing_path].get("in_bcache"):
        return False, "所选设备已在 bcache 中使用，不能重复创建", None
    if by_path[cache_path].get("mounted") or by_path[backing_path].get("mounted"):
        return False, "所选设备已挂载，请先卸载后再创建", None
    before = set(glob.glob("/dev/bcache[0-9]*"))
    # 旧盘可能残留 bcache 签名并被 udev 自动注册，导致 wipefs 报 busy。
    # 先 stop 注册再擦除，并重试，避免与 udev 的自动注册发生竞态。
    last_err = ""
    for attempt in range(8):
        ok, msg = _release_old_bcache([cache_path, backing_path])
        if not ok:
            return False, msg, None
        rc, _, err = _run(["wipefs", "-a", cache_path, backing_path], timeout=60)
        if rc == 0:
            # 擦除成功后，若 udev 旧事件又把它注册回去，再清一次
            ok, msg = _release_old_bcache([cache_path, backing_path])
            if ok:
                break
            last_err = msg
        else:
            last_err = (err or "").strip()[-300:] or "清空设备失败"
        time.sleep(0.4)
    else:
        return False, last_err or "清空设备失败（设备持续 busy，可能仍被占用）", None
    rc, _, err = _run(["make-bcache", "-C", cache_path, "-B", backing_path], timeout=120)
    if rc != 0:
        return False, (err or "").strip()[-400:] or "make-bcache 失败", None
    new = None
    for _ in range(20):
        cur = set(glob.glob("/dev/bcache[0-9]*"))
        diff = cur - before
        if diff:
            new = sorted(diff)[0]
            break
        _run(["sleep", "0.5"], timeout=5)
    if not new:
        return False, "bcache 设备未自动出现，请检查 udev/内核注册", None
    return True, f"创建成功：{new}", new


def set_mode(name: str, mode: str) -> tuple[bool, str]:
    if mode not in ("writethrough", "writeback", "writearound", "none"):
        return False, "缓存模式必须是 writethrough/writeback/writearound/none"
    if _mountpoint(name):
        return False, f"{name} 已挂载，切换缓存模式前请先卸载"
    path = f"/sys/block/{name}/bcache/cache_mode"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(mode)
        return True, f"{name} 缓存模式已切换为 {mode}"
    except Exception as exc:
        return False, str(exc)


def detach(name: str) -> tuple[bool, str]:
    if _mountpoint(name):
        return False, f"{name} 已挂载，请先卸载再解绑"
    try:
        with open(f"/sys/block/{name}/bcache/detach", "w", encoding="utf-8") as f:
            f.write("1")
        return True, f"{name} 已与缓存集解绑"
    except Exception as exc:
        return False, str(exc)


def stop(name: str) -> tuple[bool, str]:
    if _mountpoint(name):
        return False, f"{name} 已挂载，请先卸载再停止"
    try:
        with open(f"/sys/block/{name}/bcache/stop", "w", encoding="utf-8") as f:
            f.write("1")
        return True, f"{name} 已停止"
    except Exception as exc:
        return False, str(exc)


def mount_bcache(name: str, mountpoint: str | None = None) -> tuple[bool, str]:
    if _mountpoint(name):
        return True, f"{name} 已挂载在 {_mountpoint(name)}"
    mountpoint = (mountpoint or f"/mnt/{name}").strip()
    if not mountpoint.startswith("/") or mountpoint == "/":
        return False, "挂载点需为绝对路径且不能是根目录"
    blocked = ("/boot", "/boot/efi", "/etc", "/usr", "/proc", "/sys", "/dev", "/run", "/root")
    if mountpoint == "/mnt" or any(mountpoint == b or mountpoint.startswith(b + "/") for b in blocked):
        return False, "挂载点不能选择系统关键目录"
    _run(["mkdir", "-p", mountpoint], timeout=20)
    rc, _, err = _run(["mount", f"/dev/{name}", mountpoint], timeout=60)
    if rc != 0:
        return False, (err or "").strip()[-300:] or "挂载失败（可能未格式化）"
    return True, f"{name} 已挂载到 {mountpoint}"


def umount_bcache(name: str) -> tuple[bool, str]:
    mp = _mountpoint(name)
    if not mp:
        return True, f"{name} 未挂载"
    rc, _, err = _run(["umount", f"/dev/{name}"], timeout=60)
    if rc != 0:
        return False, (err or "").strip()[-300:] or "卸载失败"
    return True, f"{name} 已卸载"


def device_stats(name: str) -> dict:
    """读取 /sys/block/<name>/bcache/stats_* 与缓存设备侧计数器。"""
    base = f"/sys/block/{name}/bcache"
    stats: dict[str, dict[str, str]] = {}
    for period in ("total", "day", "hour", "five_minute"):
        d = os.path.join(base, f"stats_{period}")
        if not os.path.isdir(d):
            continue
        entry = {}
        try:
            for fn in os.listdir(d):
                entry[fn] = _read(os.path.join(d, fn))
        except Exception:
            pass
        stats[period] = entry
    backing = _read(os.path.join(base, "backing_dev_name")) or ""
    cache_dev = None
    cset_base = "/sys/fs/bcache"
    if os.path.isdir(cset_base):
        for uuid in os.listdir(cset_base):
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
                continue
            bdev = os.path.join(cset_base, uuid, "bdev0")
            if os.path.islink(bdev):
                target = os.readlink(bdev)
                if backing and f"/block/{backing}/" in target:
                    cache_link = os.path.join(cset_base, uuid, "cache0")
                    if os.path.islink(cache_link):
                        m = re.search(r"/block/([^/]+)/", os.readlink(cache_link))
                        if m:
                            cache_dev = m.group(1)
                    break
    cache_info = {}
    if cache_dev:
        cb = f"/sys/block/{cache_dev}/bcache"
        for key in ("discard", "written", "metadata_written", "btree_written",
                    "nbuckets", "block_size", "bucket_size", "cache_replacement_policy"):
            cache_info[key] = _read(os.path.join(cb, key))
        cache_info["device"] = f"/dev/{cache_dev}"
    return {"device": f"/dev/{name}", "backing_device": backing, "stats": stats, "cache_info": cache_info}


def trigger_writeback(name: str) -> tuple[bool, str]:
    base = f"/sys/block/{name}/bcache"
    dirty = _read(os.path.join(base, "dirty_data")) or "0.0k"
    try:
        with open(os.path.join(base, "writeback_running"), "w", encoding="utf-8") as f:
            f.write("1")
        # 把回写延迟临时降到 0，让内核尽快开始回写（不改持久配置）
        with open(os.path.join(base, "writeback_delay"), "w", encoding="utf-8") as f:
            f.write("0")
        return True, f"已触发回写；当前脏数据 {dirty}"
    except Exception as exc:
        return False, str(exc)


def erase_superblock(dev_path: str) -> tuple[bool, str]:
    """擦除块设备上的 bcache 超级块（wipefs）。调用方必须二次确认。"""
    if not re.fullmatch(r"/dev/(sd[a-z]+|nvme\d+n\d+)$", dev_path):
        return False, "只允许擦除 sdX / nvmeXnY 整块设备"
    last_err = ""
    for _ in range(8):
        ok, msg = _release_old_bcache([dev_path])
        if not ok:
            return False, msg
        rc, _, err = _run(["wipefs", "-a", dev_path], timeout=60)
        if rc == 0:
            _release_old_bcache([dev_path])
            return True, f"{dev_path} 的超级块已擦除"
        last_err = (err or "").strip()[-300:] or "擦除失败"
        time.sleep(0.4)
    return False, last_err
    return True, f"{dev_path} 的超级块已擦除"


def gc_status(cset_uuid: str) -> dict:
    base = os.path.join("/sys/fs/bcache", cset_uuid, "internal")
    if not os.path.isdir(base):
        return {"error": "缓存集不存在或未注册"}
    keys = (
        "btree_gc_last_sec", "btree_gc_average_duration_ms",
        "btree_gc_average_frequency_sec", "btree_gc_max_duration_ms",
        "btree_used_percent", "reclaimed_journal_buckets",
        "copy_gc_enabled", "prune_cache", "gc_after_writeback",
        "reclaim", "active_journal_entries",
    )
    info = {}
    for key in keys:
        info[key] = _read(os.path.join(base, key))
    return info


def trigger_gc(cset_uuid: str) -> tuple[bool, str]:
    path = os.path.join("/sys/fs/bcache", cset_uuid, "internal", "trigger_gc")
    if not os.path.exists(path):
        return False, "无法找到 trigger_gc，缓存集不存在？"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("1")
        return True, "已触发一次垃圾回收"
    except Exception as exc:
        return False, str(exc)


def unregister_cset(cset_uuid: str) -> tuple[bool, str]:
    """注销整个缓存集（相当于把缓存盘下线并删除其 bcache 归属）。"""
    base = f"/sys/fs/bcache/{cset_uuid}"
    path = os.path.join(base, "unregister")
    if not os.path.exists(path):
        return False, "缓存集不存在或未注册"
    # 检查该缓存集下是否有仍挂载/运行的 bcache 设备
    for dev in bcache_devices()[0]["devices"]:
        if dev.get("mounted"):
            return False, f"{dev.get('device')} 仍挂载，请先卸载并停止"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("1")
        return True, f"缓存集 {cset_uuid} 已注销（缓存盘已下线）"
    except Exception as exc:
        return False, str(exc)


TUNABLES = (
    "writeback_percent",
    "writeback_rate",
    "writeback_delay",
    "writeback_metadata",
    "sequential_cutoff",
    "readahead_cache_policy",
)


def tunables(name: str) -> dict:
    base = f"/sys/block/{name}/bcache"
    out = {}
    for key in TUNABLES:
        val = _read(os.path.join(base, key))
        if val is not None:
            out[key] = val
    return out


def _parse_size_bytes(val) -> tuple[bool, int | str]:
    try:
        return True, int(val)
    except (ValueError, TypeError):
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmMgG])", str(val).strip())
        if not m:
            return False, val
        mult = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]
        return True, int(float(m.group(1)) * mult)


def _dirty_bytes(text: str | None) -> int:
    try:
        t = str(text or "").strip().lower()
        if not t:
            return 0
        mult = 1
        if t.endswith("k"):
            mult = 1024
            t = t[:-1]
        elif t.endswith("m"):
            mult = 1024 ** 2
            t = t[:-1]
        elif t.endswith("g"):
            mult = 1024 ** 3
            t = t[:-1]
        return int(float(t) * mult)
    except (ValueError, TypeError):
        return 0


def _super_role(path: str) -> str:
    rc, out, _ = _run(["bcache-super-show", path], timeout=15)
    if rc != 0:
        return ""
    low = out.lower()
    if "backing device" in low:
        return "backing"
    if "cache device" in low:
        return "cache"
    return ""


def _super_cache_state_dirty(path: str) -> bool:
    rc, out, _ = _run(["bcache-super-show", path], timeout=15)
    if rc != 0:
        return False
    return bool(re.search(r"dev\.data\.cache_state\s+\d+\s+\[dirty\]", out, re.IGNORECASE))


def _super_first_sector(path: str) -> int | None:
    rc, out, _ = _run(["bcache-super-show", path], timeout=15)
    if rc != 0:
        return None
    m = re.search(r"dev\.data\.first_sector\s+(\d+)", out)
    return int(m.group(1)) if m else None


def emergency_status() -> dict:
    mounts = []
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2:
                    continue
                src, mp = parts[0], parts[1]
                if not src.startswith("/dev/loop") or not mp.startswith("/mnt/emergency-"):
                    continue
                loop_name = src.replace("/dev/", "")
                try:
                    with open(f"/sys/block/{loop_name}/loop/backing_file", encoding="utf-8") as bf:
                        backing = bf.read().strip()
                except Exception:
                    backing = ""
                mounts.append({"loop": src, "backing": backing, "mountpoint": mp, "readonly": "ro" in parts[3].split(",")})
    except Exception:
        pass
    eligible = [d for d in block_devices() if d.get("bcache_role") == "backing"]
    return {"mounts": mounts, "eligible_backing": eligible}


def emergency_mount(backing_path: str) -> tuple[bool, str, str | None]:
    if not re.fullmatch(r"/dev/(sd[a-z]+|nvme\d+n\d+)$", backing_path):
        return False, "只支持整块设备", None
    first = _super_first_sector(backing_path)
    if first is None:
        return False, "该设备不是 bcache backing（没有数据偏移信息）", None
    for m in emergency_status()["mounts"]:
        if m["backing"] == backing_path:
            return True, f"已应急挂载：{m['mountpoint']}", m["mountpoint"]
    rc, out, err = _run(["losetup", "-f"], timeout=15)
    if rc != 0:
        return False, "无法分配 loop 设备", None
    loop_path = out.strip()
    offset = first * 512
    rc, _, err = _run(["losetup", "-o", str(offset), loop_path, backing_path], timeout=20)
    if rc != 0:
        return False, f"losetup 失败：{(err or '').strip()[-200:]}", None
    mountpoint = f"/mnt/emergency-{os.path.basename(backing_path)}"
    _run(["mkdir", "-p", mountpoint], timeout=20)
    rc, _, err = _run(["mount", "-o", "ro", loop_path, mountpoint], timeout=60)
    if rc != 0:
        _run(["losetup", "-d", loop_path], timeout=20)
        return False, f"只读挂载失败：{(err or '').strip()[-200:]}", None
    return True, f"应急数据已挂载（只读）：{mountpoint}", mountpoint


def emergency_unmount(backing_path: str) -> tuple[bool, str]:
    for m in emergency_status()["mounts"]:
        if m["backing"] == backing_path:
            rc, _, err = _run(["umount", m["mountpoint"]], timeout=60)
            if rc != 0:
                return False, f"卸载失败：{(err or '').strip()[-200:]}"
            _run(["losetup", "-d", m["loop"]], timeout=20)
            return True, f"已卸载应急数据：{m['mountpoint']}"
    return True, f"{backing_path} 没有应急挂载"


def set_tunable(name: str, key: str, value) -> tuple[bool, str]:
    if key not in TUNABLES:
        return False, f"不支持的参数 {key}"
    path = f"/sys/block/{name}/bcache/{key}"
    if key == "writeback_percent":
        try:
            v = int(value)
        except (ValueError, TypeError):
            return False, "writeback_percent 必须是整数"
        if not (0 <= v <= 100):
            return False, "writeback_percent 需在 0-100 之间"
        text = str(v)
    elif key == "writeback_rate":
        ok, v = _parse_size_bytes(value)
        if not ok or int(v) < 0:
            return False, "writeback_rate 需为非负大小（如 0 或 512M）"
        text = str(int(v))
    elif key == "writeback_delay":
        try:
            v = int(value)
        except (ValueError, TypeError):
            return False, "writeback_delay 必须是整数"
        if v < 0:
            return False, "writeback_delay 不能为负"
        text = str(v)
    elif key == "writeback_metadata":
        if str(value) not in ("0", "1"):
            return False, "writeback_metadata 只能是 0 或 1"
        text = "1" if str(value) == "1" else "0"
    elif key == "sequential_cutoff":
        ok, v = _parse_size_bytes(value)
        if not ok or int(v) < 0:
            return False, "sequential_cutoff 需为非负大小（0 表示关闭）"
        text = str(int(v))
    elif key == "readahead_cache_policy":
        v = str(value).strip()
        if v not in ("all", "meta-only"):
            return False, "readahead_cache_policy 只能是 all 或 meta-only"
        text = v
    else:
        text = str(value)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True, f"{key} 已设置为 {text}"
    except Exception as exc:
        return False, str(exc)


def register_device(path: str) -> tuple[bool, str]:
    if not re.fullmatch(r"/dev/(sd[a-z]+|nvme\d+n\d+)$", path):
        return False, "只允许注册 sdX / nvmeXnY 整块设备"
    try:
        with open("/sys/fs/bcache/register_quiet", "w", encoding="utf-8") as f:
            f.write(path)
        return True, f"{path} 已注册"
    except Exception as exc:
        return False, str(exc)


def attach(name: str, cset_uuid: str) -> tuple[bool, str]:
    if not re.fullmatch(r"bcache\d+", name):
        return False, "非法 bcache 设备名"
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", cset_uuid):
        return False, "非法缓存集 UUID"
    try:
        with open(f"/sys/block/{name}/bcache/attach", "w", encoding="utf-8") as f:
            f.write(cset_uuid)
        return True, f"{name} 已绑定缓存集 {cset_uuid[:8]}…"
    except Exception as exc:
        return False, str(exc)


def reattach(backing_path: str, cset_uuid: str) -> tuple[bool, str, str | None]:
    """注册一个带 bcache superblock 的 backing，并重新 attach 到已有缓存集。"""
    before = set(glob.glob("/sys/block/bcache[0-9]*"))
    ok, msg = register_device(backing_path)
    if not ok:
        return False, msg, None
    new = None
    for _ in range(30):
        cur = set(glob.glob("/sys/block/bcache[0-9]*"))
        diff = cur - before
        if diff:
            new = sorted(diff)[0]
            break
        _run(["sleep", "0.5"], timeout=5)
    if not new:
        return False, "已注册但未出现 bcache 设备，请刷新后再试", None
    name = os.path.basename(new)
    device = f"/dev/{name}"
    mode = _read(f"/sys/block/{name}/bcache/cache_mode") or ""
    if "[none]" in mode:
        ok, msg = attach(name, cset_uuid)
        if not ok:
            return False, f"{device} 已注册，但绑定失败：{msg}", device
    return True, f"{device} 已重新绑定缓存集", device


def recover() -> tuple[bool, str]:
    """恢复 bcache：清理失效缓存集引用，注册缓存盘与 backing，再重新绑定。"""
    devs = block_devices()
    candidates = [d for d in devs
                  if d.get("bcache_role") in ("backing", "cache") and not d.get("in_bcache")]
    if not candidates:
        return False, "没有找到需要恢复的 bcache 盘"
    try:
        mounts: dict[str, str] = {}
        # 1) 先把脏数据全部写回，避免注销缓存集时丢数据
        for dev in bcache_devices()[0]["devices"]:
            name = dev.get("name")
            if not name:
                continue
            if dev.get("mounted"):
                mounts[name] = dev["mounted"]
            dirty = _dirty_bytes(dev.get("dirty_data"))
            if dirty > 0:
                try:
                    with open(f"/sys/block/{name}/bcache/writeback_running", "w", encoding="utf-8") as f:
                        f.write("1")
                    with open(f"/sys/block/{name}/bcache/writeback_delay", "w", encoding="utf-8") as f:
                        f.write("0")
                except Exception:
                    pass
                waited = 0
                while waited < 60:
                    time.sleep(2)
                    waited += 2
                    now_dirty = _dirty_bytes(_read(f"/sys/block/{name}/bcache/dirty_data"))
                    if now_dirty <= 0:
                        break
                if _dirty_bytes(_read(f"/sys/block/{name}/bcache/dirty_data")) > 0:
                    return False, f"{name} 仍有脏数据未写回，请先手动回写完成后再恢复"
        # 2) 卸载并停止现有 bcache 设备
        for dev in bcache_devices()[0]["devices"]:
            name = dev.get("name")
            if not name:
                continue
            if dev.get("mounted"):
                rc, _, err = _run(["umount", f"/dev/{name}"], timeout=60)
                if rc != 0:
                    return False, f"卸载 /dev/{name} 失败：{(err or '').strip()[-200:]}"
            try:
                with open(f"/sys/block/{name}/bcache/stop", "w", encoding="utf-8") as f:
                    f.write("1")
            except Exception:
                pass
        # 3) 注销所有缓存集（清理失效 cache0 引用）
        time.sleep(1)
        for uuid in _cache_set_uuids():
            path = f"/sys/fs/bcache/{uuid}/unregister"
            if os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("1")
        # 4) 重新注册缓存盘与 backing
        for d in candidates:
            ok, msg = register_device(d["path"])
            if not ok:
                return False, f"注册 {d['path']} 失败：{msg}"
        # 5) 等待 bcache 设备出现并绑定
        new_bcache = None
        for _ in range(40):
            time.sleep(0.5)
            info = bcache_devices()[0]
            if info["devices"] and info["devices"][0]["state"] != "no cache":
                new_bcache = info["devices"][0]["device"]
                break
            if info["devices"] and not info["cache_sets"]:
                continue
            if info["cache_sets"] and not any(c.get("cache_offline") for c in info["cache_sets"]):
                new_bcache = info["devices"][0]["device"] if info["devices"] else None
                break
        if not new_bcache:
            return False, "缓存盘/backing 已注册，但 bcache 设备未完全恢复，请稍后刷新"
        name = os.path.basename(new_bcache)
        state = _read(f"/sys/block/{name}/bcache/state") or ""
        if "no cache" in state.lower():
            uuids = _cache_set_uuids()
            if uuids:
                ok, msg = attach(name, uuids[0])
                if not ok:
                    return False, f"{new_bcache} 已恢复缓存盘，但绑定失败：{msg}"
        # 6) 重新挂载原来的目录
        name = os.path.basename(new_bcache)
        for dev_name, mp in mounts.items():
            if dev_name == name and not _mountpoint(name):
                _run(["mkdir", "-p", mp], timeout=20)
                rc, _, err = _run(["mount", f"/dev/{name}", mp], timeout=60)
                if rc != 0:
                    return False, f"{new_bcache} 已恢复，但自动挂载 {mp} 失败：{(err or '').strip()[-200:]}"
        return True, f"缓存已恢复：{new_bcache}"
    except Exception as exc:
        return False, str(exc)


def _cset_uuid_for_cache(path: str) -> str | None:
    devname = os.path.basename(path)
    for uuid in _cache_set_uuids():
        link = f"/sys/fs/bcache/{uuid}/cache0"
        if not os.path.islink(link):
            continue
        try:
            target = os.readlink(link)
        except Exception:
            continue
        if f"/block/{devname}/" in target:
            return uuid
    return None


def destroy_cache(cache_path: str) -> tuple[bool, str]:
    """注销并清空一块缓存盘，使其变成空盘。"""
    if not re.fullmatch(r"/dev/(sd[a-z]+|nvme\d+n\d+)$", cache_path):
        return False, "只支持整块设备"
    devs = block_devices()
    by_path = {d["path"]: d for d in devs}
    if cache_path not in by_path:
        return False, "缓存盘不存在于系统可见列表中"
    try:
        # 0) 硬保护：若 backing 仍标记 dirty 且没有可运行的 bcache0 负责回写，则拒绝
        running_info = bcache_devices()[0]
        can_flush = any(
            d.get("state") and d.get("state") != "no cache"
            for d in running_info["devices"]
        )
        if not can_flush:
            for d in block_devices():
                if d.get("bcache_role") == "backing" and _super_cache_state_dirty(d["path"]):
                    return False, "有脏数据未回写，无法安全销毁（请先恢复 bcache0 并完成回写）"
        # 1) 确保没有脏数据留在缓存上
        for dev in bcache_devices()[0]["devices"]:
            name = dev.get("name")
            if not name:
                continue
            if dev.get("mounted"):
                rc, _, err = _run(["umount", f"/dev/{name}"], timeout=60)
                if rc != 0:
                    return False, f"卸载 /dev/{name} 失败：{(err or '').strip()[-200:]}"
            try:
                with open(f"/sys/block/{name}/bcache/stop", "w", encoding="utf-8") as f:
                    f.write("1")
            except Exception:
                pass
        # 2) 注销包含该缓存盘的缓存集
        time.sleep(1)
        uuid = _cset_uuid_for_cache(cache_path)
        if uuid:
            path = f"/sys/fs/bcache/{uuid}/unregister"
            if os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write("1")
            time.sleep(1)
        # 3) wipefs 清空缓存盘数据
        rc, _, err = _run(["wipefs", "-a", cache_path], timeout=60)
        if rc != 0:
            return False, (err or "").strip()[-300:] or "wipefs 失败"
        _run(["blockdev", "--rereadpt", cache_path], timeout=20)
        return True, f"{cache_path} 已销毁并清空"
    except Exception as exc:
        return False, str(exc)
