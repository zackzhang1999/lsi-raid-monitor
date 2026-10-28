"""多机管理：中心节点登记并采集被管节点。

这是本产品从"单机工具"变成"机房产品"的分界线。目标很具体：
一个页面看到所有机器的健康，某台机器连不上不会影响其它机器的采集。

采集方向选择**中心拉取**而不是节点上报：

- 被管节点不需要对中心开放任何入站端口，只需中心能访问它的 Web 端口
- 凭据集中在中心，节点侧只需一个只读令牌
- 某台节点宕机时，中心侧的超时与重试逻辑是唯一的故障处理路径，
  不用在每个节点上都实现一遍断线缓存

正确性上有一条硬要求：**任何一台机器的失败都必须被隔离**。采集循环里
每次请求单独 try/except，超时的节点只记一条错误快照，绝不中断整轮采集。
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import db

BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
TOKEN_FILE = BASE_DIR / ".agent_token"
REMOTE_ACK_FILE = BASE_DIR / "remote_ack_config.json"

POLL_INTERVAL = int(os.environ.get("LSI_HOST_POLL_INTERVAL", "60") or 60)
POLL_TIMEOUT = int(os.environ.get("LSI_HOST_POLL_TIMEOUT", "8") or 8)
KEEP_SAMPLES = 500

_started = False
_lock = threading.Lock()
_last = {"ts": 0.0, "results": []}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---- 被管节点侧：只读令牌 ----


def agent_token() -> str:
    """本节点的采集令牌。首次调用时生成并持久化——
    重启后令牌不变，否则中心侧每次都要重新配置。"""
    try:
        if TOKEN_FILE.exists():
            t = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if t:
                return t
    except Exception:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(tok, encoding="utf-8")
        os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass
    return tok


def rotate_agent_token() -> str:
    try:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
    except Exception:
        pass
    return agent_token()


def token_matches(token: str) -> bool:
    ref = agent_token()
    return bool(token) and secrets.compare_digest(str(token), ref)


def remote_ack_enabled() -> bool:
    """中心能否代本节点确认告警。

    默认关闭。采集令牌的承诺是"只读、不能执行任何操作"，让它顺手获得写
    权限等于悄悄扩大了它的授权范围。要做这件事必须由被管节点自己显式
    打开，并且只放开"确认告警"这一项——确认只改告警状态，不碰任何数据。
    """
    try:
        if REMOTE_ACK_FILE.exists():
            cfg = json.loads(REMOTE_ACK_FILE.read_text(encoding="utf-8"))
            return bool(cfg.get("allow_remote_ack"))
    except Exception:
        pass
    return False


def set_remote_ack(enabled: bool) -> bool:
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        REMOTE_ACK_FILE.write_text(
            json.dumps({"allow_remote_ack": bool(enabled)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


# ---- 中心侧：主机登记 ----


def _host_row(r) -> dict:
    d = dict(r)
    d["enabled"] = bool(d.get("enabled"))
    d["local"] = bool(d.get("local"))
    d.pop("token", None)  # 令牌不出现在列表里，只有编辑时单独取
    return d


def _ensure_group_column() -> None:
    """给 hosts 表补 group_name 列。

    老库没有这一列，而 SQLite 的 ALTER TABLE ADD COLUMN 是幂等的（重复
    添加会报错，所以先查一次）。分组是多机管理的第一需求：几十台机器
    不分机房/环境，列表就没法用。
    """
    conn = db.get_conn()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(hosts)").fetchall()}
    if "group_name" not in cols:
        conn.execute("ALTER TABLE hosts ADD COLUMN group_name TEXT DEFAULT ''")
        conn.commit()


def list_hosts() -> list[dict]:
    conn = db.get_conn()
    _ensure_group_column()
    rows = conn.execute("SELECT * FROM hosts ORDER BY local DESC, id").fetchall()
    out = []
    for r in rows:
        h = _host_row(r)
        snap = latest_sample(h["id"])
        h["online"] = bool(snap and snap["online"])
        h["last_seen"] = snap["ts"] if snap else ""
        h["error"] = (snap or {}).get("error") or ""
        h["summary"] = (snap or {}).get("data") or {}
        out.append(h)
    return out


def get_host(host_id: int) -> dict | None:
    conn = db.get_conn()
    r = conn.execute("SELECT * FROM hosts WHERE id = ?", (int(host_id),)).fetchone()
    return _host_row(r) if r else None


def get_host_token(host_id: int) -> str:
    conn = db.get_conn()
    r = conn.execute("SELECT token FROM hosts WHERE id = ?", (int(host_id),)).fetchone()
    return str(r["token"]) if r else ""


def add_host(name: str, address: str, token: str, note: str = "", group_name: str = "") -> dict:
    _ensure_group_column()
    name = str(name or "").strip()
    address = str(address or "").strip().rstrip("/")
    if not name:
        raise ValueError("请填写主机名称")
    if not address:
        raise ValueError("请填写主机地址")
    if not address.startswith(("http://", "https://")):
        address = "http://" + address
    conn = db.get_conn()
    exist = conn.execute("SELECT id FROM hosts WHERE name = ?", (name,)).fetchone()
    if exist:
        raise ValueError(f"主机名称「{name}」已存在")
    cur = conn.execute(
        "INSERT INTO hosts(name, address, token, enabled, note, local, created_at, group_name)"
        " VALUES (?,?,?,1,?,0,?,?)",
        (name, address, str(token or "").strip(), str(note or "").strip(), _now(),
         str(group_name or "").strip()),
    )
    conn.commit()
    return get_host(cur.lastrowid) or {}


def update_host(host_id: int, **fields) -> dict | None:
    _ensure_group_column()
    allowed = {
        k: v for k, v in fields.items()
        if k in ("name", "address", "token", "note", "enabled", "group_name")
    }
    if not allowed:
        return get_host(host_id)
    if "enabled" in allowed:
        allowed["enabled"] = 1 if allowed["enabled"] else 0
    if "address" in allowed:
        a = str(allowed["address"]).strip().rstrip("/")
        if not a.startswith(("http://", "https://")):
            a = "http://" + a
        allowed["address"] = a
    cols = ", ".join(f"{k} = ?" for k in allowed)
    conn = db.get_conn()
    conn.execute(f"UPDATE hosts SET {cols} WHERE id = ?", (*allowed.values(), int(host_id)))
    conn.commit()
    return get_host(host_id)


def remove_host(host_id: int) -> bool:
    conn = db.get_conn()
    row = conn.execute("SELECT local FROM hosts WHERE id = ?", (int(host_id),)).fetchone()
    if not row or int(row["local"] or 0) == 1:
        return False  # 本机条目不允许删除
    conn.execute("DELETE FROM host_samples WHERE host_id = ?", (int(host_id),))
    conn.execute("DELETE FROM hosts WHERE id = ?", (int(host_id),))
    conn.commit()
    return True


def ensure_local_host(name: str | None = None, port: int | None = None) -> dict:
    """把中心自己登记为一个主机条目。

    单机用户看到的应该是"一台机器、正常"，而不是一个空列表——
    空列表会被理解成功能坏了。
    """
    host = name or socket.gethostname()
    addr = f"http://127.0.0.1:{port or os.environ.get('LSI_WEB_PORT', '5200')}"
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM hosts WHERE local = 1").fetchone()
    if row:
        if row["name"] != host or row["address"] != addr:
            conn.execute(
                "UPDATE hosts SET name = ?, address = ? WHERE id = ?",
                (host, addr, row["id"]),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM hosts WHERE id = ?", (row["id"],)).fetchone()
        return _host_row(row)
    cur = conn.execute(
        "INSERT INTO hosts(name, address, token, enabled, note, local, created_at)"
        " VALUES (?,?,?,1,?,1,?)",
        (host, addr, "", "本机（中心节点）", _now()),
    )
    conn.commit()
    return get_host(cur.lastrowid) or {}


# ---- 中心侧：快照采集 ----


def save_sample(host_id: int, online: bool, data: dict | None, error: str = "") -> None:
    conn = db.get_conn()
    ts = _now()
    conn.execute(
        "INSERT OR REPLACE INTO host_samples(host_id, ts, ts_epoch, online, error, data)"
        " VALUES (?,?,?,?,?,?)",
        (
            int(host_id),
            ts,
            int(time.time()),
            1 if online else 0,
            error or "",
            json.dumps(data or {}, ensure_ascii=False) if data else None,
        ),
    )
    # 只保留最近 N 条，避免长期运行把库撑大
    conn.execute(
        "DELETE FROM host_samples WHERE host_id = ? AND id NOT IN"
        " (SELECT id FROM host_samples WHERE host_id = ? ORDER BY id DESC LIMIT ?)",
        (int(host_id), int(host_id), KEEP_SAMPLES),
    )
    conn.commit()


def latest_sample(host_id: int) -> dict | None:
    conn = db.get_conn()
    r = conn.execute(
        "SELECT * FROM host_samples WHERE host_id = ? ORDER BY id DESC LIMIT 1",
        (int(host_id),),
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["data"] = json.loads(d["data"]) if d.get("data") else {}
    except (ValueError, TypeError):
        d["data"] = {}
    return d


def history(host_id: int, limit: int = 60) -> list[dict]:
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT ts, online, error FROM host_samples WHERE host_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (int(host_id), max(1, min(int(limit), 500))),
    ).fetchall()
    return [dict(r) for r in rows][::-1]


def fetch_summary(address: str, token: str) -> tuple[bool, dict | None, str]:
    """拉取一个节点的摘要。返回 (是否成功, 数据, 错误)。"""
    url = address.rstrip("/") + "/api/agent/summary"
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Agent-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT) as resp:
            if resp.status != 200:
                return False, None, f"HTTP {resp.status}"
            body = resp.read().decode("utf-8", errors="replace")
        return True, json.loads(body), ""
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, None, "采集令牌不匹配"
        return False, None, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, None, f"无法连接: {e.reason}"
    except Exception as e:
        return False, None, str(e)


def poll_all(local_summary: dict | None = None) -> list[dict]:
    """采集一轮所有启用的主机。

    每台机器的失败被单独捕获：一台超时不能拖垮整轮采集，
    否则机房里一台设备故障会让整个中心页面的数据一起停更。
    本机条目直接用内存里的状态，不走 HTTP——本地取数没有必要绕一圈网络。
    """
    results = []
    for h in list_hosts():
        if not h.get("enabled"):
            continue
        if h.get("local"):
            if local_summary is not None:
                save_sample(h["id"], True, local_summary, "")
                results.append({"id": h["id"], "name": h["name"], "online": True, "error": ""})
            continue
        token = get_host_token(h["id"])
        ok, data, err = fetch_summary(h["address"], token)
        save_sample(h["id"], ok, data, err)
        results.append({"id": h["id"], "name": h["name"], "online": ok, "error": err})
    return results


def summary_stats() -> dict:
    hosts = list_hosts()
    online = sum(1 for h in hosts if h.get("online"))
    return {
        "total": len(hosts),
        "online": online,
        "offline": len(hosts) - online,
    }


def unified_alerts(include_acked: bool = False) -> list[dict]:
    """把所有节点的未处理告警汇成一张表。

    每条都带主机归属——多机视图里"哪台机器出的问题"和"出了什么问题"
    一样重要，少了归属就得挨个点进去找。
    """
    out: list[dict] = []
    for h in list_hosts():
        s = h.get("summary") or {}
        if not h.get("online"):
            # 节点失联本身就是要报的：中心的其他数据全在，唯独这台没有，
            # 不显式标出来就会被当成"这台没问题"。
            out.append({
                "host_id": h["id"],
                "host": h["name"],
                "address": h["address"],
                "local": h.get("local"),
                "alert_id": None,
                "key": f"host:{h['id']}:offline",
                "level": "error",
                "title": f"节点 {h['name']} 失联",
                "detail": h.get("error") or "无法获取该节点状态",
                "status": "active",
                "first_seen": "",
                "last_seen": h.get("last_seen") or "",
                "offline": True,
            })
            continue
        for a in s.get("alerts") or []:
            if not include_acked and a.get("status") != "active":
                continue
            out.append({
                "host_id": h["id"],
                "host": h["name"],
                "address": h["address"],
                "local": h.get("local"),
                "alert_id": a.get("id"),
                "key": a.get("key"),
                "level": a.get("level") or "warning",
                "title": a.get("title") or "",
                "detail": a.get("detail") or "",
                "status": a.get("status"),
                "count": a.get("count"),
                "first_seen": a.get("first_seen") or "",
                "last_seen": a.get("last_seen") or "",
                "offline": False,
            })
    rank = {"error": 0, "warning": 1, "info": 2}
    out.sort(key=lambda x: (rank.get(x.get("level"), 3), x.get("last_seen") or ""))
    return out


def remote_ack(node_address: str, token: str, alert_id: int) -> tuple[bool, str]:
    """请求某个节点确认它自己的告警。"""
    url = node_address.rstrip("/") + f"/api/agent/alerts/{int(alert_id)}/ack"
    req = urllib.request.Request(url, method="POST", data=b"{}")
    req.add_header("X-Agent-Token", token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            j = json.loads(body)
        except ValueError:
            j = {}
        if j.get("ok"):
            return True, ""
        return False, j.get("error") or f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode("utf-8", errors="replace"))
            return False, j.get("error") or f"HTTP {e.code}"
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)


def check_host_alerts() -> None:
    """节点失联时在中心侧产生告警。

    只把"离线"显示在列表里是不够的：中心页面的其它数据都在，唯独少了一台，
    看起来和"这台没问题"没有区别。必须变成告警才会有人去处理。
    """
    import lsi_alert

    for h in list_hosts():
        key = f"host:{h['id']}:offline"
        if h.get("online"):
            lsi_alert._clear(key)
            continue
        name = h.get("name") or f"#{h['id']}"
        lsi_alert._flag(
            key,
            f"存储节点 {name} 失联",
            f"中心无法获取 {name}（{h.get('address')}）的状态："
            f"{h.get('error') or '未知原因'}。该节点的监控数据已停止更新，"
            f"请检查网络连通性与被管节点的服务状态。",
            "error",
        )


def last_poll() -> dict:
    return {"ts": _last["ts"], "results": _last["results"]}


def start_poller(local_summary_fn) -> None:
    """启动后台采集线程。local_summary_fn 用于取本机摘要，避免循环依赖
    （本模块不 import web_server，由调用方把取数函数传进来）。"""
    global _started
    with _lock:
        if _started:
            return
        _started = True

        def _loop():
            while True:
                try:
                    ensure_local_host()
                    local = None
                    try:
                        local = local_summary_fn()
                    except Exception:
                        local = None
                    _last["results"] = poll_all(local)
                    _last["ts"] = time.time()
                    try:
                        check_host_alerts()
                    except Exception:
                        pass
                except Exception:
                    pass
                time.sleep(max(15, POLL_INTERVAL))

        threading.Thread(target=_loop, daemon=True, name="host-poller").start()
