"""服务器本地 PAM 认证。

通过 ctypes 调用系统 libpam，验证用户名/口令；不依赖第三方 Python 包。
角色映射：默认只有 root 是 admin；可用环境变量显式指定额外管理员：
  LSI_PAM_ADMIN_USERS  - 逗号分隔的用户名（如 root,ops）
  LSI_PAM_ADMIN_GROUP  - 管理员组名（不设置则只有 root/显式用户是 admin）
其余系统用户一律视为 viewer（只读）。
"""

from __future__ import annotations

import ctypes
import grp
import os
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    c_char_p,
    c_int,
    c_size_t,
    c_void_p,
    cast,
)
from ctypes.util import find_library

PAM_SUCCESS = 0
PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2
PAM_ERROR_MSG = 3
PAM_TEXT_INFO = 4


class PamMessage(Structure):
    _fields_ = [("msg_style", c_int), ("msg", c_char_p)]


class PamResponse(Structure):
    _fields_ = [("resp", c_char_p), ("resp_retcode", c_int)]


_conv_cb = CFUNCTYPE(
    c_int,
    c_int,
    POINTER(POINTER(PamMessage)),
    POINTER(POINTER(PamResponse)),
    c_void_p,
)


class PamConv(Structure):
    _fields_ = [("conv", _conv_cb), ("appdata_ptr", c_void_p)]


_libpam = ctypes.CDLL(find_library("pam") or "libpam.so.0")
_libc = ctypes.CDLL(None)

_pam_start = _libpam.pam_start
_pam_start.restype = c_int
_pam_start.argtypes = [c_char_p, c_char_p, POINTER(PamConv), POINTER(c_void_p)]

_pam_authenticate = _libpam.pam_authenticate
_pam_authenticate.restype = c_int
_pam_authenticate.argtypes = [c_void_p, c_int]

_pam_acct_mgmt = _libpam.pam_acct_mgmt
_pam_acct_mgmt.restype = c_int
_pam_acct_mgmt.argtypes = [c_void_p, c_int]

_pam_end = _libpam.pam_end
_pam_end.restype = c_int
_pam_end.argtypes = [c_void_p, c_int]

_libc.calloc.restype = c_void_p
_libc.calloc.argtypes = [c_size_t, c_size_t]
_libc.strdup.restype = c_void_p
_libc.strdup.argtypes = [c_char_p]

_password = None


@_conv_cb
def _conversation(num_msg, msg, resp, appdata_ptr):
    global _password
    if num_msg <= 0 or not msg or not resp:
        return 1
    block = _libc.calloc(num_msg, ctypes.sizeof(PamResponse))
    if not block:
        return 1
    responses = cast(block, POINTER(PamResponse))
    for i in range(num_msg):
        style = msg[i].contents.msg_style
        if style in (PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON) and _password:
            responses[i].resp = cast(_libc.strdup(_password.encode("utf-8")), c_char_p)
            responses[i].resp_retcode = 0
        else:
            responses[i].resp = c_char_p()
            responses[i].resp_retcode = 0
    resp[0] = responses
    return 0


def authenticate(username: str, password: str, service: str | None = None) -> bool:
    global _password
    username = (username or "").strip()
    if not username or password is None:
        return False
    service = service or os.environ.get("LSI_PAM_SERVICE", "login")
    _password = password
    conv = PamConv(_conversation, None)
    pamh = c_void_p()
    try:
        if _pam_start(service.encode("utf-8"), username.encode("utf-8"), ctypes.byref(conv), ctypes.byref(pamh)) != PAM_SUCCESS:
            return False
        try:
            if _pam_authenticate(pamh, 0) != PAM_SUCCESS:
                return False
            if _pam_acct_mgmt(pamh, 0) != PAM_SUCCESS:
                return False
            return True
        finally:
            _pam_end(pamh, 0)
    finally:
        _password = None


def _in_group(username: str, group: str) -> bool:
    try:
        members = grp.getgrnam(group).gr_mem
        if username in members:
            return True
        # 主组或附加组
        import pwd

        u = pwd.getpwnam(username)
        if u.pw_gid == grp.getgrnam(group).gr_gid:
            return True
        for g in os.getgrouplist(username, u.pw_gid):
            if g == grp.getgrnam(group).gr_gid:
                return True
    except Exception:
        return False
    return False


def user_role(username: str) -> str:
    if username == "root":
        return "admin"
    admin_users = {
        u.strip()
        for u in os.environ.get("LSI_PAM_ADMIN_USERS", "").split(",")
        if u.strip()
    }
    if username in admin_users:
        return "admin"
    admin_group = os.environ.get("LSI_PAM_ADMIN_GROUP", "")
    if admin_group and _in_group(username, admin_group):
        return "admin"
    return "viewer"
