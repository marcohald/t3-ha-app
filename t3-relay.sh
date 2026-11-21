#!/bin/bash
# Remember and restore the T3 Connect relay across add-on updates.
#
# t3 serve and its cloudflared relay process live inside the container, so an
# add-on update kills the tunnel. Only /data survives: the stored authorization
# and the managed relay client (cloudflared) are there, but after an update the
# status reads "Environment link: pending server startup / Relay: not
# provisioned". T3's own fix is: run `t3 connect`, then restart T3 Code so it
# applies the link and launches the managed tunnel. This script does exactly
# that, automatically, but only when the relay was connected before.
set -u

DATA_HOME="${T3CODE_HOME:-/data/.t3}"
MARKER="${DATA_HOME}/t3-relay-was-connected"
RELAY_STATE_DIR="${DATA_HOME}/userdata/relay-clients"
LOG="${DATA_HOME}/t3-relay-restore.log"
T3_BIN="${T3_BIN:-/usr/local/bin/t3}"
T3_PORT="${T3_PORT:-3774}"

log() { echo "[T3 Relay] $*" >&2; }

status_text() {
    T3CODE_HOME="$DATA_HOME" timeout 15 "$T3_BIN" connect status 2>/dev/null || true
}

tunnel_connected() {
    grep -qE 'Tunnel connected|Other devices can reach this machine'
}

case "${1:-}" in
    remember)
        mkdir -p "$(dirname "$MARKER")"
        printf '1\n' > "$MARKER"
        log "Recorded an active T3 Connect relay in ${MARKER}"
        ;;
    forget)
        rm -f "$MARKER"
        log "Cleared the T3 Connect relay marker"
        ;;
    was-connected)
        [ -f "$MARKER" ]
        ;;
    restore)
        if [ ! -x "$T3_BIN" ]; then
            log "No usable t3 binary at ${T3_BIN}; cannot restore the relay."
            exit 0
        fi

        if [ ! -f "$MARKER" ]; then
            # Older installs have no marker yet. A stored credential together
            # with an enabled exposure is T3's persistent state for "this
            # machine was connected", so treat it as previously connected.
            status="$(status_text)"
            if grep -q 'Authorization: stored credential' <<< "$status" && \
               grep -q 'Exposure: enabled' <<< "$status"; then
                log "Stored T3 Connect authorization found; restoring relay."
            else
                log "No previous relay connection recorded; nothing to restore."
                exit 0
            fi
        else
            log "Relay was connected before the update; restoring T3 Connect."
        fi

        # relay-clients/<pid>.json points at the old container's PIDs. Drop it
        # so t3 does not think a relay is still running in this fresh one.
        if [ -d "$RELAY_STATE_DIR" ]; then
            rm -f "$RELAY_STATE_DIR"/*.json 2>/dev/null || true
        fi

        (
            # Wait for t3 serve (started by run.sh) to come up and provision
            # the environment link, then check whether it already relinked.
            sleep 8
            if status_text | tunnel_connected; then
                echo "$(date -Is) relay already connected after restart" >> "$LOG"
                exit 0
            fi

            # `t3 connect` installs the managed relay client if needed and
            # applies the environment link.
            echo "$(date -Is) running 't3 connect' to reinstall/relink relay" >> "$LOG"
            if T3CODE_HOME="$DATA_HOME" timeout 120 "$T3_BIN" connect --headless >> "$LOG" 2>&1; then
                # T3 requires a server restart so the new link takes effect.
                echo "$(date -Is) restarting t3 serve to apply the relay link" >> "$LOG"
                fuser -k "${T3_PORT}/tcp" 2>/dev/null || true
                sleep 1
                setsid env T3CODE_HOME="$DATA_HOME" "$T3_BIN" serve --lan \
                    --port "$T3_PORT" /homeassistant >> /data/t3_serve.log 2>&1 < /dev/null &
            else
                echo "$(date -Is) 't3 connect' failed; relay not restored" >> "$LOG"
            fi
        ) &
        ;;
    *)
        echo "Usage: t3-relay.sh {remember|forget|was-connected|restore}" >&2
        exit 2
        ;;
esac
