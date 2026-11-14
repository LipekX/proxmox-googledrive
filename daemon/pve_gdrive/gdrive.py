"""Google Drive API v3 client using Python stdlib urllib.request."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .config import Config
from .oauth import OAuthManager

FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveAPIError(RuntimeError):
    """Error returned by Google Drive API v3."""

    def __init__(self, status: int, message: str, reason: str = "") -> None:
        super().__init__(f"Drive API HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.reason = reason


def _escape_drive_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _safe_filename(name: str, fallback: str) -> str:
    base = os.path.basename(name.strip())
    if not base or base in (".", ".."):
        return fallback
    return base.replace("/", "_").replace("\x00", "")


class DriveClient:
    """Thread-safe Google Drive API v3 helper with quota caching."""

    def __init__(self, config: Config, oauth: OAuthManager) -> None:
        self.config = config
        self.oauth = oauth
        self._lock = threading.RLock()
        self._quota_cache: Dict[str, Dict[str, Any]] = {}

    def _request_json(
        self,
        method: str,
        url: str,
        access_token: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> Dict[str, Any]:
        if params:
            filtered = {k: v for k, v in params.items() if v is not None and v != ""}
            if filtered:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{urllib.parse.urlencode(filtered)}"

        headers = {"Authorization": f"Bearer {access_token}"}
        data: Optional[bytes] = None
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(json_body).encode("utf-8")

        max_attempts = 4 if timeout >= 3.0 else 1
        for attempt in range(max_attempts):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as err:
                raw = err.read().decode("utf-8", errors="replace")
                msg = raw
                reason = ""
                try:
                    parsed = json.loads(raw)
                    err_obj = parsed.get("error", {})
                    if isinstance(err_obj, dict):
                        msg = str(err_obj.get("message") or raw)
                        errors = err_obj.get("errors") or []
                        if errors and isinstance(errors, list):
                            reason = str(errors[0].get("reason") or "")
                except Exception:
                    pass
                is_rate_limit = (
                    err.code in (429, 500, 502, 503, 504)
                    or (
                        err.code == 403
                        and (
                            reason in ("rateLimitExceeded", "userRateLimitExceeded")
                            or "Quota exceeded" in msg
                            or "Rate Limit" in msg
                        )
                    )
                )
                if is_rate_limit and attempt + 1 < max_attempts:
                    time.sleep(0.5 * (1.8 ** attempt))
                    continue
                raise DriveAPIError(err.code, msg, reason=reason) from err
            except urllib.error.URLError as err:
                raise RuntimeError(f"Drive API network error: {err.reason}") from err
        return {}

    def _download_bytes(
        self, url: str, access_token: str, timeout: float = 120.0
    ) -> bytes:
        req = urllib.request.Request(
            url, method="GET", headers={"Authorization": f"Bearer {access_token}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            raise DriveAPIError(err.code, raw) from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"Drive download failed: {err.reason}") from err

    def _upload_multipart(
        self,
        access_token: str,
        name: str,
        parent_id: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        timeout: float = 120.0,
    ) -> Dict[str, Any]:
        boundary = f"pve-gdrive-{secrets.token_hex(12)}"
        meta = json.dumps({"name": name, "parents": [parent_id]}).encode("utf-8")
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + meta + (
            f"\r\n--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

        url = f"{self.config.drive_upload_base}/files?uploadType=multipart&supportsAllDrives=true"
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            raise DriveAPIError(err.code, raw) from err

    def get_quota(
        self,
        account_id: str,
        timeout: Optional[float] = None,
        allow_stale: bool = True,
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Query Drive about.get storageQuota with TTL caching and non-blocking fallback."""
        now = time.time()
        with self._lock:
            cached = self._quota_cache.get(account_id)
            if (
                not force_refresh
                and cached is not None
                and (now - cached["fetched_at"]) < self.config.quota_ttl
            ):
                result = dict(cached)
                result["stale"] = False
                return result

        effective_timeout = (
            self.config.status_timeout if timeout is None else float(timeout)
        )
        try:
            token = self.oauth.get_access_token(account_id)
            data = self._request_json(
                "GET",
                f"{self.config.drive_api_base}/about",
                token,
                params={"fields": "storageQuota,user"},
                timeout=effective_timeout,
            )
            sq = data.get("storageQuota") or {}
            limit_raw = sq.get("limit")
            usage_raw = sq.get("usage", 0)
            usage_drive_raw = sq.get("usageInDrive", usage_raw)
            unlimited = limit_raw is None or int(limit_raw) <= 0
            limit_val = int(limit_raw) if not unlimited else 0
            usage_val = int(usage_raw or 0)
            usage_drive_val = int(usage_drive_raw or 0)
            free_val = max(0, limit_val - usage_val) if not unlimited else 0

            entry = {
                "limit": limit_val,
                "usage": usage_val,
                "usage_in_drive": usage_drive_val,
                "free": free_val,
                "unlimited": unlimited,
                "fetched_at": time.time(),
                "stale": False,
            }
            with self._lock:
                self._quota_cache[account_id] = dict(entry)
            return entry
        except Exception:
            if allow_stale:
                with self._lock:
                    cached = self._quota_cache.get(account_id)
                    if cached is not None:
                        result = dict(cached)
                        result["stale"] = True
                        return result
            return None

    def invalidate_quota(self, account_id: str) -> None:
        """Drop cached quota for an account."""
        with self._lock:
            self._quota_cache.pop(account_id, None)

    def list_drives(self, account_id: str) -> List[Dict[str, str]]:
        """List My Drive + all accessible Shared Drives (supportsAllDrives=true)."""
        drives: List[Dict[str, str]] = [
            {"id": "", "name": "My Drive", "kind": "my_drive"}
        ]
        token = self.oauth.get_access_token(account_id)
        page_token: Optional[str] = None
        while True:
            try:
                data = self._request_json(
                    "GET",
                    f"{self.config.drive_api_base}/drives",
                    token,
                    params={
                        "pageSize": 100,
                        "pageToken": page_token or "",
                        "supportsAllDrives": "true",
                    },
                    timeout=10.0,
                )
            except DriveAPIError as err:
                # drive.file scope may forbid drives.list; My Drive remains usable.
                if err.status == 403:
                    break
                raise
            for item in data.get("drives") or []:
                drives.append(
                    {
                        "id": str(item.get("id", "")),
                        "name": str(item.get("name", "")),
                        "kind": "shared_drive",
                    }
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return drives

    def get_folder(self, account_id: str, folder_id: str) -> Dict[str, Any]:
        """Fetch folder metadata to verify pinned folder_id."""
        token = self.oauth.get_access_token(account_id)
        return self._request_json(
            "GET",
            f"{self.config.drive_api_base}/files/{urllib.parse.quote(folder_id, safe='')}",
            token,
            params={
                "fields": "id,name,mimeType,trashed,parents",
                "supportsAllDrives": "true",
            },
            timeout=10.0,
        )

    def ensure_folder(
        self,
        account_id: str,
        name: str,
        parent_id: Optional[str] = None,
        drive_id: str = "",
    ) -> str:
        """Deterministically resolve or create a Drive folder under parent_id."""
        token = self.oauth.get_access_token(account_id)
        effective_parent = parent_id or (drive_id if drive_id else "root")
        q = (
            "trashed = false and "
            f"mimeType = '{FOLDER_MIME}' and "
            f"name = '{_escape_drive_query(name)}' and "
            f"'{_escape_drive_query(effective_parent)}' in parents"
        )
        params: Dict[str, Any] = {
            "q": q,
            "fields": "files(id,name,createdTime,parents)",
            "orderBy": "createdTime",
            "pageSize": 10,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if drive_id:
            params["corpora"] = "drive"
            params["driveId"] = drive_id
        else:
            params["corpora"] = "user"

        res = self._request_json(
            "GET",
            f"{self.config.drive_api_base}/files",
            token,
            params=params,
            timeout=10.0,
        )
        files = res.get("files") or []
        if files:
            return str(files[0]["id"])

        created = self._request_json(
            "POST",
            f"{self.config.drive_api_base}/files",
            token,
            params={"supportsAllDrives": "true", "fields": "id,name"},
            json_body={
                "name": name,
                "mimeType": FOLDER_MIME,
                "parents": [effective_parent],
            },
            timeout=10.0,
        )
        return str(created["id"])

    def ensure_storage_layout(
        self,
        account_id: str,
        drive_id: str = "",
        subfolder: str = "",
        pinned_root_folder_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve/create proxmox/[subfolder/]{dump,template/{iso,cache}} and pin root_folder_id."""
        proxmox_id = ""
        storage_root_id = ""

        if pinned_root_folder_id:
            try:
                meta = self.get_folder(account_id, pinned_root_folder_id)
                if not meta.get("trashed") and meta.get("mimeType") == FOLDER_MIME:
                    storage_root_id = str(meta["id"])
                    proxmox_id = storage_root_id
            except DriveAPIError as err:
                if err.status not in (404,):
                    raise

        if not storage_root_id:
            top_parent = drive_id if drive_id else "root"
            proxmox_id = self.ensure_folder(
                account_id, "proxmox", parent_id=top_parent, drive_id=drive_id
            )
            current = proxmox_id
            clean_sub = (subfolder or "").strip().strip("/")
            if clean_sub:
                for segment in clean_sub.split("/"):
                    seg = segment.strip()
                    if not seg or seg in (".", ".."):
                        raise ValueError(f"Invalid subfolder segment in {subfolder!r}")
                    current = self.ensure_folder(
                        account_id, seg, parent_id=current, drive_id=drive_id
                    )
            storage_root_id = current

        dump_id = self.ensure_folder(
            account_id, "dump", parent_id=storage_root_id, drive_id=drive_id
        )
        template_id = self.ensure_folder(
            account_id, "template", parent_id=storage_root_id, drive_id=drive_id
        )
        iso_id = self.ensure_folder(
            account_id, "iso", parent_id=template_id, drive_id=drive_id
        )
        cache_id = self.ensure_folder(
            account_id, "cache", parent_id=template_id, drive_id=drive_id
        )

        return {
            "root_folder_id": storage_root_id,
            "proxmox_folder_id": proxmox_id,
            "folders": {
                "dump": dump_id,
                "template": template_id,
                "template/iso": iso_id,
                "template/cache": cache_id,
            },
        }

    def get_start_page_token(self, account_id: str, drive_id: str = "") -> str:
        """Fetch initial Drive changes.list startPageToken."""
        token = self.oauth.get_access_token(account_id)
        params: Dict[str, Any] = {"supportsAllDrives": "true"}
        if drive_id:
            params["driveId"] = drive_id
        res = self._request_json(
            "GET",
            f"{self.config.drive_api_base}/changes/startPageToken",
            token,
            params=params,
            timeout=10.0,
        )
        return str(res.get("startPageToken") or "")

    def list_changes(
        self,
        account_id: str,
        page_token: str,
        drive_id: str = "",
        page_size: int = 100,
    ) -> Dict[str, Any]:
        """Poll Google Drive changes.list from page_token."""
        token = self.oauth.get_access_token(account_id)
        params: Dict[str, Any] = {
            "pageToken": page_token,
            "pageSize": page_size,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": (
                "nextPageToken,newStartPageToken,"
                "changes(fileId,removed,file(id,name,mimeType,trashed,parents,size,modifiedTime))"
            ),
        }
        if drive_id:
            params["driveId"] = drive_id
        res = self._request_json(
            "GET",
            f"{self.config.drive_api_base}/changes",
            token,
            params=params,
            timeout=15.0,
        )
        return {
            "changes": res.get("changes") or [],
            "next_page_token": res.get("nextPageToken") or "",
            "new_start_page_token": res.get("newStartPageToken") or "",
        }

    def picker_import(
        self,
        account_id: str,
        file_ids: Any,
        target_type: str,
        target_folder_id: str,
        picker_access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Import Google Picker files into proxmox/template/{iso,cache}/ via copy or download+upload."""
        if isinstance(file_ids, str):
            ids = [item.strip() for item in file_ids.split(",") if item.strip()]
        elif isinstance(file_ids, list):
            ids = [str(item).strip() for item in file_ids if str(item).strip()]
        else:
            raise ValueError("file_ids must be a list or comma-separated string")

        if not ids:
            raise ValueError("file_ids cannot be empty")

        norm_type = (target_type or "iso").strip().lower()
        if norm_type not in ("iso", "vztmpl", "cache"):
            raise ValueError(f"Unsupported target_type: {target_type!r}")

        acct_token = self.oauth.get_access_token(account_id)
        read_token = (picker_access_token or "").strip() or acct_token

        imported: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for src_id in ids:
            try:
                meta = self._request_json(
                    "GET",
                    f"{self.config.drive_api_base}/files/{urllib.parse.quote(src_id, safe='')}",
                    read_token,
                    params={
                        "fields": "id,name,mimeType,size",
                        "supportsAllDrives": "true",
                    },
                    timeout=15.0,
                )
                file_name = _safe_filename(str(meta.get("name") or ""), src_id)
                mime_type = str(meta.get("mimeType") or "application/octet-stream")

                copied: Optional[Dict[str, Any]] = None
                method = "copy"

                # Try server-side copy first (account token, then picker token if distinct).
                copy_tokens = [acct_token]
                if read_token != acct_token:
                    copy_tokens.append(read_token)

                for tok in copy_tokens:
                    try:
                        copied = self._request_json(
                            "POST",
                            f"{self.config.drive_api_base}/files/{urllib.parse.quote(src_id, safe='')}/copy",
                            tok,
                            params={
                                "supportsAllDrives": "true",
                                "fields": "id,name,size,parents",
                            },
                            json_body={
                                "name": file_name,
                                "parents": [target_folder_id],
                            },
                            timeout=60.0,
                        )
                        break
                    except DriveAPIError as copy_err:
                        if copy_err.status not in (400, 403, 404):
                            raise

                if copied is None:
                    method = "download_upload"
                    raw_bytes = self._download_bytes(
                        f"{self.config.drive_api_base}/files/{urllib.parse.quote(src_id, safe='')}?alt=media&supportsAllDrives=true",
                        read_token,
                    )
                    copied = self._upload_multipart(
                        acct_token,
                        name=file_name,
                        parent_id=target_folder_id,
                        content=raw_bytes,
                        mime_type=mime_type,
                    )

                imported.append(
                    {
                        "source_id": src_id,
                        "file_id": str(copied.get("id") or ""),
                        "name": str(copied.get("name") or file_name),
                        "size": int(copied.get("size") or meta.get("size") or 0),
                        "method": method,
                        "target_folder_id": target_folder_id,
                    }
                )
            except Exception as exc:
                errors.append({"source_id": src_id, "error": str(exc)})

        return {"imported": imported, "errors": errors}
