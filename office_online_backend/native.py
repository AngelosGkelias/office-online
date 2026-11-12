"""Chrome native-messaging host proxy.

Frames are 4-byte little-endian lengths followed by exact UTF-8 JSON bodies.
This module enforces Chrome's limits: 64 MiB from Chrome to the host and 1 MiB
from the host to Chrome.  It never writes anything to stdout except response
frames, and it never creates unsolicited messages.
"""

from __future__ import annotations

import json
import logging
import os
import selectors
import socket
import sys
import time
from typing import Optional

from .config import Paths
from .errors import FrameError
from .util import stable_json_id

LOGGER = logging.getLogger(__name__)

MAX_INPUT_FRAME = 64 * 1024 * 1024
MAX_OUTPUT_FRAME = 1024 * 1024
MAX_RESPONSE_LINE = MAX_OUTPUT_FRAME
DRAIN_SECONDS = 2.0


def encode_frame(body: bytes) -> bytes:
    length = len(body)
    if length <= 0:
        raise FrameError("native message body is empty")
    if length > MAX_OUTPUT_FRAME:
        raise FrameError(
            f"native message is too large for Chrome ({length} > {MAX_OUTPUT_FRAME} bytes)"
        )
    return length.to_bytes(4, "little") + body


def pop_frames(
    buffer: bytearray, max_frame: int = MAX_INPUT_FRAME
) -> list[bytes]:
    """Remove and return every complete frame from *buffer*.

    Partial trailing bytes stay in the buffer for the next read.  An invalid
    length raises immediately instead of allocating attacker-controlled data.
    """

    frames: list[bytes] = []
    offset = 0
    while len(buffer) - offset >= 4:
        length = int.from_bytes(buffer[offset : offset + 4], "little")
        if length <= 0:
            raise FrameError("native message length must be positive")
        if length > max_frame:
            raise FrameError(
                f"native message length {length} exceeds limit {max_frame}"
            )
        end = offset + 4 + length
        if end > len(buffer):
            break
        frames.append(bytes(buffer[offset + 4 : end]))
        offset = end
    if offset:
        del buffer[:offset]
    return frames


def run_native_host(paths: Optional[Paths] = None) -> int:
    paths = paths or Paths.from_env()
    socket_path = paths.socket_path
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(socket_path))
        sock.settimeout(None)
    except OSError as error:
        LOGGER.error("cannot connect to the office_online daemon socket: %s", error)
        try:
            sock.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return 1

    selector = selectors.DefaultSelector()
    stdin_fd = sys.stdin.fileno() if hasattr(sys.stdin, "fileno") else 0
    stdout_fd = sys.stdout.fileno() if hasattr(sys.stdout, "fileno") else 1
    input_buffer = bytearray()
    socket_buffer = bytearray()
    pending: set[str] = set()
    stdin_open = True
    drain_deadline: Optional[float] = None

    try:
        selector.register(stdin_fd, selectors.EVENT_READ, "stdin")
        selector.register(sock.fileno(), selectors.EVENT_READ, "socket")
        while True:
            events = selector.select(0.2)
            for key, _ in events:
                if key.data == "stdin":
                    try:
                        chunk = os.read(stdin_fd, 64 * 1024)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        stdin_open = False
                        try:
                            selector.unregister(stdin_fd)
                        except Exception:
                            pass
                        try:
                            sock.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        drain_deadline = time.monotonic() + DRAIN_SECONDS
                        continue
                    input_buffer.extend(chunk)
                    try:
                        bodies = pop_frames(input_buffer)
                    except FrameError as error:
                        LOGGER.error("invalid native frame: %s", error)
                        return 1
                    for body in bodies:
                        if not _forward_request(body, sock, stdout_fd, pending):
                            return 1
                elif key.data == "socket":
                    try:
                        chunk = sock.recv(64 * 1024)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        LOGGER.error("the office_online daemon closed the native proxy connection")
                        return 1
                    socket_buffer.extend(chunk)
                    if len(socket_buffer) > MAX_RESPONSE_LINE + 1 and b"\n" not in socket_buffer:
                        LOGGER.error("oversized response from the office_online daemon")
                        return 1
                    while b"\n" in socket_buffer:
                        line, _, remainder = socket_buffer.partition(b"\n")
                        socket_buffer = bytearray(remainder)
                        if not line:
                            continue
                        if not _forward_response(line, stdout_fd, pending):
                            return 1
            if not stdin_open:
                if not pending:
                    break
                if drain_deadline is not None and time.monotonic() >= drain_deadline:
                    break
    finally:
        try:
            selector.close()
        except Exception:
            pass
        try:
            sock.close()
        except OSError:
            pass
    return 0


def _forward_request(
    body: bytes, sock: socket.socket, stdout_fd: int, pending: set[str]
) -> bool:
    try:
        request = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _write_error(stdout_fd, "invalid JSON request")
    if not isinstance(request, dict) or "id" not in request:
        return _write_error(stdout_fd, "request must be a JSON object with an id")
    try:
        line = json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        return _write_error(stdout_fd, "request is not valid JSON")
    if len(line) > MAX_INPUT_FRAME:
        return _write_error(stdout_fd, "request is too large")
    try:
        sock.sendall(line + b"\n")
    except OSError as error:
        LOGGER.error("could not forward native request: %s", error)
        return False
    pending.add(stable_json_id(request.get("id")))
    return True


def _write_error(stdout_fd: int, message: str) -> bool:
    payload = json.dumps(
        {"id": None, "ok": False, "error": message},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        _write_all(stdout_fd, encode_frame(payload))
        return True
    except (OSError, FrameError) as error:
        LOGGER.error("could not write native error response: %s", error)
        return False


def _forward_response(line: bytes, stdout_fd: int, pending: set[str]) -> bool:
    try:
        response = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        LOGGER.error("daemon sent invalid JSON; closing native proxy")
        return False
    if not isinstance(response, dict) or "id" not in response:
        LOGGER.error("daemon response has no id; closing native proxy")
        return False
    try:
        frame = encode_frame(line)
    except FrameError as error:
        LOGGER.error("cannot send daemon response to Chrome: %s", error)
        return False
    try:
        _write_all(stdout_fd, frame)
    except OSError as error:
        LOGGER.error("could not write native response: %s", error)
        return False
    pending.discard(stable_json_id(response.get("id")))
    return True


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to native stdout")
        view = view[written:]
