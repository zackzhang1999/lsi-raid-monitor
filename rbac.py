"""角色权限模型。

四个角色按**能造成多大影响**划分，而不是按菜单或页面划分：

- viewer   只读。看得到一切，改不了任何东西。
- operator 日常运维。告警确认、维护窗口、定位灯、巡读与一致性检查。
- admin    全部权限，含建阵列、删虚拟盘、擦除、扩容、用户与授权、多机管理。
- auditor  只读 + 审计导出。合规审查要能查"谁在什么时候做了什么"，
           但不能改任何状态——包括告警确认，因为确认本身会写入审计记录。

一条关键取舍：**危险操作一律归 admin，不下放给 operator**。
建阵列、擦除、扩容这类操作做错一次就是数据丢失。把它们放进"日常运维"
角色，等于让一次误点就能造成不可逆后果。operator 的价值是让值班人员
能处理告警、跑巡检，而不是绕开变更审批。
"""

from __future__ import annotations

ROLES = {
    "viewer": {
        "name": "只读",
        "desc": "可以查看全部监控数据，不能做任何修改",
    },
    "operator": {
        "name": "运维",
        "desc": "只读权限外加告警确认、维护窗口、定位灯、巡读与一致性检查",
    },
    "admin": {
        "name": "管理员",
        "desc": "全部权限，含建阵列、删除虚拟盘、擦除、扩容、用户与授权",
    },
    "auditor": {
        "name": "审计",
        "desc": "只读权限外加操作记录与日志导出，不能修改任何状态",
    },
}

# 权限 -> 拥有该权限的角色
PERMS = {
    "view": ["viewer", "operator", "admin", "auditor"],
    "operate": ["operator", "admin"],
    "configure": ["admin"],
    "audit": ["admin", "auditor"],
}

PERM_LABELS = {
    "view": "查看",
    "operate": "日常运维",
    "configure": "变更配置",
    "audit": "审计导出",
}


def normalize(role: str) -> str:
    r = str(role or "").strip().lower()
    return r if r in ROLES else "viewer"


def has_perm(role: str, perm: str) -> bool:
    return normalize(role) in PERMS.get(perm, [])


def perms_for(role: str) -> list[str]:
    r = normalize(role)
    return [p for p, roles in PERMS.items() if r in roles]


def role_label(role: str) -> str:
    return ROLES.get(normalize(role), {}).get("name", role)


def describe() -> dict:
    """供界面渲染角色选择器与权限矩阵。"""
    return {
        "roles": {k: {**v, "perms": perms_for(k)} for k, v in ROLES.items()},
        "perms": {
            k: {"label": PERM_LABELS.get(k, k), "roles": v} for k, v in PERMS.items()
        },
    }
