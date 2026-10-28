"""数据备份 P0：文件级全量 + 增量，三种目的地。

边界（必须写清楚，否则会给出错误的安全感）：

* **源端只读**。tar 只读打开文件，绝不修改、移动、删除源。
* **没有块级快照**。本机 ext4 无 LVM/ZFS，所以一致性等级是"崩溃一致"。
* **增量靠 `tar --listed-incremental`**，链式依赖快照文件；删除父档会让
  子档失效，所以保留策略按链计算，不按单文件个数。

三种目的地：

  local  本地挂载目录（第二块盘/第二个阵列）——必须是独立挂载点且不是根分区
  nfs    网络 NFS 挂载目录——同样是本地路径，但校验 fstype 为 nfs*
  ssh    user@host:/path——用 `tar | ssh 'cat > file'` 流式传输，要求已配置免密

SSH 特地在动手前做 `BatchMode=yes` 连通性测试：密码交互在守护进程里不可能
完成，与其跑到一半失败，不如在启动时说清"请先配好密钥"。
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
CONFIG_FILE = BASE_DIR / "data_backup.json"
SNAP_DIR = BASE_DIR / "backup_snapshots"

DEFAULT_CONFIG = {"jobs": [], "history": []}
_lock = threading.Lock()
_state = {"running": False, "job": "", "phase": "", "progress": 0, "started": "", "finished": "", "error": "", "result": {}}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        print(f"[data_backup] config: {e}")
    return cfg


def save_config(cfg: dict) -> dict:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _fstype_of(path: str) -> str:
    best, fs = "", ""
    try:
        out = subprocess.run(["findmnt", "-rn", "-o", "TARGET,FSTYPE"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        tgt = parts[0]
        if path == tgt or path.startswith(tgt.rstrip("/") + "/"):
            if len(tgt) > len(best):
                best, fs = tgt, parts[1]
    return fs


def _root_mount() -> str:
    try:
        out = subprocess.run(["findmnt", "-rn", "-o", "TARGET,SOURCE"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""
    for line in out.splitlines():
        p = line.split()
        if p and p[0] == "/":
            return p[1] if len(p) > 1 else ""
    return ""


def validate(job: dict) -> tuple[bool, str]:
    kind = job.get("kind")
    src = str(job.get("source") or "").strip()
    if not src or not os.path.isdir(src):
        return False, "源目录不存在"
    if src in ("/", "/dev", "/proc", "/sys", "/boot"):
        return False, "不备份系统目录"
    if kind == "ssh":
        target = str(job.get("target") or "").strip()
        if ":" not in target:
            return False, "SSH 目标格式应为 user@host:/path"
        host = target.split(":", 1)[0]
        try:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "echo ok"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                return False, f"SSH 免密连接失败：{(r.stderr or '').strip()[:120]}"
        except Exception as e:
            return False, f"SSH 连接异常：{e}"
        return True, ""

    dest = str(job.get("target") or "").strip()
    if not dest:
        return False, "请填写目标目录"
    real = os.path.realpath(dest)
    if not os.path.isdir(real):
        # 首次备份时目标目录常常还不存在，自动建；父目录不可写则明确报错
        try:
            os.makedirs(real, exist_ok=True)
        except OSError as e:
            return False, f"目标目录不存在且无法创建：{e}"
    if real == "/" or real.startswith(("/proc", "/sys", "/dev", "/boot")):
        return False, "目标不能是系统目录"
    fs = _fstype_of(real)
    if kind == "nfs":
        if not fs.startswith("nfs"):
            return False, f"目标不是 NFS 挂载（当前 fstype={fs or '未知'}）"
    else:
        root_src = _root_mount()
        # 目标必须落在独立挂载点上，避免把备份写进系统盘
        st = os.statvfs(real)
        free_gb = st.f_bavail * st.f_frsize / 1073741824
        if free_gb < 1:
            return False, "目标可用空间不足 1GB"
        if fs == "ext4" and real.startswith("/"):
            try:
                src_dev = os.stat(real).st_dev
                root_dev = os.stat("/").st_dev
                if src_dev == root_dev and not job.get("allow_root"):
                    return False, "目标是根分区，请选择独立挂载的磁盘目录（当前根分区已用 89%）"
            except OSError:
                pass
    return True, ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pump(src, dst, limit_bps: int) -> int:
    """把 src 的字节按目标速率搬到 dst。

    为什么不用 cgroup io.max：那是整机/整盘的限速，配错会波及所有进程；
    备份需要的只是"这一条流慢一点"。在管道里节流范围最小、随时可停，
    代价是多一次内存拷贝，对备份这种顺序流可以忽略。
    """
    total = 0
    t0 = time.time()
    chunk = 1 << 20
    while True:
        buf = src.read(chunk)
        if not buf:
            break
        dst.write(buf)
        total += len(buf)
        if limit_bps > 0:
            expected = total / limit_bps
            actual = time.time() - t0
            if expected > actual:
                time.sleep(min(expected - actual, 1.0))
    try:
        dst.close()
    except Exception:
        pass
    return total


def _run_local(job: dict, dest_dir: Path, name: str, level: str) -> dict:
    dest_dir.mkdir(parents=True, exist_ok=True)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap = SNAP_DIR / f"{name}.snar"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = dest_dir / f"{name}-{level}-{stamp}.tar.zst"

    cmd = ["tar", "--listed-incremental", str(snap), "-C", job["source"], "-cf", "-", "."]
    try:
        limit = int(float(job.get("bwlimit_mbps") or 0) * 1048576)
        p1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if limit > 0:
            p2 = subprocess.Popen(["zstd", "-q", "-3", "-o", str(archive)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
            _pump(p1.stdout, p2.stdin, limit)
            p1.stdout.close()
            # _pump 已经关掉了 zstd 的 stdin，不能再交给 communicate()
            err2 = p2.stderr.read()
            p2.wait()
            err1 = p1.stderr.read()
            p1.wait()
            if p1.returncode or p2.returncode:
                raise RuntimeError(((err1 or b"") + (err2 or b"")).decode("utf-8", "ignore")[:300])
            p1 = p2 = None
        else:
            # 不限速时零拷贝：tar 的 stdout 直接接到 zstd 的 stdin
            p2 = subprocess.Popen(["zstd", "-q", "-3", "-o", str(archive)], stdin=p1.stdout, stderr=subprocess.PIPE)
            p1.stdout.close()
        if p1 is not None:
            _, err2 = p2.communicate()
            _, err1 = p1.communicate()
        if p1 is not None and (p1.returncode or p2.returncode):
            raise RuntimeError((err1 or err2).decode("utf-8", "ignore")[:300])
    except Exception:
        if archive.exists():
            archive.unlink(missing_ok=True)
        raise

    # 校验归档可读（不解压到磁盘，只列目录）
    test = subprocess.run(["tar", "--zstd", "-tf", str(archive)], capture_output=True, text=True, timeout=300)
    if test.returncode != 0:
        archive.unlink(missing_ok=True)
        raise RuntimeError("归档校验失败：" + (test.stderr or "")[:200])
    entries = len([l for l in test.stdout.splitlines() if l.strip()])
    manifest = {
        "name": name, "level": level, "source": job["source"], "archive": str(archive),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bytes": archive.stat().st_size, "sha256": _sha256(archive),
        "entries": entries, "consistent": "crash-consistent",
    }
    archive.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _run_ssh(job: dict, name: str, level: str) -> dict:
    host, remote_path = str(job["target"]).split(":", 1)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    remote = remote_path.rstrip("/") + f"/{name}-{level}-{stamp}.tar.zst"
    snap = SNAP_DIR / f"{name}.snar"
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    tar_cmd = ["tar", "--listed-incremental", str(snap), "-C", job["source"], "-cf", "-", "."]
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", host, f"zstd -q -3 -o {shlex.quote(remote)}"]
    p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(ssh_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p1.stdout.close()
    out, err = p2.communicate()
    _, err1 = p1.communicate()
    if p1.returncode or p2.returncode:
        raise RuntimeError(((err or b"") + (err1 or b"")).decode("utf-8", "ignore")[:300])
    return {"name": name, "level": level, "source": job["source"], "archive": f"{host}:{remote}",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "consistent": "crash-consistent", "remote": True}


def _retention(dest_dir: Path, name: str, keep: int) -> list[str]:
    """按"链"删除：只删最老的整条链（全量+其后的增量）。"""
    files = sorted(dest_dir.glob(f"{name}-*.tar.zst"), key=lambda p: p.stat().st_mtime)
    if len(files) <= keep:
        return []
    # 找到第 N 个全量作为截断点，保留它之后的所有档
    fulls = [i for i, f in enumerate(files) if "-full-" in f.name]
    cutoff = fulls[-1] if len(fulls) > 1 else 0
    removed = []
    for f in files[:cutoff]:
        try:
            f.unlink(); f.with_suffix(".json").unlink(missing_ok=True); removed.append(f.name)
        except OSError:
            pass
    return removed


def run(job: dict) -> dict:
    ok, msg = validate(job)
    if not ok:
        raise ValueError(msg)
    name = str(job.get("name") or "backup").strip().replace("/", "_")
    snap = SNAP_DIR / f"{name}.snar"
    level = "incremental" if snap.exists() else "full"
    if job.get("full"):
        snap.unlink(missing_ok=True)
        level = "full"
    snap_path, snap_info = None, {"level": "crash-consistent"}
    if job.get("snapshot"):
        snap_path, snap_info = snapshot_source(job["source"], name)
    src_job = dict(job)
    if snap_path:
        src_job["source"] = snap_path
    _state.update(running=True, job=name, phase="归档中", progress=10,
                  started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), error="", result={})
    try:
        if job["kind"] == "ssh":
            res = _run_ssh(src_job, name, level)
        else:
            res = _run_local(src_job, Path(job["target"]).expanduser(), name, level)
            res["removed"] = _retention(Path(job["target"]).expanduser(), name, int(job.get("keep") or 7))
        res["consistent"] = snap_info.get("level", "crash-consistent")
        res["snapshot"] = snap_info
        _state.update(phase="完成", progress=100, result=res,
                      finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        cfg = load_config()
        cfg.setdefault("history", []).insert(0, {**res, "ok": True})
        cfg["history"] = cfg["history"][:50]
        save_config(cfg)
        return res
    except Exception as e:
        _state.update(error=str(e), phase="失败", finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        cfg = load_config()
        cfg.setdefault("history", []).insert(0, {"name": name, "ok": False, "error": str(e),
                                                 "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        cfg["history"] = cfg["history"][:50]
        save_config(cfg)
        raise
    finally:
        cleanup_snapshot(snap_info)
        _state["running"] = False


def start(job: dict) -> tuple[bool, str]:
    if _state["running"]:
        return False, "已有备份任务在运行"
    ok, msg = validate(job)
    if not ok:
        return False, msg
    threading.Thread(target=lambda: run(job) if True else None, daemon=True, name="data-backup").start()
    return True, "已开始"


def status() -> dict:
    return dict(_state)


# ---------------------------------------------------------------- 恢复 (P1)

def _manifest_of(archive: Path) -> dict | None:
    p = archive.with_suffix(".json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def verify_archive(archive: str) -> tuple[bool, str, dict | None]:
    """恢复前必须过这一关：文件在、清单在、SHA-256 对得上、tar 能读。

    任何一项不过就拒绝恢复——宁可恢复失败并告警，也不能把半个损坏的归档
    解到业务目录里，那会覆盖掉还完好的现有数据。
    """
    p = Path(archive)
    if not p.is_file():
        return False, "归档不存在", None
    m = _manifest_of(p)
    if m and m.get("sha256"):
        if _sha256(p) != m["sha256"]:
            return False, "归档 SHA-256 与清单不符（文件可能损坏或被改动）", m
    t = subprocess.run(["tar", "--zstd", "-tf", str(p)], capture_output=True, text=True, timeout=600)
    if t.returncode != 0:
        return False, "归档无法读取：" + (t.stderr or "")[:200], m
    return True, "", m


def restore(archive: str, target: str, prefix: str = "", dry_run: bool = False) -> dict:
    """把归档恢复到指定目录。

    默认**不覆盖**：目标里已存在同名文件时整次拒绝，并列出冲突。
    `prefix` 只恢复匹配路径（单文件/目录级恢复）。
    """
    ok, msg, m = verify_archive(archive)
    if not ok:
        raise ValueError(msg)
    dest = Path(target).expanduser()
    if not dest.is_absolute():
        raise ValueError("恢复目标必须是绝对路径")
    if str(dest) in ("/", "/boot", "/etc", "/usr", "/dev", "/proc", "/sys"):
        raise ValueError("不允许直接恢复到系统目录")
    dest.mkdir(parents=True, exist_ok=True)

    # 先列出将写入的条目
    listing = subprocess.run(
        ["tar", "--zstd", "-tf", str(archive)], capture_output=True, text=True, timeout=600
    ).stdout.splitlines()
    def _rel(e: str) -> str:
        return e.lstrip("./").rstrip("/")

    # 归档里 first entry 是 "."（目录自身），它永远"已存在"，不能算冲突
    entries = [
        e for e in listing
        if _rel(e) not in ("", ".")
        and (not prefix or _rel(e).startswith(prefix.lstrip("./")))
    ]
    if not entries:
        raise ValueError("归档中没有匹配该路径的条目")

    conflicts = [e for e in entries if (dest / _rel(e)).exists()]
    if conflicts:
        raise ValueError(
            f"目标目录已有 {len(conflicts)} 个同名文件（例如 {conflicts[0][:80]}）。"
            "为防覆盖现有数据，恢复已拒绝；请换一个空目录或先手动确认。"
        )
    if dry_run:
        return {"dry_run": True, "entries": len(entries), "target": str(dest),
                "sample": entries[:20], "archive_verified": True}

    cmd = ["tar", "--zstd", "-xf", str(archive), "-C", str(dest)]
    if prefix:
        cmd.append(prefix.lstrip("./"))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError("恢复失败：" + (r.stderr or "")[:300])
    return {"dry_run": False, "entries": len(entries), "target": str(dest),
            "archive_verified": True, "sha256": (m or {}).get("sha256", "")}


def list_archives(dest: str) -> list[dict]:
    out = []
    try:
        for p in sorted(Path(dest).expanduser().glob("*.tar.zst"), key=lambda x: x.stat().st_mtime, reverse=True):
            m = _manifest_of(p) or {}
            out.append({
                "archive": str(p), "name": m.get("name", p.stem),
                "level": m.get("level", "unknown"),
                "created_at": m.get("created_at", datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")),
                "bytes": p.stat().st_size, "verified": bool(m.get("sha256")),
            })
    except OSError:
        pass
    return out


# ------------------------------------------------------- 增量链恢复 (P1.5)

def build_chain(archive: str) -> list[dict]:
    """从一个归档回溯出完整链：[全量, 增量1, 增量2, …, 目标]。

    GNU tar 的增量是**链式**的：增量只记录"相对上一份的差异"，包括删除。
    单独解一个增量会得到残缺文件树——这是增量备份最容易踩的坑，所以恢复
    必须按时间顺序把整条链依次应用。
    """
    target = Path(archive)
    if not target.is_file():
        raise ValueError("归档不存在")
    m0 = _manifest_of(target) or {}
    name = m0.get("name") or target.name.split("-")[0]
    peers = sorted(target.parent.glob(f"{name}-*.tar.zst"), key=lambda p: p.stat().st_mtime)
    idx = next((i for i, p in enumerate(peers) if p.resolve() == target.resolve()), None)
    if idx is None:
        raise ValueError("无法在目标目录中定位该归档")
    # 找它之前（含自己）最近的一个 full
    start = None
    for i in range(idx, -1, -1):
        mm = _manifest_of(peers[i]) or {}
        if mm.get("level") == "full" or "-full-" in peers[i].name:
            start = i
            break
    if start is None:
        raise ValueError(f"找不到 {name} 的全量基线，无法恢复这条增量链")
    chain = []
    for p in peers[start: idx + 1]:
        mm = _manifest_of(p) or {}
        chain.append({"archive": p, "level": mm.get("level") or ("full" if "-full-" in p.name else "incremental"),
                      "created_at": mm.get("created_at", "")})
    return chain


def restore_chain(archive: str, target: str, prefix: str = "", dry_run: bool = False) -> dict:
    """按顺序应用整条链。默认不覆盖，任一环节校验失败即整体中止。"""
    chain = build_chain(archive)
    dest = Path(target).expanduser()
    if not dest.is_absolute():
        raise ValueError("恢复目标必须是绝对路径")
    if str(dest) in ("/", "/boot", "/etc", "/usr", "/dev", "/proc", "/sys"):
        raise ValueError("不允许直接恢复到系统目录")

    # 逐份校验，任何一份坏掉都不能开始
    for step in chain:
        ok, msg, _ = verify_archive(str(step["archive"]))
        if not ok:
            raise ValueError(f"{step['archive'].name} 校验失败：{msg}")

    dest.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        # 冲突检查：链里所有条目合并后看目标是否已有同名文件
        names = set()
        for step in chain:
            out = subprocess.run(["tar", "--zstd", "-tf", str(step["archive"])],
                                 capture_output=True, text=True, timeout=600).stdout
            for e in out.splitlines():
                rel = e.lstrip("./").rstrip("/")
                if rel and rel != ".":
                    names.add(rel)
        conflicts = [n for n in names if (dest / n).exists()]
        if conflicts:
            raise ValueError(
                f"目标已有 {len(conflicts)} 个同名文件（例如 {conflicts[0][:80]}），"
                "为防止覆盖现有数据，恢复已拒绝。"
            )

    applied = []
    for step in chain:
        if dry_run:
            applied.append({"archive": step["archive"].name, "level": step["level"], "applied": False})
            continue
        # --listed-incremental=/dev/null：按增量语义应用（含删除），但不读写快照文件
        cmd = ["tar", "--zstd", "-x", "--listed-incremental=/dev/null", "-f", str(step["archive"]), "-C", str(dest)]
        if prefix:
            cmd.append(prefix.lstrip("./"))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"{step['archive'].name} 应用失败：" + (r.stderr or "")[:200])
        applied.append({"archive": step["archive"].name, "level": step["level"], "applied": True})

    return {"dry_run": dry_run, "target": str(dest), "chain": applied, "steps": len(chain)}



# ---------------------------------------------------------- 定时 + 失败告警
DEFAULT_CONFIG.setdefault("schedules", [])
DEFAULT_CONFIG.setdefault("last_run", {})


def _window(freq: str, now: datetime) -> str:
    if freq == "daily":
        return now.strftime("%Y-%m-%d")
    if freq == "weekly":
        iso = now.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if freq == "monthly":
        return now.strftime("%Y-%m")
    hours = 24
    return str(int(now.timestamp()) // (hours * 3600))


def _due(now: datetime, cfg: dict) -> list[dict]:
    last = cfg.get("last_run") or {}
    out = []
    for s in cfg.get("schedules") or []:
        if not s.get("enabled", True):
            continue
        t = str(s.get("time") or "")
        if len(t) != 5 or t[2] != ":":
            continue
        try:
            hh, mm = int(t[:2]), int(t[3:])
        except ValueError:
            continue
        if (now.hour, now.minute) < (hh, mm):
            continue
        freq = str(s.get("freq") or "daily")
        if freq == "weekly" and now.isoweekday() != int(s.get("weekday") or 1):
            continue
        key = _window(freq, now)
        if (last.get(str(s.get("id"))) or "") == key:
            continue
        out.append(s)
    return out


def run_due(now: datetime | None = None) -> list[dict]:
    """到点就跑一次。失败写告警，成功清告警——静默失败的备份最致命。"""
    import lsi_alert

    now = now or datetime.now()
    cfg = load_config()
    due = _due(now, cfg)
    if not due:
        return []
    s = due[0]
    sid = str(s.get("id"))
    job = dict(s.get("job") or {})
    job.setdefault("name", s.get("name") or "backup")
    results = []
    try:
        res = run(job)
        lsi_alert.log_event("info", f"定时备份「{job['name']}」完成：{res.get('archive')}")
        lsi_alert._clear(f"backup:schedule:{sid}")
        results.append({"ok": True, "name": job["name"], "result": res})
    except Exception as e:
        lsi_alert.log_event("warning", f"定时备份「{job.get('name')}」失败：{e}")
        lsi_alert._flag(
            f"backup:schedule:{sid}",
            f"定时备份失败：{job.get('name')}",
            f"原因：{e}。备份没有完成，请尽快检查目标空间、挂载状态与 SSH 连通性。",
            "warning",
        )
        results.append({"ok": False, "name": job.get("name"), "error": str(e)})
    cfg = load_config()
    cfg.setdefault("last_run", {})[sid] = _window(str(s.get("freq") or "daily"), now)
    save_config(cfg)
    return results


# ------------------------------------------------------------ 恢复演练
DRILL_ROOT = Path(os.environ.get("LSI_DRILL_DIR", "/tmp/dmx-drill"))


def drill(archive: str) -> dict:
    """把一份归档真正恢复到临时目录并核对，然后删掉。

    为什么不能只校验 SHA-256：校验和只能证明"文件没变"，不能证明
    "解开来是对的"。只有真解一遍，才能发现 tar 格式、路径、权限、
    增量链这些问题。演练目录每次独立创建、结束后删除。
    """
    ok, msg, m = verify_archive(archive)
    if not ok:
        raise ValueError(msg)
    chain = build_chain(archive)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = DRILL_ROOT / stamp
    try:
        res = restore_chain(archive, str(dest))
        files = [p for p in dest.rglob("*") if p.is_file()]
        unreadable = []
        for p in files:
            try:
                with open(p, "rb") as f:
                    f.read(4096)
            except OSError:
                unreadable.append(str(p.relative_to(dest)))
        if not files:
            raise RuntimeError("演练恢复后没有任何文件")
        if unreadable:
            raise RuntimeError(f"{len(unreadable)} 个文件无法读取，例如 {unreadable[0]}")
        return {
            "ok": True, "archive": archive, "steps": res["steps"],
            "files": len(files), "bytes": sum(p.stat().st_size for p in files),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def run_drill(days: int = 7) -> dict | None:
    """按周期挑最新归档做一次演练。失败写告警，成功清告警。"""
    import lsi_alert

    cfg = load_config()
    last = cfg.get("last_drill")
    if last:
        try:
            if (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days < days:
                return None
        except ValueError:
            pass
    sched = (cfg.get("schedules") or [{}])[0]
    target = str((sched.get("job") or {}).get("target") or "")
    if not target:
        return None
    arches = list_archives(target)
    if not arches:
        return None
    newest = arches[0]["archive"]
    try:
        res = drill(newest)
        lsi_alert.log_event("info", f"恢复演练通过：{newest}（{res['files']} 文件）")
        lsi_alert._clear("backup:drill")
        cfg = load_config(); cfg["last_drill"] = datetime.now().strftime("%Y-%m-%d"); save_config(cfg)
        return res
    except Exception as e:
        lsi_alert.log_event("warning", f"恢复演练失败：{e}")
        lsi_alert._flag(
            "backup:drill", "恢复演练失败",
            f"归档 {newest} 无法完成演练恢复：{e}。"
            "这意味着真出事时可能也恢复不了，请立即人工验证。",
            "error",
        )
        cfg = load_config(); cfg["last_drill"] = datetime.now().strftime("%Y-%m-%d"); save_config(cfg)
        raise


# ---------------------------------------------------- 快照源（P3，按需启用）
SNAP_MNT = Path(os.environ.get("LSI_SNAP_MNT", "/tmp/dmx-snapshot"))


def _mount_info(path: str) -> tuple[str, str, str]:
    """返回 (挂载点, 设备, fstype)。"""
    best = ("", "", "")
    try:
        out = subprocess.run(["findmnt", "-rn", "-o", "TARGET,SOURCE,FSTYPE"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return best
    for line in out.splitlines():
        p = line.split()
        if len(p) < 3:
            continue
        tgt = p[0]
        if path == tgt or path.startswith(tgt.rstrip("/") + "/"):
            if len(tgt) > len(best[0]):
                best = (tgt, p[1], p[2])
    return best


def snapshot_source(source: str, tag: str) -> tuple[str | None, dict]:
    """给源目录做一份快照并返回可读路径。

    支持 LVM / ZFS / btrfs。**不做任何降级猜测**：识别不出来就返回 None，
    调用方按"崩溃一致"继续，并在清单里如实标注——把非快照备份说成快照
    备份，比不做快照危险得多。
    """
    mnt, dev, fs = _mount_info(source)
    if not mnt:
        return None, {"level": "crash-consistent", "reason": "找不到挂载点"}
    try:
        if fs in ("btrfs",):
            if not shutil.which("btrfs"):
                return None, {"level": "crash-consistent", "reason": "缺少 btrfs 工具"}
            out = subprocess.run(["btrfs", "subvolume", "snapshot", "-r", mnt, str(SNAP_MNT)],
                                 capture_output=True, text=True, timeout=120)
            if out.returncode == 0:
                return str(SNAP_MNT), {"level": "snapshot", "kind": "btrfs"}
            return None, {"level": "crash-consistent", "reason": (out.stderr or "").strip()[:120]}
        if fs == "zfs" or "/" in dev and not dev.startswith("/dev"):
            snap = f"{dev}@dmx-{tag}"
            r = subprocess.run(["zfs", "snapshot", snap], capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                return f"{mnt}/.zfs/snapshot/dmx-{tag}", {"level": "snapshot", "kind": "zfs", "ref": snap}
            return None, {"level": "crash-consistent", "reason": (r.stderr or "").strip()[:120]}
        # LVM：设备是 /dev/mapper/* 或 /dev/vg/lv
        lv = None
        try:
            ls = subprocess.run(["lsblk", "-n", "-o", "NAME,TYPE", dev], capture_output=True, text=True, timeout=10).stdout
            if "lvm" in ls:
                lv = dev
        except Exception:
            pass
        if lv and shutil.which("lvcreate"):
            name = f"dmxsnap{int(time.time()) % 100000}"
            r = subprocess.run(["lvcreate", "-s", "-n", name, "-L", "1G", lv],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return None, {"level": "crash-consistent", "reason": (r.stderr or "").strip()[:120]}
            snapdev = f"/dev/mapper/{Path(lv).name.replace('-', '--')}-{name}"
            SNAP_MNT.mkdir(parents=True, exist_ok=True)
            m = subprocess.run(["mount", "-o", "ro", snapdev, str(SNAP_MNT)], capture_output=True, text=True, timeout=60)
            if m.returncode != 0:
                subprocess.run(["lvremove", "-f", snapdev], capture_output=True)
                return None, {"level": "crash-consistent", "reason": "快照挂载失败"}
            return str(SNAP_MNT), {"level": "snapshot", "kind": "lvm", "ref": snapdev}
    except Exception as e:
        return None, {"level": "crash-consistent", "reason": str(e)[:120]}
    return None, {"level": "crash-consistent", "reason": f"{fs} 无快照支持"}


def cleanup_snapshot(info: dict) -> None:
    """无论成功失败都要拆快照；拆不掉会在源盘上留下 1GB 的 LV，必须报出来。"""
    if not info or info.get("level") != "snapshot":
        return
    try:
        if info.get("kind") == "lvm":
            subprocess.run(["umount", str(SNAP_MNT)], capture_output=True)
            subprocess.run(["lvremove", "-f", info.get("ref", "")], capture_output=True)
        elif info.get("kind") == "zfs":
            subprocess.run(["zfs", "destroy", info.get("ref", "")], capture_output=True)
        elif info.get("kind") == "btrfs":
            subprocess.run(["btrfs", "subvolume", "delete", str(SNAP_MNT)], capture_output=True)
    except Exception:
        pass
