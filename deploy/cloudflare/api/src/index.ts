// Caramelo API/MCP Worker: proxies api. and mcp. hosts to the uvicorn
// container. mcp.caramelo.dev.br/* maps to the container's /mcp/*.

import { Container, getContainer } from "@cloudflare/containers";

export class ApiContainer extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "15m";
}

interface Env {
  API: DurableObjectNamespace<ApiContainer>;
}

export default {
  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);
    if (url.hostname.startsWith("mcp.")) {
      url.pathname = `/mcp${url.pathname === "/" ? "/" : url.pathname}`;
    }
    const proxied = new Request(url.toString(), request);
    return getContainer(env.API, "api").fetch(proxied);
  },
} satisfies ExportedHandler<Env>;
