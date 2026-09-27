import os
import sys
import json
import shutil
import platform
import subprocess
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from camera_recorder.config.config import is_termux, get_google_photos_root, create_default_env, load_env, get_env_path

def detect_platform():
    if is_termux():
        return "termux"
    return platform.system().lower()

def get_root():
    if is_termux():
        return get_google_photos_root()
    return "/CameraNAS/DCIM/CameraNAS"

def quick_setup():
    print("=" * 50)
    print("  CameraNAS Quick Setup")
    print("=" * 50)
    plat = detect_platform()
    root = get_root()
    print(f"  Platform: {plat}")
    print(f"  Root:    {root}")
    print()

    dirs = [root, os.path.join(root, "xiaomi_camera_videos"), os.path.join(root, "cpplus_videos"),
            os.path.join(root, "system", "database"), os.path.join(root, "system", "logs"),
            os.path.join(root, "system", "config")]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    # NAS share = same as recording directory (all cameras here)
    print("[OK] Directories created")
    print(f"  NAS + Recording: {root}/")
    print(f"  Phone access: /storage/emulated/0/DCIM/CameraNAS/")
    # Create .env with defaults if missing (SINGLE .env - never copied to DCIM)
    created = create_default_env()
    if created:
        print(f"[OK] .env created at: {created}")
    else:
        load_env()
        print(f"[OK] .env loaded from: {get_env_path()}")

    if plat == "termux":
        # lxml (python-pptx dependency) needs these to build on Termux
        try:
            subprocess.run(["pkg", "install", "-y", "libxml2", "libxslt"], check=True, timeout=120, capture_output=True)
        except Exception:
            pass

    print("[*] Installing Python dependencies...")
    req = os.path.join(SCRIPT_DIR, "requirements.txt")
    try:
        if os.path.exists(req):
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req],
                           check=True, timeout=120, capture_output=True)
        else:
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "python-pptx", "pysmb"],
                           check=True, timeout=120, capture_output=True)
    except Exception:
        pass
    print("[OK] Dependencies installed")

    if plat != "windows":
        print("[*] Installing FFmpeg...")
        try:
            if plat == "termux":
                subprocess.run(["pkg", "install", "-y", "ffmpeg"], check=True, timeout=60, capture_output=True)
            else:
                subprocess.run(["sudo", "apt-get", "install", "-y", "ffmpeg"], check=True, timeout=60, capture_output=True)
        except Exception:
            pass
        print("[OK] FFmpeg installed")

        print("[*] Setting up Samba/NAS...")
        try:
            if plat == "termux":
                subprocess.run(["pkg", "install", "-y", "samba"], check=True, timeout=60, capture_output=True)
            else:
                subprocess.run(["sudo", "apt-get", "install", "-y", "samba"], check=True, timeout=60, capture_output=True)
        except Exception:
            pass
        print("[OK] Samba configured")
    else:
        print("[OK] Windows: SMB share created automatically at runtime (needs Admin)")

    db_path = os.path.join(root, "system", "database", "camera.db")
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.executescript('''CREATE TABLE IF NOT EXISTS cameras (camera_id TEXT PRIMARY KEY, name TEXT, camera_type TEXT, protocol TEXT, storage_path TEXT, priority TEXT, enabled INTEGER DEFAULT 1, rtsp_url TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS recordings (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id TEXT, file_path TEXT, file_size_mb REAL, duration_seconds INTEGER, status TEXT DEFAULT RECORDING, backup_status TEXT DEFAULT PENDING, local_delete_allowed INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS storage_state (id INTEGER PRIMARY KEY AUTOINCREMENT, total_gb REAL, free_gb REAL, used_gb REAL, status TEXT, low_threshold_gb REAL, critical_threshold_gb REAL, timestamp TEXT DEFAULT CURRENT_TIMESTAMP);''')
    conn.commit()
    conn.close()
    print("[OK] Database initialized")

    for log_name in ["cpplus.log", "mi_nas.log", "google_backup.log", "storage.log"]:
        Path(os.path.join(root, "system", "logs", log_name)).touch()
    print("[OK] Log files created")

    try:
        if hasattr(os, 'statvfs'):
            stat = os.statvfs(root)
            total = round((stat.f_blocks * stat.f_frsize) / (1024**3), 2)
            free = round((stat.f_bavail * stat.f_frsize) / (1024**3), 2)
            print(f"[OK] Storage: {total}GB total, {free}GB free")
            print(f"[OK] Dynamic thresholds: Low={round(total*0.25,1)}GB | Critical={round(total*0.15,1)}GB")
    except Exception:
        pass

    print()
    print("=" * 50)
    print("  Setup Complete!")
    print(f"  Run: python {os.path.basename(__file__)}")
    print(f"  .env: {get_env_path()}")
    print(f"  NAS + Recording: {root}")
    print(f"  Phone path: /storage/emulated/0/DCIM/CameraNAS")
    load_env()
    print(f"  Samba user: {os.environ.get('NAS_SMB_USER', 'nasuser')}")
    try:
        from camera_recorder.nas.nas_manager import NASManager
        _m = NASManager()
        _ip = _m.get_samba_ip()
        _smb_url = _m.get_share_url().replace("//", "smb://")
    except Exception:
        _ip = "127.0.0.1"
        _smb_url = "smb://127.0.0.1/CameraNAS"
    print(f"  Device IP: {_ip}")
    print(f"  Mi app:     {_smb_url}")
    print(f"  Browser:    http://{_ip}:8080/")
    print("=" * 50)

def print_env_diagnostic():
    """Show EXACTLY which .env is used and what values are loaded"""
    path = get_env_path()
    exists = os.path.exists(path)
    print("=" * 50)
    print("  ENV DIAGNOSTIC")
    print(f"  .env path: {path}")
    if exists:
        import time
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
        print(f"  Status: EXISTS (last modified: {mtime})")
    else:
        print("  Status: MISSING -> auto-created with DEFAULT values!")
        print("  >>> EDIT THIS FILE NOW with your real values")
    load_env()
    user = os.environ.get("NAS_SMB_USER", "?")
    rtsp = os.environ.get("RTSP_URL", "?")
    rtsp_masked = rtsp.split("@")[-1] if "@" in rtsp else rtsp
    print(f"  NAS_SMB_USER: {user}")
    print(f"  RTSP_URL: rtsp://***@{rtsp_masked}")
    if user in ("nasuser", "?"):
        print("  [WARN] DEFAULT values detected! .env not edited yet")
    else:
        print("  [OK] Values loaded from .env file")
    print("=" * 50)

def main():
    try:
        create_default_env()
        load_env()
    except Exception:
        pass
    if len(sys.argv) > 1 and sys.argv[1] == "--env":
        print_env_diagnostic()
        return
    # Print ENV diagnostic on every normal startup
    if len(sys.argv) == 1:
        print_env_diagnostic()
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        quick_setup()
    elif len(sys.argv) > 1 and sys.argv[1] == "--nas-status":
        from camera_recorder.nas.nas_manager import NASManager
        nm = NASManager()
        nm.print_nas_access()
    elif len(sys.argv) > 1 and sys.argv[1] == "--status":
        from camera_recorder.utils.storage_check import get_storage_status
        import json
        print(json.dumps(get_storage_status(), indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "--test":
        import py_compile
        print("[*] Running syntax check...")
        errors = 0
        for root, dirs, files in os.walk(SCRIPT_DIR):
            for f in files:
                if f.endswith(".py"):
                    fp = os.path.join(root, f)
                    try:
                        py_compile.compile(fp, doraise=True)
                        print(f"  OK: {fp}")
                    except Exception as e:
                        errors += 1
                        print(f"  FAIL: {fp} -> {e}")
        print(f"\n  {'All OK!' if errors == 0 else f'{errors} errors found'}")
    else:
        print("CameraNAS System")
        print("Usage:")
        print("  python app.py           - Start the system")
        print("  python app.py --setup   - One-command setup")
        print("  python app.py --status  - Check storage status")
        print("  python app.py --nas-status - Check NAS/Samba status")
        print("  python app.py --env    - Check which .env is loaded + values")
        print("  python app.py --test    - Test all Python files")
        print()
        print(f"  Platform: {detect_platform()}")
        print(f"  Root:     {get_root()}")
        print()
        print("  Google Photos: Auto-backup from DCIM/ folder")
        print("  Turn on Google Photos backup for DCIM on phone (ONE TIME)")
        print()
        print("  Quick start: python app.py --setup && python app.py")
        print()
        from camera_recorder.main import CameraNASManager
        manager = CameraNASManager()
        manager.start_all()

if __name__ == "__main__":
    main()