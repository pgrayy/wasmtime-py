"""Background-thread bridge for async WASM calls.

Uses a socket pair to receive wake notifications from wasmtime's
background Tokio runtime. When a WASM call future's internal tasks
complete, Tokio writes a byte to the notification socket, and Python's
asyncio event loop wakes up to re-poll the future.

Works on both Unix (selector) and Windows (proactor) event loops.
"""

import socket
import sys


class NotifyBridge:
    """Manages a socket pair for wake notifications from Tokio.

    The write end is passed to Rust via wasmtime_call_future_poll_with_notify.
    When background Tokio threads complete work, the Waker writes a byte.
    Python monitors the read end via asyncio.
    """

    def __init__(self) -> None:
        family = socket.AF_UNIX if sys.platform != "win32" else socket.AF_INET
        self._read_sock, self._write_sock = socket.socketpair(family, socket.SOCK_STREAM)
        self._read_sock.setblocking(False)
        self._write_sock.setblocking(False)

    @property
    def write_fd(self) -> int:
        return self._write_sock.fileno()

    @property
    def read_sock(self) -> socket.socket:
        return self._read_sock

    def close(self) -> None:
        self._read_sock.close()
        self._write_sock.close()
