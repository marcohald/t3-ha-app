#!/bin/bash
set -e

export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export LANGUAGE=en_US:en
export HOME=/data
export T3CODE_HOME=/data/.t3
unset GODEBUG

echo "=========================================================="
echo "Starting T3 Code Lite Home Assistant App..."
echo "=========================================================="

# Configure reliable DNS resolvers (prevents local CoreDNS negative caching of newly minted Cloudflare Tunnels)
if [ -w /etc/resolv.conf ]; then
    echo "nameserver 1.1.1.1" > /tmp/resolv.conf
    echo "nameserver 8.8.8.8" >> /tmp/resolv.conf
    grep -E '^nameserver' /etc/resolv.conf >> /tmp/resolv.conf 2>/dev/null || true
    cat /tmp/resolv.conf > /etc/resolv.conf 2>/dev/null || true
fi

# Ensure gh CLI binary symlink exists in /usr/bin
if [ -f /usr/local/bin/gh ] && [ ! -e /usr/bin/gh ]; then
    ln -sf /usr/local/bin/gh /usr/bin/gh 2>/dev/null || true
fi

# Persistent File Overlay System (/data/overlay -> /)
if [ -d /data/overlay ]; then
    echo "[Overlay] Restoring persistent file overlays from /data/overlay..."
    cp -rf /data/overlay/* / 2>/dev/null || true
fi
if [ -f /data/overlay/opt/t3code/html/index.html ]; then
    ln -sf /data/overlay/opt/t3code/html/index.html /opt/t3code/html/index.html
fi

# Ensure persistence and working directories exist
mkdir -p /data/.t3 /data/workspace /homeassistant /var/log/nginx /tmp/uploads

# ---------------------------------------------------------------------------
# Persistence: point every package manager at /data and write the agent
# instruction files that tell T3's agents to install software there.
# ---------------------------------------------------------------------------
/opt/t3code/persist.sh
# shellcheck disable=SC1091
. /data/tools/env.sh

# Configure Server / Environment Display Name
echo "Configuring T3 Code server/environment name..."
AUTH_TOKEN="${SUPERVISOR_TOKEN:-$HASSIO_TOKEN}"
SERVER_NAME=$(jq -r '.server_name // .addon_name // empty' /data/options.json 2>/dev/null || true)
if [ -z "$SERVER_NAME" ] && [ -n "$AUTH_TOKEN" ]; then
    SERVER_NAME=$(curl -s -H "Authorization: Bearer $AUTH_TOKEN" http://supervisor/core/api/config 2>/dev/null | \
        jq -r '.external_url // .internal_url // empty' 2>/dev/null | \
        sed -E 's|^https?://||; s|:[0-9]+/?$||; s|/$||')
fi

if [ -n "$SERVER_NAME" ]; then
    echo "Using server name: $SERVER_NAME"
    echo "PRETTY_HOSTNAME=\"$SERVER_NAME\"" > /etc/machine-info 2>/dev/null || true
    mkdir -p /data/overlay/etc /data/.t3
    echo "PRETTY_HOSTNAME=\"$SERVER_NAME\"" > /data/overlay/etc/machine-info
    echo "$SERVER_NAME" > /data/overlay/etc/hostname
    echo "$SERVER_NAME" > /etc/hostname 2>/dev/null || true
    echo "$SERVER_NAME" > /data/.t3/usage-host-id
    export T3CODE_HOST_ID="$SERVER_NAME"
fi

# ---------------------------------------------------------------------------
# T3 CLI: keep the binary and its runtime on /data so `t3 update` and
# `t3 doctor` are not undone by the next add-on update.
# ---------------------------------------------------------------------------
T3_BIN=$(/opt/t3code/t3-runtime.sh | tail -n1)
if [ -z "$T3_BIN" ] || [ ! -x "$T3_BIN" ]; then
    echo "ERROR: no usable t3 binary found." >&2
    exit 1
fi
export T3_BIN
echo "Using t3 binary: ${T3_BIN} ($("$T3_BIN" --version 2>/dev/null || echo unknown))"

# ---------------------------------------------------------------------------
# Home Assistant credentials for the bundled skill and for MCP.
# SUPERVISOR_TOKEN rotates on every start, so the token file is rewritten each
# boot and never baked into an image layer.
# ---------------------------------------------------------------------------
if [ -n "$AUTH_TOKEN" ]; then
    printf '%s\n' "$AUTH_TOKEN" > /data/.t3/ha.token
    chmod 600 /data/.t3/ha.token 2>/dev/null || true
    export HA_URL="${HA_URL:-http://supervisor/core}"
    export HA_TOKEN_FILE="${HA_TOKEN_FILE:-/data/.t3/ha.token}"
    cat << 'HAENV_EOF' > /data/tools/ha-env.sh
# Managed by t3-ha-app: Home Assistant credentials for the homeassistant skill.
export HA_URL="${HA_URL:-http://supervisor/core}"
export HA_TOKEN_FILE="${HA_TOKEN_FILE:-/data/.t3/ha.token}"
HAENV_EOF
    echo "[Skill] Home Assistant API credentials configured (${HA_URL})."
fi

# Install the bundled Home Assistant skill into every agent skill root on /data.
/opt/t3code/skills.sh

# Configure readline for UTF-8 input
cat << 'INPUTRC_EOF' > /data/.inputrc
set input-meta on
set output-meta on
set convert-meta off
INPUTRC_EOF

# Read configured LAN port (default 3774)
T3_PORT=$(jq -r '.port // 3774' /data/options.json 2>/dev/null || echo "3774")
export T3_PORT

# Start T3 Code Server in background on configured LAN port
LAN_ARG="--lan"
if [ -f "/data/.t3/service.env" ]; then
    if grep -q "T3CODE_LAN=false" /data/.t3/service.env; then
        LAN_ARG=""
    fi
fi
echo "Starting T3 Code server with 't3 serve $LAN_ARG --port $T3_PORT'..."
"$T3_BIN" serve $LAN_ARG --port "$T3_PORT" --auto-bootstrap-project-from-cwd /homeassistant > /data/t3_serve.log 2>&1 &
T3_PID=$!
echo "T3 Code server process launched with PID $T3_PID on port $T3_PORT (LAN: ${LAN_ARG:-disabled})."

# Configure MCP Servers for Home Assistant
echo "Configuring MCP Servers..."
HA_TOKEN=$(jq -r '.ha_token // empty' /data/options.json 2>/dev/null || true)
MQTT_HOST=$(jq -r '.mqtt_host // empty' /data/options.json 2>/dev/null || true)
MQTT_PORT=$(jq -r '.mqtt_port // "1883"' /data/options.json 2>/dev/null || true)
MQTT_USER=$(jq -r '.mqtt_user // empty' /data/options.json 2>/dev/null || true)
MQTT_PASS=$(jq -r '.mqtt_pass // empty' /data/options.json 2>/dev/null || true)

cat << 'MCP_EOF' > /homeassistant/mcp.json
{
  "mcpServers": {}
}
MCP_EOF

if [ -n "$HA_TOKEN" ]; then
    echo "Adding Home Assistant Core MCP server..."
    jq '.mcpServers.homeassistant = {"command": "npx", "args": ["-y", "mcp-server-home-assistant"], "env": {"HA_URL": "http://supervisor/core", "HA_TOKEN": "'"$HA_TOKEN"'"}}' /homeassistant/mcp.json > /tmp/mcp.json && mv /tmp/mcp.json /homeassistant/mcp.json
fi

if [ -n "$MQTT_HOST" ]; then
    echo "Adding Zigbee2MQTT MCP server..."
    MQTT_URL="mqtt://${MQTT_HOST}:${MQTT_PORT}"
    jq --arg url "$MQTT_URL" --arg user "$MQTT_USER" --arg pass "$MQTT_PASS" '.mcpServers.zigbee2mqtt = {"command": "npx", "args": ["-y", "mcp-server-zigbee2mqtt"], "env": {"MQTT_URL": $url, "MQTT_USERNAME": $user, "MQTT_PASSWORD": $pass}}' /homeassistant/mcp.json > /tmp/mcp.json && mv /tmp/mcp.json /homeassistant/mcp.json
fi

export MCP_CONFIG_PATH=/homeassistant/mcp.json

# Wait briefly for t3 serve to initialize, then display pairing details
sleep 3
echo "=========================================================="
echo "T3 Code Server Status & Mobile App Pairing Info:"
"$T3_BIN" pair || true
echo "=========================================================="

# Start Python Upload & API server on port 62906
python3 /opt/t3code/upload.py &

# Start ttyd on port 62905 connected to attach script
ttyd -a -b /ttyd -t copyOnSelect=true -t enableZmodem=true -t disableLeaveAlert=true -t disableResizeOverlay=true -t scrollback=10000 -p 62905 /opt/t3code/attach.sh &

# Start NGINX reverse proxy on Ingress port 62901
echo "Starting NGINX reverse proxy on port 62901..."
exec nginx -c /etc/nginx/nginx.conf -g "daemon off;"
