"""Caramelo MCP server — generated from the FastAPI app.

Every REST endpoint becomes an MCP tool with the same name, params and
behavior, so agents and HTTP clients see one contract.

Run (stdio, for local agents):   python -m caramelo.mcp_server
Run (http):                      fastmcp run caramelo.mcp_server:mcp --transport http
"""

from __future__ import annotations

from fastmcp import FastMCP

from caramelo.api import app

mcp = FastMCP.from_fastapi(
    app,
    name="caramelo",
)

if __name__ == "__main__":
    mcp.run()
