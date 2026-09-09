"""bcache 健康告警。

每分钟读取 bcache 状态并评估：
  - cache_available_percent 低于阈值
  - dirty_data 长时间不下降（回写卡住）
  - 命中率骤降（五分钟窗口对比上次）
  - 缓存盘掉线
  - 回写积压（backlog）超过阈值

触发后写入事件并通过既有邮件/Webhook 通道告警（flag-once 去重）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import bcache_mgr
import lsi_alert

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
CONFIG_FILE = BASE_DIR / "bcache_alert_config.json"
STATE_FILE = BASE_DIR / ".bcache_alert_state.json"

DEFAULT_CONFIG = {
    "enabled": True,
    "cache_available_warn": 20,       # 缓存可用比例低于该值时告警
    "dirty_stuck_minutes": 10,        # dirty_data 持续不下降超过该分钟数告警
    "hit_drop_points": 20,            # 五分钟命中率相对上次下降超过该点数告警
    "backlog_bytes": 536870912,       # 脏数据积压超过 512MB 告警
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k in DEFAULT_CONFIG:
                if k in saved:
                    cfg[k] = saved[k]
    except Exception:
        pass
    return cfg


def save_config(new_cfg: dict) -> dict:
    cfg = load_config()
    for k in DEFAULT_CONFIG:
        if k in new_cfg:
            cfg[k] = new_cfg[k]
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _parse_size(text: str) -> int:
    """把 '0.0k'/'199.3M'/'1.2G' 转成字节。"""
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


def evaluate():
    """执行一轮巡检，触发满足条件的告警。"""
    cfg = load_config()
    if not cfg.get("enabled"):
        return
    state = _load_state()
    now = time.time()
    devices = state.setdefault("devices", {})
    csets = state.setdefault("csets", {})

    try:
        status = bcache_mgr.status()
    except Exception:
        return

    for c in status.get("cache_sets", []):
        key = c.get("uuid", "")
        prev = csets.get(key, {})
        cur = {}
        cache_avail = c.get("cache_available_percent")
        if cache_avail is not None and cache_avail < int(cfg["cache_available_warn"]):
            if not prev.get("avail_alerted"):
                lsi_alert._alert(
                    f"bcache 缓存可用率过低 {cache_avail}%",
                    f"缓存集 {key} 可用缓存仅剩 {cache_avail}%，低于阈值 {cfg['cache_available_warn']}%。",
                )
                cur["avail_alerted"] = True
        else:
            cur["avail_alerted"] = False

        if not c.get("cache_info") or not c.get("cache_info", {}).get("device") or c.get("cache_offline"):
            if not prev.get("offline_alerted"):
                lsi_alert._alert("bcache 缓存盘掉线", f"缓存集 {key} 找不到缓存盘设备。")
                cur["offline_alerted"] = True
        else:
            cur["offline_alerted"] = False

        hit = c.get("cache_hit_ratio")
        prev_hit = prev.get("hit_ratio")
        cur["hit_ratio"] = hit
        if prev_hit is not None and hit is not None and prev_hit - hit >= int(cfg["hit_drop_points"]):
            if not prev.get("hit_alerted"):
                lsi_alert._alert(
                    "bcache 命中率骤降",
                    f"缓存集 {key} 命中率由 {prev_hit}% 降至 {hit}%，疑似缓存未命中增加。",
                    "warning",
                )
                cur["hit_alerted"] = True
        else:
            cur["hit_alerted"] = False
        csets[key] = cur

    for d in status.get("devices", []):
        key = d.get("name", "")
        prev = devices.get(key, {})
        cur = {}
        dirty = _parse_size(d.get("dirty_data"))
        cur["dirty"] = dirty
        if dirty > 0:
            dirty_since = prev.get("dirty_since") or now
            cur["dirty_since"] = dirty_since
            stuck_min = (now - dirty_since) / 60
            if stuck_min >= int(cfg["dirty_stuck_minutes"]) and not prev.get("stuck_alerted"):
                lsi_alert._alert(
                    "bcache 脏数据长时间不下降",
                    f"{key} 脏数据 {d.get('dirty_data')} 持续 {int(stuck_min)} 分钟未下降，回写可能卡住。",
                )
                cur["stuck_alerted"] = True
            if dirty > int(cfg["backlog_bytes"]) and not prev.get("backlog_alerted"):
                lsi_alert._alert(
                    "bcache 回写积压",
                    f"{key} 脏数据积压达 {d.get('dirty_data')}，超过阈值 {cfg['backlog_bytes']} 字节。",
                    "warning",
                )
                cur["backlog_alerted"] = True
        else:
            cur["dirty_since"] = None
            cur["stuck_alerted"] = False
            cur["backlog_alerted"] = False
        devices[key] = cur

    _save_state(state)
