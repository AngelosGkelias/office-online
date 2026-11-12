"""Small GTK4 upload progress window for ``office-online open --gui``.

The GUI runs as a separate, non-blocking child of the short-lived CLI process.
Progress is a newline-delimited stdin protocol:

``text MESSAGE``
    Replace the status text.
``progress PERCENT``
    Show a determinate fraction instead of the default pulsing bar.
``100``
    Upload finished; close the window immediately.
``error MESSAGE``
    Show a persistent error and a Close button.  The error window remains
    visible until the user dismisses it, even after the CLI closes stdin.

EOF closes a normal progress window.  EOF after an ``error`` message leaves the
error visible.  Closing or dismissing the window never cancels the upload; the
CLI simply keeps running and later writes are ignored.

The application ID deliberately matches both the desktop file ID and the icon
name installed per-user by ``install.py``:
``org.local.OfficeOnline.Uploader``.  GTK 4 uses the GApplication ID as the
Wayland xdg-toplevel app_id, and GNOME Shell matches that app_id against
``$XDG_DATA_HOME/applications/org.local.OfficeOnline.Uploader.desktop``.  This
is what gives the progress window its branded dock icon instead of a generic
Zenity cog.
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

APPLICATION_ID = "org.local.OfficeOnline.Uploader"
DESKTOP_FILENAME = f"{APPLICATION_ID}.desktop"
ICON_NAME = APPLICATION_ID
DEFAULT_STATUS = "Uploading working copy to OneDrive…"
WINDOW_TITLE = "Office Online Upload"
ERROR_TITLE = "Upload failed — Office Online"
MAX_MESSAGE_LENGTH = 4000


@dataclass(frozen=True)
class UploadCommand:
    """One parsed command from the CLI-to-UI pipe."""

    kind: str
    value: Optional[str] = None


def parse_command(raw_line: str) -> Optional[UploadCommand]:
    """Parse one protocol line, returning ``None`` for blank/unknown lines."""

    line = raw_line.rstrip("\r\n")
    stripped = line.strip()
    if not stripped:
        return None
    if stripped == "100":
        return UploadCommand("complete")
    command, separator, value = line.partition(" ")
    if not separator:
        return None
    value = value.strip()
    if command == "text":
        return UploadCommand("text", value)
    if command == "progress":
        return UploadCommand("progress", value)
    if command == "error":
        return UploadCommand("error", value or "Upload failed.")
    return None


class ProtocolReader:
    """Incrementally decode UTF-8 pipe bytes into protocol commands.

    Keeping this independent of GTK lets the line protocol be tested without a
    display, a session bus, or a child process.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""

    def feed(self, data: bytes) -> list[UploadCommand]:
        self._buffer += self._decoder.decode(data)
        return self._drain(final=False)

    def finish(self) -> list[UploadCommand]:
        """Flush a final unterminated line after the pipe reaches EOF."""

        self._buffer += self._decoder.decode(b"", final=True)
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[UploadCommand]:
        commands: list[UploadCommand] = []
        while "\n" in self._buffer:
            raw_line, self._buffer = self._buffer.split("\n", 1)
            command = parse_command(raw_line)
            if command is not None:
                commands.append(command)
        if final and self._buffer:
            command = parse_command(self._buffer)
            self._buffer = ""
            if command is not None:
                commands.append(command)
        return commands


@dataclass
class UploadViewState:
    """Pure state machine for the progress/error window."""

    status: str = DEFAULT_STATUS
    fraction: Optional[float] = None
    error: Optional[str] = None
    completed: bool = False

    def apply(self, command: UploadCommand) -> None:
        # Error and completion are terminal: later writes cannot resurrect a
        # window the user is already dismissing.
        if self.error is not None or self.completed:
            return
        if command.kind == "text":
            if command.value:
                self.status = command.value
        elif command.kind == "progress":
            try:
                percent = float(command.value or "")
            except ValueError:
                return
            if percent < 0:
                percent = 0
            elif percent > 100:
                percent = 100
            self.fraction = percent / 100.0
        elif command.kind == "complete":
            self.fraction = 1.0
            self.completed = True
        elif command.kind == "error":
            self.error = command.value or "Upload failed."
            self.fraction = None

    @property
    def display_text(self) -> str:
        if self.error is not None:
            return f"Upload failed\n\n{self.error}"
        return self.status

    @property
    def should_close_on_eof(self) -> bool:
        # Keep an error visible for the user; a successful/normal progress
        # window disappears with the CLI's pipe.
        return self.error is None


def _one_line(text: object) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return "Upload failed."
    if len(cleaned) > MAX_MESSAGE_LENGTH:
        cleaned = cleaned[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
    return cleaned


class UploadUIHandle:
    """Parent-side handle for the child UI's stdin protocol.

    Every write is best-effort.  A dismissed, crashed, or already-exited UI must
    never change the upload result or make the CLI raise ``BrokenPipeError``.
    """

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._closed = False

    @property
    def stdin(self) -> Optional[BinaryIO]:
        return self._process.stdin

    def update(self, message: str) -> None:
        self._send("text " + _one_line(message))

    def progress(self, percent: float) -> None:
        try:
            value = float(percent)
        except (TypeError, ValueError):
            return
        value = max(0.0, min(100.0, value))
        self._send(f"progress {value:g}")

    def complete(self) -> None:
        self._send("100")
        self.close()

    def fail(self, message: object) -> None:
        self._send("error " + _one_line(message))
        self.close()

    def close(self) -> None:
        """Close the UI's stdin, which makes a normal progress window exit."""

        if self._closed:
            return
        self._closed = True
        stream = self._process.stdin
        if stream is None:
            return
        try:
            stream.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def _send(self, line: str) -> None:
        if self._closed:
            return
        stream = self._process.stdin
        if stream is None:
            return
        try:
            payload = (line + "\n").encode("utf-8")
            stream.write(payload)
            stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            # The user dismissed the window or the child exited.  Uploads are
            # independent of the GUI, so this is intentionally non-fatal.
            self._closed = True


def gtk_available() -> bool:
    """Return whether this interpreter can import GTK 4 through PyGObject."""

    try:
        import gi

        gi.require_version("Gtk", "4.0")
    except (ImportError, ValueError, RuntimeError):
        return False
    return True


def installed_icon_path() -> Optional[Path]:
    """Locate the per-user hicolor SVG installed by ``install.py``.

    Reading the file directly for the in-window logo avoids depending on a
    possibly stale ``icon-theme.cache`` in ``$XDG_DATA_HOME/icons/hicolor``.
    """

    data_homes: list[Path] = []
    configured = os.environ.get("XDG_DATA_HOME")
    if configured:
        data_homes.append(Path(configured).expanduser())
    data_homes.append(Path.home() / ".local" / "share")
    for data_home in data_homes:
        candidate = (
            data_home
            / "icons"
            / "hicolor"
            / "scalable"
            / "apps"
            / f"{ICON_NAME}.svg"
        )
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def python_executable() -> Optional[str]:
    """Find a real Python interpreter for the standalone UI child.

    Some bundled applications expose a non-Python ``sys.executable`` (for
    example an AppImage launcher).  Prefer a python-looking current executable,
    then the normal ``python3`` on PATH, then the system interpreter.
    """

    candidates: list[Optional[str]] = []
    current = sys.executable
    if current and re.fullmatch(
        r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", Path(current).name, re.IGNORECASE
    ):
        candidates.append(current)
    candidates.extend((shutil.which("python3"), "/usr/bin/python3", current))
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            continue
    return None


def launch_upload_ui(message: str = DEFAULT_STATUS) -> Optional[UploadUIHandle]:
    """Start the standalone GTK uploader child without waiting for it."""

    if not gtk_available():
        return None
    script = Path(__file__).resolve()
    python = python_executable()
    if not python:
        return None
    try:
        process = subprocess.Popen(
            [python, str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return None
    handle = UploadUIHandle(process)
    handle.update(message)
    return handle


def run_ui() -> int:
    """Run the GTK application.  Called in the child process."""

    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, Gio, GLib, Gtk
    except (ImportError, ValueError, RuntimeError) as error:
        print(f"office-online upload UI unavailable: {error}", file=sys.stderr)
        return 2

    class UploaderApplication(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id=APPLICATION_ID,
                flags=Gio.ApplicationFlags.NON_UNIQUE,
            )
            self._state = UploadViewState()
            self._reader = ProtocolReader()
            self._window = None
            self._label = None
            self._progress = None
            self._close_button = None
            self._watch_id = 0
            self._pulse_id = 0
            self._stdin_fd: Optional[int] = None
            self.connect("activate", self._on_activate)
            self.connect("shutdown", self._on_shutdown)

        # ------------------------------------------------------------- widgets
        def _on_activate(self, _application: object) -> None:
            if self._window is not None:
                self._window.present()
                return

            window = Gtk.ApplicationWindow(application=self, title=WINDOW_TITLE)
            window.set_default_size(440, 190)
            window.set_resizable(False)
            window.connect("close-request", self._on_close_request)
            try:
                window.set_icon_name(ICON_NAME)
            except (AttributeError, TypeError):
                pass

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            box.set_margin_top(22)
            box.set_margin_bottom(20)
            box.set_margin_start(24)
            box.set_margin_end(24)
            window.set_child(box)

            icon_path = installed_icon_path()
            if icon_path is not None:
                icon = Gtk.Image.new_from_file(str(icon_path))
            else:
                icon = Gtk.Image.new_from_icon_name(ICON_NAME)
            icon.set_pixel_size(52)
            if icon_path is None:
                display = Gdk.Display.get_default()
                theme = Gtk.IconTheme.get_for_display(display) if display else None
                if theme is not None and not theme.has_icon(ICON_NAME):
                    icon.set_visible(False)
            box.append(icon)

            label = Gtk.Label(label=self._state.display_text)
            label.set_wrap(True)
            label.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
            label.set_xalign(0.0)
            label.set_max_width_chars(52)
            label.set_selectable(True)
            box.append(label)

            progress = Gtk.ProgressBar()
            progress.set_hexpand(True)
            progress.set_show_text(False)
            box.append(progress)

            button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            button_row.set_halign(Gtk.Align.END)
            close_button = Gtk.Button(label="Close")
            close_button.connect("clicked", self._on_close_clicked)
            close_button.set_visible(False)
            button_row.append(close_button)
            box.append(button_row)

            self._window = window
            self._label = label
            self._progress = progress
            self._close_button = close_button
            self._render()
            self._start_input()
            if self._window is None:
                return
            self._start_pulse()
            window.present()

        def _start_pulse(self) -> None:
            if self._pulse_id:
                return
            self._pulse_id = GLib.timeout_add(90, self._on_pulse)

        def _on_pulse(self) -> bool:
            if self._window is None or self._state.error is not None:
                self._pulse_id = 0
                return GLib.SOURCE_REMOVE
            if self._state.fraction is None and self._progress is not None:
                self._progress.pulse()
            return GLib.SOURCE_CONTINUE

        def _stop_pulse(self) -> None:
            if self._pulse_id:
                GLib.source_remove(self._pulse_id)
                self._pulse_id = 0

        def _render(self) -> None:
            if self._label is None or self._progress is None or self._close_button is None:
                return
            self._label.set_text(self._state.display_text)
            if self._state.error is not None:
                self._label.add_css_class("error")
                self._progress.set_visible(False)
                self._close_button.set_visible(True)
                if self._window is not None:
                    self._window.set_title(ERROR_TITLE)
                self._stop_pulse()
            else:
                self._label.remove_css_class("error")
                self._progress.set_visible(True)
                self._close_button.set_visible(False)
                if self._state.fraction is not None:
                    self._progress.set_fraction(self._state.fraction)
                if self._state.completed:
                    self._progress.set_fraction(1.0)

        # ---------------------------------------------------------- stdin pipe
        def _start_input(self) -> None:
            try:
                self._stdin_fd = sys.stdin.fileno()
            except (AttributeError, OSError, ValueError):
                self._finish_input()
                return
            self._watch_id = GLib.io_add_watch(
                self._stdin_fd,
                GLib.PRIORITY_DEFAULT,
                GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
                self._on_stdin_ready,
            )

        def _on_stdin_ready(self, _source: object, condition: object) -> bool:
            data = b""
            if condition & (GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR):
                try:
                    data = os.read(self._stdin_fd, 65536) if self._stdin_fd is not None else b""
                except BlockingIOError:
                    return GLib.SOURCE_CONTINUE
                except OSError:
                    data = b""

            commands = self._reader.feed(data) if data else self._reader.finish()
            for command in commands:
                self._state.apply(command)
            self._render()

            if self._state.completed:
                self._dismiss(from_watch=True)
                return GLib.SOURCE_REMOVE
            if not data:
                if self._state.should_close_on_eof:
                    self._dismiss(from_watch=True)
                else:
                    self._watch_id = 0
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        def _finish_input(self) -> None:
            """Act as if the pipe reached EOF without starting a watch."""

            for command in self._reader.finish():
                self._state.apply(command)
            self._render()
            if self._state.should_close_on_eof:
                self._dismiss()

        def _remove_watch(self) -> None:
            if self._watch_id:
                GLib.source_remove(self._watch_id)
                self._watch_id = 0

        # --------------------------------------------------------------- close
        def _on_close_clicked(self, _button: object) -> None:
            if self._window is not None:
                self._window.close()

        def _on_close_request(self, _window: object) -> bool:
            self._stop_pulse()
            self._remove_watch()
            self._window = None
            return False

        def _dismiss(self, *, from_watch: bool = False) -> None:
            self._stop_pulse()
            if from_watch:
                self._watch_id = 0
            else:
                self._remove_watch()
            window = self._window
            self._window = None
            if window is not None:
                window.close()

        def _on_shutdown(self, _application: object) -> None:
            self._stop_pulse()
            self._remove_watch()

    application = UploaderApplication()
    return int(application.run([sys.argv[0]]))


if __name__ == "__main__":
    raise SystemExit(run_ui())
