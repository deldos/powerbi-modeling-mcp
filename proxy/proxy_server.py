"""
proxy_server.py — XMLA-readiness aware MCP stdio proxy.

Architecture
------------
MCP Client (Claude Code / Copilot)
    │  JSON-RPC / stdio
    ▼
proxy_server.py  ← THIS FILE
    │  intercepts table_operations.RefreshWithXMLA
    │  probes readiness via dax_query_operations.EXECUTE
    │  retries for up to XMLA_READINESS_TIMEOUT_S seconds
    │  disambiguates multiple Desktop connections
    │  JSON-RPC / stdio (subprocess pipe)
    ▼
powerbi-modeling-mcp.exe  (closed-source Microsoft binary)

Usage (mcp.json)
----------------
Replace the direct exe call with the proxy so every client gets readiness
handling automatically:

    "command": "python",
    "args": ["-m", "proxy.proxy_server", "--start"],
    "env": {
        "POWERBI_MCP_EXE": "C:\\\\path\\\\to\\\\powerbi-modeling-mcp.exe",
        "XMLA_READINESS_TIMEOUT_S": "120",
        "XMLA_READINESS_POLL_S": "5"
    }

Environment variables
---------------------
POWERBI_MCP_EXE             Path to the binary (auto-detected from NPX cache if absent).
XMLA_READINESS_TIMEOUT_S    Max seconds to wait for AS engine (default 120).
XMLA_READINESS_POLL_S       Seconds between readiness probes (default 5).
XMLA_PROXY_LOG              Set to "1" to write debug log to stderr.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .mcp_protocol import (
    dump_line,
    is_tools_call,
    make_jsonrpc_error,
    parse_line,
    response_error_text,
    response_is_error,
)
from .xmla_readiness import (
    DEFAULT_POLL_S,
    DEFAULT_TIMEOUT_S,
    build_multi_connection_error,
    build_not_ready_response,
    build_probe_request,
    extract_connection_names,
    is_not_ready_error,
)

# ---------------------------------------------------------------------------
# Logging (stderr only — stdout is the JSON-RPC channel)
# ---------------------------------------------------------------------------

_log = logging.getLogger("xmla-proxy")
if os.environ.get("XMLA_PROXY_LOG") == "1":
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ---------------------------------------------------------------------------
# Server binary discovery
# ---------------------------------------------------------------------------

def find_server_exe() -> str:
    """
    Locate ``powerbi-modeling-mcp.exe``.

    Priority:
    1. ``POWERBI_MCP_EXE`` environment variable.
    2. Common NPX cache directories (Windows).
    3. PATH.

    Raises ``FileNotFoundError`` with an actionable message if not found.
    """
    env_path = os.environ.get("POWERBI_MCP_EXE", "").strip()
    if env_path and Path(env_path).is_file():
        return env_path

    patterns = [
        str(Path.home() / "scoop/persist/nodejs-lts/cache/_npx/**/powerbi-modeling-mcp.exe"),
        str(Path.home() / "AppData/Roaming/npm-cache/_npx/**/powerbi-modeling-mcp.exe"),
        str(Path.home() / ".npm/_npx/**/powerbi-modeling-mcp.exe"),
        # Global installs
        str(Path.home() / "scoop/shims/powerbi-modeling-mcp.exe"),
        r"C:\Program Files\PowerBIModelingMCP\powerbi-modeling-mcp.exe",
    ]
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]

    raise FileNotFoundError(
        "powerbi-modeling-mcp.exe not found. "
        "Run 'npx @microsoft/powerbi-modeling-mcp@latest' once to download it, "
        "then set POWERBI_MCP_EXE to the full path."
    )


# ---------------------------------------------------------------------------
# Proxy core
# ---------------------------------------------------------------------------

_PROBE_PREFIX = "__probe_"
# Sentinel: short timeout for individual probe calls (the engine either replies
# quickly or is definitely not ready)
_PROBE_CALL_TIMEOUT_S = 12.0
# Timeout for the connection-list disambiguation call
_LIST_CALL_TIMEOUT_S = 10.0


class XmlaReadinessProxy:
    """
    Async MCP stdio proxy with XMLA engine readiness interception.

    All messages flow through the proxy transparently except for
    ``table_operations.RefreshWithXMLA``, which triggers:
    1. Optional: ``connection_operations.List`` to check for multiple Desktop
       instances when ``database`` is not specified.
    2. Readiness probe via ``dax_query_operations.EXECUTE`` with a trivial DAX.
    3. Retry loop until the probe succeeds or ``timeout_s`` elapses.
    4. Either forward the original request or return a structured error.
    """

    def __init__(
        self,
        server_exe: str,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        poll_s: float = DEFAULT_POLL_S,
        extra_args: list[str] | None = None,
    ) -> None:
        self._exe = server_exe
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        # Args forwarded to the real exe (e.g. ["--readwrite", "--skip-confirmation"])
        self._extra_args: list[str] = extra_args or ["--start"]

        self._proc: asyncio.subprocess.Process | None = None

        # Pending probe futures: probe_id → Future[dict]
        self._probe_futures: dict[str, asyncio.Future[dict]] = {}
        # Pending connection-list futures: list_id → Future[dict]
        self._list_futures: dict[str, asyncio.Future[dict]] = {}

        # Output lock so concurrent tasks don't interleave JSON lines to stdout
        self._out_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._exe,
            *self._extra_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # inherit — Desktop logs to our stderr
        )
        _log.debug("started %s (pid=%d)", self._exe, self._proc.pid)
        try:
            await asyncio.gather(
                self._read_client(),
                self._read_server(),
            )
        finally:
            if self._proc.returncode is None:
                self._proc.terminate()

    # ------------------------------------------------------------------
    # Client → Server (stdin)
    # ------------------------------------------------------------------

    async def _read_client(self) -> None:
        """Read JSON-RPC lines from stdin and dispatch."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _blocking_reader() -> None:
            try:
                for raw in sys.stdin.buffer:
                    loop.call_soon_threadsafe(queue.put_nowait, raw)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        asyncio.get_running_loop().run_in_executor(None, _blocking_reader)

        while True:
            raw = await queue.get()
            if raw is None:
                break
            msg = parse_line(raw)
            if msg is None:
                _log.debug("non-JSON line from client: %r", raw[:120])
                continue
            _log.debug("client → proxy: method=%s id=%s", msg.get("method"), msg.get("id"))

            if is_tools_call(msg, "table_operations", "RefreshWithXMLA"):
                asyncio.create_task(self._handle_refresh(msg))
            else:
                await self._send_server(msg)

    # ------------------------------------------------------------------
    # Server → Client (subprocess stdout)
    # ------------------------------------------------------------------

    async def _read_server(self) -> None:
        """Read JSON-RPC lines from the server and route."""
        assert self._proc and self._proc.stdout
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                break
            msg = parse_line(raw)
            if msg is None:
                # Pass through non-JSON output (e.g. startup banner)
                async with self._out_lock:
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
                continue

            msg_id = msg.get("id")
            _log.debug("server → proxy: id=%s is_error=%s", msg_id, response_is_error(msg))

            if isinstance(msg_id, str):
                if msg_id.startswith(_PROBE_PREFIX):
                    fut = self._probe_futures.get(msg_id)
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue  # do NOT forward probe responses to client
                if msg_id.startswith("__list_"):
                    fut = self._list_futures.get(msg_id)
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue  # do NOT forward list responses to client

            await self._send_client(msg)

    # ------------------------------------------------------------------
    # RefreshWithXMLA interception
    # ------------------------------------------------------------------

    async def _handle_refresh(self, req: dict) -> None:
        """
        Gate-check then forward (or reject) a RefreshWithXMLA request.

        Steps:
        1. If ``database`` not specified: check for multiple Desktop connections.
           Return ``MULTIPLE_DESKTOP_CONNECTIONS`` error if > 1 found.
        2. Probe the XMLA engine with a lightweight DAX query.
        3. Retry the probe every ``poll_s`` for up to ``timeout_s`` seconds.
        4. Forward the request when the probe succeeds.
        5. Return ``XMLA_ENGINE_NOT_READY`` if timeout is reached.
        """
        request_id = req.get("id")
        args = req.get("params", {}).get("arguments", {})
        database: str | None = args.get("database") or None

        _log.debug(
            "intercepted RefreshWithXMLA id=%s database=%s", request_id, database
        )

        # ── Step 1: multi-instance disambiguation ──────────────────────────
        if not database:
            multi_err = await self._check_connections(request_id)
            if multi_err is not None:
                await self._send_client(multi_err)
                return

        # ── Step 2-5: readiness probe + retry ─────────────────────────────
        loop = asyncio.get_running_loop()
        start = loop.time()
        attempt = 0

        while True:
            elapsed = loop.time() - start
            if elapsed >= self._timeout_s:
                _log.info(
                    "XMLA engine not ready after %.0fs for database=%s", elapsed, database
                )
                await self._send_client(
                    build_not_ready_response(request_id, elapsed, database)
                )
                return

            ready = await self._probe_once(attempt, database)
            if ready:
                _log.debug("XMLA engine ready after %.1fs", loop.time() - start)
                await self._send_server(req)
                return

            attempt += 1
            _log.debug(
                "probe %d failed (elapsed=%.0fs) — retry in %.0fs",
                attempt,
                elapsed,
                self._poll_s,
            )
            await asyncio.sleep(self._poll_s)

    async def _probe_once(self, attempt: int, database: str | None) -> bool:
        """
        Send one DAX probe and return True if the engine responded successfully.

        Returns False for engine-not-ready errors.
        Re-raises (i.e. forwards the original request) for unexpected errors
        so the real server can surface the actual problem.
        """
        probe_id = f"{_PROBE_PREFIX}{uuid.uuid4().hex}_{attempt}__"
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._probe_futures[probe_id] = fut

        try:
            await self._send_server(build_probe_request(probe_id, database))
            response = await asyncio.wait_for(fut, timeout=_PROBE_CALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            _log.debug("probe %s timed out after %.0fs", probe_id, _PROBE_CALL_TIMEOUT_S)
            return False
        finally:
            self._probe_futures.pop(probe_id, None)

        if not response_is_error(response):
            return True  # engine is ready

        error_text = response_error_text(response)
        _log.debug("probe error: %s", error_text[:200])
        if is_not_ready_error(error_text):
            return False  # engine warming up — keep retrying

        # Unexpected error (e.g. model not found, bad DAX) — engine may be
        # ready but the probe itself failed.  Treat as ready so the original
        # request surfaces the actual server error rather than a misleading
        # XMLA_ENGINE_NOT_READY message.
        _log.info(
            "probe returned unexpected error (not a not-ready condition): %s",
            error_text[:200],
        )
        return True

    async def _check_connections(self, request_id: Any) -> dict | None:
        """
        Call ``connection_operations.List`` and return an error dict if multiple
        Desktop instances are active, or None if it is safe to proceed.
        """
        list_id = f"__list_{uuid.uuid4().hex}__"
        fut: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._list_futures[list_id] = fut

        list_req = {
            "jsonrpc": "2.0",
            "id": list_id,
            "method": "tools/call",
            "params": {"name": "connection_operations", "arguments": {"action": "List"}},
        }
        try:
            await self._send_server(list_req)
            response = await asyncio.wait_for(fut, timeout=_LIST_CALL_TIMEOUT_S)
        except asyncio.TimeoutError:
            _log.debug("connection_operations.List timed out — proceeding without check")
            return None
        finally:
            self._list_futures.pop(list_id, None)

        if response_is_error(response):
            return None  # can't list → proceed; server will return its own error

        names = extract_connection_names(response)
        if len(names) > 1:
            _log.info("multiple Desktop connections: %s", names)
            return build_multi_connection_error(request_id, names)
        return None

    # ------------------------------------------------------------------
    # Low-level I/O helpers
    # ------------------------------------------------------------------

    async def _send_server(self, msg: dict) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(dump_line(msg))
        await self._proc.stdin.drain()

    async def _send_client(self, msg: dict) -> None:
        async with self._out_lock:
            sys.stdout.buffer.write(dump_line(msg))
            sys.stdout.buffer.flush()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _async_main() -> None:
    timeout_s = int(os.environ.get("XMLA_READINESS_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    poll_s = float(os.environ.get("XMLA_READINESS_POLL_S", DEFAULT_POLL_S))
    # Space-separated extra args forwarded to the real exe, e.g. "--readwrite --skip-confirmation"
    raw_extra = os.environ.get("POWERBI_MCP_ARGS", "--start").strip()
    extra_args = raw_extra.split() if raw_extra else ["--start"]

    try:
        exe = find_server_exe()
    except FileNotFoundError as exc:
        err = make_jsonrpc_error(None, -32000, str(exc))
        sys.stdout.buffer.write(dump_line(err))
        sys.exit(1)

    _log.info("proxy starting: exe=%s args=%s timeout=%ds poll=%.0fs", exe, extra_args, timeout_s, poll_s)
    proxy = XmlaReadinessProxy(exe, timeout_s=timeout_s, poll_s=poll_s, extra_args=extra_args)
    await proxy.run()


def main() -> None:
    """Entry point for ``python -m proxy.proxy_server``."""
    # --start is passed by MCP clients and accepted but ignored here
    # (the proxy passes it through to the real server)
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
