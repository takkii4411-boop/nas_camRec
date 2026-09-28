import os
import subprocess
import json
import platform
import shutil
import time
import threading
import signal
from pathlib import Path
from datetime import datetime

try:
    from camera_recorder.utils.logger import LoggerManager
    from camera_recorder.config.config import load_config, get_google_photos_root, is_termux, load_env
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from camera_recorder.utils.logger import LoggerManager
    from config.config import load_config, get_google_photos_root, is_termux, load_env


class NASManager:
    def __init__(self):
        self.logger = LoggerManager.get_logger("NAS_MANAGER")
        self.samba_running = False
        self.config = self._load_config()

    def dbg(self, msg):
        """Direct print (always visible, even if logger filters it)"""
        print(f"[SMB-DEBUG] {msg}", flush=True)

    def _port_bindable(self, port):
        """Can we bind this port? (Android blocks <1024 for non-root apps)"""
        try:
            import socket
            s = socket.socket()
            s.bind(("0.0.0.0", port))
            s.close()
            return True
        except PermissionError:
            return False
        except OSError:
            # In use = permission is fine
            return True

    def _smbd_running(self):
        """True if a real smbd process is alive (same /proc scan as kill)."""
        try:
            me = str(os.getpid())
            for ent in os.listdir("/proc"):
                if not ent.isdigit() or ent == me:
                    continue
                try:
                    with open(os.path.join("/proc", ent, "cmdline"), "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
                    if "smbd" not in cmd:
                        continue
                    if any(x in cmd for x in ("smbd_untag", "log.smbd", "smbd.log", "app.py")):
                        continue
                    return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    def _kill_smbd(self):
        """Kill old smbd. pkill -x is UNRELIABLE here (comm never matches 'smbd'
        exactly on Termux) -> scan /proc cmdlines directly."""
        killed = []
        try:
            me = str(os.getpid())
            for ent in os.listdir("/proc"):
                if not ent.isdigit() or ent == me:
                    continue
                try:
                    with open(os.path.join("/proc", ent, "cmdline"), "rb") as f:
                        cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
                    if "smbd" not in cmd:
                        continue
                    if any(x in cmd for x in ("smbd_untag", "log.smbd", "smbd.log", "app.py")):
                        continue
                    os.kill(int(ent), signal.SIGTERM)
                    killed.append(ent)
                except Exception:
                    continue
        except Exception as e:
            self.dbg(f"kill_smbd: scan failed: {e}")
        self.dbg(f"kill_smbd: killed pids={killed}")
        time.sleep(1)
        return bool(killed)

    def _build_untag_shim(self):
        """Two Android/Termux bugs fixed by one LD_PRELOAD shim:
        1) Heap tagged-pointers break Samba NTLMSSP msrpc_parse() - mallopt(-204, 0).
        2) Android seccomp blocks openat2 (arm64 nr 437) with SIGSYS - smbd child
           dies at first directory open; syscall() interposer routes 437 via openat()."""
        d = os.path.join(os.path.expanduser("~"), ".smb")
        Path(d).mkdir(parents=True, exist_ok=True)
        src = os.path.join(d, "smbd_untag.c")
        so = os.path.join(d, "smbd_untag.so")
        code = (
            "#define _GNU_SOURCE\n"
            "#include <malloc.h>\n"
            "#include <dlfcn.h>\n"
            "#include <fcntl.h>\n"
            "#include <unistd.h>\n"
            "#include <errno.h>\n"
            "#include <stdarg.h>\n"
            "#include <stdint.h>\n"
            "#include <sys/types.h>\n"
            "extern int mallopt(int, int);\n"
            "static long (*real_syscall)(long, ...);\n"
            "__attribute__((constructor))\n"
            "static void shim_init(void) {\n"
            "    mallopt(-204, 0);\n"
            "    real_syscall = (long (*)(long, ...))dlsym(RTLD_NEXT, \"syscall\");\n"
            "}\n"
            "long syscall(long n, ...) {\n"
            "    va_list ap; long a1=0,a2=0,a3=0,a4=0,a5=0,a6=0;\n"
            "    va_start(ap, n);\n"
            "    a1=va_arg(ap,long); a2=va_arg(ap,long); a3=va_arg(ap,long);\n"
            "    a4=va_arg(ap,long); a5=va_arg(ap,long); a6=va_arg(ap,long);\n"
            "    va_end(ap);\n"
            "    if (n == 437 || n == 326) {\n"
            "        const uint64_t *how = (const uint64_t *)a3;\n"
            "        if (!how || (uint64_t)a4 < 8) { errno = EINVAL; return -1; }\n"
            "        return (long)openat((int)a1, (const char *)a2, (int)how[0], (mode_t)how[1]);\n"
            "    }\n"
            "    if (!real_syscall) { errno = ENOSYS; return -1; }\n"
            "    return real_syscall(n, a1, a2, a3, a4, a5, a6);\n"
            "}\n"
        )
        try:
            need_build = not os.path.exists(so)
            try:
                with open(src) as f:
                    if f.read() != code:
                        need_build = True
            except OSError:
                need_build = True
            if need_build:
                with open(src, "w") as f:
                    f.write(code)
                try:
                    os.remove(so)
                except OSError:
                    pass
            if os.path.exists(so):
                self.dbg("shim: using cached build")
                return so
            clang = shutil.which("clang")
            if not clang:
                self.dbg("shim: clang not found -> pkg install clang")
                subprocess.run(["pkg", "install", "-y", "clang"], capture_output=True, timeout=600)
                clang = shutil.which("clang")
            if not clang:
                self.dbg("shim: clang unavailable - SMB will FAIL (Android seccomp/tagged-ptr bugs)")
                return None
            r = subprocess.run([clang, "-shared", "-fPIC", "-o", so, src, "-ldl"],
                               capture_output=True, text=True, timeout=60)
            self.dbg(f"shim: compile rc={r.returncode} err='{r.stderr.strip()[:200]}'")
            if r.returncode == 0 and os.path.exists(so):
                return so
        except Exception as e:
            self.dbg(f"shim: FAILED {type(e).__name__}: {e}")
        return None

    def _restore_445(self):
        """Reboot/termux-session wipes net.ipv4.ip_unprivileged_port_start ->
        445 becomes unbindable -> smbd silently falls back to 4460 -> Windows
        //ip/CameraNAS and Mi app (both hardcode 445) FAIL. Restore via su."""
        if getattr(self, "_sysctl_tried", False):
            return
        self._sysctl_tried = True
        su = shutil.which("su")
        if not su:
            self.dbg("restore445: no su binary - keep fallback")
            return
        try:
            r = subprocess.run(
                [su, "-c", "sysctl -w net.ipv4.ip_unprivileged_port_start=0"],
                capture_output=True, timeout=5)
            out = r.stdout.decode(errors="replace").strip()[:80]
            err = r.stderr.decode(errors="replace").strip()[:80]
            self.dbg(f"restore445: rc={r.returncode} out='{out}' err='{err}'")
        except Exception as e:
            self.dbg(f"restore445 failed: {e}")
        if self._port_bindable(445):
            self.logger.info("[OK] port 445 privilege RESTORED (sysctl) - SMB on 445")
        else:
            self.logger.warning(
                "port 445 still blocked - SMB falls back to 4460 "
                "(connect with smb://<ip>:4460/CameraNAS)")

    def _smb_port(self):
        """445 if bindable, else high-port fallback (Android privileged port block)"""
        if not hasattr(self, "_smb_port_cache"):
            load_env()
            env_port = os.environ.get("NAS_SMB_PORT", "").strip()
            port = int(env_port) if env_port.isdigit() else None
            if port is None:
                if not self._port_bindable(445):
                    self._restore_445()
                port = 445 if self._port_bindable(445) else 4460
            self._smb_port_cache = port
            if port != 445:
                msg = (f"Port 445 BLOCKED by Android (Permission denied - privileged port). "
                       f"Using port {port} instead.")
                self.dbg(f"smb_port: {msg}")
                self.logger.warning(msg)
                self.logger.warning("  ROOT FIX (permanent): sysctl -w net.ipv4.ip_unprivileged_port_start=0")
                self.logger.warning(f"  No root: Mi app connect manually -> smb://<ip>:{port}/CameraNAS")
        return self._smb_port_cache

    def _load_config(self):
        try:
            return load_config()
        except Exception:
            return {}

    def _create_samba_user(self, username, password, plat):
        self.logger.info(f"Creating Samba user: {username}")
        self.dbg(f"create_samba_user: user='{username}' pass='{'*' * len(password)}' plat={plat}")
        try:
            if plat == "termux":
                # NOTE: `smbpasswd -l` does not exist (rc=1, always failed).
                # Add/update the user in OUR config's passdb so admin login
                # works (guest already works without it). -a is idempotent:
                # re-running just updates the password.
                conf = os.path.join(os.path.expanduser("~"), ".smb", "smb.conf")
                cmd = ["smbpasswd", "-a", "-s"]
                if os.path.exists(conf):
                    cmd += ["-c", conf]
                cmd.append(username)
                r = subprocess.run(
                    cmd,
                    input=f"{password}\n{password}\n".encode(),
                    capture_output=True, timeout=10
                )
                err = r.stderr.decode(errors="replace").strip()[:200]
                self.dbg(f"smbpasswd -a {username} rc={r.returncode} err='{err}'")
                if r.returncode == 0:
                    self.logger.info(f"[OK] Samba user '{username}' added (admin login + guest both work)")
                else:
                    self.logger.info(f"Samba user '{username}' not added (GUEST access still works)")
            elif plat == "linux":
                subprocess.run(
                    ["sudo", "smbpasswd", "-a", "-s", username],
                    input=f"{password}\n{password}\n".encode(),
                    capture_output=True, timeout=10
                )
                self.logger.info(f"Samba password set for user '{username}' on Linux")
            return True
        except Exception as e:
            self.dbg(f"create_samba_user EXCEPTION: {e}")
            self.logger.info(f"Samba user step skipped: {e} (GUEST access enabled)")
            return False

    def set_samba_password(self, username, new_password):
        load_env()
        self.logger.info(f"Setting Samba password for user '{username}'")
        try:
            if is_termux():
                conf = os.path.join(os.path.expanduser("~"), ".smb", "smb.conf")
                cmd = ["smbpasswd"]
                if os.path.exists(conf):
                    cmd += ["-c", conf]
                cmd.append(username)
                subprocess.run(
                    cmd,
                    input=f"{new_password}\n{new_password}\n".encode(),
                    capture_output=True, timeout=10
                )
            else:
                subprocess.run(
                    ["sudo", "smbpasswd", username],
                    input=f"{new_password}\n{new_password}\n".encode(),
                    capture_output=True, timeout=10
                )
            self.logger.info(f"Samba password updated for '{username}'")
            return True
        except Exception as e:
            self.logger.error(f"Failed to update Samba password: {e}")
            return False

    def change_samba_password(self, new_password=None):
        load_env()
        username = os.environ.get("NAS_SMB_USER", self.config.get('samba', {}).get('username', 'nasuser'))
        if new_password is None:
            new_password = os.environ.get("NAS_SMB_PASS_NEW")
        if not new_password:
            self.logger.error("No new password provided. Set NAS_SMB_PASS_NEW in .env")
            return False
        return self.set_samba_password(username, new_password)

    def get_samba_credentials(self):
        load_env()
        return {
            "username": os.environ.get("NAS_SMB_USER", self.config.get('samba', {}).get('username', 'nasuser')),
            "password": os.environ.get("NAS_SMB_PASS", self.config.get('samba', {}).get('password', 'naspass')),
            "share_name": self.config.get('samba', {}).get('share_name', 'CameraNAS'),
            "ip": self.get_samba_ip()
        }

    def detect_platform(self):
        if is_termux():
            return "termux"
        system = platform.system()
        return system.lower()

    def setup_samba(self):
        plat = self.detect_platform()
        self.logger.info(f"Setting up Samba for platform: {plat}")
        load_env()
        smb_user = os.environ.get("NAS_SMB_USER", self.config.get('samba', {}).get('username', 'nasuser'))
        smb_pass = os.environ.get("NAS_SMB_PASS", self.config.get('samba', {}).get('password', 'naspass'))
        share_name = self.config.get('samba', {}).get('share_name', 'CameraNAS')
        self.dbg(f"setup_samba: plat={plat} user='{smb_user}' share='{share_name}' config_keys={list(self.config.keys())}")

        if plat == "termux":
            # setup FIRST so ~/.smb/smb.conf exists -> smbpasswd -c uses our
            # config (passdb in state dir) instead of a missing default conf
            ok = self._setup_samba_termux()
            self._create_samba_user(smb_user, smb_pass, plat)
            return ok
        self._create_samba_user(smb_user, smb_pass, plat)
        if plat == "linux":
            return self._setup_samba_linux()
        elif plat == "windows":
            return self._setup_samba_windows()
        return False

    def _termux_prefix(self):
        return os.environ.get("PREFIX", "/data/data/com.termux/files/usr")

    def _setup_samba_termux(self):
        try:
            prefix = self._termux_prefix()
            self.dbg(f"setup_termux: PREFIX={prefix}")
            smbd = shutil.which("smbd") or os.path.join(prefix, "sbin", "smbd")
            self.dbg(f"setup_termux: smbd binary='{smbd}' which()='{shutil.which('smbd')}' exists={os.path.exists(smbd)}")
            if not os.path.exists(smbd):
                self.dbg("setup_termux: smbd NOT found -> running pkg install samba")
                r = subprocess.run(["pkg", "install", "-y", "samba"], timeout=300,
                                   capture_output=True, text=True)
                self.dbg(f"setup_termux: pkg install rc={r.returncode} err='{r.stderr.strip()[-200:]}'")
                smbd = shutil.which("smbd") or os.path.join(prefix, "sbin", "smbd")
                self.dbg(f"setup_termux: after install smbd='{smbd}' exists={os.path.exists(smbd)}")

            share_dir = os.path.expanduser("~/storage/shared/DCIM/CameraNAS")
            Path(share_dir).mkdir(parents=True, exist_ok=True)
            self.dbg(f"setup_termux: share_dir='{share_dir}' exists={os.path.exists(share_dir)} writable={os.access(share_dir, os.W_OK)}")

            # Termux needs WRITABLE state dirs (default /var/lib/samba does not exist)
            state = os.path.join(prefix, "var", "lib", "samba")
            for d in ["", "private", "cache", "lock", "pid", "log", "tls"]:
                Path(os.path.join(state, d)).mkdir(parents=True, exist_ok=True)
            self.dbg(f"setup_termux: state dirs created at '{state}' writable={os.access(state, os.W_OK)}")

            try:
                guest_user = subprocess.run(["whoami"], capture_output=True, text=True, timeout=5).stdout.strip()
            except Exception as e:
                guest_user = ""
                self.dbg(f"setup_termux: whoami failed: {e}")
            self.dbg(f"setup_termux: whoami='{guest_user}'")
            # Verify guest user exists in Termux passwd (else smbd refuses sessions)
            passwd_file = os.path.join(prefix, "etc", "passwd")
            sys_users = []
            try:
                with open(passwd_file) as pf:
                    sys_users = [l.split(":")[0] for l in pf if l.strip()]
                self.dbg(f"setup_termux: passwd users={sys_users}")
            except Exception as e:
                self.dbg(f"setup_termux: passwd read failed: {e} -> CREATING {passwd_file}")
                # Termux ships WITHOUT $PREFIX/etc/passwd -> getpwnam() fails ->
                # guest/force-user unresolved -> every client session setup fails
                try:
                    uid, gid = os.getuid(), os.getgid()
                    home = os.path.expanduser("~")
                    lines = [
                        f"root:x:0:0:root:{home}:/system/bin/sh",
                        f"{guest_user or 'app'}:x:{uid}:{gid}:app:{home}:/system/bin/sh",
                        "nobody:x:65534:65534:nobody:/system:/system/bin/false",
                    ]
                    Path(os.path.dirname(passwd_file)).mkdir(parents=True, exist_ok=True)
                    with open(passwd_file, "w") as pf:
                        pf.write("\n".join(lines) + "\n")
                    sys_users = [l.split(":")[0] for l in lines]
                    self.dbg(f"setup_termux: CREATED passwd users={sys_users}")
                except Exception as e2:
                    self.dbg(f"setup_termux: cannot create passwd: {e2}")
            if guest_user not in sys_users and sys_users:
                old = guest_user
                guest_user = "nobody" if "nobody" in sys_users else (sys_users[1] if len(sys_users) > 1 else sys_users[0])
                self.dbg(f"setup_termux: '{old}' NOT in passwd -> fallback guest='{guest_user}'")

            share_name = self.config.get('samba', {}).get('share_name', 'CameraNAS')
            smb_port = self._smb_port()
            samba_config = f"""[global]
   workgroup = WORKGROUP
   netbios name = CAMNAS
   server string = CameraNAS Server
   security = user
   map to guest = bad user
   guest account = {guest_user}
   smb ports = {smb_port}
   disable netbios = no
   server min protocol = NT1
   local master = yes
   preferred master = yes
   lm announce = yes
   dns proxy = no
   server role = standalone server
   state directory = {state}
   cache directory = {state}/cache
   lock directory = {state}/lock
   pid directory = {state}/pid
   private dir = {state}/private
   log file = {state}/log/log.smbd
   max log size = 500
   debug level = 5
   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes

[{share_name}]
   path = {share_dir}
   browseable = yes
   read only = no
   guest ok = yes
   force user = {guest_user}
   create mask = 0755
   directory mask = 0755
"""
            config_dir = os.path.join(os.path.expanduser("~"), ".smb")
            Path(config_dir).mkdir(parents=True, exist_ok=True)
            config_path = os.path.join(config_dir, "smb.conf")
            with open(config_path, "w") as f:
                f.write(samba_config)
            self.dbg(f"setup_termux: config written '{config_path}' size={os.path.getsize(config_path)} bytes")
            self.dbg(f"setup_termux: guest_account='{guest_user}' share='{share_name}' path='{share_dir}' smb_port={smb_port}")
            self.dbg(f"setup_termux: bindable 445={self._port_bindable(445)} (chosen port {smb_port})")

            # Validate config before starting smbd
            check = subprocess.run([smbd, "-s", config_path, "-b"], capture_output=True, text=True, timeout=10)
            self.dbg(f"setup_termux: validate rc={check.returncode}")
            if check.returncode != 0:
                self.dbg(f"setup_termux: validate FAILED stdout='{check.stdout.strip()[:300]}' stderr='{check.stderr.strip()[:300]}'")
                self.logger.error(f"smb.conf INVALID: {check.stderr.strip()[:200]}")
                return False

            ip = self.get_samba_ip()
            self.logger.info(f"[OK] Samba configured: share={share_dir}")
            self.logger.info(f"[OK] Config validated: {config_path}")
            self.logger.info(f"  GUEST access: ON (no password needed from Mi app)")
            port_sfx = "" if smb_port == 445 else f":{smb_port}"
            self.logger.info(f"  SMB URL: smb://{ip}{port_sfx}/{share_name}")
            return True
        except Exception as e:
            self.dbg(f"setup_termux EXCEPTION: {type(e).__name__}: {e}")
            self.logger.error(f"Termux Samba setup failed: {e}")
            return False

    def _start_nmbd_termux(self):
        """Start nmbd (NetBIOS Name Service, UDP 137/138).

        Mi Home's NAS scan makes the camera broadcast a wildcard NBNS query
        (UDP 137, 50 bytes) - seen on the wire from 192.168.1.14 during a
        scan; Windows answers it, that's why the desktop shows up.
        Launch as the Termux USER first: smbd owns msg.lock and root nmbd
        fails samba's strict ownership check ("Failed to init messaging
        context"); binding 137 works as user via
        net.ipv4.ip_unprivileged_port_start=0. Root fallback uses a private
        lock dir created by root. LD_PRELOAD untag shim like smbd."""
        try:
            prefix = self._termux_prefix()
            nmbd = shutil.which("nmbd") or os.path.join(prefix, "bin", "nmbd")
            if not os.path.exists(nmbd):
                alt = os.path.join(prefix, "sbin", "nmbd")
                if os.path.exists(alt):
                    nmbd = alt
            if not os.path.exists(nmbd):
                self.logger.warning("nmbd not found - Mi Home auto-scan will NOT see the NAS")
                return
            config_path = os.path.join(os.path.expanduser("~"), ".smb", "smb.conf")
            log_file = os.path.join(os.path.expanduser("~"), ".smb", "nmbd.log")

            # RESTART-SAFE: keep an already-running nmbd (no pkill+relaunch
            # on app restart). /proc scan - this phone's pkill -0 is broken.
            try:
                me = str(os.getpid())
                for ent in os.listdir("/proc"):
                    if not ent.isdigit() or ent == me:
                        continue
                    try:
                        with open(os.path.join("/proc", ent, "cmdline"), "rb") as f:
                            cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
                        # wrapper shells carry '>> nmbd.log' in argv - skip them
                        if "nmbd" in cmd and "nmbd.log" not in cmd and "app.py" not in cmd:
                            self.dbg("nmbd: already running - keeping existing instance")
                            self.logger.info("[OK] nmbd running (kept, NOT restarted) - NetBIOS 'CAMNAS'")
                            return
                    except Exception:
                        continue
            except Exception as e:
                self.dbg(f"nmbd: running-check failed: {e}")

            # Kill any old instance (fresh smb.conf must be loaded)
            for as_root in (True, False):
                try:
                    cmd = (["su", "-c", "pkill -f '[b]in/nmbd'"] if as_root
                           else ["pkill", "-f", "[b]in/nmbd"])
                    subprocess.run(cmd, timeout=5, capture_output=True)
                except Exception:
                    pass
            time.sleep(1)

            shim = self._build_untag_shim()
            preload = f"LD_PRELOAD={shim} " if shim else ""
            try:
                open(log_file, "w").close()
            except Exception:
                pass

            # 1) Termux user: shares smbd's msg.lock, binds 137 via sysctl
            attempts = [(f"{preload}{nmbd} -d 3 -s {config_path} >> {log_file} 2>&1",
                         False)]
            # 2) root fallback: PRIVATE lock dir so root-created msg.lock
            #    passes the ownership check (rm first: must be root-owned)
            try:
                import re as _re
                alt_conf = config_path + ".nmbd"
                alt_lock = os.path.join(os.path.dirname(config_path), "lock_nmbd")
                cfg = open(config_path).read()
                cfg = _re.sub(r"(?m)^\s*lock directory = .*$",
                              f"   lock directory = {alt_lock}", cfg)
                with open(alt_conf, "w") as f:
                    f.write(cfg)
                attempts.append(
                    (f"rm -rf {alt_lock}; {preload}{nmbd} -d 3 -s {alt_conf} "
                     f">> {log_file} 2>&1", True))
            except Exception as e:
                self.dbg(f"nmbd: root-conf derive failed: {e}")

            alive = False
            for inner, as_root in attempts:
                try:
                    cmd = ["su", "-c", inner] if as_root else ["sh", "-c", inner]
                    with open(os.devnull, "w") as dn:
                        subprocess.Popen(cmd, stdout=dn, stderr=dn)
                    self.dbg(f"nmbd: launched as_root={as_root}")
                except Exception as e:
                    self.dbg(f"nmbd: launch as_root={as_root} failed: {e}")
                    continue
                time.sleep(2.5)
                # NOTE: this phone's pkill rejects -0 ("bad -L '0'") -> use ps
                found = False
                for chk_root in (True, False):
                    try:
                        c = ["su", "-c", "ps -A"] if chk_root else ["ps", "-A"]
                        r = subprocess.run(c, timeout=5, capture_output=True,
                                           text=True)
                        if r.returncode == 0 and "nmbd" in (r.stdout or ""):
                            found = True
                            break
                    except Exception:
                        pass
                if found:
                    alive = True
                    break
            if alive:
                self.logger.info("[OK] nmbd started - NetBIOS name 'CAMNAS' (Mi Home NBNS scan)")
            else:
                self.logger.warning("nmbd exited - see ~/.smb/nmbd.log")
                try:
                    if os.path.exists(log_file):
                        for l in open(log_file).read().splitlines()[-15:]:
                            self.dbg(f"nmbd.log: {l}")
                except Exception:
                    pass
        except Exception as e:
            self.dbg(f"nmbd EXCEPTION: {type(e).__name__}: {e}")

    def _start_smb_termux(self):
        """Start SMB daemon on Termux - foreground mode so errors are captured"""
        try:
            # Setup always runs first: creates/validates ~/.smb/smb.conf
            if not self._setup_samba_termux():
                self.logger.error("Samba config invalid - SMB cannot start. Check ~/.smb/smb.conf")
                return
            config_path = os.path.join(os.path.expanduser("~"), ".smb", "smb.conf")

            # RESTART-SAFE: if a healthy smbd from a previous run is already
            # serving, KEEP it (no kill+relaunch churn on app restart).
            # smbd re-reads smb.conf per new session, so conf edits still apply.
            smb_port = self._smb_port()
            if self._smbd_running() and self._check_port(smb_port):
                self.dbg("start_termux: smbd already running - keeping existing instance")
                self.logger.info(f"[OK] smbd running (kept, NOT restarted) - port {smb_port} LISTENING")
                self._start_nmbd_termux()
                self._start_wsdd_discovery()
                return

            # Kill old smbd (may have old broken config / no shim)
            self._kill_smbd()

            prefix = self._termux_prefix()
            smbd = shutil.which("smbd") or os.path.join(prefix, "sbin", "smbd")
            self.dbg(f"start_termux: smbd='{smbd}' exists={os.path.exists(smbd)} which()='{shutil.which('smbd')}'")
            if not os.path.exists(smbd) and not shutil.which("smbd"):
                self.logger.error("smbd not found! Run: pkg install samba")
                return

            # Foreground mode (-F): errors stay visible (unlike -D which hides them)
            # NOTE: do NOT use start_new_session=True here - it makes smbd a process
            # group leader, then its own setsid() fails with EPERM ("Failed to create session")
            # Truncate samba's own log so tail always shows THIS run's error
            state_log = os.path.join(prefix, "var", "lib", "samba", "log", "log.smbd")
            try:
                if os.path.exists(state_log):
                    open(state_log, "w").close()
                    self.dbg(f"start_termux: truncated old log {state_log}")
            except Exception as e:
                self.dbg(f"start_termux: cannot truncate {state_log}: {e}")
            log_file = os.path.join(os.path.expanduser("~"), ".smb", "smbd.log")
            cmd = [smbd, "-s", config_path, "-F"]
            # Android tagged-ptr shim: without it Samba 4.24 NTLMSSP parse ALWAYS fails
            env = os.environ.copy()
            shim = self._build_untag_shim()
            if shim:
                env["LD_PRELOAD"] = shim + (":" + env["LD_PRELOAD"] if env.get("LD_PRELOAD") else "")
                self.dbg(f"start_termux: LD_PRELOAD={shim}")
            else:
                self.dbg("start_termux: NO untag shim - session setup will fail (tagged-ptr bug)")
            self.dbg(f"start_termux: launching: {' '.join(cmd)}")
            with open(log_file, "w") as lf:
                proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
            self.dbg(f"start_termux: Popen OK pid={proc.pid}")

            # Wait up to 6s for port 445
            for i in range(12):
                time.sleep(0.5)
                smb_port = self._smb_port()
                port_open = self._check_port(smb_port)
                alive = proc.poll()
                self.dbg(f"start_termux: poll {i + 1}/12 port{smb_port}={'OPEN' if port_open else 'closed'} proc_exit={alive}")
                if port_open:
                    self.logger.info(f"[OK] smbd started - port {smb_port} LISTENING")
                    self._start_nmbd_termux()
                    self._start_wsdd_discovery()
                    return

            # Failed: show real error (our log + samba's own log)
            exit_code = proc.poll()
            if exit_code is not None and exit_code != 0:
                self.logger.error(f"smbd exited with code {exit_code}")
            else:
                # smbd alive but port not open yet - give it a moment then stop it
                time.sleep(2)
                if self._check_port(smb_port):
                    self.logger.info(f"[OK] smbd started - port {smb_port} LISTENING")
                    self._start_nmbd_termux()
                    self._start_wsdd_discovery()
                    return
                self.logger.error(f"smbd running but port {smb_port} NOT listening")
                try:
                    proc.terminate()
                except Exception:
                    pass
            self._print_smbd_errors(log_file)
        except Exception as e:
            self.dbg(f"start_termux EXCEPTION: {type(e).__name__}: {e}")
            self.logger.warning(f"Termux SMB start failed: {e}")

    def _print_smbd_errors(self, log_file):
        """Print last error lines from our log + samba's own log"""
        candidates = [log_file]
        state = os.path.join(self._termux_prefix(), "var", "lib", "samba", "log")
        try:
            if os.path.isdir(state):
                state_files = [os.path.join(state, f) for f in sorted(
                    os.listdir(state), key=lambda x: os.path.getmtime(os.path.join(state, x)),
                    reverse=True)[:3]]
                candidates += state_files
                self.dbg(f"error_scan: samba log dir files={[os.path.basename(f) for f in state_files]}")
            else:
                self.dbg(f"error_scan: samba log dir MISSING: {state}")
        except Exception as e:
            self.dbg(f"error_scan: listdir failed: {e}")
        printed = False
        for path in candidates:
            try:
                size = os.path.getsize(path)
                with open(path) as f:
                    all_lines = [l.rstrip() for l in f.readlines() if l.strip()]
                # Last 30 lines (message is ABOVE the backtrace frames)
                lines = all_lines[-30:]
                # Also highlight actual error keywords from the tail
                kw = [l for l in all_lines[-60:] if any(
                    k in l.lower() for k in
                    ("error", "failed", "panic", "fatal", "cannot", "invalid",
                     "refused", "denied", "no such", "not found", "unable"))]
                self.dbg(f"error_scan: {path} size={size} bytes, tail_lines={len(lines)}, keyword_hits={len(kw)}")
                if kw:
                    self.logger.error(f"ERROR KEYWORDS in {path}:")
                    for l in kw[-10:]:
                        self.logger.error(f"  {l}")
                if lines:
                    self.logger.error(f"Last 30 lines from {path}:")
                    for l in lines:
                        self.logger.error(f"  {l}")
                    printed = True
                    break
            except Exception as e:
                self.dbg(f"error_scan: cannot read {path}: {e}")
                continue
        if not printed:
            self.logger.error(f"No output captured. Run manually to see error:")
            self.logger.error(f"  pkill smbd; smbd -s ~/.smb/smb.conf -F")

    def _patch_wsdd(self, wsdd_path):
        """Termux: platform.system() returns 'Android' -> wsdd raises
        NotImplementedError('unsupported OS') at create_address_monitor.
        Route Android to the Linux Netlink monitor. Also fix py3.14
        asyncio.get_event_loop() crash when run with -vv, and Android
        SELinux blocking netlink bind with mcast groups for the app uid.
        All replaces are guarded so repeated runs never corrupt the file."""
        try:
            with open(wsdd_path) as f:
                t = f.read()
            orig = t
            if "if system == 'Linux':" in t:
                t = t.replace("if system == 'Linux':",
                              "if system in ['Linux', 'Android']:", 1)
            if ("asyncio.get_event_loop().set_debug(True)" in t and
                    "asyncio.set_event_loop(asyncio.new_event_loop())" not in t):
                t = t.replace("asyncio.get_event_loop().set_debug(True)",
                              "asyncio.set_event_loop(asyncio.new_event_loop()); "
                              "asyncio.get_event_loop().set_debug(True)", 1)
            # root (su): groups bind works -> live addr-change events;
            # app uid: SELinux EACCES -> fall back to no-groups socket
            # (initial enumeration still works via RTM_GETADDR dump reply).
            fb = ("try: self.socket.bind((0, rtm_groups))\n"
                  "        except OSError: self.socket.bind((0, 0))")
            if "except OSError: self.socket.bind((0, 0))" in t:
                pass
            elif "self.socket.bind((0, 0))" in t:
                t = t.replace("self.socket.bind((0, 0))", fb, 1)
            elif "self.socket.bind((0, rtm_groups))" in t:
                t = t.replace("self.socket.bind((0, rtm_groups))", fb, 1)
            if t != orig:
                with open(wsdd_path, "w") as f:
                    f.write(t)
                self.dbg("wsdd: patched for Android (NetlinkAddressMonitor)")
            else:
                self.dbg("wsdd: already patched")
        except Exception as e:
            self.dbg(f"wsdd: patch failed: {e}")

    def _start_wsdd_discovery(self):
        """Best-effort: WS-Discovery so Mi app/Windows can auto-discover the NAS.
        Android SELinux blocks netlink/mcast setup for the app uid -> run wsdd
        as root via su (Magisk grant); falls back to plain launch without root."""
        try:
            import shutil as _sh
            wsdd = _sh.which("wsdd")
            self.dbg(f"wsdd: which()='{wsdd}'")
            if not wsdd:
                self.dbg("wsdd: not installed (git clone github.com/christgau/wsdd -> $PREFIX/bin/wsdd)")
                return
            self._patch_wsdd(wsdd)
            python = _sh.which("python") or _sh.which("python3")
            if not python:
                self.dbg("wsdd: no python interpreter found")
                return
            su = _sh.which("su")
            # Idempotent: app startup path may invoke this twice - keep the
            # first instance (single log writer) instead of kill+restart churn.
            # Primary check = /proc scan (this phone's `pkill -0` is broken).
            running = False
            try:
                me = str(os.getpid())
                for ent in os.listdir("/proc"):
                    if not ent.isdigit() or ent == me:
                        continue
                    try:
                        with open(os.path.join("/proc", ent, "cmdline"), "rb") as f:
                            c = f.read().replace(b"\0", b" ").decode(errors="replace")
                        if "wsdd" in c and "app.py" not in c and "pkill" not in c:
                            running = True
                            break
                    except Exception:
                        continue
            except Exception as e:
                self.dbg(f"wsdd: running-check failed: {e}")
            if not running and su:
                running = subprocess.run([su, "-c", "pkill -0 -f '[b]in/wsdd'"],
                                         capture_output=True, timeout=10).returncode == 0
            if not running:
                running = subprocess.run(["pkill", "-0", "-f", "[b]in/wsdd"],
                                         capture_output=True, timeout=5).returncode == 0
            if running:
                self.dbg("wsdd: already running - keeping existing instance")
                self.logger.info("[OK] WS-Discovery running (auto-discovery for Mi app)")
                return
            # Kill any stale leftovers, then start fresh
            if su:
                subprocess.run([su, "-c", "pkill -f '[b]in/wsdd'"],
                               capture_output=True, timeout=10)
            subprocess.run(["pkill", "-f", "[b]in/wsdd"],
                           capture_output=True, timeout=5)
            time.sleep(0.5)
            log_file = os.path.join(os.path.expanduser("~"), ".smb", "wsdd.log")
            cmd = [su, "-c", f"{python} {wsdd} -v"] if su else [python, wsdd, "-v"]
            with open(log_file, "w") as lf:
                p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
            self.dbg(f"wsdd: started pid={p.pid} root={bool(su)} cmd={' '.join(cmd)}")
            self.logger.info("[OK] WS-Discovery started (auto-discovery for Mi app)")
        except Exception as e:
            self.dbg(f"wsdd: failed: {e}")

    def _setup_samba_windows(self):
        try:
            self.logger.info("Setting up Samba on Windows...")
            share_dir = os.path.expanduser("~/DCIM/CameraNAS")
            Path(share_dir).mkdir(parents=True, exist_ok=True)

            try:
                from smb.ServeMode import ServeMode
                self.logger.info("pysmb available - will use SMB server")
            except ImportError:
                self.logger.warning("pysmb not installed - using HTTP server fallback")

            self.logger.info(f"Windows share directory: {share_dir}")
            return True
        except Exception as e:
            self.logger.error(f"Windows setup failed: {e}")
            return False

    def start_samba(self):
        """Start both HTTP (8080) and SMB (445) servers on all platforms"""
        plat = self.detect_platform()
        self.dbg(f"start_samba: platform={plat} is_termux={is_termux()}")
        self.logger.info(f"Starting NAS on {plat}...")
        try:
            # ALWAYS start HTTP server (port 8080) - works everywhere
            self._start_http_server()
            self.dbg(f"start_samba: http_port8080={'OPEN' if self._check_port(8080) else 'closed'}")

            # ALSO try to start SMB (port 445) - platform specific
            if plat == "termux":
                self._start_smb_termux()
            elif plat == "linux":
                self._start_samba_linux()
            elif plat == "windows":
                self._start_samba_windows()
            else:
                self.logger.warning(f"Unknown platform: {plat}, HTTP only")

            time.sleep(2)
            smb_running = self.check_samba_running()
            http_running = self._check_http_running()
            ip = self.get_samba_ip()
            smb_port = self._smb_port()
            port_sfx = "" if smb_port == 445 else f":{smb_port}"
            share_name = self.config.get('samba', {}).get('share_name', 'CameraNAS')

            if smb_running:
                self.logger.info(f"[OK] SMB (port {smb_port}) RUNNING on {ip} -> Mi app: smb://{ip}{port_sfx}/{share_name}")
            else:
                self.logger.warning(f"[WARN] SMB (port {smb_port}) NOT RUNNING -> Mi app will NOT discover NAS")

            if http_running:
                self.logger.info(f"[OK] HTTP (port 8080) RUNNING on {ip} -> Browser: http://{ip}:8080/")
            else:
                self.logger.error("[FAIL] HTTP (port 8080) NOT RUNNING")

            self.logger.info(f"NAS Status: SMB={'RUNNING' if smb_running else 'STOPPED'} | HTTP={'RUNNING' if http_running else 'STOPPED'} | IP={ip}")

            self.samba_running = smb_running
            return smb_running or http_running

        except Exception as e:
            self.logger.error(f"Failed to start NAS: {e}")
            self._start_http_server()
            self.samba_running = False
            return True  # HTTP works

    def _check_http_running(self):
        """Check if HTTP server is running on port 8080"""
        return self._check_port(8080)

    def _start_samba_linux(self):
        # Write config first (so the CameraNAS share actually exists)
        try:
            share_dir = "/srv/DCIM/CameraNAS"
            Path(share_dir).mkdir(parents=True, exist_ok=True)
            share_name = self.config.get('samba', {}).get('share_name', 'CameraNAS')
            samba_config = f"""[global]
   workgroup = WORKGROUP
   server string = CameraNAS Server
   security = user
   map to guest = bad user
   dns proxy = no

[{share_name}]
   path = {share_dir}
   browseable = yes
   read only = no
   guest ok = yes
   create mask = 0755
"""
            tmp = os.path.join(os.path.expanduser("~"), ".smb", "smb.conf")
            Path(os.path.dirname(tmp)).mkdir(parents=True, exist_ok=True)
            with open(tmp, "w") as f:
                f.write(samba_config)
            subprocess.run(["sudo", "cp", tmp, "/etc/samba/smb.conf"],
                           capture_output=True, text=True, timeout=10)
            self.logger.info(f"[OK] Linux Samba config written: share={share_dir}")
        except Exception as e:
            self.logger.warning(f"Linux config write skipped: {e}")

        methods = [
            ["sudo", "service", "smbd", "start"],
            ["sudo", "systemctl", "start", "smbd"],
            ["sudo", "service", "samba", "start"],
            ["smbd", "-D"],
        ]
        for cmd in methods:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    self.logger.info(f"[OK] Started with: {' '.join(cmd)}")
                    return
            except Exception:
                continue
        self.logger.warning("No method worked for starting Samba on Linux")

    def _start_samba_windows(self):
        """Start SMB server on Windows - tries pysmb first, then Windows sharing"""
        try:
            share_dir = os.path.expanduser("~/DCIM/CameraNAS")
            Path(share_dir).mkdir(parents=True, exist_ok=True)

            # Try pysmb first (if installed)
            try:
                from smb.SMBConnection import SMBConnection
                from smb.smb_constants import SMB_PORT
                from socketserver import ThreadingTCPServer
                from smb.SMBHandler import SMBHandler

                class WindowsSMBHandler(SMBHandler):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)

                server = ThreadingTCPServer(("0.0.0.0", 445), WindowsSMBHandler)
                server.share_path = share_dir
                server.share_name = self.config.get('samba', {}).get('share_name', 'CameraNAS')
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.logger.info("[OK] Windows SMB server started on port 445 via pysmb")
                return
            except ImportError:
                self.logger.info("pysmb not available, trying Windows native sharing...")
            except Exception as e:
                self.logger.warning(f"pysmb SMB failed: {e}")

            # Fallback: Windows native sharing (requires admin)
            try:
                import subprocess as _sp
                share_name = self.config.get('samba', {}).get('share_name', 'CameraNAS')
                # Try to create share using PowerShell
                ps_cmd = f'New-SmbShare -Name "{share_name}" -Path "{share_dir}" -FullAccess "Everyone" -ErrorAction SilentlyContinue'
                result = _sp.run(["powershell", "-Command", ps_cmd], capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    self.logger.info("[OK] Windows SMB share created via PowerShell")
                else:
                    self.logger.warning("Windows native sharing needs Admin - using HTTP fallback")
            except Exception as e:
                self.logger.warning(f"Windows sharing setup failed: {e}")

        except Exception as e:
            self.logger.warning(f"Windows SMB setup failed: {e}")

    def _start_http_server(self):
        if getattr(self, '_http_server_started', False):
            if self._check_port(8080):
                self.dbg("http: already started (skip)")
                return
            # thread died somehow - allow rebind below
            self.dbg("http: flag set but port 8080 dead - rebinding")
            self._http_server_started = False
        try:
            import http.server
            import socketserver
            if is_termux():
                share_dir = os.path.expanduser("~/storage/shared/DCIM/CameraNAS")
            else:
                share_dir = os.path.expanduser("~/DCIM/CameraNAS")
            Path(share_dir).mkdir(parents=True, exist_ok=True)
            port = 8080
            # NOTE: never os.chdir() here - relative config paths would nest
            # (share/DCIM/CameraNAS/DCIM/CameraNAS). Serve via directory= instead.
            import functools
            Handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                        directory=share_dir)
            socketserver.TCPServer.allow_reuse_address = True
            httpd = socketserver.TCPServer(("0.0.0.0", port), Handler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self._http_server_started = True
            self.dbg(f"http: serving '{share_dir}' on 0.0.0.0:{port}")
            ip = self.get_samba_ip()
            self.logger.info(f"HTTP server started on 0.0.0.0:{port}")
            self.logger.info(f"Access: http://{ip}:{port}/")
            self.logger.info(f"  Mi camera: http://{ip}:{port}/xiaomi_camera_videos/")
            self.logger.info(f"  CP Plus:   http://{ip}:{port}/cpplus_videos/")
        except OSError as e:
            self.dbg(f"http: FAILED {type(e).__name__}: {e}")
            if getattr(e, "errno", None) == 98 or "in use" in str(e).lower():
                self.logger.error(
                    "HTTP port 8080 held by ANOTHER app.py instance "
                    "(stale zombie) — this instance cannot serve. "
                    "Fix: pkill -f app.py, then start ONE instance")
            else:
                self.logger.error(f"HTTP server failed: {e}")
        except Exception as e:
            self.dbg(f"http: FAILED {type(e).__name__}: {e}")
            self.logger.error(f"HTTP server failed: {e}")

    def _check_port(self, port, host="127.0.0.1"):
        """Check if a TCP port is listening"""
        try:
            import socket
            s = socket.socket()
            s.settimeout(2)
            result = s.connect_ex((host, port))
            s.close()
            return result == 0
        except Exception:
            return False

    def check_samba_running(self):
        # Most reliable: is the SMB port actually listening?
        smb_port = self._smb_port()
        port_ok = self._check_port(smb_port)
        if port_ok:
            self.dbg(f"check_samba: port {smb_port} OPEN -> RUNNING")
            return True
        checks = []
        try:
            if shutil.which("pgrep"):
                for args in (["pgrep", "-x", "smbd"], ["pgrep", "-f", "smbd"]):
                    result = subprocess.run(args, capture_output=True, text=True, timeout=5)
                    checks.append(f"{' '.join(args)} rc={result.returncode}")
                    if result.returncode == 0:
                        self.dbg(f"check_samba: {' '.join(args)} matched -> RUNNING ({'; '.join(checks)})")
                        return True
            else:
                checks.append("pgrep not found")
            if shutil.which("netstat"):
                result = subprocess.run(["netstat", "-tlnp"], capture_output=True, text=True, timeout=5)
                hit = f":{smb_port}" in result.stdout or ":139" in result.stdout
                checks.append(f"netstat hit={hit}")
                if hit:
                    self.dbg(f"check_samba: netstat shows port {smb_port}/139 -> RUNNING ({'; '.join(checks)})")
                    return True
            else:
                checks.append("netstat not found")
            if shutil.which("ss"):
                result = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
                hit = f":{smb_port}" in result.stdout or ":139" in result.stdout
                checks.append(f"ss hit={hit}")
                if hit:
                    self.dbg(f"check_samba: ss shows port {smb_port}/139 -> RUNNING ({'; '.join(checks)})")
                    return True
            else:
                checks.append("ss not found")
            self.dbg(f"check_samba: ALL FAILED -> STOPPED ({'; '.join(checks)})")
            return False
        except Exception as e:
            self.dbg(f"check_samba: EXCEPTION {e} ({'; '.join(checks)})")
            return False

    def get_samba_ip(self):
        """Get the local IP address for SMB/HTTP access - works on Termux/Linux/Windows"""
        # Method 1: Termux WiFi IP
        try:
            if is_termux():
                result = subprocess.run(["ip", "addr", "show", "wlan0"], capture_output=True, text=True, timeout=5)
                for line in result.stdout.split('\n'):
                    if 'inet ' in line and '127.0.0.1' not in line:
                        return line.strip().split()[1].split('/')[0]
        except Exception:
            pass
        # Method 2: Socket trick - works on ALL platforms (Termux/Linux/Windows)
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 53))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        # Method 3: Windows ipconfig
        try:
            if platform.system() == "Windows":
                result = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5)
                for line in result.stdout.split('\n'):
                    if "IPv4" in line and "127.0.0.1" not in line:
                        return line.split(":")[-1].strip()
        except Exception:
            pass
        # Method 4: Linux hostname -I
        try:
            result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
            for ip in result.stdout.strip().split():
                if not ip.startswith("127."):
                    return ip
        except Exception:
            pass
        return "127.0.0.1"

    def get_share_path(self):
        if is_termux():
            return os.path.join(os.path.expanduser("~/storage/shared"), "DCIM", "CameraNAS")
        return os.path.join(get_google_photos_root())

    def get_share_access_path(self):
        if is_termux():
            return "/storage/emulated/0/DCIM/CameraNAS"
        return self.get_share_path()

    def get_share_url(self):
        ip = self.get_samba_ip()
        share_name = self.config.get('samba', {}).get('share_name', 'CameraNAS')
        port = self._smb_port()
        port_sfx = "" if port == 445 else f":{port}"
        return f"//{ip}{port_sfx}/{share_name}"

    def get_samba_info(self):
        creds = self.get_samba_credentials()
        return {
            "share_path": self.get_share_path(),
            "phone_path": self.get_share_access_path(),
            "smb_url": self.get_share_url(),
            "ip": self.get_samba_ip(),
            "username": creds["username"],
            "password": creds["password"],
            "share_name": creds["share_name"],
            "samba_running": self.check_samba_running(),
        }

    def print_nas_access(self):
        running = self.check_samba_running()
        ip = self.get_samba_ip()
        info = self.get_samba_info()
        if running:
            status = "RUNNING [OK]"
            mode = "SMB"
            fallback = ""
        else:
            status = "HTTP FALLBACK [OK]"
            mode = "HTTP"
            fallback = f"  HTTP:       http://{ip}:8080/"
        print("=" * 50)
        print("  NAS Status")
        print("=" * 50)
        print(f"  Mode:           {mode}")
        print(f"  Status:         {status}")
        print(f"  Share Path:     {info['share_path']}")
        print(f"  Phone Path:     {info['phone_path']}")
        print(f"  SMB URL:        {info['smb_url']}")
        print(f"  Username:       {info['username']}")
        if fallback:
            print(f"  {fallback}")
            print(f"  xiaomi_camera_videos: http://{ip}:8080/xiaomi_camera_videos/")
            print(f"  cpplus_videos:   http://{ip}:8080/cpplus_videos/")
        print("=" * 50)

    def verify_nas_access(self):
        share_path = self.get_share_path()
        if os.path.exists(share_path):
            test_file = os.path.join(share_path, ".nas_test")
            try:
                with open(test_file, "w") as f:
                    f.write("test")
                os.remove(test_file)
                self.logger.info("NAS access verified")
                return True
            except Exception:
                pass
        self.logger.warning("NAS access verification failed")
        return False

    def get_status(self):
        info = self.get_samba_info()
        return {
            "samba_running": info["samba_running"],
            "platform": self.detect_platform(),
            "share_path": info["share_path"],
            "phone_path": info["phone_path"],
            "smb_url": info["smb_url"],
            "ip": info["ip"],
            "username": info["username"],
            "password": info["password"],
            "share_name": info["share_name"],
            "verified": self.verify_nas_access()
        }