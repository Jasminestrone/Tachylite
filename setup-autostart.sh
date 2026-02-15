#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER="$SCRIPT_DIR/server.py"
SERVICE_NAME="tachylite"

find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            PYTHON="$(command -v "$cmd")"
            return
        fi
    done
    echo "Error: python3 not found."
    exit 1
}

detect_os() {
    case "$(uname -s)" in
        Linux*)
            if grep -qiE "microsoft|wsl" /proc/version 2>/dev/null; then OS="wsl"
            elif command -v systemctl &>/dev/null && systemctl --user show-environment &>/dev/null 2>&1; then OS="linux-systemd"
            else OS="linux-cron"; fi ;;
        Darwin*)          OS="macos" ;;
        CYGWIN*|MINGW*|MSYS*) OS="windows" ;;
        *)                OS="unknown" ;;
    esac
}

install_systemd() {
    local svc_dir="$HOME/.config/systemd/user"
    local svc_file="$svc_dir/$SERVICE_NAME.service"
    mkdir -p "$svc_dir"

    local mount_root="" mount_unit=""
    local mnt
    mnt=$(df --output=target "$SCRIPT_DIR" 2>/dev/null | tail -1)
    if [[ "$mnt" != "/" && "$mnt" != "/home" && -n "$mnt" ]]; then
        mount_root="$mnt"
        mount_unit=$(systemd-escape --path "$mnt").mount
    fi

    cat > "$svc_file" <<EOF
[Unit]
Description=Tachylite Live (Obsidian Vault Viewer)
${mount_unit:+After=$mount_unit}
${mount_root:+RequiresMountsFor=$mount_root}

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=$PYTHON $SERVER
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

    sed -i '/^$/{ N; /^\n$/d; }' "$svc_file"
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME.service"
    echo "Installed: $svc_file"
}

remove_systemd() {
    systemctl --user disable --now "$SERVICE_NAME.service" 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/$SERVICE_NAME.service"
    systemctl --user daemon-reload
    echo "Removed systemd service."
}

install_launchd() {
    local plist_dir="$HOME/Library/LaunchAgents"
    local plist_file="$plist_dir/com.$SERVICE_NAME.plist"
    mkdir -p "$plist_dir"

    cat > "$plist_file" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.$SERVICE_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SERVER</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/$SERVICE_NAME.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/$SERVICE_NAME.err</string>
</dict>
</plist>
EOF

    launchctl unload "$plist_file" 2>/dev/null || true
    launchctl load -w "$plist_file"
    echo "Installed: $plist_file"
}

remove_launchd() {
    local plist_file="$HOME/Library/LaunchAgents/com.$SERVICE_NAME.plist"
    launchctl unload "$plist_file" 2>/dev/null || true
    rm -f "$plist_file"
    echo "Removed launchd agent."
}

install_windows() {
    local startup_dir
    startup_dir="$(cmd.exe /C 'echo %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup' 2>/dev/null | tr -d '\r')"
    if [[ -z "$startup_dir" || ! -d "$startup_dir" ]]; then
        echo "Error: could not locate Windows Startup folder."
        exit 1
    fi

    local vbs_file="$startup_dir\\$SERVICE_NAME.vbs"
    cat > "$vbs_file" <<EOF
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """$(cygpath -w "$PYTHON")"" ""$(cygpath -w "$SERVER")""", 0, False
EOF
    echo "Installed: $vbs_file"
}

remove_windows() {
    local startup_dir
    startup_dir="$(cmd.exe /C 'echo %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup' 2>/dev/null | tr -d '\r')"
    rm -f "$startup_dir\\$SERVICE_NAME.vbs" 2>/dev/null || true
    echo "Removed Windows startup script."
}

install_wsl() {
    local task_name="Tachylite"
    local distro
    distro=$(wslpath -m / 2>/dev/null | sed 's|^//wsl.localhost/||; s|/.*||' || echo Ubuntu)

    local helper="$SCRIPT_DIR/.tachylite-wsl-start.bat"
    cat > "$helper" <<EOF
@echo off
wsl -d $distro -- bash -c "cd '$SCRIPT_DIR' && nohup $PYTHON '$SERVER' > /tmp/$SERVICE_NAME.log 2>&1 &"
EOF

    powershell.exe -Command "
        \$action = New-ScheduledTaskAction -Execute '$(wslpath -w "$helper")'
        \$trigger = New-ScheduledTaskTrigger -AtLogOn
        \$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName '$task_name' -Action \$action -Trigger \$trigger -Settings \$settings -Force
    " 2>/dev/null
    echo "Installed Windows scheduled task: $task_name"
}

remove_wsl() {
    powershell.exe -Command "Unregister-ScheduledTask -TaskName 'Tachylite' -Confirm:\$false" 2>/dev/null || true
    rm -f "$SCRIPT_DIR/.tachylite-wsl-start.bat" 2>/dev/null || true
    echo "Removed WSL scheduled task."
}

install_cron() {
    local job="@reboot cd $SCRIPT_DIR && $PYTHON $SERVER >> /tmp/$SERVICE_NAME.log 2>&1"
    (crontab -l 2>/dev/null | grep -v "$SERVER"; echo "$job") | crontab -
    echo "Installed cron @reboot job."
}

remove_cron() {
    crontab -l 2>/dev/null | grep -v "$SERVER" | crontab - 2>/dev/null || true
    echo "Removed cron job."
}

find_python
detect_os
ACTION="${1:-install}"

if [[ "$ACTION" == "--remove" || "$ACTION" == "remove" || "$ACTION" == "uninstall" ]]; then
    echo "Removing Tachylite autostart ($OS)..."
    case "$OS" in
        linux-systemd) remove_systemd ;;
        macos)         remove_launchd ;;
        windows)       remove_windows ;;
        wsl)           remove_wsl ;;
        *)             remove_cron ;;
    esac
else
    [[ ! -f "$SERVER" ]] && { echo "Error: server.py not found at $SERVER"; exit 1; }
    echo "OS: $OS | Python: $PYTHON"
    case "$OS" in
        linux-systemd) install_systemd ;;
        macos)         install_launchd ;;
        windows)       install_windows ;;
        wsl)           install_wsl ;;
        linux-cron)    install_cron ;;
        *)             echo "Unknown OS, falling back to cron."; install_cron ;;
    esac
    echo "Tachylite will auto-start on boot at http://localhost:8000"
fi
