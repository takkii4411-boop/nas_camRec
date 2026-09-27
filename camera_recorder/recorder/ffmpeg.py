import subprocess
import os
import time
import threading
from pathlib import Path
from datetime import datetime, timedelta

try:
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.database.database import CameraDatabase
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.database.database import CameraDatabase

FFMPEG_INSTALLED = None

def check_ffmpeg():
    global FFMPEG_INSTALLED
    if FFMPEG_INSTALLED is not None:
        return FFMPEG_INSTALLED
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        FFMPEG_INSTALLED = result.returncode == 0
    except FileNotFoundError:
        FFMPEG_INSTALLED = False
    return FFMPEG_INSTALLED

def install_ffmpeg():
    system = __import__("platform").system()
    if system == "Linux":
        try:
            if os.path.exists("/system/bin/termux-setup-storage"):
                subprocess.run(["pkg", "install", "-y", "ffmpeg"], check=True, timeout=120)
            else:
                subprocess.run(["apt-get", "install", "-y", "ffmpeg"], check=True, timeout=120)
            return True
        except Exception:
            try:
                subprocess.run(["sudo", "apt-get", "install", "-y", "ffmpeg"], check=True, timeout=120)
                return True
            except Exception:
                return False
    return False

class FFmpegController:
    def __init__(self, camera_id, rtsp_url, output_dir, segment_duration=60):
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.output_dir = output_dir
        self.segment_duration = segment_duration
        self.process = None
        self.running = False
        self.reconnect_count = 0
        self.max_reconnects = 10
        self.logger = LoggerManager.get_logger(f"FFMPEG_{camera_id}")
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

    def get_ffmpeg_command(self):
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        output_pattern = os.path.join(self.output_dir, f"{timestamp}_%03d.mp4")

        cmd = [
            "ffmpeg",
            "-rtsp_transport", "tcp",
            "-probesize", "1M",
            "-analyzeduration", "3000000",
            "-i", self.rtsp_url,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "aac",
            "-max_muxing_queue_size", "1024",
            "-f", "segment",
            "-segment_time", str(self.segment_duration),
            "-reset_timestamps", "1",
            output_pattern
        ]
        return cmd

    def start_recording(self):
        if not check_ffmpeg():
            if not install_ffmpeg():
                self.logger.error("FFmpeg not found and cannot install")
                return False

        cmd = self.get_ffmpeg_command()

        try:
            self._stop_requested = False
            # stderr = FILE (never PIPE!) - an unread PIPE fills up (~64KB)
            # and wedges ffmpeg forever: recording silently freezes with
            # NO exit, NO reconnect, NO logs.
            logs_dir = os.path.join(os.path.dirname(os.path.abspath(self.output_dir)),
                                    "system", "logs")
            Path(logs_dir).mkdir(parents=True, exist_ok=True)
            err_path = os.path.join(logs_dir, f"ffmpeg_{self.camera_id}.err.log")
            if getattr(self, "_err_file", None):
                try:
                    self._err_file.close()
                except Exception:
                    pass
            self._err_file = open(err_path, "ab")
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=self._err_file,
                stdin=subprocess.DEVNULL
            )
            self.running = True
            self._started_at = time.time()
            self._start_segment_reporter()
            self._monitor_process()
            return True
        except Exception as e:
            self.logger.error(f"Failed to start FFmpeg: {e}")
            return False

    def _start_segment_reporter(self):
        """Log ONE line each time a 60s segment file is finished
        (time period + filename). Runs across reconnects — must NOT be
        tied to self.running (monitor flips it False during backoff)."""
        if getattr(self, "_reporter_started", False):
            return
        self._reporter_started = True
        self._reporter_alive = True
        session_start = time.time() - 5
        reported = set()
        dur = self.segment_duration

        def report():
            while self._reporter_alive:
                try:
                    files = sorted(Path(self.output_dir).glob("*.mp4"),
                                  key=lambda p: p.stat().st_mtime)
                except Exception:
                    files = []
                for i, f in enumerate(files):
                    if f.name in reported:
                        continue
                    try:
                        last_write = f.stat().st_mtime
                    except OSError:
                        continue
                    if last_write < session_start:
                        continue  # old file from before this run
                    superseded = i < len(files) - 1
                    stale = (time.time() - last_write) > (dur + 15)
                    if superseded or stale:
                        reported.add(f.name)
                        end = datetime.fromtimestamp(last_write)
                        start = end - timedelta(seconds=dur)
                        self.logger.info(
                            f"Segment complete ({dur}s): "
                            f"{start.strftime('%H:%M:%S')} -> {end.strftime('%H:%M:%S')} | {f.name}")
                time.sleep(5)

        threading.Thread(target=report, daemon=True).start()

    def _monitor_process(self):
        def monitor():
            while self.running:
                if self.process and self.process.poll() is not None:
                    exit_code = self.process.returncode
                    lived = time.time() - getattr(self, "_started_at", time.time())
                    self.running = False
                    if lived > 300:
                        self.reconnect_count = 0  # stable run - forget past failures
                    self.logger.warning(
                        f"FFmpeg exited for {self.camera_id} "
                        f"(code: {exit_code}, lived {int(lived)}s)")
                    if self.reconnect_count < self.max_reconnects:
                        self.reconnect_count += 1
                        # backoff: 3,6,12,24,48,60... sec - accumulates across
                        # quick failures (no reset on start), escalates properly
                        delay = min(3 * (2 ** (self.reconnect_count - 1)), 60)
                        self.logger.info(
                            f"Reconnecting {self.camera_id} in {delay}s "
                            f"({self.reconnect_count}/{self.max_reconnects})")
                        time.sleep(delay)
                        if not getattr(self, "_stop_requested", False):
                            self.start_recording()
                    else:
                        self.logger.error(
                            f"Max reconnects ({self.max_reconnects}) reached for {self.camera_id} "
                            f"— giving up until app restart")
                    break
                time.sleep(2)
        threading.Thread(target=monitor, daemon=True).start()

    def stop_recording(self):
        self._stop_requested = True
        self._reporter_alive = False
        self._reporter_started = False
        if self.process and self.running:
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except Exception:
                self.process.kill()
            self.running = False
            self.logger.info(f"FFmpeg recording STOPPED for {self.camera_id}")
            return True
        return False

    def is_recording(self):
        if not self.process:
            return False
        if self.process.poll() is None:
            return True
        return False

    def get_active_segment(self):
        if not self.running:
            return None
        files = sorted(Path(self.output_dir).glob("*.mp4"))
        if files:
            return str(files[-1])
        return None

    def reconnect(self):
        self.logger.info(f"Attempting reconnection for {self.camera_id}")
        self.stop_recording()
        time.sleep(3)
        return self.start_recording()
