"""End-to-end async demo for CI validation."""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from wasmtime import Config, Engine, Store, WasiConfig
from wasmtime.component import Component, Linker


async def main() -> None:
    config = Config()
    config.wasm_component_model = True
    config.wasm_component_model_async = True
    config.async_stack_size = 2 * 1024 * 1024
    config.async_allow_sync = True
    engine = Engine(config)
    store = Store(engine)

    wasi_config = WasiConfig()
    wasi_config.inherit_stdout()
    wasi_config.inherit_stderr()
    store.set_wasi(wasi_config)
    store.set_wasi_http()

    wasm_path = Path(__file__).parent / "pgrayy-component" / "target" / "wasm32-wasip2" / "release" / "pgrayy_component.wasm"
    component = Component.from_file(engine, str(wasm_path))

    linker = Linker(engine)
    linker.add_wasip2_async()
    linker.add_wasi_http_async()
    instance = linker.instantiate(store, component)

    greet_fn = instance.get_func(store, "greet")
    add_fn = instance.get_func(store, "add")
    fetch_fn = instance.get_func(store, "fetch")

    print(f"greet: {greet_fn(store, 'CI')}")
    print(f"add:   {add_fn(store, 1, 2)}")

    start = time.monotonic()
    result = await fetch_fn.call_async(store, "http://httpbin.org/ip")
    elapsed = (time.monotonic() - start) * 1000
    print(f"fetch: {result[:50]}... ({elapsed:.0f}ms)")

    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
