#!/bin/bash
# cloudflared SSH 通道一键重建脚本（8 步，幂等，可重复执行）
# 持久化输入：
#   ~/.cloudflared/token        tunnel token（600，不进任何 unit 文件）
#   ~/.cloudflared/user.pub     允许登录的 SSH 公钥（可多行）
#   ~/workspace/cloudflared-setup/ssh_user   SSH 用户名（单行，uid 0）
#   ~/workspace/cloudflared-setup/proxy.env  出站代理（单行，如 http://host:port，不带鉴权）
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES="$SETUP_DIR/files"
CFDIR="$HOME/.cloudflared"

SSH_USER="$(tr -d '[:space:]' < "$SETUP_DIR/ssh_user" 2>/dev/null || true)"
[ -n "$SSH_USER" ] || { echo "ERROR: 未设置 SSH 用户名，请写入 $SETUP_DIR/ssh_user（单行）"; exit 1; }

# 剥离代理 URL 中的 userinfo：密码每几分钟轮换，存下来的立刻过期；
# 且实测无鉴权 CONNECT 即返回 200，shim 从不发送 Proxy-Authorization
PROXY_RAW="$(tr -d '[:space:]' < "$SETUP_DIR/proxy.env" 2>/dev/null || true)"
PROXY="$(echo "$PROXY_RAW" | sed -E 's#^(https?://)[^/@]+@#\1#')"
[ -n "$PROXY" ] || { echo "ERROR: 未设置出站代理，请写入 $SETUP_DIR/proxy.env（单行）"; exit 1; }

log() { echo "[$(date '+%F %T')] $*"; }

### [1/8] cloudflared 二进制
log "[1/8] cloudflared"
if command -v cloudflared >/dev/null 2>&1; then
  log "  已安装：$(cloudflared --version 2>/dev/null | head -1)"
else
  DEB="$SETUP_DIR/cloudflared-linux-amd64.deb"
  [ -f "$DEB" ] || { log "  ERROR: 缺少 $DEB"; exit 1; }
  log "  dpkg -i $DEB"
  dpkg -i "$DEB"
fi

### [2/8] openssh-server + SSH 用户（uid 0，因 NoNewPrivs 下 sudo 无效）
log "[2/8] openssh-server 与用户 $SSH_USER"
if ! dpkg -s openssh-server >/dev/null 2>&1; then
  if ls "$SETUP_DIR/debs"/openssh-server_*.deb >/dev/null 2>&1; then
    log "  用 debs/ 缓存安装（免 apt）"
    dpkg -i "$SETUP_DIR"/debs/openssh-server_*.deb "$SETUP_DIR"/debs/openssh-sftp-server_*.deb || true
  fi
  if ! dpkg -s openssh-server >/dev/null 2>&1; then
    log "  缓存不可用，走 apt 安装"
    apt-get update -qq || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server
  fi
fi
if id "$SSH_USER" >/dev/null 2>&1; then
  if [ "$(id -u "$SSH_USER")" != "0" ]; then
    log "  $SSH_USER 已存在但 uid=$(id -u "$SSH_USER")，改为 0"
    usermod -u 0 -o "$SSH_USER"
  else
    log "  用户 $SSH_USER 已存在（uid 0）"
  fi
else
  log "  创建用户 $SSH_USER（uid 0，root 等效）"
  useradd -m -u 0 -o -s /bin/bash "$SSH_USER"
fi
# sudoers 照写（表意；实际不依赖 sudo）
echo "$SSH_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$SSH_USER" 2>/dev/null || true
chmod 440 "/etc/sudoers.d/$SSH_USER" 2>/dev/null || true
# 公钥同步
PUB="$CFDIR/user.pub"
[ -f "$PUB" ] || { log "  ERROR: 缺少 $PUB"; exit 1; }
SSH_HOME="$(eval echo "~$SSH_USER")"
mkdir -p "$SSH_HOME/.ssh"
grep -v '^[[:space:]]*#' "$PUB" | grep -v '^[[:space:]]*$' > "$SSH_HOME/.ssh/authorized_keys" || true
chmod 700 "$SSH_HOME/.ssh"; chmod 600 "$SSH_HOME/.ssh/authorized_keys"
chown -R 0:0 "$SSH_HOME/.ssh"
log "  公钥 $(wc -l < "$SSH_HOME/.ssh/authorized_keys") 把已同步"

### [3/8] SSH 主机密钥（持久化，重启不变）+ sshd 加固
log "[3/8] sshd 主机密钥与加固"
mkdir -p "$CFDIR/ssh_host_keys"
if ls "$CFDIR/ssh_host_keys"/ssh_host_* >/dev/null 2>&1; then
  log "  恢复持久化主机密钥"
  cp -f "$CFDIR"/ssh_host_keys/ssh_host_* /etc/ssh/
  chmod 600 /etc/ssh/ssh_host_*_key
  chmod 644 /etc/ssh/ssh_host_*_key.pub
else
  log "  快照当前主机密钥到 $CFDIR/ssh_host_keys"
  cp -f /etc/ssh/ssh_host_* "$CFDIR/ssh_host_keys/"
  chmod 600 "$CFDIR"/ssh_host_keys/*_key 2>/dev/null || true
fi
mkdir -p /etc/ssh/sshd_config.d
cp -f "$FILES/sshd-keyonly.conf" /etc/ssh/sshd_config.d/99-keyonly.conf
mkdir -p /run/sshd && chmod 755 /run/sshd  # /run 是 tmpfs，重启会丢，每次重建
sshd -t
rm -f /run/nologin /etc/nologin   # 沙箱遗留，否则报 "System is booting up"
systemctl enable --now ssh
log "  ssh: $(systemctl is-active ssh)"

### [4/8] 安装脚本与静态文件
log "[4/8] 安装 shim / wrapper / watchdog / 静态 unit"
install -m 755 "$FILES/cloudflared-edge-shim.py" /usr/local/bin/cloudflared-edge-shim.py
install -m 755 "$FILES/cloudflared-ns.sh" /usr/local/bin/cloudflared-ns.sh
install -m 755 "$FILES/cloudflared-watchdog.sh" /usr/local/bin/cloudflared-watchdog.sh
install -m 644 "$FILES/cloudflared.service" /etc/systemd/system/cloudflared.service
mkdir -p /etc/cloudflared
install -m 644 "$FILES/hosts.edge" /etc/cloudflared/hosts.edge

### [5/8] 生成 unit（替换占位符）+ daemon-reload
log "[5/8] 生成 shim unit / override / watchdog timer"
sed "s#__HTTPS_PROXY__#$PROXY#" "$FILES/cloudflared-edge-shim.service" \
  > /etc/systemd/system/cloudflared-edge-shim.service
mkdir -p /etc/systemd/system/cloudflared.service.d
install -m 644 "$FILES/cloudflared-override.conf" \
  /etc/systemd/system/cloudflared.service.d/override.conf
install -m 644 "$FILES/cloudflared-watchdog.service" \
  /etc/systemd/system/cloudflared-watchdog.service
install -m 644 "$FILES/cloudflared-watchdog.timer" \
  /etc/systemd/system/cloudflared-watchdog.timer
systemctl daemon-reload

### [6/8] 启动服务
log "[6/8] 启动服务"
systemctl enable --now cloudflared-edge-shim.service
systemctl enable --now cloudflared-watchdog.timer
if [ -f "$CFDIR/token" ]; then
  systemctl enable --now cloudflared.service
else
  log "  token 缺失，跳过 cloudflared（写入 $CFDIR/token 600 后重跑本脚本）"
fi

### [7/8] 状态自检
log "[7/8] 状态自检"
for u in cloudflared-edge-shim cloudflared-watchdog.timer ssh; do
  printf '  %-28s %s\n' "$u" "$(systemctl is-active "$u")"
done
if [ -f "$CFDIR/token" ]; then
  printf '  %-28s %s\n' cloudflared "$(systemctl is-active cloudflared)"
  HA="$(curl -s --max-time 5 http://127.0.0.1:20241/metrics 2>/dev/null \
        | awk '/^cloudflared_tunnel_ha_connections/{print $2}')"
  log "  ha_connections=${HA:-?}（期望 2）"
fi

### [8/8]
log "[8/8] 完成"
