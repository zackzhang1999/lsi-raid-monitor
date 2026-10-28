"""数据备份、恢复与保留策略。

这套系统的数据只有两类：配置（用户、告警策略、热备策略、授权）与历史样本
（SQLite + CSV）。前者丢了要重新配一遍，后者丢了就没法回溯——出了故障却
查不到当时的状态，是这个产品最没用的时刻。

三件事：

1. 一键备份，分两档：config 只含配置与授权（秒级、几十 KB）；
   full 连历史样本一起打包（分钟级、几十 MB）。日常留 config，
   交付/升级留 full。
2. 保留策略可配置，并且能**先预览**"按这个策略会删掉多少条"再决定。
   直接执行一个不可逆的清理，等于让管理员盲签。
3. 恢复不在运行时直接覆盖文件。SQLite 正被服务进程占用，边跑边覆盖
   轻则读到半截数据库，重则文件损坏。做法是解包到暂存区并留下标记，
   下次启动时先自动备份现状、再应用——这样每一步都可回退。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import time
from datetime import datetime
from pathlib import Path

import db

BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
BACKUP_DIR = BASE_DIR / "backups"
STAGING_DIR = BASE_DIR / ".restore_staging"
PENDING_MARKER = BASE_DIR / ".restore_pending"
CONFIG_FILE = BASE_DIR / "backup_config.json"

# 配置文件白名单：这些丢了要重新配置，且体积很小，永远打进备份
CONFIG_FILES = (
    "users.json",
    "alert_config.json",
    "hotspare_config.json",
    "collection_config.json",
    "backup_config.json",
    "license.json",
    ".license_state.json",
    ".secret_key",
    ".agent_token",
    ".fs_hidden.json",
    ".locate_state.json",
)

DEFAULT_CONFIG = {
    "retention_days": 0,      # 0 = 不清理样本
    "keep_backups": 10,       # 备份文件保留份数
    "auto_backup": True,      # 恢复前自动备份
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# ---- 配置 ----（保留策略与备份份数）


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in DEFAULT_CONFIG:
                    if k in data:
                        cfg[k] = data[k]
    except Exception:
        pass
    # 环境变量优先：容器化部署时用它统一设定，不必逐台改界面
    env_days = os.environ.get("LSI_RETENTION_DAYS")
    if env_days not in (None, ""):
        try:
            cfg["retention_days"] = max(0, int(env_days))
            cfg["_retention_locked"] = True
        except ValueError:
            pass
    cfg["retention_days"] = max(0, min(int(cfg.get("retention_days") or 0), 3650))
    cfg["keep_backups"] = max(1, min(int(cfg.get("keep_backups") or 10), 200))
    cfg["auto_backup"] = bool(cfg.get("auto_backup", True))
    return cfg


def save_config(cfg: dict) -> dict:
    cur = load_config()
    if "_retention_locked" in cur:
        # 环境变量锁定时不允许界面覆盖，否则改了没效果会让人困惑
        cfg = {**cfg, "retention_days": cur["retention_days"]}
    out = {
        "retention_days": max(0, min(int(cfg.get("retention_days") or 0), 3650)),
        "keep_backups": max(1, min(int(cfg.get("keep_backups") or 10), 200)),
        "auto_backup": bool(cfg.get("auto_backup", True)),
    }
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_config()


# ---- 容量与保留策略预览 ----


def data_usage() -> dict:
    """数据目录占用与样本规模，供界面展示"现在有多少数据"。"""
    total = 0
    try:
        for p in BASE_DIR.rglob("*"):
            if p.is_file() and BACKUP_DIR not in p.parents:
                total += p.stat().st_size
    except Exception:
        pass
    out = {"data_bytes": total, "backup_bytes": 0, "db_bytes": 0}
    try:
        for p in BACKUP_DIR.glob("*.tar.gz"):
            out["backup_bytes"] += p.stat().st_size
    except Exception:
        pass
    dbf = BASE_DIR / "lsi.sqlite3"
    if dbf.exists():
        out["db_bytes"] = dbf.stat().st_size
    return out


def preview_prune(days: int) -> dict:
    """预览清理影响。不可逆的操作必须先让人看到代价。"""
    days = int(days or 0)
    conn = db.get_conn()
    total = conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()
    total_n = int(total["n"] or 0) if total else 0
    if days <= 0:
        # 不清理时也要回报样本总数：界面上"样本总数"是容量信息，
        # 跟是否开启清理无关，返回 0 会让人以为数据丢了。
        span = conn.execute("SELECT MIN(ts) AS a, MAX(ts) AS b FROM samples").fetchone()
        return {
            "days": 0,
            "samples": 0,
            "total_samples": total_n,
            "oldest": (span["a"] if span else "") or "",
            "newest": (span["b"] if span else "") or "",
            "cutoff": "",
        }
    cutoff = int(time.time()) - days * 86400
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(ts) AS oldest, MAX(ts) AS newest FROM samples"
        " WHERE ts_epoch < ?",
        (cutoff,),
    ).fetchone()
    return {
        "days": days,
        "cutoff": datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M:%S"),
        "samples": int(row["n"] or 0),
        "total_samples": total_n,
        "oldest": row["oldest"] or "",
        "newest": row["newest"] or "",
    }


def apply_retention() -> dict:
    """按当前策略清理样本，并清理超额备份。返回实际删除了什么。"""
    cfg = load_config()
    removed_samples = db.prune(int(cfg["retention_days"]))
    removed_backups = _prune_backups(int(cfg["keep_backups"]))
    return {
        "removed_samples": removed_samples,
        "removed_backups": removed_backups,
        "retention_days": cfg["retention_days"],
        "keep_backups": cfg["keep_backups"],
    }


def _prune_backups(keep: int) -> int:
    files = sorted(BACKUP_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    n = 0
    for f in files[keep:]:
        try:
            f.unlink()
            n += 1
        except Exception:
            pass
    return n


# ---- 备份 ----


def _write_meta(tar_path: Path, scope: str, note: str) -> dict:
    cfg = load_config()
    meta = {
        "file": tar_path.name,
        "scope": scope,
        "note": note,
        "created_at": _now(),
        "bytes": tar_path.stat().st_size,
        "host": os.uname().nodename,
        "retention_days": cfg["retention_days"],
    }
    return meta


def create_backup(scope: str = "config", note: str = "") -> dict:
    """创建备份。scope: config（配置与授权）或 full（含历史样本）。"""
    scope = "full" if scope == "full" else "config"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"datamax-{scope}-{_stamp()}.tar.gz"
    path = BACKUP_DIR / name
    manifest = {"scope": scope, "created_at": _now(), "note": note, "entries": []}

    def _add(arcname: str, src: Path):
        if src.exists() and src.is_file():
            tar.add(src, arcname=arcname)
            manifest["entries"].append(arcname)

    with tarfile.open(path, "w:gz") as tar:
        # 配置：逐个白名单文件，避免把 .secret_key 之类的权限位漏掉
        for fn in CONFIG_FILES:
            _add(fn, BASE_DIR / fn)
        # SQLite 用备份 API 导出一致性快照，而不是直接拷文件——
        # 服务在跑时文件可能处于中间状态，直接拷贝可能得到损坏的库。
        dbf = BASE_DIR / "lsi.sqlite3"
        if dbf.exists():
            tmp_db = BACKUP_DIR / f".snapshot-{_stamp()}.sqlite3"
            try:
                src_conn = db.get_conn()
                dst = sqlite3.connect(str(tmp_db))
                with dst:
                    src_conn.backup(dst)
                dst.close()
                _add("lsi.sqlite3", tmp_db)
            except Exception:
                # 快照失败时退回直接拷贝，至少有一份可用的粗备份
                _add("lsi.sqlite3", dbf)
            finally:
                try:
                    tmp_db.unlink()
                except Exception:
                    pass
        if scope == "full":
            for child in sorted(BASE_DIR.iterdir()):
                if not child.is_dir():
                    continue
                if child.name in ("backups", ".restore_staging") or child.name.startswith("."):
                    continue
                for f in sorted(child.rglob("*")):
                    if f.is_file():
                        _add(str(f.relative_to(BASE_DIR)), f)
        # manifest 最后写：它记录的是"实际打进去的内容"，
        # 恢复时用来校验归档完整性，不能凭假设。
        data = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        import io as _io

        tar.addfile(info, _io.BytesIO(data))

    meta = _write_meta(path, scope, note)
    _prune_backups(int(load_config()["keep_backups"]))
    return meta


def list_backups() -> list[dict]:
    out = []
    if not BACKUP_DIR.exists():
        return out
    for f in sorted(BACKUP_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True):
        item = {"file": f.name, "bytes": f.stat().st_size,
                "created_at": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with tarfile.open(f, "r:gz") as tar:
                m = tar.extractfile("manifest.json")
                if m:
                    meta = json.loads(m.read().decode("utf-8"))
                    item.update({k: meta.get(k) for k in ("scope", "note", "host")})
                    item["entries"] = len(meta.get("entries") or [])
        except Exception:
            item.setdefault("scope", "unknown")
        out.append(item)
    return out


def backup_path(name: str) -> Path | None:
    """解析备份文件名。必须做路径校验：这是用户可控输入，
    直接拼路径会让 ../../etc/passwd 这类名字读到不该读的文件。"""
    safe = os.path.basename(str(name or ""))
    if not safe.endswith(".tar.gz") or safe.startswith("."):
        return None
    p = (BACKUP_DIR / safe).resolve()
    if BACKUP_DIR.resolve() not in p.parents:
        return None
    return p if p.exists() else None


def delete_backup(name: str) -> bool:
    p = backup_path(name)
    if not p:
        return False
    try:
        p.unlink()
        return True
    except Exception:
        return False


# ---- 恢复 ----


def validate_backup(name: str) -> tuple[bool, str, dict]:
    """校验归档可用性。恢复是不可逆操作，宁可在这一步严格。"""
    p = backup_path(name)
    if not p:
        return False, "备份文件不存在", {}
    try:
        with tarfile.open(p, "r:gz") as tar:
            names = tar.getnames()
            # 拒绝绝对路径与 .. 穿越：一个被篡改的归档不应能写到数据目录之外
            for n in names:
                if n.startswith("/") or ".." in Path(n).parts:
                    return False, f"归档内含不安全路径: {n}", {}
            try:
                m = tar.extractfile("manifest.json")
                meta = json.loads(m.read().decode("utf-8")) if m else {}
            except Exception:
                meta = {}
            if not meta:
                return False, "归档缺少 manifest.json，无法确认内容", {}
            missing = [e for e in (meta.get("entries") or []) if e not in names]
            if missing:
                return False, f"归档不完整，缺少 {len(missing)} 个文件", {}
            return True, "", meta
    except tarfile.TarError as e:
        return False, f"归档无法读取: {e}", {}


def stage_restore(name: str, operator: str = "") -> tuple[bool, str]:
    """把备份解到暂存区并留下标记，等下次启动时应用。

    不在运行时直接覆盖数据目录：SQLite 正被服务进程占用，
    边跑边换文件轻则读到半截库，重则损坏。
    """
    ok, reason, meta = validate_backup(name)
    if not ok:
        return False, reason
    p = backup_path(name)
    assert p is not None
    try:
        if STAGING_DIR.exists():
            shutil.rmtree(STAGING_DIR)
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        with tarfile.open(p, "r:gz") as tar:
            for m in tar.getmembers():
                if m.name == "manifest.json":
                    continue
                tar.extract(m, path=STAGING_DIR)
        PENDING_MARKER.write_text(
            json.dumps(
                {"file": name, "staged_at": _now(), "operator": operator,
                 "scope": meta.get("scope"), "created_at": meta.get("created_at")},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        return False, f"解包失败: {e}"
    return True, ""


def pending_restore() -> dict | None:
    try:
        if PENDING_MARKER.exists():
            return json.loads(PENDING_MARKER.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def cancel_restore() -> bool:
    try:
        if PENDING_MARKER.exists():
            PENDING_MARKER.unlink()
        if STAGING_DIR.exists():
            shutil.rmtree(STAGING_DIR)
        return True
    except Exception:
        return False


def apply_pending_restore() -> dict | None:
    """启动时调用：先备份现状，再应用暂存的恢复内容。

    顺序不能反——先覆盖再备份，等于把仅有的那份"可回退状态"毁掉。
    """
    info = pending_restore()
    if not info or not STAGING_DIR.exists():
        return None
    result = {"restore": info, "auto_backup": None, "applied": 0, "errors": []}
    cfg = load_config()
    if cfg.get("auto_backup", True):
        try:
            result["auto_backup"] = create_backup("config", note="恢复前自动备份")["file"]
        except Exception as e:
            result["errors"].append(f"自动备份失败: {e}")

    for src in sorted(STAGING_DIR.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(STAGING_DIR)
        dst = BASE_DIR / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result["applied"] += 1
        except Exception as e:
            result["errors"].append(f"{rel}: {e}")
    cancel_restore()
    return result
