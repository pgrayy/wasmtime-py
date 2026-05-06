"""Async event loop driver for wasmtime.

Integrates wasmtime's async runtime with Python's asyncio event loop.
The driver is task-agnostic: it only manages the event loop integration
and completion polling. Tasks are spawned from Rust; the host awaits
their completion by ID.

Platform strategies:
- Unix: Single fd. Python monitors wasmtime's kqueue/epoll fd with
  add_reader. A call_soon safety net guards against missed wakeups.
- Windows: wasmtime runs on a background thread. Python monitors a
  notification socket via ProactorEventLoop's sock_recv.
"""

import asyncio
import ctypes
import socket
import sys
from pathlib import Path
from typing import Dict, Optional


def _default_lib() -> ctypes.CDLL:
    """Load the default wasmtime shared library (lazy, only when no lib is passed)."""
    try:
        from ._ffi import dll
        return dll
    except (ImportError, OSError) as e:
        raise RuntimeError(
            "Could not load bundled wasmtime library. "
            "Pass a lib= argument to AsyncDriver with a locally-built library."
        ) from e


def _bind_async_driver(lib: ctypes.CDLL) -> None:
    """Bind the async driver C API functions on a library handle."""

    class _opaque(ctypes.Structure):
        pass

    P = ctypes.POINTER(_opaque)

    lib._async_driver_ptr_type = P

    lib.wasmtime_async_driver_new.restype = P
    lib.wasmtime_async_driver_new.argtypes = []

    lib.wasmtime_async_driver_delete.restype = None
    lib.wasmtime_async_driver_delete.argtypes = [P]

    lib.wasmtime_async_driver_io_fd.restype = ctypes.c_int64
    lib.wasmtime_async_driver_io_fd.argtypes = [P]

    lib.wasmtime_async_driver_set_notify_socket.restype = None
    lib.wasmtime_async_driver_set_notify_socket.argtypes = [P, ctypes.c_uint64]

    lib.wasmtime_async_driver_start_background.restype = None
    lib.wasmtime_async_driver_start_background.argtypes = [P]

    lib.wasmtime_async_driver_poll_once.restype = ctypes.c_int64
    lib.wasmtime_async_driver_poll_once.argtypes = [P]

    lib.wasmtime_async_driver_task_poll.restype = ctypes.c_int32
    lib.wasmtime_async_driver_task_poll.argtypes = [P, ctypes.c_uint64]

    lib.wasmtime_async_driver_task_get_response.restype = ctypes.c_void_p
    lib.wasmtime_async_driver_task_get_response.argtypes = [P, ctypes.c_uint64]

    lib.wasmtime_async_driver_task_get_error.restype = ctypes.c_void_p
    lib.wasmtime_async_driver_task_get_error.argtypes = [P, ctypes.c_uint64]

    lib.wasmtime_async_driver_free_string.restype = None
    lib.wasmtime_async_driver_free_string.argtypes = [ctypes.c_void_p]


# Task status constants (must match C enum)
_TASK_PENDING = 0
_TASK_OK = 1
_TASK_ERROR = -1


class AsyncDriver:
    """Drives wasmtime's async runtime from Python's asyncio event loop.

    Task-agnostic: the driver only manages event loop integration and
    completion polling. Tasks are spawned from Rust; Python awaits their
    completion by ID via :meth:`wait_for`.

    Args:
        lib: Optional ctypes.CDLL to use instead of the bundled wasmtime
             library. Useful for development when loading a locally-built
             libwasmtime.dylib.

    Example::

        driver = AsyncDriver()
        try:
            # task_id comes from a Rust function that calls spawn_task()
            result = await driver.wait_for(task_id)
        finally:
            driver.close()

    Or as an async context manager::

        async with AsyncDriver() as driver:
            result = await driver.wait_for(task_id)
    """

    def __init__(self, lib: Optional[ctypes.CDLL] = None) -> None:
        self._lib = lib or _default_lib()

        # Bind if not already bound
        if not hasattr(self._lib, '_async_driver_ptr_type'):
            _bind_async_driver(self._lib)

        self._ptr = self._lib.wasmtime_async_driver_new()
        if not self._ptr:
            raise RuntimeError("failed to create async driver")

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._waiters: Dict[int, asyncio.Future] = {}
        self._registered = False
        self._timer_handle: Optional[asyncio.TimerHandle] = None
        self._safety_scheduled = False
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False

        if sys.platform == "win32":
            self._notify_sock, notify_write = socket.socketpair(
                socket.AF_INET, socket.SOCK_STREAM
            )
            self._notify_sock.setblocking(False)
            self._lib.wasmtime_async_driver_set_notify_socket(
                self._ptr, notify_write.fileno()
            )
            notify_write.detach()
            self._lib.wasmtime_async_driver_start_background(self._ptr)
            self._io_fd = -1
        else:
            self._notify_sock = None
            self._io_fd = self._lib.wasmtime_async_driver_io_fd(self._ptr)

    @property
    def ptr(self):
        """Raw C pointer, for passing to C spawn functions."""
        return self._ptr

    @property
    def lib(self) -> ctypes.CDLL:
        """The underlying library handle."""
        return self._lib

    def wait_for(self, task_id: int) -> "asyncio.Future[str]":
        """Return a future that resolves when the given task completes.

        The task must have already been spawned from Rust.

        Args:
            task_id: The task ID returned by the Rust spawning function.

        Returns:
            A future that resolves to the task's result string.

        Raises:
            RuntimeError: If the driver has been closed or the task fails.
        """
        if self._closed:
            raise RuntimeError("AsyncDriver has been closed")

        loop = asyncio.get_running_loop()
        self._ensure_registered(loop)

        future: asyncio.Future[str] = loop.create_future()
        self._waiters[task_id] = future
        return future

    def close(self) -> None:
        """Close the driver and release all resources."""
        if self._closed:
            return

        self._unregister()

        for future in self._waiters.values():
            if not future.done():
                future.cancel()
        self._waiters.clear()

        if self._ptr:
            self._lib.wasmtime_async_driver_delete(self._ptr)
            self._ptr = None

        if self._notify_sock is not None:
            self._notify_sock.close()
            self._notify_sock = None

        self._closed = True

    async def __aenter__(self) -> "AsyncDriver":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        if not self._closed:
            self.close()

    # -----------------------------------------------------------------------
    # Internal: event loop registration
    # -----------------------------------------------------------------------

    def _ensure_registered(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._registered:
            return

        if sys.platform == "win32":
            self._reader_task = loop.create_task(self._windows_reader())
        else:
            loop.add_reader(self._io_fd, self._on_event)

        self._registered = True

    def _unregister(self) -> None:
        if not self._registered or not self._loop:
            return

        if sys.platform == "win32":
            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()
            self._reader_task = None
        else:
            self._loop.remove_reader(self._io_fd)

        if self._timer_handle:
            self._timer_handle.cancel()
            self._timer_handle = None

        self._safety_scheduled = False
        self._registered = False

    # -----------------------------------------------------------------------
    # Internal: platform-specific event handling
    # -----------------------------------------------------------------------

    async def _windows_reader(self) -> None:
        """Continuously wait for notifications from the background thread."""
        try:
            while True:
                await self._loop.sock_recv(self._notify_sock, 64)
                self._check_waiters()
        except (asyncio.CancelledError, OSError):
            pass

    def _on_event(self) -> None:
        """Unix: called by asyncio when the io_fd is readable."""
        self._safety_scheduled = False
        next_ms = self._lib.wasmtime_async_driver_poll_once(self._ptr)
        self._check_waiters()

        if not self._waiters:
            self._unregister()
            return

        if not self._safety_scheduled:
            self._loop.call_soon(self._safety_poll)
            self._safety_scheduled = True

        if next_ms > 0:
            if self._timer_handle:
                self._timer_handle.cancel()
            self._timer_handle = self._loop.call_later(
                next_ms / 1000.0, self._on_event
            )

    def _safety_poll(self) -> None:
        """Safety net: one extra poll to catch wakeups in the edge gap."""
        self._safety_scheduled = False
        if not self._waiters or not self._registered:
            return

        next_ms = self._lib.wasmtime_async_driver_poll_once(self._ptr)
        self._check_waiters()

        if not self._waiters:
            self._unregister()
            return

        if next_ms > 0:
            if self._timer_handle:
                self._timer_handle.cancel()
            self._timer_handle = self._loop.call_later(
                next_ms / 1000.0, self._on_event
            )

    def _check_waiters(self) -> None:
        """Resolve futures for any completed tasks."""
        done = []
        for task_id, future in self._waiters.items():
            if future.done():
                done.append(task_id)
                continue

            status = self._lib.wasmtime_async_driver_task_poll(self._ptr, task_id)
            if status == _TASK_OK:
                ptr = self._lib.wasmtime_async_driver_task_get_response(
                    self._ptr, task_id
                )
                if ptr:
                    value = ctypes.string_at(ptr).decode("utf-8", errors="replace")
                    self._lib.wasmtime_async_driver_free_string(ptr)
                else:
                    value = ""
                future.set_result(value)
                done.append(task_id)
            elif status == _TASK_ERROR:
                ptr = self._lib.wasmtime_async_driver_task_get_error(
                    self._ptr, task_id
                )
                if ptr:
                    msg = ctypes.string_at(ptr).decode("utf-8", errors="replace")
                    self._lib.wasmtime_async_driver_free_string(ptr)
                else:
                    msg = "unknown error"
                future.set_exception(RuntimeError(msg))
                done.append(task_id)

        for task_id in done:
            del self._waiters[task_id]

        if not self._waiters:
            self._unregister()

    async def yield_once(self) -> None:
        """Yield to asyncio and tick Tokio once.

        Used by Func.call_async to efficiently wait for WASM I/O to complete.
        Instead of busy-polling with asyncio.sleep(0), this ticks Tokio's
        event loop (advancing any pending I/O) and yields to asyncio so
        other tasks can run.
        """
        self._lib.wasmtime_async_driver_poll_once(self._ptr)
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Module-level driver (shared across all call_async invocations)
# ---------------------------------------------------------------------------

_global_driver: Optional["AsyncDriver"] = None


def get_or_create_driver() -> "AsyncDriver":
    """Get or create the global AsyncDriver instance.

    The driver is created lazily on first use and shared across all
    async component function calls in the process.
    """
    global _global_driver
    if _global_driver is None or _global_driver._closed:
        _global_driver = AsyncDriver()
    return _global_driver