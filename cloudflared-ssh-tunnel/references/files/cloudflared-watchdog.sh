#!/bin/bash
# VM 内 watchdog（每分钟由 systemd timer 触发）：
# 检查 shim 端口可达、cloudflared 进程存在、sshd active、metrics 非零；
# 异常则重启对应服务。日志：~/.cloudflared/watchdog.log
set -uo pipefail
CFDIR="$HOME/.cloudflared"
LOG="$CFDIR/watchdog.log"
mkdir -p "$CFDIR"

ts() { date '+%F %T'; }

# 1. shim 端口 127.0.0.1:7844 可达
if ! timeout 3 bash -c 'echo >/dev/tcp/127.0.0.1/7844' 2>/dev/null; then
  echo "$(ts) shim 7844 不可达，重启 cloudflared-edge-shim" >> "$LOG"
  systemctl restart cloudflared-edge-shim.service
fi

# 2. cloudflared 进程存在
if ! pgrep -f "cloudflared tunnel" >/dev/null 2>&1; then
  echo "$(ts) cloudflared 进程缺失，重启 cloudflared" >> "$LOG"
  systemctl restart cloudflared.service
fi

# 3. sshd active
if ! systemctl is-active -q ssh.service; then
  echo "$(ts) ssh.service 未 active，重启" >> "$LOG"
  rm -f /run/nologin /etc/nologin
  systemctl restart ssh.service
fi

# 4. metrics：ha_connections 应为 2（tunnel 注册成功）
HA="$(curl -s --max-time 5 http://127.0.0.1:20241/metrics 2>/dev/null \
      | awk '/^cloudflared_tunnel_ha_connections/{print $2}')"
if [ -n "$HA" ] && [ "$HA" != "2" ]; then
  echo "$(ts) ha_connections=$HA（期望 2），重启 cloudflared" >> "$LOG"
  systemctl restart cloudflared.service
fi
