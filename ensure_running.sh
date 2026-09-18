#!/bin/bash
# Idempotent runner for Agent Live Voice Copilot
# Checks if live_sidecar.py is already running; if not, starts it.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if pgrep -f "python.*live_sidecar.py" > /dev/null 2>&1; then
    # Already active
    exit 0
fi

if [ -t 1 ]; then
    cd "$DIR" && ./start.sh
else
    # Automatically detect running IDE (Cursor -> Antigravity IDE -> VS Code) and open internal terminal
    osascript << EOF
set ideName to ""
tell application "System Events"
    if exists (process "Cursor") then
        set ideName to "Cursor"
    else if exists (process "Antigravity IDE") then
        set ideName to "Antigravity IDE"
    else if exists (process "Code") then
        set ideName to "Code"
    end if
end tell

if ideName is not "" then
    tell application ideName to activate
    tell application "System Events"
        tell process ideName
            click menu item "New Terminal" of menu 1 of menu bar item "Terminal" of menu bar 1
            delay 0.5
            keystroke "cd \"$DIR\" && ./start.sh"
            key code 36
        end tell
    end tell
else
    tell application "Terminal" to activate
    tell application "Terminal" to do script "cd \"$DIR\" && ./start.sh"
end if
EOF
fi
