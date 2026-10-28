"""授权与版本分级。

离线授权的核心是**非对称签名**：厂商用私钥签发，产品内置公钥验签。
私钥不出厂商机房，客户机器上即使拿到全部代码也无法伪造授权——公钥
只能验签、不能签名。

两个刻意的设计选择：

1. 运行环境缺少 cryptography 时**拒绝**接受任何签名授权，只保留社区版
   与试用期。宁可功能受限，也不能接受一个无法验证的授权文件——那等于
   把授权形同虚设。
2. 未授权时退回社区版而不是停机。存储管理系统停机会让客户看不见自己
   的阵列，拿客户的生产数据当作催款手段是不体面的，也留不住客户。

试用期为首次启动起 30 天，期间开放企业版全部功能，便于评估。
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(os.environ.get("LSI_DATA_DIR", str(Path(__file__).resolve().parent / "data")))
LICENSE_FILE = BASE_DIR / "license.json"
STATE_FILE = BASE_DIR / ".license_state.json"

# 产品内置公钥（对应 tools/license_tool.py keygen 生成的私钥）。
# 换厂商密钥时只需替换这一行。
PUBLIC_KEY_B64 = "73DC2BMXyEciPKFWjHSuXTHVM1vFFPV363CLUFJ3ZF8="

TRIAL_DAYS = 30

EDITIONS = {
    "community": {"name": "社区版", "features": []},
    "pro": {
        "name": "专业版",
        "features": ["tasks", "reports", "bcache", "alert_closure", "multictrl", "metrics"],
    },
    "enterprise": {
        "name": "企业版",
        "features": [
            "tasks", "reports", "bcache", "alert_closure", "multictrl", "metrics",
            "audit", "multihost", "rbac", "sso",
        ],
    },
}

# 功能键 -> (显示名, 所属版本)
FEATURES = {
    "tasks": ("异步任务中心", "pro"),
    "reports": ("运行报表与导出", "pro"),
    "bcache": ("SSD / NVMe 缓存加速", "pro"),
    "alert_closure": ("告警确认与维护窗口", "pro"),
    "multictrl": ("多控制器管理", "pro"),
    "metrics": ("Prometheus / Grafana 对接", "pro"),
    "audit": ("不可变审计与导出", "enterprise"),
    "multihost": ("多机统一管理", "enterprise"),
    "rbac": ("细粒度角色权限", "enterprise"),
    "sso": ("SSO / MFA", "enterprise"),
}

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAS_CRYPTO = False


def _canonical(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def verify_document(doc: dict) -> tuple[bool, str]:
    """验证授权文件的签名与内容。返回 (是否有效, 原因)。"""
    if not _HAS_CRYPTO:
        return False, "运行环境缺少 cryptography 库，无法验证签名授权"
    if not isinstance(doc, dict) or "payload" not in doc or "sig" not in doc:
        return False, "授权文件格式不正确"
    payload = doc.get("payload")
    if not isinstance(payload, dict):
        return False, "授权文件缺少 payload"
    if payload.get("product") != "DataMax-LSI":
        return False, "授权文件不属于本产品"
    try:
        raw = base64.b64decode(PUBLIC_KEY_B64)
        pub = Ed25519PublicKey.from_public_bytes(raw)
        pub.verify(base64.b64decode(str(doc["sig"])), _canonical(payload))
    except Exception:
        return False, "授权文件签名无效（可能被篡改或非本厂商签发）"
    exp = str(payload.get("expires_at") or "").strip()
    if exp:
        try:
            if datetime.strptime(exp, "%Y-%m-%d").date() < datetime.now().date():
                return False, f"授权已于 {exp} 到期"
        except ValueError:
            return False, "授权有效期格式不正确"
    return True, ""


def load_license() -> dict | None:
    try:
        if LICENSE_FILE.exists():
            return json.loads(LICENSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def install_license(text: str) -> tuple[bool, str]:
    """安装授权文件。校验不通过则拒绝写入——不能把无效授权留在磁盘上，
    否则每次启动都会走一遍失败路径。"""
    try:
        doc = json.loads(text)
    except ValueError as e:
        return False, f"不是合法的 JSON: {e}"
    ok, reason = verify_document(doc)
    if not ok:
        return False, reason
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        LICENSE_FILE.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        return False, f"写入失败: {e}"
    return True, ""


def remove_license() -> bool:
    try:
        if LICENSE_FILE.exists():
            LICENSE_FILE.unlink()
            return True
    except Exception:
        pass
    return False


# ---- 试用期 ----


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict):
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def trial_info() -> dict:
    """试用期状态。首次调用时记录起始时间——试用从第一次启动算，
    而不是从安装包构建时间算，否则客户拿到货时可能已经过期。"""
    state = _load_state()
    first = state.get("first_run")
    if not first:
        first = datetime.now().strftime("%Y-%m-%d")
        state["first_run"] = first
        _save_state(state)
    try:
        start = datetime.strptime(first, "%Y-%m-%d").date()
    except ValueError:
        start = datetime.now().date()
    end = start + timedelta(days=TRIAL_DAYS)
    left = (end - datetime.now().date()).days
    return {
        "first_run": start.strftime("%Y-%m-%d"),
        "expires_at": end.strftime("%Y-%m-%d"),
        "days_left": max(0, left),
        "active": left >= 0,
    }


# ---- 对外状态 ----


def current() -> dict:
    """当前生效的授权状态：有效的 License 优先，其次试用期，最后社区版。"""
    doc = load_license()
    if doc:
        ok, reason = verify_document(doc)
        if ok:
            payload = doc["payload"]
            edition = str(payload.get("edition") or "community")
            if edition not in EDITIONS:
                edition = "community"
            return {
                "licensed": True,
                "edition": edition,
                "edition_name": EDITIONS[edition]["name"],
                "licensee": payload.get("licensee") or "",
                "serial": payload.get("serial") or "",
                "seats": int(payload.get("seats") or 1),
                "issued_at": payload.get("issued_at") or "",
                "expires_at": payload.get("expires_at") or "",
                "trial": False,
                "days_left": None,
                "reason": "",
            }
        # 有文件但无效：退回试用/社区，并把原因带给界面
        invalid_reason = reason
    else:
        invalid_reason = ""

    trial = trial_info()
    if trial["active"]:
        return {
            "licensed": False,
            "edition": "enterprise",
            "edition_name": f"试用{'（企业版全功能）'}",
            "licensee": "",
            "serial": "",
            "seats": 0,
            "issued_at": "",
            "expires_at": trial["expires_at"],
            "trial": True,
            "days_left": trial["days_left"],
            "reason": invalid_reason,
        }

    return {
        "licensed": False,
        "edition": "community",
        "edition_name": EDITIONS["community"]["name"],
        "licensee": "",
        "serial": "",
        "seats": 0,
        "issued_at": "",
        "expires_at": "",
        "trial": False,
        "days_left": 0,
        "reason": invalid_reason or f"试用期已于 {trial['expires_at']} 结束",
    }


def feature_enabled(name: str) -> bool:
    cur = current()
    edition = cur.get("edition") or "community"
    return name in (EDITIONS.get(edition) or {}).get("features", [])


def features_map() -> dict:
    cur = current()
    edition = cur.get("edition") or "community"
    enabled = set((EDITIONS.get(edition) or {}).get("features", []))
    return {
        key: {
            "name": meta[0],
            "edition": meta[1],
            "enabled": key in enabled,
        }
        for key, meta in FEATURES.items()
    }


def status() -> dict:
    cur = current()
    return {
        **cur,
        "crypto_available": _HAS_CRYPTO,
        "features": features_map(),
        "editions": {k: v["name"] for k, v in EDITIONS.items()},
        "trial_days": TRIAL_DAYS,
    }
