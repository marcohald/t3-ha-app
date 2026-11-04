#!/bin/bash
export PATH="/usr/local/bin:$PATH"
export T3CODE_HOME=/data/.t3
unset GODEBUG

echo -e "\033[1;36m┌────────────────────────────────────────────────────────┐\033[0m"
echo -e "\033[1;36m│\033[0m  \033[1;37m● T3 Code Lite Terminal (Home Assistant)\033[0m              \033[1;36m│\033[0m"
echo -e "\033[1;36m└────────────────────────────────────────────────────────┘\033[0m"
echo -e " \033[1;32m[T3 Server]\033[0m   Active on LAN port \033[1;33m3774\033[0m (or configured port)"
echo -e " \033[1;35m[Mobile App]\033[0m  Pair mobile app anytime: \033[1;33mt3 pair\033[0m"
echo -e " \033[1;34m[T3 Connect]\033[0m  Remote access relay: \033[1;33mt3 connect\033[0m or \033[1;33mt3 up --connect\033[0m"
echo -e " \033[1;32m[GitHub CLI]\033[0m  GitHub CLI available: \033[1;33mgh auth login\033[0m"
echo -e " \033[1;33m[Workspace]\033[0m   \033[0;32m/homeassistant\033[0m (Home Assistant config & share)"
echo ""
