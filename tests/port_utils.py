"""Shared helpers for tests (not pytest-specific)."""

import socket


def get_free_port() -> int:
    """Return an ephemeral TCP port (bind + listen, then release)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        s.listen(1)
        return s.getsockname()[1]
