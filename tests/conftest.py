"""
Shared fixtures for proxy test suite.

Tests do NOT require a live Power BI Desktop instance.  All subprocess and
asyncio interactions are simulated via fake server fixtures.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_response(request_id, result=None, error=None):
    """Build a minimal JSON-RPC response dict."""
    msg = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result or {}
    return msg


def make_tool_result(request_id, text, is_error=False):
    """Build an MCP tool-call result response."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        },
    }


def make_not_ready_tool_result(request_id):
    """Simulate the kind of error powerbi-modeling-mcp returns when AS is not ready."""
    return make_tool_result(
        request_id,
        "Error: connection refused to localhost:50001 — Analysis Services engine not started",
        is_error=True,
    )


def make_ready_tool_result(request_id):
    """Simulate a successful DAX probe response."""
    return make_tool_result(
        request_id,
        json.dumps([{"__xmla_ping__": 1}]),
        is_error=False,
    )


def make_connection_list(request_id, databases):
    """Simulate a connection_operations.List response."""
    items = [{"database": db, "state": "connected"} for db in databases]
    return make_tool_result(request_id, json.dumps(items))
