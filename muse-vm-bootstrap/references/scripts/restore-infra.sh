#!/bin/bash
# restore-infra.sh — rebuild /etc systemd units from ~ backups after a VM
# replacement (which wipes /etc but keeps /home). Idempotent: does nothing
# when everything is already in place.
#
# Output contract for the watchdog cron:
#   "OK: ..."       -> nothing needed, stay silent
#   "RESTORED: ..." -> something was rebuilt, worth reporting
set -u
HOME_DIR=/home/hatch
UNIT_DIR=/etc/systemd/system
changed=()
actions=()

restore_unit() { # $1 = unit name, $2 = .bak path
    local unit="$1"
    local bak="$2"
    local target="$UNIT_DIR/$unit"
    if [[ ! -f "$target" ]] || ! cmp -s "$target" "$bak"; then
        cp "$bak" "$target"
        changed+=("$unit")
        actions+=("restored $unit from backup")
    fi
}

H=$HOME_DIR
restore_unit komari-agent.service      "$H/.komari/komari-agent.service.bak"
restore_unit kuma-push.service         "$H/bin/kuma-push.service.bak"
restore_unit kuma-push.timer           "$H/bin/kuma-push.timer.bak"
restore_unit frp-relay.service         "$H/.frp/frp-relay.service.bak"
restore_unit frpc.service              "$H/.frp/frpc.service.bak"
restore_unit demo-site.service         "$H/workspace/demo-site.service.bak"
restore_unit hermes-gateway.service    "$H/.hermes/hermes-gateway.service.bak"
restore_unit ops-collect.service       "$H/workspace/ops-collect.service.bak"
restore_unit ops-collect.timer         "$H/workspace/ops-collect.timer.bak"

if ((${#changed[@]} > 0)); then
    systemctl daemon-reload
    actions+=("daemon-reload")
fi

# ensure enabled + running (long-running services only; oneshots are timer-driven
# and are *supposed* to be inactive between triggers)
for unit in komari-agent.service frp-relay.service frpc.service \
            demo-site.service hermes-gateway.service; do
    if ! systemctl is-enabled -q "$unit" 2>/dev/null; then
        systemctl enable -q "$unit" 2>/dev/null && actions+=("enabled $unit")
    fi
    if [[ "$(systemctl is-active "$unit" 2>/dev/null)" != "active" ]]; then
        systemctl start -q "$unit" 2>/dev/null && actions+=("started $unit")
    fi
done
for unit in kuma-push.service ops-collect.service kuma-push.timer ops-collect.timer; do
    if ! systemctl is-enabled -q "$unit" 2>/dev/null; then
        systemctl enable -q "$unit" 2>/dev/null && actions+=("enabled $unit")
    fi
done
for timer in kuma-push.timer ops-collect.timer; do
    if [[ "$(systemctl is-active "$timer" 2>/dev/null)" != "active" ]]; then
        systemctl start -q "$timer" 2>/dev/null && actions+=("started $timer")
    fi
done

# dead apt mirror fix (recurs after every rebuild)
SRC=/etc/apt/sources.list.d/ubuntu.sources
if [[ -f "$SRC" ]] && grep -q "mirror.cogentco.com" "$SRC"; then
    [[ -f "$SRC.bak" ]] || cp "$SRC" "$SRC.bak"
    sed -i '/mirror\.cogentco\.com/d' "$SRC"
    actions+=("removed dead mirror.cogentco.com from apt sources")
fi

if ((${#actions[@]} == 0)); then
    echo "OK: all 9 units present and correct, services active"
else
    echo "RESTORED: $(IFS='; '; echo "${actions[*]}")"
fi
