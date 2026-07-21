#!/usr/bin/env python3
# ================================================
# 用户管理模块：管理员 / 普通用户（只读）
# 用户存于 $LSI_DATA_DIR/users.json，口令以 PBKDF2 哈希存储
#
# 角色:
#   admin   拥有全部权限（含磁盘操作、格式化、用户管理等危险操作）
#   viewer  只能查看，所有写操作接口返回 403
#
# 兼容旧版: 首次启动时若设置了 WEB_PASSWORD 且尚无 users.json，
# 自动迁移为 admin 用户（用户名 admin，口令即 WEB_PASSWORD）。
# ================================================

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(PROJECT_ROOT / "data")))
USERS_FILE = BASE_DIR / "users.json"

ROLES = ("admin", "viewer")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_PBKDF2_ITERATIONS = 100_000


# ---- 口令哈希 ----


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        _PBKDF2_ITERATIONS,
    ).hex()


# ---- 用户存储 ----


def load_users() -> list[dict]:
    if not USERS_FILE.exists():
        return []
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        users = data.get("users", [])
        return users if isinstance(users, list) else []
    except (OSError, ValueError) as e:
        print(f"users load error: {e}", file=sys.stderr)
        return []


def save_users(users: list[dict]) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass


def users_exist() -> bool:
    return bool(load_users())


def ensure_bootstrap(web_password: str = "") -> None:
    """无用户时按 WEB_PASSWORD 迁移出一个 admin 用户；两者皆无则保持免认证。"""
    if users_exist():
        return
    if not web_password:
        return
    save_users([_make_user("admin", web_password, "admin")])


def _make_user(username: str, password: str, role: str) -> dict:
    salt = secrets.token_hex(16)
    return {
        "username": username,
        "role": role,
        "salt": salt,
        "password_hash": _hash_password(password, salt),
    }


# ---- 认证 ----


def verify(username: str, password: str) -> str | None:
    """校验用户名口令，成功返回角色，失败返回 None。"""
    for u in load_users():
        if u.get("username") != username:
            continue
        expected = u.get("password_hash", "")
        actual = _hash_password(password, u.get("salt", ""))
        if hmac.compare_digest(expected, actual):
            return u.get("role") if u.get("role") in ROLES else "viewer"
        return None
    return None


# ---- 用户管理（管理员） ----


def list_users() -> list[dict]:
    """返回不含哈希的用户列表。"""
    return [
        {"username": u.get("username", ""), "role": u.get("role", "viewer")}
        for u in load_users()
    ]


def _validate(username: str, password: str | None, role: str | None) -> str | None:
    if not _USERNAME_RE.match(username or ""):
        return "用户名须为 1-32 位字母、数字、_ . -"
    if password is not None and not (1 <= len(password) <= 128):
        return "口令长度须为 1-128 字符"
    if role is not None and role not in ROLES:
        return f"角色无效（仅支持 {'/'.join(ROLES)}）"
    return None


def create_user(username: str, password: str, role: str) -> tuple[bool, str]:
    err = _validate(username, password, role)
    if err:
        return False, err
    users = load_users()
    if any(u.get("username") == username for u in users):
        return False, f"用户已存在: {username}"
    users.append(_make_user(username, password, role))
    save_users(users)
    return True, username


def set_password(username: str, password: str) -> tuple[bool, str]:
    err = _validate(username, password, None)
    if err:
        return False, err
    users = load_users()
    for u in users:
        if u.get("username") == username:
            u["salt"] = secrets.token_hex(16)
            u["password_hash"] = _hash_password(password, u["salt"])
            save_users(users)
            return True, username
    return False, f"用户不存在: {username}"


def delete_user(username: str, current_username: str) -> tuple[bool, str]:
    if username == current_username:
        return False, "不能删除当前登录用户"
    users = load_users()
    target = next((u for u in users if u.get("username") == username), None)
    if target is None:
        return False, f"用户不存在: {username}"
    if target.get("role") == "admin":
        admins = [u for u in users if u.get("role") == "admin"]
        if len(admins) <= 1:
            return False, "至少需要保留一个管理员"
    save_users([u for u in users if u.get("username") != username])
    return True, username
