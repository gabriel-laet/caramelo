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
}

export default {
  async scheduled(_controller: ScheduledController, env: Env) {
    const stub = env.HARVESTER.getByName("daily-harvest");
    await stub.start();
  },
} satisfies ExportedHandler<Env>;
