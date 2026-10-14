"""Fault proxy: sits between the Integration Service and Fineract and breaks the link on
demand, for the Part J failure simulations. Python standard library only.

Two listeners:
- the data port (FAULT_PROXY_PORT, 8081) passes HTTP requests on to FAULT_PROXY_UPSTREAM;
- the admin port (FAULT_PROXY_ADMIN_PORT, 8092) has GET /health and GET/PUT/DELETE
  /admin/faults. The fault calls need the X-API-Key header (FAULT_PROXY_API_KEY).

Modes (PUT /admin/faults {"mode": ..., "delay_seconds": ..., "path_contains": ...}):
- off                 everything passes.
- down                the data port stops listening: a new connection is refused, so the
                      caller knows nothing was sent.
- timeout_after_send  reads pass. The next write (not GET/HEAD/OPTIONS) whose path holds
                      path_contains (any write when empty) is sent to Fineract and Fineract's
                      answer is read, then thrown away: the caller's connection is held for
                      delay_seconds and closed with no answer. Right after Fineract answered,
                      the proxy goes down, so the caller's follow-up lookup fails too, as in
                      a network cut. It stays down until DELETE.
- delay               every request waits delay_seconds before it is passed on.

One request per connection: the proxy asks Fineract for "Connection: close" and closes the
caller's connection after each answer, so it never has to parse the answer's framing. State
lives in this one process (no workers), so every request sees the same switch."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

READ_ONLY = frozenset({"GET", "HEAD", "OPTIONS"})
MODES = frozenset({"off", "down", "timeout_after_send", "delay"})
MAX_DELAY = 120.0
MAX_HEAD = 65536
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-connection"})


@dataclass
class Fault:
    mode: str = "off"
    delay_seconds: float = 0.0
    path_contains: str = ""
    dropped_answers: int = 0
    last_dropped: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "active": self.mode != "off",
            "delay_seconds": self.delay_seconds,
            "path_contains": self.path_contains,
            "dropped_answers": self.dropped_answers,
            "last_dropped": self.last_dropped,
        }


def log(message: str, **fields: Any) -> None:
    record = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "message": message}
    print(json.dumps(record | fields), flush=True)


class Request:
    def __init__(self, head: bytes, body: bytes) -> None:
        lines = head.decode("latin-1").split("\r\n")
        self.method, self.target, self.version = [*lines[0].split(" ", 2), "", ""][:3]
        self.headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if line:
                name, _, value = line.partition(":")
                self.headers.append((name.strip(), value.strip()))
        self.body = body

    def header(self, name: str) -> str | None:
        return next((v for k, v in self.headers if k.lower() == name.lower()), None)


async def read_request(reader: asyncio.StreamReader) -> Request | None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        return None
    request = Request(head[:-4], b"")
    length = int(request.header("Content-Length") or 0)
    if length:
        request.body = await reader.readexactly(length)
    return request


def close_after(answer: bytes) -> bytes:
    """The answer with "Connection: close", so the caller never reuses a connection the
    proxy is about to close (a reused one would fail as a reset, not as a refusal)."""
    head, sep, body = answer.partition(b"\r\n\r\n")
    if not sep:
        return answer
    lines = [
        line
        for line in head.split(b"\r\n")
        if line.split(b":", 1)[0].strip().lower().decode("latin-1") not in HOP_HEADERS
    ]
    return b"\r\n".join([*lines, b"Connection: close"]) + sep + body


class Proxy:
    def __init__(self, upstream: str, host: str, port: int, api_key: str) -> None:
        up_host, _, up_port = upstream.partition(":")
        self.upstream = (up_host, int(up_port or 80))
        self.host, self.port, self.api_key = host, port, api_key
        self.fault = Fault()
        self.server: asyncio.Server | None = None

    # --- the data port --------------------------------------------------------------------

    async def listen(self) -> None:
        if self.server is None:
            self.server = await asyncio.start_server(
                self.handle, self.host, self.port, reuse_address=True
            )

    def stop_listening(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await read_request(reader)
            if request is None:
                return
            fault = self.fault
            if fault.mode == "delay":
                await asyncio.sleep(fault.delay_seconds)
            if (
                fault.mode == "timeout_after_send"
                and request.method not in READ_ONLY
                and fault.path_contains in request.target
            ):
                await self.drop_answer(request, writer)
                return
            writer.write(await self.forward(request))
            await writer.drain()
        except (ConnectionError, OSError) as exc:
            log("proxy request failed", problem=type(exc).__name__)
        finally:
            writer.close()

    async def forward(self, request: Request) -> bytes:
        reader, writer = await asyncio.open_connection(*self.upstream)
        try:
            head = [f"{request.method} {request.target} {request.version}"]
            head += [f"{k}: {v}" for k, v in request.headers if k.lower() not in HOP_HEADERS]
            head.append("Connection: close")
            writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + request.body)
            await writer.drain()
            return close_after(await reader.read())
        finally:
            writer.close()

    async def drop_answer(self, request: Request, writer: asyncio.StreamWriter) -> None:
        answer = await self.forward(request)
        status = answer.split(b" ", 2)[1].decode("latin-1") if answer.count(b" ") >= 2 else "?"
        fault = self.fault
        fault.mode = "down"
        fault.dropped_answers += 1
        fault.last_dropped = f"{request.method} {request.target} -> Fineract answered {status}"
        self.stop_listening()
        log("fault: answer dropped, proxy now down", dropped=fault.last_dropped)
        await asyncio.sleep(fault.delay_seconds)
        writer.transport.abort()

    # --- the admin port -------------------------------------------------------------------

    async def admin(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await read_request(reader)
            if request is None:
                return
            status, body = await self.route(request)
            data = json.dumps(body).encode()
            writer.write(
                f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def route(self, request: Request) -> tuple[int, dict[str, Any]]:
        path = request.target.split("?", 1)[0]
        if path == "/health" and request.method == "GET":
            return 200, {"status": "UP", "mode": self.fault.mode}
        if path != "/admin/faults":
            return 404, {"error": {"code": "not_found", "message": "No such path."}}
        key = request.header("X-API-Key") or ""
        if not hmac.compare_digest(key.encode(), self.api_key.encode()):
            return 401, {"error": {"code": "unauthorized", "message": "Wrong or missing key."}}
        if request.method == "GET":
            return 200, self.fault.view()
        if request.method == "DELETE":
            return 200, await self.set_fault("off", 0.0, "")
        if request.method == "PUT":
            try:
                wanted = json.loads(request.body or b"{}")
                mode = str(wanted["mode"])
                delay = float(wanted.get("delay_seconds", 0))
                path_contains = str(wanted.get("path_contains") or "")
            except (ValueError, KeyError, TypeError):
                return 422, {"error": {"code": "validation_error", "message": "Bad body."}}
            if mode not in MODES or not 0 <= delay <= MAX_DELAY:
                message = f"mode is one of {sorted(MODES)}; delay_seconds 0 to {MAX_DELAY}."
                return 422, {"error": {"code": "validation_error", "message": message}}
            return 200, await self.set_fault(mode, delay, path_contains)
        return 405, {"error": {"code": "method_not_allowed", "message": "GET, PUT or DELETE."}}

    async def set_fault(self, mode: str, delay: float, path_contains: str) -> dict[str, Any]:
        counts = (self.fault.dropped_answers, self.fault.last_dropped)
        self.fault = Fault(mode, delay, path_contains, *counts)
        if mode == "down":
            self.stop_listening()
        else:
            await self.listen()
        log("fault set", **self.fault.view())
        return self.fault.view()


async def main() -> None:
    api_key = os.environ.get("FAULT_PROXY_API_KEY", "").strip()
    if not api_key:
        sys.exit("FAULT_PROXY_API_KEY is not set")
    host = os.environ.get("FAULT_PROXY_HOST", "0.0.0.0")  # noqa: S104 (inside the container)
    proxy = Proxy(
        os.environ.get("FAULT_PROXY_UPSTREAM", "fineract:8080"),
        host,
        int(os.environ.get("FAULT_PROXY_PORT", "8081")),
        api_key,
    )
    await proxy.listen()
    admin_port = int(os.environ.get("FAULT_PROXY_ADMIN_PORT", "8092"))
    admin = await asyncio.start_server(proxy.admin, host, admin_port, reuse_address=True)
    log("fault proxy started", upstream=os.environ.get("FAULT_PROXY_UPSTREAM", "fineract:8080"))
    async with admin:
        await admin.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
