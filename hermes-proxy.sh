#!/usr/bin/env bash
# Hermes Anyrouter Proxy 管理脚本
# 用法: bash hermes-proxy.sh

PLIST="$HOME/Library/LaunchAgents/local.hermes-anyrouter-proxy.plist"
LABEL="local.hermes-anyrouter-proxy"
LOG="$HOME/Code/anyrouter_proxy/proxy.log"

# 清屏（可选）
# clear

echo "======================================"
echo "   Hermes Anyrouter Proxy 管理菜单   "
echo "======================================"
echo ""
echo "  1) 启动代理"
echo "  2) 停止代理"
echo "  3) 重启代理"
echo "  4) 查看状态"
echo "  5) 查看实时日志"
echo "  6) 修改推理强度 (effort)"
echo "  7) 退出"
echo ""
read -r -p "请选择操作 [1-7]: " choice

case $choice in
  1)
    echo ""
    if launchctl list | grep -q "$LABEL"; then
      echo "代理已在运行，无需重复启动。"
    else
      launchctl bootstrap gui/$(id -u) "$PLIST"
      sleep 1
      if launchctl list | grep -q "$LABEL"; then
        echo "✅ 代理启动成功"
        lsof -i :8989 2>/dev/null | grep LISTEN
      else
        echo "❌ 代理启动失败，请检查日志: $LOG"
      fi
    fi
    ;;
  2)
    echo ""
    launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null
    sleep 1
    if launchctl list | grep -q "$LABEL"; then
      echo "⚠️ 代理仍在运行，尝试强制停止..."
      PID=$(launchctl list | grep "$LABEL" | awk '{print $1}')
      [ -n "$PID" ] && [ "$PID" != "-" ] && kill "$PID" 2>/dev/null
    else
      echo "✅ 代理已停止"
    fi
    ;;
  3)
    echo ""
    echo "正在重启..."
    launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null
    sleep 2
    launchctl bootstrap gui/$(id -u) "$PLIST"
    sleep 1
    if launchctl list | grep -q "$LABEL"; then
      echo "✅ 代理重启成功"
    else
      echo "❌ 代理重启失败"
    fi
    ;;
  4)
    echo ""
    PID=$(launchctl list | grep "$LABEL" | awk '{print $1}')
    if [ -n "$PID" ] && [ "$PID" != "-" ]; then
      echo "状态: 🟢 运行中"
      echo "PID: $PID"
      echo "端口:"
      lsof -i :8989 2>/dev/null | grep LISTEN || echo "  (未检测到 8989 端口监听)"
      echo ""
      echo "最近 5 条日志:"
      tail -n 5 "$LOG" 2>/dev/null || echo "  (暂无日志)"
    else
      echo "状态: 🔴 未运行"
    fi
    ;;
  5)
    echo ""
    echo "正在监听日志 (按 Ctrl+C 退出)..."
    tail -f "$LOG"
    ;;
  6)
    echo ""
    CURRENT=$(plutil -extract EnvironmentVariables.HERMES_PROXY_EFFORT raw "$PLIST" 2>/dev/null || echo "medium")
    echo "当前推理强度: $CURRENT"
    echo ""
    echo "  1) low    (低)"
    echo "  2) medium (中)"
    echo "  3) high   (高)"
    echo "  4) xhigh  (极高)"
    echo "  5) 取消"
    echo ""
    read -r -p "请选择新的推理强度 [1-5]: " eff_choice
    case $eff_choice in
      1) NEW_EFF="low" ;;
      2) NEW_EFF="medium" ;;
      3) NEW_EFF="high" ;;
      4) NEW_EFF="xhigh" ;;
      5) echo "已取消"; exit 0 ;;
      *) echo "❌ 无效选项"; exit 1 ;;
    esac
    plutil -replace EnvironmentVariables.HERMES_PROXY_EFFORT -string "$NEW_EFF" "$PLIST"
    echo "✅ 推理强度已设为 $NEW_EFF"
    echo ""
    echo "正在重启代理以应用新配置..."
    launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null
    sleep 2
    launchctl bootstrap gui/$(id -u) "$PLIST"
    sleep 1
    if launchctl list | grep -q "$LABEL"; then
      echo "✅ 代理重启成功，当前 effort=$NEW_EFF"
    else
      echo "❌ 代理重启失败"
    fi
    ;;
  7)
    echo ""
    echo "已退出"
    exit 0
    ;;
  *)
    echo ""
    echo "❌ 无效选项，请输入 1-7"
    ;;
esac
