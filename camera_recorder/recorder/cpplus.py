import os
import time
import json
import subprocess
from pathlib import Path
from datetime import datetime

try:
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.database.database import CameraDatabase
    from camera_recorder.config.config import get_rtsp_credentials, load_env
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.database.database import CameraDatabase
    from camera_recorder.config.config import get_rtsp_credentials, load_env

class CPPlusRecorder:
    def __init__(self, camera_id="CAM_002", rtsp_url=None, output_dir=None):
        self.camera_id = camera_id
        self.db = CameraDatabase()
        self.ffmpeg = None
        self.running = False
        self.logger = LoggerManager.get_logger("CPPLUS")
        self.segment_duration = 60

        # Load .env file
        load_env()

        # Segment length in seconds (each output file = this much video)
        try:
            self.segment_duration = int(os.environ.get("CP_SEGMENT_DURATION", "60"))
        except ValueError:
            self.segment_duration = 60

        # Get RTSP URL from .env file (priority) or parameter
        if rtsp_url:
            self.rtsp_url = rtsp_url
        else:
            self.rtsp_url = get_rtsp_credentials()

        self.output_dir = output_dir or self._get_output_dir()

    def _get_output_dir(self):
        if os.path.exists("/system/bin/termux-setup-storage"):
            return os.path.expanduser("~/storage/shared/CameraNAS/CP-Plus")
        return "/CameraNAS/CP-Plus"

    def configure_rtsp(self, rtsp_url=None):
        if rtsp_url:
            self.rtsp_url = rtsp_url
        else:
            self.rtsp_url = get_rtsp_credentials()
        self.db.register_camera(
            camera_id=self.camera_id,
            name="CP Plus CP-E35Q",
            camera_type="ip_camera",
            protocol="FFmpeg",
            storage_path=self.output_dir,
            priority="HIGH",
            rtsp_url=self.rtsp_url,
            enabled=True
        )
        self.logger.info(f"RTSP configured for {self.camera_id}: {self.rtsp_url}")

    def start(self):
        if not self.rtsp_url or self.rtsp_url == "rtsp://admin:password@CAMERA_IP:554/stream":
            self.logger.error("RTSP URL not configured. Check .env file.")
            return False

        from camera_recorder.recorder.ffmpeg import FFmpegController
        self.ffmpeg = FFmpegController(
            camera_id=self.camera_id,
            rtsp_url=self.rtsp_url,
            output_dir=self.output_dir,
            segment_duration=self.segment_duration
        )
        self.running = True
        self._record_loop()
        return True

    def _record_loop(self):
        # ONE start line at app boot; restarts/errors are logged by FFmpeg
        # monitor only when they actually happen (no per-second spam).
        self.logger.info(
            f"CP Plus recording STARTED: {self.camera_id} | "
            f"segment={self.segment_duration}s | continuous | -> {self.output_dir}")
        self.ffmpeg.start_recording()
        while self.running:
            time.sleep(10)
            exhausted = (self.ffmpeg.reconnect_count >= self.ffmpeg.max_reconnects)
            if not self.ffmpeg.is_recording() and not self.ffmpeg.running and exhausted:
                # FFmpeg monitor exhausted its reconnects - probe again after 10 min
                self.logger.warning(
                    f"CP Plus {self.camera_id} NOT recording (reconnects exhausted) "
                    f"— retry in 10 min")
                for _ in range(60):
                    if not self.running:
                        return
                    time.sleep(10)
                if self.running:
                    self.ffmpeg.reconnect_count = 0
                    self.ffmpeg.start_recording()

    def stop(self):
        self.running = False
        if self.ffmpeg:
            self.ffmpeg.stop_recording()
        self.logger.info("CP Plus recording stopped")

    def handle_disconnect(self):
        self.logger.warning("RTSP disconnected, attempting reconnect")
        self.db.log_backup_event(self.camera_id, None, "RTSP_DISCONNECTED", "FAILED")
        if self.ffmpeg:
            if self.ffmpeg.reconnect():
                self.db.log_backup_event(self.camera_id, None, "RECONNECTED", "SUCCESS")
            else:
                self.db.log_backup_event(self.camera_id, None, "RECONNECTED", "FAILED")
                return False
        return True

    def get_status(self):
        return {
            "camera_id": self.camera_id,
            "running": self.running,
            "recording": self.ffmpeg.is_recording() if self.ffmpeg else False,
            "rtsp_url": self.rtsp_url,
            "output_dir": self.output_dir,
            "segment_duration": self.segment_duration,
            "last_updated": datetime.now().isoformat()
        }
