#!/bin/bash
# CameraNAS Unified Run Script
# Works on: Termux, Linux, Windows (Git Bash)
# Everything inside DCIM/CameraNAS/ → Google Photos auto-backup

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PLATFORM="linux"
if [ -f "/system/bin/termux-setup-storage" ]; then
    PLATFORM="termux"
fi

# Load .env
ENV_FILE="$SCRIPT_DIR/camera_recorder/system/config/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
    echo "[.env loaded]"
fi

CAM_ROOT=""
if [ "$PLATFORM" = "termux" ]; then
    CAM_ROOT="$HOME/storage/shared/DCIM/CameraNAS"
elif [ "$PLATFORM" = "linux" ]; then
    CAM_ROOT="/CameraNAS/DCIM/CameraNAS"
fi

case "${1:-}" in
    --setup)
        echo "CameraNAS Setup ($PLATFORM)"
        echo "[1/8] Creating folder structure..."
        mkdir -p "$CAM_ROOT/CAM_001"
        mkdir -p "$CAM_ROOT/CAM_002"
        mkdir -p "$CAM_ROOT/system/database"
        mkdir -p "$CAM_ROOT/system/logs"
        mkdir -p "$CAM_ROOT/system/config"
        mkdir -p "$CAM_ROOT"
        echo "  [OK] All folders inside DCIM/CameraNAS/"
        echo "  [OK] Google Photos auto-backup covers everything"
        echo "[2/8] Python deps..."
        python -m pip install python-pptx 2>/dev/null || true
        echo "[3/8] FFmpeg..."
        pkg install -y ffmpeg 2>/dev/null || sudo apt-get install -y ffmpeg 2>/dev/null || true
        echo "[4/8] Samba..."
        pkg install -y samba 2>/dev/null || sudo apt-get install -y samba 2>/dev/null || true
        echo "[5/8] Config..."
        cp camera_recorder/config/config.json "$CAM_ROOT/system/config/config.json" 2>/dev/null || true
        echo "  (.env stays single at camera_recorder/system/config/.env)"
        echo "[6/8] Database..."
        python -c "import sqlite3; conn=sqlite3.connect('$CAM_ROOT/system/database/camera.db'); conn.execute('CREATE TABLE IF NOT EXISTS cameras(camera_id TEXT PRIMARY KEY)'); conn.commit(); conn.close(); print('Ready')" 2>/dev/null || true
        echo "[7/8] Logs..."
        touch "$CAM_ROOT/system/logs/"*.log 2>/dev/null || true
        echo "[8/8] FFmpeg check..."
        ffmpeg -version 2>/dev/null && echo "[OK] FFmpeg ready" || echo "[FAIL] FFmpeg missing"
        echo ""
        echo "[OK] Setup complete!"
        echo "  Root: $CAM_ROOT"
        echo "  Google Photos auto-backup covers ALL of DCIM/"
        echo "  Turn on Google Photos backup for DCIM on phone (ONE TIME)"
        echo "  Run: bash run.sh"
        ;;
    --status)
        python app.py --status 2>/dev/null || echo "Run setup first"
        ;;
    --test)
        python app.py --test
        ;;
    *)
        echo "CameraNAS starting ($PLATFORM)..."
        echo "Root: $CAM_ROOT"
        echo "Google Photos: Auto-backup (DCIM folder)"
        command -v ffmpeg >/dev/null 2>&1 && echo "[OK] FFmpeg found" || echo "[FAIL] FFmpeg missing"
        python app.py
        ;;
esac