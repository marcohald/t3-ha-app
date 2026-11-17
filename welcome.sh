#!/bin/bash
export HOME=/data
export PATH="/usr/local/bin:$PATH"
export T3CODE_HOME=/data/.t3
unset GODEBUG

# Persistent toolchain: package managers and Home Assistant credentials on /data.
if [ -f /data/tools/env.sh ]; then
    # shellcheck disable=SC1091
    . /data/tools/env.sh
fi

T3_VERSION="unknown"
if command -v t3 >/dev/null 2>&1; then
    T3_VERSION=$(t3 --version 2>/dev/null | sed -E 's/^t3 v?//' | head -n1)
fi

echo -e "\033[1;36m┌────────────────────────────────────────────────────────┐\033[0m"
echo -e "\033[1;36m│\033[0m  \033[1;37m● T3 Code Lite Terminal (Home Assistant)\033[0m              \033[1;36m│\033[0m"
echo -e "\033[1;36m└────────────────────────────────────────────────────────┘\033[0m"
echo -e " \033[1;32m[T3 Server]\033[0m   Active on LAN port \033[1;33m3774\033[0m (or configured port), t3 \033[1;33m${T3_VERSION}\033[0m"
echo -e " \033[1;35m[Mobile App]\033[0m  Pair mobile app anytime: \033[1;33mt3 pair\033[0m"
echo -e " \033[1;34m[T3 Connect]\033[0m  Remote access relay: \033[1;33mt3 connect\033[0m or \033[1;33mt3 up --connect\033[0m"
echo -e " \033[1;32m[GitHub CLI]\033[0m  GitHub CLI available: \033[1;33mgh auth login\033[0m"
echo -e " \033[1;36m[Persistent]\033[0m  Installs go to \033[1;33m/data/tools\033[0m (survives add-on updates)"
echo -e " \033[1;36m[HA Skill]\033[0m    \033[1;33mhomeassistant\033[0m skill ready: \033[1;33mha_ws.py ping\033[0m"
echo -e " \033[1;33m[Workspace]\033[0m   \033[0;32m/homeassistant\033[0m (Home Assistant config & share), \033[0;32m/data/workspace\033[0m"
echo ""
