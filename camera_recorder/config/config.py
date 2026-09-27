import os
import json
import platform
import sys
from pathlib import Path

def is_termux():
    """Detect Termux using multiple methods"""
    # Method 1: Check for TERMUX env variable (most reliable)
    if 'TERMUX' in os.environ or 'ANDROID_ARGUMENT' in os.environ:
        return True
    # Method 2: Check for termux-setup-storage (Termux specific)
    if os.path.exists("/system/bin/termux-setup-storage"):
        return True
    # Method 3: Check if running from termux prefix
    prefix = os.environ.get('PREFIX', '')
    if 'com.termux' in prefix or '/data/data/com.termux' in prefix:
        return True
    # Method 4: Check if python is in termux path
    python_path = sys.executable.lower()
    if 'com.termux' in python_path or 'termux' in python_path:
        return True
    # Method 5: Check platform - must be Linux AND have android in uname
    system = platform.system().lower()
    if system == "linux":
        try:
            import subprocess
            result = subprocess.run(["uname", "-a"], capture_output=True, text=True, timeout=2)
            if "android" in result.stdout.lower():
                return True
        except Exception:
            pass
    return False

def get_storage_root():
    """Get the correct storage root based on platform"""
    if is_termux():
        # Termux shared storage
        termux_shared = os.path.expanduser("~/storage/shared")
        if os.path.exists(termux_shared):
            return termux_shared
        # Fallback: /data/data/com.termux/files/home/storage/shared
        return os.path.expanduser("~/storage/shared")
    # Linux/Windows
    return None

def get_google_photos_root():
    """DCIM/CameraNAS/ - Google Photos auto-backup covers EVERYTHING inside DCIM/"""
    if is_termux():
        storage = get_storage_root()
        if storage:
            return os.path.join(storage, "DCIM", "CameraNAS")
    # Linux
    if os.path.exists("/system/bin/termux-setup-storage"):
        return os.path.join(os.path.expanduser("~/storage/shared"), "DCIM", "CameraNAS")
    # Default Linux path
    dcim_path = "/CameraNAS/DCIM/CameraNAS"
    Path(dcim_path).mkdir(parents=True, exist_ok=True)
    return dcim_path

def get_camera_storage_path(camera_id):
    """Get full path for camera inside DCIM/CameraNAS/"""
    return os.path.join(get_google_photos_root(), camera_id)

def resolve_path(path):
    """config.json stores paths relative to the shared storage root
    (e.g. DCIM/CameraNAS/cpplus_videos). Resolve to ABSOLUTE against the
    storage root so process CWD (HTTP chdir, launch dir) can never nest
    them wrongly (the share/DCIM/CameraNAS/DCIM/CameraNAS bug)."""
    if not path:
        return get_google_photos_root()
    path = os.path.expanduser(str(path))
    if os.path.isabs(path):
        return path
    base = get_storage_root()
    if not base:
        base = os.path.expanduser("~")
    return os.path.normpath(os.path.join(base, path))

def get_camera_nas_root():
    """Get the NAS root directory"""
    return get_google_photos_root()

def get_config_path():
    """SINGLE config.json path - same for Termux, Linux, Windows (always next to code)"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "config.json")

def load_config(config_path=None):
    if config_path is None:
        config_path = get_config_path()
    if config_path and os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)
    return {"camera_registry": {}, "storage": {}, "backup": {}}

DEFAULT_ENV = """# ===== CP Plus RTSP Camera Credentials =====
RTSP_URL=rtsp://admin:password@192.168.1.29:5543/09b030479dc266aaa457a13b6034cd73/live/channel0
CP_RTSP_USER=admin
CP_RTSP_PASSWORD=password
CP_RTSP_PORT=5543
CP_RTSP_CHANNEL=09b030479dc266aaa457a13b6034cd73/live/channel0
CP_SEGMENT_DURATION=60
CP_LOG_LEVEL=INFO

# ===== NAS / Samba Server Credentials =====
NAS_SMB_USER=nasuser
NAS_SMB_PASS=naspass123
NAS_SMB_PASS_NEW=
NAS_SMB_SHARE=CameraNAS
NAS_SMB_WORKGROUP=WORKGROUP
NAS_SMB_IP=AUTO_DETECT
# NAS share path: phone sees /storage/emulated/0/DCIM/CameraNAS
# Termux shares ~/storage/shared/DCIM/CameraNAS
NAS_SMB_TERMUX_PATH=~/storage/shared/DCIM/CameraNAS
NAS_SMB_PHONE_PATH=/storage/emulated/0/DCIM/CameraNAS
NAS_SMB_SMB_URL=smb://<phone-ip>/DCIM/CameraNAS

# ===== Google Photos Auto-Backup =====
GOOGLE_PHOTOS_BACKUP_FOLDER=DCIM/CameraNAS

# ===== System Settings =====
CAMERA_COUNT_LIMIT=99
STORAGE_LOW_THRESHOLD_PCT=25
STORAGE_CRITICAL_THRESHOLD_PCT=15
STORAGE_CLEANUP_THRESHOLD_PCT=20
MIN_RETENTION_HOURS=24
BACKUP_MAX_RETRIES=5
BACKUP_RETRY_DELAY_SECONDS=300

# ===== FFmpeg Installation Commands =====
# Termux: pkg install ffmpeg
# Linux:  sudo apt-get install ffmpeg
# Windows: winget install ffmpeg
"""

def get_env_path():
    """SINGLE .env path - same for Termux, Linux, Windows (always next to code)"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "system", "config", ".env")

def create_default_env():
    """Create the single .env file with default values if it doesn't exist"""
    path = get_env_path()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(DEFAULT_ENV)
        return path
    return None

def load_env(env_path=None):
    """Load the SINGLE .env file into os.environ. File values always win."""
    if env_path is None:
        env_path = get_env_path()

    # Create if missing
    if not os.path.exists(env_path):
        os.makedirs(os.path.dirname(env_path), exist_ok=True)
        with open(env_path, "w") as f:
            f.write(DEFAULT_ENV)

    # Load .env into os.environ
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()
    return True

def get_rtsp_credentials():
    load_env()
    rtsp_url = os.environ.get("CP_RTSP_URL", "") or os.environ.get("RTSP_URL", "")
    if rtsp_url:
        return rtsp_url
    cfg = load_config()
    cams = cfg.get("camera_registry", {})
    for cam_id, cam_info in cams.items():
        if cam_info["type"] == "ip_camera":
            url = cam_info.get("rtsp_url", "")
            if url and url != "rtsp://admin:password@CAMERA_IP:554/stream":
                return url
    return "rtsp://admin:password@192.168.1.29:5543/09b030479dc266aaa457a13b6034cd73/live/channel0"

def detect_environment():
    if is_termux():
        return "termux"
    system = platform.system()
    return system.lower()

def detect_storage():
    system = platform.system()
    total_gb = 64
    free_gb = 64
    try:
        if hasattr(os, 'statvfs'):
            path = get_google_photos_root()
            if os.path.exists(path):
                stat = os.statvfs(path)
                total_gb = round((stat.f_blocks * stat.f_frsize) / (1024**3), 2)
                free_gb = round((stat.f_bavail * stat.f_frsize) / (1024**3), 2)
        elif system == "Windows":
            import shutil
            total, used, free = shutil.disk_usage("/")
            total_gb = round(total / (1024**3), 2)
            free_gb = round(free / (1024**3), 2)
    except Exception:
        pass
    return {"total_gb": total_gb, "free_gb": free_gb}
