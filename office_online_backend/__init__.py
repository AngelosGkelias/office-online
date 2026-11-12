"""Backend implementation for the local Office Online integration.

The public entry point remains ``office_online.py``.  Keeping the backend in a
small package makes the socket/native protocol, OneDrive invocation and
filesystem-safety code independently testable.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
