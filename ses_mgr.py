"""机箱背板（SES）真实槽位与指示灯状态。

磁盘和阵列的信息来自 RAID 控制器，那是**控制器视角**。控制器只报告它
认到的盘——一块插好但没上线的盘、一个接触不良的背板口，从 storcli 看
都是"这里什么都没有"。而背板本身（SCSI Enclosure Services）看得见物理
层面的真实情况：每个槽位是否插了盘、故障灯是否亮、定位灯是否亮、供电
是否正常。

两者的差异本身就是最有价值的信号：

  * 背板说 OK，控制器却没认到盘 → 接触不良 / 背板到卡的链路问题
  * 控制器认到盘，背板说空槽 → 元数据与硬件不一致，换盘前必须查清
  * 背板故障灯亮，阵列却报 Onln → 盘在硬扛，通常会先掉线

这些情况光看其中任何一边都发现不了。

**本模块只读。** `locate` / `fault` 这两个属性在 sysfs 里是可写的，但
控制器也能控制同一盏灯；两套机制同时写会出现"灯亮着但没人知道是谁点亮的"，
所以点灯统一走 storcli，这里只负责看。
"""

from __future__ import annotations

import os
from pathlib import Path

ENCLOSURE_ROOT = Path("/sys/class/enclosure")

# 只关心插盘的位置；SES 里还有电源、风扇、温度、控制器电子元件等元素类型。
# 措辞不统一：SES 规范写 "Array Device Slot"，Linux ses 驱动实际给的是
# "array device"，所以按关键词收，不按整串匹配。
_SLOT_HINTS = ("array device", "slot")


def _is_slot_type(t: str) -> bool:
    t = (t or "").lower()
    if "controller electronics" in t:
        return False
    return any(h in t for h in _SLOT_HINTS)


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def _to_int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def list_enclosures() -> list[dict]:
    """遍历 /sys/class/enclosure，返回每个背板的槽位明细。"""
    out: list[dict] = []
    if not ENCLOSURE_ROOT.exists():
        return out

    for enc in sorted(ENCLOSURE_ROOT.iterdir(), key=lambda p: p.name):
        elements = []
        try:
            children = sorted(
                (c for c in enc.iterdir() if c.is_dir() and c.name.isdigit()),
                key=lambda c: int(c.name),
            )
        except OSError:
            continue

        for el in children:
            slot_raw = _read(el / "slot")
            if not slot_raw:
                continue
            etype = _read(el / "type")
            status = _read(el / "status")
            elements.append(
                {
                    "elem": int(el.name),
                    "slot": _to_int(slot_raw, -1),
                    "type": etype,
                    "status": status,
                    "installed": status.lower() in ("ok", "critical", "non-critical", "unknown"),
                    "fault": _read(el / "fault") == "1",
                    "locate": _read(el / "locate") == "1",
                    "active": _read(el / "active") == "1",
                    "power_status": _read(el / "power_status"),
                }
            )

        # 驱动会把背板"支持但未实现"的槽位也列出来（status=unsupported，
        # 常见是 16 口背板报 48 个元素）。它们不是真实盘位，算进总数会让
        # "16 槽装了 4 块"变成"48 槽装了 4 块"，是纯粹的误导。
        cand = [e for e in elements if _is_slot_type(e["type"])]
        slots = [e for e in cand if e["status"].lower() != "unsupported"]
        unsupported = len(cand) - len(slots)
        out.append(
            {
                "device": enc.name,
                "vendor": _read(enc / "device" / "vendor"),
                "model": _read(enc / "device" / "model"),
                "rev": _read(enc / "device" / "rev"),
                "wwid": _read(enc / "device" / "wwid"),
                "elements": len(elements),
                "slots": slots,
                "slots_total": len(slots),
                "slots_unsupported": unsupported,
                "slots_installed": sum(1 for s in slots if s["installed"]),
                "fault_count": sum(1 for s in slots if s["fault"]),
                "locate_count": sum(1 for s in slots if s["locate"]),
            }
        )
    return out


def status() -> dict:
    enclosures = list_enclosures()
    return {
        "available": bool(enclosures),
        "enclosures": enclosures,
        "totals": {
            "enclosures": len(enclosures),
            "slots": sum(e["slots_total"] for e in enclosures),
            "installed": sum(e["slots_installed"] for e in enclosures),
            "faults": sum(e["fault_count"] for e in enclosures),
            "locating": sum(e["locate_count"] for e in enclosures),
        },
    }


def slot_map() -> dict[int, dict]:
    """{槽位号: 元素}。只有单背板时才做槽位合并。

    多背板时槽位号会在每个背板内重复（都从 0 开始），而控制器给的 slot
    不带背板信息，硬按槽位号合并会把两块背板的盘对错——宁可不合并。
    """
    enclosures = list_enclosures()
    if len(enclosures) != 1:
        return {}
    return {s["slot"]: s for s in enclosures[0]["slots"]}


def compare(disks: list[dict]) -> list[dict]:
    """把背板视角与控制器视角对一遍，返回不一致项。"""
    sm = slot_map()
    if not sm:
        return []
    ctrl_slots: dict[int, dict] = {}
    for d in disks or []:
        try:
            ctrl_slots[int(d.get("slot"))] = d
        except (TypeError, ValueError):
            continue

    issues = []
    seen_ctrl = {s["slot"] for s in sm.values() if s["installed"]}
    for slot in sorted(seen_ctrl | set(ctrl_slots)):
        ses = sm.get(slot)
        ctrl = ctrl_slots.get(slot)
        if ses and ses["installed"] and not ctrl:
            issues.append(
                {
                    "slot": slot,
                    "level": "warn",
                    "title": f"槽位 {slot}：背板看到盘，RAID 控制器没有识别",
                    "detail": "背板报告该槽位已插盘（status OK），但控制器物理盘列表里没有它。"
                    "常见原因是背板到卡的链路接触不良、盘未上电，或盘刚插入尚未枚举。",
                    "advice": "重新插拔或换一个背板口验证；持续存在时检查背板线缆与背板供电。",
                }
            )
        if ctrl and ses and not ses["installed"]:
            issues.append(
                {
                    "slot": slot,
                    "level": "warn",
                    "title": f"槽位 {slot}：控制器认为有盘，背板报告未安装",
                    "detail": f"控制器列表里该槽位状态为 {ctrl.get('state') or '未知'}，"
                    f"但背板 status 为 “{ses['status']}”。",
                    "advice": "这种不一致会影响换盘的判断，请人工确认后再操作。",
                }
            )
        if ctrl and ses and ses["fault"] and str(ctrl.get("state") or "").lower() in ("onln", "optl"):
            issues.append(
                {
                    "slot": slot,
                    "level": "warn",
                    "title": f"槽位 {slot}：背板故障灯亮，阵列仍认为正常",
                    "detail": f"控制器状态 {ctrl.get('state')}，但背板 fault 位为 1。",
                    "advice": "盘通常还能读，但多数会在之后掉线；建议尽快安排计划内更换。",
                }
            )
    return issues
