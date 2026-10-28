"""BBU / 缓存单元与写缓存策略评估。

存储里最典型的静默风险是**写缓存开着而缓存保护失效**：控制器把写缓存当
普通内存用，数据还没落盘就返回"写成功"，此时掉电——这些数据就没了。
有电池（BBU/CacheVault）时这不是问题，没有或失效时它就是数据丢失。

另一个方向的坑是"看起来不对其实是对的"：卡上没有电池时，固件会把写缓存
**临时禁用**（cache code RWTD），牺牲性能换安全。这会让人以为"缓存坏了"、
去"修"它，反而把保护打开。

所以这里做的是把三种状态分清楚，并说明各自的含义与应对：

  WriteBack 生效 + 保护正常   → 正常
  WriteBack 生效 + 保护失效   → 掉电丢数据，必须立刻处理
  配置 WriteBack 但被临时禁用 → 固件在保护你，代价是性能，别去"修"它

缓存码来自 storcli：R=Read cache，W=Write cache，A=Always，
WB=Write Back，WT=Write Through，TD=Temporarily Disabled。
"""

from __future__ import annotations


def _to_int(v, default=0):
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def decode_cache(code: str) -> dict:
    """把 storcli 的 Cache 码拆成可读的读/写缓存状态。

    顺序很关键：必须先判 TD，再判 WB。RWTD 里同时含有 W、T、D，
    用简单的子串判断会把它归成 Write Through——那会把"因缺电池被临时
    禁用"说成"配置成了直写"，两种情况的处置方式完全不同。
    """
    c = str(code or "").upper()
    if not c:
        return {"read": "未知", "write": "未知", "raw": "", "temporarily_disabled": False}

    if "TD" in c:
        write = "临时禁用"
        temporarily_disabled = True
    elif "AWB" in c:
        write = "始终写回"
        temporarily_disabled = False
    elif "WB" in c:
        write = "写回"
        temporarily_disabled = False
    elif "WT" in c:
        write = "直写"
        temporarily_disabled = False
    else:
        write = "未知"
        temporarily_disabled = False

    if "NORA" in c:
        read = "不预读"
    elif "RA" in c or c.startswith("R"):
        read = "预读"
    else:
        read = "未知"
    return {
        "read": read,
        "write": write,
        "raw": c,
        "temporarily_disabled": temporarily_disabled,
    }


def evaluate(ctrl: dict, vds: list[dict]) -> dict:
    """评估一台控制器的缓存保护状况。

    ctrl 需要 bbu_state / bbu_model / bbu_present 等字段，
    vds 需要 write_cache_raw 与 write_cache_initial。
    """
    ctrl = ctrl or {}
    vds = vds or []

    present = str(ctrl.get("bbu_present") or "").strip()
    state = str(ctrl.get("bbu_state") or "").strip()
    model = str(ctrl.get("bbu_model") or "").strip()
    temp = _to_int(ctrl.get("bbu_temperature"), None)

    # HwCfg 的 BBU 字段有 Present / Absent / N/A 三种取值，
    # 只有明确 Present 才算"有保护"。
    has_unit = present.lower().startswith("present")
    state_ok = state.lower() in ("optimal", "opt", "ok") if state else False
    protection_ok = bool(has_unit and state_ok)

    ports = []
    for v in vds:
        dec = decode_cache(v.get("write_cache_raw"))
        ports.append(
            {
                "vd": v.get("dg_vd") or f"VD{v.get('vd')}",
                "ctrl": v.get("ctrl"),
                "state": v.get("state"),
                "cache_code": dec["raw"],
                "read": dec["read"],
                "write": dec["write"],
                "temporarily_disabled": dec["temporarily_disabled"],
                "configured": str(v.get("write_cache_initial") or "").strip(),
            }
        )

    findings: list[dict] = []
    any_writeback = any(p["write"] in ("写回", "始终写回") for p in ports)
    any_td = any(p["temporarily_disabled"] for p in ports)

    # 最高优先级：写回生效但保护失效 —— 这是会丢数据的状态
    if any_writeback and not protection_ok:
        reason = "未安装缓存单元" if not has_unit else f"缓存单元状态异常（{state or '未知'}）"
        findings.append(
            {
                "key": "writeback_unprotected",
                "level": "crit",
                "title": "写缓存已启用但掉电保护失效",
                "detail": f"{reason}，而虚拟盘正在使用写回模式。"
                "此时控制器会把尚未落盘的数据当作已写入返回，突然断电将丢失这部分数据。",
                "advice": "立即把虚拟盘切换为直写（Write Through），或补装/更换缓存单元。"
                "在恢复保护之前不要认为这些数据是安全的。",
            }
        )

    # 配置了写回但固件临时禁用：这是固件在保护数据，不要"修"它
    if any_td and not protection_ok:
        findings.append(
            {
                "key": "writeback_temp_disabled",
                # 分级取决于"本来该不该有缓存单元"：
                #   卡上根本没装缓存单元 → 固件禁用写缓存是**预期行为**，
                #     它永远不会自己消失，放进未处理列表就是一条永久噪音，
                #     只会让人对告警脱敏。降为 info，只在页面上说明。
                #   装了缓存单元却仍被临时禁用 → 说明单元失效或正在学习周期，
                #     这是真的需要人看的异常，保持 warn。
                "level": "warn" if has_unit else "info",
                "title": (
                    "写缓存被临时禁用（缓存单元未就绪）"
                    if has_unit
                    else "写缓存已由固件禁用（本机未安装缓存单元）"
                ),
                "detail": (
                    "阵列配置为写回模式，但控制器检测到没有可用的缓存单元，"
                    "自动把写缓存临时关闭以避免掉电丢数据。这是固件的保护行为，"
                    "不是故障，也不需要处理——数据是安全的。"
                ),
                "advice": (
                    "代价是写入性能下降。若业务对写入性能有要求，补装缓存单元即可自动恢复；"
                    "不要强制打开写缓存——那会把数据置于掉电丢失的风险中。"
                ),
            }
        )

    if has_unit and not state_ok:
        findings.append(
            {
                "key": "bbu_degraded",
                "level": "crit" if any_writeback else "warn",
                "title": f"缓存单元状态异常：{state or '未知'}",
                "detail": f"型号 {model or '未知'}，当前状态 {state or '未知'}。",
                "advice": "缓存单元异常期间写缓存随时可能被禁用；若正在使用写回模式，"
                "请尽快安排更换。",
            }
        )

    if has_unit and isinstance(temp, int) and temp >= 55:
        findings.append(
            {
                "key": "bbu_temp_high",
                "level": "warn",
                "title": f"缓存单元温度偏高：{temp}°C",
                "detail": "缓存单元长期高温会加速老化，缩短可用寿命。",
                "advice": "检查机箱风道与风扇转速。",
            }
        )

    if not has_unit:
        findings.append(
            {
                "key": "bbu_absent",
                "level": "info",
                "title": "未安装缓存单元",
                "detail": "该控制器没有 BBU / CacheVault。这不一定是问题——"
                "没有保护时固件会禁用写缓存，牺牲性能换数据安全。",
                "advice": "对写入性能有要求的场景建议补装缓存单元。",
            }
        )

    if not findings:
        findings.append(
            {
                "key": "ok",
                "level": "ok",
                "title": "缓存保护正常",
                "detail": f"缓存单元状态 {state or '正常'}，写缓存策略与保护能力匹配。",
                "advice": "",
            }
        )

    worst = "ok"
    for f in findings:
        if f["level"] == "crit":
            worst = "crit"
            break
        if f["level"] == "warn" and worst != "crit":
            worst = "warn"

    return {
        "ctrl": ctrl.get("ctrl"),
        "severity": worst,
        "unit": {
            "present": has_unit,
            "raw_presence": present or "未知",
            "model": model,
            "state": state,
            "temperature": temp,
            "protection_ok": protection_ok,
        },
        "ports": ports,
        "findings": findings,
    }


def check_alerts(assessments: list[dict]) -> None:
    """把评估结果送进告警闭环。延迟导入 lsi_alert 以避免循环依赖。"""
    import lsi_alert

    for a in assessments or []:
        cid = a.get("ctrl")
        crit = [f for f in a.get("findings") or [] if f.get("level") == "crit"]
        warn = [f for f in a.get("findings") or [] if f.get("level") == "warn"]
        for key, items, level in (
            (f"controller:{cid}:cache_unprotected", crit, "error"),
            (f"controller:{cid}:cache_degraded", warn, "warning"),
        ):
            if items:
                f = items[0]
                lsi_alert._flag(
                    key, f["title"], f"{f['detail']} {f.get('advice', '')}".strip(), level
                )
            else:
                lsi_alert._clear(key)
