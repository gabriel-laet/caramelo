// Caramelo harvest scheduler.
// UNTESTED SKELETON — written ahead of Cloudflare account creation.
// The container image runs `caramelo run-all` (harvest -> resolve -> publish
// to R2) and exits; the Durable Object holds the container lifecycle.

import { Container } from "@cloudflare/containers";

export class HarvestContainer extends Container<Env> {
  // Job-style container: no ports to expose, stop when the process exits.
  sleepAfter = "15m";
  envVars = {
    AWS_ACCESS_KEY_ID: this.env.AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY: this.env.AWS_SECRET_ACCESS_KEY,
    CARAMELO_R2_ENDPOINT: this.env.CARAMELO_R2_ENDPOINT,
    CARAMELO_R2_BUCKET: this.env.CARAMELO_R2_BUCKET,
    CARAMELO_PUBLISH_TARGET: "r2",
  };
}

interface Env {
  HARVESTER: DurableObjectNamespace<HarvestContainer>;
  AWS_ACCESS_KEY_ID: string;
  AWS_SECRET_ACCESS_KEY: string;
  CARAMELO_R2_ENDPOINT: string;
  CARAMELO_R2_BUCKET: string;
  TRIGGER_TOKEN: string;
}

async function startHarvest(env: Env): Promise<void> {
  const stub = env.HARVESTER.getByName("daily-harvest");
  await stub.start();
}

export default {
  async scheduled(_controller: ScheduledController, env: Env) {
    await startHarvest(env);
  },

  // Manual trigger for testing/ops: POST /trigger with Bearer TRIGGER_TOKEN.
  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/trigger") {
      const auth = request.headers.get("Authorization") ?? "";
      if (auth !== `Bearer ${env.TRIGGER_TOKEN}`) {
        return new Response("unauthorized", { status: 401 });
      }
      await startHarvest(env);
      return new Response("harvest started\n");
    }
    return new Response("caramelo-scheduler\n");
  },
} satisfies ExportedHandler<Env>;
