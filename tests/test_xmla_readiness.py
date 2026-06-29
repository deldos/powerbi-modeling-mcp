"""
Unit tests for proxy.xmla_readiness — no subprocess required.

These tests verify:
- Error classification (is_not_ready_error)
- Probe request construction
- Structured error response construction
- Multi-connection error construction
- Connection name extraction from List responses
"""
from __future__ import annotations

import json
import pytest

from proxy.xmla_readiness import (
    PROBE_DAX,
    build_multi_connection_error,
    build_not_ready_response,
    build_probe_request,
    extract_connection_names,
    is_not_ready_error,
)
from tests.conftest import make_connection_list


# ---------------------------------------------------------------------------
# is_not_ready_error
# ---------------------------------------------------------------------------

class TestIsNotReadyError:
    """Classify error strings as engine-not-ready vs real errors."""

    @pytest.mark.parametrize("text", [
        "Error: connection refused to localhost:50001",
        "Unable to connect to Analysis Services engine",
        "Server not ready",
        "Request timed out",
        "Timed out waiting for connection",
        "ADOMD.NET connection failure",
        "RPC server unavailable",
        "Engine not started",
        "localhost:50001 refused",
        "No connection established",
        "OLAP connection failed",
        "Could not connect to analysis services",
        "Failed to connect",
    ])
    def test_positive_cases(self, text):
        assert is_not_ready_error(text), f"Expected True for: {text!r}"

    @pytest.mark.parametrize("text", [
        "The expression refers to multiple columns",
        "DAX syntax error near ','",
        "Table 'Sales' does not exist",
        "Refresh failed: partition expression error",
        "Authentication failed",
        "Permission denied",
        "",
    ])
    def test_negative_cases(self, text):
        assert not is_not_ready_error(text), f"Expected False for: {text!r}"

    def test_case_insensitive(self):
        assert is_not_ready_error("CONNECTION REFUSED")
        assert is_not_ready_error("Unable To Connect")

    def test_mixed_message(self):
        # Engine not started appears inside a longer message
        assert is_not_ready_error(
            "[Error 500] localhost:50001 refused: Analysis Services engine not started yet"
        )


# ---------------------------------------------------------------------------
# build_probe_request
# ---------------------------------------------------------------------------

class TestBuildProbeRequest:
    def test_structure(self):
        req = build_probe_request("__probe_abc__", "MyModel")
        assert req["jsonrpc"] == "2.0"
        assert req["id"] == "__probe_abc__"
        assert req["method"] == "tools/call"
        assert req["params"]["name"] == "dax_query_operations"
        assert req["params"]["arguments"]["action"] == "EXECUTE"
        assert req["params"]["arguments"]["query"] == PROBE_DAX
        assert req["params"]["arguments"]["database"] == "MyModel"

    def test_no_database(self):
        req = build_probe_request("__probe_xyz__", None)
        assert "database" not in req["params"]["arguments"]

    def test_probe_id_preserved(self):
        probe_id = "__probe_test_0__"
        req = build_probe_request(probe_id, None)
        assert req["id"] == probe_id


# ---------------------------------------------------------------------------
# build_not_ready_response
# ---------------------------------------------------------------------------

class TestBuildNotReadyResponse:
    def test_structure(self):
        resp = build_not_ready_response(42, 120.0, "MyModel")
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 42
        result = resp["result"]
        assert result["isError"] is True
        assert result["_meta"]["code"] == "XMLA_ENGINE_NOT_READY"
        assert result["_meta"]["elapsed_s"] == 120.0
        assert result["_meta"]["database"] == "MyModel"

    def test_message_content(self):
        resp = build_not_ready_response(1, 95.3, "Sales")
        text = resp["result"]["content"][0]["text"]
        assert "XMLA_ENGINE_NOT_READY" in text
        assert "Sales" in text
        assert "95" in text

    def test_no_database(self):
        resp = build_not_ready_response(1, 30.0, None)
        text = resp["result"]["content"][0]["text"]
        assert "XMLA_ENGINE_NOT_READY" in text
        assert resp["result"]["_meta"]["database"] is None

    def test_request_id_types(self):
        # id can be int, str, or None
        for rid in (0, "abc", None):
            resp = build_not_ready_response(rid, 10.0, None)
            assert resp["id"] == rid


# ---------------------------------------------------------------------------
# build_multi_connection_error
# ---------------------------------------------------------------------------

class TestBuildMultiConnectionError:
    def test_structure(self):
        resp = build_multi_connection_error(7, ["ModelA", "ModelB"])
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 7
        result = resp["result"]
        assert result["isError"] is True
        assert result["_meta"]["code"] == "MULTIPLE_DESKTOP_CONNECTIONS"
        assert result["_meta"]["available"] == ["ModelA", "ModelB"]

    def test_message_lists_databases(self):
        resp = build_multi_connection_error(1, ["Foo", "Bar", "Baz"])
        text = resp["result"]["content"][0]["text"]
        assert "MULTIPLE_DESKTOP_CONNECTIONS" in text
        assert "Foo" in text
        assert "Bar" in text
        assert "Baz" in text
        assert "3" in text

    def test_single_db_allowed(self):
        # build_multi_connection_error only called when > 1 — but must not crash for 1
        resp = build_multi_connection_error(1, ["OnlyOne"])
        assert resp["result"]["_meta"]["available"] == ["OnlyOne"]


# ---------------------------------------------------------------------------
# extract_connection_names
# ---------------------------------------------------------------------------

class TestExtractConnectionNames:
    def _make_list_response(self, databases):
        """Use the conftest helper to build a realistic List response."""
        return make_connection_list("__list_test__", databases)

    def test_single_connection(self):
        resp = self._make_list_response(["SalesModel"])
        names = extract_connection_names(resp)
        assert names == ["SalesModel"]

    def test_multiple_connections(self):
        resp = self._make_list_response(["ModelA", "ModelB", "ModelC"])
        names = extract_connection_names(resp)
        assert set(names) == {"ModelA", "ModelB", "ModelC"}

    def test_empty_list(self):
        resp = self._make_list_response([])
        assert extract_connection_names(resp) == []

    def test_malformed_content(self):
        resp = {
            "jsonrpc": "2.0",
            "id": "x",
            "result": {"content": [{"type": "text", "text": "not valid json"}]},
        }
        assert extract_connection_names(resp) == []

    def test_error_response(self):
        resp = {
            "jsonrpc": "2.0",
            "id": "x",
            "error": {"code": -1, "message": "server error"},
        }
        assert extract_connection_names(resp) == []

    def test_missing_result(self):
        resp = {"jsonrpc": "2.0", "id": "x"}
        assert extract_connection_names(resp) == []
