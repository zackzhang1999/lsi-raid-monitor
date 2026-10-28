"""IO 慢的一键诊断。

「IO 很慢」是最常见、也最难直接回答的一条反馈：同一个现象背后可能是
完全不同的原因，处置方式也完全不同——

  * 阵列正在重建 / 一致性检查 / 巡读  → 等它跑完，或把窗口挪到夜里
  * 设备利用率接近饱和、队列堆积       → 加盘、换更快的介质、错峰
  * 延迟很高但设备不忙                 → 路径问题（线缆/背板/控制器），不是介质
  * 大量小 IO 随机访问                 → 工作负载特性，不是故障
  * 某个进程在狂写                     → 找到它，限流或挪走
  * 文件系统快满了                     → 碎片与写放大，先扩容

所以这个模块不做"再报一遍数字"，而是把这几类因素**排序**：先看有没有
能一句话解释全局的原因（后台任务），再看设备侧，最后看负载侧。每条结论
都必须带证据数字，否则运维没法据此做决定。

采样成本约 2 秒（两次 1 秒间隔的取值求差），只在用户点「开始诊断」时跑，
不做常驻轮询。

**本模块严格只读，绝不产生任何写 IO。**

性能诊断和性能压测是两件事。压测要写盘、要预热、要打满队列，那是在客户
的生产数据上做实验；这条代码路径只允许"观察"：

  * /proc/diskstats、/proc/<pid>/io、/proc/mounts、/proc/meminfo —— 只读
  * statvfs —— 只查询，不解析目录、不访问文件内容
  * /sys/block、/sys/class/enclosure —— 只读
  * storcli `show` —— 查询命令，不改配置

这里没有 dd、没有 fio、没有"测试文件"。以后要加压测，必须另立模块、
默认关闭、并且在 UI 上明确写清"会把数据盘写满"，绝不能混进诊断路径。
"""

from __future__ import annotations

import os
import time

import perf_mgr

# 判定阈值。数字不是拍脑袋：util 85% 以上、队列深度 2 以上，
# 随机 IO 的延迟曲线已经明显变陡；20ms 对 SSD 是异常，对机械盘属正常，
# 所以高延迟判定额外要求"设备不忙"，避免把机械盘误报成故障。
UTIL_BUSY = 85.0
UTIL_SATURATED = 95.0
QUEUE_BUSY = 2.0
LATENCY_HIGH_MS = 20.0
SMALL_IO_KB = 16.0
TEMP_THROTTLE = 60
FS_NEAR_FULL = 90.0

_SEVERITY_ORDER = {"crit": 0, "warn": 1, "info": 2, "ok": 3}


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _read_proc_io() -> dict[int, tuple[int, int]]:
    """{pid: (read_bytes, write_bytes)}。

    /proc/<pid>/io 里的 read_bytes/write_bytes 是**真正落到块设备**的字节，
    不含命中页缓存的读写——正是"谁在压磁盘"要看的口径。纯 CPU / 缓存
    命中的进程在这里应该是 0，那是对的。
    """
    out: dict[int, tuple[int, int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/io", encoding="utf-8") as f:
                vals = {}
                for line in f:
                    k, _, v = line.partition(":")
                    vals[k.strip()] = v.strip()
            out[pid] = (_to_int(vals.get("read_bytes")), _to_int(vals.get("write_bytes")))
        except (OSError, ValueError):
            continue
    return out


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = [p.decode("utf-8", "replace") for p in f.read().split(b"\0") if p]
        return " ".join(parts)[:160]
    except OSError:
        return ""


def _comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def top_processes(interval: float = 1.0, limit: int = 8) -> list[dict]:
    a = _read_proc_io()
    t0 = time.time()
    time.sleep(max(0.3, min(interval, 3.0)))
    b = _read_proc_io()
    dt = max(0.001, time.time() - t0)

    rows = []
    for pid, (rb, wb) in b.items():
        prev = a.get(pid)
        if not prev:
            continue
        dr = rb - prev[0]
        dw = wb - prev[1]
        # 计数器回绕或进程复用会让差值变负，丢弃这次样本
        if dr < 0 or dw < 0:
            continue
        if dr + dw <= 0:
            continue
        rows.append(
            {
                "pid": pid,
                "name": _comm(pid) or "?",
                "cmdline": _cmdline(pid),
                "read_bps": dr / dt,
                "write_bps": dw / dt,
                "total_bps": (dr + dw) / dt,
            }
        )
    rows.sort(key=lambda r: r["total_bps"], reverse=True)
    return rows[:limit]


def _real_mounts() -> list[dict]:
    """真实文件系统的用量。排除伪文件系统，否则 /proc、/snap 会混进
    "接近写满"的判定里。"""
    out = []
    seen = set()
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        p = line.split()
        if len(p) < 3 or not p[0].startswith("/dev/"):
            continue
        dev, mp = p[0], p[1]
        if mp in seen or mp.startswith(("/snap/", "/proc", "/sys", "/dev", "/run/")):
            continue
        seen.add(mp)
        try:
            st = os.statvfs(mp)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        if not total:
            continue
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        out.append(
            {
                "mountpoint": mp,
                "device": dev,
                "size": total,
                "used": used,
                "use_percent": round(used / total * 100, 1),
            }
        )
    return out


def _mem_available_percent() -> float | None:
    try:
        total = avail = None
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = _to_int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = _to_int(line.split()[1])
        if total and avail is not None:
            return round(avail / total * 100, 1)
    except OSError:
        pass
    return None


SYSTEM_MOUNTS = ("/", "/boot/efi", "/boot", "[SWAP]")


def _is_usb_system_disk(row: dict, mounts: list[dict]) -> bool:
    """是不是"通过 USB 连接、承载系统分区"的那块盘。

    这类盘属于设备自身的操作系统，不是用户的数据面。把它算进存储性能
    判定会有两个坏处：一是总量被系统噪音稀释，二是 USB 的结构性延迟
    会盖掉数据阵列真正的瓶颈。所以默认把它排除在判定之外，但单独列出来
    ——排除不等于隐瞒，系统盘慢本身仍是需要知道的事实。
    """
    if row.get("bus") != "usb":
        return False
    dev = f"/dev/{row.get('name')}"
    for m in mounts:
        d = str(m.get("device") or "")
        if not d.startswith(dev):
            continue
        mp = str(m.get("mountpoint") or "")
        if mp in SYSTEM_MOUNTS or mp.startswith("/boot"):
            return True
    return False


def _human(n) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit, div in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
        if v >= div:
            return f"{v / div:.1f} {unit}"
    return f"{v:.0f} B/s"


def diagnose(status: dict | None = None) -> dict:
    """跑一次诊断，返回按可能性排序的结论。"""
    live = perf_mgr.live(1.0)
    procs = top_processes(1.0)
    st = status or {}
    disks = st.get("physical_disks") or []
    ctrls = st.get("controllers") or []
    mounts = _real_mounts()
    mem_avail = _mem_available_percent()

    findings: list[dict] = []

    def add(level, title, detail, advice, weight=0):
        findings.append(
            {"level": level, "title": title, "detail": detail, "advice": advice, "weight": weight}
        )

    # ---- 1. 阵列后台任务：这是"盘阵突然变慢"最常见的单一原因 ----
    rebuilding = [d for d in disks if d.get("rebuild_progress") is not None]
    erasing = [d for d in disks if d.get("erase_progress") is not None]
    cc_running, pr_running = [], []
    for c in ctrls:
        m = c.get("maintenance") or {}
        cc = (m.get("consistency_check") or {}).get("state") or ""
        pr = (m.get("patrol_read") or {}).get("state") or ""
        if cc and cc.lower() not in ("stopped", "none", ""):
            cc_running.append((c, cc))
        if pr and pr.lower() not in ("stopped", "none", ""):
            pr_running.append((c, pr))

    for d in rebuilding:
        add(
            "crit",
            f"{d.get('full_label') or d.get('label')} 正在重建（{d.get('rebuild_progress')}%）",
            f"重建期间控制器会把大量带宽用于写回冗余数据，业务 IO 变慢是必然结果。"
            f"剩余约 {d.get('rebuild_eta') or '未知'}。",
            "先确认重建进度在推进；如果业务不可接受，可评估暂停重建——但降级状态下暂停会延长无保护窗口，务必先确认风险。",
            weight=100,
        )
    for d in erasing:
        add(
            "warn",
            f"{d.get('full_label') or d.get('label')} 正在擦除（{d.get('erase_progress')}%）",
            "擦除会持续占用该盘带宽。",
            "擦除期间避免对该盘所在阵列做性能敏感的操作。",
            weight=90,
        )
    for c, s in cc_running:
        add(
            "warn",
            f"C{c.get('ctrl')} 一致性检查运行中（{s}）",
            "CC 会顺序扫描整个阵列，是典型的『后台任务把 IO 拖慢』场景。",
            "如果你只是在做性能排查，可以临时停止 CC，排查完再恢复；生产环境建议把 CC 安排在业务低谷。",
            weight=85,
        )
    for c, s in pr_running:
        add(
            "warn",
            f"C{c.get('ctrl')} 巡读运行中（{s}）",
            "巡读同样是全盘扫描，会与业务 IO 争抢带宽。",
            "同上：确认是计划内任务，必要时暂停或改到夜间。",
            weight=80,
        )

    # ---- 2. 设备侧：饱和 / 高延迟低利用 ----
    all_rows = live.get("rows") or []
    # 数据面 = 全部设备 - USB 系统盘。后续所有判定与总量都只看数据面，
    # 避免"设备自己的操作系统盘"把存储性能结论带偏。
    excluded = [r for r in all_rows if _is_usb_system_disk(r, mounts)]
    excluded_names = {r["name"] for r in excluded}
    rows = [r for r in all_rows if r["name"] not in excluded_names]
    busy = [r for r in rows if r.get("util_percent", 0) >= UTIL_BUSY and r.get("iops", 0) > 1]
    for r in sorted(busy, key=lambda x: x["util_percent"], reverse=True)[:3]:
        level = "crit" if r["util_percent"] >= UTIL_SATURATED else "warn"
        add(
            level,
            f"{r['name']} 利用率 {r['util_percent']:.0f}%，队列 {r['queue_depth']:.1f}",
            f"当前 {r['iops']:.0f} IOPS、{_human(r['read_bps'] + r['write_bps'])}、"
            f"平均延迟 {r['latency_ms']:.1f}ms。设备基本没有空闲时间，延迟上升是排队造成的。",
            "这是容量/性能不足，不是故障：错峰、拆分负载、增加盘或换更快的介质。",
            weight=70,
        )
    slow_calm = [
        r for r in rows
        if r.get("latency_ms", 0) >= LATENCY_HIGH_MS
        and r.get("util_percent", 0) < 50
        and r.get("iops", 0) >= 10
    ]
    for r in sorted(slow_calm, key=lambda x: x["latency_ms"], reverse=True)[:3]:
        add(
            "warn",
            f"{r['name']} 延迟 {r['latency_ms']:.1f}ms，但利用率只有 {r['util_percent']:.0f}%",
            "设备并不忙却延迟很高，通常不是负载问题，而是链路或介质本身："
            "背板/线缆接触不良、SAS 降速、盘进入错误重试、缓存策略不当。",
            "先看该盘的重映射/待定扇区是否在涨；再查 SES 背板状态与链路速率。",
            weight=75,
        )

    # ---- 3. 负载侧：小 IO、进程热点 ----
    # USB 链路单列一条：同样的 IOPS 下延迟可能是 SATA 的几十倍，
    # 而这件事从速率指标上完全看不出来。
    # USB 盘只要上面挂了文件系统就值得提一句——它是结构性上限，不是
    # 只有忙的时候才成立；忙的时候升级成"关注"，闲的时候只作线索。
    usb_devs = [r for r in rows if r.get("bus") == "usb"]
    for r in sorted(usb_devs, key=lambda x: x.get("util_percent", 0), reverse=True)[:2]:
        is_root = any(m["device"] == f"/dev/{r['name']}" or m["device"].startswith(f"/dev/{r['name']}")
                      for m in mounts)
        busy = r.get("iops", 0) >= 5 or r.get("util_percent", 0) >= 20
        if not is_root and not busy:
            continue
        add(
            "warn" if busy else "info",
            f"{r['name']} 通过 USB 连接" + ("，且承载系统盘" if is_root else ""),
            f"当前 {r['iops']:.0f} IOPS、利用率 {r['util_percent']:.0f}%、"
            f"平均延迟 {r['latency_ms']:.1f}ms。USB 存储的队列深度和延迟都远不如 SATA/NVMe，"
            "同样的负载在 USB 上会表现为『盘没坏但就是慢』。",
            "把系统盘换到板载 SATA/NVMe，或至少确认 USB 走的是 3.0 以上端口且未被集线器共享。",
            weight=68 if busy else 25,
        )

    tot_ios = sum(r.get("iops", 0) for r in rows)
    tot_bytes = sum(r.get("read_bps", 0) + r.get("write_bps", 0) for r in rows)
    if tot_ios >= 50 and tot_bytes > 0:
        avg_kb = tot_bytes / tot_ios / 1024
        if avg_kb < SMALL_IO_KB:
            add(
                "info",
                f"平均 IO 尺寸仅 {avg_kb:.1f} KB，以小 IO 随机访问为主",
                "小 IO 的瓶颈在每秒操作数（IOPS）而不是带宽，介质随机性能决定了上限。",
                "评估能否合并写、加大预读、或用 SSD 缓存（本项目已支持 bcache）来提升命中率。",
                weight=40,
            )

    if procs and tot_bytes > 0:
        top = procs[0]
        share = top["total_bps"] / tot_bytes * 100 if tot_bytes else 0
        if top["total_bps"] > 5 * 1e6 or share > 30:
            add(
                "info",
                f"进程热点：{top['name']}（PID {top['pid']}）占 {_human(top['total_bps'])}",
                (top.get("cmdline") or "")[:150] or "（无命令行）",
                "确认这个进程是否是预期的业务负载；不是的话再查它为什么在写盘（日志、临时文件、重建索引）。",
                weight=50,
            )

    # ---- 4. 容量 / 温度 / 内存 ----
    full = [m for m in mounts if m["use_percent"] >= FS_NEAR_FULL]
    for m in full:
        add(
            "warn",
            f"{m['mountpoint']} 已用 {m['use_percent']}%",
            "文件系统接近写满时，空闲块碎片化、写放大和元数据压力都会显著上升，"
            "表现为同样的负载下 IO 明显变慢。",
            "优先扩容或清理；不要等到 100% 再处理，那时连删除都可能失败。",
            weight=65,
        )
    hot = [d for d in disks if isinstance(d.get("temperature"), int) and d["temperature"] >= TEMP_THROTTLE]
    for d in hot[:3]:
        add(
            "warn",
            f"{d.get('full_label') or d.get('label')} 温度 {d['temperature']}°C",
            "固态盘在高温下会主动降速（thermal throttling），"
            "这是「看起来盘没问题但就是慢」的典型原因。",
            "检查风道、风扇转速与机房温度。",
            weight=60,
        )
    if mem_avail is not None and mem_avail < 10:
        add(
            "warn",
            f"可用内存仅剩 {mem_avail}%",
            "页缓存被挤压后，原本能命中缓存的读会落到磁盘上，IO 量会被动放大。",
            "排查内存占用；必要时降低缓存或扩容内存。",
            weight=55,
        )

    hard = [f for f in findings if f["level"] in ("crit", "warn")]
    if not hard:
        # 只有 info（比如 USB 链路这类结构性线索）时，结论仍然是"没抓到瓶颈"。
        # 把线索当结论报出去，会让"其实没问题"看起来像"有问题"。
        summary = (
            f"未发现明显瓶颈：当前 {tot_ios:.0f} IOPS、{_human(tot_bytes)}，"
            f"最高利用率 {max((r.get('util_percent', 0) for r in rows), default=0):.0f}%。"
        )
        if findings:
            summary += f"（另有 {len(findings)} 条结构性线索，见下方）"
        add(
            "ok",
            "当前没有可归因的瓶颈",
            "设备和后台任务都处于正常范围。若用户仍反馈慢，问题可能在上层"
            "（应用、网络、锁竞争）而不是存储本身。",
            "可以对照「性能趋势」看历史曲线，确认慢是持续性的还是某个时间段。",
            weight=0,
        )
    else:
        top = sorted(hard, key=lambda f: (-f["weight"], _SEVERITY_ORDER.get(f["level"], 9)))[0]
        summary = top["title"]

    findings.sort(key=lambda f: (-f["weight"], _SEVERITY_ORDER.get(f["level"], 9)))
    severity = "crit" if any(f["level"] == "crit" for f in findings) else (
        "warn" if any(f["level"] == "warn" for f in findings) else "ok"
    )
    return {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "severity": severity,
        "findings": findings,
        "devices": rows,
        "processes": procs,
        "totals": {
            "iops": round(tot_ios, 1),
            "bytes_per_sec": round(tot_bytes, 1),
            "avg_io_kb": round(tot_bytes / tot_ios / 1024, 2) if tot_ios else None,
        },
        # 被排除在判定之外的设备（USB 系统盘）。单独返回而不是丢掉：
        # 排除是为了让数据面的结论干净，不是为了假装它不存在。
        "excluded": [
            {
                "name": r["name"],
                "bus": r.get("bus", ""),
                "iops": round(r.get("iops", 0), 1),
                "latency_ms": round(r.get("latency_ms", 0), 2),
                "reason": "USB 连接且承载系统分区",
            }
            for r in excluded
        ],
        "mounts": mounts,
        "mem_available_percent": mem_avail,
    }
