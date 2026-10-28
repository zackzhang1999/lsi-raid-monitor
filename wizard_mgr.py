"""换卡恢复向导。

换阵列卡是存储运维里最需要"按顺序做"的一件事，顺序错了会丢数据。
最典型的一条：**有外来配置时先建阵列**——新建阵列会用掉那些盘，把原阵列
的元数据直接覆盖，数据再也找不回来。这个顺序错误在紧张的时候特别容易犯，
因为界面上"创建阵列"就在手边。

所以这里做的是：先诊断现在处于哪种情况，再给出一条有顺序、有理由、
每步可执行的处置路径。诊断只读，不改动任何东西。

四种典型场景：

  normal                 一切正常，与上次快照一致
  card_replaced_ok       换了卡但阵列完好（Safe ID 变了，VD/盘对得上）
  card_replaced_missing  换了卡且阵列缺失（盘在但未配置，或出现外来配置）
  degraded               阵列降级或缺盘，尚未恢复

判定全部基于已有数据：配置快照 + 当前状态 + 外来配置探测，不做额外假设。
"""

from __future__ import annotations

import config_mgr


def _to_int(v, default=0):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _vd_key(v: dict) -> str:
    return f"C{v.get('ctrl')}:{v.get('dg_vd')}"


def _control_state(ctrl: dict) -> str:
    """把控制器的健康与固件情况归一成一句人话。"""
    h = str(ctrl.get("health") or "").strip()
    if h in ("Optimal", "Opt"):
        return "正常"
    return h or "未知"


def diagnose(ctrl_id: int, status: dict, foreign: dict | None = None,
             prev_hash: str = "", curr: dict | None = None,
             replacement: dict | None = None) -> dict:
    """对一个控制器做恢复诊断。纯只读，不改变任何状态。"""
    status = status or {}
    controllers = status.get("controllers") or []
    ctrl = next((c for c in controllers if _to_int(c.get("ctrl")) == ctrl_id), None)
    if ctrl is None:
        return {
            "ctrl": ctrl_id,
            "scenario": "absent",
            "severity": "warn",
            "title": f"控制器 C{ctrl_id} 当前不可见",
            "summary": "该控制器不在系统中。若刚拔卡或换卡，这是预期状态。",
            "evidence": [],
            "steps": [],
        }

    disks = [d for d in (status.get("physical_disks") or []) if _to_int(d.get("ctrl")) == ctrl_id]
    vds = [v for v in (status.get("virtual_disks") or []) if _to_int(v.get("ctrl")) == ctrl_id]
    foreign = foreign or {}
    foreign_present = bool(foreign.get("present"))

    snap = config_mgr.latest_snapshot(ctrl_id)
    snap_data = (snap or {}).get("data") or {}
    snap_vds = snap_data.get("vds") or []
    snap_pds = snap_data.get("pds") or []
    snap_safe_id = str(snap_data.get("safe_id") or "")
    snap_model = str(snap_data.get("model") or "")

    cur_safe_id = str((curr or {}).get("safe_id") or "")
    cur_model = str((curr or {}).get("model") or ctrl.get("model") or "")

    evidence: list[dict] = []
    if snap_safe_id and cur_safe_id and snap_safe_id != cur_safe_id:
        evidence.append({
            "level": "high",
            "text": f"控制器唯一标识已变化（{snap_safe_id[:12]}… → {cur_safe_id[:12]}…），说明换过卡或被重置",
        })
    if snap_model and cur_model and snap_model != cur_model:
        evidence.append({
            "level": "high",
            "text": f"控制器型号变化：{snap_model} → {cur_model}",
        })
    if foreign_present:
        evidence.append({
            "level": "high",
            "text": f"检测到外来阵列配置：{foreign.get('description') or foreign.get('count')}",
        })
    if snap_vds:
        now_ids = {str(v.get("dg_vd")) for v in vds}
        was_ids = {str(v.get("id")) for v in snap_vds}
        missing = sorted(was_ids - now_ids)
        if missing:
            evidence.append({
                "level": "high",
                "text": f"上次快照中的虚拟盘当前不存在：{', '.join(missing)}",
            })
    if snap_pds:
        now_slots = {str(d.get("eid")) + ":" + str(d.get("slot")) for d in disks}
        was_slots = {str(p.get("slot")) for p in snap_pds}
        missing_pd = sorted(was_slots - now_slots)
        if missing_pd:
            evidence.append({
                "level": "high",
                "text": f"上次快照中的磁盘当前不存在：{', '.join(missing_pd)}（可能是拔了盘或没插到位）",
            })
    degraded = [v for v in vds if str(v.get("state") or "").lower() not in ("optl", "optimal")]
    if degraded:
        evidence.append({
            "level": "medium",
            "text": "虚拟盘非最优：" + "、".join(
                f"{v.get('dg_vd')}={v.get('state')}" for v in degraded
            ),
        })
    unconfigured = [
        d for d in disks
        if str(d.get("state") or "") in ("UGood", "UBad", "JBOD")
    ]
    if unconfigured:
        evidence.append({
            "level": "medium" if not foreign_present else "high",
            "text": "未配置磁盘：" + "、".join(
                str(d.get("full_label") or d.get("label")) for d in unconfigured
            ),
        })

    # ---- 场景判定 ----
    # 换卡判定优先用快照历史里的漂移记录：漂移一旦被记录，最新快照就已经
    # 是新卡的配置，再拿最新快照与当前值比对永远看不出换过卡。
    if replacement is None:
        try:
            replacement = config_mgr.card_replacement(ctrl_id)
        except Exception:
            replacement = None
    card_changed = bool(replacement) or bool(
        snap_safe_id and cur_safe_id and snap_safe_id != cur_safe_id
    )
    if replacement:
        evidence.insert(0, {
            "level": "high",
            "text": (
                f"快照历史中记录了换卡：{str(replacement.get('from_model') or '')[:28]} → "
                f"{str(replacement.get('to_model') or '')[:28]}"
                f"（{replacement.get('detected_at')}）"
            ),
        })
    vds_missing = bool(snap_vds) and not vds

    if foreign_present:
        scenario = "card_replaced_missing"
        severity = "crit"
        title = f"C{ctrl_id} 存在外来阵列配置，需要导入"
        summary = (
            "这些磁盘上带着原阵列的元数据。**必须先导入配置，不要先建阵列**——"
            "新建阵列会占用这些盘并覆盖原有元数据，数据将无法恢复。"
        )
    elif card_changed and vds_missing:
        scenario = "card_replaced_missing"
        severity = "crit"
        title = f"C{ctrl_id} 换了卡，但阵列尚未恢复"
        summary = (
            "控制器已确认更换，但上次快照中的虚拟盘当前不存在。"
            "盘上通常仍带着原阵列元数据，请按步骤尝试恢复。"
        )
    elif card_changed:
        scenario = "card_replaced_ok"
        severity = "warn"
        title = f"C{ctrl_id} 控制器已更换，阵列完好"
        summary = "唯一标识变化说明换了卡，但虚拟盘与磁盘布局与上次记录一致，数据应完好。"
    elif degraded:
        scenario = "degraded"
        severity = "crit"
        title = f"C{ctrl_id} 阵列处于降级状态"
        summary = "阵列未达到最优。优先确认缺盘原因，再考虑重建或换盘。"
    elif not snap:
        scenario = "no_snapshot"
        severity = "warn"
        title = f"C{ctrl_id} 尚无配置基线"
        summary = "系统还没记录过这台控制器的配置。先记录一份基线，之后才有比对依据。"
    else:
        scenario = "normal"
        severity = "ok"
        title = f"C{ctrl_id} 配置与上次快照一致"
        summary = "未发现换卡或配置异常。"

    steps = _steps_for(scenario, ctrl_id, {
        "foreign": foreign,
        "vds": vds,
        "snap_vds": snap_vds,
        "unconfigured": unconfigured,
        "degraded": degraded,
        "missing_pd": [e for e in evidence if "磁盘当前不存在" in e.get("text", "")],
        "has_snapshot": bool(snap),
    })

    return {
        "ctrl": ctrl_id,
        "scenario": scenario,
        "severity": severity,
        "title": title,
        "summary": summary,
        "evidence": evidence,
        "steps": steps,
        "controller": {
            "model": ctrl.get("model"),
            "fw": ctrl.get("fw"),
            "health": _control_state(ctrl),
            "num_disks": ctrl.get("num_disks"),
            "num_vds": ctrl.get("num_vds"),
        },
        "snapshot": {"id": snap["id"], "ts": snap["ts"], "hash": snap["hash"]} if snap else None,
        "prev_hash": prev_hash,
    }


def _steps_for(scenario: str, ctrl_id: int, ctx: dict) -> list[dict]:
    """按场景给出有序步骤。

    每一步都写明"为什么"，因为紧张时刻人会跟着做，但事后必须能解释
    自己做过什么。危险步骤单独标出来并注明后果。
    """
    fore = ctx.get("foreign") or {}

    if scenario == "card_replaced_missing":
        steps = [
            {
                "n": 1,
                "title": "先不要创建任何新阵列",
                "detail": "盘上通常还带着原阵列的元数据。一旦用这些盘建了新阵列，"
                          "元数据会被覆盖，原数据将无法恢复。这是整个流程里最容易犯、"
                          "也最不可逆的错误。",
                "danger": True,
            },
            {
                "n": 2,
                "title": "确认所有成员盘都已插到位",
                "detail": "缺盘的阵列即使导入也会是降级状态。先对照上次快照的盘位列表"
                          "确认每一块盘都在，再继续。",
            },
        ]
        if ctx.get("missing_pd"):
            steps.append({
                "n": 3,
                "title": "先解决缺盘",
                "detail": "仍缺少快照中记录的磁盘。插入全部成员盘之前，不要执行导入。",
                "blocker": True,
            })
            offset = 4
        else:
            offset = 3
        steps.extend([
            {
                "n": offset,
                "title": "记录当前状态作为证据",
                "detail": "在动手之前先打一份配置快照。它既是出问题时回退的依据，"
                          "也是事后说明「当时是什么状态」的凭据。",
                "action": "snapshot",
            },
            {
                "n": offset + 1,
                "title": "导入外来配置",
                "detail": "把磁盘上的原阵列元数据导回控制器，恢复原有的阵列结构。"
                          "导入本身不写数据，只是让控制器重新认识这些盘。",
                "action": "foreign_import",
                "danger": True,
                "confirm": "确认已核对所有成员盘到位，且没有用这些盘创建过新阵列。",
            },
            {
                "n": offset + 2,
                "title": "与快照比对确认恢复结果",
                "detail": "导入后系统会自动记录一份新快照。把它与换卡前的快照比对，"
                          "确认虚拟盘编号、容量、成员盘归属都与原来一致。",
                "action": "diff",
            },
            {
                "n": offset + 3,
                "title": "确认数据可访问后再投入生产",
                "detail": "从操作系统层面挂载并读取文件系统，确认业务数据可访问。"
                          "在此之前不要认为恢复已经完成。",
            },
        ])
        return steps

    if scenario == "card_replaced_ok":
        return [
            {
                "n": 1,
                "title": "阵列完好，无需恢复操作",
                "detail": "磁盘布局与原记录一致，说明阵列元数据完整。不要执行导入或重建。",
            },
            {
                "n": 2,
                "title": "记录本次变更",
                "detail": "换卡是重要的硬件变更，记录一份新快照并附上说明，"
                          "让后续排查知道这张卡什么时候换过。",
                "action": "snapshot",
            },
            {
                "n": 3,
                "title": "核对固件版本",
                "detail": "新卡固件与整机其它控制器不一致时，建议统一版本——"
                          "版本混杂会显著增加后续故障排查的难度。",
            },
        ]

    if scenario == "degraded":
        steps = [
            {
                "n": 1,
                "title": "确认缺盘原因",
                "detail": "先判断是盘故障、接触不良，还是被误拔。原因不同，处理方式完全不同。",
            },
            {
                "n": 2,
                "title": "记录当前配置",
                "detail": "在处理之前留一份配置快照，便于回退与对照。",
                "action": "snapshot",
            },
            {
                "n": 3,
                "title": "按需更换或回插磁盘",
                "detail": "换盘后阵列会自动重建。重建期间不要断电或拔其它盘。",
            },
            {
                "n": 4,
                "title": "重建完成后确认阵列回到最优",
                "detail": "重建期间性能会下降，属正常现象；重建结束应恢复为 Optimal。",
            },
        ]
        return steps

    if scenario == "no_snapshot":
        return [
            {
                "n": 1,
                "title": "记录配置基线",
                "detail": "有了基线，之后任何配置变化都能被自动发现。",
                "action": "snapshot",
            }
        ]

    return [
        {
            "n": 1,
            "title": "无需操作",
            "detail": "配置与上次快照一致。系统会持续比对，发生变化时会自动告警。",
        }
    ]
