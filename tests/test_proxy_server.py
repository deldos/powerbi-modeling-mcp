"""
Unit tests for XmlaReadinessProxy logic.

The proxy's async I/O is tested by driving _probe_once and _check_connections
directly, with the subprocess pipe simulated by pre-seeding future results
into the proxy's internal dictionaries.

No live Desktop or powerbi-modeling-mcp.exe is required.
"""
from __future__ import annotations

import asyncio
import json
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from proxy.proxy_server import XmlaReadinessProxy, _PROBE_PREFIX
from proxy.xmla_readiness import DEFAULT_POLL_S, DEFAULT_TIMEOUT_S
from tests.conftest import (
    make_connection_list,
    make_not_ready_tool_result,
    make_ready_tool_result,
    make_response,
    make_tool_result,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_proxy(timeout_s=10, poll_s=0.05) -> XmlaReadinessProxy:
    """Create a proxy with fast poll interval for tests."""
    proxy = XmlaReadinessProxy.__new__(XmlaReadinessProxy)
    proxy._exe = "fake.exe"
    proxy._timeout_s = timeout_s
    proxy._poll_s = poll_s
    proxy._proc = None
    proxy._probe_futures = {}
    proxy._list_futures = {}
    proxy._out_lock = asyncio.Lock()
    proxy._server_messages = []  # collect what would go to the real server
    return proxy


def _patch_send_server(proxy: XmlaReadinessProxy) -> list[dict]:
    """Monkey-patch _send_server to collect messages and auto-resolve probes."""
    captured = []

    async def fake_send(msg):
        captured.append(msg)
        msg_id = msg.get("id", "")
        if isinstance(msg_id, str) and msg_id.startswith(_PROBE_PREFIX):
            # Auto-simulate a not-ready response for first N probes
            pass  # caller controls futures directly

    proxy._send_server = fake_send  # type: ignore[method-assign]
    return captured


# ---------------------------------------------------------------------------
# _probe_once
# ---------------------------------------------------------------------------

class TestProbeOnce:
    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        proxy = make_proxy()
        captured = _patch_send_server(proxy)

        async def _run():
            # Pre-seed the probe future result AFTER send (simulates server response)
            task = asyncio.create_task(proxy._probe_once(0, "MyModel"))
            await asyncio.sleep(0)  # let task start and _send_server run
            # Find the probe_id that was sent
            assert len(captured) == 1
            probe_id = captured[0]["id"]
            assert probe_id.startswith(_PROBE_PREFIX)
            # Deliver a success response
            fut = proxy._probe_futures.get(probe_id)
            if fut:
                fut.set_result(make_ready_tool_result(probe_id))
            return await task

        result = await asyncio.wait_for(_run(), timeout=2.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_not_ready_error(self):
        proxy = make_proxy()
        captured = _patch_send_server(proxy)

        async def _run():
            task = asyncio.create_task(proxy._probe_once(0, "MyModel"))
            await asyncio.sleep(0)
            probe_id = captured[0]["id"]
            fut = proxy._probe_futures.get(probe_id)
            if fut:
                fut.set_result(make_not_ready_tool_result(probe_id))
            return await task

        result = await asyncio.wait_for(_run(), timeout=2.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        proxy = make_proxy(timeout_s=60)
        captured = _patch_send_server(proxy)

        # _PROBE_CALL_TIMEOUT_S is 12 — patch it to 0.05 for test speed
        with patch("proxy.proxy_server._PROBE_CALL_TIMEOUT_S", 0.05):
            result = await asyncio.wait_for(proxy._probe_once(0, None), timeout=2.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_unexpected_error(self):
        """Non-not-ready errors are treated as 'engine ready' — let server handle."""
        proxy = make_proxy()
        captured = _patch_send_server(proxy)

        async def _run():
            task = asyncio.create_task(proxy._probe_once(0, "M"))
            await asyncio.sleep(0)
            probe_id = captured[0]["id"]
            fut = proxy._probe_futures.get(probe_id)
            if fut:
                fut.set_result(make_tool_result(probe_id, "Table 'X' not found", is_error=True))
            return await task

        result = await asyncio.wait_for(_run(), timeout=2.0)
        assert result is True  # forward to real server rather than looping

    @pytest.mark.asyncio
    async def test_probe_uses_database(self):
        proxy = make_proxy()
        captured = _patch_send_server(proxy)
        with patch("proxy.proxy_server._PROBE_CALL_TIMEOUT_S", 0.05):
            await proxy._probe_once(0, "SpecificModel")
        assert captured[0]["params"]["arguments"].get("database") == "SpecificModel"

    @pytest.mark.asyncio
    async def test_probe_omits_database_when_none(self):
        proxy = make_proxy()
        captured = _patch_send_server(proxy)
        with patch("proxy.proxy_server._PROBE_CALL_TIMEOUT_S", 0.05):
            await proxy._probe_once(0, None)
        assert "database" not in captured[0]["params"]["arguments"]


# ---------------------------------------------------------------------------
# _check_connections
# ---------------------------------------------------------------------------

class TestCheckConnections:
    @pytest.mark.asyncio
    async def test_single_connection_returns_none(self):
        proxy = make_proxy()
        captured = _patch_send_server(proxy)

        async def _run():
            task = asyncio.create_task(proxy._check_connections(1))
            await asyncio.sleep(0)
            list_id = captured[0]["id"]
            fut = proxy._list_futures.get(list_id)
            if fut:
                fut.set_result(make_connection_list(list_id, ["OnlyModel"]))
            return await task

        result = await asyncio.wait_for(_run(), timeout=2.0)
        assert result is None  # no error — safe to proceed

    @pytest.mark.asyncio
    async def test_multiple_connections_returns_error(self):
        proxy = make_proxy()
        captured = _patch_send_server(proxy)

        async def _run():
            task = asyncio.create_task(proxy._check_connections(2))
            await asyncio.sleep(0)
            list_id = captured[0]["id"]
            fut = proxy._list_futures.get(list_id)
            if fut:
                fut.set_result(make_connection_list(list_id, ["ModelA", "ModelB"]))
            return await task

        result = await asyncio.wait_for(_run(), timeout=2.0)
        assert result is not None
        assert result["result"]["isError"] is True
        assert "MULTIPLE_DESKTOP_CONNECTIONS" in result["result"]["_meta"]["code"]
        assert result["id"] == 2

    @pytest.mark.asyncio
    async def test_list_timeout_returns_none(self):
        """If List call times out, proceed without check (fail open)."""
        proxy = make_proxy()
        captured = _patch_send_server(proxy)

        with patch("proxy.proxy_server._LIST_CALL_TIMEOUT_S", 0.05):
            result = await asyncio.wait_for(proxy._check_connections(3), timeout=2.0)
        assert result is None

    @pytest.mark.asyncio
    async def test_list_error_returns_none(self):
        """If List returns an error, proceed — server will surface the actual problem."""
        proxy = make_proxy()
        captured = _patch_send_server(proxy)

        async def _run():
            task = asyncio.create_task(proxy._check_connections(4))
            await asyncio.sleep(0)
            list_id = captured[0]["id"]
            fut = proxy._list_futures.get(list_id)
            if fut:
                fut.set_result({"jsonrpc": "2.0", "id": list_id,
                                "error": {"code": -1, "message": "not connected"}})
            return await task

        result = await asyncio.wait_for(_run(), timeout=2.0)
        assert result is None


# ---------------------------------------------------------------------------
# _handle_refresh — integration-level scenarios
# ---------------------------------------------------------------------------

class TestHandleRefresh:
    def _make_refresh_req(self, req_id=99, database=None):
        args = {"action": "RefreshWithXMLA", "references": [{"name": "Sales"}]}
        if database:
            args["database"] = database
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": "table_operations", "arguments": args},
        }

    @pytest.mark.asyncio
    async def test_forwards_after_ready_probe(self):
        """When engine is ready on first probe, original request is forwarded."""
        proxy = make_proxy(poll_s=0.01)
        forwarded = []

        async def fake_send(msg):
            msg_id = msg.get("id", "")
            if isinstance(msg_id, str) and msg_id.startswith(_PROBE_PREFIX):
                fut = proxy._probe_futures.get(msg_id)
                if fut:
                    fut.set_result(make_ready_tool_result(msg_id))
            else:
                forwarded.append(msg)

        client_received = []

        async def fake_client(msg):
            client_received.append(msg)

        proxy._send_server = fake_send  # type: ignore[method-assign]
        proxy._send_client = fake_client  # type: ignore[method-assign]

        req = self._make_refresh_req(req_id=10, database="MyModel")
        await asyncio.wait_for(proxy._handle_refresh(req), timeout=3.0)

        assert len(forwarded) == 1
        assert forwarded[0]["id"] == 10
        assert client_received == []  # no error returned to client

    @pytest.mark.asyncio
    async def test_returns_not_ready_after_timeout(self):
        """Engine never becomes ready → XMLA_ENGINE_NOT_READY returned after timeout."""
        proxy = make_proxy(timeout_s=0.1, poll_s=0.02)
        forwarded = []
        client_received = []

        async def fake_send(msg):
            msg_id = msg.get("id", "")
            if isinstance(msg_id, str) and msg_id.startswith(_PROBE_PREFIX):
                fut = proxy._probe_futures.get(msg_id)
                if fut:
                    fut.set_result(make_not_ready_tool_result(msg_id))
            else:
                forwarded.append(msg)

        proxy._send_server = fake_send  # type: ignore[method-assign]
        proxy._send_client = AsyncMock(side_effect=lambda m: client_received.append(m) or None)  # type: ignore[method-assign]

        req = self._make_refresh_req(req_id=20, database="MyModel")
        await asyncio.wait_for(proxy._handle_refresh(req), timeout=3.0)

        assert forwarded == []  # original request NOT forwarded
        assert len(client_received) == 1
        err = client_received[0]
        assert err["id"] == 20
        assert err["result"]["isError"] is True
        assert err["result"]["_meta"]["code"] == "XMLA_ENGINE_NOT_READY"

    @pytest.mark.asyncio
    async def test_multi_connection_blocks_no_database(self):
        """Multiple connections + no database → MULTIPLE_DESKTOP_CONNECTIONS error."""
        proxy = make_proxy()
        forwarded = []
        client_received = []

        async def fake_send(msg):
            msg_id = msg.get("id", "")
            if isinstance(msg_id, str) and msg_id.startswith("__list_"):
                fut = proxy._list_futures.get(msg_id)
                if fut:
                    fut.set_result(make_connection_list(msg_id, ["M1", "M2"]))
            else:
                forwarded.append(msg)

        proxy._send_server = fake_send  # type: ignore[method-assign]
        proxy._send_client = AsyncMock(side_effect=lambda m: client_received.append(m) or None)  # type: ignore[method-assign]

        req = self._make_refresh_req(req_id=30, database=None)
        await asyncio.wait_for(proxy._handle_refresh(req), timeout=3.0)

        assert forwarded == []
        assert len(client_received) == 1
        err = client_received[0]
        assert err["id"] == 30
        assert err["result"]["_meta"]["code"] == "MULTIPLE_DESKTOP_CONNECTIONS"

    @pytest.mark.asyncio
    async def test_database_skips_connection_check(self):
        """When database is specified, no connection_operations.List is sent."""
        proxy = make_proxy(poll_s=0.01)
        sent_ids = []

        async def fake_send(msg):
            msg_id = msg.get("id", "")
            sent_ids.append(msg_id)
            if isinstance(msg_id, str) and msg_id.startswith(_PROBE_PREFIX):
                fut = proxy._probe_futures.get(msg_id)
                if fut:
                    fut.set_result(make_ready_tool_result(msg_id))

        proxy._send_server = fake_send  # type: ignore[method-assign]
        proxy._send_client = AsyncMock()  # type: ignore[method-assign]

        req = self._make_refresh_req(req_id=40, database="ExplicitDB")
        await asyncio.wait_for(proxy._handle_refresh(req), timeout=3.0)

        # Should have sent probe then original request — no __list_ calls
        assert not any(str(i).startswith("__list_") for i in sent_ids)
        assert any(str(i).startswith(_PROBE_PREFIX) for i in sent_ids)
