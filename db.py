"""SQLite 数据底座。

采集器继续写 CSV（用于导出与兼容），同时把每轮样本追加写入本库。
Web 端的历史趋势查询优先走 SQLite；历史 CSV 会在启动时幂等迁移进库。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
DB_FILE = BASE_DIR / "lsi.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    src TEXT NOT NULL,
    ts TEXT NOT NULL,
    ts_epoch INTEGER NOT NULL,
    data TEXT NOT NULL,
    dedupe TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_samples_kind_ts ON samples(kind, ts_epoch);
-- 操作任务台账：危险操作不再同步等待 storcli，而是登记为任务后台执行，
-- 每条记录都留存操作者/参数/命令/返回码，是售后追溯的依据。
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    target TEXT,
    params TEXT NOT NULL,
    command TEXT,
    status TEXT NOT NULL,
    retryable INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 1,
    parent_id INTEGER,
    operator TEXT,
    progress INTEGER,
    message TEXT,
    output TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    ts_epoch INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);
-- 告警生命周期：从"一次性标记位"升级为有身份的实体，
-- 才能做到确认、恢复通知与维护窗口静默。
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    level TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_notified TEXT,
    notify_count INTEGER NOT NULL DEFAULT 0,
    acknowledged_by TEXT,
    acknowledged_at TEXT,
    resolved_at TEXT,
    ts_epoch INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, level);
CREATE TABLE IF NOT EXISTS alert_silences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    reason TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    ts_epoch INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- 多机管理：中心节点登记被管节点，采集其状态快照。
-- 单机场景下这张表是空的，功能不启用即可，不影响任何现有行为。
CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    address TEXT NOT NULL,
    token TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    local INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    ts_epoch INTEGER NOT NULL,
    online INTEGER NOT NULL,
    error TEXT,
    data TEXT,
    UNIQUE(host_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_host_samples ON host_samples(host_id, ts_epoch);
-- 控制器配置快照：用来发现"有人改了阵列配置"以及换卡后恢复。
-- 只存归一化后的配置项——温度、运行时长这类每轮都在变的值不能进，
-- 否则每次快照都不一样，漂移检测就永远在误报。
CREATE TABLE IF NOT EXISTS ctrl_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ctrl INTEGER NOT NULL,
    ts TEXT NOT NULL,
    ts_epoch INTEGER NOT NULL,
    hash TEXT NOT NULL,
    data TEXT NOT NULL,
    operator TEXT,
    note TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_ctrl_configs ON ctrl_configs(ctrl, id DESC);
"""

# 历史 CSV 文件名 -> 样本类型
CSV_KINDS = {
    "disks.csv": "disks",
    "controller.csv": "controller",
    "vds.csv": "vds",
    "attributes.csv": "attrs",
    "smart.csv": "smart",
    "patrol.csv": "patrol",
    "consistency.csv": "consistency",
    "system.csv": "system",
    "io.csv": "io",
    "fs.csv": "fs",
    "nvme.csv": "nvme",
}

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


def _epoch(ts: str) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").timestamp())
    except ValueError:
        return None


def _dedupe(kind: str, src: str, ts: str, data: str) -> str:
    return hashlib.sha1(f"{kind}|{src}|{ts}|{data}".encode("utf-8")).hexdigest()


def insert_rows(kind: str, rows: list[dict], src: str = "live") -> int:
    """把一行或多行样本写入 SQLite，重复样本自动忽略。返回实际插入条数。"""
    if not rows:
        return 0
    payloads = []
    for row in rows:
        ts = str(row.get("timestamp", "") or "")
        epoch = _epoch(ts)
        if epoch is None:
            continue
        data = json.dumps(row, ensure_ascii=False, sort_keys=True)
        payloads.append((kind, src, ts, epoch, data, _dedupe(kind, src, ts, data)))
    if not payloads:
        return 0
    try:
        conn = get_conn()
        cur = conn.executemany(
            "INSERT OR IGNORE INTO samples(kind, src, ts, ts_epoch, data, dedupe) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            payloads,
        )
        conn.commit()
        return cur.rowcount
    except Exception as exc:  # SQLite 异常不阻断采集主流程
        print(f"[db] insert error ({kind}): {exc}", file=sys.stderr)
        return 0


def has_data() -> bool:
    try:
        return bool(get_conn().execute("SELECT 1 FROM samples LIMIT 1").fetchone())
    except Exception:
        return False


def history(kind: str, since_epoch: int) -> list[dict]:
    """返回指定类型在 since_epoch 之后的样本（按时间升序），data 已解析为 dict。"""
    try:
        rows = get_conn().execute(
            "SELECT ts_epoch, data FROM samples WHERE kind = ? AND ts_epoch >= ? ORDER BY ts_epoch, id",
            (kind, since_epoch),
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            data = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        data["_ts_epoch"] = r["ts_epoch"]
        out.append(data)
    return out


def disk_times(eid, slot, ctrl=None) -> tuple[int | None, int | None, int]:
    """返回某块盘的首次出现、最近出现时间（unix 秒）与样本条数。

    多控制器下 (eid, slot) 会重复，必须带 ctrl 一起判定，否则两张卡上
    同槽位的盘会被算成同一块。ctrl 为 None 时退化为旧行为；
    传入 ctrl 时同时接纳没有 ctrl 字段的历史样本——那些样本产生于
    只采集单一控制器的版本，归属到当前卡是安全的。
    """
    sql = (
        "SELECT MIN(ts_epoch) AS first_seen, MAX(ts_epoch) AS last_seen, COUNT(*) AS n "
        "FROM samples WHERE kind = 'disks' "
        "AND CAST(json_extract(data, '$.eid') AS TEXT) = ? "
        "AND CAST(json_extract(data, '$.slot') AS TEXT) = ?"
    )
    params: list = [str(eid), str(slot)]
    if ctrl is not None:
        sql += (
            " AND (json_extract(data, '$.ctrl') IS NULL"
            " OR CAST(json_extract(data, '$.ctrl') AS TEXT) = ?)"
        )
        params.append(str(ctrl))
    try:
        row = get_conn().execute(sql, tuple(params)).fetchone()
    except Exception:
        return None, None, 0
    if not row:
        return None, None, 0
    return row["first_seen"], row["last_seen"], row["n"]


def prune(days: int) -> int:
    """删除早于 days 天的样本，返回删除条数。days <= 0 表示不清理。"""
    if days is None or int(days) <= 0:
        return 0
    cutoff = int(datetime.now().timestamp()) - int(days) * 86400
    conn = get_conn()
    cur = conn.execute("DELETE FROM samples WHERE ts_epoch < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def migrate_from_csv() -> int:
    """把 data/YYYY-MM-DD/*.csv 历史数据幂等导入 SQLite，返回本次导入条数。"""
    if not BASE_DIR.exists():
        return 0
    conn = get_conn()
    if conn.execute("SELECT value FROM meta WHERE key = 'migrated'").fetchone():
        return 0
    total = 0
    for date_dir in sorted(BASE_DIR.iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        for filename, kind in CSV_KINDS.items():
            path = date_dir / filename
            if not path.exists():
                continue
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
            except Exception:
                continue
            total += insert_rows(kind, rows, src=filename)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('migrated', '1')")
    conn.commit()
    return total
