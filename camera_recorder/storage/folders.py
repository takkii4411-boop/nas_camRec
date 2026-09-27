import os
import shutil
import platform
from pathlib import Path
from datetime import datetime

try:
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.utils.storage_check import get_storage_status, get_thresholds, detect_storage
    from camera_recorder.database.database import CameraDatabase
    from camera_recorder.config.config import get_google_photos_root
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from camera_recorder.utils.logger import LoggerManager
    from utils.storage_check import get_storage_status, get_thresholds, detect_storage
    from database.database import CameraDatabase
    from config.config import get_google_photos_root

class StorageManager:
    def __init__(self):
        self.db = CameraDatabase()
        self.logger = LoggerManager.get_logger("STORAGE")
        self.storage_info = detect_storage()
        self.thresholds = get_thresholds(self.storage_info.get("total_gb"))
        self.google_photos_root = get_google_photos_root()

    def create_folder_structure(self):
        """
        ALL folders are INSIDE DCIM/CameraNAS/ so Google Photos auto-backup covers everything.

        Structure:
        DCIM/CameraNAS/
        ├── xiaomi_camera_videos/  ← Mi camera writes here itself (device -> SMB)
        │   └── <DD_MM_YYYY>/<DD_MM_YY h am|pm>/<H>.<MM>.mp4   (organizer)
        ├── cpplus_videos/         ← CP Plus ffmpeg segments (staged flat)
        │   └── <DD_MM_YYYY>/<DD_MM_YY h am|pm>/<H>.<MM>.mp4   (organizer)
        ├── system/                ← Database, logs, config
        │   ├── database/
        │   ├── logs/
        │   └── config/
        └── ...          ← Google Photos auto-backup covers ALL of DCIM/
        """
        root = self.google_photos_root
        folders = [
            root,
            os.path.join(root, "xiaomi_camera_videos"),
            os.path.join(root, "cpplus_videos"),
            os.path.join(root, "system"),
            os.path.join(root, "system", "database"),
            os.path.join(root, "system", "logs"),
            os.path.join(root, "system", "config"),
        ]

        for folder in folders:
            Path(folder).mkdir(parents=True, exist_ok=True)

        self.logger.info(f"Folder structure created at {root}")
        self.logger.info(f"ALL folders are inside DCIM/CameraNAS/ -> Google Photos auto-backup covers everything")
        return folders

    def get_folder_structure(self):
        root = self.google_photos_root
        return {
            "root": root,
            "xiaomi_camera_videos": os.path.join(root, "xiaomi_camera_videos"),
            "cpplus_videos": os.path.join(root, "cpplus_videos"),
            "system": os.path.join(root, "system"),
            "database": os.path.join(root, "system", "database"),
            "logs": os.path.join(root, "system", "logs"),
            "config": os.path.join(root, "system", "config"),
            "google_photos_note": "Everything inside DCIM/CameraNAS/ is auto-backed up by Google Photos",
        }

    def get_camera_folder(self, camera_id):
        """Get the folder for a specific camera inside DCIM/CameraNAS/"""
        root = self.google_photos_root
        # If camera already has a folder, use it
        existing = os.path.join(root, camera_id)
        if os.path.exists(existing):
            return existing
        # Create new folder for new camera
        new_folder = os.path.join(root, camera_id)
        Path(new_folder).mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Created new camera folder: {new_folder}")
        self.logger.info(f"Google Photos will auto-backup everything inside {root}/")
        return new_folder

    def organize_file(self, camera_id, source_file):
        """
        Organize recording file into camera-specific folder inside DCIM/CameraNAS/.
        Google Photos auto-backup covers everything inside DCIM/.
        """
        cam_folder = self.get_camera_folder(camera_id)
        date_str = datetime.now().strftime("%Y-%m-%d")
        hour_str = datetime.now().strftime("%H")

        dest_dir = os.path.join(cam_folder, date_str, f"{hour_str}-00_to_{hour_str}-59", "Footage")
        Path(dest_dir).mkdir(parents=True, exist_ok=True)

        filename = os.path.basename(source_file)
        dest_path = os.path.join(dest_dir, filename)

        if os.path.exists(source_file) and not os.path.exists(dest_path):
            shutil.copy2(source_file, dest_path)
            self.logger.info(f"Organized {source_file} -> {dest_path}")
            self.logger.info(f"Google Photos will auto-backup this file (inside DCIM/)")
            return dest_path
        return dest_path

    def organize_recordings(self):
        """Move finished .mp4 files into <DD_MM_YYYY>/<DD_MM_YY h am|pm>/<H>.<MM>.mp4

        Sources:
          xiaomi_camera_videos/<device_id>/<YYYYMMDDHH>/<MM>M<SS>S_<epoch>.mp4
          cpplus_videos/<flat ffmpeg segments>
        Only files idle >10 min are moved (camera/ffmpeg may still be writing).
        Already-organized files (under a DD_MM_YYYY folder) are skipped.
        """
        import re
        import time

        now = time.time()
        moved = 0
        date_re = re.compile(r"^\d{2}_\d{2}_\d{4}$")
        cam_re = re.compile(r"^_(\d{10})\.mp4$")
        cp_re = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})")
        slot_re = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})$")

        for base_name in ("xiaomi_camera_videos", "cpplus_videos"):
            base = os.path.join(self.google_photos_root, base_name)
            if not os.path.isdir(base):
                continue

            for dirpath, _, files in os.walk(base):
                rel = os.path.relpath(dirpath, base)
                parts = [] if rel == "." else rel.split(os.sep)
                if any(date_re.match(p) for p in parts):
                    continue  # already organized
                for fn in files:
                    if not fn.lower().endswith(".mp4"):
                        continue
                    src = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(src)
                    except OSError:
                        continue
                    if now - st.st_mtime < 600:
                        continue  # still being written

                    dt = None
                    m = cam_re.search(fn)
                    if m:
                        dt = datetime.fromtimestamp(int(m.group(1)))
                    if dt is None:
                        m = cp_re.match(fn)
                        if m:
                            dt = datetime(int(m.group(1)), int(m.group(2)),
                                          int(m.group(3)), int(m.group(4)))
                    if dt is None:
                        m = slot_re.match(os.path.basename(dirpath))
                        if m:
                            dt = datetime(int(m.group(1)), int(m.group(2)),
                                          int(m.group(3)), int(m.group(4)))
                    if dt is None:
                        dt = datetime.fromtimestamp(st.st_mtime)

                    hour12 = dt.hour % 12 or 12
                    ampm = "am" if dt.hour < 12 else "pm"
                    date_dir = dt.strftime("%d_%m_%Y")
                    hour_dir = f"{dt.strftime('%d_%m_%y')} {hour12}{ampm}"
                    dest_dir = os.path.join(base, date_dir, hour_dir)
                    Path(dest_dir).mkdir(parents=True, exist_ok=True)

                    name = f"{hour12}.{dt.strftime('%M')}.mp4"
                    dest = os.path.join(dest_dir, name)
                    if os.path.exists(dest):
                        stem = name[:-4]
                        i = 1
                        while os.path.exists(dest):
                            dest = os.path.join(dest_dir, f"{stem}_{i}.mp4")
                            i += 1
                    try:
                        shutil.move(src, dest)
                        moved += 1
                        self.logger.info(
                            f"Organized: {base_name}/{rel}/{fn} -> {date_dir}/{hour_dir}/{os.path.basename(dest)}")
                    except Exception as e:
                        self.logger.warning(f"Organize move failed {src}: {e}")

            # prune empty device/hour dirs left behind
            for dirpath, dirnames, filenames in os.walk(base, topdown=False):
                if dirpath == base:
                    continue
                if not dirnames and not filenames:
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        pass
        return moved

    def check_and_cleanup(self):
        status = get_storage_status()
        self.db.save_storage_state(
            total_gb=status["total_gb"],
            free_gb=status["free_gb"],
            used_gb=status["used_gb"],
            status=status["status"],
            low_threshold=status["low_threshold_gb"],
            critical_threshold=status["critical_threshold_gb"]
        )

        if status["status"] == "CRITICAL":
            self.logger.critical(f"CRITICAL storage: {status['free_gb']}GB free")
            return self._emergency_cleanup()
        elif status["status"] == "LOW":
            self.logger.warning(f"LOW storage: {status['free_gb']}GB free")
            return self._prepare_cleanup()
        else:
            self.logger.info(f"Storage NORMAL: {status['free_gb']}GB free")
            return {"action": "none", "status": "NORMAL"}

    def _emergency_cleanup(self):
        from camera_recorder.backup.backup_manager import BackupManager
        bm = BackupManager()
        result = bm.cleanup_verified_files()
        self.db.log_backup_event(None, None, "STORAGE", "CRITICAL_CLEANUP", f"deleted: {len(result['deleted'])} files")
        return result

    def _prepare_cleanup(self):
        from camera_recorder.backup.backup_manager import BackupManager
        bm = BackupManager()
        result = bm.cleanup_verified_files()
        self.db.log_backup_event(None, None, "STORAGE", "PREPARED_CLEANUP", f"deleted: {len(result['deleted'])} files")
        return result

    def should_cleanup(self):
        status = get_storage_status()
        return status["status"] in ["LOW", "CRITICAL"]

    def get_stats(self):
        status = get_storage_status()
        return {
            **status,
            "max_files": int((status["total_gb"] * 1024) / 500),
            "thresholds": self.thresholds,
            "google_photos_root": self.google_photos_root,
            "google_photos_note": "All recordings inside DCIM/ -> auto-backed up by Google Photos"
        }
