# vehicle-data 工具服务（T1.4）

为 AgentTeams Worker 提供 HTTP 工具网关（等价工具契约，对齐官方 OpsPilot Demo 模式）。

- 协议：`POST http://172.18.0.1:18091/tools/{scenario_id}/{tool_name}`
- 数据源：`../../data/`（合成数据）；运行时状态写在 `./runtime/`（已 gitignore）
- 工具清单：见 `app.py` 顶部注释（9 个）

## 启动

```bash
pip install -r requirements.txt
nohup python3 app.py > server.log 2>&1 &
```

## 验证

```bash
curl http://127.0.0.1:18091/
curl -X POST http://127.0.0.1:18091/tools/scenario_a_obc_overvoltage/vehicle_data.query_dtc \
  -H 'Content-Type: application/json' -d '{"vin":"LSVA0000DEMO00001"}'
# 期望返回：dtc_active: ["P3509"]
```

## 安全设计（双保险）

`vehicle_action.execute` 在服务端硬拦截 L2/L3 动作——即使 TeamLeader 违规直接调用，也会被拒绝并引导去 `vehicle_action.create_approval`。授权不依赖 Agent 自觉。

## 重置演示状态

清码后 DTC 状态会持久化在 `runtime/`。重新演示前：

```bash
rm -rf runtime/
```
