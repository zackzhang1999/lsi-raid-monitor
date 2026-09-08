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
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
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


def disk_times(eid, slot) -> tuple[int | None, int | None, int]:
    """返回某块盘的首次出现、最近出现时间（unix 秒）与样本条数。"""
    try:
        row = get_conn().execute(
            "SELECT MIN(ts_epoch) AS first_seen, MAX(ts_epoch) AS last_seen, COUNT(*) AS n "
            "FROM samples WHERE kind = 'disks' "
            "AND CAST(json_extract(data, '$.eid') AS TEXT) = ? "
            "AND CAST(json_extract(data, '$.slot') AS TEXT) = ?",
            (str(eid), str(slot)),
        ).fetchone()
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
