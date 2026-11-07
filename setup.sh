#!/bin/bash
# CameraNAS One-Command Setup Script
# Works on: Termux, Linux, Windows (Git Bash)
# Usage: bash setup.sh
# Everything goes inside DCIM/CameraNAS/ → Google Photos auto-backup covers it all

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PLATFORM="linux"
if [ -f "/system/bin/termux-setup-storage" ]; then
    PLATFORM="termux"
fi

echo "=============================================="
echo "  CameraNAS One-Command Setup"
echo "  Platform: $PLATFORM"
echo "  Google Photos: DCIM/ auto-backup"
echo "=============================================="

# Load .env
ENV_FILE="$SCRIPT_DIR/camera_recorder/system/config/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
    echo "[OK] .env loaded"
fi

# STEP 1: Create folder structure
echo "[1/12] Creating directory structure..."
if [ "$PLATFORM" = "termux" ]; then
    CAM_ROOT="$HOME/storage/shared/DCIM/CameraNAS"
    NAS_SHARE="$HOME/storage/shared/DCIM/CameraNAS"
else
    CAM_ROOT="/CameraNAS/DCIM/CameraNAS"
    NAS_SHARE="/CameraNAS/DCIM/CameraNAS"
fi
mkdir -p "$CAM_ROOT/CAM_001"
mkdir -p "$CAM_ROOT/CAM_002"
mkdir -p "$CAM_ROOT/system/database"
mkdir -p "$CAM_ROOT/system/logs"
mkdir -p "$CAM_ROOT/system/config"
mkdir -p "$CAM_ROOT"
echo "  [OK] All inside DCIM/CameraNAS/ - same for NAS and recordings"

# STEP 2: Copy config files (NOTE: .env is NEVER copied - single .env at camera_recorder/system/config/.env)
echo "[2/12] Copying config files..."
mkdir -p "$CAM_ROOT/system/config"
[ -f "$SCRIPT_DIR/camera_recorder/config/config.json" ] && cp "$SCRIPT_DIR/camera_recorder/config/config.json" "$CAM_ROOT/system/config/config.json"
echo "  [OK] Config copied (.env stays in code folder only)"

# STEP 3: Python deps
echo "[3/12] Installing Python dependencies..."
PYTHON_CMD=$(command -v python3 || command -v python)
if [ "$PLATFORM" = "termux" ]; then
    # lxml build needs libxml2/libxslt on Termux
    pkg install -y libxml2 libxslt 2>/dev/null || true
fi
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    $PYTHON_CMD -m pip install -q -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null || true
else
    $PYTHON_CMD -m pip install python-pptx 2>/dev/null || true
fi
echo "  [OK] Dependencies installed"

# STEP 4: FFmpeg
echo "[4/12] Installing FFmpeg..."
if [ "$PLATFORM" = "termux" ]; then
    pkg install -y ffmpeg 2>/dev/null && FFMPEG_INSTALLED=true || FFMPEG_INSTALLED=false
elif [ "$PLATFORM" = "linux" ]; then
    sudo apt-get install -y ffmpeg 2>/dev/null && FFMPEG_INSTALLED=true || apt-get install -y ffmpeg 2>/dev/null && FFMPEG_INSTALLED=true
fi
if [ "$FFMPEG_INSTALLED" = "true" ]; then
    echo "  [OK] FFmpeg installed"
else
    echo "  [WARN] FFmpeg install failed"
fi

# STEP 5: Samba/NAS
echo "[5/12] Setting up Samba/NAS..."
if [ "$PLATFORM" = "termux" ]; then
    pkg install -y samba 2>/dev/null || true
elif [ "$PLATFORM" = "linux" ]; then
    sudo apt-get install -y samba samba-common-bin 2>/dev/null || apt-get install -y samba 2>/dev/null || true
fi
echo "  [OK] Samba configured"

# STEP 5.5: Install pysmb for laptop
echo "[5.5/12] Installing pysmb for laptop sharing..."
$PYTHON_CMD -m pip install -q pysmb 2>/dev/null || true
echo "  [OK] pysmb installed"

# STEP 5.6: Install requirements
echo "[5.6/12] Installing requirements..."
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    $PYTHON_CMD -m pip install -q -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null || true
fi
echo "  [OK] Requirements installed"

# STEP 6: Create Samba user
echo "[6/12] Creating Samba user..."
if [ -f "$ENV_FILE" ]; then
    NAS_USER=$(grep "^NAS_SMB_USER=" "$ENV_FILE" | cut -d'=' -f2)
    NAS_PASS=$(grep "^NAS_SMB_PASS=" "$ENV_FILE" | cut -d'=' -f2)
    if [ -n "$NAS_USER" ] && [ -n "$NAS_PASS" ]; then
        if [ "$PLATFORM" = "termux" ]; then
            useradd -s /bin/false "$NAS_USER" 2>/dev/null || true
            echo "$NAS_PASS" | smbpasswd -a -s "$NAS_USER" 2>/dev/null || true
        elif [ "$PLATFORM" = "linux" ]; then
            sudo useradd -s /bin/false "$NAS_USER" 2>/dev/null || true
            echo "$NAS_PASS" | sudo smbpasswd -a -s "$NAS_USER" 2>/dev/null || true
        fi
        echo "  [OK] Samba user '$NAS_USER' created"
    fi
fi

# STEP 6: Database
echo "[6/12] Initializing database..."
DB_PATH="$CAM_ROOT/system/database/camera.db"
if [ ! -f "$DB_PATH" ]; then
    $PYTHON_CMD -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
conn.executescript('''
CREATE TABLE IF NOT EXISTS cameras (camera_id TEXT PRIMARY KEY, name TEXT, camera_type TEXT, protocol TEXT, storage_path TEXT, priority TEXT, enabled INTEGER DEFAULT 1, rtsp_url TEXT);
CREATE TABLE IF NOT EXISTS recordings (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT, file_path TEXT, status TEXT DEFAULT RECORDING, backup_status TEXT DEFAULT PENDING, local_delete_allowed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS backup_log (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT, action TEXT, status TEXT, details TEXT, timestamp TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS storage_state (id INTEGER PRIMARY KEY AUTOINCREMENT, total_gb REAL, free_gb REAL, status TEXT);
''')
conn.commit()
conn.close()
print('  [OK] Database initialized')
" 2>/dev/null || echo "  [WARN] DB init skipped"
fi

# STEP 7: Logs
echo "[7/12] Creating log files..."
for log in cpplus.log mi_nas.log google_backup.log storage.log; do
    touch "$CAM_ROOT/system/logs/$log" 2>/dev/null || true
done
echo "  [OK] Log files created"

# STEP 8: FFmpeg verify
echo "[8/12] Verifying FFmpeg..."
if command -v ffmpeg &>/dev/null; then
    echo "  [OK] FFmpeg ready"
else
    echo "  [FAIL] FFmpeg not available"
fi

# STEP 9: Verify .env
echo "[9/12] Checking .env..."
if [ -f "$ENV_FILE" ]; then
    echo "  [OK] .env found (single file): $ENV_FILE"
fi

# STEP 10: Storage detection
echo "[10/12] Detecting storage..."
if command -v df &>/dev/null; then
    TOTAL_GB=$(df -BG "$CAM_ROOT" 2>/dev/null | awk 'NR==2{print $2}' | tr -d 'G')
    if [ -n "$TOTAL_GB" ]; then
        LOW=$(echo "$TOTAL_GB * 0.25" | bc 2>/dev/null || echo "$((TOTAL_GB / 4))")
        CRIT=$(echo "$TOTAL_GB * 0.15" | bc 2>/dev/null || echo "$((TOTAL_GB * 15 / 100))")
        echo "  [OK] Storage: ${TOTAL_GB}GB | Low: ${LOW}GB | Critical: ${CRIT}GB"
    fi
fi

# STEP 11: Done
echo "[11/12] Done!"
echo ""
echo "=============================================="
echo "  Setup Complete!"
echo "  Root: $CAM_ROOT"
echo ""
echo "  IMPORTANT: Turn on Google Photos backup"
echo "  for DCIM folder on your phone (ONE TIME)."
echo "  After that: Everything auto-backups!"
echo ""
echo "  NAS Access from phone:"
echo "    - Laptop IP: $(hostname -I 2>/dev/null | awk '{print $1}')"
echo "    - Share: smb://<laptop-ip>/DCIM/CameraNAS"
echo "    - Or: http://<laptop-ip>:8080/"
echo ""
echo "  Start system: bash run.sh"
echo "  Or Python:    python app.py"
echo "=============================================="