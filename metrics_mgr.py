"""Prometheus 指标导出。

企业采购清单里几乎必然有一条"能接入现有监控平台"。自建一套图表解决不了
这个问题——客户的运维已经在用 Prometheus + Grafana，他们要的是把存储
指标汇进同一个面板，而不是多开一个页面。

两条实现原则：

1. **不引入依赖**。文本暴露格式很简单，手写比装 prometheus_client 更可控，
   也让离线交付少一个包。
2. **标签基数必须可控**。序列号、型号这类每台机器都不同的值绝不能做标签，
   否则时间序列数量会随磁盘数线性膨胀，最终把客户的 Prometheus 拖垮。
   所以标签只用 控制器号 / 槽位 / 盘位状态 这些稳定且有限的值。
"""

from __future__ import annotations

import re

_LABEL_ESCAPE = str.maketrans({"\\": r"\\", '"': r"\"", "\n": r"\n"})


def _esc(v) -> str:
    return str(v if v is not None else "").translate(_LABEL_ESCAPE)


def _labels(pairs: list[tuple[str, object]]) -> str:
    items = [(k, v) for k, v in pairs if v not in (None, "")]
    if not items:
        return ""
    inner = ",".join(f'{k}="{_esc(v)}"' for k, v in items)
    return "{" + inner + "}"


class _Out:
    """指标累积器。按指标名分组输出，保证 HELP/TYPE 只出现一次。"""

    def __init__(self):
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def _declare(self, name: str, mtype: str, help_text: str):
        if name in self._declared:
            return
        self._declared.add(name)
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {mtype}")

    def sample(self, name: str, mtype: str, help_text: str, value, labels=None):
        if value is None:
            return
        self._declare(name, mtype, help_text)
        self.lines.append(f"{name}{_labels(labels or [])} {value}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


_HEALTH_VALUE = {"ok": 0, "unknown": 1, "warn": 1, "crit": 2}

# storcli 的状态名 -> 稳定的小写标识，避免同一状态出现多种拼写
_STATE_ALIASES = {
    "onln": "online",
    "online": "online",
    "optl": "optimal",
    "optimal": "optimal",
    "ugood": "unconfigured_good",
    "jbod": "jbod",
    "ghs": "global_hotspare",
    "dhs": "dedicated_hotspare",
    "rbld": "rebuild",
    "cpybck": "copyback",
    "ubad": "unconfigured_bad",
    "failed": "failed",
    "offln": "offline",
    "offline": "offline",
    "dgrd": "degraded",
    "degraded": "degraded",
    "missing": "missing",
}


def _state_key(state) -> str:
    s = str(state or "").strip().lower()
    return _STATE_ALIASES.get(s, re.sub(r"[^a-z0-9_]", "_", s) or "unknown")


def _bytes(size_text: str) -> int | None:
    """把 storcli 的 '297.062 GB' / '148.531 GB' 转成字节。
    统一成字节是为了让 Prometheus 侧可以直接做容量换算与告警。"""
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?B)\s*$", str(size_text or ""), re.I)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    mult = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
    unit = m.group(2).upper()
    if unit not in mult:
        return None
    # storcli 用的是 1000 进制（148.531 GB = 160 GB 标称盘）
    return int(v * mult[unit])


def render(status: dict, alerts: dict | None = None, tasks: dict | None = None,
           filesystems: list[dict] | None = None, bcache: dict | None = None) -> str:
    o = _Out()
    st = status or {}

    o.sample("datamax_up", "gauge", "采集是否成功（始终为 1，用于存活探测）", 1)
    o.sample(
        "datamax_health", "gauge",
        "整机健康：0=正常 1=警告 2=严重",
        _HEALTH_VALUE.get(str(st.get("health") or "").lower()),
    )
    o.sample(
        "datamax_consistency_ok", "gauge",
        "跨控制器一致性检查是否通过（1=通过）",
        1 if (st.get("consistency") or {}).get("ok", True) else 0,
    )

    for c in st.get("controllers") or []:
        cid = c.get("ctrl")
        o.sample(
            "datamax_controller_health", "gauge",
            "控制器健康：0=正常 1=警告 2=严重",
            _HEALTH_VALUE.get(str(c.get("health") or "").lower()),
            [("ctrl", cid)],
        )
        o.sample(
            "datamax_controller_roc_temperature_celsius", "gauge",
            "控制器 ROC 温度（摄氏度）",
            c.get("roc_temp"), [("ctrl", cid)],
        )
        o.sample(
            "datamax_controller_disks", "gauge",
            "该控制器下的物理磁盘数量",
            c.get("num_disks"), [("ctrl", cid)],
        )
        o.sample(
            "datamax_controller_virtual_disks", "gauge",
            "该控制器下的虚拟磁盘数量",
            c.get("num_vds"), [("ctrl", cid)],
        )
        fw = c.get("fw")
        # 固件版本放进独立的 info 指标，值恒为 1——它是文本元数据，
        # 不是可以求和的数值，用 info 型指标表达才不会误导使用者。
        if fw or c.get("model"):
            o.sample(
                "datamax_controller_info", "gauge",
                "控制器型号与固件版本（值恒为 1）",
                1, [("ctrl", cid), ("model", c.get("model")), ("firmware", fw)],
            )

    disks = st.get("physical_disks") or []
    o.sample("datamax_disks", "gauge", "物理磁盘总数", len(disks))

    state_count: dict[str, int] = {}
    for d in disks:
        lbl = [
            ("ctrl", d.get("ctrl")),
            ("enclosure", d.get("eid")),
            ("slot", d.get("slot")),
        ]
        state = _state_key(d.get("state"))
        state_count[state] = state_count.get(state, 0) + 1
        o.sample(
            "datamax_disk_state", "gauge",
            "磁盘状态（每个状态下该盘为 1，其余状态不出样本）",
            1, lbl + [("state", state)],
        )
        o.sample(
            "datamax_disk_temperature_celsius", "gauge",
            "磁盘温度（摄氏度）",
            d.get("temperature"), lbl,
        )
        o.sample(
            "datamax_disk_media_errors", "gauge",
            "磁盘介质错误计数（累计）",
            d.get("media_error"), lbl,
        )
        o.sample(
            "datamax_disk_other_errors", "gauge",
            "磁盘其它错误计数（累计）",
            d.get("other_error"), lbl,
        )
        o.sample(
            "datamax_disk_predictive_failure", "gauge",
            "磁盘预测性故障计数",
            d.get("predictive_failure"), lbl,
        )
        o.sample(
            "datamax_disk_reallocated_sectors", "gauge",
            "磁盘重映射扇区数（累计，上升是坏盘信号）",
            d.get("reallocated"), lbl,
        )
        o.sample(
            "datamax_disk_pending_sectors", "gauge",
            "磁盘待定扇区数（累计）",
            d.get("pending"), lbl,
        )
        o.sample(
            "datamax_disk_uncorrectable_sectors", "gauge",
            "磁盘无法纠正扇区数（累计）",
            d.get("uncorrectable"), lbl,
        )
        o.sample(
            "datamax_disk_power_on_hours", "gauge",
            "磁盘通电时长（小时）",
            d.get("power_on_hours"), lbl,
        )
        o.sample(
            "datamax_disk_rebuild_progress_percent", "gauge",
            "重建进度百分比（未在重建则无样本）",
            d.get("rebuild_progress"), lbl,
        )
        # 每块盘的状态用独立指标 + state 标签已经表达；这里再给一个
        # "是否异常"的布尔值，方便 Grafana 直接做面板而不必写状态枚举。
        ok_states = ("online", "unconfigured_good", "jbod", "global_hotspare", "dedicated_hotspare")
        bad = state not in ok_states
        o.sample(
            "datamax_disk_unhealthy", "gauge",
            "磁盘是否异常（1=异常）",
            1 if bad else 0, lbl,
        )

    for state, n in sorted(state_count.items()):
        o.sample(
            "datamax_disks_by_state", "gauge",
            "按状态统计的磁盘数量",
            n, [("state", state)],
        )

    for v in st.get("virtual_disks") or []:
        lbl = [("ctrl", v.get("ctrl")), ("vd", v.get("dg_vd")), ("level", v.get("type"))]
        o.sample(
            "datamax_virtual_disk_state", "gauge",
            "虚拟磁盘状态（1=正常 0=非最优）",
            1 if str(v.get("state") or "").lower() in ("optl", "optimal") else 0,
            lbl,
        )
        o.sample(
            "datamax_virtual_disk_size_bytes", "gauge",
            "虚拟磁盘容量（字节）",
            _bytes(v.get("size")), lbl,
        )

    a = alerts or {}
    for level in ("active", "acknowledged", "open"):
        o.sample(
            "datamax_alerts", "gauge",
            "告警数量（按状态）",
            a.get(level), [("status", level)],
        )

    t = tasks or {}
    for k in ("queued", "running", "succeeded", "failed"):
        o.sample(
            "datamax_tasks", "gauge",
            "任务数量（按状态）",
            t.get(k), [("status", k)],
        )

    for fs in filesystems or []:
        lbl = [("mountpoint", fs.get("mountpoint")), ("device", fs.get("device")),
               ("fstype", fs.get("fstype"))]
        o.sample(
            "datamax_filesystem_size_bytes", "gauge",
            "文件系统总容量（字节）",
            fs.get("size_bytes"), lbl,
        )
        o.sample(
            "datamax_filesystem_used_bytes", "gauge",
            "文件系统已用容量（字节）",
            fs.get("used_bytes"), lbl,
        )
        o.sample(
            "datamax_filesystem_used_percent", "gauge",
            "文件系统使用率（百分比）",
            fs.get("use_percent"), lbl,
        )

    for cs in (bcache or {}).get("cache_sets") or []:
        cdev = cs.get("cache_device") or cs.get("uuid") or ""
        lbl = [("cache_device", cdev)]
        # 命中率按四档窗口各出一条时间序列：五分钟档看突发，天档看趋势，
        # 只给一个总数没法做趋势告警。
        ps = cs.get("period_stats") or {}
        for win in ("five_minute", "hour", "day", "total"):
            entry = ps.get(win)
            if not entry:
                continue
            o.sample(
                "datamax_bcache_hit_ratio_percent", "gauge",
                "bcache 缓存命中率（百分比，按统计窗口）",
                entry.get("cache_hit_ratio"), lbl + [("window", win)],
            )
        o.sample(
            "datamax_bcache_cache_available_percent", "gauge",
            "bcache 缓存可用比例（百分比，writeback 模式下低于阈值需关注）",
            cs.get("cache_available_percent"), lbl,
        )
        o.sample(
            "datamax_bcache_root_usage_percent", "gauge",
            "bcache 元数据区域使用率（百分比）",
            cs.get("root_usage_percent"), lbl,
        )
        # 脏数据桶占比：writeback 模式下这是"断电会丢多少"的直接体现。
        # bcache 只按桶给出百分比，不给字节数，所以指标名用 percent。
        dirty = (cs.get("cache_info") or {}).get("bucket_dirty")
        if dirty not in (None, ""):
            try:
                o.sample(
                    "datamax_bcache_dirty_bucket_percent", "gauge",
                    "bcache 脏数据桶占比（百分比，writeback 模式关键指标）",
                    float(dirty), lbl,
                )
            except (TypeError, ValueError):
                pass
        o.sample(
            "datamax_bcache_running", "gauge",
            "缓存集是否可用（1=可用）",
            1 if cs.get("available", True) is not False and not cs.get("cache_offline") else 0,
            lbl,
        )

    return o.text()
