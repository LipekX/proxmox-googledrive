"""HTTP/1.1 JSON IPC server and client over Unix domain socket."""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import socketserver
import urllib.parse
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from . import __version__
from .config import Config, ensure_dir, validate_account_id, validate_store_id
from .gdrive import DriveAPIError

if TYPE_CHECKING:
    from .daemon import DaemonApp

logger = logging.getLogger("pve-gdrived.ipc")
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def ipc_request(
    sock_path: str,
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
) -> Tuple[int, Dict[str, Any]]:
    """Send an HTTP/1.1 JSON request over the daemon Unix socket."""
    conn = _UnixHTTPConnection(sock_path, timeout=timeout)
    try:
        headers: Dict[str, str] = {"Accept": "application/json"}
        raw_body: Optional[bytes] = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw_body = json.dumps(body).encode("utf-8")
        conn.request(method, path, body=raw_body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw) if raw else {}
        return resp.status, parsed
    finally:
        conn.close()


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Multi-threaded Unix domain socket HTTP server."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: str, handler_cls: type[BaseHTTPRequestHandler], app: "DaemonApp") -> None:
        self.app = app
        super().__init__(server_address, handler_cls)


class IPCRequestHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 JSON request dispatcher for pve-gdrived."""

    protocol_version = "HTTP/1.1"

    def address_string(self) -> str:
        return "unix"

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug("IPC %s", format % args)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        raw_len = self.headers.get("Content-Length", "").strip()
        if not raw_len:
            return {}
        try:
            length = int(raw_len)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("Request body too large")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Malformed JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON request body must be an object")
        return parsed

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        app: DaemonApp = self.server.app  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [urllib.parse.unquote(p) for p in path.lstrip("/").split("/") if p]

        try:
            body = self._read_json_body() if method in ("POST", "PUT", "PATCH") else {}
            status, result = self._route(app, method, parts, body)
            self._send_json(status, result)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._send_json(404, {"error": str(exc)})
        except TimeoutError as exc:
            self._send_json(504, {"error": str(exc)})
        except DriveAPIError as exc:
            status = exc.status if 400 <= exc.status < 600 else 502
            self._send_json(status, {"error": str(exc)})
        except Exception as exc:
            logger.exception("Unhandled error handling %s %s", method, path)
            self._send_json(500, {"error": str(exc)})

    def _route(
        self,
        app: "DaemonApp",
        method: str,
        parts: list[str],
        body: Dict[str, Any],
    ) -> Tuple[int, Dict[str, Any]]:
        if len(parts) < 2 or parts[0] != "v1":
            return 404, {"error": "Unknown API version or route"}

        sub = parts[1:]

        # GET /v1/health
        if method == "GET" and sub == ["health"]:
            return 200, {"ok": True, "version": __version__, "pid": os.getpid()}

        # GET /v1/accounts
        if method == "GET" and sub == ["accounts"]:
            accounts = app.oauth.list_accounts()
            enriched = []
            for acct in accounts:
                quota = app.drive.get_quota(
                    acct["id"],
                    timeout=min(app.config.status_timeout, 1.0),
                    allow_stale=True,
                )
                item = dict(acct)
                if quota is not None:
                    item["quota"] = {
                        "limit": quota["limit"],
                        "usage": quota["usage"],
                        "free": quota["free"],
                        "unlimited": quota["unlimited"],
                        "stale": quota["stale"],
                    }
                else:
                    item["quota"] = None
                enriched.append(item)
            return 200, {"accounts": enriched}

        # GET /v1/relay/config
        if method == "GET" and sub == ["relay", "config"]:
            return 200, app.oauth.get_relay_config()

        # POST /v1/relay/config
        if method == "POST" and sub == ["relay", "config"]:
            res = app.oauth.save_relay_config(
                enabled=bool(body.get("enabled", False)),
                client_id=body.get("client_id"),
                client_secret=body.get("client_secret"),
                redirect_uri=body.get("redirect_uri"),
                scope=body.get("scope") or "drive.file",
            )
            return 200, res

        # POST /v1/oauth/start
        if method == "POST" and sub == ["oauth", "start"]:
            res = app.oauth.start_oauth(
                mode=str(body.get("mode") or "relay"),
                client_id=body.get("client_id"),
                client_secret=body.get("client_secret"),
                scope=body.get("scope"),
                redirect_uri=body.get("redirect_uri"),
                ui_origin=body.get("ui_origin"),
            )
            return 200, res

        # POST /v1/oauth/finish
        if method == "POST" and sub == ["oauth", "finish"]:
            account = app.oauth.finish_oauth(
                state=str(body.get("state") or ""),
                code=body.get("code"),
                tokens_json=body.get("tokens_json"),
                account_id=body.get("account_id"),
            )
            return 200, {"account": account}

        # POST /v1/accounts/service-account
        if method == "POST" and sub == ["accounts", "service-account"]:
            account = app.oauth.add_service_account(
                service_account_json=body.get("service_account_json"),
                account_id=body.get("account_id"),
            )
            return 200, {"account": account}

        # DELETE /v1/accounts/<id>
        if method == "DELETE" and len(sub) == 2 and sub[0] == "accounts":
            acct_id = validate_account_id(sub[1])
            app.oauth.delete_account(acct_id)
            app.drive.invalidate_quota(acct_id)
            return 200, {"ok": True, "deleted": acct_id}

        # GET /v1/accounts/<id>/drives
        if method == "GET" and len(sub) == 3 and sub[0] == "accounts" and sub[2] == "drives":
            acct_id = validate_account_id(sub[1])
            drives = app.drive.list_drives(acct_id)
            return 200, {"drives": drives}

        # POST /v1/storage/init
        if method == "POST" and sub == ["storage", "init"]:
            storeid = validate_store_id(str(body.get("storeid") or ""))
            account = validate_account_id(str(body.get("account") or ""))
            res = app.vfs.init_storage(
                storeid=storeid,
                account=account,
                drive_id=str(body.get("drive_id") or ""),
                subfolder=str(body.get("subfolder") or ""),
                root_folder_id=body.get("root_folder_id"),
                cache_dir=body.get("cache_dir"),
                cache_max_size=body.get("cache_max_size"),
                encrypt=bool(body.get("encrypt", False)),
                async_upload=bool(body.get("async_upload", False)),
                crypt_password=body.get("crypt_password"),
                crypt_password2=body.get("crypt_password2"),
            )
            return 200, res

        # POST /v1/storage/activate
        if method == "POST" and sub == ["storage", "activate"]:
            storeid = validate_store_id(str(body.get("storeid") or ""))
            res = app.vfs.activate_storage(
                storeid=storeid,
                account=body.get("account"),
                drive_id=body.get("drive_id"),
                subfolder=body.get("subfolder"),
                root_folder_id=body.get("root_folder_id"),
                cache_dir=body.get("cache_dir"),
                cache_max_size=body.get("cache_max_size"),
                encrypt=body.get("encrypt"),
                async_upload=body.get("async_upload"),
            )
            return 200, res

        # POST /v1/storage/deactivate
        if method == "POST" and sub == ["storage", "deactivate"]:
            storeid = validate_store_id(str(body.get("storeid") or ""))
            res = app.vfs.deactivate_storage(storeid)
            return 200, res

        # GET /v1/storage/<storeid>/status
        if method == "GET" and len(sub) == 3 and sub[0] == "storage" and sub[2] == "status":
            storeid = validate_store_id(sub[1])
            return 200, app.vfs.get_storage_status(storeid)

        # POST /v1/uploads/wait
        if method == "POST" and sub == ["uploads", "wait"]:
            storeid = validate_store_id(str(body.get("storeid") or ""))
            target_path = str(body.get("path") or "")
            timeout_sec = float(body.get("timeout_sec") or 3600.0)
            res = app.vfs.wait_for_upload(
                storeid=storeid,
                path=target_path,
                timeout_sec=timeout_sec,
            )
            return 200, res

        # GET /v1/queue
        if method == "GET" and sub == ["queue"]:
            return 200, app.vfs.get_queue()

        # POST /v1/picker/import
        if method == "POST" and sub == ["picker", "import"]:
            storeid = validate_store_id(str(body.get("storeid") or ""))
            meta = app.vfs.get_storage_meta(storeid)
            account = str(body.get("account") or meta.get("account") or "")
            validate_account_id(account)
            target_type = str(body.get("target_type") or "iso").strip().lower()
            folder_key = "template/iso" if target_type == "iso" else "template/cache"
            folders = meta.get("folders") or {}
            target_folder_id = str(folders.get(folder_key) or "")
            if not target_folder_id:
                layout = app.drive.ensure_storage_layout(
                    account_id=account,
                    drive_id=str(meta.get("drive_id") or ""),
                    subfolder=str(meta.get("subfolder") or ""),
                    pinned_root_folder_id=meta.get("root_folder_id"),
                )
                target_folder_id = str(layout["folders"][folder_key])

            res = app.drive.picker_import(
                account_id=account,
                file_ids=body.get("file_ids"),
                target_type=target_type,
                target_folder_id=target_folder_id,
                picker_access_token=body.get("access_token"),
            )
            return 200, {
                "ok": len(res["errors"]) == 0,
                "storeid": storeid,
                "imported": res["imported"],
                "errors": res["errors"],
            }

        return 404, {"error": f"Unknown route: {method} /v1/{'/'.join(sub)}"}


def create_ipc_server(config: Config, app: "DaemonApp") -> ThreadingUnixHTTPServer:
    """Create and bind the Unix domain socket HTTP server."""
    sock_dir = os.path.dirname(os.path.abspath(config.sock_path))
    ensure_dir(sock_dir, mode=0o755)
    if os.path.exists(config.sock_path):
        os.unlink(config.sock_path)
    server = ThreadingUnixHTTPServer(config.sock_path, IPCRequestHandler, app)
    os.chmod(config.sock_path, config.sock_mode)
    return server
