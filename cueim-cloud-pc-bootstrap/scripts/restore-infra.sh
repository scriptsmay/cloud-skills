#!/usr/bin/env bash
# Idempotently restore SSH, cloudflared, and CFSM probe services after resume.
# Configure through a root-owned file (default: /etc/cueim-cloud-pc/restore.conf).
set -u

CONFIG_FILE="${1:-/etc/cueim-cloud-pc/restore.conf}"
if [ "$(id -u)" -ne 0 ]; then
  exec sudo -n bash "$0" "$CONFIG_FILE"
fi
if [ ! -r "$CONFIG_FILE" ]; then
  echo "WARN: config file unavailable"
  exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG_FILE"

: "${LOGIN_USER:?set LOGIN_USER in restore.conf}"
: "${CLOUDFLARED_BIN:=/usr/bin/cloudflared}"
: "${CLOUDFLARED_TOKEN_FILE:=/etc/cloudflared/token}"
: "${CLOUDFLARED_UNIT_BACKUP:=/var/lib/cueim-cloud-pc/cloudflared.service}"
: "${CF_PROBE_BIN:=/usr/local/bin/cf-probe}"
: "${CF_PROBE_CONFIG:=/etc/config/cf-probe/config.conf}"
: "${CF_PROBE_UNIT_BACKUP:=/var/lib/cueim-cloud-pc/cf-probe.service}"
: "${AUTHORIZED_PUBKEY_FILE:=/etc/cueim-cloud-pc/authorized.pub}"

actions=(); warnings=(); changed=0
restore_unit() {
  local unit="$1" backup="$2" target="/etc/systemd/system/$1"
  if [ ! -r "$backup" ]; then warnings+=("missing $1 backup"); return; fi
  if [ ! -f "$target" ] || ! cmp -s "$target" "$backup"; then
    install -o root -g root -m 0644 "$backup" "$target"
    actions+=("restored $1"); changed=1
  fi
}

home="$(getent passwd "$LOGIN_USER" | cut -d: -f6)"
if [ -z "$home" ] || [ ! -d "$home" ]; then
  warnings+=("login user unavailable")
elif [ -s "$AUTHORIZED_PUBKEY_FILE" ]; then
  install -d -m 0700 -o "$LOGIN_USER" -g "$(id -gn "$LOGIN_USER")" "$home/.ssh"
  auth="$home/.ssh/authorized_keys"
  touch "$auth"
  while IFS= read -r key; do
    [ -n "$key" ] || continue
    if ! grep -qxF -- "$key" "$auth"; then printf '%s\n' "$key" >> "$auth"; actions+=("restored authorized key"); fi
  done < "$AUTHORIZED_PUBKEY_FILE"
  chown "$LOGIN_USER:$(id -gn "$LOGIN_USER")" "$auth"
  chmod 0600 "$auth"
else
  warnings+=("authorized public key file unavailable")
fi

if [ -x "$CLOUDFLARED_BIN" ] && [ -s "$CLOUDFLARED_TOKEN_FILE" ]; then
  restore_unit cloudflared.service "$CLOUDFLARED_UNIT_BACKUP"
else warnings+=("cloudflared binary or token unavailable"); fi
if [ -x "$CF_PROBE_BIN" ] && [ -s "$CF_PROBE_CONFIG" ]; then
  restore_unit cf-probe.service "$CF_PROBE_UNIT_BACKUP"
else warnings+=("CFSM probe binary or config unavailable"); fi

if [ "$changed" -eq 1 ]; then systemctl daemon-reload; actions+=("reloaded systemd"); fi
for unit in ssh.service cloudflared.service cf-probe.service; do
  if ! systemctl is-enabled -q "$unit" 2>/dev/null; then
    systemctl enable -q "$unit" 2>/dev/null && actions+=("enabled $unit") || warnings+=("could not enable service")
  fi
  if ! systemctl is-active -q "$unit" 2>/dev/null; then
    systemctl start "$unit" 2>/dev/null && actions+=("started $unit") || warnings+=("could not start service")
  fi
done

if [ "${#warnings[@]}" -gt 0 ]; then
  printf 'WARN: %s\n' "${warnings[*]}"; exit 1
elif [ "${#actions[@]}" -gt 0 ]; then
  printf 'RESTORED: %s\n' "${actions[*]}"
else
  echo 'OK: SSH, tunnel, and monitor services are active'
fi
