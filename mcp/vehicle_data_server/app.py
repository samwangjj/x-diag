#!/usr/bin/env python3
"""vehicle-data 工具服务（T1.4）

为 AgentTeams Worker 提供 HTTP 工具网关，统一协议：
    POST /tools/{scenario_id}/{tool_name}   Content-Type: application/json

工具清单（9 个）：
    vehicle_data.query_signals      查询信号时序（CSV 数据源）
    vehicle_data.query_dtc          查询 DTC 事件（清码后状态会变化）
    vehicle_data.get_freeze_frame   获取 DTC 冻结帧
    vehicle_data.get_vehicle_profile 获取车辆档案
    knowledge.search_cases          检索相似案例
    knowledge.upsert_case           写入案例（W4 白名单）
    archive.write_report            写入诊断报告（W4 白名单）
    vehicle_action.execute          执行车辆动作（仅 L0/L1 放行，L2/L3 拒绝）
    vehicle_action.create_approval  创建审批单
"""
import csv, json, random, time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

BASE = Path(__file__).resolve().parents[2]           # x-diag/
DATA = BASE / "data"
CASES = BASE / "knowledge" / "cases"
REPORTS = BASE / "knowledge" / "reports"
APPROVALS = BASE / "knowledge" / "approvals"
RUNTIME = Path(__file__).resolve().parent / "runtime"
for d in (CASES, REPORTS, APPROVALS, RUNTIME):
    d.mkdir(parents=True, exist_ok=True)

SCENARIOS = {
    "scenario_a_obc_overvoltage": DATA / "scenario_a_obc_overvoltage",
    "scenario_b_thermal_runaway": DATA / "scenario_b_thermal_runaway",
}
ACTION_LEVELS = {                                # 动作安全分级（服务端权威映射，硬阻断依据）
    "clear_dtc": "L1",
    "remote_charge_inhibit": "L2",
    "dispatch_tow": "L3",
}
PORT = 18091
app = FastAPI(title="x-diag vehicle-data tool server")


# ---------- 数据加载 ----------
def scenario_dir(sid: str) -> Path:
    if sid not in SCENARIOS:
        raise KeyError(f"未知 scenario_id：{sid}，可选：{list(SCENARIOS)}")
    return SCENARIOS[sid]

def load_rows(sid: str):
    d = scenario_dir(sid)
    rows = []
    for f in [d / "signals_timeseries.csv", RUNTIME / f"{sid}_post_clear.csv"]:
        if f.exists():
            with open(f, encoding="utf-8-sig") as fp:
                rows.extend(list(csv.DictReader(fp)))
    return rows

def state_file(sid: str) -> Path:
    return RUNTIME / f"{sid}_state.json"

def load_state(sid: str) -> dict:
    f = state_file(sid)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

def save_state(sid: str, st: dict):
    state_file(sid).write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 工具实现 ----------
def t_query_signals(sid, body):
    rows = load_rows(sid)
    if not rows:
        return {"error": "无信号数据"}
    start, end = body.get("start") or "", body.get("end") or "9999"
    wanted = [s for s in (body.get("signals") or []) if s]
    header = list(rows[0].keys())
    if wanted:
        unknown = [s for s in wanted if s not in header]
        if unknown:
            return {"error": f"未知信号 {unknown}，可用信号：{header}"}
    cols = ["timestamp"] + (wanted or [c for c in header if c != "timestamp"])
    out = [{c: r.get(c, "") for c in cols} for r in rows if start <= r["timestamp"] <= end]
    n = len(out)
    if n > 600:                                   # 降采样，防止上下文爆炸
        step = (n + 599) // 600
        out = out[::step]
    return {"vin": body.get("vin", ""), "count": len(out), "sampled": n > 600, "rows": out}

def t_query_dtc(sid, body):
    d = scenario_dir(sid)
    events = json.loads((d / "dtc_events.json").read_text(encoding="utf-8"))
    st = load_state(sid)
    active = [e["dtc"] for e in events["events"] if e["status"] == "confirmed"]
    if st.get("dtc_cleared"):
        for e in events["events"]:
            e["status"], e["clear_time"] = "cleared", st["cleared_at"]
        active = []
    return {"vin": body.get("vin", ""), "dtc_active": active, "events": events["events"]}

def t_get_freeze_frame(sid, body):
    d = scenario_dir(sid)
    ff = json.loads((d / "freeze_frame.json").read_text(encoding="utf-8"))
    if body.get("dtc") and ff["trigger"]["dtc"] != body["dtc"]:
        return {"error": f"无 {body['dtc']} 的冻结帧，现有：{ff['trigger']['dtc']}"}
    return ff

def t_get_vehicle_profile(sid, body):
    profs = json.loads((DATA / "vehicle_profiles.json").read_text(encoding="utf-8"))
    vin = body.get("vin", "")
    return profs.get(vin) or {"error": f"未知 VIN：{vin}，可用：{list(profs)}"}

def t_search_cases(sid, body):
    q = (body.get("query") or "").lower()
    k = int(body.get("top_k") or 3)
    hits = []
    for f in sorted(CASES.glob("CASE-*.json")):
        c = json.loads(f.read_text(encoding="utf-8"))
        text = json.dumps(c, ensure_ascii=False).lower()
        tokens = set(q.replace("，", " ").replace(",", " ").split())
        score = sum(text.count(t) for t in tokens if len(t) >= 2)
        if score:
            hits.append({"score": score, "case": c})
    hits.sort(key=lambda h: -h["score"])
    return {"query": q, "count": min(k, len(hits)),
            "cases": [h["case"] | {"match_score": h["score"]} for h in hits[:k]]}

def t_upsert_case(sid, body):
    case = body.get("case") or {}
    if not case:
        return {"error": "body.case 不能为空"}
    existing = {int(f.stem.split("-")[1]) for f in CASES.glob("CASE-*.json")}
    n = 1
    while n in existing:                           # 取最小空闲编号，避免覆盖预置案例（如 CASE-0005）
        n += 1
    cid = f"CASE-{n:04d}"
    case.update(case_id=cid, scenario=sid, stored_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    (CASES / f"{cid}.json").write_text(json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"case_id": cid, "stored": True, "path": f"knowledge/cases/{cid}.json"}

def t_write_report(sid, body):
    report = body.get("report") or {}
    if not report:
        return {"error": "body.report 不能为空"}
    rid = time.strftime("RPT-%Y%m%d-%H%M%S")
    (REPORTS / f"{rid}.json").write_text(
        json.dumps({"report_id": rid, "scenario": sid, **report}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return {"report_id": rid, "stored": True, "report_ref": f"knowledge/reports/{rid}.json"}

def t_execute(sid, body):
    action = body.get("action", "")
    level = body.get("risk_level", "") or ACTION_LEVELS.get(action, "")
    if level in ("L2", "L3"):                      # 网关硬拦截：双保险（body 声明或 ACTION_LEVELS 映射）
        aid = body.get("approval_id", "")
        if level == "L3":                          # L3 红线：任何情况下都禁止 agent 远程执行
            return {"executed": False, "action": action, "risk_level": level,
                    "error": f"L3 动作 {action} 为安全红线，严禁 agent 远程执行；请留痕并交由人工/维修站线下处置"}
        if not aid:                                # L2 无审批单：指导先建单
            return {"executed": False, "action": action, "risk_level": level,
                    "error": f"L2 动作 {action} 需人工审批：请先 vehicle_action.create_approval 建单，待人工批准（房间回复『同意』）后，携带 approval_id 重新调用本工具执行"}
        f = APPROVALS / f"{aid}.json"
        if not f.exists():
            return {"executed": False, "action": action, "risk_level": level, "approval_id": aid,
                    "error": f"审批单 {aid} 不存在，请核对单号或重新 create_approval"}
        rec = json.loads(f.read_text(encoding="utf-8"))
        if rec.get("status") != "approved":        # L2 有单但未批准：继续等待
            return {"executed": False, "action": action, "risk_level": level, "approval_id": aid,
                    "approval_status": rec.get("status"),
                    "error": f"审批单 {aid} 当前状态 {rec.get('status')}（未批准），请等待人工在房间回复『同意』后重试"}
        st = load_state(sid)                       # L2 已批准：放行执行
        st[f"executed_{action}"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "approval_id": aid,
                                    "by": "vehicle_action.execute"}
        save_state(sid, st)
        return {"executed": True, "action": action, "risk_level": level, "approval_id": aid,
                "result": f"{action} 已按审批单 {aid} 执行（人工已批准），请在房间留痕并安排复检"}
    if action != "clear_dtc":
        return {"executed": False, "error": f"不支持的动作：{action}，当前支持：clear_dtc（L1）"}
    st = load_state(sid)
    if st.get("dtc_cleared"):
        return {"executed": True, "action": action, "result": "DTC 已处于清除状态，无需重复执行"}
    rows = load_rows(sid)
    last = rows[-1]
    cleared_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    gen_post_clear_rows(sid, last)                 # 生成"维修后恢复"信号段供复检
    save_state(sid, {"dtc_cleared": True, "cleared_at": cleared_at,
                     "cleared_by": "vehicle_action.execute", "risk_level": level})
    return {"executed": True, "action": action, "risk_level": level,
            "result": "P3509 已清除（模拟进站检修 OBC 输出电压传感器后清码），信号已恢复 398V 正常区间，可复检",
            "cleared_at": cleared_at}

def gen_post_clear_rows(sid, last):
    f = RUNTIME / f"{sid}_post_clear.csv"
    if f.exists():
        return
    import datetime as dt
    t0 = dt.datetime.fromisoformat(last["timestamp"])
    header = list(last.keys())
    rnd = random.Random(7)
    with open(f, "w", newline="", encoding="utf-8") as fp:
        w = csv.DictWriter(fp, fieldnames=header)
        w.writeheader()
        soc, temp = float(last["soc"]), float(last["obc_temperature"])
        for i in range(1, 601):                    # 清码后 10 分钟：停机未充电，电压恢复正常
            w.writerow({
                "timestamp": (t0 + dt.timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S"),
                "vin": last["vin"], "charge_status": 3,
                "ac_input_voltage": 0, "ac_input_current": 0, "obc_charge_power_kw": 0,
                "obc_output_voltage": round(398 + rnd.uniform(-0.4, 0.4), 2),
                "obc_output_current": 0,
                "obc_temperature": round(max(33, temp - i * 0.02), 1),
                "pack_voltage": round(398 + rnd.uniform(-0.3, 0.3), 2),
                "pack_current": 0, "soc": soc,
                "cell_temp_max": round(35 + rnd.uniform(-0.2, 0.2), 1),
                "insulation_resistance": round(2500 + rnd.uniform(-15, 15)),
                "alarm_level": 0, "dtc_active": ""})

def t_create_approval(sid, body):
    aid = time.strftime("APR-%Y%m%d-%H%M%S")
    rec = {"approval_id": aid, "scenario": sid, "title": body.get("title", ""),
           "details": body.get("details", {}), "status": "pending",
           "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "note": "等待人工在 Team 房间回复『同意』后，由 TeamLeader 执行"}
    (APPROVALS / f"{aid}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"approval_id": aid, "status": "pending", "created": True}


TOOLS = {
    "vehicle_data.query_signals": t_query_signals,
    "vehicle_data.query_dtc": t_query_dtc,
    "vehicle_data.get_freeze_frame": t_get_freeze_frame,
    "vehicle_data.get_vehicle_profile": t_get_vehicle_profile,
    "knowledge.search_cases": t_search_cases,
    "knowledge.upsert_case": t_upsert_case,
    "archive.write_report": t_write_report,
    "vehicle_action.execute": t_execute,
    "vehicle_action.create_approval": t_create_approval,
}


@app.get("/")
def index():
    return {"service": "x-diag vehicle-data tool server", "scenarios": list(SCENARIOS),
            "tools": list(TOOLS), "protocol": "POST /tools/{scenario_id}/{tool_name}"}

@app.post("/approvals/{aid}/decision")
def approval_decision(aid: str, body: dict = {}):
    """人工审批回写（由 webbridge 在收到演示台审批时调用）：将审批单标记为 approved/rejected。"""
    f = APPROVALS / f"{aid}.json"
    if not f.exists():
        return JSONResponse({"error": f"审批单不存在：{aid}"}, 404)
    decision = (body or {}).get("decision", "")
    if decision not in ("approve", "reject"):
        return JSONResponse({"error": 'decision 只能是 "approve" 或 "reject"'}, 400)
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["status"] = "approved" if decision == "approve" else "rejected"
    rec["decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    rec["decided_by"] = "售后站长（人工，经由 webbridge 回写）"
    f.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"approval_id": aid, "status": rec["status"]}


@app.post("/tools/{scenario_id}/{tool_name}")
def call_tool(scenario_id: str, tool_name: str, body: dict = {}):
    if tool_name not in TOOLS:
        return JSONResponse({"error": f"未知工具：{tool_name}，可用：{list(TOOLS)}"}, 404)
    try:
        return TOOLS[tool_name](scenario_id, body or {})
    except KeyError as e:
        return JSONResponse({"error": str(e)}, 404)
    except FileNotFoundError as e:
        return JSONResponse({"error": f"数据文件缺失：{e.filename}（该场景数据可能尚未生成）"}, 500)
    except Exception as e:                          # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 500)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
