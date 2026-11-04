#!/bin/bash
SESSION_ID=${1:-1}
SESSION_NAME="t3_${SESSION_ID}"

export TERM=xterm-256color
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export LANGUAGE=en_US:en
export PATH="/usr/local/bin:$PATH"
export T3CODE_HOME=/data/.t3
unset GODEBUG

# Ensure UI overlay link is active if present
if [ -f /data/overlay/opt/t3code/html/index.html ] && [ ! -L /opt/t3code/html/index.html ]; then
    ln -sf /data/overlay/opt/t3code/html/index.html /opt/t3code/html/index.html
fi

# Create tmux session if it doesn't exist yet
if ! tmux -f /etc/tmux.conf has-session -t "$SESSION_NAME" 2>/dev/null; then
    tmux -f /etc/tmux.conf new-session -d -s "$SESSION_NAME" -c "/homeassistant" "bash -c '/opt/t3code/welcome.sh; exec bash'"
fi

# Attach to the tmux session
exec tmux -f /etc/tmux.conf attach-session -t "$SESSION_NAME"
