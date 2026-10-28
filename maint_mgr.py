"""维护计划：把"该定期做的事"变成自动执行的动作。

存储运维里有几件事是**必须周期性做、但人一定会忘**的：

  * 巡读（Patrol Read）——主动扫盘面，把弱扇区提前重映射掉
  * 一致性检查（Consistency Check）——校验冗余是否还有效，RAID5/6 的最后防线
  * 磁盘 SMART 自检——让盘自己做一次长/短自检

这三件事的价值在于"在坏之前发现"，而它们的共同特征是：不做不会有任何
即时反馈，所以一定会被无限期推迟。计划任务就是解决这个问题的。

设计上的几个取舍：

1. **动作走 task_engine**，不另起执行路径。危险操作已经统一在任务队列里
   串行执行、留痕、可重试；计划任务只是"由时间而非人触发"的同一个队列。
2. **按窗口键去重**，和报表投递同一套思路：日计划用日期、周计划用 ISO 周、
   月计划用月份。服务重启、整机停机一天，只会补做一次，不会重复下发。
3. **只允许登记白名单动作**。计划任务没有人盯着，所以能配置的动作必须
   是幂等、可中断、不会丢数据的。建阵列、擦除、删虚拟盘这些一律不允许
   出现在计划里——它们必须有人在场。
4. **执行失败要告警**。静默失败的定时任务是运维最怕的东西：它让你以为
   巡检一直在做，其实早就停了。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
CONFIG_FILE = BASE_DIR / "maintenance.json"

HISTORY_MAX = 60

# 允许被计划触发的动作白名单：幂等、可中断、不动数据。
ACTIONS = {
    "patrolread_start": "启动巡读",
    "patrolread_stop": "停止巡读",
    "cc_start": "启动一致性检查",
    "cc_stop": "停止一致性检查",
    "smart_short": "磁盘 SMART 短自检",
    "smart_long": "磁盘 SMART 长自检",
}

DEFAULT_CONFIG = {
    "enabled": False,
    "plans": [],
    "last_run": {},
    "history": [],
}

_NEXT_ID = 1

# 由 web_server 注入：把一条计划翻译成 (任务类型, 任务参数, 目标描述, 命令预览)。
# 命令模板的唯一出处是 web_server 里的 RAID_ACTIONS，抄一份到这里迟早会对不上。
_builder = None


def set_builder(fn) -> None:
    global _builder
    _builder = fn


def _to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for k in DEFAULT_CONFIG:
                if k in saved:
                    cfg[k] = saved[k]
    except Exception as e:
        print(f"[maint_mgr] config load error: {e}")
    return cfg


def save_config(new_cfg: dict) -> dict:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    old = load_config()
    was_enabled = bool(old.get("enabled"))
    cfg = old
    for k in ("enabled", "plans", "last_run", "history"):
        if k in new_cfg:
            cfg[k] = new_cfg[k]
    # 与报表投递同理：启用时先占位当前窗口，否则"下午三点勾上启用"
    # 会立刻把所有到点的计划补跑一遍，那不是订阅动作。
    if new_cfg.get("enabled") and not was_enabled:
        now = datetime.now()
        last = dict(cfg.get("last_run") or {})
        for p in cfg.get("plans") or []:
            last[str(p.get("id"))] = window_key(p, now)
        cfg["last_run"] = last
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


# ---- 时间 ----


def _parse_hhmm(s: str):
    t = str(s or "").strip()
    if len(t) != 5 or t[2] != ":" or not (t[:2] + t[3:]).isdigit():
        return None
    h, m = int(t[:2]), int(t[3:])
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def valid_time(s: str) -> bool:
    return _parse_hhmm(s) is not None


def _month_last_day(now: datetime) -> int:
    nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (nxt - timedelta(days=1)).day


def window_key(plan: dict, now: datetime) -> str:
    freq = str(plan.get("freq") or "daily")
    if freq == "daily":
        return now.strftime("%Y-%m-%d")
    if freq == "weekly":
        iso = now.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if freq == "monthly":
        return now.strftime("%Y-%m")
    if freq == "interval":
        # 按小时间隔的计划用"当前处于第几个间隔"做键，
        # 这样 6 小时一次的计划一天正好触发 4 次，重启也不会多跑。
        hours = max(1, _to_int(plan.get("interval_hours"), 24))
        return str(int(now.timestamp()) // (hours * 3600))
    return now.strftime("%Y-%m-%d")


def due_plans(now: datetime, cfg: dict) -> list[dict]:
    if not cfg.get("enabled"):
        return []
    last = cfg.get("last_run") or {}
    out = []
    for plan in cfg.get("plans") or []:
        if not plan.get("enabled", True):
            continue
        if plan.get("action") not in ACTIONS:
            continue
        pid = str(plan.get("id"))
        freq = str(plan.get("freq") or "daily")
        if freq == "interval":
            hours = max(1, _to_int(plan.get("interval_hours"), 24))
            key = window_key(plan, now)
            if last.get(pid) != key and now.minute < 5:
                # 间隔计划在每个间隔的头 5 分钟内触发一次，避免整点抖动错过
                out.append(plan)
            continue

        hhmm = _parse_hhmm(plan.get("time"))
        if not hhmm or (now.hour, now.minute) < hhmm:
            continue
        if freq == "weekly":
            if now.isoweekday() != _to_int(plan.get("weekday"), 1):
                continue
        elif freq == "monthly":
            day = min(max(1, _to_int(plan.get("day"), 1)), _month_last_day(now))
            if now.day != day:
                continue
        if last.get(pid) != window_key(plan, now):
            out.append(plan)
    return out


# ---- 执行 ----


def _run_plan(plan: dict, trigger: str) -> dict:
    """把计划里的动作交给任务引擎。"""
    import task_engine

    action = plan.get("action")
    if _builder is None:
        return {"ok": False, "task_id": None, "message": "命令构造器未注入"}
    try:
        spec = _builder(plan)
        task = task_engine.submit(
            spec["kind"],
            spec.get("params") or {},
            target=spec.get("target") or "",
            command=spec.get("command") or "",
            operator=f"计划任务（{trigger}）",
        )
        return {"ok": True, "task_id": task.get("id"), "message": f"已下发任务 #{task.get('id')}"}
    except Exception as e:
        return {"ok": False, "task_id": None, "message": str(e)}


def _record(cfg: dict, plan: dict, res: dict, trigger: str) -> dict:
    entry = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "plan_id": plan.get("id"),
        "name": plan.get("name") or ACTIONS.get(plan.get("action"), ""),
        "action": plan.get("action"),
        "ok": bool(res.get("ok")),
        "task_id": res.get("task_id"),
        "message": res.get("message", ""),
        "trigger": trigger,
    }
    hist = list(cfg.get("history") or [])
    hist.insert(0, entry)
    cfg["history"] = hist[:HISTORY_MAX]
    return entry


def run_due(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    cfg = load_config()
    due = due_plans(now, cfg)
    if not due:
        return []

    import lsi_alert

    last = dict(cfg.get("last_run") or {})
    results = []
    # 一轮最多跑一个计划：多个计划同时下发会在任务队列里排长队，
    # 而巡检动作本身对延迟并不敏感。
    plan = due[0]
    res = _run_plan(plan, "定时")
    last[str(plan.get("id"))] = window_key(plan, now)
    entry = _record(cfg, plan, res, "定时")
    results.append(entry)

    if res["ok"]:
        lsi_alert.log_event("info", f"维护计划「{entry['name']}」已下发（任务 #{res['task_id']}）")
        lsi_alert._clear(f"maint:plan:{plan.get('id')}")
    else:
        lsi_alert.log_event("warning", f"维护计划「{entry['name']}」下发失败：{res['message']}")
        lsi_alert._flag(
            f"maint:plan:{plan.get('id')}",
            f"维护计划「{entry['name']}」执行失败",
            f"原因：{res['message']}。计划任务不会自己重试，请检查控制器状态后手动执行一次。",
            "warning",
        )

    cfg["last_run"] = last
    save_config(cfg)
    return results


def run_now(plan_id, trigger: str = "手动") -> dict:
    """立即执行某个计划，用于验证配置是否正确。"""
    cfg = load_config()
    plan = next((p for p in cfg.get("plans") or [] if str(p.get("id")) == str(plan_id)), None)
    if not plan:
        return {"ok": False, "message": "计划不存在"}
    if plan.get("action") not in ACTIONS:
        return {"ok": False, "message": "该动作不允许被执行"}
    res = _run_plan(plan, trigger)
    entry = _record(cfg, plan, res, trigger)
    save_config(cfg)
    return {"ok": res["ok"], "message": res["message"], "entry": entry}


def normalize(plans: list[dict]) -> list[dict]:
    """校验并补全计划字段。坏配置在这里挡住，而不是等到执行时才炸。"""
    out = []
    seen = set()
    for i, p in enumerate(plans or []):
        if not isinstance(p, dict):
            continue
        action = str(p.get("action") or "")
        if action not in ACTIONS:
            raise ValueError(f"不支持的动作：{action or '(空)'}")
        pid = p.get("id")
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            pid = i + 1
        if pid in seen:
            raise ValueError(f"计划编号重复：{pid}")
        seen.add(pid)
        freq = str(p.get("freq") or "daily")
        if freq not in ("daily", "weekly", "monthly", "interval"):
            raise ValueError(f"不支持的频率：{freq}")
        item = {
            "id": pid,
            "name": str(p.get("name") or ACTIONS[action])[:60],
            "action": action,
            "freq": freq,
            "enabled": bool(p.get("enabled", True)),
            "target": str(p.get("target") or "")[:40],
            "params": p.get("params") or {},
        }
        if freq == "interval":
            hours = _to_int(p.get("interval_hours"), 24)
            if not (1 <= hours <= 720):
                raise ValueError("间隔小时数须在 1~720 之间")
            item["interval_hours"] = hours
        else:
            t = str(p.get("time") or "")
            if not valid_time(t):
                raise ValueError(f"「{item['name']}」的时间格式应为 HH:MM")
            item["time"] = t
            if freq == "weekly":
                wd = _to_int(p.get("weekday"), 1)
                if not (1 <= wd <= 7):
                    raise ValueError("星期须为 1~7")
                item["weekday"] = wd
            if freq == "monthly":
                d = _to_int(p.get("day"), 1)
                if not (1 <= d <= 31):
                    raise ValueError("日期须为 1~31")
                item["day"] = d
        out.append(item)
    return out


def view() -> dict:
    cfg = load_config()
    now = datetime.now()
    plans = []
    for p in cfg.get("plans") or []:
        q = dict(p)
        q["last_run"] = (cfg.get("last_run") or {}).get(str(p.get("id")), "")
        q["window_key"] = window_key(p, now)
        plans.append(q)
    return {
        "config": cfg,
        "plans": plans,
        "actions": [{"key": k, "label": v} for k, v in ACTIONS.items()],
    }
