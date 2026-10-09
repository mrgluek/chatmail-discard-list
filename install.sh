#!/bin/bash
# Install, update or remove the shared discard list on a chatmail relay.
#
#   curl -fsSL https://raw.githubusercontent.com/mrgluek/chatmail-discard-list/main/install.sh | sudo bash
#   curl -fsSL .../install.sh | sudo bash -s -- --uninstall
#
# Running it again updates sync-discard-list to the current version and keeps
# the cron schedule. The sync script never updates itself: that would let
# anyone with write access to the repo run code as root on every relay.
#
# Everything is inside main(), which is called on the last line: if the
# download is cut off half-way, bash runs nothing instead of half a script.
set -euo pipefail

main() {
    local repo=https://raw.githubusercontent.com/mrgluek/chatmail-discard-list/main
    local bin=/usr/local/sbin/sync-discard-list
    local cron=/etc/cron.d/chatmail-discard-list
    local map=/etc/postfix/transport_discard_shared

    [ "$(id -u)" = 0 ] || { echo "run as root (sudo bash)" >&2; exit 1; }
    command -v postconf >/dev/null || { echo "postfix not found, is this a mail relay?" >&2; exit 1; }

    if [ "${1:-}" = "--uninstall" ]; then
        rm -f "$cron" "$bin"
        # Drop only our map from transport_maps, keep whatever else is there.
        local maps
        maps=$(postconf -h transport_maps | tr ',' '\n' | sed 's/^[[:space:]]*//' | grep -vxF "hash:$map" | paste -sd, - | sed 's/,/, /g')
        postconf -e "transport_maps = $maps"
        rm -f "$map" "$map.db"
        postfix reload 2>/dev/null
        echo "removed; transport_maps = $maps"
        return
    fi

    local tmp
    tmp=$(mktemp)
    curl -fsS --max-time 30 --proto =https "$repo/sync-discard-list.sh" -o "$tmp"
    bash -n "$tmp"   # do not install a broken or truncated script
    install -m 755 "$tmp" "$bin"
    rm -f "$tmp"
    echo "installed $bin"

    # Keep an existing schedule. A new one gets a random minute and hour offset,
    # so relays do not all fetch the list at the same moment.
    if [ ! -f "$cron" ]; then
        local m=$((RANDOM % 60)) h=$((RANDOM % 6))
        echo "$m $h,$((h + 6)),$((h + 12)),$((h + 18)) * * * root $bin" > "$cron"
        echo "cron: $(cat "$cron")"
    fi

    "$bin"
    echo "transport_maps = $(postconf -h transport_maps)"
    echo "entries in shared list: $(grep -cv '^[[:space:]]*\(#\|$\)' "$map")"
}

main "$@"
