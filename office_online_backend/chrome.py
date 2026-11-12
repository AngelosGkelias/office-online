"""Chrome launch helpers.

The backend never installs or enables the extension and never signs the user
in.  It only asks a configured browser executable to open the extension bridge
page; the extension then owns tab mapping and navigation.
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import BackendError
from .util import extension_bridge_url

LOGGER = logging.getLogger(__name__)


@dataclass
class ChromeLauncher:
    browser: str

    def resolved_command(self) -> list[str]:
        try:
            parts = shlex.split(self.browser)
        except ValueError as error:
            raise BackendError(f"invalid browser setting: {error}") from error
        if not parts:
            raise BackendError("browser setting is empty")
        executable = parts[0]
        if os_path_like(executable):
            candidate = Path(executable).expanduser()
            if not candidate.is_file():
                raise BackendError(f"browser executable not found: {candidate}")
            parts[0] = str(candidate)
        else:
            found = shutil.which(executable)
            if found is None:
                raise BackendError(
                    f"browser executable not found: {executable} "
                    "(install Chrome or set 'browser' in config.json)"
                )
            parts[0] = found
        return parts

    def validate(self) -> list[str]:
        return self.resolved_command()

    def launch(self, extension_id: str, session_id: str) -> None:
        url = extension_bridge_url(extension_id, session_id)
        command = self.resolved_command()
        if any("{url}" in argument for argument in command[1:]):
            command = [argument.replace("{url}", url) for argument in command]
        else:
            command = command + [url]
        try:
            subprocess.Popen(
                command,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise BackendError(f"browser executable not found: {command[0]}") from error
        except OSError as error:
            raise BackendError(
                f"could not start browser {command[0]}: {error.strerror}"
            ) from error
        LOGGER.info("asked %s to open the extension bridge", command[0])


def os_path_like(executable: str) -> bool:
    return "/" in executable or executable.startswith(".")
