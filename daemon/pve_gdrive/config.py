"""Configuration constants and atomic file helpers for pve-gdrived."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Any

DEFAULT_ACCOUNTS_DIR = "/etc/pve/priv/gdrive/accounts"
DEFAULT_STORAGES_DIR = "/etc/pve/priv/gdrive/storages"
DEFAULT_RELAY_CONFIG_PATH = "/etc/pve/priv/gdrive/relay.json"
DEFAULT_SOCK_PATH = "/run/pve-gdrived.sock"
DEFAULT_RUN_DIR = "/run/pve-gdrive"
DEFAULT_CACHE_DIR = "/var/cache/pve-gdrive"
DEFAULT_MNT_BASE = "/mnt/pve"

DEFAULT_RELAY_URL = "https://auth.pve-gdrive.org/oauth/callback"
DEFAULT_RELAY_CLIENT_ID = "pve-storage-gdrive-relay"
DEFAULT_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
DEFAULT_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DEFAULT_DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
_SAFE_STOREID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass
class Config:
    """Runtime configuration resolved from environment variables."""

    accounts_dir: str = DEFAULT_ACCOUNTS_DIR
    storages_dir: str = DEFAULT_STORAGES_DIR
    relay_config_path: str = DEFAULT_RELAY_CONFIG_PATH
    sock_path: str = DEFAULT_SOCK_PATH
    run_dir: str = DEFAULT_RUN_DIR
    cache_dir: str = DEFAULT_CACHE_DIR
    mnt_base: str = DEFAULT_MNT_BASE
    oauth_relay_url: str = DEFAULT_RELAY_URL
    relay_client_id: str = DEFAULT_RELAY_CLIENT_ID
    google_auth_url: str = DEFAULT_GOOGLE_AUTH_URL
    google_token_url: str = DEFAULT_GOOGLE_TOKEN_URL
    google_userinfo_url: str = DEFAULT_GOOGLE_USERINFO_URL
    drive_api_base: str = DEFAULT_DRIVE_API_BASE
    drive_upload_base: str = DEFAULT_DRIVE_UPLOAD_BASE
    rclone_bin: str = "rclone"
    openssl_bin: str = "openssl"
    quota_ttl: float = 120.0
    status_timeout: float = 1.2
    sock_mode: int = 0o660

    @classmethod
    def from_env(cls) -> "Config":
        """Build Config using environment variable overrides."""
        sock_mode_raw = os.environ.get("PVE_GDRIVE_SOCK_MODE", "").strip()
        sock_mode = int(sock_mode_raw, 8) if sock_mode_raw else 0o660
        return cls(
            accounts_dir=os.environ.get("PVE_GDRIVE_ACCOUNTS_DIR", DEFAULT_ACCOUNTS_DIR),
            storages_dir=os.environ.get("PVE_GDRIVE_STORAGES_DIR", DEFAULT_STORAGES_DIR),
            relay_config_path=os.environ.get(
                "PVE_GDRIVE_RELAY_CONFIG", DEFAULT_RELAY_CONFIG_PATH
            ),
            sock_path=os.environ.get("PVE_GDRIVE_SOCK", DEFAULT_SOCK_PATH),
            run_dir=os.environ.get("PVE_GDRIVE_RUN_DIR", DEFAULT_RUN_DIR),
            cache_dir=os.environ.get("PVE_GDRIVE_CACHE_DIR", DEFAULT_CACHE_DIR),
            mnt_base=os.environ.get("PVE_GDRIVE_MNT_BASE", DEFAULT_MNT_BASE),
            oauth_relay_url=os.environ.get("PVE_GDRIVE_RELAY_URL", DEFAULT_RELAY_URL),
            relay_client_id=os.environ.get(
                "PVE_GDRIVE_RELAY_CLIENT_ID", DEFAULT_RELAY_CLIENT_ID
            ),
            google_auth_url=os.environ.get(
                "PVE_GDRIVE_GOOGLE_AUTH_URL", DEFAULT_GOOGLE_AUTH_URL
            ),
            google_token_url=os.environ.get(
                "PVE_GDRIVE_GOOGLE_TOKEN_URL", DEFAULT_GOOGLE_TOKEN_URL
            ),
            google_userinfo_url=os.environ.get(
                "PVE_GDRIVE_GOOGLE_USERINFO_URL", DEFAULT_GOOGLE_USERINFO_URL
            ),
            drive_api_base=os.environ.get(
                "PVE_GDRIVE_DRIVE_API_BASE", DEFAULT_DRIVE_API_BASE
            ),
            drive_upload_base=os.environ.get(
                "PVE_GDRIVE_DRIVE_UPLOAD_BASE", DEFAULT_DRIVE_UPLOAD_BASE
            ),
            rclone_bin=os.environ.get("PVE_GDRIVE_RCLONE_BIN", "rclone"),
            openssl_bin=os.environ.get("PVE_GDRIVE_OPENSSL_BIN", "openssl"),
            quota_ttl=float(os.environ.get("PVE_GDRIVE_QUOTA_TTL", "120")),
            status_timeout=float(os.environ.get("PVE_GDRIVE_STATUS_TIMEOUT", "1.2")),
            sock_mode=sock_mode,
        )


def validate_account_id(account_id: str) -> str:
    """Validate and return a safe account identifier."""
    if not account_id or "/" in account_id or "\\" in account_id or ".." in account_id:
        raise ValueError(f"Invalid account id: {account_id!r}")
    if not _SAFE_ID_RE.match(account_id):
        raise ValueError(f"Invalid account id format: {account_id!r}")
    return account_id


def validate_store_id(storeid: str) -> str:
    """Validate and return a safe Proxmox storage identifier."""
    if not storeid or "/" in storeid or "\\" in storeid or ".." in storeid:
        raise ValueError(f"Invalid storeid: {storeid!r}")
    if not _SAFE_STOREID_RE.match(storeid):
        raise ValueError(f"Invalid storeid format: {storeid!r}")
    return storeid


def ensure_dir(path: str, mode: int = 0o700) -> str:
    """Create directory tree if missing and enforce mode on the target dir."""
    os.makedirs(path, mode=mode, exist_ok=True)
    os.chmod(path, mode)
    return path


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o600, dir_mode: int = 0o700) -> None:
    """Atomically write bytes to path with restrictive permissions."""
    parent = os.path.dirname(os.path.abspath(path))
    ensure_dir(parent, mode=dir_mode)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    try:
        os.chmod(tmp_path, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_text(path: str, text: str, mode: int = 0o600, dir_mode: int = 0o700) -> None:
    """Atomically write UTF-8 text to path with restrictive permissions."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode, dir_mode=dir_mode)


def atomic_write_json(path: str, obj: Any, mode: int = 0o600, dir_mode: int = 0o700) -> None:
    """Atomically serialize JSON to path with mode 0600."""
    payload = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, payload, mode=mode, dir_mode=dir_mode)


def read_json(path: str, default: Any = None) -> Any:
    """Read JSON from path, returning default if file does not exist."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
