#!/bin/bash
# Integration test for the built T3 Code add-on container.
# Validates that all services boot cleanly, the CLI tools exist and run,
# and that no invalid flags are used when starting t3 serve.
set -euo pipefail

IMAGE_TAG="${1:-local/t3-ha-app:test}"
CONTAINER_NAME="${CONTAINER_NAME:-t3-ha-app-ci-test}"
API_PORT="${API_PORT:-62906}"
T3_PORT="${T3_PORT:-3774}"
EXPECT_CHANNEL="${EXPECT_CHANNEL:-stable}"

echo "=== [CI Test] Running integration test on image: ${IMAGE_TAG} ==="

cleanup() {
    echo "=== [CI Test] Cleaning up test container ==="
    docker rm -f "${CONTAINER_NAME}" 2>/dev/null || true
}
trap cleanup EXIT

# 1. Start container with mock supervisor & homeassistant mounts
echo "=== [CI Test] Starting container ==="
docker run -d \
    --name "${CONTAINER_NAME}" \
    -e SUPERVISOR_TOKEN="test_ci_mock_token" \
    -e HASSIO_TOKEN="test_ci_mock_token" \
    -p "${T3_PORT}" \
    "${IMAGE_TAG}"

# 2. Wait up to 120 seconds for t3 serve, nginx and upload.py to start.
#    A fresh container downloads and installs the pinned T3 runtime on first
#    boot, which can take a while on a slow runner.
echo "=== [CI Test] Waiting for services to initialize ==="
ready=0
for i in $(seq 1 120); do
    if docker exec "${CONTAINER_NAME}" pgrep -f "nginx" >/dev/null 2>&1 && \
       docker exec "${CONTAINER_NAME}" pgrep -f "upload.py" >/dev/null 2>&1 && \
       docker exec "${CONTAINER_NAME}" pgrep -f "t3 serve" >/dev/null 2>&1; then
        echo "Core processes (t3 serve, nginx, upload.py) running (attempt $i)."
        ready=1
        break
    fi
    sleep 1
done
if [ "${ready}" -ne 1 ]; then
    echo "::error::services did not start within 120s"
    docker logs "${CONTAINER_NAME}" || true
    docker exec "${CONTAINER_NAME}" cat /data/t3_serve.log 2>/dev/null || true
    exit 1
fi

# 3. Verify t3 CLI is present, executable, and matches expected syntax
echo "=== [CI Test] Checking T3 CLI binary and serve process ==="
docker exec "${CONTAINER_NAME}" /usr/local/bin/t3 --version
docker exec "${CONTAINER_NAME}" bash -l -c "t3 --version"
T3_VERSION_STRING=$(docker exec "${CONTAINER_NAME}" /usr/local/bin/t3 --version | tr -d '\r')
echo "T3 version: ${T3_VERSION_STRING}"
if [ "${EXPECT_CHANNEL}" = "nightly" ]; then
    grep -qi 'nightly' <<< "${T3_VERSION_STRING}" || {
        echo "::error::expected a nightly T3 build, got ${T3_VERSION_STRING}"; exit 1; }
    echo "✓ Nightly T3 build verified."
fi

# Verify t3 serve is actually running and did not crash
sleep 2
if docker exec "${CONTAINER_NAME}" pgrep -f "t3 serve" >/dev/null 2>&1; then
    echo "✓ 't3 serve' is actively running."
else
    echo "::error::'t3 serve' is not running! Checking /data/t3_serve.log..."
    docker exec "${CONTAINER_NAME}" cat /data/t3_serve.log || true
    docker logs "${CONTAINER_NAME}" || true
    exit 1
fi

# 4. Verify Claude Code CLI is present, executable, and functional
echo "=== [CI Test] Checking Claude Code CLI ==="
docker exec "${CONTAINER_NAME}" claude --version
docker exec "${CONTAINER_NAME}" bash -l -c "claude --version"
echo "✓ Claude Code CLI verified."

# 4b. Verify OpenCode CLI is present and pinned
echo "=== [CI Test] Checking OpenCode CLI ==="
docker exec "${CONTAINER_NAME}" opencode --version
docker exec "${CONTAINER_NAME}" bash -l -c "opencode --version"
EXPECTED_OC=$(docker exec "${CONTAINER_NAME}" cat /opt/t3code/opencode-version.txt | tr -d '[:space:]')
ACTUAL_OC=$(docker exec "${CONTAINER_NAME}" opencode --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 | tr -d '[:space:]')
if [ "${ACTUAL_OC}" != "${EXPECTED_OC}" ]; then
    echo "::error::opencode version mismatch: expected ${EXPECTED_OC}, got ${ACTUAL_OC}"
    exit 1
fi
echo "✓ OpenCode CLI verified (${ACTUAL_OC})."

# 4c. Verify T3 Connect relay persistence across add-on updates
echo "=== [CI Test] Checking T3 Connect relay restore helper ==="
docker exec "${CONTAINER_NAME}" test -x /opt/t3code/t3-relay.sh
docker exec "${CONTAINER_NAME}" /opt/t3code/t3-relay.sh forget
docker exec "${CONTAINER_NAME}" test ! -e /data/.t3/t3-relay-was-connected
docker exec "${CONTAINER_NAME}" /opt/t3code/t3-relay.sh remember
docker exec "${CONTAINER_NAME}" test -f /data/.t3/t3-relay-was-connected
docker exec "${CONTAINER_NAME}" /opt/t3code/t3-relay.sh was-connected
docker exec "${CONTAINER_NAME}" /opt/t3code/t3-relay.sh forget
echo "✓ T3 Connect relay marker verified."

# 4d. Verify extra-package installer and the linux-packages skill
echo "=== [CI Test] Checking extra-package support ==="
docker exec "${CONTAINER_NAME}" test -x /opt/t3code/install-extra-packages.sh
docker exec "${CONTAINER_NAME}" test -f /opt/t3code/skill/linux-packages/SKILL.md
docker exec "${CONTAINER_NAME}" test -f /data/.claude/skills/linux-packages/SKILL.md
docker exec "${CONTAINER_NAME}" test -x /opt/t3code/skill/linux-packages/scripts/pkgs.py
docker exec "${CONTAINER_NAME}" python3 /opt/t3code/skill/linux-packages/scripts/pkgs.py list >/dev/null
docker exec "${CONTAINER_NAME}" python3 - <<'PY'
import http.server
import json
import os
import subprocess
import tempfile
import threading

STATE = {
    "options": {
        "port": 3774,
        "server_name": "",
        "ha_token": "",
        "mqtt_host": "",
        "mqtt_port": "1883",
        "mqtt_user": "",
        "mqtt_pass": "!secret mqtt_pass",
        "extra_packages": [],
    },
    "posts": [],
}

class MockSupervisor(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer test-token":
            return self._send(401, {"result": "error", "message": "Unauthorized"})
        if self.path == "/addons/self/info":
            return self._send(200, {"result": "ok", "data": {"options": STATE["options"]}})
        return self._send(404, {"result": "error", "message": "Not found"})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer test-token":
            return self._send(401, {"result": "error", "message": "Unauthorized"})
        if self.path == "/addons/self/options":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            STATE["posts"].append(payload)
            STATE["options"] = dict(payload["options"])
            return self._send(200, {"result": "ok", "data": {}})
        return self._send(404, {"result": "error", "message": "Not found"})

srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockSupervisor)
port = srv.server_address[1]
thread = threading.Thread(target=srv.serve_forever, daemon=True)
thread.start()

try:
    with tempfile.TemporaryDirectory() as tmp:
        opts_file = os.path.join(tmp, "options.json")
        with open(opts_file, "w", encoding="utf-8") as fh:
            json.dump({"port": 3774, "extra_packages": []}, fh)
        legacy_file = os.path.join(tmp, "extra-packages.list")
        log_file = os.path.join(tmp, "extra-packages.log")

        env = dict(
            os.environ,
            SUPERVISOR_URL=f"http://127.0.0.1:{port}",
            SUPERVISOR_TOKEN="test-token",
            OPTIONS_FILE=opts_file,
            EXTRA_PACKAGES_LIST=legacy_file,
            EXTRA_PACKAGES_LOG=log_file,
        )
        pkgs_py = "/opt/t3code/skill/linux-packages/scripts/pkgs.py"

        res = subprocess.run(["python3", pkgs_py, "add", "--no-install", "openssh-client"], env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        res = subprocess.run(["python3", pkgs_py, "add", "--no-install", "htop"], env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert STATE["options"]["extra_packages"] == ["openssh-client", "htop"], STATE["options"]
        assert STATE["options"]["mqtt_pass"] == "!secret mqtt_pass", STATE["options"]

        res = subprocess.run(["python3", pkgs_py, "list"], env=env, capture_output=True, text=True)
        assert res.returncode == 0 and "openssh-client" in res.stdout and "htop" in res.stdout, res.stdout

        res = subprocess.run(["python3", pkgs_py, "remove", "openssh-client"], env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert STATE["options"]["extra_packages"] == ["htop"], STATE["options"]

        with open(legacy_file, "w", encoding="utf-8") as fh:
            fh.write("# legacy list\nbash\n")
        res = subprocess.run(["/opt/t3code/install-extra-packages.sh"], env=env, capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        assert not os.path.exists(legacy_file), "Legacy list file was not removed after migration"
        assert STATE["options"]["extra_packages"] == ["htop", "bash"], STATE["options"]
finally:
    srv.shutdown()
    srv.server_close()
PY
echo "✓ Extra-package installer and skill verified."

# 5. Verify GitHub CLI (gh) is present
echo "=== [CI Test] Checking GitHub CLI ==="
docker exec "${CONTAINER_NAME}" gh --version
echo "✓ GitHub CLI verified."

# 6. Verify Home Assistant Skill
echo "=== [CI Test] Checking Home Assistant Skill ==="
docker exec "${CONTAINER_NAME}" test -f /data/.claude/skills/homeassistant/SKILL.md
docker exec "${CONTAINER_NAME}" python3 /opt/t3code/skill/homeassistant/scripts/ha_ws.py --help >/dev/null
echo "✓ Home Assistant skill verified."

# 6b. Verify /data entries point at the shared data directory
echo "=== [CI Test] Checking shared data directory ==="
docker exec "${CONTAINER_NAME}" test -d /share/t3-ha-app-data
SHARED_TARGET=$(docker exec "${CONTAINER_NAME}" readlink -f /data/.t3)
if [ "${SHARED_TARGET}" != "/share/t3-ha-app-data/.t3" ]; then
    echo "::error::/data/.t3 is not linked to the shared data directory: ${SHARED_TARGET}"
    exit 1
fi
echo "✓ Shared data directory verified (${SHARED_TARGET})."

# 7. Check Version Endpoint from upload.py API
echo "=== [CI Test] Checking /version_check API response ==="
docker exec -e "TEST_API_PORT=${API_PORT}" "${CONTAINER_NAME}" python3 -c '
import os, urllib.request, json
res = urllib.request.urlopen("http://127.0.0.1:" + os.environ["TEST_API_PORT"] + "/version_check")
data = json.loads(res.read().decode())
print("API Response:", json.dumps(data, indent=2))
assert data.get("claude_version") not in (None, "not installed", "unknown"), "Claude CLI missing in API"
assert data.get("opencode_version") not in (None, "not installed", "unknown"), "OpenCode CLI missing in API"
assert data.get("ha_skill") == "installed", "HA skill missing in API"
'
echo "✓ API version check verified."

echo "=== [CI Test] All add-on integration tests PASSED! ==="
