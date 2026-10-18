"""Measure every zip service the same way, through the gateway, and print one table.

Each request streams its upload while reading the archive at the same time (full duplex,
as the contract allows), so "first byte" shows when a service starts answering: a
streaming service answers during the upload, a buffering one only after it.

Environment:
    GATEWAY_URL   e.g. http://gateway:8000 (plain HTTP only: run it next to the gateway)
    API_KEY       a key from GATEWAY_API_KEYS
    SERVICES      comma-separated, e.g. claude,grok,dsh
    SIZE_MIB      size of the single-file uploads (default 100)
    PARALLEL      concurrent requests in the parallel scenario (default 4)
    CSV           optional path: also write the results as CSV
"""

import asyncio
import csv
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import h11

GATEWAY = urlsplit(os.environ.get("GATEWAY_URL", "http://gateway:8000"))
API_KEY = os.environ.get("API_KEY", "")
SERVICES = [s for s in os.environ.get("SERVICES", "claude,grok,dsh").split(",") if s]
SIZE_MIB = int(os.environ.get("SIZE_MIB", "100"))
PARALLEL = int(os.environ.get("PARALLEL", "4"))
MIB = 1024 * 1024
# 1 MiB of random bytes, repeated: still incompressible for deflate (its window is 32 KiB)
# and costs the client nothing to produce.
RANDOM_BLOCK = os.urandom(MIB)
ZERO_BLOCK = bytes(MIB)


@dataclass
class Part:
    name: str
    size: int
    block: bytes


@dataclass
class Result:
    status: int
    bytes_in: int
    bytes_out: int
    first_byte_s: float | None
    total_s: float
    error: str = ""


def multipart(parts: list[Part], boundary: str) -> tuple[int, Callable[[], AsyncIterator[bytes]]]:
    """The body's length, and a generator for it (built on the fly, never in memory)."""
    heads = [
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
            f"filename=\"{p.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        ).encode()
        for p in parts
    ]
    tail = f"--{boundary}--\r\n".encode()
    length = sum(len(h) + p.size + 2 for h, p in zip(heads, parts, strict=True)) + len(tail)

    async def body() -> AsyncIterator[bytes]:
        for head, part in zip(heads, parts, strict=True):
            yield head
            left = part.size
            while left:
                chunk = part.block[: min(left, len(part.block))]
                left -= len(chunk)
                yield chunk
            yield b"\r\n"
        yield tail

    return length, body


async def zip_request(service: str, parts: list[Part]) -> Result:
    boundary = uuid.uuid4().hex
    length, body = multipart(parts, boundary)
    bytes_in = sum(p.size for p in parts)
    started = time.perf_counter()
    reader, writer = await asyncio.open_connection(GATEWAY.hostname, GATEWAY.port or 80)
    conn = h11.Connection(h11.CLIENT)
    status, bytes_out, first = 0, 0, None
    response_done = asyncio.Event()

    def send(event: h11.Event) -> None:
        data = conn.send(event)
        if data:
            writer.write(data)

    async def upload() -> None:
        send(
            h11.Request(
                method="POST",
                target=f"/{service}/api/v1/zip",
                headers=[
                    ("Host", GATEWAY.netloc),
                    ("Authorization", f"Bearer {API_KEY}"),
                    ("Content-Type", f"multipart/form-data; boundary={boundary}"),
                    ("Content-Length", str(length)),
                ],
            )
        )
        async for chunk in body():
            if response_done.is_set():
                return  # the service answered early (an error): stop sending
            send(h11.Data(data=chunk))
            await writer.drain()
        send(h11.EndOfMessage())
        await writer.drain()

    async def download() -> None:
        nonlocal status, bytes_out, first
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                data = await reader.read(256 * 1024)
                conn.receive_data(data)
                if not data and conn.their_state is not h11.DONE:
                    raise ConnectionError("connection closed mid-response")
                continue
            if isinstance(event, h11.Response):
                status = event.status_code
            elif isinstance(event, h11.Data):
                if first is None:
                    first = time.perf_counter() - started
                bytes_out += len(event.data)
            elif isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
                response_done.set()
                return

    error = ""
    sending = asyncio.create_task(upload())
    try:
        await download()
        if status == 200:
            await sending
    except Exception as exc:  # report it in the table rather than abort the run
        error = f"{type(exc).__name__}: {exc}"[:80]
    finally:
        sending.cancel()
        writer.close()
    return Result(status, bytes_in, bytes_out, first, time.perf_counter() - started, error)


SCENARIOS: dict[str, Callable[[], list[list[Part]]]] = {
    f"1 x {SIZE_MIB} MiB random": lambda: [[Part("random.bin", SIZE_MIB * MIB, RANDOM_BLOCK)]],
    f"1 x {SIZE_MIB} MiB zeros": lambda: [[Part("zeros.bin", SIZE_MIB * MIB, ZERO_BLOCK)]],
    "500 x 4 KiB random": lambda: [
        [Part(f"f{i:03}.bin", 4096, RANDOM_BLOCK[(i % 256) * 4096 :][:4096]) for i in range(500)]
    ],
    f"{PARALLEL} parallel x {SIZE_MIB // PARALLEL} MiB random": lambda: [
        [Part(f"p{n}.bin", SIZE_MIB // PARALLEL * MIB, RANDOM_BLOCK)] for n in range(PARALLEL)
    ],
}


async def run_scenario(service: str, requests: list[list[Part]]) -> dict[str, str]:
    started = time.perf_counter()
    results = await asyncio.gather(*(zip_request(service, parts) for parts in requests))
    wall = time.perf_counter() - started
    bytes_in = sum(r.bytes_in for r in results)
    ok = [r for r in results if r.status == 200 and not r.error]
    firsts = [r.first_byte_s for r in ok if r.first_byte_s is not None]
    statuses = sorted({str(r.status) for r in results})
    return {
        "MiB/s": f"{bytes_in / MIB / wall:.1f}" if len(ok) == len(results) else "-",
        "total s": f"{wall:.2f}",
        "first byte s": f"{max(firsts):.2f}" if firsts else "-",
        "out MiB": f"{sum(r.bytes_out for r in results) / MIB:.1f}",
        "status": ",".join(statuses) + (f" ({results[0].error})" if results[0].error else ""),
    }


async def main() -> int:
    if not API_KEY:
        print("API_KEY is not set", file=sys.stderr)
        return 2
    rows = []
    for service in SERVICES:
        for name, build in SCENARIOS.items():
            print(f"{service}: {name} ...", file=sys.stderr, flush=True)
            rows.append({"service": service, "scenario": name, **await run_scenario(service, build())})
    columns = list(rows[0])
    print("| " + " | ".join(columns) + " |")
    print("|" + "---|" * len(columns))
    for row in rows:
        print("| " + " | ".join(row[c] for c in columns) + " |")
    if path := os.environ.get("CSV"):
        with open(path, "w", newline="") as handle:
            writer = csv.DictWriter(handle, columns)
            writer.writeheader()
            writer.writerows(rows)
    failed = any(row["MiB/s"] == "-" for row in rows)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
