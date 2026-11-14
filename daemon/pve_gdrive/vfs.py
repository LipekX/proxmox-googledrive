"""Per-storage rclone VFS mount supervisor, crypt layering, statvfs cap, and RC queue."""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    Config,
    atomic_write_json,
    atomic_write_text,
    ensure_dir,
    read_json,
    validate_store_id,
)
from .gdrive import DriveClient
from .oauth import OAuthManager

# Rclone's documented public fixed AES-256-CTR obscure key.
_RCLONE_OBSCURE_KEY_HEX = (
    "9c935b48730a554d6bfd7c63c886a92bd390198eb8128afbf4de162b8b95f638"
)
_AUTO_CAP_MAX_BYTES = 20 * 1024**3  # 20 GiB
_AUTO_CAP_FREE_RATIO = 0.25

_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?i?B?)?\s*$", re.IGNORECASE)
_SIZE_MULTIPLIERS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1000,
    "KI": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1000**2,
    "MI": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1000**3,
    "GI": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1000**4,
    "TI": 1024**4,
    "TIB": 1024**4,
}


def parse_size(value: Any) -> int:
    """Parse a human-readable size (e.g. '20G', '512Mi', '1048576') into bytes."""
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"Size cannot be negative: {value!r}")
        return int(value)
    text = str(value or "").strip()
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"Invalid size specification: {value!r}")
    num = float(match.group(1))
    unit = (match.group(2) or "").upper()
    if unit not in _SIZE_MULTIPLIERS:
        raise ValueError(f"Unsupported size unit in {value!r}")
    return int(num * _SIZE_MULTIPLIERS[unit])


def format_rclone_size(num_bytes: int) -> str:
    """Format byte count into an exact rclone size string."""
    if num_bytes <= 0:
        return "0B"
    if num_bytes % (1024**3) == 0:
        return f"{num_bytes // (1024**3)}G"
    if num_bytes % (1024**2) == 0:
        return f"{num_bytes // (1024**2)}M"
    if num_bytes % 1024 == 0:
        return f"{num_bytes // 1024}K"
    return f"{num_bytes}B"


def _existing_stat_path(path: str) -> str:
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return probe


def compute_cache_cap(
    cache_dir: str, cache_max_size: Optional[str] = None
) -> Dict[str, Any]:
    """Run pre-flight statvfs check and compute min(20G, 25% free) when unset."""
    probe = _existing_stat_path(cache_dir)
    st = os.statvfs(probe)
    free_bytes = int(st.f_frsize * st.f_bavail)
    total_fs_bytes = int(st.f_frsize * st.f_blocks)

    if cache_max_size is not None and str(cache_max_size).strip() != "":
        cap_bytes = parse_size(cache_max_size)
        auto_capped = False
    else:
        cap_bytes = min(_AUTO_CAP_MAX_BYTES, int(free_bytes * _AUTO_CAP_FREE_RATIO))
        auto_capped = True

    return {
        "cap_bytes": cap_bytes,
        "cap_rclone": format_rclone_size(cap_bytes),
        "free_bytes": free_bytes,
        "total_fs_bytes": total_fs_bytes,
        "auto_capped": auto_capped,
    }


def obscure_rclone_password(
    plaintext: str,
    rclone_bin: str = "rclone",
    openssl_bin: str = "openssl",
) -> str:
    """Obscure a password in rclone's AES-256-CTR format."""
    if shutil.which(rclone_bin):
        proc = subprocess.run(
            [rclone_bin, "obscure", plaintext],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()

    iv = os.urandom(16)
    proc = subprocess.run(
        [
            openssl_bin,
            "enc",
            "-aes-256-ctr",
            "-K",
            _RCLONE_OBSCURE_KEY_HEX,
            "-iv",
            iv.hex(),
            "-nopad",
        ],
        input=plaintext.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Failed to obscure rclone crypt password: {err}")
    return base64.urlsafe_b64encode(iv + proc.stdout).rstrip(b"=").decode("ascii")


class _UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP/1.1 client connection over a Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = 1.5) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _allocate_rc_addr() -> str:
    """Allocate an ephemeral localhost TCP address for rclone --rc-addr."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"127.0.0.1:{port}"


def _rc_reachable(rc_addr: str, timeout: float = 0.1) -> bool:
    """Return True if a TCP listener is accepting connections at rc_addr."""
    try:
        host, port_str = rc_addr.rsplit(":", 1)
        with socket.create_connection((host, int(port_str)), timeout=timeout):
            return True
    except OSError:
        return False


def rc_call(
    rc_sock_path: str,
    endpoint: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 1.2,
) -> Dict[str, Any]:
    """Send a JSON POST request to rclone's RC endpoint (TCP addr file or Unix socket)."""
    conn: http.client.HTTPConnection
    if os.path.exists(rc_sock_path):
        st_mode = os.stat(rc_sock_path).st_mode
        if stat.S_ISSOCK(st_mode):
            conn = _UnixHTTPConnection(rc_sock_path, timeout=timeout)
        else:
            with open(rc_sock_path, "r", encoding="utf-8") as fh:
                addr = fh.read().strip()
            if not addr or ":" not in addr:
                raise FileNotFoundError(f"Invalid RC address in {rc_sock_path}")
            host, port_str = addr.rsplit(":", 1)
            conn = http.client.HTTPConnection(host, int(port_str), timeout=timeout)
    elif ":" in rc_sock_path and not rc_sock_path.startswith("/"):
        host, port_str = rc_sock_path.rsplit(":", 1)
        conn = http.client.HTTPConnection(host, int(port_str), timeout=timeout)
    else:
        raise FileNotFoundError(f"RC socket not found: {rc_sock_path}")
    try:
        body = json.dumps(payload or {}).encode("utf-8")
        path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        conn.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 400:
            raise RuntimeError(f"rclone RC {path} HTTP {resp.status}: {raw}")
        return json.loads(raw) if raw else {}
    finally:
        conn.close()


def _dir_size_bytes(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for root, _, files in os.walk(path):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
    return total


class _ManagedMount:
    """Tracks a supervised rclone mount subprocess."""

    def __init__(self, storeid: str, cmd: List[str], rc_sock: str, mount_point: str, cache_dir: str, cache_max: int) -> None:
        self.storeid = storeid
        self.cmd = cmd
        self.rc_sock = rc_sock
        self.mount_point = mount_point
        self.cache_dir = cache_dir
        self.cache_max = cache_max
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.active = True
        self.started_at = time.time()
        self.restart_count = 0
        self.last_error = ""


class VFSSupervisor:
    """Manages per-storage rclone configs, statvfs cache caps, mounts, and upload tracking."""

    def __init__(
        self,
        config: Config,
        oauth: OAuthManager,
        drive: DriveClient,
    ) -> None:
        self.config = config
        self.oauth = oauth
        self.drive = drive
        self._lock = threading.RLock()
        self._mounts: Dict[str, _ManagedMount] = {}
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def _storage_meta_path(self, storeid: str) -> str:
        safe = validate_store_id(storeid)
        return os.path.join(self.config.storages_dir, f"{safe}.json")

    def _crypt_path(self, storeid: str) -> str:
        safe = validate_store_id(storeid)
        return os.path.join(self.config.storages_dir, f"{safe}.crypt.json")

    def _rclone_conf_path(self, storeid: str) -> str:
        safe = validate_store_id(storeid)
        return os.path.join(self.config.run_dir, f"rclone-{safe}.conf")

    def _ephemeral_sa_path(self, storeid: str) -> str:
        safe = validate_store_id(storeid)
        return os.path.join(self.config.run_dir, f"sa-{safe}.json")

    def _rc_sock_path(self, storeid: str) -> str:
        safe = validate_store_id(storeid)
        return os.path.join(self.config.run_dir, f"rc-{safe}.sock")

    def mount_point_for(self, storeid: str) -> str:
        safe = validate_store_id(storeid)
        return os.path.join(self.config.mnt_base, safe)

    def get_storage_meta(self, storeid: str) -> Dict[str, Any]:
        """Read saved storage metadata from disk."""
        data = read_json(self._storage_meta_path(storeid))
        if not isinstance(data, dict):
            raise FileNotFoundError(f"Storage {storeid!r} is not initialized")
        return data

    def init_storage(
        self,
        storeid: str,
        account: str,
        drive_id: str = "",
        subfolder: str = "",
        root_folder_id: Optional[str] = None,
        cache_dir: Optional[str] = None,
        cache_max_size: Optional[str] = None,
        encrypt: bool = False,
        async_upload: bool = False,
        crypt_password: Optional[str] = None,
        crypt_password2: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Initialize remote Google Drive folder layout and persist pinned storage metadata."""
        safe_storeid = validate_store_id(storeid)
        # Validate account exists.
        self.oauth.get_account(account)

        layout = self.drive.ensure_storage_layout(
            account_id=account,
            drive_id=drive_id or "",
            subfolder=subfolder or "",
            pinned_root_folder_id=(root_folder_id or "").strip() or None,
        )

        meta = {
            "storeid": safe_storeid,
            "account": account,
            "drive_id": drive_id or "",
            "subfolder": subfolder or "",
            "root_folder_id": layout["root_folder_id"],
            "proxmox_folder_id": layout["proxmox_folder_id"],
            "folders": layout["folders"],
            "cache_dir": (cache_dir or self.config.cache_dir).strip(),
            "cache_max_size": (cache_max_size or "").strip(),
            "encrypt": bool(encrypt),
            "async_upload": bool(async_upload),
            "updated_at": int(time.time()),
        }

        ensure_dir(self.config.storages_dir, mode=0o700)
        atomic_write_json(self._storage_meta_path(safe_storeid), meta, mode=0o600)

        if crypt_password:
            crypt_data: Dict[str, str] = {"password": str(crypt_password)}
            if crypt_password2:
                crypt_data["password2"] = str(crypt_password2)
            atomic_write_json(self._crypt_path(safe_storeid), crypt_data, mode=0o600)

        return {
            "ok": True,
            "storeid": safe_storeid,
            "root_folder_id": layout["root_folder_id"],
            "proxmox_folder_id": layout["proxmox_folder_id"],
            "folders": layout["folders"],
        }

    def _read_crypt_credentials(self, storeid: str) -> Tuple[str, str]:
        crypt_data = read_json(self._crypt_path(storeid))
        if not isinstance(crypt_data, dict):
            raise FileNotFoundError(
                f"Encryption enabled for {storeid!r}, but {self._crypt_path(storeid)} does not exist"
            )
        pw = str(
            crypt_data.get("password")
            or crypt_data.get("passphrase")
            or crypt_data.get("crypt_password")
            or ""
        )
        if not pw:
            raise ValueError(f"Empty encryption password in {self._crypt_path(storeid)}")
        pw2 = str(
            crypt_data.get("password2")
            or crypt_data.get("salt")
            or crypt_data.get("crypt_password2")
            or ""
        )
        return pw, pw2

    def write_rclone_config(self, storeid: str, meta: Dict[str, Any]) -> Tuple[str, str]:
        """Write ephemeral rclone-<storeid>.conf (0600) and return (conf_path, remote_target)."""
        safe_storeid = validate_store_id(storeid)
        account_id = str(meta["account"])
        profile = self.oauth.get_account(account_id)
        ensure_dir(self.config.run_dir, mode=0o700)

        base_section = f"gdrive_{safe_storeid}"
        lines: List[str] = [
            f"[{base_section}]",
            "type = drive",
            f"scope = {profile.get('scope', 'drive.file')}",
        ]

        if profile.get("mode") == "service_account":
            sa_path = self._ephemeral_sa_path(safe_storeid)
            sa_obj = {
                "type": "service_account",
                "project_id": profile.get("project_id", ""),
                "private_key_id": profile.get("private_key_id", ""),
                "private_key": profile["private_key"],
                "client_email": profile["client_email"],
                "token_uri": profile.get("token_uri", self.config.google_token_url),
            }
            atomic_write_json(sa_path, sa_obj, mode=0o600)
            lines.append(f"service_account_file = {sa_path}")
        else:
            access_token = self.oauth.get_access_token(account_id)
            expiry_ts = self.oauth.get_cached_token_expiry(account_id) or (
                time.time() + 3600
            )
            expiry_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(expiry_ts)
            )
            token_blob = json.dumps(
                {
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "refresh_token": profile.get("refresh_token", ""),
                    "expiry": expiry_iso,
                },
                separators=(",", ":"),
            )
            if profile.get("client_id"):
                lines.append(f"client_id = {profile['client_id']}")
            if profile.get("client_secret"):
                lines.append(f"client_secret = {profile['client_secret']}")
            lines.append(f"token = {token_blob}")

        if meta.get("root_folder_id"):
            lines.append(f"root_folder_id = {meta['root_folder_id']}")
        if meta.get("drive_id"):
            lines.append(f"team_drive = {meta['drive_id']}")

        remote_target = f"{base_section}:"

        if meta.get("encrypt"):
            pw, pw2 = self._read_crypt_credentials(safe_storeid)
            obs_pw = obscure_rclone_password(
                pw, rclone_bin=self.config.rclone_bin, openssl_bin=self.config.openssl_bin
            )
            crypt_section = f"crypt_{safe_storeid}"
            lines.extend(
                [
                    "",
                    f"[{crypt_section}]",
                    "type = crypt",
                    f"remote = {base_section}:",
                    "filename_encryption = standard",
                    "directory_name_encryption = true",
                    f"password = {obs_pw}",
                ]
            )
            if pw2:
                obs_pw2 = obscure_rclone_password(
                    pw2,
                    rclone_bin=self.config.rclone_bin,
                    openssl_bin=self.config.openssl_bin,
                )
                lines.append(f"password2 = {obs_pw2}")
            remote_target = f"{crypt_section}:"

        conf_path = self._rclone_conf_path(safe_storeid)
        atomic_write_text(conf_path, "\n".join(lines) + "\n", mode=0o600)
        return conf_path, remote_target

    def build_rclone_command(
        self,
        storeid: str,
        remote_target: str,
        conf_path: str,
        mount_point: str,
        storage_cache_dir: str,
        cap_rclone: str,
        rc_sock: str,
        rc_addr: Optional[str] = None,
    ) -> List[str]:
        """Build the supervised rclone mount command line."""
        if not rc_addr:
            rc_addr = _allocate_rc_addr()
        return [
            self.config.rclone_bin,
            "mount",
            remote_target,
            mount_point,
            "--config",
            conf_path,
            "--cache-dir",
            storage_cache_dir,
            "--vfs-cache-mode",
            "full",
            "--vfs-cache-max-size",
            cap_rclone,
            "--vfs-cache-max-age",
            "1h",
            "--allow-other",
            "--rc",
            "--rc-addr",
            rc_addr,
            "--rc-no-auth",
        ]

    def _start_monitor_locked(self) -> None:
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="vfs-supervisor", daemon=True
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(2.0):
            with self._lock:
                items = list(self._mounts.items())
            for storeid, managed in items:
                if not managed.active or managed.process is None:
                    continue
                rc = managed.process.poll()
                if rc is not None and managed.active:
                    managed.last_error = f"rclone exited with code {rc}"
                    managed.restart_count += 1
                    try:
                        meta = self.get_storage_meta(storeid)
                        conf_path, _ = self.write_rclone_config(storeid, meta)
                        managed.cmd[managed.cmd.index("--config") + 1] = conf_path
                        managed.process = subprocess.Popen(
                            managed.cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE,
                        )
                    except Exception as exc:
                        managed.last_error = str(exc)

    def activate_storage(
        self,
        storeid: str,
        account: Optional[str] = None,
        drive_id: Optional[str] = None,
        subfolder: Optional[str] = None,
        root_folder_id: Optional[str] = None,
        cache_dir: Optional[str] = None,
        cache_max_size: Optional[str] = None,
        encrypt: Optional[bool] = None,
        async_upload: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Activate and mount a storage via supervised rclone mount."""
        safe_storeid = validate_store_id(storeid)

        try:
            meta = self.get_storage_meta(safe_storeid)
        except FileNotFoundError:
            if not account:
                raise
            self.init_storage(
                storeid=safe_storeid,
                account=account,
                drive_id=drive_id or "",
                subfolder=subfolder or "",
                root_folder_id=root_folder_id,
                cache_dir=cache_dir,
                cache_max_size=cache_max_size,
                encrypt=bool(encrypt),
                async_upload=bool(async_upload),
            )
            meta = self.get_storage_meta(safe_storeid)

        # Apply any updated options passed on activate.
        updated = False
        for key, val in (
            ("account", account),
            ("drive_id", drive_id),
            ("subfolder", subfolder),
            ("root_folder_id", root_folder_id),
            ("cache_dir", cache_dir),
            ("cache_max_size", cache_max_size),
        ):
            if val is not None and val != meta.get(key):
                meta[key] = val
                updated = True
        for key, val in (("encrypt", encrypt), ("async_upload", async_upload)):
            if val is not None and bool(val) != bool(meta.get(key)):
                meta[key] = bool(val)
                updated = True
        if updated:
            ensure_dir(self.config.storages_dir, mode=0o700)
            atomic_write_json(self._storage_meta_path(safe_storeid), meta, mode=0o600)

        with self._lock:
            existing = self._mounts.get(safe_storeid)
            if existing and existing.active and existing.process and existing.process.poll() is None:
                return {
                    "ok": True,
                    "storeid": safe_storeid,
                    "mount_point": existing.mount_point,
                    "cache_max": existing.cache_max,
                    "active": True,
                }

        base_cache_dir = meta.get("cache_dir") or self.config.cache_dir
        ensure_dir(base_cache_dir, mode=0o700)
        storage_cache_dir = os.path.join(base_cache_dir, safe_storeid)
        ensure_dir(storage_cache_dir, mode=0o700)

        cap_info = compute_cache_cap(
            storage_cache_dir, meta.get("cache_max_size") or None
        )
        if cap_info["free_bytes"] <= 0:
            raise RuntimeError(
                f"No free disk space on cache filesystem for {storage_cache_dir}"
            )

        mount_point = self.mount_point_for(safe_storeid)
        ensure_dir(mount_point, mode=0o755)

        conf_path, remote_target = self.write_rclone_config(safe_storeid, meta)
        rc_sock = self._rc_sock_path(safe_storeid)
        if os.path.exists(rc_sock):
            os.unlink(rc_sock)
        rc_addr = _allocate_rc_addr()
        atomic_write_text(rc_sock, rc_addr + "\n", mode=0o600)

        rclone_resolved = shutil.which(self.config.rclone_bin) or (
            self.config.rclone_bin if os.path.isfile(self.config.rclone_bin) else None
        )
        if not rclone_resolved:
            raise FileNotFoundError(
                f"rclone binary not found: {self.config.rclone_bin!r}"
            )

        cmd = self.build_rclone_command(
            storeid=safe_storeid,
            remote_target=remote_target,
            conf_path=conf_path,
            mount_point=mount_point,
            storage_cache_dir=storage_cache_dir,
            cap_rclone=cap_info["cap_rclone"],
            rc_sock=rc_sock,
            rc_addr=rc_addr,
        )
        cmd[0] = rclone_resolved

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Wait briefly to catch immediate startup failures.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if proc.poll() is not None:
                if os.path.exists(rc_sock):
                    try:
                        os.unlink(rc_sock)
                    except OSError:
                        pass
                stderr_out = (proc.stderr.read() if proc.stderr else b"").decode(
                    "utf-8", errors="replace"
                ).strip()
                raise RuntimeError(
                    f"rclone mount failed immediately (rc={proc.returncode}): {stderr_out}"
                )
            if os.path.ismount(mount_point) or _rc_reachable(rc_addr):
                break
            time.sleep(0.05)

        managed = _ManagedMount(
            storeid=safe_storeid,
            cmd=cmd,
            rc_sock=rc_sock,
            mount_point=mount_point,
            cache_dir=storage_cache_dir,
            cache_max=cap_info["cap_bytes"],
        )
        managed.process = proc

        with self._lock:
            self._mounts[safe_storeid] = managed
            self._start_monitor_locked()

        return {
            "ok": True,
            "storeid": safe_storeid,
            "mount_point": mount_point,
            "cache_max": cap_info["cap_bytes"],
            "cache_max_rclone": cap_info["cap_rclone"],
            "active": True,
        }

    def deactivate_storage(self, storeid: str) -> Dict[str, Any]:
        """Stop supervised rclone mount and remove ephemeral config/sockets."""
        safe_storeid = validate_store_id(storeid)
        with self._lock:
            managed = self._mounts.pop(safe_storeid, None)

        mount_point = self.mount_point_for(safe_storeid)
        if managed is not None:
            managed.active = False
            proc = managed.process
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2.0)
                if proc.stderr:
                    try:
                        proc.stderr.close()
                    except OSError:
                        pass

        if os.path.ismount(mount_point):
            for unmount_cmd in (
                ["fusermount3", "-u", "-z", mount_point],
                ["fusermount", "-u", "-z", mount_point],
                ["umount", "-l", mount_point],
            ):
                if shutil.which(unmount_cmd[0]):
                    res = subprocess.run(unmount_cmd, capture_output=True, check=False)
                    if res.returncode == 0:
                        break

        for path in (
            self._rclone_conf_path(safe_storeid),
            self._ephemeral_sa_path(safe_storeid),
            self._rc_sock_path(safe_storeid),
        ):
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError:
                pass

        return {"ok": True, "storeid": safe_storeid, "active": False}

    def shutdown_all(self) -> None:
        """Stop monitor thread and deactivate all managed mounts."""
        self._stop_event.set()
        with self._lock:
            storeids = list(self._mounts.keys())
        for sid in storeids:
            try:
                self.deactivate_storage(sid)
            except Exception:
                pass

    def _query_rc_stats(self, rc_sock: str, timeout: float) -> Tuple[bool, Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
        if not os.path.exists(rc_sock):
            return False, {}, {}, []
        try:
            vfs_stats = rc_call(rc_sock, "/vfs/stats", timeout=timeout)
            core_stats = rc_call(rc_sock, "/core/stats", timeout=timeout)
            queue_data = rc_call(rc_sock, "/vfs/queue", timeout=timeout)
            return (
                True,
                vfs_stats,
                core_stats,
                list(queue_data.get("queue") or []),
            )
        except Exception:
            return False, {}, {}, []

    def get_storage_status(self, storeid: str) -> Dict[str, Any]:
        """Return {active, total, avail, used, mounted, cache_used, cache_max} without stalling."""
        safe_storeid = validate_store_id(storeid)
        try:
            meta = self.get_storage_meta(safe_storeid)
        except FileNotFoundError:
            return {
                "active": False,
                "mounted": False,
                "total": 0,
                "avail": 0,
                "used": 0,
                "cache_used": 0,
                "cache_max": 0,
            }

        with self._lock:
            managed = self._mounts.get(safe_storeid)

        mount_point = self.mount_point_for(safe_storeid)
        proc_alive = bool(
            managed and managed.active and managed.process and managed.process.poll() is None
        )
        rc_sock = self._rc_sock_path(safe_storeid)
        rc_ok, vfs_stats, _core_stats, _queue = self._query_rc_stats(
            rc_sock, timeout=min(self.config.status_timeout, 0.5)
        )
        mounted = os.path.ismount(mount_point) or (proc_alive and rc_ok)
        active = bool(proc_alive or mounted)

        # Compute cache usage and cap.
        base_cache_dir = meta.get("cache_dir") or self.config.cache_dir
        storage_cache_dir = os.path.join(base_cache_dir, safe_storeid)
        disk_cache = vfs_stats.get("diskCache") or {}
        cache_used = int(disk_cache.get("bytesUsed") or _dir_size_bytes(storage_cache_dir))

        if managed:
            cache_max = managed.cache_max
        else:
            try:
                cap_info = compute_cache_cap(
                    storage_cache_dir, meta.get("cache_max_size") or None
                )
                cache_max = cap_info["cap_bytes"]
            except Exception:
                cache_max = 0

        # Query quota with non-blocking timeout + stale cache fallback.
        quota = self.drive.get_quota(
            str(meta["account"]),
            timeout=self.config.status_timeout,
            allow_stale=True,
        )
        if quota is None:
            total, used, avail = 0, 0, 0
        elif quota.get("unlimited"):
            used = int(quota.get("usage", 0))
            avail = 10 * 1024**4  # 10 TiB virtual headroom for unlimited Workspace
            total = used + avail
        else:
            total = int(quota.get("limit", 0))
            used = int(quota.get("usage", 0))
            avail = int(quota.get("free", 0))

        return {
            "active": active,
            "mounted": mounted,
            "total": total,
            "avail": avail,
            "used": used,
            "cache_used": cache_used,
            "cache_max": cache_max,
            "uploads_in_progress": int(disk_cache.get("uploadsInProgress") or 0),
            "uploads_queued": int(disk_cache.get("uploadsQueued") or 0),
        }

    def get_queue(self) -> Dict[str, Any]:
        """Return aggregated upload queue and per-storage cache stats."""
        uploads: List[Dict[str, Any]] = []
        storages: Dict[str, Dict[str, Any]] = {}

        storeids: set[str] = set()
        if os.path.isdir(self.config.storages_dir):
            for entry in os.listdir(self.config.storages_dir):
                if entry.endswith(".json") and not entry.endswith(".crypt.json") and not entry.startswith("."):
                    storeids.add(entry[:-5])
        with self._lock:
            storeids.update(self._mounts.keys())

        for sid in sorted(storeids):
            rc_sock = self._rc_sock_path(sid)
            rc_ok, vfs_stats, core_stats, queue_items = self._query_rc_stats(
                rc_sock, timeout=min(self.config.status_timeout, 0.8)
            )
            disk_cache = vfs_stats.get("diskCache") or {}
            transferring = {
                str(t.get("name") or ""): t
                for t in (core_stats.get("transferring") or [])
            }

            seen_names: set[str] = set()
            for item in queue_items:
                name = str(item.get("name") or "")
                seen_names.add(name)
                xfer = transferring.get(name, {})
                uploads.append(
                    {
                        "storeid": sid,
                        "name": name,
                        "size": int(item.get("size") or xfer.get("size") or 0),
                        "bytes": int(xfer.get("bytes") or 0),
                        "percentage": int(xfer.get("percentage") or 0),
                        "speed": float(xfer.get("speed") or 0.0),
                        "uploading": bool(item.get("uploading") or name in transferring),
                        "tries": int(item.get("tries") or 0),
                        "expiry": float(item.get("expiry") or 0.0),
                    }
                )
            for name, xfer in transferring.items():
                if name and name not in seen_names:
                    uploads.append(
                        {
                            "storeid": sid,
                            "name": name,
                            "size": int(xfer.get("size") or 0),
                            "bytes": int(xfer.get("bytes") or 0),
                            "percentage": int(xfer.get("percentage") or 0),
                            "speed": float(xfer.get("speed") or 0.0),
                            "uploading": True,
                            "tries": 0,
                            "expiry": 0.0,
                        }
                    )

            with self._lock:
                managed = self._mounts.get(sid)
            if managed is not None:
                cache_max = managed.cache_max
            else:
                try:
                    meta = self.get_storage_meta(sid)
                    base_cache_dir = meta.get("cache_dir") or self.config.cache_dir
                    storage_cache_dir = os.path.join(base_cache_dir, sid)
                    cache_max = compute_cache_cap(
                        storage_cache_dir, meta.get("cache_max_size") or None
                    )["cap_bytes"]
                except Exception:
                    cache_max = 0

            storages[sid] = {
                "rc_reachable": rc_ok,
                "cache_used": int(disk_cache.get("bytesUsed") or 0),
                "cache_max": cache_max,
                "uploads_in_progress": int(disk_cache.get("uploadsInProgress") or 0),
                "uploads_queued": int(disk_cache.get("uploadsQueued") or 0),
            }

        return {"uploads": uploads, "storages": storages}

    def wait_for_upload(
        self,
        storeid: str,
        path: str,
        timeout_sec: float = 3600.0,
        poll_interval: float = 0.25,
    ) -> Dict[str, Any]:
        """Block until path finishes uploading via rclone VFS RC queue, failing loudly on error."""
        safe_storeid = validate_store_id(storeid)
        mount_point = os.path.abspath(self.mount_point_for(safe_storeid))
        raw_path = (path or "").strip()
        if not raw_path:
            raise ValueError("path is required")

        if os.path.isabs(raw_path):
            abs_target = os.path.abspath(raw_path)
            if abs_target == mount_point or abs_target.startswith(mount_point + os.sep):
                rel_path = os.path.relpath(abs_target, mount_point)
            else:
                rel_path = raw_path.lstrip("/")
        else:
            rel_path = raw_path.lstrip("/")

        rel_posix = rel_path.replace(os.sep, "/")
        basename = os.path.basename(rel_posix)
        rc_sock = self._rc_sock_path(safe_storeid)

        deadline = time.time() + float(timeout_sec)
        while True:
            with self._lock:
                managed = self._mounts.get(safe_storeid)
            if managed and managed.process is not None and managed.process.poll() is not None:
                raise RuntimeError(
                    f"rclone mount for {safe_storeid!r} exited while waiting for {rel_posix}"
                )

            if not os.path.exists(rc_sock):
                raise RuntimeError(
                    f"Storage {safe_storeid!r} RC socket is unavailable ({rc_sock})"
                )

            vfs_stats = rc_call(rc_sock, "/vfs/stats", timeout=2.0)
            core_stats = rc_call(rc_sock, "/core/stats", timeout=2.0)
            queue_data = rc_call(rc_sock, "/vfs/queue", timeout=2.0)

            queue_items = list(queue_data.get("queue") or [])
            transferring = list(core_stats.get("transferring") or [])
            fatal_errors = int(core_stats.get("fatalError") or 0)
            errors_count = int(core_stats.get("errors") or 0)

            matching_q = [
                item
                for item in queue_items
                if str(item.get("name") or "").lstrip("/") in (rel_posix, basename)
            ]
            matching_xfer = [
                item
                for item in transferring
                if str(item.get("name") or "").lstrip("/") in (rel_posix, basename)
            ]

            for item in matching_q:
                if int(item.get("tries") or 0) > 0 and item.get("error"):
                    raise RuntimeError(
                        f"Upload failed for {rel_posix}: {item.get('error')}"
                    )

            if fatal_errors > 0 and (matching_q or matching_xfer):
                last_err = core_stats.get("lastError") or "rclone fatal transfer error"
                raise RuntimeError(f"Upload failed for {rel_posix}: {last_err}")

            disk_cache = vfs_stats.get("diskCache") or {}
            in_prog = int(disk_cache.get("uploadsInProgress") or 0)
            queued = int(disk_cache.get("uploadsQueued") or 0)

            if not matching_q and not matching_xfer and in_prog == 0 and queued == 0:
                if errors_count > 0 and core_stats.get("lastError"):
                    raise RuntimeError(
                        f"Upload error on {safe_storeid}: {core_stats['lastError']}"
                    )
                return {
                    "ok": True,
                    "storeid": safe_storeid,
                    "path": rel_posix,
                    "status": "uploaded",
                }

            # If other unrelated files are queued but our target file is absent from both
            # queue and transferring, treat target as uploaded unless queue has no names.
            if not matching_q and not matching_xfer and (queue_items or transferring):
                has_named_entries = any(q.get("name") for q in queue_items) or any(
                    t.get("name") for t in transferring
                )
                if has_named_entries:
                    return {
                        "ok": True,
                        "storeid": safe_storeid,
                        "path": rel_posix,
                        "status": "uploaded",
                    }

            if time.time() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout_sec}s waiting for upload of {rel_posix}"
                )
            time.sleep(poll_interval)
