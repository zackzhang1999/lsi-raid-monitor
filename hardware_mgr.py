"""硬件环境监控：风扇、温度、电压、机箱入侵。

磁盘和阵列是"数据面"，机箱本身才是"物理面"。一台存储最容易被忽略的
故障模式恰恰在这里：某个风扇停转 → 进风温度爬升 → 磁盘温度集体上升 →
先是降速、然后是 SMART 告警、最后是掉盘。等磁盘告警出现时，根因已经
发生很久了。所以风扇转速和进风温度必须是一等公民，而不是"能读到就显示"。

数据来源按可用性回退：

  1. `ipmitool sdr` —— 走 BMC，风扇/温度/电压/机箱入侵都能拿。BMC 自己
     带门限判定，所以**优先信它给的 status（ok/nc/cr/nr）**，而不是我们
     在应用层重新猜阈值。厂商的门限是按这颗 CPU、这个机箱调的。
  2. `/sys/class/hwmon` —— 没有 BMC 时的兜底（CPU 温度等）。

两个工程上的坑：

* `ipmitool sdr` 一次要 2~3 秒，绝不能放在请求路径上同步调用，所以做
  30 秒 TTL 缓存 + 后台刷新。
* 未安装的风扇位会报 `No Reading` 且状态是 `ns`，这不是故障；而**装了的
  风扇报 0 RPM 才是**（BMC 有时不把停转判成 nc）。这两种要分开。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))

IPMITOOL = os.environ.get("IPMITOOL_PATH", "/usr/bin/ipmitool")
CACHE_TTL = 30
IPMI_TIMEOUT = 12

_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()
_refresh_lock = threading.Lock()

# BMC 的传感器状态码 -> 我们的级别。ns/na/nf 是"这个传感器位没装东西"，
# 不当故障；BMC 门限报警（nc/cr/nr）原样透传。
_STATUS_LEVEL = {
    "nc": "warn",
    "cr": "crit",
    "nr": "crit",
    "unr": "crit",
}
_STATUS_TEXT = {
    "ok": "正常",
    "nc": "非临界",
    "cr": "临界",
    "nr": "不可恢复",
    "ns": "无读数",
    "na": "不适用",
    "nf": "不存在",
}

# 环境温度类传感器的门限比 BMC 的保守得多：BMC 常在 90°C 才报临界，
# 而机箱进风温度超过 40°C 就已经在把磁盘寿命往回拉了。
_AMBIENT_KEYS = ("system temp", "peripheral", "inlet", "ambient", "front")
_CPU_KEYS = ("cpu", "soc", "vrm", "dimm", "pch", "vrmabcd", "vrmefgh")

AMBIENT_WARN, AMBIENT_CRIT = 50.0, 62.0
CPU_WARN, CPU_CRIT = 85.0, 95.0
BOARD_WARN, BOARD_CRIT = 75.0, 88.0


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _reading_kind(text: str, name: str = "") -> tuple[str, float | None]:
    """把 IPMI 的读数文本拆成 (类型, 数值)。

    空读数的传感器（未安装的风扇位、空内存槽）在 BMC 里是 `no reading`，
    这时只能靠名字判断归属，否则它们会被归到"离散量"里，界面上看起来像
    一堆莫名其妙的条目，而不是"这个槽位没装东西"。
    """
    t = (text or "").strip()
    n = (name or "").lower()
    if not t or t.startswith("0x") or t.lower() in ("disabled", "enabled", "no reading"):
        if "fan" in n:
            return "fan", None
        if "temp" in n:
            return "temp", None
        if any(k in n for k in ("volt", "vcc", "vdd", "vbat", "vrm", "socrun", "socdual")):
            return "voltage", None
        return "discrete", None
    m = re.match(r"^(-?[\d.]+)\s*(.*)$", t)
    if not m:
        return "discrete", None
    val = _to_float(m.group(1))
    unit = m.group(2).strip().lower()
    if "rpm" in unit:
        return "fan", val
    if "degree" in unit or unit in ("c", "celsius"):
        return "temp", val
    if "volt" in unit:
        return "voltage", val
    if "watt" in unit:
        return "power", val
    if "amp" in unit:
        return "current", val
    if "percent" in unit:
        return "other", val
    return "other", val


def _temp_band(name: str) -> tuple[float, float]:
    n = name.lower()
    if any(k in n for k in _AMBIENT_KEYS):
        return AMBIENT_WARN, AMBIENT_CRIT
    if any(k in n for k in _CPU_KEYS):
        return CPU_WARN, CPU_CRIT
    return BOARD_WARN, BOARD_CRIT


def _read_ipmi() -> tuple[bool, str, list[dict]]:
    if not (os.path.isfile(IPMITOOL) and os.access(IPMITOOL, os.X_OK)):
        return False, "未安装 ipmitool", []
    try:
        proc = subprocess.run(
            [IPMITOOL, "sdr"],
            capture_output=True,
            timeout=IPMI_TIMEOUT,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return False, "ipmitool 超时（BMC 无响应？）", []
    except Exception as e:
        return False, str(e), []
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return False, err[0] if err else f"ipmitool 返回 {proc.returncode}", []

    sensors = []
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split("|")]
        # ipmitool 有两种输出格式，取决于调用方式：
        #   紧凑  `CPU Temp         | 34 degrees C | ok`            （`sdr`）
        #   扩展  `FAN2 | 42h | ok | 29.2 | 3500 RPM`               （`sdr type fan`）
        # 判据是第二列像不像传感器 ID（42h 这种十六进制），不能按列数硬判。
        if len(parts) >= 5 and re.fullmatch(r"[0-9a-fA-F]+h", parts[1] or ""):
            name, sid, status, entity, reading = parts[0], parts[1], parts[2], parts[3], parts[4]
        elif len(parts) >= 3:
            name, reading, status = parts[0], parts[1], parts[2]
            sid, entity = "", ""
        else:
            continue
        if not name:
            continue
        kind, value = _reading_kind(reading, name)
        sensors.append(
            {
                "name": name,
                "id": sid,
                "status": status.lower(),
                "entity": entity,
                "raw": reading,
                "kind": kind,
                "value": value,
            }
        )
    return True, "", sensors


def _read_hwmon() -> list[dict]:
    """没有 BMC 时的兜底：/sys/class/hwmon 里的温度通道。"""
    out = []
    root = Path("/sys/class/hwmon")
    if not root.exists():
        return out
    for dev in sorted(root.iterdir()):
        try:
            chip = (dev / "name").read_text().strip()
        except OSError:
            continue
        for f in sorted(dev.glob("temp*_input")):
            try:
                raw = int(f.read_text().strip())
            except (OSError, ValueError):
                continue
            label = f.name.replace("_input", "")
            lab_file = dev / f"{label}_label"
            if lab_file.exists():
                try:
                    label = lab_file.read_text().strip()
                except OSError:
                    pass
            out.append(
                {
                    "name": f"{chip} {label}",
                    "id": f"{dev.name}/{label}",
                    "status": "ok",
                    "entity": "hwmon",
                    "raw": f"{raw / 1000:.1f} degrees C",
                    "kind": "temp",
                    "value": round(raw / 1000, 1),
                }
            )
    return out


def read_sensors(force: bool = False) -> dict:
    """读取全部传感器。带 TTL 缓存——ipmitool 单次 2~3 秒，不能每请求都跑。"""
    now = time.time()
    with _lock:
        if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
            return _cache["data"]

    # 刷新串行化：多开几个标签页时不能让几个请求同时去跑 ipmitool，
    # 那既拖慢 BMC，也让"30 秒缓存"形同虚设。
    with _refresh_lock:
        with _lock:
            if not force and _cache["data"] is not None and time.time() - _cache["at"] < CACHE_TTL:
                return _cache["data"]  # 等锁期间别人已经刷好了

        ok, err, sensors = _read_ipmi()
        source = "ipmi"
        if not ok:
            # BMC 不可用（虚拟机、没插管理网）不该让卡片整块消失：
            # 退到 hwmon 至少还能给 CPU 温度，并明确标注降级原因。
            sensors = _read_hwmon()
            source = "hwmon" if sensors else "none"

        data = {
            "ok": ok or bool(sensors),
            "source": source,
            "error": "" if ok else err,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sensors": sensors,
            "groups": _group(sensors),
        }
        with _lock:
            if ok or sensors:
                _cache["at"] = time.time()
                _cache["data"] = data
        return data


def _group(sensors: list[dict]) -> dict:
    fans, temps, volts, discrete = [], [], [], []
    for s in sensors:
        if s["kind"] == "fan":
            fans.append(s)
        elif s["kind"] == "temp":
            temps.append(s)
        elif s["kind"] in ("voltage", "power", "current"):
            volts.append(s)
        else:
            discrete.append(s)
    fans.sort(key=lambda x: x["name"])
    temps.sort(key=lambda x: x["name"])
    return {"fans": fans, "temps": temps, "power": volts, "discrete": discrete}


def evaluate(data: dict | None = None) -> dict:
    """把原始读数变成结论：哪些正常、哪些要处理。"""
    data = data or read_sensors()
    g = data.get("groups") or {}
    findings: list[dict] = []

    fans = g.get("fans") or []
    # 未装风扇位报 "No Reading"，不算故障；装了却停转才是。
    spinning = [f for f in fans if isinstance(f.get("value"), (int, float)) and f["value"] > 0]
    stalled = [
        f
        for f in fans
        if isinstance(f.get("value"), (int, float))
        and f["value"] <= 0
        and f["status"] in ("ok", "ns", "na")
    ]
    for f in fans:
        lvl = _STATUS_LEVEL.get(f["status"])
        if lvl:
            findings.append(
                {
                    "key": f"fan:{f['name']}",
                    "level": lvl,
                    "title": f"风扇 {f['name']} 报告异常（{_STATUS_TEXT.get(f['status'], f['status'])}）",
                    "detail": f"当前读数 {f['raw']}。",
                    "advice": "检查风扇是否卡阻或供电异常；机箱过热会连带影响磁盘寿命。",
                }
            )
    for f in stalled:
        findings.append(
            {
                "key": f"fan:{f['name']}",
                "level": "warn",
                "title": f"风扇 {f['name']} 转速为 0",
                "detail": f"该风扇位有读数但转速为 0，说明装了却停转（{len(spinning)} 个风扇在转）。",
                "advice": "BMC 有时不把停转判成非临界，需要人工确认；停机更换前留意其余风扇转速。",
            }
        )

    for t in g.get("temps") or []:
        lvl = _STATUS_LEVEL.get(t["status"])
        band = _temp_band(t["name"])
        val = t.get("value")
        if lvl is None and isinstance(val, (int, float)):
            # 厂商门限偏晚，这里用更保守的应用层门限补一道
            if val >= band[1]:
                lvl = "crit"
            elif val >= band[0]:
                lvl = "warn"
        if lvl:
            findings.append(
                {
                    "key": f"temp:{t['name']}",
                    "level": lvl,
                    "title": f"{t['name']} 温度 {t['raw']}",
                    "detail": f"应用层门限 {band[0]:.0f}°C 警告 / {band[1]:.0f}°C 临界"
                    f"（BMC 状态 {_STATUS_TEXT.get(t['status'], t['status'])}）。",
                    "advice": "检查风道、滤网与机房空调；进风温度每升高 10°C，磁盘寿命约减半。",
                }
            )

    for p in g.get("power") or []:
        lvl = _STATUS_LEVEL.get(p["status"])
        if lvl:
            findings.append(
                {
                    "key": f"pwr:{p['name']}",
                    "level": lvl,
                    "title": f"电源读数 {p['name']} 异常（{_STATUS_TEXT.get(p['status'], p['status'])}）",
                    "detail": f"当前读数 {p['raw']}。",
                    "advice": "冗余电源中一路掉电时，剩下的那路一旦再故障就是整机断电，尽快更换。",
                }
            )

    for d in g.get("discrete") or []:
        name = d["name"].lower()
        # 机箱入侵是物理安全事件，单独提级
        if "intru" in name:
            val = str(d.get("raw") or "").lower()
            if val not in ("0x0", "0x00", "0", ""):
                findings.append(
                    {
                        "key": "chassis:intrusion",
                        "level": "warn",
                        "title": "机箱盖被打开过",
                        "detail": f"机箱入侵传感器当前值 {d['raw']}。",
                        "advice": "确认是计划内维护；非计划内的开盖要按安全事件处理。",
                    }
                )
        else:
            lvl = _STATUS_LEVEL.get(d["status"])
            if lvl:
                findings.append(
                    {
                        "key": f"disc:{d['name']}",
                        "level": lvl,
                        "title": f"{d['name']} 状态异常（{_STATUS_TEXT.get(d['status'], d['status'])}）",
                        "detail": f"当前读数 {d['raw']}。",
                        "advice": "",
                    }
                )

    lvls = [f["level"] for f in findings]
    severity = "crit" if "crit" in lvls else "warn" if "warn" in lvls else "ok"
    return {
        "ok": data.get("ok"),
        "source": data.get("source"),
        "error": data.get("error", ""),
        "at": data.get("at", ""),
        "severity": severity,
        "groups": g,
        "findings": findings,
        "counts": {
            "fans": len(fans),
            "fans_spinning": len(spinning),
            "temps": len(g.get("temps") or []),
            "power": len(g.get("power") or []),
        },
    }


def check_alerts(assessment: dict | None = None) -> None:
    """把硬件结论送进告警闭环。延迟导入避免循环依赖。"""
    import alert_center
    import lsi_alert

    a = assessment or evaluate()
    if not a.get("ok"):
        # BMC 读不到不算"硬件故障"，是一条采集异常
        lsi_alert._flag(
            "hw:unavailable",
            "机箱传感器不可读",
            f"原因：{a.get('error') or '未知'}。风扇转速、进风温度、电源状态在恢复前都不受监控。",
            "warning",
        )
        return
    lsi_alert._clear("hw:unavailable")

    active = set()
    for f in a.get("findings") or []:
        key = f"hw:{f['key']}"
        active.add(key)
        lsi_alert._flag(
            key,
            f["title"],
            f"{f['detail']} {f.get('advice', '')}".strip(),
            "error" if f["level"] == "crit" else "warning",
        )
    # 恢复判定必须从告警中心当前的未处理列表里反查，而不是靠进程内记忆：
    # 服务重启后进程内集合是空的，那种写法会让旧故障永远不被清除。
    try:
        for row in alert_center.list_alerts(status="open", limit=500):
            key = str(row.get("key") or "")
            if key.startswith("hw:") and key not in active:
                lsi_alert._clear(key)
    except Exception:
        pass
