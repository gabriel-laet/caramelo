// Caramelo events worker — webhooks v1.
//
// Publisher writes events/<ts>.jsonl to R2 -> cron scan fans out one queue
// message per (event, subscription) -> consumer delivers with HMAC signature,
// logs the attempt, retries independently per subscription, and suspends
// endpoints that fail persistently.
//
// Public API (rate-limited):
//   POST /subscriptions {url, types?, uf?}
//     -> {id, secret}; a verification challenge is POSTed to the URL and the
//        response body must echo the challenge token to activate.
//   GET  /subscriptions/:id      (Authorization: Bearer <secret>) status+log
//   POST /subscriptions/:id/verify {token}   alternative activation path
//   DELETE /subscriptions/:id    (Authorization: Bearer <secret>)
//   GET  /debug/echo | POST      echoes body (webhook testing aid)
//
// Deliveries:
//   POST <url> body=event JSON
//   X-Caramelo-Event: <type>   X-Caramelo-Delivery: <uuid>
//   X-Caramelo-Signature: sha256=<hex hmac(secret, body)>
//   Respond 2xx to ack; 410 Gone unsubscribes permanently.

interface Env {
  DATA: R2Bucket;
  SUBS: KVNamespace;
  EVENTS_QUEUE: Queue<DeliveryMsg>;
  ADMIN_TOKEN: string;
}

interface CaramelloEvent { type: string; at: string; [k: string]: unknown }
interface DeliveryMsg { sub_id: string; event: CaramelloEvent }

interface Subscription {
  url: string;
  types?: string[];
  uf?: string;
  secret: string;
  status: "pending" | "active" | "suspended" | "gone";
  verify_token?: string;
  consecutive_failures?: number;
  created_at: string;
}

const CURSOR_KEY = "cursor:events";
const SUSPEND_AFTER = 25;
const SIGNUPS_PER_HOUR = 10;

// ---------------------------------------------------------------- helpers

const json = (data: unknown, status = 200) =>
  Response.json(data, { status });

function hex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmac(secret: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign(
    "HMAC", key, new TextEncoder().encode(body)));
}

async function getSub(env: Env, id: string): Promise<Subscription | null> {
  return env.SUBS.get(`sub:${id}`, "json");
}

async function putSub(env: Env, id: string, sub: Subscription): Promise<void> {
  await env.SUBS.put(`sub:${id}`, JSON.stringify(sub));
}

async function appendLog(env: Env, id: string, entry: unknown): Promise<void> {
  const log = ((await env.SUBS.get(`log:${id}`, "json")) as unknown[]) ?? [];
  log.unshift(entry);
  await env.SUBS.put(`log:${id}`, JSON.stringify(log.slice(0, 20)));
}

function matches(event: CaramelloEvent, sub: Subscription): boolean {
  if (sub.status !== "active") return false;
  if (sub.types?.length &&
      !sub.types.some((t) => event.type.startsWith(t))) return false;
  if (sub.uf && event.uf && event.uf !== sub.uf) return false;
  if (sub.uf && !event.uf) return false;
  return true;
}

// ------------------------------------------------------------------ scan

async function scan(env: Env): Promise<{ files: number; deliveries: number }> {
  const cursor = (await env.SUBS.get(CURSOR_KEY)) ?? "";
  const listing = await env.DATA.list({ prefix: "events/" });
  const fresh = listing.objects.map((o) => o.key)
    .filter((k) => k > `events/${cursor}`).sort();

  const subs: Array<Subscription & { id: string }> = [];
  const list = await env.SUBS.list({ prefix: "sub:" });
  for (const key of list.keys) {
    const value = (await env.SUBS.get(key.name, "json")) as Subscription;
    if (value) subs.push({ id: key.name.slice(4), ...value });
  }

  let deliveries = 0;
  for (const key of fresh) {
    const obj = await env.DATA.get(key);
    if (!obj) continue;
    const lines = (await obj.text()).split("\n").filter((l) => l.trim());
    const msgs: { body: DeliveryMsg }[] = [];
    for (const line of lines) {
      const event = JSON.parse(line) as CaramelloEvent;
      for (const sub of subs) {
        if (matches(event, sub)) msgs.push({ body: { sub_id: sub.id, event } });
      }
    }
    for (let i = 0; i < msgs.length; i += 100) {
      await env.EVENTS_QUEUE.sendBatch(msgs.slice(i, i + 100));
    }
    deliveries += msgs.length;
    await env.SUBS.put(CURSOR_KEY, key.replace("events/", ""));
  }
  return { files: fresh.length, deliveries };
}

// -------------------------------------------------------------- delivery

async function deliver(env: Env, msg: DeliveryMsg):
    Promise<"ok" | "retry" | "gone"> {
  const sub = await getSub(env, msg.sub_id);
  if (!sub || sub.status !== "active") return "ok"; // silently drop
  const body = JSON.stringify(msg.event);
  const deliveryId = crypto.randomUUID();
  let status = 0;
  try {
    const resp = await fetch(sub.url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Caramelo-Event": msg.event.type,
        "X-Caramelo-Delivery": deliveryId,
        "X-Caramelo-Signature": `sha256=${await hmac(sub.secret, body)}`,
      },
      body,
      signal: AbortSignal.timeout(15_000),
    });
    status = resp.status;
  } catch { status = 0; }

  const ok = status >= 200 && status < 300;
  await appendLog(env, msg.sub_id, {
    at: new Date().toISOString(), delivery: deliveryId,
    event: msg.event.type, status, ok,
  });

  if (status === 410) {
    sub.status = "gone";
    await putSub(env, msg.sub_id, sub);
    return "gone";
  }
  if (ok) {
    if (sub.consecutive_failures) {
      sub.consecutive_failures = 0;
      await putSub(env, msg.sub_id, sub);
    }
    return "ok";
  }
  sub.consecutive_failures = (sub.consecutive_failures ?? 0) + 1;
  if (sub.consecutive_failures >= SUSPEND_AFTER) sub.status = "suspended";
  await putSub(env, msg.sub_id, sub);
  return sub.status === "suspended" ? "gone" : "retry";
}

// ------------------------------------------------------------ public API

async function rateLimit(env: Env, ip: string): Promise<boolean> {
  const key = `rl:${ip}:${new Date().toISOString().slice(0, 13)}`;
  const n = parseInt((await env.SUBS.get(key)) ?? "0", 10) + 1;
  await env.SUBS.put(key, String(n), { expirationTtl: 3700 });
  return n <= SIGNUPS_PER_HOUR;
}

async function createSubscription(request: Request, env: Env):
    Promise<Response> {
  const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
  if (!(await rateLimit(env, ip))) {
    return json({ error: "rate limited, try later" }, 429);
  }
  let body: { url?: string; types?: string[]; uf?: string };
  try { body = await request.json(); } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  if (!body.url?.startsWith("https://")) {
    return json({ error: "url must be https" }, 400);
  }
  const id = crypto.randomUUID().slice(0, 12);
  const secret = hex(crypto.getRandomValues(new Uint8Array(24)).buffer);
  const verifyToken = crypto.randomUUID();
  const sub: Subscription = {
    url: body.url, types: body.types, uf: body.uf?.toUpperCase(),
    secret, status: "pending", verify_token: verifyToken,
    created_at: new Date().toISOString(),
  };

  // URL-ownership challenge: endpoint must echo the token in its response
  let activated = false;
  try {
    const challenge = JSON.stringify({
      type: "subscription.verify", subscription_id: id,
      verify_token: verifyToken,
      hint: "echo verify_token in the response body (or POST it to /subscriptions/{id}/verify) to activate",
    });
    const resp = await fetch(body.url, {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 "X-Caramelo-Event": "subscription.verify" },
      body: challenge,
      signal: AbortSignal.timeout(10_000),
    });
    const text = (await resp.text()).slice(0, 4096);
    if (resp.ok && text.includes(verifyToken)) activated = true;
  } catch { /* endpoint unreachable: stays pending */ }

  if (activated) { sub.status = "active"; sub.verify_token = undefined; }
  await putSub(env, id, sub);
  return json({
    id, secret, status: sub.status,
    verify_token: activated ? undefined : verifyToken,
    note: activated
      ? "active — deliveries are signed with X-Caramelo-Signature (HMAC-SHA256 of the body with your secret)"
      : "pending — confirm ownership: POST {\"token\": verify_token} to /subscriptions/" + id + "/verify (we also POSTed the token to your URL)",
  }, 201);
}

// ---------------------------------------------------------------- worker

export default {
  async scheduled(_c: ScheduledController, env: Env) {
    const r = await scan(env);
    console.log(`scan: ${r.files} file(s), ${r.deliveries} deliveries queued`);
  },

  async queue(batch: MessageBatch<DeliveryMsg>, env: Env) {
    for (const msg of batch.messages) {
      const result = await deliver(env, msg.body);
      if (result === "retry") msg.retry();
      else msg.ack();
    }
  },

  async fetch(request: Request, env: Env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const auth = (request.headers.get("Authorization") ?? "")
      .replace("Bearer ", "");

    if (path === "/debug/echo") {
      return new Response(await request.text() || "echo", { status: 200 });
    }
    if (request.method === "POST" && path === "/subscriptions") {
      return createSubscription(request, env);
    }
    const subMatch = path.match(/^\/subscriptions\/([a-z0-9-]+)(\/verify)?$/);
    if (subMatch) {
      const [, id, isVerify] = subMatch;
      const sub = await getSub(env, id);
      if (!sub) return json({ error: "not found" }, 404);
      if (request.method === "POST" && isVerify) {
        const body = (await request.json().catch(() => ({}))) as
          { token?: string };
        if (sub.status === "pending" && body.token &&
            body.token === sub.verify_token) {
          sub.status = "active"; sub.verify_token = undefined;
          await putSub(env, id, sub);
          return json({ id, status: "active" });
        }
        return json({ error: "invalid token" }, 400);
      }
      if (auth !== sub.secret && auth !== env.ADMIN_TOKEN) {
        return json({ error: "unauthorized" }, 401);
      }
      if (request.method === "GET") {
        const log = await env.SUBS.get(`log:${id}`, "json");
        const { secret: _s, verify_token: _v, ...visible } = sub;
        return json({ id, ...visible, deliveries: log ?? [] });
      }
      if (request.method === "DELETE") {
        await env.SUBS.delete(`sub:${id}`);
        await env.SUBS.delete(`log:${id}`);
        return json({ deleted: id });
      }
    }
    // admin utilities
    if (auth === env.ADMIN_TOKEN) {
      if (request.method === "POST" && path === "/scan") {
        return json(await scan(env));
      }
      if (request.method === "GET" && path === "/subscriptions") {
        const out = [];
        const list = await env.SUBS.list({ prefix: "sub:" });
        for (const k of list.keys) {
          const s = (await env.SUBS.get(k.name, "json")) as Subscription;
          out.push({ id: k.name.slice(4), url: s.url, status: s.status,
                     types: s.types, uf: s.uf });
        }
        return json(out);
      }
    }
    return new Response(
      "caramelo-events — docs: https://caramelo.dev.br/docs/events.md\n");
  },
} satisfies ExportedHandler<Env>;
