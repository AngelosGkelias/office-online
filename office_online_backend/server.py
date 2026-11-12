"""Private newline-delimited JSON control socket.

Wire shape is exactly the contract: every response echoes the request id and
contains either ``sessions`` or an ``error``.  There are no unsolicited
messages.  The socket is 0600 inside a 0700 directory, and treatment of socket
EOF is owned by the backend (final save for sessions bound to that connection).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import stat
import threading
import uuid
from typing import Any, Optional

from .backend import BackendService
from .config import Paths
from .errors import BackendError
from .util import ensure_private_dir, redact_secrets

LOGGER = logging.getLogger(__name__)

MAX_REQUEST_LINE = 64 * 1024 * 1024


class UnixControlServer:
    def __init__(self, paths: Paths, backend: BackendService):
        self.paths = paths
        self.backend = backend
        self._socket: Optional[socket.socket] = None
        self._lock_fd: Optional[int] = None
        self._stop = threading.Event()
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._socket_inode: Optional[int] = None

    def prepare(self) -> None:
        ensure_private_dir(self.paths.socket_dir)
        self._acquire_lock()
        path = self.paths.socket_path
        try:
            try:
                info = path.lstat()
            except FileNotFoundError:
                return
            if stat.S_ISLNK(info.st_mode):
                # A symlink in our private runtime directory is not a live daemon.
                path.unlink()
                return
            if not stat.S_ISSOCK(info.st_mode):
                raise BackendError(
                    f"{path} exists and is not a Unix socket; refusing to delete it"
                )
            # The advisory daemon lock is held, so this is a stale socket.
            path.unlink()
        except Exception:
            self._release_lock()
            raise

    def _acquire_lock(self) -> None:
        lock_path = self.paths.socket_dir / "daemon.lock"
        try:
            fd = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as error:
            raise BackendError(f"could not open daemon lock: {error.strerror}") from error
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            raise BackendError(
                "another office_online daemon is already running for this runtime directory"
            ) from error
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        os.fchmod(fd, 0o600)
        self._lock_fd = fd

    def bind(self) -> None:
        path = self.paths.socket_path
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            old_umask = os.umask(0o077)
            try:
                server.bind(str(path))
            finally:
                os.umask(old_umask)
            os.chmod(path, 0o600)
            self._socket_inode = path.stat().st_ino
            server.listen(32)
            server.settimeout(0.5)
        except OSError as error:
            server.close()
            self._release_lock()
            raise BackendError(f"could not bind control socket {path}: {error}") from error
        self._socket = server
        LOGGER.info("control socket listening at %s", path)

    def serve_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        if self._socket is None:
            self.bind()
        stop = stop_event or self._stop
        while not (stop.is_set() or self._stop.is_set()):
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if stop.is_set():
                    break
                raise
            connection.setblocking(True)
            with self._clients_lock:
                self._clients.add(connection)
            thread = threading.Thread(
                target=self._handle_connection,
                args=(connection,),
                daemon=True,
                name="office-online-client",
            )
            thread.start()
        self.close()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        path = self.paths.socket_path
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if (
            info is not None
            and stat.S_ISSOCK(info.st_mode)
            and self._socket_inode is not None
            and info.st_ino == self._socket_inode
        ):
            try:
                path.unlink()
            except OSError:
                pass
        self._release_lock()

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def _handle_connection(self, connection: socket.socket) -> None:
        connection_id = uuid.uuid4().hex
        LOGGER.debug("native/client connection %s opened", connection_id)
        try:
            with connection.makefile("rb") as reader:
                while not self._stop.is_set():
                    line = reader.readline(MAX_REQUEST_LINE + 1)
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        if len(line) > MAX_REQUEST_LINE:
                            LOGGER.warning("closing oversized control-socket request")
                        break
                    payload = line[:-1]
                    if len(payload) > MAX_REQUEST_LINE:
                        LOGGER.warning("closing oversized control-socket request")
                        break
                    if not payload.strip():
                        continue
                    response = self._dispatch(payload, connection_id)
                    encoded = (
                        json.dumps(
                            response,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
                    try:
                        connection.sendall(encoded)
                    except OSError:
                        break
        except (OSError, ValueError):
            LOGGER.debug("control connection ended", exc_info=True)
        finally:
            try:
                connection.close()
            except OSError:
                pass
            with self._clients_lock:
                self._clients.discard(connection)
            LOGGER.debug("native/client connection %s closed", connection_id)
            try:
                self.backend.connection_closed(connection_id)
            except Exception:
                LOGGER.exception("failed to handle native connection close")

    def _dispatch(self, payload: bytes, connection_id: str) -> dict[str, Any]:
        request_id: Any = None
        try:
            request = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"id": None, "ok": False, "error": "invalid JSON request"}
        if not isinstance(request, dict):
            return {"id": None, "ok": False, "error": "request must be a JSON object"}
        request_id = request.get("id")
        action = request.get("action")
        if not isinstance(action, str) or not action:
            return {"id": request_id, "ok": False, "error": "missing action"}

        try:
            if action == "status":
                sessions = self.backend.public_sessions()
            elif action == "bind":
                session_id = _required_session_id(request)
                tab_id = request.get("tab_id")
                sessions = self.backend.bind(connection_id, session_id, tab_id)
            elif action == "save":
                session_id = _required_session_id(request)
                finish = request.get("finish", False)
                if not isinstance(finish, bool):
                    raise BackendError("finish must be a boolean")
                sessions = self.backend.request_save(session_id, finish)
            elif action == "tab_closed":
                session_id = _required_session_id(request)
                sessions = self.backend.tab_closed(session_id)
            elif action == "retry":
                session_id = _required_session_id(request)
                sessions = self.backend.retry(session_id)
            elif action == "cleanup":
                dry_run = request.get("dry_run", False)
                if not isinstance(dry_run, bool):
                    raise BackendError("dry_run must be a boolean")
                results = self.backend.request_cleanup(dry_run)
                return {"id": request_id, "ok": True, "cleanup": results,
                        "sessions": self.backend.public_sessions()}
            elif action == "open":
                paths = request.get("paths")
                if not isinstance(paths, list):
                    raise BackendError("paths must be a list")
                sessions = self.backend.open_paths(paths)
            else:
                return {
                    "id": request_id,
                    "ok": False,
                    "error": f"unknown action: {action}",
                }
        except BackendError as error:
            return {
                "id": request_id,
                "ok": False,
                "error": redact_secrets(str(error), limit=1000),
            }
        except Exception:
            LOGGER.exception("unexpected error handling action %s", action)
            return {
                "id": request_id,
                "ok": False,
                "error": "internal backend error",
            }
        return {"id": request_id, "ok": True, "sessions": sessions}


def _required_session_id(request: dict[str, Any]) -> str:
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise BackendError("session_id must be a non-empty string")
    return session_id
