#!/bin/bash
# 自救巡检（由外部 cron 每 5 分钟触发，VM 之外）：
# 检查 6 项：cloudflared / shim / ssh 三个 unit、SSH 用户存在、
# 无 /run/nologin、ha_connections==2。
# 健康 -> 静默 HEALTHY；异常 -> flock 单例执行 restore.sh，90 秒后复验，
# 输出 RESCUED（需通知用户）或 FAILED（告警+附日志）。
# VM 正在重启导致脚本无法运行时保持静默，下个 tick 重试。
set -uo pipefail
SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFDIR="$HOME/.cloudflared"
SSH_USER="$(tr -d '[:space:]' < "$SETUP_DIR/ssh_user" 2>/dev/null || true)"
LOCK="$CFDIR/healthcheck.lock"

check() {
  systemctl is-active -q cloudflared-edge-shim.service || return 1
  systemctl is-active -q cloudflared.service || return 1
  systemctl is-active -q ssh.service || return 1
  [ -n "$SSH_USER" ] && id "$SSH_USER" >/dev/null 2>&1 || return 1
  [ ! -e /run/nologin ] || return 1
  local ha
  ha="$(curl -s --max-time 5 http://127.0.0.1:20241/metrics 2>/dev/null \
        | awk '/^cloudflared_tunnel_ha_connections/{print $2}')"
  [ "$ha" = "2" ] || return 1
  return 0
}

if check; then
  echo "HEALTHY"
  exit 0
fi

mkdir -p "$CFDIR"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "RESCUE_IN_PROGRESS"
  exit 0
fi

echo "$(date '+%F %T') rescue start" >> "$CFDIR/rescue.log"
if ! bash "$SETUP_DIR/restore.sh" >> "$CFDIR/rescue.log" 2>&1; then
  echo "$(date '+%F %T') restore.sh 执行失败" >> "$CFDIR/rescue.log"
fi
sleep 90
if check; then
  echo "$(date '+%F %T') RESCUED" >> "$CFDIR/rescue.log"
  echo "RESCUED"
else
  echo "$(date '+%F %T') FAILED" >> "$CFDIR/rescue.log"
  echo "FAILED"
  echo "--- rescue.log 尾部 ---"
  tail -60 "$CFDIR/rescue.log"
fi
