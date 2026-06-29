#!/usr/bin/env python3
"""
validate_xmla.py — Manual XMLA readiness validation script.

Runs a lightweight DAX probe against a running Power BI Desktop instance via
the powerbi-modeling-mcp server and reports whether the AS engine is ready.

Usage
-----
    # Check if engine is ready (auto-detect connection):
    python validate_xmla.py

    # Target a specific model when multiple Desktop instances are open:
    python validate_xmla.py --database "My Sales Model"

    # Retry for up to 120 s (useful right after opening Desktop):
    python validate_xmla.py --wait --timeout 120

    # Verbose output:
    python validate_xmla.py --verbose

Prerequisites
-------------
1. Power BI Desktop is open with a model loaded.
2. powerbi-modeling-mcp is installed and accessible via npx.
3. Python 3.10+ (Windows).

Exit codes
----------
0 — XMLA engine is ready.
1 — XMLA engine is not ready (or timed out).
2 — Usage / configuration error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Make proxy importable whether run as script or from package root
sys.path.insert(0, str(Path(__file__).parent))

from proxy.proxy_server import find_server_exe
from proxy.xmla_readiness import (
    DEFAULT_POLL_S,
    DEFAULT_TIMEOUT_S,
    PROBE_DAX,
    is_not_ready_error,
)


# ---------------------------------------------------------------------------
# MCP call helper
# ---------------------------------------------------------------------------

def _mcp_call(exe: str, request: dict, timeout_s: float = 15.0) -> dict:
    """
    Send a single JSON-RPC request to the powerbi-modeling-mcp executable and
    return the first non-banner JSON response.

    The server prints a startup banner to stdout before the MCP session
    begins; we skip lines that aren't valid JSON.
    """
    proc = subprocess.Popen(
        [exe, "--start"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=False,
    )
    try:
        payload = (json.dumps(request) + "\n").encode()
        proc.stdin.write(payload)
        proc.stdin.flush()

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
                if "id" in msg and str(msg["id"]) == str(request["id"]):
                    return msg
            except json.JSONDecodeError:
                continue  # skip banner / non-JSON lines
        return {"error": {"message": "response timed out"}}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def _list_connections(exe: str) -> list[str]:
    """Return database names from connection_operations.List."""
    req = {
        "jsonrpc": "2.0",
        "id": "validate-list-1",
        "method": "tools/call",
        "params": {"name": "connection_operations", "arguments": {"action": "List"}},
    }
    resp = _mcp_call(exe, req, timeout_s=10.0)
    if "error" in resp:
        return []
    content = (resp.get("result") or {}).get("content", [])
    for block in content:
        if block.get("type") == "text":
            try:
                data = json.loads(block["text"])
                if isinstance(data, list):
                    return [
                        str(item.get("database") or item.get("name") or "?")
                        for item in data
                        if isinstance(item, dict)
                    ]
            except json.JSONDecodeError:
                pass
    return []


def _run_probe(exe: str, database: str | None) -> tuple[bool, str]:
    """
    Run the DAX readiness probe.

    Returns (is_ready, message).
    """
    args: dict = {"action": "EXECUTE", "query": PROBE_DAX}
    if database:
        args["database"] = database

    req = {
        "jsonrpc": "2.0",
        "id": "validate-probe-1",
        "method": "tools/call",
        "params": {"name": "dax_query_operations", "arguments": args},
    }
    resp = _mcp_call(exe, req, timeout_s=15.0)

    if "error" in resp and resp["error"]:
        err_text = str(resp["error"].get("message", ""))
        return False, f"JSON-RPC error: {err_text}"

    result = resp.get("result") or {}
    if result.get("isError"):
        content = result.get("content", [])
        text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        return False, f"Tool error: {text}"

    return True, "DAX probe succeeded — XMLA engine is ready"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate XMLA engine readiness for Power BI Desktop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--database",
        metavar="NAME",
        help="Target model name (required when multiple Desktop instances are open)",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Retry until the engine is ready or --timeout elapses",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        metavar="SECONDS",
        help=f"Max seconds to wait (default {DEFAULT_TIMEOUT_S}, with --wait)",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=DEFAULT_POLL_S,
        metavar="SECONDS",
        help=f"Seconds between retries (default {DEFAULT_POLL_S}, with --wait)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print extra diagnostic information",
    )
    args = parser.parse_args()

    # ── Locate server binary ──────────────────────────────────────────────
    try:
        exe = find_server_exe()
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Using server: {exe}")

    # ── List connections ──────────────────────────────────────────────────
    print("Listing active Desktop connections...", end=" ", flush=True)
    connections = _list_connections(exe)
    if connections:
        print(f"{len(connections)} found: {', '.join(connections)}")
    else:
        print("none found (is Desktop open with a model?)")

    if len(connections) > 1 and not args.database:
        print(
            f"\nWARNING: Multiple connections found. Specify --database to target one.\n"
            f"Available: {', '.join(connections)}"
        )
        return 2

    database = args.database or (connections[0] if connections else None)
    if database:
        print(f"Target database: {database}")

    # ── Probe loop ────────────────────────────────────────────────────────
    start = time.monotonic()
    attempt = 0

    while True:
        elapsed = time.monotonic() - start
        attempt += 1
        print(f"\n[Attempt {attempt}] Elapsed: {elapsed:.0f}s — probing XMLA engine...", end=" ", flush=True)

        ready, message = _run_probe(exe, database)

        if ready:
            print(f"READY ✓")
            print(f"\n{message}")
            print(f"Total elapsed: {time.monotonic() - start:.1f}s")
            return 0

        not_ready_flag = is_not_ready_error(message)
        status = "NOT READY" if not_ready_flag else "ERROR"
        print(f"{status}")

        if args.verbose:
            print(f"  Detail: {message}")

        if not args.wait or not not_ready_flag:
            print(f"\nFailed: {message}")
            if not args.wait and not_ready_flag:
                print("Hint: Use --wait to retry automatically.")
            return 1

        if elapsed >= args.timeout:
            print(f"\nTimed out after {elapsed:.0f}s — XMLA engine did not become ready.")
            print(f"  Last error: {message}")
            return 1

        remaining = args.timeout - elapsed
        wait_for = min(args.poll, remaining)
        print(f"  Retry in {wait_for:.0f}s ({remaining:.0f}s remaining)...")
        time.sleep(wait_for)


if __name__ == "__main__":
    sys.exit(main())
