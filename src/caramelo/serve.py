"""Combined HTTP server: REST API at / and the MCP server at /mcp.

One uvicorn process serves both surfaces from the same domain functions:

    uvicorn caramelo.serve:app --host 0.0.0.0 --port 8080

In production the Worker routes api.caramelo.dev.br -> / and
mcp.caramelo.dev.br -> /mcp on this container.
"""

from __future__ import annotations

from caramelo.api import app
from caramelo.mcp_server import mcp

mcp_app = mcp.http_app(path="/")
# FastMCP's ASGI app owns a lifespan (session manager); adopt it.
app.router.lifespan_context = mcp_app.lifespan
app.mount("/mcp", mcp_app)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True}
