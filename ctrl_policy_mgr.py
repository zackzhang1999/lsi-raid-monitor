"""控制器策略、VD 策略、PHY 错误计数与 VPD 导出。

这一层把 storcli 里"散落在十几个 set 子命令上"的策略收敛成一张表：

  * 后台任务速率（rebuildrate / ccrate / prrate）——重建时业务变慢的第一处置手段
  * 自动重建与回拷（autorebuild / copyback）——故障恢复是否自动发生
  * 一组控制器开关（cachebypass / ncq / foreignautoimport …）
  * VD 级策略（访问策略 / IO 策略 / UNMAP / 主机访问模式）
  * PHY 与错误计数器（读 + 复位）——区分"介质坏"和"链路坏"
  * VPD 导出（售后换件时的资产凭证）

三条设计原则：

1. **读写分离**：`read_*` 只跑 `show`，不碰任何状态；写操作全部返回
   待执行命令，由 web_server 交给任务引擎串行执行并留痕。
2. **能力探测**：同一套代码要跑在不同代际的卡上，某条策略不支持时
   返回"不支持"而不是抛异常——否则一个旧卡会让整页打不开。
3. **风险标注**：`Blocked` 访问策略、关闭自动重建这类操作会让业务中断
   或降低可用性，在数据层就标出来，UI 才知道该不该弹二次确认。
"""

from __future__ import annotations

import re

# key -> (显示名, 类型, 取值, 命令模板, 风险)
# 类型：int=数值，onoff=开关，enum=枚举
CTRL_KEYS = {
    "rebuildrate": ("重建速率", "int", (0, 100), "rebuildrate={v}", ""),
    "ccrate": ("一致性检查速率", "int", (0, 100), "ccrate={v}", ""),
    "prrate": ("巡读速率", "int", (0, 100), "prrate={v}", ""),
    "cacheflushint": ("缓存刷新间隔（秒）", "int", (1, 60), "cacheflushint={v}", ""),
    "autorebuild": ("自动重建", "onoff", ("on", "off"), "autorebuild={v}", "关闭后，盘重新插回也不会自动重建"),
    "copyback": ("自动回拷", "onoff", ("on", "off"), "copyback={v} type=ctrl", ""),
    "cachebypass": ("缓存旁路", "onoff", ("on", "off"), "cachebypass={v}", ""),
    "ncq": ("NCQ（原生命令队列）", "onoff", ("on", "off"), "ncq={v}", ""),
    "foreignautoimport": ("外来配置自动导入", "onoff", ("on", "off"), "foreignautoimport={v}", ""),
    "directpdmapping": ("物理盘直映射", "onoff", ("on", "off"), "directpdmapping={v}", ""),
    "loadbalancemode": ("负载均衡模式", "onoff", ("on", "off"), "loadbalancemode={v}", ""),
    "restorehotspare": ("热备恢复", "onoff", ("on", "off"), "restorehotspare={v}", ""),
    "abortcconerror": ("CC遇错即停", "onoff", ("on", "off"), "abortcconerror={v}", ""),
    "prcorrectunconfiguredareas": ("巡读修正未配置区", "onoff", ("on", "off"), "prcorrectunconfiguredareas={v}", ""),
    "maintainpdfailhistory": ("保留物理盘失效历史", "onoff", ("on", "off"), "maintainpdfailhistory={v}", ""),
    "configautobalance": ("配置自动均衡", "onoff", ("on", "off"), "configautobalance={v}", ""),
    "eghs": ("紧急全局热备（EGHS）", "onoff", ("on", "off"), "eghs state={v}", ""),
}

VD_KEYS = {
    "accesspolicy": {
        "label": "访问策略",
        "values": ["RW", "RO", "Blocked", "RmvBlkd"],
        "cmd": "accesspolicy={v}",
        "risk": "Blocked 会让业务直接报错，只能在确认业务已停止后使用",
    },
    "iopolicy": {
        "label": "IO 策略",
        "values": ["Cached", "Direct"],
        "cmd": "iopolicy={v}",
        "risk": "",
    },
    "Unmap": {
        "label": "UNMAP / TRIM",
        "values": ["On", "Off"],
        "cmd": "Unmap={v}",
        "risk": "",
    },
    "HostAccess": {
        "label": "主机访问模式",
        "values": ["ExclusiveAccess", "SharedAccess"],
        "cmd": "HostAccess={v}",
        "risk": "多个主机同时访问同一 VD 会损坏数据，集群场景才用 Shared",
    },
}

_SCALAR = (str, int, float, bool)


def first_scalar(obj):
    """从 storcli 的嵌套 JSON 里取第一个标量。"""
    if isinstance(obj, _SCALAR):
        return obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            got = first_scalar(v)
            if got is not None:
                return got
    if isinstance(obj, list):
        for v in obj:
            got = first_scalar(v)
            if got is not None:
                return got
    return None


def flatten(obj, prefix="", out=None):
    """把嵌套字典压平成 [(路径, 值)]，给 PHY 计数这类不定结构的表用。"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}{k}", out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            flatten(v, f"{prefix}[{i}]", out)
    elif isinstance(obj, _SCALAR):
        if str(obj).strip() not in ("", "None"):
            out.append((prefix, obj))
    return out


def pick_value(resp: dict):
    """从 Response Data 里取真正的值。

    storcli 的形态是
        {"Controller Properties": [{"Ctrl_Prop": "Rebuildrate", "Value": "30%"}]}
    直接"取第一个标量"会拿到属性名而不是值——这点很容易写错，所以这里
    显式优先读 Value 字段。
    """
    props = (resp or {}).get("Controller Properties")
    if isinstance(props, list) and props and isinstance(props[0], dict):
        v = props[0].get("Value")
        if v is not None:
            return v
    return first_scalar(resp)


def policy_catalog() -> dict:
    return {
        "ctrl": [
            {"key": k, "label": v[0], "type": v[1], "values": list(v[2]),
             "risk": v[4]}
            for k, v in CTRL_KEYS.items()
        ],
        "vd": [
            {"key": k, "label": v["label"], "values": v["values"], "risk": v["risk"]}
            for k, v in VD_KEYS.items()
        ],
    }


def ctrl_cmd(ctrl: int, key: str, value) -> str:
    spec = CTRL_KEYS.get(key)
    if not spec:
        raise ValueError(f"未知策略：{key}")
    kind = spec[1]
    if kind == "int":
        lo, hi = spec[2]
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec[0]} 必须是整数")
        if not (lo <= v <= hi):
            raise ValueError(f"{spec[0]} 须在 {lo}~{hi} 之间")
        return f"/c{ctrl} set " + spec[3].format(v=v)
    v = str(value).lower()
    if v not in ("on", "off"):
        raise ValueError(f"{spec[0]} 只能是 on/off")
    return f"/c{ctrl} set " + spec[3].format(v=v)


def vd_cmd(ctrl: int, vd: int, key: str, value) -> str:
    spec = VD_KEYS.get(key)
    if not spec:
        raise ValueError(f"未知 VD 策略：{key}")
    v = str(value)
    if v not in spec["values"]:
        raise ValueError(f"{spec['label']} 取值必须是 {', '.join(spec['values'])}")
    return f"/c{ctrl}/v{vd} set " + spec["cmd"].format(v=v)


def parse_value(key: str, raw) -> str:
    """把 storcli 返回的标量归一成 UI 能直接用的字符串。"""
    if raw is None:
        return ""
    spec = CTRL_KEYS.get(key) or ("", "",)
    s = str(raw).strip()
    if spec[1] == "int":
        # 值形如 "30%"，取数字部分
        m = re.search(r"-?\d+", s)
        return m.group(0) if m else ""
    if spec[1] == "onoff":
        return "on" if s.lower() in ("on", "1", "true", "enabled") else "off"
    return s
