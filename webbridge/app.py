#!/usr/bin/env python3
"""webbridge — x-diag 演示台前端桥接服务

连接 React 演示台与既有系统：Matrix（Conduit）团队房间发任务/收消息/代发审批、工具网关
（POST /tools/{scenario_id}/{tool_name}，无认证）代理信号查询、共享目录文件浏览、WEB_DIST 静态托管。
BRIDGE_MOCK=1 时不连 Matrix，事件流回放 WEB_DIST/playback/{scenario}.json，信号返回合成数据。
"""
import asyncio, datetime as dt, html, json, logging, math, os, random, re, time, uuid
from pathlib import Path

import httpx, uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

BASE = Path(__file__).resolve().parent
RUNTIME, TASKS_DIR = BASE / "runtime", BASE / "tasks"
STATE_FILE = RUNTIME / "bridge_state.json"

# ---------- 环境配置 ----------
PORT = int(os.environ.get("WEBBRIDGE_PORT", "18092"))
MOCK = os.environ.get("BRIDGE_MOCK", "") == "1"
MATRIX_BASE = os.environ.get("MATRIX_BASE", "http://localhost:18080").rstrip("/")
MATRIX_USER = os.environ.get("MATRIX_USER", "@webbridge:matrix-local.agentteams.io:18080")
MATRIX_PASS = os.environ.get("MATRIX_PASS", "")
TEAM_ROOM_NAME = os.environ.get("TEAM_ROOM_NAME", "Team: x-diag")
_DOMAIN = MATRIX_USER.split(":", 1)[1] if ":" in MATRIX_USER else "matrix-local.agentteams.io:18080"
LEADER_USER = os.environ.get("LEADER_USER", f"@x-diag-leader:{_DOMAIN}")
TOOL_BASE = os.environ.get("TOOL_BASE", "http://localhost:18091").rstrip("/")
SHARED_TASKS_DIR = Path(os.environ.get("SHARED_TASKS_DIR", "~/agentteams/shared/tasks")).expanduser()
_dist = Path(os.environ.get("WEB_DIST", str(BASE.parent / "web" / "dist")))
if not _dist.is_dir() and (BASE / "web" / "dist").is_dir():  # 仓库未构建前端时退回内置演示页
    _dist = BASE / "web" / "dist"
WEB_DIST = _dist
TIMEOUT = 10.0  # 外部调用统一超时（/sync 长轮询除外）

AGENTS = [
    {"id": "x-diag-leader", "name": "x-diag-leader", "cn_name": "队长", "role": "TeamLeader：任务调度、分级处置决策、审批对接"},
    {"id": "w2-monitor", "name": "w2-monitor", "cn_name": "监控员", "role": "信号巡检、DTC 取证、复检确认"},
    {"id": "w3-diagnosis", "name": "w3-diagnosis", "cn_name": "诊断师", "role": "DTC 解码、交叉验证、根因排序"},
    {"id": "w4-knowledge", "name": "w4-knowledge", "cn_name": "知识员", "role": "报告输出、案例沉淀"},
]
SCENARIOS = [
    {"id": "scenario_a_obc_overvoltage", "title": "OBC 输出过压（交流慢充中断）", "vin": "LSVA0000DEMO00001",
     "severity": "中", "level_flow": "告警取证 → 诊断推理 → 分级处置（L1 清码自动执行）→ 复检 → 报告与知识沉淀"},
    {"id": "scenario_b_thermal_runaway", "title": "直流快充电池热失控风险", "vin": "LSVA0000DEMO00002",
     "severity": "高", "level_flow": "告警取证 → 诊断推理 → 分级处置（L2/L3 人工审批）→ 复检 → 报告与知识沉淀"},
]
SCENARIO_IDS = {s["id"] for s in SCENARIOS}
# 有序匹配：一条消息提及多个阶段时取「最靠后」的阶段（LLM 措辞多变，规则宁宽勿漏）
STAGE_MAP = [("诊断报告", 4), ("知识沉淀", 4), ("沉淀", 4), ("归档", 4), ("闭环", 4), ("RPT-", 4),
             ("复检", 3), ("复测", 3), ("持续监控", 3),
             ("诊断推理", 1), ("根因", 1),
             ("告警取证", 0), ("巡检", 0), ("取证", 0), ("告警", 0),
             ("分级处置", 2), ("处置", 2), ("审批", 2), ("clear_dtc", 2), ("治理", 2)]
APR_RE = re.compile(r"APR-\d{8}-\d{6}")
ALLOWED_EXT, MAX_FILE = {".json", ".md", ".csv"}, 2 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webbridge")
app = FastAPI(title="x-diag webbridge")

# ---------- Matrix Client（Client-Server API v3） ----------
class Matrix:
    def __init__(self):
        self.token, self.room_id, self.room_ts = None, None, 0.0
        self.http = httpx.AsyncClient(base_url=MATRIX_BASE, timeout=TIMEOUT)

    def _h(self):
        return {"Authorization": f"Bearer {self.token}"}

    async def login(self):
        user = MATRIX_USER[1:].split(":")[0] if MATRIX_USER.startswith("@") else MATRIX_USER
        r = await self.http.post("/_matrix/client/v3/login", json={
            "type": "m.login.password", "password": MATRIX_PASS,
            "identifier": {"type": "m.id.user", "user": user}})
        r.raise_for_status()
        self.token = r.json()["access_token"]
        log.info("matrix login ok: %s", MATRIX_USER)

    async def resolve_room(self, force=False):
        """/joined_rooms 后逐个读 m.room.name state 匹配 TEAM_ROOM_NAME，缓存 60s。"""
        if not force and self.room_id and time.time() - self.room_ts < 60:
            return self.room_id
        r = await self.http.get("/_matrix/client/v3/joined_rooms", headers=self._h())
        r.raise_for_status()
        for rid in r.json().get("joined_rooms", []):
            try:
                nr = await self.http.get(
                    f"/_matrix/client/v3/rooms/{rid}/state/m.room.name", headers=self._h())
                if nr.status_code == 200 and nr.json().get("name") == TEAM_ROOM_NAME:
                    self.room_id, self.room_ts = rid, time.time()
                    log.info("room resolved: %s -> %s", TEAM_ROOM_NAME, rid)
                    return rid
            except Exception:  # noqa: BLE001 单个房间读失败不阻塞
                continue
        raise RuntimeError(f"未加入名为「{TEAM_ROOM_NAME}」的房间（请先把 {MATRIX_USER} 邀请进房间）")

    async def send_text(self, body, html_body):
        rid = await self.resolve_room()
        url = f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{uuid.uuid4().hex}"
        r = await self.http.put(url, headers=self._h(),
                                json={"msgtype": "m.text", "body": body, "format": "org.matrix.custom.html",
                                      "formatted_body": html_body, "m.mentions": {"user_ids": [LEADER_USER]}})
        r.raise_for_status()
        return r.json().get("event_id", "")

    async def sync(self, since=None, timeout_ms=30000):
        params = {"timeout": timeout_ms, **({"since": since} if since else {})}
        r = await self.http.get("/_matrix/client/v3/sync", headers=self._h(),
                                params=params, timeout=timeout_ms / 1000 + TIMEOUT)
        r.raise_for_status()
        return r.json()

    async def recent_messages(self, limit=60):
        """拉取房间最近消息（dir=b 倒序），供 SSE 新客户端补历史重建界面状态。"""
        r = await self.http.get(f"/_matrix/client/v3/rooms/{self.room_id}/messages",
                                headers=self._h(), params={"dir": "b", "limit": limit},
                                timeout=TIMEOUT)
        r.raise_for_status()
        return [e for e in r.json().get("chunk", []) if e.get("type") == "m.room.message"]
matrix = Matrix()
def pill_html(text):
    """纯文本 → 带 leader pill（matrix.to 链接）的 formatted_body。"""
    handle = "@" + LEADER_USER[1:].split(":")[0]
    pill = f'<a href="https://matrix.to/#/{LEADER_USER}">{html.escape(handle)}</a>'
    esc = html.escape(text).replace("\n", "<br>")
    return esc.replace(html.escape(handle), pill, 1) if handle in text else pill + "<br>" + esc

# ---------- 消息归一化与审批单提取 ----------
def classify(text):
    t = text.replace(" ", "").replace('"', "")
    # 顺序敏感：「报告/闭环」必须先于「审批单」判定——闭环汇报里常引用 APR 单号，
    # 若审批规则在前，最终报告会被误标为 approval，前端永远等不到 report/done
    if ("完成闭环" in text or ("最终诊断报告" in text and len(text) > 200)
            or ("诊断报告" in text and "项目状态" in text)
            or text.lstrip().startswith("## ")):
        return "report"
    if "审批单" in text or APR_RE.search(text):
        return "approval"
    if "executed:false" in t or "硬阻断" in text:
        return "block"
    if ("派发子任务" in text or "验收通过" in text or "委派" in text or "派遣" in text
            or "新任务" in text or "已创建" in t or "已就绪" in t):
        return "stage"
    if "完成" in text or "复检" in text or "复测" in text or "验收" in text:
        return "summary"
    return "chat"
def stage_num(text):  # 阶段关键词 → 整数序号（命中列表中最靠后的阶段）；无法判断返回 None（不带 stage 字段）
    for w, n in STAGE_MAP:
        if text and w in text:
            return n
    return None
def norm_sender(sender):  # MXID → localpart（@w2-monitor:domain → w2-monitor）；桥接账号代发 → human
    if sender == MATRIX_USER:
        return "human"
    return sender[1:].split(":")[0] if sender.startswith("@") else sender
def normalize(event):
    text = (event.get("content") or {}).get("body", "")
    msg = {"ts": event.get("origin_server_ts", int(time.time() * 1000)),
           "sender": norm_sender(event.get("sender", "")), "text": text, "kind": classify(text)}
    stage = stage_num(text)
    if stage is not None:
        msg["stage"] = stage
    return msg
class Feed:
    """房间消息订阅中心：单条 /sync 长轮询循环，广播给全部 SSE 客户端。"""

    def __init__(self):
        self.subs, self.approvals, self.since = [], {}, None
        self.load_state()

    def load_state(self):
        if STATE_FILE.exists():
            try:
                self.approvals = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("approvals", {})
            except Exception as e:  # noqa: BLE001
                log.warning("state load failed: %s", e)

    def save_state(self):
        RUNTIME.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({"approvals": self.approvals}, ensure_ascii=False, indent=2), encoding="utf-8")

    def extract_approvals(self, msg):
        text = msg["text"]
        for aid in set(APR_RE.findall(text)):
            rec = self.approvals.setdefault(aid, {"id": aid, "title": "", "action": "", "level": "",
                                                  "status": "pending", "raw": ""})
            def field(names):
                m = re.search(rf"(?:{'|'.join(names)})\s*[:：]\s*(.+)", text)
                return m.group(1).strip() if m else ""
            rec["title"] = rec["title"] or field(["标题", "审批事项", "事项"])
            rec["action"] = rec["action"] or field(["动作", "处置动作"])
            m = re.search(r"L[0-3]", field(["级别", "风险级别", "等级"]) or text)
            rec["level"] = rec["level"] or (m.group(0) if m else "")
            if "人工审批通过" in text or text.startswith("同意"):
                rec["status"] = "approved"
            elif text.startswith("拒绝") or "审批不通过" in text:
                rec["status"] = "rejected"
            rec["raw"] = text[:600]
            self.save_state()

    def dispatch(self, msg):
        self.extract_approvals(msg)
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:  # 慢客户端丢消息不阻塞主循环
                pass
feed = Feed()
async def feed_loop():
    """后台长轮询：登录 → 解析房间 → /sync(since 游标)。Matrix 不可用时退避重试，不崩溃。"""
    backoff = 2
    while True:
        try:
            if not matrix.token:
                await matrix.login()
            rid = await matrix.resolve_room(force=True)
            data = await matrix.sync(since=feed.since)
            feed.since = data.get("next_batch", feed.since)
            timeline = (data.get("rooms", {}).get("join", {}).get(rid, {})
                        .get("timeline", {}).get("events", []))
            for e in timeline:
                if e.get("type") == "m.room.message":
                    feed.dispatch(normalize(e))
            backoff = 2
        except Exception as e:  # noqa: BLE001
            log.warning("matrix sync failed: %s", e)
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                matrix.token = None  # token 失效，下一轮重新登录
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
@app.on_event("startup")
async def _startup():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    log.info("mode=%s dist=%s shared=%s", "mock" if MOCK else "live", WEB_DIST, SHARED_TASKS_DIR)
    if not MOCK:
        asyncio.create_task(feed_loop())
async def probe(url):
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            return (await c.get(url)).status_code < 400  # 404 也算不在线，防误报
    except Exception:  # noqa: BLE001
        return False
def err(msg, code=502):
    return JSONResponse({"ok": False, "error": msg}, code)
def _sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

# ---------- API ----------
@app.get("/api/health")
async def health():
    live = not MOCK
    return {"ok": True, "mode": "live" if live else "mock",
            "matrix": await probe(f"{MATRIX_BASE}/_matrix/client/versions") if live else False,
            "tool_server": await probe(f"{TOOL_BASE}/") if live else False,
            "shared_dir": SHARED_TASKS_DIR.is_dir()}
@app.get("/api/meta")
def meta():
    return {"agents": AGENTS, "scenarios": SCENARIOS,
            "leader": LEADER_USER, "room": TEAM_ROOM_NAME, "mode": "mock" if MOCK else "live"}
@app.post("/api/scenarios/{sid}/start")
async def scenario_start(sid: str):
    f = TASKS_DIR / f"{sid}.txt"
    if sid not in SCENARIO_IDS or not f.exists():
        return err(f"未知场景：{sid}，可选：{sorted(SCENARIO_IDS)}", 404)
    text = f.read_text(encoding="utf-8").strip()
    if MOCK:
        return {"ok": True, "mock": True, "room": TEAM_ROOM_NAME, "message": text}
    try:
        return {"ok": True, "event_id": await matrix.send_text(text, pill_html(text)),
                "room": matrix.room_id}
    except Exception as e:  # noqa: BLE001
        return err(f"Matrix 发送失败：{type(e).__name__}: {e}")
async def mock_replay(scenario):
    yield ": mock replay start\n\n"
    f = WEB_DIST / "playback" / f"{scenario}.json"
    if not f.exists():
        yield _sse({"kind": "block", "text": f"回放文件缺失：{f.name}"})
        return
    try:
        events = json.loads(f.read_text(encoding="utf-8")).get("events", [])
    except Exception as e:  # noqa: BLE001
        yield _sse({"kind": "block", "text": f"回放文件解析失败：{e}"})
        return
    t0 = 0.0
    for e in events:
        t = float(e.get("t", 0))
        await asyncio.sleep(max(0.0, min(t - t0, 5.0)))  # 单条间隔封顶 5s，便于演示
        t0 = t
        text = e.get("text", "")
        msg = {"ts": int(time.time() * 1000), "sender": norm_sender(e.get("sender", "mock")),
               "text": text, "kind": e.get("kind") or classify(text)}
        stage = e.get("stage")  # 回放数据约定为整数序号；中文标签/缺省时从文本推断
        if isinstance(stage, str) or stage is None:
            stage = stage_num(stage if isinstance(stage, str) else text)
        if stage is not None:
            msg["stage"] = stage
        feed.extract_approvals(msg)  # mock 下也能演示审批流
        yield _sse(msg)
    yield ": mock replay done\n\n"
@app.get("/api/events")
async def events(scenario: str = Query("scenario_a_obc_overvoltage")):
    if MOCK:
        return StreamingResponse(mock_replay(scenario), media_type="text/event-stream")
    q = asyncio.Queue(maxsize=200)
    feed.subs.append(q)

    async def gen():
        try:
            yield ": connected\n\n"
            last_ts = 0
            try:  # 历史回放：刷新/重连后补齐最近 60 条，前端据此重建 DAG/审批/报告状态
                for e in reversed(await matrix.recent_messages(60)):
                    m = normalize(e)
                    m["replay"] = True
                    last_ts = max(last_ts, m.get("ts", 0))
                    yield _sse(m)
            except Exception as e:  # noqa: BLE001
                log.warning("history replay failed: %s", e)
            while True:
                try:
                    m = await asyncio.wait_for(q.get(), timeout=15)
                    if m.get("ts", 0) <= last_ts:
                        continue  # 已被历史回放覆盖，去重
                    yield _sse(m)
                except asyncio.TimeoutError:
                    yield ": hb\n\n"  # 15s 心跳注释行
        finally:
            if q in feed.subs:
                feed.subs.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
@app.get("/api/approvals")
def approvals():
    items = sorted(feed.approvals.values(), key=lambda r: r["id"], reverse=True)
    return {"approvals": items, "count": len(items)}
@app.post("/api/approvals/{aid}/decision")
async def approval_decision(aid: str, body: dict):
    decision = (body or {}).get("decision", "")
    if decision not in ("approve", "reject"):
        return err('decision 只能是 "approve" 或 "reject"', 400)
    if not APR_RE.fullmatch(aid):
        return err(f"审批单号格式非法：{aid}", 400)
    if decision == "approve":
        text = f"同意。我是售后站长，人工审批通过审批单 {aid}，请按审批内容继续执行处置动作，并在房间留痕。"
    else:
        text = f"拒绝。我是售后站长，人工审批不通过审批单 {aid}，请停止相关处置动作，保持车辆安全状态并说明原因。"
    rec = feed.approvals.setdefault(aid, {"id": aid, "title": "", "action": "", "level": "",
                                          "status": "pending", "raw": ""})
    rec["status"] = "approved" if decision == "approve" else "rejected"
    feed.save_state()
    # 同步回写工具服务器审批单状态，使 L2 动作可凭 approval_id 放行执行（失败不阻断主流程）
    try:
        async with httpx.AsyncClient(base_url=TOOL_BASE, timeout=4) as c:
            await c.post(f"/approvals/{aid}/decision", json={"decision": decision})
    except Exception as e:  # noqa: BLE001
        log.warning("tool-server approval decision sync failed: %s", e)
    if MOCK:
        return {"ok": True, "mock": True, "approval_id": aid, "status": rec["status"], "message": text}
    try:
        event_id = await asyncio.wait_for(matrix.send_text(text, pill_html(f"@x-diag-leader {text}")), timeout=8)
        return {"ok": True, "approval_id": aid, "status": rec["status"], "event_id": event_id}
    except Exception as e:  # noqa: BLE001
        return err(f"Matrix 发送失败（状态已本地更新）：{type(e).__name__}: {e}")
def synth_signals(scenario):
    """mock 合成信号：确定性伪随机，列名与真实 CSV 对齐。"""
    rnd = random.Random(scenario)
    base = dt.datetime.fromisoformat("2026-08-15T09:00:00" if scenario.endswith("overvoltage")
                                     else "2026-08-16T14:00:00")
    cols = ["timestamp", "pack_voltage", "pack_current", "soc", "cell_temp_max", "alarm_level"]
    rows = [[(base + dt.timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S"),
             round(398 + rnd.uniform(-1, 1), 2), round(-16 + rnd.uniform(-1, 1), 2),
             round(55 + i * 0.01, 2), round(32 + i * 0.01 + rnd.uniform(-0.2, 0.2), 1),
             1 if i > 480 else 0] for i in range(600)]
    return {"columns": cols, "rows": rows}
@app.get("/api/signals")
async def signals(scenario: str = Query(""), sig: str = Query("", alias="signals"),
                  frm: str = Query("", alias="from"), to: str = Query("", alias="to")):
    if scenario not in SCENARIO_IDS:
        return err(f"未知场景：{scenario}，可选：{sorted(SCENARIO_IDS)}", 404)
    if MOCK:
        f = WEB_DIST / "playback" / f"{scenario}_signals.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 回放文件坏了就退回合成数据
                pass
        return synth_signals(scenario) | {"count": 600, "sampled": False, "mock": True}
    body = {"start": frm, "end": to, "signals": [s for s in sig.split(",") if s]}
    try:
        async with httpx.AsyncClient(base_url=TOOL_BASE, timeout=TIMEOUT) as c:
            r = await c.post(f"/tools/{scenario}/vehicle_data.query_signals", json=body)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return err(f"工具网关调用失败：{type(e).__name__}: {e}")
    if "error" in data:
        return err(f"工具网关返回错误：{data['error']}")
    rows, n = data.get("rows") or [], len(data.get("rows") or [])
    if n > 300:  # >300 行等距降采样
        rows = rows[:: math.ceil(n / 300)]
    cols = list(rows[0].keys()) if rows else []
    return {"columns": cols, "rows": [[r.get(c, "") for c in cols] for r in rows],
            "count": len(rows), "sampled": n > 300, "vin": data.get("vin", "")}
@app.get("/api/files")
def list_files(prefix: str = Query("")):
    if not SHARED_TASKS_DIR.is_dir():
        return {"files": [], "note": f"共享目录不存在（{SHARED_TASKS_DIR}），返回空清单"}
    out = []
    for p in sorted(SHARED_TASKS_DIR.rglob("*")):
        try:
            if not (p.is_file() and p.suffix in ALLOWED_EXT and p.stat().st_size <= MAX_FILE):
                continue
            rel = str(p.relative_to(SHARED_TASKS_DIR))
            if rel.startswith(prefix):
                out.append({"path": rel, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)})
        except OSError:
            continue
    return {"files": out, "count": len(out), "root": str(SHARED_TASKS_DIR)}
@app.get("/api/file")
def read_shared_file(path: str = Query("")):
    root = SHARED_TASKS_DIR.resolve()
    p = (root / path).resolve()
    if p != root and root not in p.parents:
        return err("路径越界：仅允许访问共享目录内文件", 403)
    if p.suffix not in ALLOWED_EXT:
        return err(f"仅支持 {sorted(ALLOWED_EXT)} 文件", 403)
    if not p.is_file():
        return err(f"文件不存在：{path}", 404)
    if p.stat().st_size > MAX_FILE:
        return err("文件超过 2MB，不予返回", 413)
    return PlainTextResponse(p.read_text(encoding="utf-8", errors="replace"))
@app.get("/api/selftest")
async def selftest():
    def ck(cond, okmsg, failmsg):
        return {"status": "ok" if cond else "fail", "detail": okmsg if cond else failmsg}
    checks = {}
    if MOCK:
        checks["matrix_login"] = {"status": "skip", "detail": "mock 模式不连接 Matrix"}
        checks["room_resolve"] = {"status": "skip", "detail": "mock 模式不解析房间"}
    else:
        try:
            await matrix.login()
            checks["matrix_login"] = {"status": "ok", "detail": f"{MATRIX_USER} 登录成功"}
        except Exception as e:  # noqa: BLE001
            checks["matrix_login"] = {"status": "fail", "detail":
                f"登录失败：{type(e).__name__}: {e}。请检查 MATRIX_BASE/MATRIX_USER/MATRIX_PASS，并确认账号已注册"}
        try:
            rid = await matrix.resolve_room(force=True)
            checks["room_resolve"] = {"status": "ok", "detail": f"房间「{TEAM_ROOM_NAME}」= {rid}"}
        except Exception as e:  # noqa: BLE001
            checks["room_resolve"] = {"status": "fail", "detail": f"{e}。请把 {MATRIX_USER} 邀请进房间后重试"}
    checks["tool_server"] = ck(await probe(f"{TOOL_BASE}/"), f"工具网关在线：{TOOL_BASE}",
                               f"连不上 {TOOL_BASE}，请先启动 mcp/vehicle_data_server/app.py（端口 18091）")
    checks["shared_dir"] = ck(SHARED_TASKS_DIR.is_dir(), f"共享目录存在：{SHARED_TASKS_DIR}",
                              f"共享目录不存在：{SHARED_TASKS_DIR}（AgentTeams 未运行时正常，文件清单返回空）")
    idx = WEB_DIST / "index.html"
    checks["dist_exists"] = ck(idx.exists(), f"前端目录：{WEB_DIST}",
                               f"找不到 {idx}，请先构建前端或设置 WEB_DIST")
    ok = all(v["status"] != "fail" for v in checks.values())
    return {"ok": ok, "mode": "mock" if MOCK else "live", "checks": checks}

# ---------- 静态托管 + SPA fallback（须注册在最后） ----------
@app.get("/{full_path:path}")
async def spa(full_path: str):
    root = WEB_DIST.resolve()
    if full_path:
        p = (root / full_path).resolve()
        if p.is_file() and (p == root or root in p.parents):
            return FileResponse(p)
    idx = WEB_DIST / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return JSONResponse({"ok": False, "error": f"前端未构建：找不到 {idx}（可设 WEB_DIST 指向构建产物）"}, 404)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
