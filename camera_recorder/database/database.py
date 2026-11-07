import sqlite3
import json
import os
from datetime import datetime
from pathlib import Path
from camera_recorder.config.config import get_google_photos_root

class CameraDatabase:
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(get_google_photos_root(), "system", "database", "camera.db")
        self.db_path = db_path
        self.conn = None
        self._ensure_dirs()
        self._connect()
        self._create_tables()

    def _ensure_dirs(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def _create_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS cameras (
            camera_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            camera_type TEXT,
            protocol TEXT,
            storage_path TEXT,
            priority TEXT DEFAULT 'MEDIUM',
            enabled INTEGER DEFAULT 1,
            rtsp_url TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS recordings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size_mb REAL,
            duration_seconds INTEGER,
            status TEXT DEFAULT 'RECORDING' CHECK(status IN ('RECORDING','COMPLETED','BACKED_UP','VERIFIED','DELETED_LOCAL','FAILED','RETRY')),
            backup_status TEXT DEFAULT 'PENDING' CHECK(backup_status IN ('PENDING','ATTEMPTED','CONFIRMED','FAILED','RETRY')),
            backup_verified_at TEXT,
            local_delete_allowed INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (camera_id) REFERENCES cameras(camera_id)
        );

        CREATE TABLE IF NOT EXISTS backup_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id TEXT,
            recording_id INTEGER,
            action TEXT,
            status TEXT,
            details TEXT,
            retries INTEGER DEFAULT 0,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (camera_id) REFERENCES cameras(camera_id),
            FOREIGN KEY (recording_id) REFERENCES recordings(id)
        );

        CREATE TABLE IF NOT EXISTS storage_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total_gb REAL,
            free_gb REAL,
            used_gb REAL,
            status TEXT,
            low_threshold_gb REAL,
            critical_threshold_gb REAL,
            cleanup_triggered INTEGER DEFAULT 0,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS system_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        self.conn.commit()

    def register_camera(self, camera_id, name, camera_type, protocol, storage_path, priority="MEDIUM", rtsp_url=None, enabled=True):
        cur = self.conn.cursor()
        cur.execute("""INSERT OR REPLACE INTO cameras (camera_id, name, camera_type, protocol, storage_path, priority, rtsp_url, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (camera_id, name, camera_type, protocol, storage_path, priority, rtsp_url, 1 if enabled else 0))
        self.conn.commit()
        return cur.rowcount

    def get_camera(self, camera_id):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM cameras WHERE camera_id = ?", (camera_id,))
        return cur.fetchone()

    def get_all_cameras(self):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM cameras WHERE enabled = 1 ORDER BY priority DESC, created_at")
        return [dict(r) for r in cur.fetchall()]

    def add_recording(self, camera_id, file_path, file_size_mb=None, duration_seconds=None):
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO recordings (camera_id, file_path, file_size_mb, duration_seconds) VALUES (?, ?, ?, ?)""", (camera_id, file_path, file_size_mb, duration_seconds))
        self.conn.commit()
        return cur.lastrowid

    def update_recording_status(self, recording_id, status):
        cur = self.conn.cursor()
        cur.execute("UPDATE recordings SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, recording_id))
        self.conn.commit()

    def update_backup_status(self, recording_id, backup_status, verified_at=None):
        cur = self.conn.cursor()
        updates = ["backup_status = ?"]
        params = [backup_status]
        if verified_at:
            updates.append("backup_verified_at = ?")
            params.append(verified_at)
        if backup_status == "CONFIRMED":
            updates.append("local_delete_allowed = 1")
        params.append(recording_id)
        cur.execute(f"UPDATE recordings SET {', '.join(updates)} WHERE id = ?", params)
        self.conn.commit()

    def get_recordings_by_status(self, status):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM recordings WHERE status = ? ORDER BY created_at", (status,))
        return [dict(r) for r in cur.fetchall()]

    def get_pending_backups(self):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM recordings WHERE backup_status IN ('PENDING','FAILED','ATTEMPTED') ORDER BY created_at")
        return [dict(r) for r in cur.fetchall()]

    def get_verified_for_cleanup(self):
        cur = self.conn.cursor()
        cur.execute("""SELECT r.* FROM recordings r WHERE r.backup_status = 'CONFIRMED' AND r.status IN ('COMPLETED','VERIFIED','BACKED_UP') AND r.local_delete_allowed = 1 ORDER BY r.created_at ASC""")
        return [dict(r) for r in cur.fetchall()]

    def get_active_recordings(self):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM recordings WHERE status = 'RECORDING'")
        return [dict(r) for r in cur.fetchall()]

    def log_backup_event(self, camera_id, recording_id, action, status, details="", retries=0):
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO backup_log (camera_id, recording_id, action, status, details, retries) VALUES (?, ?, ?, ?, ?, ?)""", (camera_id, recording_id, action, status, details, retries))
        self.conn.commit()

    def save_storage_state(self, total_gb, free_gb, used_gb, status, low_threshold, critical_threshold, cleanup_triggered=0):
        cur = self.conn.cursor()
        cur.execute("""INSERT INTO storage_state (total_gb, free_gb, used_gb, status, low_threshold_gb, critical_threshold_gb, cleanup_triggered) VALUES (?, ?, ?, ?, ?, ?, ?)""", (total_gb, free_gb, used_gb, status, low_threshold, critical_threshold, cleanup_triggered))
        self.conn.commit()

    def get_latest_storage_state(self):
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM storage_state ORDER BY timestamp DESC LIMIT 1")
        return cur.fetchone()

    def set_system_state(self, key, value):
        cur = self.conn.cursor()
        cur.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()

    def get_system_state(self, key):
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM system_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def get_pending_work_after_restart(self):
        return {
            "pending_recordings": self.get_recordings_by_status("RECORDING"),
            "pending_backups": self.get_pending_backups(),
            "unverified": self.get_recordings_by_status("COMPLETED"),
        }

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
