#!/bin/bash
set -e

export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export LANGUAGE=en_US:en
export PATH="/usr/local/bin:$PATH"
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

# Persistent File Overlay System (/data/overlay -> /)
if [ -d /data/overlay ]; then
    echo "[Overlay] Restoring persistent file overlays from /data/overlay..."
    cp -rf /data/overlay/* / 2>/dev/null || true
fi
if [ -f /data/overlay/opt/t3code/html/index.html ]; then
    ln -sf /data/overlay/opt/t3code/html/index.html /opt/t3code/html/index.html
fi

# Background auto-update check for T3 CLI
echo "Checking for T3 CLI updates..."
(
    curl -fsSL --connect-timeout 4 --max-time 20 https://t3.codes/install.sh | T3CODE_INSTALL_BIN_DIR=/usr/local/bin T3CODE_HOME=/opt/t3 sh 2>&1 | tee /tmp/t3_update.log || true
) &

# Ensure persistence and working directories exist
mkdir -p /data/.t3 /homeassistant /var/log/nginx /tmp/uploads

# Configure readline for UTF-8 input
cat << 'EOF' > /data/.inputrc
set input-meta on
set output-meta on
set convert-meta off
EOF

# Read configured LAN port (default 3774)
T3_PORT=$(jq -r '.port // 3774' /data/options.json 2>/dev/null || echo "3774")
export T3_PORT

# Start T3 Code Server in background on configured LAN port
echo "Starting T3 Code server with 't3 serve --lan --port $T3_PORT'..."
/usr/local/bin/t3 serve --lan --port "$T3_PORT" --auto-bootstrap-project-from-cwd /homeassistant > /data/t3_serve.log 2>&1 &
T3_PID=$!
echo "T3 Code server process launched with PID $T3_PID on port $T3_PORT."

# Configure MCP Servers for Home Assistant
echo "Configuring MCP Servers..."
HA_TOKEN=$(jq -r '.ha_token // empty' /data/options.json 2>/dev/null || true)
MQTT_HOST=$(jq -r '.mqtt_host // empty' /data/options.json 2>/dev/null || true)
MQTT_PORT=$(jq -r '.mqtt_port // "1883"' /data/options.json 2>/dev/null || true)
MQTT_USER=$(jq -r '.mqtt_user // empty' /data/options.json 2>/dev/null || true)
MQTT_PASS=$(jq -r '.mqtt_pass // empty' /data/options.json 2>/dev/null || true)

cat << 'EOF' > /homeassistant/mcp.json
{
  "mcpServers": {}
}
EOF

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
/usr/local/bin/t3 pair || true
echo "=========================================================="

# Start Python Upload & API server on port 62906
python3 /opt/t3code/upload.py &

# Start ttyd on port 62905 connected to attach script
ttyd -a -b /ttyd -t copyOnSelect=true -t enableZmodem=true -t disableLeaveAlert=true -t disableResizeOverlay=true -t scrollback=10000 -p 62905 /opt/t3code/attach.sh &

# Start NGINX reverse proxy on Ingress port 62901
echo "Starting NGINX reverse proxy on port 62901..."
exec nginx -c /etc/nginx/nginx.conf -g "daemon off;"
