#!/usr/bin/env python3
# ================================================
# 用户管理模块
# 用户存储在 $LSI_DATA_DIR/users.json，口令使用 PBKDF2-HMAC-SHA256 加盐哈希
# 角色: admin（全部权限） / viewer（只读）
# 文件中没有任何用户时视为“未启用认证”，Web 端提示创建第一个管理员
# ================================================

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
USERS_FILE = BASE_DIR / "users.json"

_PBKDF2_ITERATIONS = 120_000


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return dk.hex()


def load_users() -> dict:
    try:
        if USERS_FILE.exists():
            with open(USERS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        print(f"[user_mgr] users load error: {e}")
    return {}


def save_users(users: dict):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, USERS_FILE)


def users_exist() -> bool:
    return len(load_users()) > 0


def list_users() -> list[dict]:
    return [
        {"username": name, "role": rec.get("role", "viewer")}
        for name, rec in sorted(load_users().items())
    ]


def verify_password(username: str, password: str) -> str | None:
    """校验成功返回角色，失败返回 None"""
    rec = load_users().get(username)
    if not rec:
        return None
    try:
        salt = bytes.fromhex(rec["salt"])
    except (KeyError, ValueError):
        return None
    expected = rec.get("password_hash", "")
    actual = _hash_password(password, salt)
    if secrets.compare_digest(expected, actual):
        return rec.get("role", "viewer")
    return None


def create_user(username: str, password: str, role: str = "viewer") -> tuple[bool, str]:
    username = (username or "").strip()
    if not username or not password:
        return False, "用户名和口令不能为空"
    if role not in ("admin", "viewer"):
        return False, "非法角色"
    users = load_users()
    if username in users:
        return False, "用户已存在"
    salt = secrets.token_bytes(16)
    users[username] = {
        "role": role,
        "salt": salt.hex(),
        "password_hash": _hash_password(password, salt),
    }
    save_users(users)
    return True, "创建成功"


def delete_user(username: str) -> tuple[bool, str]:
    users = load_users()
    if username not in users:
        return False, "用户不存在"
    del users[username]
    save_users(users)
    return True, "已删除"


def set_password(username: str, password: str) -> tuple[bool, str]:
    if not password:
        return False, "口令不能为空"
    users = load_users()
    rec = users.get(username)
    if not rec:
        return False, "用户不存在"
    salt = secrets.token_bytes(16)
    rec["salt"] = salt.hex()
    rec["password_hash"] = _hash_password(password, salt)
    save_users(users)
    return True, "口令已重置"
