"""Comprehensive unittest suite for pve-gdrived and pve_gdrive."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON_DIR = os.path.join(REPO_ROOT, "daemon")
if DAEMON_DIR not in sys.path:
    sys.path.insert(0, DAEMON_DIR)

from pve_gdrive.config import Config, atomic_write_json, ensure_dir  # noqa: E402
from pve_gdrive.daemon import DaemonApp  # noqa: E402
from pve_gdrive.gdrive import DriveClient  # noqa: E402
from pve_gdrive.ipc import ipc_request  # noqa: E402
from pve_gdrive.oauth import OAuthManager  # noqa: E402
from pve_gdrive.vfs import VFSSupervisor, compute_cache_cap, parse_size  # noqa: E402


def _b64url_decode(val: str) -> bytes:
    padded = val + "=" * ((4 - len(val) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


class _MockGoogleState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.token_requests: List[Dict[str, str]] = []
        self.about_calls = 0
        self.about_delay = 0.0
        self.about_fail = False
        self.quota_limit = 100 * 1024**3
        self.quota_usage = 25 * 1024**3
        self.drives = [{"id": "0ADriveShared1", "name": "Proxmox Team Drive"}]
        self.folders: Dict[str, Dict[str, Any]] = {}
        self.files: Dict[str, Dict[str, Any]] = {
            "picker-iso-1": {
                "id": "picker-iso-1",
                "name": "debian-12.iso",
                "mimeType": "application/octet-stream",
                "size": "655360000",
                "content": b"ISO-DATA-STREAM",
                "forbid_copy": False,
            },
            "picker-iso-2": {
                "id": "picker-iso-2",
                "name": "ubuntu-24.iso",
                "mimeType": "application/octet-stream",
                "size": "1234",
                "content": b"FALLBACK-ISO-BYTES",
                "forbid_copy": True,
            },
        }
        self.folder_seq = 0
        self.copied_seq = 0
        self.refresh_count = 0
        self.list_queries: List[Dict[str, str]] = []


class _MockGoogleHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, code: int, body: Any, content_type: str = "application/json") -> None:
        if isinstance(body, (dict, list)):
            raw = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            raw = body.encode("utf-8")
        else:
            raw = body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_bytes(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length > 0 else b""

    def do_GET(self) -> None:
        state: _MockGoogleState = self.server.state  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/oauth2/v2/userinfo":
            self._send(200, {"email": "admin@example.org"})
            return

        if path == "/drive/v3/about":
            with state.lock:
                state.about_calls += 1
                delay = state.about_delay
                fail = state.about_fail
                limit = state.quota_limit
                usage = state.quota_usage
            if delay > 0:
                time.sleep(delay)
            if fail:
                self._send(503, {"error": {"message": "Service unavailable"}})
                return
            self._send(
                200,
                {
                    "user": {"emailAddress": "admin@example.org"},
                    "storageQuota": {
                        "limit": str(limit),
                        "usage": str(usage),
                        "usageInDrive": str(usage),
                    },
                },
            )
            return

        if path == "/drive/v3/drives":
            self._send(200, {"drives": state.drives})
            return

        if path == "/drive/v3/changes/startPageToken":
            self._send(200, {"startPageToken": "tok-100"})
            return

        if path == "/drive/v3/changes":
            self._send(
                200,
                {
                    "newStartPageToken": "tok-101",
                    "changes": [
                        {
                            "fileId": "chg-file-1",
                            "removed": False,
                            "file": {"id": "chg-file-1", "name": "vzdump.vma.zst"},
                        }
                    ],
                },
            )
            return

        if path == "/drive/v3/files":
            q = (qs.get("q") or [""])[0]
            with state.lock:
                state.list_queries.append(
                    {k: v[0] for k, v in qs.items() if v}
                )
                matched = []
                for fid, fobj in state.folders.items():
                    if f"name = '{fobj['name']}'" in q and f"'{fobj['parent']}' in parents" in q:
                        matched.append(
                            {
                                "id": fid,
                                "name": fobj["name"],
                                "mimeType": fobj["mimeType"],
                                "parents": [fobj["parent"]],
                            }
                        )
            self._send(200, {"files": matched})
            return

        if path.startswith("/drive/v3/files/"):
            fid = urllib.parse.unquote(path[len("/drive/v3/files/") :])
            alt = (qs.get("alt") or [""])[0]
            with state.lock:
                obj = state.folders.get(fid) or state.files.get(fid)
            if not obj:
                self._send(404, {"error": {"message": "Not found"}})
                return
            if alt == "media":
                self._send(200, obj.get("content", b""), content_type="application/octet-stream")
                return
            self._send(
                200,
                {
                    "id": obj["id"],
                    "name": obj["name"],
                    "mimeType": obj["mimeType"],
                    "trashed": obj.get("trashed", False),
                    "size": obj.get("size", "0"),
                    "parents": [obj.get("parent", "root")],
                },
            )
            return

        self._send(404, {"error": {"message": f"Unknown mock GET {path}"}})

    def do_POST(self) -> None:
        state: _MockGoogleState = self.server.state  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        raw = self._read_bytes()

        if path == "/token":
            form = {
                k: v[0]
                for k, v in urllib.parse.parse_qs(raw.decode("utf-8")).items()
            }
            with state.lock:
                state.token_requests.append(form)
                grant = form.get("grant_type")
                if grant == "refresh_token":
                    state.refresh_count += 1
                    tok = f"refreshed-tok-{state.refresh_count}"
                elif grant == "urn:ietf:params:oauth:grant-type:jwt-bearer":
                    tok = "sa-access-token-1"
                else:
                    tok = "oauth-access-token-1"
            self._send(
                200,
                {
                    "access_token": tok,
                    "refresh_token": "mock-refresh-token-1",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )
            return

        if path == "/drive/v3/files":
            payload = json.loads(raw.decode("utf-8"))
            with state.lock:
                state.folder_seq += 1
                fid = f"folder-{state.folder_seq}"
                parent = (payload.get("parents") or ["root"])[0]
                obj = {
                    "id": fid,
                    "name": payload["name"],
                    "mimeType": payload["mimeType"],
                    "parent": parent,
                    "trashed": False,
                }
                state.folders[fid] = obj
            self._send(200, {"id": fid, "name": payload["name"]})
            return

        if path.startswith("/drive/v3/files/") and path.endswith("/copy"):
            fid = urllib.parse.unquote(path[len("/drive/v3/files/") : -len("/copy")])
            payload = json.loads(raw.decode("utf-8"))
            with state.lock:
                src = state.files.get(fid)
                if not src:
                    self._send(404, {"error": {"message": "Source not found"}})
                    return
                if src.get("forbid_copy"):
                    self._send(403, {"error": {"message": "Copy forbidden for source"}})
                    return
                state.copied_seq += 1
                new_id = f"copied-{state.copied_seq}"
                new_obj = {
                    "id": new_id,
                    "name": payload.get("name", src["name"]),
                    "size": src.get("size", "0"),
                    "parents": payload.get("parents", []),
                }
            self._send(200, new_obj)
            return

        if path == "/upload/drive/v3/files":
            with state.lock:
                state.copied_seq += 1
                new_id = f"uploaded-{state.copied_seq}"
            self._send(
                200,
                {
                    "id": new_id,
                    "name": "ubuntu-24.iso",
                    "size": str(len(b"FALLBACK-ISO-BYTES")),
                },
            )
            return

        self._send(404, {"error": {"message": f"Unknown mock POST {path}"}})


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _MockRcloneRCServer:
    """Unix-socket mock mimicking rclone --rc --rc-unix."""

    def __init__(self, sock_path: str) -> None:
        self.sock_path = sock_path
        self.lock = threading.Lock()
        self.queue: List[Dict[str, Any]] = []
        self.transferring: List[Dict[str, Any]] = []
        self.bytes_used = 1048576
        self.fatal_error = 0
        self.last_error = ""
        self.polls = 0
        self.clear_after_polls: Optional[int] = None
        self.server: Optional[socketserver.UnixStreamServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        parent = self
        if os.path.exists(self.sock_path):
            os.unlink(self.sock_path)

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def address_string(self) -> str:
                return "unix"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                with parent.lock:
                    parent.polls += 1
                    if (
                        parent.clear_after_polls is not None
                        and parent.polls >= parent.clear_after_polls
                    ):
                        parent.queue = []
                        parent.transferring = []

                    if self.path == "/vfs/stats":
                        payload = {
                            "diskCache": {
                                "bytesUsed": parent.bytes_used,
                                "uploadsInProgress": len(parent.transferring),
                                "uploadsQueued": len(parent.queue),
                            }
                        }
                    elif self.path == "/core/stats":
                        payload = {
                            "transferring": list(parent.transferring),
                            "fatalError": parent.fatal_error,
                            "errors": 1 if parent.last_error else 0,
                            "lastError": parent.last_error,
                        }
                    elif self.path == "/vfs/queue":
                        payload = {"queue": list(parent.queue)}
                    else:
                        payload = {}
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

        class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = UnixServer(self.sock_path, Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if os.path.exists(self.sock_path):
            try:
                os.unlink(self.sock_path)
            except OSError:
                pass


def _generate_test_rsa_keypair(tmp_dir: str) -> Tuple[str, str]:
    priv_path = os.path.join(tmp_dir, "sa-priv.pem")
    pub_path = os.path.join(tmp_dir, "sa-pub.pem")
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", priv_path],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "rsa", "-pubout", "-in", priv_path, "-out", pub_path],
        check=True,
        capture_output=True,
    )
    with open(priv_path, "r", encoding="utf-8") as fh:
        priv_pem = fh.read()
    return priv_pem, pub_path


def _create_mock_rclone_script(tmp_dir: str) -> str:
    """Create a tiny Python mock rclone executable that opens --rc-addr or --rc-unix and sleeps."""
    script_path = os.path.join(tmp_dir, "mock-rclone")
    code = """#!/usr/bin/env python3
import http.server, json, os, signal, socketserver, sys, time

rc_sock = None
rc_addr = None
args = sys.argv[1:]
for idx, arg in enumerate(args):
    if arg == "--rc-unix" and idx + 1 < len(args):
        rc_sock = args[idx + 1]
    elif arg == "--rc-addr" and idx + 1 < len(args):
        rc_addr = args[idx + 1]

if len(args) >= 2 and args[0] == "obscure":
    print("OBSCURED_" + args[1])
    sys.exit(0)

stop = False
def _sig(_s, _f):
    global stop
    stop = True
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def address_string(self): return "mock"
    def log_message(self, *a): return
    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        if ln: self.rfile.read(ln)
        if self.path == "/vfs/stats":
            body = {"diskCache": {"bytesUsed": 4096, "uploadsInProgress": 0, "uploadsQueued": 0}}
        elif self.path == "/core/stats":
            body = {"transferring": [], "fatalError": 0, "errors": 0}
        elif self.path == "/vfs/queue":
            body = {"queue": []}
        else:
            body = {}
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

srv = None
if rc_addr:
    host, port_s = rc_addr.rsplit(":", 1)
    class TS(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True
    srv = TS((host, int(port_s)), H)
    srv.timeout = 0.2
    while not stop:
        srv.handle_request()
    srv.server_close()
elif rc_sock:
    if os.path.exists(rc_sock):
        os.unlink(rc_sock)
    class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True
    srv = S(rc_sock, H)
    srv.timeout = 0.2
    while not stop:
        srv.handle_request()
    srv.server_close()
else:
    while not stop:
        time.sleep(0.2)
"""
    with open(script_path, "w", encoding="utf-8") as fh:
        fh.write(code)
    os.chmod(script_path, 0o755)
    return script_path


class DaemonTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.state = _MockGoogleState()
        self.httpd = _ThreadingHTTPServer(("127.0.0.1", 0), _MockGoogleHandler)
        self.httpd.state = self.state  # type: ignore[attr-defined]
        self.http_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.http_thread.start()
        port = self.httpd.server_address[1]
        self.mock_url = f"http://127.0.0.1:{port}"
        self.mock_rclone = _create_mock_rclone_script(self.base)

        self.config = Config(
            accounts_dir=os.path.join(self.base, "accounts"),
            storages_dir=os.path.join(self.base, "storages"),
            sock_path=os.path.join(self.base, "run", "pve-gdrived.sock"),
            run_dir=os.path.join(self.base, "run", "pve-gdrive"),
            cache_dir=os.path.join(self.base, "cache"),
            mnt_base=os.path.join(self.base, "mnt"),
            google_token_url=f"{self.mock_url}/token",
            drive_api_base=f"{self.mock_url}/drive/v3",
            drive_upload_base=f"{self.mock_url}/upload/drive/v3",
            rclone_bin=self.mock_rclone,
            quota_ttl=60.0,
            status_timeout=0.5,
        )

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()


class TestConfigAndPermissions(DaemonTestBase):
    def test_atomic_write_json_and_dir_permissions(self) -> None:
        target = os.path.join(self.config.accounts_dir, "acct1.json")
        atomic_write_json(target, {"id": "acct1", "secret": "xyz"})
        dir_mode = stat.S_IMODE(os.stat(self.config.accounts_dir).st_mode)
        file_mode = stat.S_IMODE(os.stat(target).st_mode)
        self.assertEqual(dir_mode, 0o700)
        self.assertEqual(file_mode, 0o600)

    def test_pmxcfs_skips_chmod_on_etc_pve(self) -> None:
        with mock.patch("os.makedirs"), mock.patch("os.chmod") as mock_chmod:
            ensure_dir("/etc/pve/priv/gdrive/accounts", mode=0o700)
            mock_chmod.assert_not_called()


class TestOAuthAndServiceAccount(DaemonTestBase):
    def test_service_account_in_memory_refresh_without_disk_rewrite(self) -> None:
        priv_pem, _pub_path = _generate_test_rsa_keypair(self.base)
        oauth = OAuthManager(self.config)
        acct = oauth.add_service_account(
            {
                "client_email": "pve-sa@example.iam.gserviceaccount.com",
                "private_key": priv_pem,
                "private_key_id": "key-1",
                "token_uri": f"{self.mock_url}/token",
            }
        )
        self.assertEqual(acct["mode"], "service_account")
        self.assertEqual(acct["email"], "pve-sa@example.iam.gserviceaccount.com")

        profile_path = os.path.join(self.config.accounts_dir, f"{acct['id']}.json")
        mtime_before = os.stat(profile_path).st_mtime_ns
        with open(profile_path, "r", encoding="utf-8") as fh:
            raw_before = fh.read()

        # First call exchanges JWT for access token and caches in memory
        tok1 = oauth.get_access_token(acct["id"])
        self.assertEqual(tok1, "sa-access-token-1")
        self.assertEqual(len(self.state.token_requests), 1)

        # Second call returns cached token without network call
        tok2 = oauth.get_access_token(acct["id"])
        self.assertEqual(tok2, "sa-access-token-1")
        self.assertEqual(len(self.state.token_requests), 1)

        # Force expiry in memory and verify refresh does NOT touch pmxcfs profile file
        oauth._token_cache[acct["id"]]["expires_at"] = time.time() - 10
        tok3 = oauth.get_access_token(acct["id"])
        self.assertEqual(tok3, "sa-access-token-1")
        self.assertEqual(len(self.state.token_requests), 2)
        self.assertEqual(os.stat(profile_path).st_mtime_ns, mtime_before)
        with open(profile_path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), raw_before)

        # Verify public listing strips private_key
        listed = oauth.list_accounts()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("private_key", listed[0])

    def test_service_account_jwt_signed_and_verified_by_openssl(self) -> None:
        priv_pem, pub_path = _generate_test_rsa_keypair(self.base)
        oauth = OAuthManager(self.config)
        acct = oauth.add_service_account(
            {
                "client_email": "pve-sa@example.iam.gserviceaccount.com",
                "private_key": priv_pem,
                "private_key_id": "key-1",
                "token_uri": f"{self.mock_url}/token",
            }
        )
        self.assertEqual(acct["mode"], "service_account")
        tok = oauth.get_access_token(acct["id"])
        self.assertEqual(tok, "sa-access-token-1")

        req = self.state.token_requests[-1]
        self.assertEqual(req["grant_type"], "urn:ietf:params:oauth:grant-type:jwt-bearer")
        assertion = req["assertion"]
        hdr_b64, claims_b64, sig_b64 = assertion.split(".")
        claims = json.loads(_b64url_decode(claims_b64))
        self.assertEqual(claims["iss"], "pve-sa@example.iam.gserviceaccount.com")

        sig_file = os.path.join(self.base, "jwt.sig")
        with open(sig_file, "wb") as fh:
            fh.write(_b64url_decode(sig_b64))
        verify = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", pub_path, "-signature", sig_file],
            input=f"{hdr_b64}.{claims_b64}".encode("ascii"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr.decode())

    def test_standard_oauth_login_and_in_memory_refresh(self) -> None:
        oauth = OAuthManager(self.config)
        started = oauth.start_oauth_login()
        self.assertIn("accounts.google.com/o/oauth2/auth", started["auth_url"])
        self.assertTrue(started["state"])

        acct = oauth.finish_oauth_login(
            f"http://127.0.0.1:53682/?state={started['state']}&code=auth-code-xyz",
            state=started["state"],
        )
        self.assertEqual(acct["mode"], "oauth")
        self.assertEqual(acct["email"], "admin@example.org")

        # Subsequent get_access_token uses cached token, and refresh uses refresh_token
        oauth._token_cache[acct["id"]]["expires_at"] = time.time() - 10
        refreshed = oauth.get_access_token(acct["id"])
        self.assertTrue(refreshed.startswith("refreshed-tok-"))


class TestDriveClient(DaemonTestBase):
    def _create_acct(self, oauth: OAuthManager) -> str:
        priv_pem, _ = _generate_test_rsa_keypair(self.base)
        acct = oauth.add_service_account(
            {
                "client_email": "drive-sa@example.iam.gserviceaccount.com",
                "private_key": priv_pem,
                "token_uri": f"{self.mock_url}/token",
            }
        )
        return acct["id"]

    def test_quota_ttl_cache_and_non_blocking_stale_fallback(self) -> None:
        oauth = OAuthManager(self.config)
        drive = DriveClient(self.config, oauth)
        acct_id = self._create_acct(oauth)

        q1 = drive.get_quota(acct_id)
        self.assertIsNotNone(q1)
        self.assertEqual(q1["limit"], 100 * 1024**3)
        self.assertFalse(q1["stale"])
        self.assertEqual(self.state.about_calls, 1)

        # Within TTL -> served from cache
        q2 = drive.get_quota(acct_id)
        self.assertEqual(self.state.about_calls, 1)
        self.assertEqual(q2["free"], 75 * 1024**3)

        # Expire TTL and simulate slow/offline WAN (> status_timeout)
        drive._quota_cache[acct_id]["fetched_at"] -= 1000.0
        self.state.about_delay = 1.0
        t0 = time.monotonic()
        q3 = drive.get_quota(acct_id, timeout=0.15, allow_stale=True)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.8)
        self.assertIsNotNone(q3)
        self.assertTrue(q3["stale"])
        self.assertEqual(q3["limit"], 100 * 1024**3)

    def test_drives_folder_pinning_changes_and_picker_import(self) -> None:
        oauth = OAuthManager(self.config)
        drive = DriveClient(self.config, oauth)
        acct_id = self._create_acct(oauth)

        drives = drive.list_drives(acct_id)
        self.assertEqual(drives[0]["name"], "My Drive")
        self.assertEqual(drives[1]["id"], "0ADriveShared1")

        # First layout creation builds proxmox/cluster-a/{dump,template/{iso,cache}}
        layout1 = drive.ensure_storage_layout(
            acct_id, drive_id="0ADriveShared1", subfolder="cluster-a"
        )
        self.assertTrue(layout1["root_folder_id"].startswith("folder-"))
        self.assertEqual(
            set(layout1["folders"].keys()),
            {"dump", "template", "template/iso", "template/cache"},
        )
        # Verify supportsAllDrives=true on Drive queries
        self.assertTrue(
            any(q.get("supportsAllDrives") == "true" for q in self.state.list_queries)
        )

        # Second call with pinned_root_folder_id reuses same root_folder_id deterministically
        folder_count_before = self.state.folder_seq
        layout2 = drive.ensure_storage_layout(
            acct_id,
            drive_id="0ADriveShared1",
            subfolder="cluster-a",
            pinned_root_folder_id=layout1["root_folder_id"],
        )
        self.assertEqual(layout2["root_folder_id"], layout1["root_folder_id"])
        self.assertEqual(self.state.folder_seq, folder_count_before)

        # changes.list helper
        tok = drive.get_start_page_token(acct_id, drive_id="0ADriveShared1")
        self.assertEqual(tok, "tok-100")
        chg = drive.list_changes(acct_id, tok, drive_id="0ADriveShared1")
        self.assertEqual(chg["new_start_page_token"], "tok-101")
        self.assertEqual(len(chg["changes"]), 1)

        # picker_import: item 1 server-side copy, item 2 fallback download+upload
        imp = drive.picker_import(
            account_id=acct_id,
            file_ids=["picker-iso-1", "picker-iso-2"],
            target_type="iso",
            target_folder_id=layout1["folders"]["template/iso"],
            picker_access_token="picker-tok",
        )
        self.assertEqual(imp["errors"], [])
        self.assertEqual(len(imp["imported"]), 2)
        self.assertEqual(imp["imported"][0]["method"], "copy")
        self.assertEqual(imp["imported"][1]["method"], "download_upload")


class TestVFSAndRcloneConfig(DaemonTestBase):
    def test_statvfs_auto_cap_calculation(self) -> None:
        # Case 1: 40 GiB free -> 25% = 10 GiB (< 20 GiB cap)
        fake_small = os.statvfs_result((4096, 4096, 100, 10485760, 10485760, 0, 0, 0, 0, 255))
        with mock.patch("os.statvfs", return_value=fake_small):
            res = compute_cache_cap(self.config.cache_dir, None)
        self.assertEqual(res["cap_bytes"], 10 * 1024**3)
        self.assertEqual(res["cap_rclone"], "10G")
        self.assertTrue(res["auto_capped"])

        # Case 2: 200 GiB free -> capped at 20 GiB
        fake_large = os.statvfs_result((4096, 4096, 100, 52428800, 52428800, 0, 0, 0, 0, 255))
        with mock.patch("os.statvfs", return_value=fake_large):
            res2 = compute_cache_cap(self.config.cache_dir, None)
        self.assertEqual(res2["cap_bytes"], 20 * 1024**3)
        self.assertEqual(res2["cap_rclone"], "20G")

        # Case 3: explicit cache_max_size overrides auto-cap
        res3 = compute_cache_cap(self.config.cache_dir, "512M")
        self.assertEqual(res3["cap_bytes"], parse_size("512M"))
        self.assertFalse(res3["auto_capped"])

    def test_rclone_conf_crypt_layering_and_wait_for_upload(self) -> None:
        oauth = OAuthManager(self.config)
        drive = DriveClient(self.config, oauth)
        vfs = VFSSupervisor(self.config, oauth, drive)

        priv_pem, _ = _generate_test_rsa_keypair(self.base)
        acct = oauth.add_service_account(
            {
                "client_email": "vfs-sa@example.iam.gserviceaccount.com",
                "private_key": priv_pem,
                "token_uri": f"{self.mock_url}/token",
            }
        )
        vfs.init_storage(
            storeid="gdrive-enc",
            account=acct["id"],
            encrypt=True,
            crypt_password="super-secret-passphrase",
            crypt_password2="optional-salt",
        )
        meta = vfs.get_storage_meta("gdrive-enc")
        conf_path, target = vfs.write_rclone_config("gdrive-enc", meta)
        self.assertEqual(target, "crypt_gdrive-enc:")
        self.assertEqual(stat.S_IMODE(os.stat(conf_path).st_mode), 0o600)
        with open(conf_path, "r", encoding="utf-8") as fh:
            conf_text = fh.read()
        self.assertIn("[gdrive_gdrive-enc]", conf_text)
        self.assertIn("[crypt_gdrive-enc]", conf_text)
        self.assertIn("type = crypt", conf_text)
        self.assertIn("OBSCURED_super-secret-passphrase", conf_text)

        # Verify wait_for_upload polls RC queue until clear
        ensure_dir(self.config.run_dir, mode=0o700)
        rc_sock = os.path.join(self.config.run_dir, "rc-gdrive-enc.sock")
        rc_srv = _MockRcloneRCServer(rc_sock)
        rc_srv.queue = [{"name": "dump/vzdump-qemu-100.vma.zst", "size": 1000, "uploading": True, "tries": 0}]
        rc_srv.transferring = [{"name": "dump/vzdump-qemu-100.vma.zst", "size": 1000, "bytes": 500, "percentage": 50, "speed": 100.0}]
        rc_srv.clear_after_polls = 2
        rc_srv.start()
        try:
            res = vfs.wait_for_upload(
                "gdrive-enc",
                os.path.join(self.config.mnt_base, "gdrive-enc", "dump", "vzdump-qemu-100.vma.zst"),
                timeout_sec=5.0,
                poll_interval=0.05,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["path"], "dump/vzdump-qemu-100.vma.zst")

            # Verify fatal upload error fails loudly
            rc_srv.queue = [{"name": "dump/bad.vma", "size": 100, "uploading": True, "tries": 1, "error": "quotaExceeded"}]
            rc_srv.clear_after_polls = None
            with self.assertRaises(RuntimeError):
                vfs.wait_for_upload("gdrive-enc", "dump/bad.vma", timeout_sec=2.0, poll_interval=0.05)
        finally:
            rc_srv.stop()


class TestUnixSocketIPCAndCLI(DaemonTestBase):
    def test_full_ipc_lifecycle_and_cli(self) -> None:
        app = DaemonApp(self.config)
        app.start(background=True)
        try:
            # Wait briefly for socket bind
            for _ in range(20):
                if os.path.exists(self.config.sock_path):
                    break
                time.sleep(0.05)

            code, health = ipc_request(self.config.sock_path, "GET", "/v1/health")
            self.assertEqual(code, 200)
            self.assertTrue(health["ok"])

            # Service Account registration
            priv_pem, _ = _generate_test_rsa_keypair(self.base)
            code, sa_res = ipc_request(
                self.config.sock_path,
                "POST",
                "/v1/accounts/service-account",
                {
                    "account_id": "sa-ipc",
                    "service_account_json": json.dumps(
                        {
                            "client_email": "ipc-sa@example.iam.gserviceaccount.com",
                            "private_key": priv_pem,
                            "token_uri": f"{self.mock_url}/token",
                        }
                    ),
                },
            )
            self.assertEqual(code, 200)
            acct_id = sa_res["account"]["id"]

            # GET /v1/accounts
            code, accts = ipc_request(self.config.sock_path, "GET", "/v1/accounts")
            self.assertEqual(code, 200)
            self.assertEqual(len(accts["accounts"]), 1)
            self.assertIsNotNone(accts["accounts"][0]["quota"])

            # GET /v1/accounts/<id>/drives
            code, drives = ipc_request(self.config.sock_path, "GET", f"/v1/accounts/{acct_id}/drives")
            self.assertEqual(code, 200)
            self.assertEqual(len(drives["drives"]), 2)

            # POST /v1/storage/init & /v1/storage/activate
            code, init_res = ipc_request(
                self.config.sock_path,
                "POST",
                "/v1/storage/init",
                {"storeid": "gd1", "account": acct_id, "drive_id": "0ADriveShared1"},
            )
            self.assertEqual(code, 200)
            self.assertTrue(init_res["root_folder_id"])

            code, act_res = ipc_request(
                self.config.sock_path,
                "POST",
                "/v1/storage/activate",
                {"storeid": "gd1"},
            )
            self.assertEqual(code, 200)
            self.assertTrue(act_res["active"])

            # GET /v1/storage/gd1/status
            code, st_res = ipc_request(self.config.sock_path, "GET", "/v1/storage/gd1/status")
            self.assertEqual(code, 200)
            self.assertTrue(st_res["active"])
            self.assertTrue(st_res["mounted"])
            self.assertEqual(st_res["total"], 100 * 1024**3)
            self.assertEqual(st_res["avail"], 75 * 1024**3)

            # GET /v1/queue & POST /v1/uploads/wait
            code, q_res = ipc_request(self.config.sock_path, "GET", "/v1/queue")
            self.assertEqual(code, 200)
            self.assertIn("gd1", q_res["storages"])
            self.assertGreater(q_res["storages"]["gd1"]["cache_max"], 0)

            code, w_res = ipc_request(
                self.config.sock_path,
                "POST",
                "/v1/uploads/wait",
                {"storeid": "gd1", "path": "dump/test.vma.zst", "timeout_sec": 5},
            )
            self.assertEqual(code, 200)
            self.assertEqual(w_res["status"], "uploaded")

            # POST /v1/picker/import
            code, p_res = ipc_request(
                self.config.sock_path,
                "POST",
                "/v1/picker/import",
                {"storeid": "gd1", "file_ids": ["picker-iso-1"], "target_type": "iso"},
            )
            self.assertEqual(code, 200)
            self.assertTrue(p_res["ok"])
            self.assertEqual(len(p_res["imported"]), 1)

            # CLI helper smoke tests
            cli_bin = os.path.join(DAEMON_DIR, "pve-gdrive")
            cli_status = subprocess.run(
                [cli_bin, "--sock", self.config.sock_path, "--json", "status", "gd1"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertTrue(json.loads(cli_status.stdout)["active"])

            cli_wait = subprocess.run(
                [cli_bin, "--sock", self.config.sock_path, "wait-upload", "gd1", "dump/test.vma.zst", "--timeout", "5"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("Uploaded:", cli_wait.stdout)

            # Deactivate & delete account
            code, deact = ipc_request(
                self.config.sock_path, "POST", "/v1/storage/deactivate", {"storeid": "gd1"}
            )
            self.assertEqual(code, 200)
            self.assertFalse(deact["active"])

            code, deleted = ipc_request(
                self.config.sock_path, "DELETE", f"/v1/accounts/{acct_id}"
            )
            self.assertEqual(code, 200)
            self.assertEqual(deleted["deleted"], acct_id)
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
