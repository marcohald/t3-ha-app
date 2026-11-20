#!/bin/bash
# Integration test for the built T3 Code add-on container.
# Validates that all services boot cleanly, the CLI tools exist and run,
# and that no invalid flags are used when starting t3 serve.
set -euo pipefail

IMAGE_TAG="${1:-local/t3-ha-app:test}"
CONTAINER_NAME="t3-ha-app-ci-test"

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
    -p 3774 \
    "${IMAGE_TAG}"

# 2. Wait up to 30 seconds for ttyd and nginx / upload.py to start
echo "=== [CI Test] Waiting for services to initialize ==="
for i in $(seq 1 30); do
    if docker exec "${CONTAINER_NAME}" pgrep -f "nginx" >/dev/null 2>&1 && \
       docker exec "${CONTAINER_NAME}" pgrep -f "upload.py" >/dev/null 2>&1; then
        echo "Core processes (nginx, upload.py) running (attempt $i)."
        break
    fi
    sleep 1
done

# 3. Verify t3 CLI is present, executable, and matches expected syntax
echo "=== [CI Test] Checking T3 CLI binary and serve process ==="
docker exec "${CONTAINER_NAME}" /usr/local/bin/t3 --version
docker exec "${CONTAINER_NAME}" bash -l -c "t3 --version"

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
ACTUAL_OC=$(docker exec "${CONTAINER_NAME}" opencode --version | sed -n 's/^v\{0,1\}\([0-9][^ ]*\).*/\1/p' | tr -d '[:space:]')
if [ "${ACTUAL_OC}" != "${EXPECTED_OC}" ]; then
    echo "::error::opencode version mismatch: expected ${EXPECTED_OC}, got ${ACTUAL_OC}"
    exit 1
fi
echo "✓ OpenCode CLI verified (${ACTUAL_OC})."

# 5. Verify GitHub CLI (gh) is present
echo "=== [CI Test] Checking GitHub CLI ==="
docker exec "${CONTAINER_NAME}" gh --version
echo "✓ GitHub CLI verified."

# 6. Verify Home Assistant Skill
echo "=== [CI Test] Checking Home Assistant Skill ==="
docker exec "${CONTAINER_NAME}" test -f /data/.claude/skills/homeassistant/SKILL.md
docker exec "${CONTAINER_NAME}" python3 /opt/t3code/skill/homeassistant/scripts/ha_ws.py --help >/dev/null
echo "✓ Home Assistant skill verified."

# 7. Check Version Endpoint from upload.py API
echo "=== [CI Test] Checking /version_check API response ==="
docker exec "${CONTAINER_NAME}" python3 -c '
import urllib.request, json
res = urllib.request.urlopen("http://127.0.0.1:62906/version_check")
data = json.loads(res.read().decode())
print("API Response:", json.dumps(data, indent=2))
assert data.get("claude_version") not in (None, "not installed", "unknown"), "Claude CLI missing in API"
assert data.get("opencode_version") not in (None, "not installed", "unknown"), "OpenCode CLI missing in API"
assert data.get("ha_skill") == "installed", "HA skill missing in API"
'
echo "✓ API version check verified."

echo "=== [CI Test] All add-on integration tests PASSED! ==="
