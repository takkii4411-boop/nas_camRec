# CameraNAS - Complete System

## Quick Start (SAME commands on Termux, Linux, Windows)

```bash
python app.py --setup   # One-command setup
python app.py           # Start system
```

Linux me `python` na ho to: `python3 app.py --setup && python3 app.py`

Or use scripts: `bash run.sh --setup` / `bash run.sh` / `./run.ps1`

## Single .env (ONE file for all 3 platforms)

```
camera_recorder/system/config/.env
```

- Sirf EK `.env` file hai - Termux, Linux, Windows sab isi ko padhte hain
- Path code ke saath hai (repo folder ke andar) - kabhi copy nahi hota
- Values change karo → turant log me dikhegi (file hi source of truth hai)
- `.env` me: RTSP credentials, NAS_SMB_USER/PASS - code me kabhi hardcoded nahi
- Run ke baad log me check karo: `Username: admin` (aapki .env value)

## How Google Photos Backup Works (Pixel Phone)

### Setup ONCE:
1. On your Pixel phone → Settings → Google Photos → Backup → Turn ON for **DCIM** folder
2. After that: **EVERYTHING inside DCIM/ is automatically backed up to Google Photos**
3. Unlimited free storage on Pixel

### Code Flow:
```
1. CP Plus records → Saves MP4 segment
2. Code moves file to: DCIM/CameraNAS/CAM_XXX/
3. Google Photos auto-picks it up (already ON)
4. Wait sufficient time
5. Delete local copy only after confirmed

NO API credentials. NO OAuth2. NO credentials.json.
Just save to DCIM/ → Google Photos does the rest!
```

### Folder Structure:
```
DCIM/CameraNAS/          ← NAS share = recording folder (SAME folder)
├── CAM_001/             ← Mi 360 recordings
├── CAM_002/             ← CP Plus recordings
├── CAM_003/             ← Any new camera (auto-created)
└── system/
    ├── database/        ← SQLite state
    ├── logs/            ← All logs
    └── config/          ← config.json (copy, optional)
```

**When adding a new camera:** Code auto-creates `DCIM/CameraNAS/CAM_XXX/`. Google Photos covers it automatically.

## CP Plus Recording

```
CP Plus → FFmpeg → 1-min MP4 segments → Move to DCIM/CameraNAS/CAM_002/
→ Google Photos auto-backup → Wait → Delete local copy
```

RTSP URL from `.env` file. Credentials never in code.

## NAS Access (SMB + HTTP - both run together)

Startup flow (5 steps): NAS → wait → CP Plus → Mi camera → backup/storage

Log me clearly dikhta hai:
```
[OK] SMB (port 445) RUNNING on 192.168.1.3 -> Mi app will discover smb://192.168.1.3/CameraNAS
[OK] HTTP (port 8080) RUNNING on 192.168.1.3 -> Browser: http://192.168.1.3:8080/
NAS Status: SMB=RUNNING | HTTP=RUNNING | IP=192.168.1.3
```

- **SMB (445)**: Mi Home app ke liye - `smb://<ip>/CameraNAS`
- **HTTP (8080)**: Browser/file manager ke liye - `http://<ip>:8080/`
- IP auto-detect: Termux=wlan0, Linux=hostname, Windows=ipconfig (hardcoded IP nahi)
- User/pass: `.env` se (`NAS_SMB_USER`, `NAS_SMB_PASS`)

## Commands

| Command | Action |
|---------|--------|
| `python app.py --setup` | Install everything |
| `python app.py` | Start system |
| `python app.py --status` | Storage status |
| `python app.py --nas-status` | SMB/HTTP status |
| `python app.py --test` | Syntax check |
| `bash run.sh --setup` | Same (Bash) |
| `./run.ps1` | Start (Windows PowerShell) |

## File Structure (minimal)

```
nas_camRecording/
├── app.py                    ← Entry point (all platforms)
├── requirements.txt          ← python-pptx, pysmb
├── setup.sh / run.sh / run.ps1
├── README.md
├── PPT (project plan)
└── camera_recorder/
    ├── main.py               ← 5-step startup flow
    ├── config/
    │   ├── config.py         ← is_termux, load_env, get_env_path
    │   └── config.json       ← camera registry
    ├── system/config/.env    ← THE single .env (RTSP + NAS creds)
    ├── recorder/
    │   ├── cpplus.py         ← CP Plus recording
    │   ├── ffmpeg.py         ← FFmpeg controller
    │   └── mi_monitor.py     ← Mi camera
    ├── nas/nas_manager.py    ← SMB + HTTP servers, IP auto-detect
    ├── backup/backup_manager.py
    ├── storage/folders.py    ← Folder structure
    ├── database/database.py
    └── utils/
        ├── logger.py
        └── storage_check.py
```

## Safety Rules

- Local file NEVER deleted unless Google Photos backup confirmed
- Backup FAIL/UNKNOWN = keep local file forever
- Active recordings never cleaned up
- SQLite state persists across restarts

## Supported Platforms

- **Termux** (Android) - Primary
- **Linux** (PC/Laptop)
- **Windows** (PowerShell)

Same command works on all three: `python app.py --setup` then `python app.py`

## License

CameraNAS - Open Source Project
