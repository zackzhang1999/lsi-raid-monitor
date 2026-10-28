"""顺序 / 随机读写基准测试（fio）。

这是全项目**唯一**会主动写盘的模块，所以安全边界比功能更重要：

1. **只碰自己创建的文件。** 目标目录里生成 `dmx-bench-<hex>.bin`，用
   `O_CREAT|O_EXCL` 打开——同名文件存在就失败，绝不覆盖任何现存文件。
2. **绝不对块设备直接写。** 只接受目录；`/dev/sdX` 在入口就被拒。
3. **拒绝系统盘整块。** 包括 /boot、/boot/efi 等所有分区。
4. **空间不够不跑。** 空闲 ≥ 测试文件 1.5 倍。
5. **跑完必删。** finally 只删记录下来的那个精确路径。
6. **同时只允许一个**；后台任务（重建/CC/巡读）在跑时不测。
7. **默认丢弃页缓存**（可关），否则测出来的是内存带宽。
   fio 的 O_DIRECT 在 bcache 这类栈上并不保证绕过缓存——实测同一文件
   命中缓存 7.9 GB/s、丢弃后 1.5 GB/s，差了 5 倍。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import perf_mgr

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))

FIO = os.environ.get("FIO_PATH", "fio")
FILE_PREFIX = "dmx-bench-"
MIN_SIZE_MB = 128
MAX_SIZE_MB = 8192
MAX_RUNTIME = 600
MAX_RAMP = 300
LIVE_POINTS = 600

_FORBIDDEN_PREFIXES = ("/proc", "/sys", "/dev", "/boot", "/etc", "/bin", "/sbin", "/lib", "/usr", "/var", "/run")

# 模式 -> (显示名, fio rw, 是否需要读写混合)
MODES = {
    "rand-rw": ("随机混合读写", "randrw"),
    "rand-read": ("随机读", "randread"),
    "rand-write": ("随机写", "randwrite"),
    "seq-read": ("顺序读", "read"),
    "seq-write": ("顺序写", "write"),
}

_lock = threading.Lock()
_state = {
    "running": False,
    "phase": "",
    "progress": 0,
    "elapsed": 0,
    "total": 0,
    "started_at": "",
    "finished_at": "",
    "target": "",
    "file": "",
    "size_mb": 0,
    "cfg": {},
    "live": [],
    "summary": None,
    "log": [],
    "error": "",
    "cancelled": False,
}
_proc: subprocess.Popen | None = None
_cancel = threading.Event()


def fio_available() -> bool:
    return shutil.which(FIO) is not None


def _fail(msg: str) -> tuple[bool, str]:
    return False, msg


def _disk_of(dev: str) -> str:
    """sdc3 -> sdc，nvme0n1p2 -> nvme0n1。"""
    d = re.sub(r"p?\d+$", "", str(dev or ""))
    return d or str(dev or "")


def _log(msg: str) -> None:
    _state["log"].append({"at": datetime.now().strftime("%H:%M:%S"), "msg": msg})
    _state["log"] = _state["log"][-80:]


def _target_dev(mountpoint: str) -> str:
    mounts = perf_mgr._mounts()
    best, best_len = "", -1
    for d, mps in mounts.items():
        for mp in mps:
            if (mountpoint == mp or mountpoint.startswith(mp.rstrip("/") + "/")) and len(mp) > best_len:
                best, best_len = d, len(mp)
    return best


def validate(target: str, size_mb: int) -> tuple[bool, str]:
    if not fio_available():
        return _fail("未安装 fio，无法执行基准测试（apt install fio）")
    target = str(target or "").strip()
    if not target:
        return _fail("请选择测试目录")
    if target.startswith("/dev/"):
        return _fail("拒绝对块设备直接写入：请指定一个目录，测试只会在里面创建自己的临时文件")
    p = Path(target)
    if not p.is_absolute():
        return _fail("必须是绝对路径")
    try:
        real = Path(os.path.realpath(p))
    except OSError as e:
        return _fail(f"路径无法解析：{e}")
    if not real.exists() or not real.is_dir():
        return _fail("目标必须是一个已存在的目录")
    for bad in _FORBIDDEN_PREFIXES:
        if str(real) == bad or str(real).startswith(bad + "/"):
            return _fail(f"不允许在系统目录 {bad} 下做基准测试")

    size_mb = int(size_mb)
    if not (MIN_SIZE_MB <= size_mb <= MAX_SIZE_MB):
        return _fail(f"测试文件大小须在 {MIN_SIZE_MB}~{MAX_SIZE_MB} MB 之间")

    mounts = perf_mgr._mounts()
    root_dev = next((d for d, mps in mounts.items() if "/" in mps), "")
    root_disk = _disk_of(root_dev)
    dev = _target_dev(str(real))
    if not dev:
        return _fail("目标不在任何独立挂载点上，无法确认它对应的存储")
    if _disk_of(dev) == root_disk:
        return _fail(
            f"目标 {dev} 位于系统盘（{root_disk}）上——包括 /boot、/boot/efi 在内的"
            "任何系统盘分区都不允许做基准测试"
        )
    st = os.statvfs(str(real))
    free_mb = st.f_bavail * st.f_frsize / 1048576
    if free_mb < size_mb * 1.5:
        return _fail(f"空闲空间不足：需要 {int(size_mb * 1.5)} MB，当前仅 {int(free_mb)} MB")
    return True, ""


def _background_busy(status: dict) -> str:
    for d in status.get("physical_disks") or []:
        if d.get("rebuild_progress") is not None:
            return f"{d.get('full_label') or d.get('label')} 正在重建"
        if d.get("erase_progress") is not None:
            return f"{d.get('full_label') or d.get('label')} 正在擦除"
    for c in status.get("controllers") or []:
        m = c.get("maintenance") or {}
        for key, label in (("consistency_check", "一致性检查"), ("patrol_read", "巡读")):
            state = str((m.get(key) or {}).get("state") or "")
            if state and state.lower() not in ("stopped", "none", ""):
                return f"C{c.get('ctrl')} {label}运行中（{state}）"
    return ""


def _drop_caches() -> bool:
    """先 sync 刷脏页，再丢弃干净的页缓存。

    只影响缓存内容，不会丢任何数据；代价是随后一段时间其它进程的读会变慢。
    """
    try:
        subprocess.run(["sync"], timeout=60, check=False)
    except Exception:
        pass
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("3\n")
        return True
    except OSError:
        return False


def _fio_cmd(path: Path, size_mb: int, cfg: dict) -> list[str]:
    rw = MODES[cfg["mode"]][1]
    cmd = [
        FIO,
        "--name=bench",
        f"--filename={path}",
        f"--size={size_mb}M",
        f"--rw={rw}",
        f"--bs={cfg['bs']}",
        "--ioengine=libaio",
        f"--iodepth={cfg['iodepth']}",
        f"--numjobs={cfg['threads']}",
        "--time_based",
        f"--runtime={cfg['runtime']}",
        "--output-format=json",
        "--allow_file_create=0",
        "--unlink=0",
        "--group_reporting",
        "--randrepeat=1",
        "--norandommap=1",
        "--end_fsync=1",
        "--invalidate=1",
    ]
    if cfg.get("ramp", 0) > 0:
        cmd.append(f"--ramp_time={cfg['ramp']}")
    if cfg.get("direct", True):
        cmd.append("--direct=1")
    if rw == "randrw":
        cmd.append(f"--rwmixread={cfg.get('read_mix', 70)}")
    return cmd


def _pct(d: dict, key: str) -> float:
    p = (d.get("clat_ns") or {}).get("percentile") or {}
    v = p.get(key) or p.get(key.replace(".000000", "")) or 0
    return round(float(v) / 1e6, 3)


def _parse_fio(raw: str) -> dict:
    """把 fio 的 JSON 汇总成一份读数。

    随机混合读写时 read/write 两组计数都存在，要按 IO 数加权平均延迟，
    否则 70/30 的负载会被算术平均成 50/50 的延迟。
    """
    try:
        j = (json.loads(raw).get("jobs") or [{}])[0]
        r, w = j.get("read") or {}, j.get("write") or {}
        io_total = int(r.get("io_bytes") or 0) + int(w.get("io_bytes") or 0)
        ios = float(r.get("total_ios") or 0) + float(w.get("total_ios") or 0)
        bw = int(r.get("bw_bytes") or 0) + int(w.get("bw_bytes") or 0)
        lat = 0.0
        for acc, n in ((r, float(r.get("total_ios") or 0)), (w, float(w.get("total_ios") or 0))):
            mean = float(((acc.get("lat_ns") or {}).get("mean") or 0)) / 1e6
            lat += mean * n
        lat = lat / ios if ios else 0.0
        return {
            "ok": True,
            "iops": round(float(r.get("iops") or 0) + float(w.get("iops") or 0), 1),
            "bw_bytes": bw,
            "lat_ms": round(lat, 3),
            "p95_ms": max(_pct(r, "95.000000"), _pct(w, "95.000000")),
            "p99_ms": max(_pct(r, "99.000000"), _pct(w, "99.000000")),
            "read_bytes": int(r.get("io_bytes") or 0),
            "total_bytes": io_total,
            "read_ratio": round(int(r.get("io_bytes") or 0) / io_total * 100, 1) if io_total else 0,
            "runtime_s": round(float(j.get("elapsed") or 0), 1),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _sample_loop(dev: str, total: int, stop: threading.Event) -> None:
    """测试期间每秒采一次目标设备的 IOPS / 带宽 / 延迟。

    直接读 /proc/diskstats，而不是解析 fio 的 stdout——那是测试工具自己的
    输出格式，会随版本变；设备计数器是稳定的、也和用户在「性能中心」里
    看到的数字同源。
    """
    prev = None
    t0 = time.time()
    while not stop.wait(1.0):
        cur = perf_mgr._read_diskstats().get(dev)
        if not cur:
            continue
        if prev:
            rate = perf_mgr._delta(prev, cur, 1.0)
            if rate:
                _state["live"].append(
                    {
                        "t": round(time.time() - t0, 1),
                        "iops": round(rate["iops"], 1),
                        "mbps": round((rate["read_bps"] + rate["write_bps"]) / 1048576, 2),
                        "lat_ms": round(rate["latency_ms"], 3),
                        "util": round(rate["util_percent"], 1),
                    }
                )
                _state["live"] = _state["live"][-LIVE_POINTS:]
        prev = cur
        _state["elapsed"] = int(time.time() - t0)
        _state["progress"] = min(99, int(_state["elapsed"] / max(1, total) * 100))


def _run(target: str, size_mb: int, cfg: dict) -> None:
    global _proc
    path = Path(target) / f"{FILE_PREFIX}{uuid.uuid4().hex[:12]}.bin"
    created = False
    stop = threading.Event()
    sampler = None
    try:
        _state.update(phase="准备测试文件", progress=0, live=[], summary=None, log=[], error="")
        _log(f"测试任务已启动（{MODES[cfg['mode']][0]} · {size_mb}MB · {cfg['runtime']}s）")
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        created = True
        try:
            os.ftruncate(fd, size_mb * 1048576)
        finally:
            os.close(fd)
        _log(f"已创建测试文件 {path.name}（{size_mb} MB，跑完自动删除）")

        if cfg.get("drop_cache", True):
            _state["phase"] = "丢弃页缓存"
            _log("正在丢弃页缓存（sync + drop_caches）…")
            if _drop_caches():
                _log("页缓存已丢弃，读测试将直接落到介质")
            else:
                _log("警告：页缓存丢弃失败，读结果可能偏高")

        dev = _target_dev(target)
        sampler = threading.Thread(target=_sample_loop, args=(dev, cfg["runtime"], stop), daemon=True)
        sampler.start()

        if cfg.get("ramp", 0) > 0:
            _log(f"预热阶段开始（{cfg['ramp']}s）")
        _state["phase"] = "测试中"
        cmd = _fio_cmd(path, size_mb, cfg)
        _proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = _proc.communicate(timeout=cfg["runtime"] + cfg.get("ramp", 0) + 180)
        rc = _proc.returncode
        _proc = None
        if cfg.get("ramp", 0) > 0:
            _log("预热完成，进入稳定采样窗口")
        if _cancel.is_set():
            _state["cancelled"] = True
            _log("测试被用户取消")
        elif rc != 0:
            _state["error"] = (err or out or f"fio 退出码 {rc}").strip()[:300]
            _log(f"fio 失败：{_state['error']}")
        else:
            summary = _parse_fio(out)
            _state["summary"] = summary
            if summary.get("ok"):
                _log(
                    f"测试完成：平均 {summary['iops']:.0f} IOPS、"
                    f"{summary['bw_bytes'] / 1048576:.1f} MB/s"
                )
            else:
                _log("结果解析失败")
        _state["progress"] = 100
        _state["phase"] = "已完成" if not _state["cancelled"] else "已取消"
    except subprocess.TimeoutExpired:
        _state["error"] = "fio 执行超时，已中止"
        _log("fio 执行超时，已中止")
        if _proc:
            _proc.kill()
    except Exception as e:
        _state["error"] = str(e)
        _log(f"异常：{e}")
    finally:
        stop.set()
        _proc = None
        if created:
            try:
                os.unlink(str(path))
                _log("测试文件已删除")
            except OSError:
                pass
        _state["running"] = False
        _state["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm_cfg(data: dict) -> dict:
    mode = str(data.get("mode") or "rand-rw")
    if mode not in MODES:
        mode = "rand-rw"
    bs = str(data.get("bs") or "4k").lower()
    if not re.fullmatch(r"\d+[kmg]", bs):
        bs = "4k"
    return {
        "mode": mode,
        "bs": bs,
        "iodepth": max(1, min(int(data.get("iodepth") or 32), 128)),
        "threads": max(1, min(int(data.get("threads") or 4), 64)),
        "runtime": max(10, min(int(data.get("runtime") or 60), MAX_RUNTIME)),
        "ramp": max(0, min(int(data.get("ramp") or 0), MAX_RAMP)),
        "read_mix": max(1, min(int(data.get("read_mix") or 70), 99)),
        "direct": bool(data.get("direct", True)),
        "drop_cache": bool(data.get("drop_cache", True)),
    }


def start(target: str, size_mb: int = 512, data: dict | None = None, status: dict | None = None) -> tuple[bool, str]:
    global _proc
    if _state["running"]:
        return _fail("已有基准测试在运行")
    ok, msg = validate(target, size_mb)
    if not ok:
        return False, msg
    cfg = _norm_cfg(data or {})
    if status:
        busy = _background_busy(status)
        if busy:
            return _fail(f"后台任务正在运行（{busy}），此时测出的数字没有参考价值")
    _cleanup_stale(target)
    with _lock:
        if _state["running"]:
            return _fail("已有基准测试在运行")
        _cancel.clear()
        _state.update(
            running=True, phase="启动中", progress=0, elapsed=0, total=cfg["runtime"],
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), finished_at="",
            cfg=cfg, cancelled=False, summary=None, live=[], log=[], error="",
        )
    t = threading.Thread(target=_run, args=(target, int(size_mb), cfg), daemon=True, name="io-bench")
    t.start()
    return True, "已开始"


def cancel() -> dict:
    _cancel.set()
    if _proc:
        try:
            _proc.kill()
        except OSError:
            pass
    return status()


def _cleanup_stale(target: str) -> int:
    """清理上次异常中断（服务被 kill）留下的测试文件。

    只删 30 分钟前、且严格匹配我们命名规则的文件，绝不碰别人的东西。
    """
    removed = 0
    now = time.time()
    try:
        for p in Path(target).glob(f"{FILE_PREFIX}*.bin"):
            try:
                if now - p.stat().st_mtime < 1800:
                    continue
                p.unlink()
                removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def status() -> dict:
    return dict(_state)


def targets() -> list[dict]:
    mounts = perf_mgr._mounts()
    root_dev = next((d for d, mps in mounts.items() if "/" in mps), "")
    root_disk = _disk_of(root_dev)
    out, seen = [], set()
    for d, mps in mounts.items():
        if _disk_of(d) == root_disk:
            continue
        for mp in mps:
            if mp in seen or mp.startswith(("/snap/", "/proc", "/sys", "/dev", "/run")):
                continue
            if any(mp == bad or mp.startswith(bad + "/") for bad in _FORBIDDEN_PREFIXES):
                continue
            if not os.path.ismount(mp):
                continue
            try:
                st = os.statvfs(mp)
            except OSError:
                continue
            free_mb = st.f_bavail * st.f_frsize / 1048576
            if free_mb < MIN_SIZE_MB * 1.5:
                continue
            seen.add(mp)
            out.append({"mountpoint": mp, "device": d, "free_mb": int(free_mb)})
    out.sort(key=lambda x: -x["free_mb"])
    return out


def modes() -> list[dict]:
    return [{"key": k, "label": v[0]} for k, v in MODES.items()]
