# X主机厂智能诊断系统（x-diag）

> GOAI 世界人工智能开源大赛 · Agent Infra 新智基座赛道 参赛作品
> 面向汽车售后场景的多 Agent 智能诊断闭环平台，基于 AgentTeams 的「1+3」架构

## 在线演示

- 🖥️ **交互演示台**：http://124.222.91.86:18092 （监控大盘 / 诊断驾驶舱 / 场景一键跑通）
- 💬 **Element 协作房间**：http://124.222.91.86:18088 （多 Agent 协作全程留痕、可回放）
- 📊 **AgentTeams 面板**：http://124.222.91.86:13000 （1+3 团队实时状态）
- 🎬 **演示视频**：（链接待补充）

> 本仓库为演示台与文档开源版本；车辆与故障数据均为**合成演示数据**，不含任何真实车厂数据。

## 这是什么

售后故障诊断今天依赖专家个人经验：告警靠人盯、诊断靠人查、知识随人走。本项目把「监控预警 → 诊断推理 → 分级处置 → 知识沉淀」做成一条多 Agent 接力链路，设计原则只有两条：

- **只有需要自主决策的职能才做成 Agent**——其余确定性工作沉淀为 MCP 工具与可复用 Skill；
- **「自觉不可靠，授权才可靠」**——处置动作按 L0–L3 安全分级，经 Manager 唯一执行出口放行，高风险动作人工审批（人在回路），全程房间留痕、可审计、可回滚。

## 架构：1 Manager + 3 Workers

| 角色 | 职能 | 权限边界 |
| --- | --- | --- |
| Manager（编排器） | 任务拆解 · Worker 调度 · 上下文路由 · 结论仲裁 | 唯一执行出口；L0–L3 分级网关；不直接诊断 |
| W2 监控预警 Agent | 信号巡检 · 异常检测 · 告警取证 | L0 只读，无任何写入 |
| W3 诊断推理 Agent | DTC 解码 · 交叉验证 · 案例检索 · 假设排序 | L0 为主，健康档案可写 |
| W4 知识构建 Agent | 报告结构化 · 案例抽取 · 知识库沉淀 | L1，仅知识库可写 |

确定性职能不做成 Agent：数据接入 → MCP 工具层；引导诊修 → guided-flow-executor Skill；处置执行 → execute_action 工具 + 分级网关。

## 目录结构

| 目录 | 内容 | 状态 |
| --- | --- | --- |
| `data/dictionaries/` | GB/T 32960 公共信号字典 + 场景扩展信号（JSON Schema） | ✅ |
| `data/scenario_a_obc_overvoltage/` | 场景 A：OBC 输出电压过压合成数据（时序 CSV + DTC + 冻结帧 + 真值） | ✅ |
| `data/scenario_b_thermal_runaway/` | 场景 B：直流快充末端电池热失控前驱合成数据（时序 CSV + DTC + 冻结帧 + 真值） | ✅ |
| `mcp/vehicle_data_server/` | vehicle-data 工具服务（9 个工具端点，HTTP 契约，已通过冒烟测试） | ✅ |
| `skills/` | guided-flow-executor 等 Skill 定义（九要素） | 待补充 |
| `knowledge/` | DTC 字典 / 故障模式库 / Golden·Badcase 评估集 | 待补充 |
| `agents/` | [Team 创建消息](agents/create_team_messages.md)（Manager 编排器 + W2/W3/W4，含工具契约与 L0–L3 规则） | ✅ |
| `eval/` | 评估脚本与结果 | 待补充 |
| `docs/` | 架构图、设计说明、[环境部署指引](docs/环境部署指引.md) | ✅ |

## 快速开始

环境要求：4C8G、Docker。

```bash
# 安装 AgentTeams（Apache-2.0）
bash <(curl -sSL https://raw.githubusercontent.com/agentscope-ai/AgentTeams/main/install/agentteams-install.sh)
# LLM 使用阿里云百炼 qwen3.5-plus，安装向导中填入 API Key
# 启动后打开 Element Web UI：http://127.0.0.1:18088
```

完整部署步骤与演示剧本将随复赛代码包提供。

## 演示场景

- **场景 A（主场景）**：OBC 输出电压过压，DTC P3509，442V ≥ 440V 阈值——预警 → 派单 → 诊断 → L1 清码处置 → 复检入档，全链路自动接力；
- **场景 B（安全场景）**：电池热失控前兆——L2 高风险动作进入房间人工审批，证明「放权不放任」。

## 数据说明

全部数据为**合成数据**，不使用任何主机厂真实车辆与系统数据；信号字典基于 GB/T 32960 公共信号，场景扩展信号与阈值假设全部公开、可复算。详见 `data/README.md`。

## 安全设计

| 级别 | 定义 | 放行方式 |
| --- | --- | --- |
| L0 | 只读查询 | 按身份授权后自动放行 |
| L1 | 低风险写入（白名单：报告、知识库） | 自动放行 + 留痕 |
| L2 | 中风险动作 | 房间内人工审批 |
| L3 | 高风险动作 | 仅生成方案，强制人工审批，禁止直接执行 |

执行权唯一出口在 Manager；授权由 Higress AI 网关按身份下发；留痕在 Matrix 房间，天然支持人在回路。

## 开源协议

Apache License 2.0，见 [LICENSE](LICENSE)。

## 开发路线

- Phase 0–1：环境与端到端骨架（本仓库 + 合成数据层）
- Phase 2（当前）：核心诊断链路（MCP Server + Skills + 三 Worker）
- Phase 3：安全分级闭环 + 可观测（Trace / Log / Metrics）
- Phase 4：Demo 录制与复赛打包（8.25–9.3 提交窗口）
