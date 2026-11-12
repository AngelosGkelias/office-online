"""Durable session records.

``sessions.json`` is private, written atomically, and retains one previous
version in ``sessions.json.bak``.  A corrupt main file is recovered from the
backup if possible instead of silently discarding session state.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .config import Paths
from .errors import BackendError
from .util import ensure_private_dir, read_file_no_follow, replace_with_backup, utcnow_iso

LOGGER = logging.getLogger(__name__)

STATES = {"opening", "editing", "saving", "saved", "error", "conflict"}
PUBLIC_FIELDS = (
    "session_id",
    "name",
    "original_path",
    "url",
    "state",
    "tab_id",
    "error",
    "last_saved_at",
    "result_path",
    "cloud_deleted_at",
)

_STRING_FIELDS = (
    "name",
    "original_path",
    "url",
    "error",
    "last_saved_at",
    "result_path",
    "created_at",
    "working_path",
    "remote_dir",
    "remote_relpath",
    "local_hash",
    "backup_path",
    "link_mode",
    "cloud_deleted_at",
)
_OPTIONAL_STRING_FIELDS = {
    "created_at",
    "url",
    "error",
    "last_saved_at",
    "result_path",
    "working_path",
    "remote_dir",
    "remote_relpath",
    "local_hash",
    "backup_path",
    "link_mode",
    "cloud_deleted_at",
}


@dataclass
class Session:
    session_id: str
    name: str
    original_path: str
    url: Optional[str]
    state: str
    tab_id: Optional[int]
    error: Optional[str]
    last_saved_at: Optional[str]
    result_path: Optional[str]
    created_at: str = field(default_factory=utcnow_iso)
    working_path: Optional[str] = None
    remote_dir: Optional[str] = None
    remote_relpath: Optional[str] = None
    local_hash: Optional[str] = None
    backup_path: Optional[str] = None
    link_mode: Optional[str] = None
    cloud_deleted_at: Optional[str] = None
    finish_requested: bool = False
    recovery_only: bool = False
    retry_count: int = 0
    # Connection ids are deliberately not persisted: they only describe the
    # current daemon process's live native-host connections.
    bound_connection: Optional[str] = None

    def public(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in PUBLIC_FIELDS}

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name in (
            "session_id",
            *_STRING_FIELDS,
            "tab_id",
            "state",
            "finish_requested",
            "recovery_only",
            "retry_count",
        ):
            value[name] = getattr(self, name)
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "Session":
        if not isinstance(value, dict):
            raise BackendError("sessions.json contains a non-object session entry")
        session_id = value.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise BackendError("sessions.json contains a session without a valid id")
        state = value.get("state")
        if not isinstance(state, str) or state not in STATES:
            raise BackendError(f"session {session_id} has an invalid state")
        for name in _STRING_FIELDS:
            item = value.get(name)
            if item is None and name in _OPTIONAL_STRING_FIELDS:
                continue
            if not isinstance(item, str):
                raise BackendError(f"session {session_id}: field {name} must be a string")
        tab_id = value.get("tab_id")
        if tab_id is not None and (
            not isinstance(tab_id, int) or isinstance(tab_id, bool) or tab_id < 0
        ):
            raise BackendError(f"session {session_id}: tab_id must be a non-negative integer")
        finish = value.get("finish_requested", False)
        recovery = value.get("recovery_only", False)
        retries = value.get("retry_count", 0)
        if not isinstance(finish, bool) or not isinstance(recovery, bool):
            raise BackendError(f"session {session_id}: invalid boolean flag")
        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
            raise BackendError(f"session {session_id}: invalid retry_count")
        name = value.get("name")
        original_path = value.get("original_path")
        if not isinstance(name, str) or not name:
            raise BackendError(f"session {session_id}: missing name")
        if not isinstance(original_path, str) or not original_path:
            raise BackendError(f"session {session_id}: missing original_path")
        return cls(
            session_id=session_id,
            name=name,
            original_path=original_path,
            url=value.get("url"),
            state=state,
            tab_id=tab_id,
            error=value.get("error"),
            last_saved_at=value.get("last_saved_at"),
            result_path=value.get("result_path"),
            created_at=value.get("created_at") or utcnow_iso(),
            working_path=value.get("working_path"),
            remote_dir=value.get("remote_dir"),
            remote_relpath=value.get("remote_relpath"),
            local_hash=value.get("local_hash"),
            backup_path=value.get("backup_path"),
            link_mode=value.get("link_mode"),
            cloud_deleted_at=value.get("cloud_deleted_at"),
            finish_requested=finish,
            recovery_only=recovery,
            retry_count=retries,
        )


class SessionStore:
    """Thread-safe session collection and durable JSON persistence."""

    def __init__(self, paths: Paths):
        self.paths = paths
        ensure_private_dir(paths.data_dir)
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self.recovered_from_backup = False
        self._load()

    def _load(self) -> None:
        main = self.paths.sessions_file
        backup = main.with_name(main.name + ".bak")
        last_error: Optional[Exception] = None
        found_any = False
        for candidate, is_backup in ((main, False), (backup, True)):
            if not candidate.exists() and not candidate.is_symlink():
                continue
            found_any = True
            try:
                raw = read_file_no_follow(candidate, max_bytes=16 * 1024 * 1024)
                records = _decode_sessions(raw)
            except (BackendError, ValueError) as error:
                last_error = error
                continue
            self._sessions = {record.session_id: record for record in records}
            if is_backup:
                self.recovered_from_backup = True
                LOGGER.warning("sessions.json was unreadable; recovered %s", backup)
            return
        if found_any and last_error is not None:
            raise BackendError(f"could not load {main}: {last_error}") from last_error

    def _persist_locked(self) -> None:
        payload = {
            "version": 1,
            "sessions": [
                session.to_dict()
                for session in sorted(
                    self._sessions.values(), key=lambda item: (item.created_at, item.session_id)
                )
            ],
        }
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        replace_with_backup(self.paths.sessions_file, data)

    def snapshot(self, session_id: str) -> Optional[Session]:
        with self._lock:
            session = self._sessions.get(session_id)
            return copy.deepcopy(session) if session else None

    def snapshots(self) -> list[Session]:
        with self._lock:
            return [
                copy.deepcopy(item)
                for item in sorted(
                    self._sessions.values(), key=lambda session: (session.created_at, session.session_id)
                )
            ]

    def public_sessions(self) -> list[dict[str, Any]]:
        return [session.public() for session in self.snapshots()]

    def add(self, session: Session) -> Session:
        with self._lock:
            if session.session_id in self._sessions:
                raise BackendError(f"duplicate session id: {session.session_id}")
            self._sessions[session.session_id] = copy.deepcopy(session)
            self._persist_locked()
            return copy.deepcopy(session)

    def update(self, session_id: str, **changes: Any) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise BackendError(f"unknown session: {session_id}")
            _apply_changes(session, changes)
            self._persist_locked()
            return copy.deepcopy(session)

    def update_if(
        self,
        session_id: str,
        predicate: Callable[[Session], bool],
        **changes: Any,
    ) -> Optional[Session]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not predicate(session):
                return None
            _apply_changes(session, changes)
            self._persist_locked()
            return copy.deepcopy(session)

    def prune(self, session_ids: Iterable[Any] | str) -> list[str]:
        """Durably remove completed, cloud-confirmed session history records.

        Only ``saved`` sessions carrying ``cloud_deleted_at`` may be pruned;
        this mirrors the cleanup invariant that history disappears only after
        the cloud deletion was confirmed.  The write is performed twice on
        purpose: ``replace_with_backup`` preserves the *previous* main file in
        ``sessions.json.bak``, so a single write would leave the just-pruned
        records recoverable from the fallback.  Writing the sanitized snapshot
        twice leaves both files clean and creates no ``.bak.bak``.  If either
        write fails, the in-memory records are restored so a later cleanup can
        retry instead of losing the marker.
        """

        values: Iterable[Any]
        if isinstance(session_ids, str):
            values = (session_ids,)
        elif isinstance(session_ids, Session):
            values = (session_ids,)
        elif session_ids is None:
            values = ()
        else:
            values = session_ids
        requested = set()
        for item in values:
            if isinstance(item, str) and item:
                requested.add(item)
            else:
                session_id = getattr(item, "session_id", None)
                if isinstance(session_id, str) and session_id:
                    requested.add(session_id)
        with self._lock:
            removable = [
                session_id
                for session_id, session in self._sessions.items()
                if session_id in requested
                and session.state == "saved"
                and bool(session.cloud_deleted_at)
            ]
            if not removable:
                return []
            removed = {
                session_id: self._sessions.pop(session_id)
                for session_id in removable
            }
            try:
                self._persist_locked()
                # The first write copied the pre-prune snapshot into .bak;
                # repeat with the already-sanitized snapshot so recovery
                # cannot resurrect expired records.
                self._persist_locked()
            except BaseException:
                self._sessions.update(removed)
                raise
            return sorted(removable)

    def find_by_original(self, original_path: str) -> Optional[Session]:
        with self._lock:
            matches = [
                session
                for session in self._sessions.values()
                if session.original_path == original_path
            ]
            if not matches:
                return None
            matches.sort(key=lambda session: (session.created_at, session.session_id))
            return copy.deepcopy(matches[-1])

    def is_empty(self) -> bool:
        with self._lock:
            return not self._sessions


def _apply_changes(session: Session, changes: dict[str, Any]) -> None:
    allowed = set(Session.__dataclass_fields__)
    for name, value in changes.items():
        if name not in allowed:
            raise BackendError(f"internal error: unknown session field {name}")
        if name == "state" and value not in STATES:
            raise BackendError(f"internal error: invalid session state {value}")
        setattr(session, name, value)


def _decode_sessions(raw: bytes) -> list[Session]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON: {error}") from error
    if isinstance(decoded, list):
        records = decoded
    elif isinstance(decoded, dict):
        records = decoded.get("sessions")
        if not isinstance(records, list):
            raise ValueError("missing sessions array")
    else:
        raise ValueError("expected a JSON object or array")
    return [Session.from_dict(record) for record in records]
