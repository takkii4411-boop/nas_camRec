import os
import time
import json
import subprocess
import threading
from pathlib import Path
from datetime import datetime

try:
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.database.database import CameraDatabase
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.database.database import CameraDatabase

class MiCameraRecorder:
    def __init__(self, camera_id="CAM_001", storage_path=None):
        self.camera_id = camera_id
        self.storage_path = storage_path or "/CameraNAS/Mi"
        self.db = CameraDatabase()
        self.running = False
        self.logger = LoggerManager.get_logger("MI_NAS")
        self.mi_home_api_available = False

    def check_mi_home(self):
        try:
            result = subprocess.run(
                ["mi", "--version"], capture_output=True, timeout=5
            )
            self.mi_home_api_available = result.returncode == 0
        except FileNotFoundError:
            self.mi_home_api_available = False
        return self.mi_home_api_available

    def start_nas_transfer(self):
        self.running = True
        self.logger.info(
            f"Mi camera monitor STARTED: {self.camera_id} | device records itself "
            f"directly into xiaomi_camera_videos/ (Mi Home -> NAS) | "
            f"watching for new files every 60s")
        self._watch_loop()
        return True

    def _watch_loop(self):
        """Activity watchdog — logs only state changes / problems.

        Camera ek naya .mp4 ~60s me likhta hai:
          - naya file mila          -> silent (ya RESUMED line agar gap ke baad)
          - 15 min tak koi naya file -> WARNING (rate-limited, har 15 min ek)
        """
        base = self.storage_path
        silent_since = None
        last_warn = 0.0
        while self.running:
            age = self._newest_file_age(base)
            now = time.time()
            active = age is not None and age < 900
            if active:
                if silent_since is not None:
                    self.logger.info("Mi camera recordings RESUMED — new file detected")
                    silent_since = None
            else:
                if silent_since is None:
                    silent_since = now
                if now - last_warn >= 900:
                    age_txt = "no mp4 files" if age is None else f"newest {int(age)}s old"
                    self.logger.warning(
                        f"Mi camera: NO new recording in 15 min — check camera "
                        f"power / Mi Home / NAS ({age_txt})")
                    last_warn = now
            time.sleep(60)

    def _newest_file_age(self, base):
        """Seconds since newest .mp4 anywhere under base (recursive), else None."""
        if not base or not os.path.isdir(base):
            return None
        newest = 0.0
        for dirpath, _, files in os.walk(base):
            for fn in files:
                if fn.lower().endswith(".mp4"):
                    try:
                        mt = os.path.getmtime(os.path.join(dirpath, fn))
                        if mt > newest:
                            newest = mt
                    except OSError:
                        pass
        if newest == 0.0:
            return None
        return time.time() - newest

    def _check_connection(self):
        age = self._newest_file_age(self.storage_path)
        return age is not None and age < 900

    def get_sd_card_files(self):
        files = []
        sd_path = "/storage/emulated/0/DCIM/Camera"
        if os.path.exists(sd_path):
            for f in Path(sd_path).glob("*.mp4"):
                files.append(str(f))
        return sorted(files)

    def transfer_to_nas(self, file_path):
        try:
            dest = os.path.join(self.storage_path, datetime.now().strftime("%Y-%m-%d"), "Footage")
            Path(dest).mkdir(parents=True, exist_ok=True)
            filename = os.path.basename(file_path)
            dest_path = os.path.join(dest, filename)
            if not os.path.exists(dest_path):
                os.link(file_path, dest_path)
            self.logger.info(f"Transferred {file_path} to {dest_path}")
            return dest_path
        except Exception as e:
            self.logger.error(f"Transfer failed: {e}")
            return None

    def handle_connection_lost(self):
        self.logger.warning("Mi camera connection lost")
        self.db.log_backup_event(self.camera_id, None, "CONNECTION_FAILED", "FAILED")
        time.sleep(5)
        if self._check_connection():
            self.db.log_backup_event(self.camera_id, None, "CONNECTION_OK", "SUCCESS")
            return True
        return False

    def get_status(self):
        return {
            "camera_id": self.camera_id,
            "running": self.running,
            "mi_home_available": self.mi_home_api_available,
            "storage_path": self.storage_path,
            "last_updated": datetime.now().isoformat()
        }

    def stop(self):
        self.running = False
        self.logger.info("Mi NAS transfer stopped")
