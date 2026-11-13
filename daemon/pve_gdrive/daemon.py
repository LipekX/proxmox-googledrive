"""Main pve-gdrived application lifecycle and signal handling."""

from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Optional

from .config import Config, ensure_dir
from .gdrive import DriveClient
from .ipc import ThreadingUnixHTTPServer, create_ipc_server
from .oauth import OAuthManager
from .vfs import VFSSupervisor

logger = logging.getLogger("pve-gdrived")


class DaemonApp:
    """Wires OAuthManager, DriveClient, VFSSupervisor, and IPC server."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config.from_env()
        self.oauth = OAuthManager(self.config)
        self.drive = DriveClient(self.config, self.oauth)
        self.vfs = VFSSupervisor(self.config, self.oauth, self.drive)
        self.server: Optional[ThreadingUnixHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None

    def ensure_runtime_dirs(self) -> None:
        """Create required state directories with secure permissions."""
        ensure_dir(self.config.accounts_dir, mode=0o700)
        ensure_dir(self.config.storages_dir, mode=0o700)
        ensure_dir(self.config.run_dir, mode=0o700)
        ensure_dir(self.config.cache_dir, mode=0o700)

    def start(self, background: bool = False) -> None:
        """Start the Unix domain socket IPC server."""
        self.ensure_runtime_dirs()
        self.server = create_ipc_server(self.config, self)
        if background:
            self._server_thread = threading.Thread(
                target=self.server.serve_forever,
                name="pve-gdrived-ipc",
                daemon=True,
            )
            self._server_thread.start()
        else:
            self.server.serve_forever()

    def stop(self) -> None:
        """Gracefully stop IPC server, unmount VFS supervisors, and remove socket."""
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            self.server = None
        self.vfs.shutdown_all()
        try:
            if os.path.exists(self.config.sock_path):
                os.unlink(self.config.sock_path)
        except OSError:
            pass


def run_daemon(config: Optional[Config] = None) -> int:
    """Run pve-gdrived in the foreground until SIGINT or SIGTERM."""
    logging.basicConfig(
        level=os.environ.get("PVE_GDRIVE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = DaemonApp(config=config)

    stop_once = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        if not stop_once.is_set():
            stop_once.set()
            logger.info("Received signal %d, shutting down", signum)
            threading.Thread(target=app.stop, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Starting pve-gdrived on %s", app.config.sock_path)
    try:
        app.start(background=False)
    finally:
        app.stop()
    return 0
