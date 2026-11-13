"""OAuth2 (PKCE relay & BYOC), Service Account JWT, and account profile management."""

from __future__ import annotations

import base64
import hashlib
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
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    DEFAULT_GOOGLE_TOKEN_URL,
    Config,
    atomic_write_json,
    ensure_dir,
    read_json,
    validate_account_id,
)

SCOPE_MAP = {
    "drive.file": "https://www.googleapis.com/auth/drive.file",
    "drive": "https://www.googleapis.com/auth/drive",
}
USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
PENDING_OAUTH_TTL = 900.0
TOKEN_EXPIRY_SKEW = 60.0


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> Tuple[str, str]:
    """Generate a RFC 7636 PKCE (code_verifier, code_challenge) pair using S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _b64url_encode(digest)
    return verifier, challenge


def normalize_scope(scope: Optional[str]) -> Tuple[str, List[str]]:
    """Return (short_scope, full_oauth_scopes) for Google Drive OAuth."""
    raw = (scope or "drive.file").strip()
    if raw in SCOPE_MAP:
        return raw, [SCOPE_MAP[raw], USERINFO_EMAIL_SCOPE]
    if raw == SCOPE_MAP["drive"]:
        return "drive", [SCOPE_MAP["drive"], USERINFO_EMAIL_SCOPE]
    if raw == SCOPE_MAP["drive.file"]:
        return "drive.file", [SCOPE_MAP["drive.file"], USERINFO_EMAIL_SCOPE]
    parts = [p for p in raw.split() if p]
    if not parts:
        return "drive.file", [SCOPE_MAP["drive.file"], USERINFO_EMAIL_SCOPE]
    short = "drive" if SCOPE_MAP["drive"] in parts else "drive.file"
    if USERINFO_EMAIL_SCOPE not in parts:
        parts.append(USERINFO_EMAIL_SCOPE)
    return short, parts


def sanitize_account_slug(value: str) -> str:
    """Derive a URL-safe, filename-safe account ID from an email or label."""
    cleaned = value.strip().lower().replace("@", "-at-")
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", cleaned).strip("-.")
    if not cleaned:
        cleaned = f"acct-{secrets.token_hex(4)}"
    return validate_account_id(cleaned[:120])


def _parse_tokens_payload(tokens_json: Any) -> Dict[str, Any]:
    if isinstance(tokens_json, dict):
        return dict(tokens_json)
    if not isinstance(tokens_json, str):
        raise ValueError("tokens_json must be a JSON object, string, or base64 payload")
    text = tokens_json.strip()
    if not text:
        raise ValueError("tokens_json cannot be empty")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    padded = text + "=" * ((4 - len(text) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parsed = json.loads(decoded)
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        raise ValueError(f"Invalid tokens_json payload: {exc}") from exc
    raise ValueError("tokens_json must decode to a JSON object")


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


def _http_get_json(url: str, access_token: str, timeout: float = 10.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {err.code} from {url}: {detail}") from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Request failed for {url}: {err.reason}") from err


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
    scope_str = " ".join(scopes or [SCOPE_MAP["drive"]])
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
    """Manages Google accounts, PKCE flows, Service Accounts, and in-memory token cache."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._pending_states: Dict[str, Dict[str, Any]] = {}
        self._token_cache: Dict[str, Dict[str, Any]] = {}

    def _account_path(self, account_id: str) -> str:
        safe_id = validate_account_id(account_id)
        return os.path.join(self.config.accounts_dir, f"{safe_id}.json")

    def _prune_pending_locked(self, now: float) -> None:
        expired = [
            key
            for key, item in self._pending_states.items()
            if now - item["created_at"] > PENDING_OAUTH_TTL
        ]
        for key in expired:
            del self._pending_states[key]

    @staticmethod
    def public_account_view(profile: Dict[str, Any]) -> Dict[str, Any]:
        """Strip sensitive credentials before returning account info over IPC."""
        return {
            "id": profile["id"],
            "email": profile.get("email", ""),
            "mode": profile.get("mode", "relay"),
            "scope": profile.get("scope", "drive.file"),
            "created_at": profile.get("created_at", 0),
        }

    @staticmethod
    def public_relay_view(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Return non-secret relay configuration view safe for Web UI responses."""
        cfg = data or {}
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "client_id": str(cfg.get("client_id") or ""),
            "has_client_secret": bool(str(cfg.get("client_secret") or "").strip()),
            "redirect_uri": str(cfg.get("redirect_uri") or ""),
            "scope": str(cfg.get("scope") or "drive.file"),
        }

    def get_relay_config(self) -> Dict[str, Any]:
        """Read Proxmox-hosted OAuth relay configuration without exposing client_secret."""
        with self._lock:
            raw = read_json(self.config.relay_config_path, default={}) or {}
        return self.public_relay_view(raw)

    def save_relay_config(
        self,
        enabled: bool,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        scope: Optional[str] = "drive.file",
    ) -> Dict[str, Any]:
        """Persist Proxmox-hosted OAuth relay configuration to pmxcfs."""
        with self._lock:
            existing = read_json(self.config.relay_config_path, default={}) or {}
            clean_id = (
                str(client_id).strip()
                if client_id is not None
                else str(existing.get("client_id") or "").strip()
            )
            clean_secret = (
                str(client_secret).strip()
                if client_secret is not None and str(client_secret).strip()
                else str(existing.get("client_secret") or "").strip()
            )
            clean_redirect = (
                str(redirect_uri).strip()
                if redirect_uri is not None
                else str(existing.get("redirect_uri") or "").strip()
            )
            short_scope, _ = normalize_scope(scope or str(existing.get("scope") or "drive.file"))
            if short_scope not in ("drive.file", "drive"):
                raise ValueError("Relay scope must be 'drive.file' or 'drive'")

            if enabled:
                if not clean_id:
                    raise ValueError("client_id is required when enabling self-hosted relay")
                if clean_redirect and not (
                    clean_redirect.startswith("https://") or clean_redirect.startswith("http://")
                ):
                    raise ValueError("redirect_uri must start with https:// or http://")

            payload = {
                "enabled": bool(enabled),
                "client_id": clean_id,
                "client_secret": clean_secret,
                "redirect_uri": clean_redirect,
                "scope": short_scope,
                "updated_at": int(time.time()),
            }
            atomic_write_json(self.config.relay_config_path, payload, mode=0o600)
        return self.public_relay_view(payload)

    def start_oauth(
        self,
        mode: str = "relay",
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        scope: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        ui_origin: Optional[str] = None,
    ) -> Dict[str, str]:
        """Start an OAuth2 flow (relay, pve_self, or byoc) and return {state, auth_url}."""
        mode_clean = (mode or "relay").strip().lower()
        if mode_clean not in ("relay", "pve_self", "byoc"):
            raise ValueError(f"Unsupported OAuth mode: {mode!r}")

        with self._lock:
            self_relay_cfg = read_json(self.config.relay_config_path, default={}) or {}

        if mode_clean == "relay" and bool(self_relay_cfg.get("enabled", False)):
            mode_clean = "pve_self"

        effective_scope = scope
        if mode_clean == "pve_self" and (scope is None or scope == ""):
            effective_scope = str(self_relay_cfg.get("scope") or "drive.file")
        short_scope, full_scopes = normalize_scope(effective_scope)

        default_ui_callback = ""
        if ui_origin and str(ui_origin).strip():
            clean_origin = str(ui_origin).strip().rstrip("/")
            if clean_origin.startswith("https://") or clean_origin.startswith("http://"):
                default_ui_callback = f"{clean_origin}/pve2/gdrive-oauth-callback.html"

        if mode_clean == "pve_self":
            effective_client_id = (
                (client_id or "").strip() or str(self_relay_cfg.get("client_id") or "").strip()
            )
            if not effective_client_id:
                raise ValueError("Self-hosted Proxmox OAuth relay requires client_id")
            effective_secret = (
                (client_secret or "").strip()
                or str(self_relay_cfg.get("client_secret") or "").strip()
            )
            effective_redirect = (
                (redirect_uri or "").strip()
                or str(self_relay_cfg.get("redirect_uri") or "").strip()
                or default_ui_callback
            )
            if not effective_redirect:
                raise ValueError(
                    "redirect_uri or ui_origin is required for self-hosted Proxmox OAuth relay"
                )
            relay_source = "self_hosted"
        elif mode_clean == "relay":
            effective_redirect = (redirect_uri or self.config.oauth_relay_url).strip()
            effective_client_id = (client_id or self.config.relay_client_id).strip()
            effective_secret = (client_secret or "").strip()
            relay_source = "external_relay"
        else:
            effective_client_id = (client_id or "").strip()
            if not effective_client_id:
                raise ValueError("client_id is required for byoc mode")
            effective_secret = (client_secret or "").strip()
            effective_redirect = (
                redirect_uri or default_ui_callback or "http://127.0.0.1:53682/callback"
            ).strip()
            relay_source = "byoc"

        use_pkce = mode_clean in ("byoc", "pve_self")
        verifier = ""
        challenge = ""
        if use_pkce:
            verifier, challenge = generate_pkce()
        state = secrets.token_urlsafe(32)
        now = time.time()

        with self._lock:
            self._prune_pending_locked(now)
            self._pending_states[state] = {
                "state": state,
                "mode": mode_clean,
                "client_id": effective_client_id,
                "client_secret": effective_secret,
                "scope": short_scope,
                "scopes": full_scopes,
                "redirect_uri": effective_redirect,
                "code_verifier": verifier,
                "created_at": now,
            }

        params = {
            "client_id": effective_client_id,
            "redirect_uri": effective_redirect,
            "response_type": "code",
            "scope": " ".join(full_scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        if use_pkce:
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"

        auth_url = f"{self.config.google_auth_url}?{urllib.parse.urlencode(params)}"
        result: Dict[str, str] = {
            "state": state,
            "auth_url": auth_url,
            "redirect_uri": effective_redirect,
            "relay_source": relay_source,
        }
        if use_pkce:
            result["code_challenge"] = challenge
        return result

    def _fetch_user_email(self, access_token: str) -> str:
        try:
            info = _http_get_json(self.config.google_userinfo_url, access_token, timeout=5.0)
            email = info.get("email")
            if email:
                return str(email)
        except Exception:
            pass
        try:
            about = _http_get_json(
                f"{self.config.drive_api_base}/about?fields=user",
                access_token,
                timeout=5.0,
            )
            email = (about.get("user") or {}).get("emailAddress")
            if email:
                return str(email)
        except Exception:
            pass
        return ""

    def finish_oauth(
        self,
        state: str,
        code: Optional[str] = None,
        tokens_json: Any = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Finish OAuth2 flow via relay tokens payload or authorization code exchange."""
        if not state:
            raise ValueError("state is required")
        now = time.time()
        with self._lock:
            self._prune_pending_locked(now)
            pending = self._pending_states.pop(state, None)
        if pending is None:
            raise ValueError("Invalid or expired OAuth state")

        if tokens_json is not None:
            token_data = _parse_tokens_payload(tokens_json)
        elif code:
            form = {
                "code": code.strip(),
                "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"],
                "grant_type": "authorization_code",
            }
            if pending["code_verifier"]:
                form["code_verifier"] = pending["code_verifier"]
            if pending["client_secret"]:
                form["client_secret"] = pending["client_secret"]
            token_data = _http_post_form(self.config.google_token_url, form)
        else:
            raise ValueError("Either code or tokens_json must be provided")

        if "error" in token_data and not token_data.get("access_token"):
            raise RuntimeError(
                f"OAuth token exchange error: {token_data.get('error_description') or token_data['error']}"
            )

        access_token = str(token_data.get("access_token") or "").strip()
        refresh_token = str(token_data.get("refresh_token") or "").strip()
        expires_in = float(token_data.get("expires_in") or 3600)
        if not access_token and not refresh_token:
            raise RuntimeError("Token payload missing access_token and refresh_token")

        email = str(token_data.get("email") or "").strip()
        if not email and access_token:
            email = self._fetch_user_email(access_token)

        resolved_id = (
            validate_account_id(account_id.strip())
            if account_id and account_id.strip()
            else sanitize_account_slug(email or f"google-{pending['mode']}")
        )

        # Preserve existing refresh_token if Google omits it on re-auth without revoke
        existing = read_json(self._account_path(resolved_id), default={}) or {}
        if not refresh_token:
            refresh_token = str(existing.get("refresh_token") or "")

        profile = {
            "id": resolved_id,
            "email": email or existing.get("email") or resolved_id,
            "mode": pending["mode"],
            "scope": pending["scope"],
            "scopes": pending["scopes"],
            "client_id": str(token_data.get("client_id") or pending["client_id"]),
            "client_secret": str(
                token_data.get("client_secret") or pending["client_secret"]
            ),
            "refresh_token": refresh_token,
            "token_uri": self.config.google_token_url,
            "created_at": existing.get("created_at") or int(now),
        }

        ensure_dir(self.config.accounts_dir, mode=0o700)
        atomic_write_json(self._account_path(resolved_id), profile, mode=0o600)

        if access_token:
            with self._lock:
                self._token_cache[resolved_id] = {
                    "access_token": access_token,
                    "expires_at": now + expires_in,
                }

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
            scopes=[SCOPE_MAP["drive"]],
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
            "scopes": [SCOPE_MAP["drive"]],
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
        mode = profile.get("mode", "relay")

        if mode == "service_account":
            configured_token_url = self.config.google_token_url
            profile_token_uri = str(profile.get("token_uri") or "").strip()
            # Prefer explicit test override if profile holds the default Google URL.
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
                scopes=profile.get("scopes") or [SCOPE_MAP["drive"]],
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
        else:
            refresh_token = str(profile.get("refresh_token") or "").strip()
            if not refresh_token:
                raise RuntimeError(f"Account {account_id!r} has no refresh_token")
            form = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": str(profile.get("client_id") or ""),
            }
            if profile.get("client_secret"):
                form["client_secret"] = str(profile["client_secret"])
            token_url = self.config.google_token_url
            token_data = _http_post_form(token_url, form)

        access_token = str(token_data.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError(f"Refresh failed for account {account_id!r}: missing access_token")
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
