import os
import platform
import shutil
from camera_recorder.config.config import get_google_photos_root, is_termux

def detect_storage():
    result = {}
    system = platform.system()
    total_gb = 64
    free_gb = 64
    try:
        if hasattr(os, 'statvfs'):
            dcim_path = get_google_photos_root()
            if os.path.exists(dcim_path):
                stat = os.statvfs(dcim_path)
                total_gb = round((stat.f_blocks * stat.f_frsize) / (1024**3), 2)
                free_gb = round((stat.f_bavail * stat.f_frsize) / (1024**3), 2)
            else:
                stat = os.statvfs("/")
                total_gb = round((stat.f_blocks * stat.f_frsize) / (1024**3), 2)
                free_gb = round((stat.f_bavail * stat.f_frsize) / (1024**3), 2)
        elif system == "Windows":
            total, used, free = shutil.disk_usage("/")
            total_gb = round(total / (1024**3), 2)
            free_gb = round(free / (1024**3), 2)
    except Exception:
        pass

    if is_termux():
        result["device_type"] = "termux"
    elif system == "Linux" and platform.uname().machine in ("aarch64", "armv7l"):
        result["device_type"] = "android"
    elif system == "Linux":
        result["device_type"] = "linux"
    else:
        result["device_type"] = system.lower()

    result["total_gb"] = total_gb
    result["free_gb"] = free_gb
    return result

def get_thresholds(total_gb=None):
    if total_gb is None:
        storage = detect_storage()
        total_gb = storage.get("total_gb", 64)
    low_pct = 0.25
    critical_pct = 0.15
    cleanup_pct = 0.20
    return {
        "total_gb": total_gb,
        "low_threshold_gb": round(total_gb * low_pct, 2),
        "critical_threshold_gb": round(total_gb * critical_pct, 2),
        "cleanup_threshold_gb": round(total_gb * cleanup_pct, 2),
        "low_threshold_pct": int(low_pct * 100),
        "critical_threshold_pct": int(critical_pct * 100),
        "cleanup_threshold_pct": int(cleanup_pct * 100),
    }

def get_storage_status():
    storage = detect_storage()
    thresholds = get_thresholds(storage.get("total_gb"))
    free_gb = storage.get("free_gb", 0)
    total_gb = storage.get("total_gb", 64)
    thresholds["free_gb"] = free_gb
    thresholds["total_gb"] = total_gb
    thresholds["used_gb"] = round(total_gb - free_gb, 2)
    if free_gb < thresholds["critical_threshold_gb"]:
        thresholds["status"] = "CRITICAL"
    elif free_gb < thresholds["low_threshold_gb"]:
        thresholds["status"] = "LOW"
    else:
        thresholds["status"] = "NORMAL"
    return thresholds

def estimate_max_files(total_gb=None, avg_file_mb=500):
    if total_gb is None:
        total_gb = detect_storage().get("total_gb", 64)
    return int((total_gb * 1024) / avg_file_mb)