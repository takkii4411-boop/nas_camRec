import subprocess
import os
import sys
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
            "-fflags", "+genpts",
            "-probesize", "5M",
            "-analyzeduration", "5000000",
            "-i", self.rtsp_url,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "aac",
            "-avoid_negative_ts", "make_zero",
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
            self._err_path = err_path
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=self._err_file,
                stdin=subprocess.DEVNULL
            )
            self.running = True
            self._started_at = time.time()
            self._start_segment_reporter()
            self._start_progress_bar()
            self._monitor_process()
            return True
        except Exception as e:
            self.logger.error(f"Failed to start FFmpeg: {e}")
            return False

    def _start_progress_bar(self):
        """pip-style live bar: fills 1..60s for the current segment, then the
        'Segment complete' line takes over. Hides itself when recording is
        NOT alive (problem state). Only on an interactive terminal - keeps
        nohup/background logs clean."""
        if getattr(self, "_bar_started", False):
            return
        try:
            if not sys.stdout.isatty():
                return
        except Exception:
            return
        self._bar_started = True
        dur = self.segment_duration

        def tick():
            while getattr(self, "_reporter_alive", False):
                alive = (self.running and self.process
                         and self.process.poll() is None)
                try:
                    if alive:
                        el = time.time() - getattr(self, "_started_at", time.time())
                        seg = int(el) % dur
                        filled = int((el % dur) / dur * 24)
                        bar = "█" * filled + "░" * (24 - filled)
                        sys.stdout.write(
                            f"\r  CP Plus [{bar}] {seg + 1:2d}/{dur}s ")
                        sys.stdout.flush()
                    else:
                        sys.stdout.write("\r" + " " * 70 + "\r")
                        sys.stdout.flush()
                except Exception:
                    pass
                time.sleep(1)
            # hidden - clear the line for the logger line
            try:
                sys.stdout.write("\r" + " " * 70 + "\r")
                sys.stdout.flush()
            except Exception:
                pass

        threading.Thread(target=tick, daemon=True).start()

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

    def _last_err_line(self):
        """Surface the real ffmpeg failure reason into the main log so the
        cause is visible in the terminal (err.log stays as full detail)."""
        path = getattr(self, "_err_path", None)
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode(errors="replace")
        except Exception:
            return None
        keys = ("error", "failed", "unauthorized", "denied", "refused",
                "timeout", "invalid", "not found", "no route", "busy")
        line = None
        for ln in tail.splitlines():
            low = ln.lower()
            if any(k in low for k in keys):
                line = ln.strip()
        if line is None:
            nonempty = [ln.strip() for ln in tail.splitlines() if ln.strip()]
            line = nonempty[-1] if nonempty else None
        if line and len(line) > 180:
            line = line[:180] + "..."
        return line

    def _monitor_process(self):
        def monitor():
            while self.running:
                if self.process and self.process.poll() is not None:
                    exit_code = self.process.returncode
                    lived = time.time() - getattr(self, "_started_at", time.time())
                    self.running = False
                    # code 0 + ran a while = camera closed session normally
                    # (server-side cycle) - NOT a failure: reconnect fast,
                    # reset counter, never escalate to "giving up".
                    clean_close = (exit_code == 0 and lived >= 60)
                    if lived > 300 or clean_close:
                        self.reconnect_count = 0
                    if clean_close:
                        self.logger.info(
                            f"Camera closed RTSP session (normal, lived {int(lived)}s) "
                            f"— reconnecting in 2s")
                        time.sleep(2)
                        if not getattr(self, "_stop_requested", False):
                            self.start_recording()
                        break
                    self.logger.warning(
                        f"FFmpeg exited for {self.camera_id} "
                        f"(code: {exit_code}, lived {int(lived)}s)")
                    # Real reason from ffmpeg stderr, right in this log:
                    reason = self._last_err_line()
                    if reason:
                        self.logger.error(f"FFmpeg says: {reason}")
                    if exit_code == 8 and reason and "401" in reason:
                        self.logger.error(
                            "RTSP AUTH FAILED (401) — password in "
                            "system/config/.env is wrong (camera expects "
                            "admin / admin1234), fix .env and restart")
                    elif exit_code == 8 and not reason:
                        self.logger.error(
                            "exit 8 = RTSP server rejected request "
                            "(401/404 class) — check .env RTSP_URL + password")
                    # SECURITY RULE: NEVER give up - keep retrying forever.
                    # Backoff 3,6,12,24,48 then capped at 60s => worst-case
                    # footage gap ~60s (vs old 10-min blackout).
                    self.reconnect_count += 1
                    delay = min(3 * (2 ** (self.reconnect_count - 1)), 60)
                    # verbose for first attempts, then quiet (1 log per ~10 min)
                    if self.reconnect_count <= 5 or self.reconnect_count % 10 == 0:
                        self.logger.info(
                            f"Reconnecting {self.camera_id} in {delay}s "
                            f"(attempt {self.reconnect_count})")
                    time.sleep(delay)
                    if not getattr(self, "_stop_requested", False):
                        self.start_recording()
                    break
                time.sleep(2)
        threading.Thread(target=monitor, daemon=True).start()

    def stop_recording(self):
        self._stop_requested = True
        self._reporter_alive = False
        self._reporter_started = False
        self._bar_started = False
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
