"""Small safety, filesystem, URL and desktop-notification helpers.

The backend deliberately uses only the Python standard library.  Anything that
touches a user file is written through a temporary file in the destination
directory and then atomically renamed, after taking a private backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlsplit

from .errors import BackendError

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

DOCUMENT_SUFFIXES = {".docx", ".pptx", ".xlsx"}
EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")

_MICROSOFT_HOST_SUFFIXES = (
    ".sharepoint.com",
    ".onedrive.com",
    ".onedrive.live.com",
    ".microsoftpersonalcontent.com",
    ".live.com",
    ".live.net",
    ".office.com",
    ".office.net",
    ".officeapps.live.com",
    ".microsoft.com",
    ".microsoftonline.com",
    ".microsoft365.com",
    ".cloud.microsoft",
)
_MICROSOFT_EXACT_HOSTS = {"1drv.ms", "onedrive.live.com"}
_URL_RE = re.compile(r"https://[^\s<>\"']+", re.IGNORECASE)
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|client_secret|code)"
    r"\s*[:=]\s*[\"']?([^&\s\"']+)"
)


def utcnow_iso() -> str:
    """Return a timezone-aware ISO-8601 timestamp."""

    return datetime.now(timezone.utc).isoformat()


def ensure_private_dir(path: Path) -> Path:
    """Create *path* (and parents) with a private mode and no symlink target.

    Only the requested directory is chmod'ed; XDG parents belong to the user's
    desktop session and are not modified.
    """

    path = Path(path)
    old_umask = os.umask(0o077)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    finally:
        os.umask(old_umask)

    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise BackendError(f"refusing to use symlinked directory: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise BackendError(f"expected a directory: {path}")
    os.chmod(path, PRIVATE_DIR_MODE)
    return path


def ensure_private_file(path: Path) -> Path:
    """Check that an existing file is a regular non-symlink and make it 0600."""

    path = Path(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise BackendError(f"refusing to use symlinked file: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise BackendError(f"expected a regular file: {path}")
    os.chmod(path, PRIVATE_FILE_MODE)
    return path


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, mode: int = PRIVATE_FILE_MODE) -> None:
    """Atomically replace *path* with *data* (0600 by default)."""

    path = Path(path)
    parent = path.parent
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(parent)
    except Exception:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any, mode: int = PRIVATE_FILE_MODE) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    atomic_write_bytes(path, payload, mode=mode)


def read_file_no_follow(path: Path, max_bytes: Optional[int] = None) -> bytes:
    """Read a regular file without following a symlink."""

    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise BackendError(f"could not open {path}: {error.strerror}") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise BackendError(f"expected a regular file: {path}")
        if max_bytes is not None and info.st_size > max_bytes:
            raise BackendError(f"file is unexpectedly large: {path}")
        chunks = []
        with os.fdopen(fd, "rb", closefd=False) as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                if max_bytes is not None and sum(len(c) for c in chunks) > max_bytes:
                    raise BackendError(f"file is unexpectedly large: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def replace_with_backup(path: Path, data: bytes, mode: int = PRIVATE_FILE_MODE) -> None:
    """Replace *path* atomically while keeping the previous bytes in .bak.

    The old bytes are written to ``<path>.bak`` first.  If the process dies
    before the main replace, the main file is still the complete old version;
    if it dies afterwards, the backup is complete as well.
    """

    path = Path(path)
    backup = path.with_name(path.name + ".bak")
    if path.exists() or path.is_symlink():
        old = read_file_no_follow(path)
        atomic_write_bytes(backup, old, mode=mode)
    atomic_write_bytes(path, data, mode=mode)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_file_atomic(
    source: Path,
    destination: Path,
    *,
    mode: int = PRIVATE_FILE_MODE,
    preserve_metadata: bool = False,
) -> tuple[str, int]:
    """Copy source bytes to destination via a same-directory temp + rename.

    Returns ``(sha256, size)`` of the copied bytes.  The source is read
    normally (the caller has already resolved intentional user symlinks), but
    the destination is always replaced as a regular file.
    """

    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    try:
        info = destination.lstat()
    except FileNotFoundError:
        info = None
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise BackendError(f"refusing to replace symlink: {destination}")

    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary_path = Path(temporary)
    digest = hashlib.sha256()
    copied = 0
    source_stat = source.stat()
    try:
        with open(source, "rb") as reader, os.fdopen(fd, "wb") as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(temporary_path, mode)
        if preserve_metadata:
            os.utime(
                temporary_path,
                ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            )
        os.replace(temporary_path, destination)
        _fsync_directory(destination.parent)
    except Exception:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise
    return digest.hexdigest(), copied


def copy_file_verified(source: Path, destination: Path, mode: int = PRIVATE_FILE_MODE) -> str:
    """Copy a new file and verify the source and destination hashes match."""

    before = sha256_file(source)
    copied, _ = copy_file_atomic(source, destination, mode=mode, preserve_metadata=True)
    if copied != before:
        try:
            Path(destination).unlink()
        except OSError:
            pass
        raise BackendError(f"copy verification failed for {Path(source).name}")
    return copied


def copy_to_unique_sibling(
    source: Path,
    directory: Path,
    proposed_name: str,
    *,
    mode: int = PRIVATE_FILE_MODE,
) -> tuple[Path, str]:
    """Atomically create a uniquely named regular file in *directory*.

    A temporary file is copied first, then hard-linked to a candidate name with
    ``O_EXCL`` semantics.  This means an existing user file is never replaced.
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    fd, temporary = tempfile.mkstemp(prefix=".office-online-", suffix=".tmp", dir=directory)
    temporary_path = Path(temporary)
    digest = hashlib.sha256()
    try:
        with open(source, "rb") as reader, os.fdopen(fd, "wb") as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.chmod(temporary_path, mode)
        candidate = directory / proposed_name
        counter = 2
        while True:
            try:
                os.link(temporary_path, candidate)
                break
            except FileExistsError:
                candidate = directory / _numbered_name(proposed_name, counter)
                counter += 1
        _fsync_directory(directory)
        return candidate, digest.hexdigest()
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass


def _numbered_name(name: str, number: int) -> str:
    path = Path(name)
    return f"{path.stem} ({number}){path.suffix}"


def conflict_sibling_path(original: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = Path(original)
    label = f" (office-online conflict {stamp})"
    # Keep the sibling name below the usual 255-byte filename limit even when
    # the original name is already close to it.
    stem = path.stem
    maximum = 240 - len(label) - len(path.suffix)
    if maximum < 1:
        stem = "document"
    elif len(stem) > maximum:
        stem = stem[:maximum].rstrip(" .") or "document"
    return f"{stem}{label}{path.suffix}"


def resolve_document_path(raw: str) -> Path:
    """Validate a CLI path or ``file://`` URI and return its resolved path.

    No shell expansion, globbing, or command substitution is performed.
    ``~`` is expanded as a convenience, and a file URI is decoded with
    ``urllib``.  Only DOCX/PPTX/XLSX regular files are accepted.
    """

    if not isinstance(raw, str) or not raw:
        raise BackendError("empty document path")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise BackendError("document path contains an invalid control character")

    if raw.lower().startswith("file:"):
        parsed = urlsplit(raw)
        if parsed.scheme.lower() != "file":
            raise BackendError("only file:// URIs are supported")
        if parsed.netloc not in ("", "localhost"):
            raise BackendError("file:// URI must refer to this machine")
        if parsed.query or parsed.fragment:
            raise BackendError("file:// URI must not contain a query or fragment")
        candidate = unquote(parsed.path)
        if not candidate:
            raise BackendError("empty file:// URI")
        if any(character in candidate for character in ("\x00", "\n", "\r")):
            raise BackendError("file:// URI contains an invalid control character")
    else:
        if "://" in raw:
            raise BackendError("only local paths and file:// URIs are supported")
        candidate = raw

    path = Path(candidate).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise BackendError(f"file does not exist: {path}") from error
    except OSError as error:
        raise BackendError(f"could not resolve {path}: {error.strerror}") from error

    if not resolved.is_file():
        raise BackendError(f"not a regular file: {resolved}")
    if resolved.suffix.lower() not in DOCUMENT_SUFFIXES:
        raise BackendError(
            f"unsupported file type {resolved.suffix or '(none)'}; "
            "expected DOCX, PPTX or XLSX"
        )
    if resolved.name.startswith("~$"):
        raise BackendError("that looks like a temporary Office lock file, not a document")
    return resolved


def sanitize_onedrive_filename(name: str) -> str:
    """Return a OneDrive-safe filename while preserving the document suffix.

    The original user filename remains in ``Session.name``; this value is only
    used for the upload working copy and remote path.
    """

    base = Path(name).name
    suffix = Path(base).suffix
    stem = Path(base).stem
    invalid = set('"*:<>?/\\|') | {chr(code) for code in range(0x20)}
    stem = "".join("_" if character in invalid else character for character in stem)
    stem = stem.replace("_vti_", "_vti-").strip().rstrip(" .")
    if not stem:
        stem = "document"
    reserved = {
        ".lock",
        "con",
        "prn",
        "aux",
        "nul",
        "desktop.ini",
    } | {f"com{i}" for i in range(10)} | {f"lpt{i}" for i in range(10)}
    if stem.lower() in reserved:
        stem = f"_{stem}"
    max_stem = 180 - len(suffix)
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip(" .")
    if not suffix:
        suffix = ""
    return f"{stem}{suffix}"


def extension_id_is_valid(extension_id: str) -> bool:
    return bool(EXTENSION_ID_RE.fullmatch(extension_id or ""))


def extension_bridge_url(extension_id: str, session_id: str) -> str:
    if not extension_id_is_valid(extension_id):
        raise BackendError(
            "extension_id is not configured correctly in config.json "
            "(expected 32 characters in a-p)"
        )
    from urllib.parse import quote

    return (
        f"chrome-extension://{extension_id}/open.html?"
        f"session={quote(session_id, safe='')}"
    )


def is_microsoft_https_url(url: str) -> bool:
    """Accept only credential-free HTTPS Microsoft document/share URLs."""

    if not isinstance(url, str) or not url:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    host = parsed.hostname.lower().rstrip(".")
    if host in _MICROSOFT_EXACT_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _MICROSOFT_HOST_SUFFIXES)


def urls_from_output(output: str) -> list[str]:
    found: list[str] = []
    for match in _URL_RE.finditer(output or ""):
        candidate = match.group(0).rstrip(").,;:")
        if is_microsoft_https_url(candidate) and candidate not in found:
            found.append(candidate)
    return found


def url_from_link_output(output: str) -> Optional[str]:
    """Find a link in current abraunegg/onedrive output.

    ``--get-file-link`` prints ``WebURL: ...`` and ``--create-share-link``
    prints ``File Shareable Link: ...``.  Prefer those lines, then fall back to
    any Microsoft HTTPS URL for future output-format changes.
    """

    preferred: list[str] = []
    for line in (output or "").splitlines():
        lowered = line.lower()
        if (
            "weburl" in lowered
            or "file shareable link" in lowered
            or "shareable link:" in lowered
        ):
            preferred.extend(urls_from_output(line))
    if preferred:
        return preferred[0]
    urls = urls_from_output(output)
    return urls[0] if urls else None


def redact_secrets(text: str, limit: int = 500) -> str:
    """Make subprocess output safe for an error message or a log line."""

    sanitized = _SECRET_PARAM_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text or "")
    sanitized = sanitized.replace("\x00", " ")
    sanitized = " ".join(sanitized.split())
    if len(sanitized) > limit:
        sanitized = sanitized[: limit - 1] + "…"
    return sanitized


def process_error_text(stderr: str, stdout: str, limit: int = 500) -> str:
    """Pick the most useful short, redacted line from process output."""

    for stream in (stderr, stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return redact_secrets(" | ".join(lines[-3:]), limit=limit)
    return ""


def notify_desktop(
    title: str,
    body: str,
    *,
    urgency: str = "normal",
    runner: Any = None,
) -> bool:
    """Best-effort desktop notification via ``notify-send``.

    Never raises: notifications are a UX nicety, not part of save correctness.
    """

    executable = shutil.which("notify-send")
    if executable is None:
        return False
    title = redact_secrets(str(title), limit=120)
    body = redact_secrets(str(body), limit=400)
    if urgency not in {"low", "normal", "critical"}:
        urgency = "normal"
    invoke = runner or subprocess.run
    try:
        invoke(
            [
                executable,
                "--app-name=Office Online",
                f"--urgency={urgency}",
                "--",
                title,
                body,
            ],
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return True
    except Exception:
        return False


def stable_json_id(value: Any) -> str:
    """Canonical JSON representation used to match request/response ids."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def iter_nonempty_lines(chunks: Iterable[bytes]) -> Iterable[bytes]:
    """Test/debug helper: yield newline-delimited non-empty chunks."""

    remainder = b""
    for chunk in chunks:
        remainder += chunk
        while b"\n" in remainder:
            line, remainder = remainder.split(b"\n", 1)
            if line:
                yield line
    if remainder:
        yield remainder
