"""异步任务引擎。

危险存储操作不再同步等待 storcli 返回，而是登记成任务后台执行：

- 并发度固定为 1。存储操作天然不能并行——同一张控制器上两个 storcli 写
  命令同时下发，轻则报忙重则互相干扰。串行是这里唯一安全的选择。
- 每条任务留存操作者、参数、命令预览、返回码与完整输出，售后争议时可查。
- 长任务（建阵列 / 擦除 / 扩容 / 迁移）不再阻塞界面。
- 仅幂等操作允许重试，且必须由人显式触发。
- 瞬时操作（定位灯、蜂鸣器）不进队列，但同样写一条已完成的任务记录，
  这样"操作记录"里是一条完整时间线，而不是缺一段。

任务记录与历史样本共用同一个 SQLite 文件（tasks 表）。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import traceback
from datetime import datetime
from pathlib import Path

import db

BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(Path(__file__).resolve().parent / "data")))

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

# kind -> {"title": str, "retryable": bool, "fn": callable}
_HANDLERS: dict[str, dict] = {}
_queue: "queue.Queue[int]" = queue.Queue()
_current: dict = {"id": None}
_lock = threading.Lock()
_worker: threading.Thread | None = None
_started = False


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def register(kind: str, title: str, retryable: bool = False):
    """注册一种任务类型。

    retryable 必须由注册方显式声明：只有"重跑一次不会造成额外影响"的
    操作才配得上这个标记。建阵列、擦除、扩容这类不允许自动重试。
    """

    def deco(fn):
        _HANDLERS[kind] = {"title": title, "retryable": bool(retryable), "fn": fn}
        return fn

    return deco


def known_kinds() -> list[str]:
    return sorted(_HANDLERS)


def register_info(kind: str, title: str):
    """只登记类型元数据，不提供后台执行体。

    定位灯、蜂鸣器这类瞬时操作是同步执行完再记录的（record_instant），
    不需要 worker 执行体，但审计列表里要能显示中文名称。
    """
    entry = _HANDLERS.setdefault(kind, {})
    entry["title"] = title
    entry["retryable"] = False
    entry.setdefault("fn", None)


# ---- 存储层 ----


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("params"):
        try:
            d["params"] = json.loads(d["params"])
        except (ValueError, TypeError):
            pass
    d["retryable"] = bool(d.get("retryable"))
    return d


def get_task(task_id: int) -> dict | None:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
    return _row_to_dict(row) if row else None


def list_tasks(limit: int = 50, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM tasks"
    where = []
    args: list = []
    if status and status != "all":
        if status == "active":
            where.append("status IN (?, ?)")
            args.extend([STATUS_QUEUED, STATUS_RUNNING])
        else:
            where.append("status = ?")
            args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    conn = db.get_conn()
    return [_row_to_dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def stats() -> dict:
    conn = db.get_conn()
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
    out = {
        STATUS_QUEUED: 0,
        STATUS_RUNNING: 0,
        STATUS_SUCCEEDED: 0,
        STATUS_FAILED: 0,
    }
    for r in rows:
        out[str(r["status"])] = int(r["n"])
    out["active"] = out[STATUS_QUEUED] + out[STATUS_RUNNING]
    return out


def _insert(row: dict) -> int:
    conn = db.get_conn()
    cur = conn.execute(
        "INSERT INTO tasks(kind, title, target, params, command, status, retryable,"
        " attempt, parent_id, operator, progress, message, output, created_at,"
        " started_at, finished_at, ts_epoch)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["kind"],
            row["title"],
            row.get("target") or "",
            json.dumps(row.get("params") or {}, ensure_ascii=False),
            row.get("command") or "",
            row["status"],
            1 if row.get("retryable") else 0,
            int(row.get("attempt") or 1),
            row.get("parent_id"),
            row.get("operator") or "",
            row.get("progress"),
            row.get("message") or "",
            row.get("output") or "",
            row.get("created_at") or _now(),
            row.get("started_at"),
            row.get("finished_at"),
            int(datetime.now().timestamp()),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _update(task_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn = db.get_conn()
    conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), int(task_id)))
    conn.commit()


# ---- 对外入口 ----


def submit(
    kind: str,
    params: dict,
    *,
    target: str = "",
    command: str = "",
    operator: str = "",
    parent_id: int | None = None,
    attempt: int = 1,
) -> dict:
    """登记一个后台任务并入队，返回任务记录。"""
    handler = _HANDLERS.get(kind)
    if not handler or not handler.get("fn"):
        raise ValueError(f"未知任务类型: {kind}")
    task_id = _insert(
        {
            "kind": kind,
            "title": handler["title"],
            "target": target,
            "params": params,
            "command": command,
            "status": STATUS_QUEUED,
            "retryable": handler["retryable"],
            "attempt": attempt,
            "parent_id": parent_id,
            "operator": operator,
            "created_at": _now(),
        }
    )
    _queue.put(task_id)
    return get_task(task_id) or {}


def record_instant(
    kind: str,
    params: dict,
    *,
    ok: bool,
    message: str = "",
    output: str = "",
    target: str = "",
    command: str = "",
    operator: str = "",
) -> int:
    """记录一个已经同步执行完的瞬时操作。

    定位灯、蜂鸣器这类操作耗时不到一秒，进队列只会徒增延迟；
    但审计时间线里不能缺它们，所以直接写一条终态记录。
    """
    handler = _HANDLERS.get(kind)
    now = _now()
    return _insert(
        {
            "kind": kind,
            "title": (handler or {}).get("title") or kind,
            "target": target,
            "params": params,
            "command": command,
            "status": STATUS_SUCCEEDED if ok else STATUS_FAILED,
            "retryable": bool((handler or {}).get("retryable")),
            "operator": operator,
            "progress": 100 if ok else None,
            "message": message,
            "output": output,
            "created_at": now,
            "started_at": now,
            "finished_at": now,
        }
    )


def retry(task_id: int, operator: str = "") -> dict:
    """重试一个失败任务。只允许幂等类型，且必须处于失败终态。"""
    src = get_task(task_id)
    if not src:
        raise ValueError("任务不存在")
    if src["status"] != STATUS_FAILED:
        raise ValueError("只有失败的任务可以重试")
    if not src["retryable"]:
        raise ValueError(f"「{src['title']}」不允许重试：重复执行可能造成额外影响")
    return submit(
        src["kind"],
        src.get("params") or {},
        target=src.get("target") or "",
        command=src.get("command") or "",
        operator=operator,
        parent_id=src["id"],
        attempt=int(src.get("attempt") or 1) + 1,
    )


def start_worker() -> None:
    global _worker, _started
    with _lock:
        if _started:
            return
        _started = True
        db.get_conn()  # 确保 tasks 表已建
        _worker = threading.Thread(target=_loop, name="task-worker", daemon=True)
        _worker.start()


def _loop():
    while True:
        task_id = _queue.get()
        try:
            _run(task_id)
        except Exception:
            traceback.print_exc()
        finally:
            _current["id"] = None
            _queue.task_done()


def _run(task_id: int):
    task = get_task(task_id)
    if not task:
        return
    handler = _HANDLERS.get(task["kind"])
    if not handler:
        _update(
            task_id,
            status=STATUS_FAILED,
            message=f"任务类型 {task['kind']} 未注册",
            finished_at=_now(),
        )
        return

    _current["id"] = task_id
    _update(task_id, status=STATUS_RUNNING, started_at=_now(), progress=0)

    def progress(pct, message: str = ""):
        fields = {"progress": max(0, min(100, int(pct)))}
        if message:
            fields["message"] = message
        _update(task_id, **fields)

    try:
        ok, message, output = handler["fn"](task.get("params") or {}, progress)
    except Exception as e:
        ok, message, output = False, f"执行异常: {e}", traceback.format_exc()

    _update(
        task_id,
        status=STATUS_SUCCEEDED if ok else STATUS_FAILED,
        message=message or "",
        output=(output or "")[:20000],
        progress=100 if ok else None,
        finished_at=_now(),
    )
