#!/usr/bin/env bash
set -u
XD=/workspace/x-diag
GREEN='\033[32m'; RED='\033[31m'; YELLOW='\033[33m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✅ $1${NC}"; }
bad()  { echo -e "  ${RED}❌ $1${NC}"; }
note() { echo -e "  ${YELLOW}👉 $1${NC}"; }
port_up() { ss -tln 2>/dev/null | grep -qE ":$1[[:space:]]"; }

echo ""
echo "== 1/4 Matrix 服务端 (18080) =="
if port_up 18080; then ok "18080 已在运行"; else
  bad "18080 未运行"; note "请用你原来的命令启动 Matrix，启动后重新执行本脚本"
fi

echo ""
echo "== 2/4 工具服务器 (18091) =="
if port_up 18091; then ok "18091 已在运行"; else
  cd "$XD/mcp/vehicle_data_server" && nohup python3 app.py > server.log 2>&1 &
  sleep 2
  if port_up 18091; then ok "18091 已启动"; else
    bad "18091 启动失败，最后几行日志："; tail -5 "$XD/mcp/vehicle_data_server/server.log"
  fi
fi

echo ""
echo "== 3/4 桥接服务 (18092，带登录凭据) =="
if port_up 18092; then ok "18092 已在运行"; else
  cd "$XD/webbridge" && MATRIX_USER='@admin:matrix-local.agentteams.io:18080' MATRIX_PASS="${MATRIX_PASS:?请先在环境变量中设置 MATRIX_PASS}" nohup python3 app.py > bridge.log 2>&1 &
  sleep 3
  if port_up 18092; then ok "18092 已启动"; else
    bad "18092 启动失败，最后几行日志："; tail -5 "$XD/webbridge/bridge.log"
  fi
fi

echo ""
echo "== 4/4 Agent 团队面板 (13000) =="
if port_up 13000; then
  ok "13000 已在运行"; note "若 agent 显示离线，打开 13000 面板页面点「唤醒」"
else
  bad "13000 未运行"; note "请用你原来的命令启动 agent 团队面板"
fi

echo ""
echo "== 健康检查 =="
H=$(curl -s -m 4 localhost:18092/api/health 2>/dev/null)
if echo "$H" | grep -q '"ok":true'; then
  ok "桥接返回：$H"
  echo "$H" | grep -q '"matrix":true'      && ok "Matrix 连接正常"    || bad "Matrix 未连接（检查 18080）"
  echo "$H" | grep -q '"tool_server":true' && ok "工具服务器连接正常" || bad "工具服务器未连接（检查 18091）"
else
  bad "桥接健康检查失败，稍等 3 秒重跑本脚本"
fi

echo ""
echo "全部 ✅ 后，把下面网址粘贴到【浏览器地址栏】："
echo "  http://<你的服务器IP>:18092"
