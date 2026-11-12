"""Backend error types."""


class BackendError(Exception):
    """A human-readable, expected backend failure.

    Callers should show ``str(error)`` to the user.  The message must never
    contain credentials, access tokens, or complete OneDrive command output.
    """


class OneDriveError(BackendError):
    """A failure while invoking or interpreting the OneDrive client."""


class FrameError(Exception):
    """A malformed or oversized Chrome native-messaging frame."""
