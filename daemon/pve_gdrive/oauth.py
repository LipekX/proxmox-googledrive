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

DEFAULT_RCLONE_CLIENT_ID = "202264815644.apps.googleusercontent.com"
DEFAULT_RCLONE_ENCRYPTED_SECRET = "eX8GpZTVx3vxMWVkuuBdDWmAUE6rGhTwVrvG9GhllYccSdj2-mvHVg"
DEFAULT_RCLONE_REDIRECT_URI = "http://127.0.0.1:53682/"
DEFAULT_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
_RCLONE_OBSCURE_KEY_HEX = (
    "9c935b48730a554d6bfd7c63c886a92bd390198eb8128afbf4de162b8b95f638"
)
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
TOKEN_EXPIRY_SKEW = 60.0


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(val: str) -> bytes:
    padded = val + "=" * ((4 - len(val) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def reveal_rclone_obscured(
    obscured: str = DEFAULT_RCLONE_ENCRYPTED_SECRET,
    openssl_bin: str = "openssl",
) -> str:
    """Decrypt an rclone obscured string using openssl AES-256-CTR."""
    raw = _b64url_decode(obscured)
    if len(raw) <= 16:
        raise ValueError("Invalid obscured string")
    iv = raw[:16]
    ct = raw[16:]
    proc = subprocess.run(
        [
            openssl_bin,
            "enc",
            "-d",
            "-aes-256-ctr",
            "-K",
            _RCLONE_OBSCURE_KEY_HEX,
            "-iv",
            iv.hex(),
            "-nopad",
        ],
        input=ct,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"openssl decrypt failed: {err}")
    return proc.stdout.decode("utf-8")


def extract_oauth_code(raw_input: str) -> str:
    """Extract an OAuth authorization code or token JSON from a pasted redirect URL or string."""
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("Authorization code or URL cannot be empty")
    if text.startswith("http://") or text.startswith("https://") or "code=" in text:
        parsed = urllib.parse.urlparse(text if "://" in text else f"http://localhost/?{text.lstrip('?')}")
        qs = urllib.parse.parse_qs(parsed.query)
        if "error" in qs and qs["error"]:
            raise ValueError(f"Google OAuth error: {qs['error'][0]}")
        if "code" in qs and qs["code"]:
            return qs["code"][0].strip()
    return text


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
    """Manages Google OAuth2 / Service Account profiles and in-memory access token cache."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._token_cache: Dict[str, Dict[str, Any]] = {}
        self._pending_states: Dict[str, Dict[str, Any]] = {}

    def _account_path(self, account_id: str) -> str:
        safe = validate_account_id(account_id)
        return os.path.join(self.config.accounts_dir, f"{safe}.json")

    @staticmethod
    def public_account_view(profile: Dict[str, Any]) -> Dict[str, Any]:
        """Return account metadata with private keys and refresh tokens stripped."""
        return {
            "id": profile["id"],
            "email": profile.get("email", ""),
            "mode": profile.get("mode", "oauth"),
            "scope": profile.get("scope", "drive"),
            "project_id": profile.get("project_id", ""),
            "created_at": profile.get("created_at", 0),
        }

    def _fetch_user_email(self, access_token: str) -> str:
        """Fetch user email from Google Drive about endpoint using access_token."""
        url = f"{self.config.drive_api_base}/about?fields=user"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
                user = data.get("user") or {}
                email = str(user.get("emailAddress") or "").strip()
                if email:
                    return email
        except Exception:
            pass
        return ""

    def start_oauth_login(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        scope: str = "drive",
    ) -> Dict[str, str]:
        """Generate a Google OAuth2 login URL (defaults to rclone's built-in desktop OAuth client)."""
        cid = (client_id or "").strip() or DEFAULT_RCLONE_CLIENT_ID
        csec = (client_secret or "").strip()
        if not csec and cid == DEFAULT_RCLONE_CLIENT_ID:
            csec = reveal_rclone_obscured(
                DEFAULT_RCLONE_ENCRYPTED_SECRET,
                openssl_bin=self.config.openssl_bin,
            )
        redir = (redirect_uri or "").strip() or DEFAULT_RCLONE_REDIRECT_URI
        scope_Norm = "drive.file" if scope == "drive.file" else "drive"
        full_scope = (
            "https://www.googleapis.com/auth/drive.file"
            if scope_Norm == "drive.file"
            else DRIVE_SCOPE
        )
        state = secrets.token_urlsafe(16)
        with self._lock:
            self._pending_states[state] = {
                "client_id": cid,
                "client_secret": csec,
                "redirect_uri": redir,
                "scope": scope_Norm,
                "created_at": time.time(),
            }
        params = {
            "access_type": "offline",
            "client_id": cid,
            "redirect_uri": redir,
            "response_type": "code",
            "scope": full_scope,
            "state": state,
            "prompt": "consent",
        }
        auth_url = f"{DEFAULT_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
        return {
            "state": state,
            "auth_url": auth_url,
            "redirect_uri": redir,
            "scope": scope_Norm,
        }

    def finish_oauth_login(
        self,
        code_or_token: Any,
        state: Optional[str] = None,
        account_id: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Complete Google login from a pasted redirect URL, auth code, or rclone authorize token."""
        pending: Dict[str, Any] = {}
        if state:
            with self._lock:
                pending = self._pending_states.pop(state.strip(), {}) or {}

        cid = (client_id or "").strip() or pending.get("client_id") or DEFAULT_RCLONE_CLIENT_ID
        csec = (client_secret or "").strip() or pending.get("client_secret") or ""
        if not csec and cid == DEFAULT_RCLONE_CLIENT_ID:
            csec = reveal_rclone_obscured(
                DEFAULT_RCLONE_ENCRYPTED_SECRET,
                openssl_bin=self.config.openssl_bin,
            )
        redir = (redirect_uri or "").strip() or pending.get("redirect_uri") or DEFAULT_RCLONE_REDIRECT_URI
        scope_norm = pending.get("scope") or "drive"

        token_data: Optional[Dict[str, Any]] = None
        if isinstance(code_or_token, dict):
            token_data = dict(code_or_token)
        elif isinstance(code_or_token, str):
            raw_str = code_or_token.strip()
            if not raw_str:
                raise ValueError("Authorization code, redirect URL, or token JSON is required")
            # Check if the user pasted `rclone authorize "drive"` output or a JSON token object
            json_match = re.search(r"\{[^{}]*\"refresh_token\"[^{}]*\}", raw_str)
            if raw_str.startswith("{") or json_match:
                try:
                    candidate = json.loads(json_match.group(0) if json_match else raw_str)
                    if isinstance(candidate, dict):
                        if isinstance(candidate.get("tokens"), dict):
                            token_data = dict(candidate["tokens"])
                        else:
                            token_data = candidate
                except json.JSONDecodeError:
                    pass
            elif not raw_str.startswith("http") and "code=" not in raw_str:
                # Also support base64-encoded JSON payload or rclone base64 blob
                try:
                    decoded = base64.b64decode(raw_str).decode("utf-8")
                    if "{" in decoded:
                        inner_match = re.search(r"\{[^{}]*\"refresh_token\"[^{}]*\}", decoded)
                        cand = json.loads(inner_match.group(0) if inner_match else decoded)
                        if isinstance(cand, dict):
                            token_data = dict(cand.get("tokens") if isinstance(cand.get("tokens"), dict) else cand)
                except Exception:
                    pass

            if token_data is None:
                code = extract_oauth_code(raw_str)
                token_data = _http_post_form(
                    self.config.google_token_url,
                    {
                        "code": code,
                        "client_id": cid,
                        "client_secret": csec,
                        "redirect_uri": redir,
                        "grant_type": "authorization_code",
                    },
                )
        else:
            raise ValueError("Invalid authorization input")

        refresh_token = str(token_data.get("refresh_token") or "").strip()
        access_token = str(token_data.get("access_token") or "").strip()
        if not refresh_token and not access_token:
            raise ValueError("Token response did not contain a refresh_token or access_token")

        if not access_token and refresh_token:
            refreshed = _http_post_form(
                self.config.google_token_url,
                {
                    "client_id": cid,
                    "client_secret": csec,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            access_token = str(refreshed.get("access_token") or "").strip()
            expires_in = float(refreshed.get("expires_in") or 3600)
        else:
            expires_in = float(token_data.get("expires_in") or 3600)

        email = self._fetch_user_email(access_token) if access_token else ""
        resolved_id = (
            validate_account_id(account_id.strip())
            if account_id and account_id.strip()
            else sanitize_account_slug(email or "google-drive")
        )
        now = int(time.time())
        profile = {
            "id": resolved_id,
            "email": email or resolved_id,
            "mode": "oauth",
            "scope": scope_norm,
            "scopes": [DRIVE_SCOPE],
            "client_id": cid,
            "client_secret": csec,
            "refresh_token": refresh_token,
            "token_uri": self.config.google_token_url,
            "created_at": now,
        }

        ensure_dir(self.config.accounts_dir, mode=0o700)
        atomic_write_json(self._account_path(resolved_id), profile, mode=0o600)
        with self._lock:
            if access_token:
                self._token_cache[resolved_id] = {
                    "access_token": access_token,
                    "expires_at": time.time() + expires_in,
                }
            else:
                self._token_cache.pop(resolved_id, None)
        return self.public_account_view(profile)

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
        mode = profile.get("mode", "oauth")

        configured_token_url = self.config.google_token_url
        profile_token_uri = str(profile.get("token_uri") or "").strip()
        if (
            configured_token_url != DEFAULT_GOOGLE_TOKEN_URL
            and (not profile_token_uri or profile_token_uri == DEFAULT_GOOGLE_TOKEN_URL)
        ):
            token_uri = configured_token_url
        else:
            token_uri = profile_token_uri or configured_token_url

        if mode == "service_account":
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
        elif mode in ("oauth", "relay", "byoc") or profile.get("refresh_token"):
            refresh_token = str(profile.get("refresh_token") or "").strip()
            if not refresh_token:
                raise RuntimeError(
                    f"Account {account_id!r} is missing refresh_token; please run 'pve-gdrive login' again."
                )
            cid = str(profile.get("client_id") or "").strip() or DEFAULT_RCLONE_CLIENT_ID
            csec = str(profile.get("client_secret") or "").strip()
            if not csec and cid == DEFAULT_RCLONE_CLIENT_ID:
                csec = reveal_rclone_obscured(
                    DEFAULT_RCLONE_ENCRYPTED_SECRET,
                    openssl_bin=self.config.openssl_bin,
                )
            token_data = _http_post_form(
                token_uri,
                {
                    "client_id": cid,
                    "client_secret": csec,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        else:
            raise RuntimeError(
                f"Account {account_id!r} uses unsupported mode {mode!r}; "
                "please run 'pve-gdrive login' to authenticate."
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
