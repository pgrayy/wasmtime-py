"""Generated binding for pgrayy:client (background-thread approach).

Uses Func.call_async() with the socket-pair notification bridge.
Tokio's multi-thread runtime handles I/O on background threads.
When a thread completes, it writes to a socket, waking Python's
asyncio event loop to re-poll the WASM call future.

No custom Tokio runtime. No event_fd. No poll_once. No busy-polling.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from wasmtime import Config, Engine, Store, WasiConfig  # noqa: E402
from wasmtime.component import Component, Linker  # noqa: E402


class PgrayyClient:
    """Binding for pgrayy:client/pgrayy-client.

    World definition:
        package pgrayy:client@0.1.0;
        world pgrayy-client {
            export greet: func(name: string) -> string;
            export add: func(a: s32, b: s32) -> s32;
            export fetch: func(url: string) -> string;
        }
    """

    def __init__(self) -> None:
        config = Config()
        config.wasm_component_model = True
        config.wasm_component_model_async = True
        config.async_stack_size = 2 * 1024 * 1024
        engine = Engine(config)
        self._store = Store(engine)

        wasi_config = WasiConfig()
        wasi_config.inherit_stdout()
        wasi_config.inherit_stderr()
        self._store.set_wasi(wasi_config)
        self._store.set_wasi_http()

        wasm_path = Path(__file__).parent / "pgrayy-component" / "target" / "wasm32-wasip2" / "release" / "pgrayy_component.wasm"
        component = Component.from_file(engine, str(wasm_path))

        linker = Linker(engine)
        linker.add_wasip2_async()
        linker.add_wasi_http_async()
        self._instance = linker.instantiate_async(self._store, component)

        self._greet_fn = self._instance.get_func(self._store, "greet")
        self._add_fn = self._instance.get_func(self._store, "add")
        self._fetch_fn = self._instance.get_func(self._store, "fetch")

    def greet(self, name: str) -> str:
        return self._greet_fn(self._store, name)

    def add(self, a: int, b: int) -> int:
        return self._add_fn(self._store, a, b)

    async def fetch(self, url: str) -> str:
        result = await self._fetch_fn.call_async(self._store, url)
        return result
