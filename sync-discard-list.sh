#!/bin/bash
# Fetch the shared discard list and install it as an extra postfix transport map.
# Runs on the relay as root, from cron. See README.md.
#
#   sync-discard-list.sh            normal run (quiet when nothing changed)
#   sync-discard-list.sh --force    accept a list that adds more than MAX_ADD domains
#
# The local /etc/postfix/transport stays first in transport_maps, so the
# operator's own entries always win over the shared list.
set -euo pipefail
export LC_ALL=C   # [a-z] in grep must not match uppercase or non-ASCII letters

URL=${DISCARD_LIST_URL:-https://raw.githubusercontent.com/mrgluek/chatmail-discard-list/main/transport}
DST=/etc/postfix/transport_discard_shared
# Refuse a list that suddenly discards many new domains: a mistake or a
# compromised repo should not blackhole mail on every relay at once.
MAX_ADD=${DISCARD_LIST_MAX_ADD:-10}
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { echo "$*"; logger -t chatmail-discard-sync -- "$*" 2>/dev/null || true; }
keys() { awk '!/^[[:space:]]*(#|$)/ {print tolower($1)}' "$1" | sort -u; }

tmp=$(mktemp)
trap 'rm -f "$tmp" "$tmp.new" "$tmp.old"' EXIT
curl -fsS --max-time 30 --proto =https "$URL" -o "$tmp"

# 1. Only comments, empty lines and "<domain or [IPv4]> discard:".
#    Without this check a bad list could add "example.org smtp:[attacker]" and
#    route our users' mail to a third party instead of dropping it.
domain='[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+'
ipv4='\[[0-9]{1,3}(\.[0-9]{1,3}){3}\]'
bad=$(grep -nvE "^[[:space:]]*(#.*)?\$|^($domain|$ipv4)[[:space:]]+discard:[[:space:]]*\$" "$tmp" || true)
if [ -n "$bad" ]; then
    log "rejected list, invalid lines: $bad"
    exit 1
fi

# 2. Never discard our own domain or the big public relays, whatever the list says.
mail_domain=$(sed -n 's/^mail_domain[[:space:]]*=[[:space:]]*//p' /usr/local/lib/chatmaild/chatmail.ini 2>/dev/null || true)
protected="$mail_domain $(postconf -h myhostname) nine.testrun.org ${DISCARD_LIST_PROTECT:-}"
keys "$tmp" > "$tmp.new"
for d in $protected; do
    if grep -qxiF "$d" "$tmp.new"; then
        log "rejected list: protected domain $d is in it"
        exit 1
    fi
done

# 3. Limit how many domains one update may add. The very first install is a
#    manual step, so it is allowed to add the whole list.
if [ -f "$DST" ]; then keys "$DST" > "$tmp.old"; else : > "$tmp.old"; fi
added=$(comm -13 "$tmp.old" "$tmp.new" | tr '\n' ' ')
removed=$(comm -23 "$tmp.old" "$tmp.new" | tr '\n' ' ')
n_added=$(wc -w <<< "$added")
if [ -f "$DST" ] && [ "$n_added" -gt "$MAX_ADD" ] && [ "$FORCE" = 0 ]; then
    log "refused: list adds $n_added domains at once (limit $MAX_ADD); review it and run with --force"
    exit 1
fi

changed=0
if ! cmp -s "$tmp" "$DST"; then
    install -m 644 "$tmp" "$DST"
    postmap "hash:$DST"
    changed=1
    log "list updated: added: ${added:-none}; removed: ${removed:-none}"
fi

# 4. Make sure postfix uses the map, after the local one. Checked on every run
#    because `cmdeploy run` rewrites main.cf and drops transport_maps.
cur=$(postconf -h transport_maps)
if [[ " ${cur//,/ } " != *" hash:$DST "* ]]; then
    new="${cur:+$cur, }hash:$DST"
    postconf -e "transport_maps = $new"
    changed=1
    log "transport_maps = $new"
fi

if [ "$changed" = 1 ]; then
    postfix reload 2>/dev/null   # it prints "refreshing..." to stderr, which cron would mail
fi
