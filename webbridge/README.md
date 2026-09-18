# webbridge — x-diag 演示台桥接服务

FastAPI 单文件服务（`app.py`，默认端口 **18092**），把演示台前端与既有系统连接起来：

- **Matrix（Conduit）**：向团队房间「Team: x-diag」发送诊断任务（带对队长的真 mention）、SSE 实时转发房间消息、代发人工审批回复；
- **工具网关**（`POST /tools/{scenario_id}/{tool_name}`，无认证）：代理信号查询并降采样；
- **共享目录**：浏览 agent 产出的 workspace 文件（仅 .json/.md/.csv，≤2MB，防目录穿越）；
- **静态托管**：托管前端构建产物（SPA fallback 到 index.html）。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WEBBRIDGE_PORT` | `18092` | 本服务监听端口 |
| `BRIDGE_MOCK` | 空 | 置 `1` 进入 mock 模式：不连 Matrix，事件流回放 `WEB_DIST/playback/*.json`，信号返回合成数据 |
| `MATRIX_BASE` | `http://localhost:18080` | Matrix（Conduit） homeserver 地址 |
| `MATRIX_USER` | `@webbridge:matrix-local.agentteams.io:18080` | 桥接服务登录账号（**需已注册且被邀请进团队房间**） |
| `MATRIX_PASS` | 空 | 桥接账号密码 |
| `TEAM_ROOM_NAME` | `Team: x-diag` | 团队房间名（按 m.room.name 精确匹配，见「已知限制」） |
| `LEADER_USER` | `@x-diag-leader:<MATRIX_USER 的域名>` | 队长 Matrix ID，用于任务消息 / 审批回复的 mention pill |
| `TOOL_BASE` | `http://localhost:18091` | vehicle-data 工具网关地址 |
| `SHARED_TASKS_DIR` | `~/agentteams/shared/tasks` | 共享 workspace 目录；不存在时文件清单降级返回空 |
| `WEB_DIST` | `../web/dist` | 前端构建产物目录；不存在时自动退回内置演示页 `webbridge/web/dist` |

## 启动步骤（小白版）

```bash
cd x-diag/webbridge

# 1. 安装依赖（只需一次）
pip install -r requirements.txt

# 2. 确认工具网关已启动（另开一个终端）
cd ../mcp/vehicle_data_server && nohup python3 app.py > tool.log 2>&1 &

# 3. 启动桥接服务（live 模式，连接真实 Matrix）
cd ../../webbridge
MATRIX_USER='@webbridge:matrix-local.agentteams.io:18080' \
MATRIX_PASS='你的密码' \
nohup python3 app.py > bridge.log 2>&1 &

# 没有 Matrix 环境？用 mock 模式先跑通演示：
BRIDGE_MOCK=1 nohup python3 app.py > bridge.log 2>&1 &

# 4. 打开浏览器
#    http://localhost:18092/           演示台页面
#    http://localhost:18092/docs       FastAPI 自动接口文档
```

停止服务：`pkill -f "python3 app.py"` 或 `kill $(lsof -ti:18092)`。

## 自检方法

```bash
# 逐项体检（小白友好，会告诉你每项失败该怎么办）
curl -s http://localhost:18092/api/selftest | python3 -m json.tool

# 健康总览：mode=live|mock，matrix/tool_server/shared_dir 是否可用
curl -s http://localhost:18092/api/health

# 看 SSE 消息流（mock 模式会按回放脚本逐条吐出）
curl -N --max-time 10 'http://localhost:18092/api/events'

# 发起场景 A 诊断任务（live 模式会真实投递到 Matrix 房间并 @ 队长）
curl -s -X POST http://localhost:18092/api/scenarios/scenario_a_obc_overvoltage/start

# 信号查询（live 模式代理工具网关，>300 行自动降采样）
curl -s 'http://localhost:18092/api/signals?scenario=scenario_a_obc_overvoltage&signals=pack_voltage,soc' | head -c 400

# 审批单列表 / 人工审批（approve 或 reject）
curl -s http://localhost:18092/api/approvals
curl -s -X POST http://localhost:18092/api/approvals/APR-20260815-094215/decision \
     -H 'Content-Type: application/json' -d '{"decision":"approve"}'

# 共享目录文件
curl -s 'http://localhost:18092/api/files?prefix='
curl -s 'http://localhost:18092/api/file?path=<上面列出的相对路径>'
```

## API 一览

| 接口 | 说明 |
|---|---|
| `GET /api/health` | `{ok, mode, matrix, tool_server, shared_dir}` |
| `GET /api/meta` | agent 名册 + 场景 A/B 元数据（硬编码） |
| `POST /api/scenarios/{id}/start` | 读 `tasks/{id}.txt` 模板，向房间发任务消息（m.text + org.matrix.custom.html pill + m.mentions） |
| `GET /api/events` | SSE 消息流 `{ts, sender, text, kind, stage?}`，15s 心跳注释行；mock 下回放 `playback/{scenario}.json` |
| `GET /api/approvals` | 从消息流正则提取的审批单（内存 + `runtime/bridge_state.json` 持久化） |
| `POST /api/approvals/{aid}/decision` | 代发「同意/拒绝。我是售后站长……」审批回复（带 leader mention） |
| `GET /api/signals` | 代理 `vehicle_data.query_signals`，返回 `{columns, rows}` 紧凑格式 |
| `GET /api/files` / `GET /api/file` | 共享目录文件清单 / 内容（严格限制在 SHARED_TASKS_DIR 内） |
| `GET /api/selftest` | matrix_login / room_resolve / tool_server / shared_dir / dist_exists 逐项检查 |

## 已知限制

- **房间名解析策略**：live 模式通过 `/joined_rooms` 列出全部已加入房间，再逐个读 `m.room.name` state 与 `TEAM_ROOM_NAME` **精确匹配**，结果缓存 60s。若桥接账号未进房或房间重名，会解析失败（selftest 会提示）。
- 审批单字段（标题/动作/级别）依赖房间消息中的「字段名：值」文本格式，agent 措辞变化时可能提取不全（`raw` 字段保留原文兜底）。
- SSE 为单进程内存广播，多实例部署需各自连 Matrix；`/sync` 长轮询 timeout=30s，客户端超时相应放宽（其余外部调用均 ≤10s）。
- 消息 kind 推断是正则启发式，仅用于前端展示分色，不影响真实业务流程。
