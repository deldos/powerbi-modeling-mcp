"""
MCP JSON-RPC 2.0 message types and helpers.

The MCP protocol uses newline-delimited JSON over stdio.  Each line is either
a request (has 'id' and 'method'), a response (has 'id' and 'result'/'error'),
or a notification (has 'method' but no 'id').
"""
from __future__ import annotations

import json
from typing import Any


def parse_line(raw: bytes) -> dict | None:
    """Decode and JSON-parse one protocol line.  Returns None on malformed input."""
    try:
        return json.loads(raw.decode("utf-8", errors="replace").strip())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def dump_line(msg: dict) -> bytes:
    """Serialise a message to a protocol line (JSON + newline)."""
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def is_tools_call(msg: dict, tool_name: str, action: str | None = None) -> bool:
    """Return True if *msg* is a tools/call request for the given tool and action."""
    if msg.get("method") != "tools/call":
        return False
    params = msg.get("params", {})
    if params.get("name") != tool_name:
        return False
    if action is not None:
        return params.get("arguments", {}).get("action") == action
    return True


def make_error_result(request_id: Any, text: str, meta: dict | None = None) -> dict:
    """
    Build an MCP tool-error response.

    MCP errors are returned as a successful JSON-RPC response whose 'result'
    has ``isError: true`` and a text content block.  This keeps the client
    in the normal tool-call flow rather than triggering a JSON-RPC protocol
    error branch.
    """
    content = [{"type": "text", "text": text}]
    result: dict = {"content": content, "isError": True}
    if meta:
        result["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_jsonrpc_error(request_id: Any, code: int, message: str, data: dict | None = None) -> dict:
    """Build a JSON-RPC protocol-level error (used for infrastructure failures)."""
    err: dict = {"code": code, "message": message}
    if data:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


def response_is_error(msg: dict) -> bool:
    """Return True if the server response indicates a failure (either kind)."""
    if "error" in msg and msg["error"]:
        return True
    result = msg.get("result") or {}
    if isinstance(result, dict) and result.get("isError"):
        return True
    return False


def response_error_text(msg: dict) -> str:
    """Extract the human-readable error string from any failure response."""
    if "error" in msg and msg["error"]:
        err = msg["error"]
        parts = [str(err.get("message", ""))]
        if err.get("data"):
            parts.append(str(err["data"]))
        return " ".join(parts)
    content = (msg.get("result") or {}).get("content", [])
    return " ".join(c.get("text", "") for c in content if c.get("type") == "text")
