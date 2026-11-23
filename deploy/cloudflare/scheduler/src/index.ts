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
    PYTHONUNBUFFERED: "1",
    CARAMELO_SEARCHAPI_KEY: this.env.SEARCHAPI_KEY,
    CARAMELO_MEDIA_BUDGET: "25",
    CARAMELO_AI_TOKEN: this.env.AI_TOKEN,
    // YouTube Data API: channel/video metadata + stats only (media and
    // transcripts are pulled on the local network, never in this container)
    CARAMELO_YOUTUBE_KEY: this.env.YOUTUBE_KEY,
    CARAMELO_YOUTUBE_UNITS: "8000",
  };
}

interface Env {
  HARVESTER: DurableObjectNamespace<HarvestContainer>;
  AWS_ACCESS_KEY_ID: string;
  AWS_SECRET_ACCESS_KEY: string;
  CARAMELO_R2_ENDPOINT: string;
  CARAMELO_R2_BUCKET: string;
  TRIGGER_TOKEN: string;
  SEARCHAPI_KEY: string;
  AI_TOKEN: string;
  YOUTUBE_KEY?: string;
}

type StartResult = { ok: boolean; message: string };

const RUNNING = new Set(["running", "healthy"]);

// start() only means "the start command was issued" — during an image
// rollout it can resolve without a container ever running (Nov 2026). So
// poll the Durable Object's state until the container is really up.
async function startHarvest(env: Env): Promise<StartResult> {
  const stub = env.HARVESTER.getByName("daily-harvest");
  const before = await stub.getState();
  if (RUNNING.has(before.status)) {
    return { ok: true, message: "harvest already running" };
  }
  const t0 = Date.now();
  await stub.start();
  for (let i = 0; i < 30; i++) {
    const s = await stub.getState();
    if (s.lastChange >= t0) {
      if (RUNNING.has(s.status)) {
        return { ok: true, message: "harvest started" };
      }
      if (s.status === "stopped_with_code") {
        return { ok: false,
                 message: `harvest exited right away (code ${s.exitCode ?? "?"})` };
      }
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  const s = await stub.getState();
  return { ok: false,
           message: `container not running after 60s (status: ${s.status})` };
}

export default {
  async scheduled(_controller: ScheduledController, env: Env) {
    const r = await startHarvest(env);
    console.log(`cron: ${r.message}`);
    if (!r.ok) throw new Error(r.message); // marks the cron run as failed
  },

  // Manual trigger for testing/ops: POST /trigger with Bearer TRIGGER_TOKEN.
  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);
    if (request.method === "POST" && url.pathname === "/trigger") {
      const auth = request.headers.get("Authorization") ?? "";
      if (auth !== `Bearer ${env.TRIGGER_TOKEN}`) {
        return new Response("unauthorized", { status: 401 });
      }
      const r = await startHarvest(env);
      return new Response(`${r.message}\n`, { status: r.ok ? 202 : 503 });
    }
    return new Response("caramelo-scheduler\n");
  },
} satisfies ExportedHandler<Env>;
