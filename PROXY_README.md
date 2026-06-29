# XMLA Readiness Proxy — `fix/xmla-refresh-readiness`

> **Branch**: `fix/xmla-refresh-readiness` in [deldos/powerbi-modeling-mcp](https://github.com/deldos/powerbi-modeling-mcp)  
> **Upstream**: [microsoft/powerbi-modeling-mcp](https://github.com/microsoft/powerbi-modeling-mcp)  
> **Tracking**: AW `action-1782715264209`

## Operator note

This repository wraps the Microsoft closed-source MCP binary. It does **not** patch the binary. The proxy intercepts only `table_operations.RefreshWithXMLA` and passes all other MCP calls through unchanged. `deldos/powerbi-modeling-mcp` is not a full source fork of `microsoft/powerbi-modeling-mcp` — Microsoft does not publish source for their runtime.

## Problem

When Power BI Desktop opens a `.pbip` file its embedded Analysis Services engine
takes **60–120 seconds** to initialise.  During this window:

- `connection_operations.List` **already reports** the Desktop connection — the
  entry appears before the engine is ready.
- Any XMLA write (`table_operations.RefreshWithXMLA`) sent during warm-up
  receives a raw `connection refused` / timeout error with **no structured signal**
  that the caller should retry.
- When **multiple Desktop instances** are open, the server may silently pick
  the wrong connection if `database` is not specified.

## Solution

A thin Python stdio proxy that sits between your MCP client and the
`powerbi-modeling-mcp.exe` binary:

```
MCP Client  →  proxy/proxy_server.py  →  powerbi-modeling-mcp.exe
```

For every `table_operations.RefreshWithXMLA` call the proxy:

1. **Disambiguates connections** — if `database` is not supplied and multiple
   Desktop instances are connected, returns `MULTIPLE_DESKTOP_CONNECTIONS`
   immediately rather than silently choosing one.
2. **Probes the AS engine** — sends `EVALUATE ROW("__xmla_ping__", 1)` via
   `dax_query_operations.EXECUTE` (lightweight; no model objects needed).
3. **Retries** every 5 s for up to 120 s (both configurable via env vars).
4. **Returns `XMLA_ENGINE_NOT_READY`** with elapsed time, database name, and
   actionable hint if the engine does not respond in time.
5. **Forwards** the original `RefreshWithXMLA` call once the probe succeeds.

All other tool calls pass through the proxy transparently.

## Files added in this branch

```
proxy/
├── __init__.py
├── mcp_protocol.py     # JSON-RPC 2.0 helpers
├── xmla_readiness.py   # probe logic, error builders, connection parser
└── proxy_server.py     # async stdio proxy entry point

tests/
├── conftest.py
├── test_xmla_readiness.py   # 38 unit tests — no Desktop required
└── test_proxy_server.py     # 14 integration-level tests — no Desktop required

validate_xmla.py    # standalone manual validation script
pyproject.toml
PROXY_README.md     # this file
```

## Usage

### Option A — pip-install and register

```bash
pip install -e .   # from repo root

# mcp.json:
{
  "powerbi-modeling-mcp": {
    "type": "stdio",
    "command": "pbi-mcp-proxy",
    "args": ["--start"],
    "env": {
      "POWERBI_MCP_EXE": "C:\\path\\to\\powerbi-modeling-mcp.exe",
      "XMLA_READINESS_TIMEOUT_S": "120",
      "XMLA_READINESS_POLL_S": "5"
    }
  }
}
```

### Option B — run from source

```bash
# mcp.json:
{
  "powerbi-modeling-mcp": {
    "type": "stdio",
    "command": "python",
    "args": ["-m", "proxy.proxy_server", "--start"],
    "env": {
      "POWERBI_MCP_EXE": "C:\\...\\powerbi-modeling-mcp.exe"
    }
  }
}
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `POWERBI_MCP_EXE` | auto-detected from NPX cache | Path to the server binary |
| `XMLA_READINESS_TIMEOUT_S` | `120` | Max seconds to wait for AS engine |
| `XMLA_READINESS_POLL_S` | `5` | Seconds between readiness probes |
| `XMLA_PROXY_LOG` | `` | Set to `1` to write debug log to stderr |

### Finding the binary path

```powershell
# After running `npx @microsoft/powerbi-modeling-mcp@latest` at least once:
Get-ChildItem -Recurse $env:LOCALAPPDATA\npm-cache -Filter "powerbi-modeling-mcp.exe" |
    Select-Object -First 1 FullName
```

## Manual validation

Use `validate_xmla.py` to test readiness outside the proxy loop:

```bash
# Check immediately:
python validate_xmla.py

# Wait up to 120 s for the engine to become ready:
python validate_xmla.py --wait --timeout 120 --verbose

# Target a specific model when multiple Desktop instances are open:
python validate_xmla.py --database "My Sales Model" --wait
```

Exit codes: `0` = ready, `1` = not ready / timed out, `2` = config error.

## Tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
# Expected: 52 passed
```

Tests run entirely without a live Desktop or `powerbi-modeling-mcp.exe`.
Subprocess and asyncio interactions are simulated via pre-seeded futures.

## Error response format

### `XMLA_ENGINE_NOT_READY`

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "content": [{"type": "text", "text": "XMLA_ENGINE_NOT_READY: Power BI Desktop AS engine for database 'MyModel' did not become ready within 120s. ..."}],
    "isError": true,
    "_meta": {
      "code": "XMLA_ENGINE_NOT_READY",
      "elapsed_s": 120.0,
      "database": "MyModel"
    }
  }
}
```

### `MULTIPLE_DESKTOP_CONNECTIONS`

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "content": [{"type": "text", "text": "MULTIPLE_DESKTOP_CONNECTIONS: 2 Power BI Desktop instances are connected. Specify 'database' in your RefreshWithXMLA call. Available: 'ModelA', 'ModelB'"}],
    "isError": true,
    "_meta": {
      "code": "MULTIPLE_DESKTOP_CONNECTIONS",
      "available": ["ModelA", "ModelB"]
    }
  }
}
```

## Design notes

- **Fail-closed** on ambiguity: multiple connections without `database` → error,
  not a silent guess.
- **Probe is idempotent**: `EVALUATE ROW("__xmla_ping__", 1)` has no side effects
  and works even with no model objects present.
- **Unexpected errors pass through**: if the probe fails for a reason that isn't
  an engine-not-ready pattern (e.g. bad DAX syntax), the proxy treats the engine
  as ready and forwards the original request so the real server surfaces the
  actual error.
- **DAX flexibility preserved**: the proxy does not add any restrictions on raw
  DAX queries through `dax_query_operations`.
- **Pure stdlib**: the proxy package has zero additional dependencies beyond
  Python 3.10+ standard library.

## Contributing upstream

Once validated in production (AW `action-1782715264209`), the intention is to
propose this logic for inclusion in the upstream
[microsoft/powerbi-modeling-mcp](https://github.com/microsoft/powerbi-modeling-mcp)
via a PR against `main`.  SpendHQ-specific logic (registry queries, PBIR audit)
stays in the private fork — only the generic readiness probe and error structures
are proposed upstream.
