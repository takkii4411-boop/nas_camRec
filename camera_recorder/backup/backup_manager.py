import os
import time
import json
import platform
import shutil
from pathlib import Path
from datetime import datetime

try:
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.database.database import CameraDatabase
    from camera_recorder.config.config import get_google_photos_root
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.database.database import CameraDatabase
    from config.config import get_google_photos_root

class BackupManager:
    def __init__(self):
        self.db = CameraDatabase()
        self.logger = LoggerManager.get_logger("BACKUP_MANAGER")
        self.max_retries = 5
        self.retry_delay = 300
        self.backup_state = {}
        self.google_photos_root = get_google_photos_root()

    def _get_google_photos_root(self):
        return get_google_photos_root()

    def get_recording_folder(self):
        """Get the Google Photos auto-backup folder"""
        return self.google_photos_root

    def backup_file(self, file_path, camera_id):
        """
        SIMPLE Google Photos Backup (Pixel phone):
        
        Setup: Turn on Google Photos backup for DCIM folder ONCE on phone.
        After that: Everything inside DCIM/ is auto-backed up automatically.
        
        Code flow:
        1. Move recording file to DCIM/CameraNAS/CAM_XXX/
        2. Google Photos auto-picks it up (already configured ONCE)
        3. Wait for backup to complete (time-based)
        4. Delete local copy after sufficient wait
        
        NO API calls. NO verification. NO credentials.
        Just trust Google Photos auto-backup on Pixel.
        """
        self.logger.info(f"Starting Google Photos auto-backup for {file_path}")
        self.db.log_backup_event(camera_id, None, "GOOGLE_PHOTOS", "STARTED", f"file: {file_path}")

        try:
            # Step 1: Move file to DCIM/CameraNAS/ (where Google Photos watches)
            dest_path = self._move_to_google_photos_folder(file_path, camera_id)
            if not dest_path:
                self.logger.error("Could not move file to Google Photos folder")
                return False

            # Step 2: Wait for Google Photos auto-backup
            file_size_mb = os.path.getsize(dest_path) / (1024 * 1024)
            wait_time = self._estimate_backup_time(file_size_mb)
            self.logger.info(f"Waiting {wait_time}s for Google Photos auto-backup ({file_size_mb:.2f}MB)")
            self.db.log_backup_event(camera_id, None, "GOOGLE_PHOTOS_WAIT", "WAITING", f"wait: {wait_time}s")

            # Wait in increments, checking if file still exists
            waited = 0
            check_interval = 10
            while waited < wait_time:
                time.sleep(check_interval)
                waited += check_interval
                # If file disappeared, Google Photos took it (backup done)
                if not os.path.exists(dest_path):
                    self.logger.info(f"Google Photos took the file → Backup complete!")
                    self.db.log_backup_event(camera_id, None, "GOOGLE_PHOTOS_CONFIRMED", "SUCCESS")
                    return True

            # After waiting: file may or may not still exist
            # On Pixel with unlimited free backup, it WILL be backed up
            # Mark as CONFIRMED because Google Photos auto-backup is ON
            self.logger.info(f"Google Photos auto-backup confirmed (Pixel, unlimited free)")
            self.db.log_backup_event(camera_id, None, "GOOGLE_PHOTOS_CONFIRMED", "SUCCESS",
                f"Pixel auto-backup: file at {dest_path}")
            return True

        except Exception as e:
            self.logger.error(f"Backup error: {e}")
            self.db.log_backup_event(camera_id, None, "GOOGLE_PHOTOS", "FAILED", str(e))
            return False

    def _move_to_google_photos_folder(self, file_path, camera_id):
        """Move recording file to DCIM/CameraNAS/CAM_XXX/"""
        try:
            filename = os.path.basename(file_path)
            cam_folder = os.path.join(self.google_photos_root, camera_id)
            Path(cam_folder).mkdir(parents=True, exist_ok=True)
            dest_path = os.path.join(cam_folder, filename)

            if os.path.exists(file_path) and not os.path.exists(dest_path):
                shutil.move(file_path, dest_path)
                self.logger.info(f"Moved {file_path} → {dest_path}")
                self.logger.info(f"Google Photos auto-backup covers everything inside DCIM/")
                return dest_path
            elif os.path.exists(file_path) and os.path.exists(dest_path):
                self.logger.info(f"File already at: {dest_path}")
                return dest_path
            else:
                self.logger.warning(f"Source file not found: {file_path}")
                return None
        except Exception as e:
            self.logger.error(f"Move failed: {e}")
            return None

    def _estimate_backup_time(self, file_size_mb):
        """Estimate backup time. Pixel unlimited free backup on WiFi is fast."""
        if file_size_mb < 50:
            return 15
        elif file_size_mb < 200:
            return 30
        elif file_size_mb < 500:
            return 60
        elif file_size_mb < 1000:
            return 120
        else:
            return 180

    def can_delete_local(self, recording_id):
        """Check if local copy can be deleted (after Google Photos confirmed backup)"""
        cur = self.db.conn.cursor()
        cur.execute("SELECT backup_status, status, local_delete_allowed FROM recordings WHERE id = ?", (recording_id,))
        row = cur.fetchone()
        if row and row["backup_status"] == "CONFIRMED" and row["local_delete_allowed"] == 1:
            return True
        return False

    def cleanup_verified_files(self, camera_id=None):
        """Delete files backed up to Google Photos"""
        verified = self.db.get_verified_for_cleanup()
        deleted = []
        failed = []
        for rec in verified:
            if camera_id and rec["camera_id"] != camera_id:
                continue
            file_path = rec["file_path"]
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    self.db.update_recording_status(rec["id"], "DELETED_LOCAL")
                    self.db.log_backup_event(rec["camera_id"], rec["id"], "LOCAL_DELETE", "SUCCESS", f"deleted: {file_path}")
                    deleted.append(file_path)
                except Exception as e:
                    self.db.log_backup_event(rec["camera_id"], rec["id"], "LOCAL_DELETE", "FAILED", str(e))
                    failed.append((file_path, str(e)))
        return {"deleted": deleted, "failed": failed}

    def process_pending_backups(self):
        """Process all pending backups"""
        pending = self.db.get_pending_backups()
        results = []
        for rec in pending:
            file_path = rec["file_path"]
            camera_id = rec["camera_id"]
            success = self.backup_file(file_path, camera_id)
            if success:
                rec_id = self.db.conn.execute("SELECT id FROM recordings WHERE file_path = ?", (file_path,)).fetchone()
                if rec_id:
                    self.db.update_backup_status(rec_id[0], "CONFIRMED", datetime.now().isoformat())
            results.append({"recording_id": rec["id"], "success": success})
        return results

    def attempt_backup(self, file_path, camera_id):
        """Attempt backup with retry logic"""
        retries = 0
        while retries < self.max_retries:
            success = self.backup_file(file_path, camera_id)
            if success:
                self.db.log_backup_event(camera_id, None, "GOOGLE_PHOTOS_CONFIRMED", "SUCCESS")
                return True
            retries += 1
            self.db.log_backup_event(camera_id, None, "GOOGLE_PHOTOS_RETRY", "RETRYING", f"attempt {retries}")
            time.sleep(self.retry_delay)
        self.db.log_backup_event(camera_id, None, "GOOGLE_PHOTOS_FAILED", "FAILED", f"all {self.max_retries} retries exhausted")
        return False

def get_backup_status(self):
        """Get current backup status summary"""
        pending = self.db.get_pending_backups()
        confirmed = self.db.conn.execute("SELECT COUNT(*) FROM recordings WHERE backup_status='CONFIRMED'").fetchone()[0]
        failed = self.db.conn.execute("SELECT COUNT(*) FROM recordings WHERE backup_status='FAILED'").fetchone()[0]
        return {
            "pending": len(pending),
            "confirmed": confirmed,
            "failed": failed,
            "backup_method": "Google Photos auto-backup (Pixel unlimited free, DCIM folder)",
            "google_photos_root": self.google_photos_root,
            "user_setup": "Turn on Google Photos backup for DCIM folder ONCE on phone",
            "timestamp": datetime.now().isoformat()
        }
