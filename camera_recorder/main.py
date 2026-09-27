import os
import sys
import json
import time
import signal
import threading
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from camera_recorder.config.config import load_config, get_camera_nas_root, detect_environment, load_env, resolve_path
from camera_recorder.recorder.cpplus import CPPlusRecorder
from camera_recorder.recorder.mi_monitor import MiCameraRecorder
from camera_recorder.nas.nas_manager import NASManager
from camera_recorder.backup.backup_manager import BackupManager
from camera_recorder.storage.folders import StorageManager
from camera_recorder.database.database import CameraDatabase
from camera_recorder.utils.logger import LoggerManager
from camera_recorder.utils.storage_check import get_storage_status

class CameraNASManager:
    def __init__(self):
        self.logger = LoggerManager.get_logger("CAMERANAS_MAIN")
        self.config = load_config()
        self.db = CameraDatabase()
        self.nas = NASManager()
        self.backup = BackupManager()
        self.storage = StorageManager()
        self.cameras = {}
        self.running = False
        self._register_cameras()
        self._setup_environment()

    def _register_cameras(self):
        load_env()
        for cam_id, cam_info in self.config.get("camera_registry", {}).items():
            if cam_info.get("enabled", False):
                self.db.register_camera(
                    camera_id=cam_id,
                    name=cam_info["name"],
                    camera_type=cam_info["type"],
                    protocol=cam_info["protocol"],
                    storage_path=resolve_path(cam_info.get("storage_path") or get_camera_nas_root()),
                    priority=cam_info.get("priority", "MEDIUM"),
                    rtsp_url=cam_info.get("rtsp_url"),
                    enabled=True
                )
        self.logger.info(f"Registered {len(self.config.get('camera_registry', {}))} cameras")

    def _setup_environment(self):
        self.storage.create_folder_structure()
        self.nas.setup_samba()
        self.nas.start_samba()
        self.logger.info("Environment setup complete")

    def start_all(self):
        self.running = True
        self.logger.info("=" * 50)
        self.logger.info("  CameraNAS System Starting")
        self.logger.info("=" * 50)
        self._setup_signal_handlers()

        # STEP 1: Start NAS (Samba) first
        self.logger.info("[1/5] Starting NAS (Samba)...")
        if self.nas.start_samba():
            self.logger.info("[OK] NAS is running")
        else:
            self.logger.error("[FAIL] NAS failed to start")
        self.nas.print_nas_access()

        # STEP 2: Wait for NAS to be ready
        self.logger.info("[2/5] Waiting for NAS to be ready...")
        time.sleep(2)

        # STEP 3: Start CP Plus recording
        self.logger.info("[3/5] Starting CP Plus recording...")
        cp_threads = []
        for cam_id, cam_info in self.config.get("camera_registry", {}).items():
            if not cam_info.get("enabled", False):
                continue
            if cam_info["type"] == "ip_camera":
                t = threading.Thread(target=self._run_cp_plus, args=(cam_id, cam_info), daemon=True)
                cp_threads.append(t)
                t.start()
        if cp_threads:
            self.logger.info(f"[OK] {len(cp_threads)} CP Plus camera(s) recording")

        # STEP 4: Start Mi camera recording
        self.logger.info("[4/5] Starting Mi camera recording...")
        mi_threads = []
        for cam_id, cam_info in self.config.get("camera_registry", {}).items():
            if not cam_info.get("enabled", False):
                continue
            if cam_info["type"] == "mi_camera":
                t = threading.Thread(target=self._run_mi_camera, args=(cam_id, cam_info), daemon=True)
                mi_threads.append(t)
                t.start()
        if mi_threads:
            self.logger.info(f"[OK] {len(mi_threads)} Mi camera(s) recording")

        # STEP 5: Start backup and storage loops
        self.logger.info("[5/5] Starting backup and storage...")
        t_backup = threading.Thread(target=self._backup_loop, daemon=True)
        t_storage = threading.Thread(target=self._storage_loop, daemon=True)
        t_backup.start()
        t_storage.start()

        self.logger.info("All systems started!")
        self._print_dashboard()
        all_threads = cp_threads + mi_threads + [t_backup, t_storage]
        for t in all_threads:
            t.join()

    def _run_mi_camera(self, cam_id, cam_info):
        recorder = MiCameraRecorder(camera_id=cam_id, storage_path=cam_info["storage_path"])
        self.cameras[cam_id] = recorder
        recorder.start_nas_transfer()

    def _run_cp_plus(self, cam_id, cam_info):
        from camera_recorder.config.config import get_rtsp_credentials
        rtsp_url = cam_info.get("rtsp_url") or get_rtsp_credentials()
        output_path = resolve_path(cam_info.get("storage_path") or get_camera_nas_root())
        recorder = CPPlusRecorder(camera_id=cam_id, rtsp_url=rtsp_url, output_dir=output_path)
        self.cameras[cam_id] = recorder
        recorder.start()

    def _backup_loop(self):
        while self.running:
            try:
                self.backup.process_pending_backups()
            except Exception as e:
                self.logger.error(f"Backup loop error: {e}")
            time.sleep(60)

    def _storage_loop(self):
        while self.running:
            try:
                moved = self.storage.organize_recordings()
                if moved:
                    self.logger.info(f"Organized {moved} recording(s) into date/time folders")
                if self.storage.should_cleanup():
                    self.storage.check_and_cleanup()
            except Exception as e:
                self.logger.error(f"Storage loop error: {e}")
            time.sleep(300)

    def _setup_signal_handlers(self):
        def signal_handler(sig, frame):
            self.logger.info("Shutdown signal received")
            self.stop_all()
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def stop_all(self):
        self.running = False
        for cam_id, recorder in self.cameras.items():
            try:
                recorder.stop()
            except Exception:
                pass
        self.logger.info("CameraNAS System Stopped")
        sys.exit(0)

    def _print_dashboard(self):
        status = get_storage_status()
        self.logger.info(f"Dashboard: Storage={status['status']} ({status['free_gb']}GB free) | Cameras={len(self.cameras)}")

    def get_status(self):
        return {
            "running": self.running,
            "cameras": {cid: cam.get_status() for cid, cam in self.cameras.items()},
            "storage": get_storage_status(),
            "backup": self.backup.get_backup_status(),
            "samba": self.nas.get_status(),
            "timestamp": datetime.now().isoformat()
        }

def main():
    manager = CameraNASManager()
    manager.start_all()

if __name__ == "__main__":
    main()