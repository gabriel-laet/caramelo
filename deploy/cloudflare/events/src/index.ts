// Caramelo events worker.
//
// Flow: publisher writes events/<ts>.jsonl to R2 -> cron scan finds files
// newer than the KV cursor -> each event line becomes a queue message ->
// the queue consumer matches events against subscriptions (KV) and POSTs
// webhooks. Failed deliveries are retried by the queue (max_retries, then
// dead-letter behavior per queue settings).
//
// Subscription registry (KV):
//   key:   sub:<id>
//   value: { "url": "https://...", "types": ["emenda.paid"], "uf": "RR" }
//          types/uf are optional filters; omitted = match everything.
//
// Admin API (Bearer ADMIN_TOKEN):
//   POST   /subscriptions   {url, types?, uf?}  -> {id}
//   GET    /subscriptions                       -> list
//   DELETE /subscriptions/<id>
//   POST   /scan            force an R2 scan now (testing/ops)

interface Env {
  DATA: R2Bucket;
  SUBS: KVNamespace;
  EVENTS_QUEUE: Queue<CaramelloEvent>;
  ADMIN_TOKEN: string;
}

interface CaramelloEvent {
  type: string;
  at: string;
  [key: string]: unknown;
}

interface Subscription {
  url: string;
  types?: string[];
  uf?: string;
}

const CURSOR_KEY = "cursor:events";

async function scan(env: Env): Promise<{ files: number; events: number }> {
  const cursor = (await env.SUBS.get(CURSOR_KEY)) ?? "";
  const listing = await env.DATA.list({ prefix: "events/" });
  const fresh = listing.objects
    .map((o) => o.key)
    .filter((k) => k > `events/${cursor}`)
    .sort();

  let events = 0;
  for (const key of fresh) {
    const obj = await env.DATA.get(key);
    if (!obj) continue;
    const lines = (await obj.text()).split("\n").filter((l) => l.trim());
    const messages = lines.map((l) => ({ body: JSON.parse(l) as CaramelloEvent }));
    // sendBatch caps at 100 messages per call
    for (let i = 0; i < messages.length; i += 100) {
      await env.EVENTS_QUEUE.sendBatch(messages.slice(i, i + 100));
    }
    events += messages.length;
    await env.SUBS.put(CURSOR_KEY, key.replace("events/", ""));
  }
  return { files: fresh.length, events };
}

function matches(event: CaramelloEvent, sub: Subscription): boolean {
  if (sub.types && !sub.types.some((t) => event.type.startsWith(t))) {
    return false;
  }
  if (sub.uf && event.uf !== sub.uf) return false;
  return true;
}

async function listSubscriptions(env: Env): Promise<Array<Subscription & { id: string }>> {
  const out: Array<Subscription & { id: string }> = [];
  const list = await env.SUBS.list({ prefix: "sub:" });
  for (const key of list.keys) {
    const value = await env.SUBS.get(key.name, "json");
    if (value) out.push({ id: key.name.slice(4), ...(value as Subscription) });
  }
  return out;
}

function unauthorized(): Response {
  return new Response("unauthorized", { status: 401 });
}

export default {
  async scheduled(_c: ScheduledController, env: Env) {
    const result = await scan(env);
    console.log(`scan: ${result.files} new file(s), ${result.events} event(s) enqueued`);
  },

  async queue(batch: MessageBatch<CaramelloEvent>, env: Env) {
    const subs = await listSubscriptions(env);
    for (const msg of batch.messages) {
      const targets = subs.filter((s) => matches(msg.body, s));
      let failed = false;
      for (const sub of targets) {
        try {
          const resp = await fetch(sub.url, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Caramelo-Event": msg.body.type,
            },
            body: JSON.stringify(msg.body),
          });
          if (!resp.ok && resp.status !== 410) failed = true;
        } catch {
          failed = true;
        }
      }
      if (failed) msg.retry();
      else msg.ack();
    }
  },

  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);
    const auth = request.headers.get("Authorization") ?? "";
    if (auth !== `Bearer ${env.ADMIN_TOKEN}`) return unauthorized();

    if (request.method === "POST" && url.pathname === "/scan") {
      const result = await scan(env);
      return Response.json(result);
    }
    if (request.method === "POST" && url.pathname === "/subscriptions") {
      const body = (await request.json()) as Subscription;
      if (!body.url?.startsWith("https://")) {
        return new Response("url must be https", { status: 400 });
      }
      const id = crypto.randomUUID().slice(0, 8);
      await env.SUBS.put(`sub:${id}`, JSON.stringify(body));
      return Response.json({ id });
    }
    if (request.method === "GET" && url.pathname === "/subscriptions") {
      return Response.json(await listSubscriptions(env));
    }
    if (request.method === "DELETE" && url.pathname.startsWith("/subscriptions/")) {
      await env.SUBS.delete(`sub:${url.pathname.split("/")[2]}`);
      return new Response("deleted\n");
    }
    return new Response("caramelo-events\n");
  },
} satisfies ExportedHandler<Env>;
