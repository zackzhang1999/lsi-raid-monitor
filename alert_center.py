"""告警中心：告警的生命周期管理。

原来的告警只是一组"已报过"的标记位，报完就忘：不能确认、不能静默、
恢复了也不通知。值班时这意味着两条极端——要么被重复报警淹没，
要么把已经处理过的告警当成新问题再查一遍。

这里把告警变成有身份的实体：

- 每条告警有稳定的 key（如 disk:C1:E134:S0:temp_crit），同一问题反复
  触发只累加次数，不会变成 N 条
- 支持确认：确认 = "我看到了，正在处理"，不等于问题消失
- 支持恢复：条件消失时把告警置为 resolved 并发出恢复通知
- 支持维护窗口：静默期内照常记录告警，但不发送邮件/Webhook——
  静默的语义是"别打扰人"，不是"假装没发生"

key 采用层级结构，前缀匹配即自然的作用域：
    system:consistency:fw_mismatch
    controller:C1:health
    disk:C1:E134:S0:temp_crit
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import db

BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(Path(__file__).resolve().parent / "data")))

STATUS_ACTIVE = "active"
STATUS_ACKED = "acknowledged"
STATUS_RESOLVED = "resolved"


def _now() -> datetime:
    return datetime.now()


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _row(row) -> dict:
    d = dict(row)
    d["acknowledged"] = d.get("status") == STATUS_ACKED
    d["resolved"] = d.get("status") == STATUS_RESOLVED
    return d


def scope_of(key: str) -> str:
    """从 key 推出作用域：取前两段（disk:C1 / controller:C1 / system）。"""
    parts = str(key).split(":")
    if len(parts) <= 2:
        return ":".join(parts[:1]) or "system"
    return ":".join(parts[:2])


# ---- 维护窗口（静默） ----


def _silence_matches(scope: str, key: str) -> bool:
    s = str(scope or "").strip()
    if not s:
        return False
    if s == "all":
        return True
    if key == s or key.startswith(s + ":"):
        return True
    # 控制器级维护窗口应连带静默该卡下的磁盘告警，
    # 否则"给这张卡做维护"还要逐个盘去静默。
    if s.startswith("controller:"):
        cid = s.split(":", 1)[1]
        if key.startswith(f"disk:C{cid}:"):
            return True
    return False


def active_silences() -> list[dict]:
    conn = db.get_conn()
    now = _fmt(_now())
    rows = conn.execute(
        "SELECT * FROM alert_silences WHERE ends_at > ? ORDER BY ends_at",
        (now,),
    ).fetchall()
    return [dict(r) for r in rows]


def is_silenced(key: str) -> dict | None:
    """返回命中的静默窗口；没有则返回 None。"""
    for s in active_silences():
        if _silence_matches(s.get("scope", ""), key):
            return s
    return None


def add_silence(scope: str, minutes: int, reason: str = "", user: str = "") -> dict:
    minutes = max(1, min(int(minutes or 60), 60 * 24 * 30))
    now = _now()
    ends = now + timedelta(minutes=minutes)
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO alert_silences(scope, reason, created_by, created_at, ends_at, ts_epoch)"
        " VALUES (?,?,?,?,?,?)",
        (
            str(scope or "all").strip() or "all",
            reason or "",
            user,
            _fmt(now),
            _fmt(ends),
            int(now.timestamp()),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM alert_silences WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row) if row else {}


def remove_silence(silence_id: int) -> bool:
    conn = db.get_conn()
    cur = conn.execute("DELETE FROM alert_silences WHERE id = ?", (int(silence_id),))
    conn.commit()
    return cur.rowcount > 0


# ---- 告警生命周期 ----


def raise_alert(
    key: str,
    level: str,
    title: str,
    detail: str,
    escalate_minutes: int = 0,
) -> tuple[dict, bool, dict | None]:
    """登记或更新一条告警。

    返回 (alert, should_notify, silence)。
    should_notify 为真表示调用方应当发送邮件/Webhook：
    - 新出现的告警
    - 已确认过的告警在 escalate_minutes 后仍未恢复（升级提醒）
    静默期内的告警照常记录，但 should_notify 恒为假。
    """
    now = _now()
    now_s = _fmt(now)
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()

    if row is None:
        conn.execute(
            "INSERT INTO alerts(key, scope, level, title, detail, status, count,"
            " first_seen, last_seen, last_notified, notify_count, ts_epoch)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                scope_of(key),
                level,
                title,
                detail,
                STATUS_ACTIVE,
                1,
                now_s,
                now_s,
                None,
                0,
                int(now.timestamp()),
            ),
        )
        conn.commit()
        alert = dict(conn.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone())
        silence = is_silenced(key)
        return alert, silence is None, silence

    prev = dict(row)
    was_resolved = prev.get("status") == STATUS_RESOLVED
    count = 1 if was_resolved else int(prev.get("count") or 0) + 1
    first_seen = now_s if was_resolved else prev.get("first_seen") or now_s

    # 已确认的告警不因重复触发而回到未确认状态：确认的语义是
    # "我看到了，正在处理"，问题没消失前不该反复要求人再确认一次。
    status = prev.get("status")
    if was_resolved:
        status = STATUS_ACTIVE
    ack_by = prev.get("acknowledged_by") if status == STATUS_ACKED else None
    ack_at = prev.get("acknowledged_at") if status == STATUS_ACKED else None
    if was_resolved:
        ack_by = None
        ack_at = None

    conn.execute(
        "UPDATE alerts SET level=?, title=?, detail=?, status=?, count=?,"
        " first_seen=?, last_seen=?, resolved_at=NULL, acknowledged_by=?,"
        " acknowledged_at=? WHERE key=?",
        (
            level,
            title,
            detail,
            status,
            count,
            first_seen,
            now_s,
            ack_by,
            ack_at,
            key,
        ),
    )
    conn.commit()
    alert = dict(conn.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone())

    silence = is_silenced(key)
    if silence:
        return alert, False, silence

    if was_resolved:
        return alert, True, None

    # 长时间未确认且未恢复 → 升级提醒一次
    if escalate_minutes > 0:
        last = alert.get("last_notified") or alert.get("first_seen")
        try:
            last_dt = datetime.strptime(str(last), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            last_dt = now
        if now - last_dt >= timedelta(minutes=int(escalate_minutes)):
            return alert, True, None

    return alert, False, None


def resolve_alert(key: str) -> dict | None:
    """把告警置为已恢复。返回被恢复的告警；本来就不是活跃状态则返回 None。"""
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM alerts WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    prev = dict(row)
    if prev.get("status") == STATUS_RESOLVED:
        return None
    now_s = _fmt(_now())
    conn.execute(
        "UPDATE alerts SET status=?, resolved_at=?, last_seen=? WHERE key=?",
        (STATUS_RESOLVED, now_s, now_s, key),
    )
    conn.commit()
    return prev


def mark_notified(key: str):
    conn = db.get_conn()
    conn.execute(
        "UPDATE alerts SET last_notified=?, notify_count=notify_count+1 WHERE key=?",
        (_fmt(_now()), key),
    )
    conn.commit()


def acknowledge(alert_id: int, user: str) -> dict | None:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (int(alert_id),)).fetchone()
    if not row or dict(row).get("status") == STATUS_RESOLVED:
        return None
    conn.execute(
        "UPDATE alerts SET status=?, acknowledged_by=?, acknowledged_at=? WHERE id=?",
        (STATUS_ACKED, user, _fmt(_now()), int(alert_id)),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM alerts WHERE id = ?", (int(alert_id),)).fetchone())


def acknowledge_all(user: str) -> int:
    conn = db.get_conn()
    cur = conn.execute(
        "UPDATE alerts SET status=?, acknowledged_by=?, acknowledged_at=?"
        " WHERE status=?",
        (STATUS_ACKED, user, _fmt(_now()), STATUS_ACTIVE),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def list_alerts(status: str = "open", limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM alerts"
    args: list = []
    if status == "open":
        sql += " WHERE status IN (?, ?)"
        args.extend([STATUS_ACTIVE, STATUS_ACKED])
    elif status in (STATUS_ACTIVE, STATUS_ACKED, STATUS_RESOLVED):
        sql += " WHERE status = ?"
        args.append(status)
    # 未确认排在已确认之前；同级按最近触发时间倒序
    sql += " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, last_seen DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    conn = db.get_conn()
    return [_row(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def counts() -> dict:
    conn = db.get_conn()
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM alerts GROUP BY status").fetchall()
    out = {STATUS_ACTIVE: 0, STATUS_ACKED: 0, STATUS_RESOLVED: 0}
    for r in rows:
        out[str(r["status"])] = int(r["n"])
    out["open"] = out[STATUS_ACTIVE] + out[STATUS_ACKED]
    return out
