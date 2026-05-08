"""Async WASM component demo.

Shows Python asyncio tasks running concurrently with a WASM HTTP call.
The WASM guest uses wasi:http backed by Tokio. Python's event loop
stays responsive during the call via a socket-pair notification bridge.

To run:
    cd examples/async_component
    python demo.py
"""

import asyncio
import time
from client import PgrayyClient


def section(title: str) -> None:
    print()
    print(f"{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")
    print()


async def main() -> None:
    client = PgrayyClient()

    section("1. Sync calls (pure WASM compute)")

    print(f"  greet:  {client.greet('pgrayy')}")
    print(f"  add:    17 + 25 = {client.add(17, 25)}")

    section("2. Python tasks run DURING a WASM call")
    print("  A slow HTTP call (1s) runs while Python tasks complete alongside it.")
    print()

    completed = []

    async def python_task(name: str, delay: float, t0: float) -> None:
        await asyncio.sleep(delay)
        completed.append((time.monotonic() - t0, "Python", name))

    async def wasm_task(t0: float) -> None:
        await client.fetch("http://httpbin.org/delay/1")
        completed.append((time.monotonic() - t0, "WASM", "HTTP /delay/1"))

    start = time.monotonic()
    await asyncio.gather(
        wasm_task(start),
        python_task("sleep 100ms", 0.1, start),
        python_task("sleep 2000ms", 2.0, start),
    )
    total = (time.monotonic() - start) * 1000

    completed.sort(key=lambda x: x[0])
    print("  completed_at  task")
    print("  ------------  ----")
    for elapsed_s, label, name in completed:
        print(f"  {elapsed_s*1000:>8.0f}ms   [{label:6}] {name}")

    print()
    print(f"  total:  {total:.0f}ms")
    print()
    print("-" * 60)
    print("  Python tasks completed while WASM was still waiting.")
    print("-" * 60)


if __name__ == "__main__":
    asyncio.run(main())
