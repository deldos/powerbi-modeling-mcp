"""
XMLA engine readiness probe for Power BI Desktop.

Background
----------
Power BI Desktop embeds an Analysis Services (XMLA) engine that starts in-process
when a .pbip or .pbix file is opened.  The ``connection_operations.List`` tool
returns the connection entry as soon as Desktop registers it — but the AS engine
itself needs another 60-120 seconds to finish initialising.  Any XMLA write
(e.g. ``table_operations.RefreshWithXMLA``) sent during this window receives a
raw connection-refused or timeout error with no structured signal.

This module supplies:
* ``is_not_ready_error`` — classify an error string as "engine warming up"
* ``build_probe_request``  — lightweight ``EVALUATE ROW(...)`` probe call
* ``build_not_ready_response``  — structured MCP error for caller
* ``build_multi_connection_error`` — error when multiple Desktop instances exist
  and no database was specified (fail-closed rather than pick the wrong one)

Error codes (returned in ``_meta.code``)
-----------------------------------------
* ``XMLA_ENGINE_NOT_READY``     — engine did not respond within ``timeout_s``
* ``MULTIPLE_DESKTOP_CONNECTIONS`` — caller must supply ``database`` to select one
"""
from __future__ import annotations

import re
from typing import Any

from .mcp_protocol import make_error_result

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Lightweight DAX that costs virtually nothing and requires no model objects.
PROBE_DAX = 'EVALUATE ROW("__xmla_ping__", 1)'

#: Default maximum wait for the AS engine to become ready (seconds).
DEFAULT_TIMEOUT_S: int = 120

#: Default poll interval between readiness retries (seconds).
DEFAULT_POLL_S: float = 5.0

# ---------------------------------------------------------------------------
# Not-ready detection
# ---------------------------------------------------------------------------

# Connection-refused / engine-not-started patterns seen in practice on Windows.
# Patterns are applied to the lower-cased error text.
_NOT_READY_PATTERNS: list[str] = [
    r"connection.*refused",
    r"unable to connect",
    r"server.*not.*ready",
    r"timed?\s*out",
    r"adomd.*connection",
    r"rpc.*server.*unavailable",
    r"engine.*not.*started",
    r"localhost.*\d{4,5}.*refused",
    r"no.*connection.*established",
    r"olap.*connection.*fail",
    r"could not connect to.*analysis",
    r"failed to connect",
]

_NOT_READY_RE = re.compile("|".join(_NOT_READY_PATTERNS), re.IGNORECASE)


def is_not_ready_error(error_text: str) -> bool:
    """
    Return True if *error_text* matches a known AS-engine-not-ready pattern.

    Call this on the text extracted from a failed probe response to decide
    whether to retry or to surface the error immediately.
    """
    return bool(_NOT_READY_RE.search(error_text))


# ---------------------------------------------------------------------------
# Request / response builders
# ---------------------------------------------------------------------------

def build_probe_request(probe_id: str, database: str | None) -> dict:
    """
    Build a ``dax_query_operations.EXECUTE`` request used as a readiness probe.

    The probe uses a synthetic *probe_id* that the proxy uses to route the
    response back to the probe handler rather than forwarding it to the client.

    Args:
        probe_id:  Unique ID string (must start with ``__probe_``).
        database:  The target database/model name, or None to use the server's
                   current connection context.
    """
    args: dict[str, Any] = {"action": "EXECUTE", "query": PROBE_DAX}
    if database:
        args["database"] = database
    return {
        "jsonrpc": "2.0",
        "id": probe_id,
        "method": "tools/call",
        "params": {"name": "dax_query_operations", "arguments": args},
    }


def build_not_ready_response(request_id: Any, elapsed_s: float, database: str | None) -> dict:
    """
    Build an ``XMLA_ENGINE_NOT_READY`` MCP error result.

    The message is human-readable and actionable.  The ``_meta`` dict carries
    machine-readable fields the caller can inspect programmatically.

    Args:
        request_id:  The JSON-RPC id of the original RefreshWithXMLA request.
        elapsed_s:   How long the proxy waited before giving up (seconds).
        database:    The target model name (or None if not specified).
    """
    db_clause = f" for database '{database}'" if database else ""
    text = (
        f"XMLA_ENGINE_NOT_READY: Power BI Desktop AS engine{db_clause} did not "
        f"become ready within {elapsed_s:.0f}s. "
        "The embedded Analysis Services engine typically needs 60-120 s after "
        "Desktop opens. Wait for the file to fully load, then retry."
    )
    return make_error_result(
        request_id,
        text,
        meta={
            "code": "XMLA_ENGINE_NOT_READY",
            "elapsed_s": round(elapsed_s, 1),
            "database": database,
        },
    )


def build_multi_connection_error(request_id: Any, connection_names: list[str]) -> dict:
    """
    Build a ``MULTIPLE_DESKTOP_CONNECTIONS`` error.

    Raised when ``connection_operations.List`` returns more than one active
    Desktop connection and the caller has not supplied a ``database`` argument.
    Failing closed here prevents silently refreshing the wrong model.

    Args:
        request_id:        The JSON-RPC id of the original RefreshWithXMLA request.
        connection_names:  List of available database/connection names.
    """
    names_str = ", ".join(f"'{n}'" for n in connection_names)
    text = (
        f"MULTIPLE_DESKTOP_CONNECTIONS: {len(connection_names)} Power BI Desktop "
        "instances are connected. Specify 'database' in your RefreshWithXMLA "
        f"call to select one. Available: {names_str}"
    )
    return make_error_result(
        request_id,
        text,
        meta={
            "code": "MULTIPLE_DESKTOP_CONNECTIONS",
            "available": connection_names,
        },
    )


def extract_connection_names(list_response: dict) -> list[str]:
    """
    Parse ``connection_operations.List`` result into a list of database names.

    The tool returns JSON in a text content block.  This function is tolerant
    of schema changes — it falls back to an empty list on any parse error.
    """
    import json as _json

    content = (list_response.get("result") or {}).get("content", [])
    for block in content:
        if block.get("type") != "text":
            continue
        raw = block.get("text", "")
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError:
            continue
        if isinstance(data, list):
            names = []
            for item in data:
                if isinstance(item, dict):
                    name = item.get("database") or item.get("name") or item.get("id")
                    if name:
                        names.append(str(name))
            return names
    return []
