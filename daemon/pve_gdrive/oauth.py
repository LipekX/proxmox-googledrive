"""Google Service Account JWT authentication and account profile management."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .config import (
    DEFAULT_GOOGLE_TOKEN_URL,
    Config,
    atomic_write_json,
    ensure_dir,
    read_json,
    validate_account_id,
)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
TOKEN_EXPIRY_SKEW = 60.0


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sanitize_account_slug(value: str) -> str:
    """Derive a URL-safe, filename-safe account ID from an email or label."""
    cleaned = value.strip().lower().replace("@", "-at-")
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", cleaned).strip("-.")
    if not cleaned:
        cleaned = f"sa-{secrets.token_hex(4)}"
    return validate_account_id(cleaned[:120])


def _http_post_form(url: str, form: Dict[str, str], timeout: float = 15.0) -> Dict[str, Any]:
    encoded = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Token endpoint HTTP {err.code}: {detail}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Token endpoint request failed: {err.reason}") from err


def create_service_account_jwt(
    client_email: str,
    private_key_pem: str,
    token_uri: str,
    scopes: Optional[List[str]] = None,
    private_key_id: Optional[str] = None,
    openssl_bin: str = "openssl",
    now: Optional[int] = None,
) -> str:
    """Create and RS256-sign a Google Service Account JWT assertion using openssl."""
    issued_at = int(time.time()) if now is None else int(now)
    header: Dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
    if private_key_id:
        header["kid"] = private_key_id
    scope_str = " ".join(scopes or [DRIVE_SCOPE])
    claims = {
        "iss": client_email,
        "scope": scope_str,
        "aud": token_uri,
        "iat": issued_at,
        "exp": issued_at + 3600,
    }
    signing_input = (
        f"{_b64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))}."
        f"{_b64url_encode(json.dumps(claims, separators=(',', ':')).encode('utf-8'))}"
    ).encode("ascii")

    fd, key_path = tempfile.mkstemp(prefix="pve-sa-", suffix=".pem")
    try:
        os.chmod(key_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(private_key_pem)
        proc = subprocess.run(
            [openssl_bin, "dgst", "-sha256", "-sign", key_path],
            input=signing_input,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"openssl JWT signing failed: {err_msg}")
        signature = _b64url_encode(proc.stdout)
        return f"{signing_input.decode('ascii')}.{signature}"
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            pass


class OAuthManager:
    """Manages Google Service Account profiles and in-memory JWT access token cache."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._token_cache: Dict[str, Dict[str, Any]] = {}

    def _account_path(self, account_id: str) -> str:
        safe = validate_account_id(account_id)
        return os.path.join(self.config.accounts_dir, f"{safe}.json")

    @staticmethod
    def public_account_view(profile: Dict[str, Any]) -> Dict[str, Any]:
        """Return account metadata with private key stripped."""
        return {
            "id": profile["id"],
            "email": profile.get("email", ""),
            "mode": profile.get("mode", "service_account"),
            "scope": profile.get("scope", "drive"),
            "project_id": profile.get("project_id", ""),
            "created_at": profile.get("created_at", 0),
        }

    def add_service_account(
        self,
        service_account_json: Any,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate and store a Google Service Account JSON profile."""
        if isinstance(service_account_json, str):
            try:
                sa = json.loads(service_account_json)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid service_account_json: {exc}") from exc
        elif isinstance(service_account_json, dict):
            sa = dict(service_account_json)
        else:
            raise ValueError("service_account_json must be a JSON string or object")

        client_email = str(sa.get("client_email") or "").strip()
        private_key = str(sa.get("private_key") or "").strip()
        if not client_email or not private_key:
            raise ValueError("Service account JSON must contain client_email and private_key")

        token_uri = str(sa.get("token_uri") or self.config.google_token_url).strip()
        private_key_id = str(sa.get("private_key_id") or "").strip() or None

        # Verify openssl can sign with this private key before persisting.
        create_service_account_jwt(
            client_email=client_email,
            private_key_pem=private_key,
            token_uri=token_uri,
            scopes=[DRIVE_SCOPE],
            private_key_id=private_key_id,
            openssl_bin=self.config.openssl_bin,
        )

        resolved_id = (
            validate_account_id(account_id.strip())
            if account_id and account_id.strip()
            else sanitize_account_slug(client_email)
        )
        now = int(time.time())
        profile = {
            "id": resolved_id,
            "email": client_email,
            "mode": "service_account",
            "scope": "drive",
            "scopes": [DRIVE_SCOPE],
            "client_email": client_email,
            "private_key": private_key,
            "private_key_id": private_key_id or "",
            "project_id": str(sa.get("project_id") or ""),
            "token_uri": token_uri,
            "created_at": now,
        }

        ensure_dir(self.config.accounts_dir, mode=0o700)
        atomic_write_json(self._account_path(resolved_id), profile, mode=0o600)
        with self._lock:
            self._token_cache.pop(resolved_id, None)
        return self.public_account_view(profile)

    def get_account(self, account_id: str) -> Dict[str, Any]:
        """Load full account profile from disk (read-only)."""
        path = self._account_path(account_id)
        data = read_json(path)
        if data is None:
            raise FileNotFoundError(f"Account {account_id!r} not found")
        return data

    def list_accounts(self) -> List[Dict[str, Any]]:
        """List all saved account profiles (secrets stripped)."""
        if not os.path.isdir(self.config.accounts_dir):
            return []
        accounts: List[Dict[str, Any]] = []
        for entry in sorted(os.listdir(self.config.accounts_dir)):
            if not entry.endswith(".json") or entry.startswith("."):
                continue
            full = os.path.join(self.config.accounts_dir, entry)
            data = read_json(full)
            if isinstance(data, dict) and "id" in data:
                accounts.append(self.public_account_view(data))
        return accounts

    def delete_account(self, account_id: str) -> None:
        """Delete an account profile from disk and evict cached tokens."""
        path = self._account_path(account_id)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Account {account_id!r} not found")
        os.unlink(path)
        with self._lock:
            self._token_cache.pop(account_id, None)

    def get_access_token(self, account_id: str, force_refresh: bool = False) -> str:
        """Return a valid access token using the in-memory cache; never rewrites profile files."""
        now = time.time()
        with self._lock:
            cached = self._token_cache.get(account_id)
            if (
                not force_refresh
                and cached
                and cached.get("access_token")
                and cached.get("expires_at", 0.0) - TOKEN_EXPIRY_SKEW > now
            ):
                return str(cached["access_token"])

        profile = self.get_account(account_id)
        if profile.get("mode", "service_account") != "service_account":
            raise RuntimeError(
                f"Account {account_id!r} uses unsupported mode {profile.get('mode')!r}; "
                "re-add it as a Google Service Account."
            )

        configured_token_url = self.config.google_token_url
        profile_token_uri = str(profile.get("token_uri") or "").strip()
        if (
            configured_token_url != DEFAULT_GOOGLE_TOKEN_URL
            and (not profile_token_uri or profile_token_uri == DEFAULT_GOOGLE_TOKEN_URL)
        ):
            token_uri = configured_token_url
        else:
            token_uri = profile_token_uri or configured_token_url

        assertion = create_service_account_jwt(
            client_email=profile["client_email"],
            private_key_pem=profile["private_key"],
            token_uri=token_uri,
            scopes=profile.get("scopes") or [DRIVE_SCOPE],
            private_key_id=profile.get("private_key_id") or None,
            openssl_bin=self.config.openssl_bin,
        )
        token_data = _http_post_form(
            token_uri,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )

        access_token = str(token_data.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError(f"Token exchange failed for account {account_id!r}: missing access_token")
        expires_in = float(token_data.get("expires_in") or 3600)

        with self._lock:
            self._token_cache[account_id] = {
                "access_token": access_token,
                "expires_at": time.time() + expires_in,
            }
        return access_token

    def get_cached_token_expiry(self, account_id: str) -> Optional[float]:
        """Return cached token expiration timestamp if present."""
        with self._lock:
            entry = self._token_cache.get(account_id)
            return float(entry["expires_at"]) if entry else None
