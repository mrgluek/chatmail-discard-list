#!/bin/bash
# Install, update or remove the shared discard list on a chatmail relay.
#
#   curl -fsSL https://raw.githubusercontent.com/mrgluek/chatmail-discard-list/main/install.sh | sudo bash
#   curl -fsSL .../install.sh | sudo bash -s -- --uninstall
#   curl -fsSL .../install.sh | sudo bash -s -- --docker [DIR]    # chatmail/docker, run on the host
#
# Running it again updates sync-discard-list to the current version and keeps
# the schedule. The sync script never updates itself: that would let anyone
# with write access to the repo run code as root on every relay.
#
# Everything is inside main(), which is called on the last line: if the
# download is cut off half-way, bash runs nothing instead of half a script.
set -euo pipefail

REPO=${DISCARD_LIST_REPO:-https://raw.githubusercontent.com/mrgluek/chatmail-discard-list/main}
BIN=/usr/local/sbin/sync-discard-list
UNIT=chatmail-discard-sync
MAP=/etc/postfix/transport_discard_shared
OLD_CRON=/etc/cron.d/chatmail-discard-list   # used by versions before the timer

service_unit() {
    cat <<EOF
[Unit]
Description=Sync the shared chatmail discard list into postfix
Documentation=https://github.com/mrgluek/chatmail-discard-list
# chatmail/docker runs cmdeploy at container start (chatmail-init.service),
# which rewrites main.cf. Sync after it, or our transport_maps entry is lost
# until the next run. Where that unit does not exist this line does nothing.
After=chatmail-init.service postfix.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$BIN
EOF
}

# Every 6 hours at a per-relay random minute and hour offset, so that relays do
# not all fetch at once. RandomizedDelaySec is not used: it would also delay
# the run shortly after boot (or container start), which has to be quick
# because cmdeploy may just have dropped transport_maps.
timer_unit() {
    cat <<EOF
[Unit]
Description=Sync the shared chatmail discard list every 6 hours

[Timer]
OnBootSec=3min
OnCalendar=*-*-* $1:00
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
}

# "H,H,H,H:MM" for OnCalendar. Reuses the minute and hours of an existing
# timer or old cron entry, so an update does not move the schedule.
schedule() {
    local timer=$1 m h
    if [ -f "$timer" ]; then
        sed -n 's/^OnCalendar=\*-\*-\* \(.*\):00$/\1/p' "$timer"
    elif [ -f "$OLD_CRON" ]; then
        # cron "*/6" means "0/6" in OnCalendar syntax
        awk '{sub(/^\*/, "0", $2); printf "%s:%02d\n", $2, $1}' "$OLD_CRON"
    else
        m=$((RANDOM % 60)) h=$((RANDOM % 6))
        printf '%s,%s,%s,%s:%02d\n' $h $((h + 6)) $((h + 12)) $((h + 18)) $m
    fi
}

download_script() {
    local tmp
    tmp=$(mktemp)
    curl -fsS --max-time 30 --proto =https "$REPO/sync-discard-list.sh" -o "$tmp"
    bash -n "$tmp"   # do not install a broken or truncated script
    echo "$tmp"
}

# Write in place (same inode). A file bind-mounted into a container keeps
# pointing at the old inode if it is replaced by a new file instead.
write_file() {   # write_file <path> <mode>  (content on stdin)
    [ -e "$1" ] || install -m "$2" /dev/null "$1"
    cat > "$1"
    chmod "$2" "$1"
}

uninstall() {
    systemctl disable --now "$UNIT.timer" 2>/dev/null || true
    rm -f "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.timer" "$OLD_CRON" "$BIN"
    systemctl daemon-reload
    # Drop only our map from transport_maps, keep whatever else is there.
    local maps
    maps=$(postconf -h transport_maps | tr ',' '\n' | sed 's/^[[:space:]]*//' | grep -vxF "hash:$MAP" | paste -sd, - | sed 's/,/, /g' || true)
    postconf -e "transport_maps = $maps"
    rm -f "$MAP" "$MAP.db"
    postfix reload 2>/dev/null
    echo "removed; transport_maps = $maps"
}

install_host() {
    local tmp sched
    command -v postconf >/dev/null || { echo "postfix not found, is this a mail relay? (chatmail/docker: use --docker)" >&2; exit 1; }
    command -v systemctl >/dev/null || { echo "systemd not found" >&2; exit 1; }

    tmp=$(download_script)
    install -m 755 "$tmp" "$BIN"
    rm -f "$tmp"
    echo "installed $BIN"

    sched=$(schedule "/etc/systemd/system/$UNIT.timer")
    service_unit > "/etc/systemd/system/$UNIT.service"
    timer_unit "$sched" > "/etc/systemd/system/$UNIT.timer"
    rm -f "$OLD_CRON"
    systemctl daemon-reload
    systemctl enable --now "$UNIT.timer" 2>/dev/null
    echo "timer: OnCalendar=*-*-* $sched:00 (and 3 min after boot)"

    "$BIN"
    echo "transport_maps = $(postconf -h transport_maps)"
    echo "entries in shared list: $(grep -cv '^[[:space:]]*\(#\|$\)' "$MAP")"
}

# chatmail/docker: the container is rebuilt from the image on every update,
# so files installed inside it are lost. Keep them on the host and bind-mount
# them into the container instead.
install_docker() {
    local dir=$1 tmp sched
    mkdir -p "$dir"
    tmp=$(download_script)
    write_file "$dir/sync-discard-list" 755 < "$tmp"
    rm -f "$tmp"
    sched=$(schedule "$dir/$UNIT.timer")
    service_unit | write_file "$dir/$UNIT.service" 644
    timer_unit "$sched" | write_file "$dir/$UNIT.timer" 644
    # Enabling a unit means a symlink in timers.target.wants/, but a bind mount
    # is always a regular file there, which systemd ignores. A drop-in that
    # adds the dependency to timers.target works as a plain file.
    printf '[Unit]\nWants=%s.timer\n' "$UNIT" | write_file "$dir/timers-target.conf" 644
    echo "files in $dir; timer: OnCalendar=*-*-* $sched:00 (and 3 min after container start)"
    cat <<EOF

Add to docker-compose.override.yaml (next to docker-compose.yaml) if not there yet:

services:
  chatmail:
    volumes:
      - $dir/sync-discard-list:$BIN:ro
      - $dir/$UNIT.service:/etc/systemd/system/$UNIT.service:ro
      - $dir/$UNIT.timer:/etc/systemd/system/$UNIT.timer:ro
      - $dir/timers-target.conf:/etc/systemd/system/timers.target.d/$UNIT.conf:ro

then run: docker compose up -d
After an update (running this again) no restart is needed.
EOF
}

main() {
    [ "$(id -u)" = 0 ] || { echo "run as root (sudo bash)" >&2; exit 1; }
    case "${1:-}" in
        --uninstall) uninstall ;;
        --docker) install_docker "${2:-/opt/chatmail-discard-list}" ;;
        "") install_host ;;
        *) echo "usage: install.sh [--uninstall | --docker [DIR]]" >&2; exit 1 ;;
    esac
}

main "$@"
