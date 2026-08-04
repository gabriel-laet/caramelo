# Deploying Caramelo on Cloudflare (100%)

Target architecture:

```
Cron Trigger → Container (this repo's Dockerfile): caramelo run-all → R2
R2 (public bucket): latest/*.parquet + manifest.json, harvests/, events/
Worker (next phase): reads new events/*.jsonl → Queues → webhook delivery
Workers (later): api.caramelo.dev.br, mcp.caramelo.dev.br
Pages: caramelo.dev.br docs
```

## One-time account setup

1. **Workers Paid plan** ($5/mo) — required for Containers and Queues.
2. **R2 bucket**: create `caramelo-data`. Note the account id; the S3
   endpoint is `https://<account_id>.r2.cloudflarestorage.com`.
3. **R2 API token** with Object Read & Write on the bucket. This yields the
   S3-style key pair used by the publisher (boto3).
4. **Public data access**: enable public access on the bucket (or attach the
   custom domain `data.caramelo.dev.br`) so `latest/*.parquet` is fetchable
   by anyone — DuckDB-WASM clients included.
5. **DNS**: point `caramelo.dev.br` nameservers at Cloudflare (registro.br
   panel), so Workers routes and the data domain can be attached.

## Harvest scheduler (Container + Cron)

The `scheduler/` Worker owns the container and triggers it on a schedule.
Deploy once the account exists:

```bash
cd deploy/cloudflare/scheduler
npm install
npx wrangler secret put AWS_ACCESS_KEY_ID
npx wrangler secret put AWS_SECRET_ACCESS_KEY
npx wrangler secret put CARAMELO_R2_ENDPOINT   # https://<acct>.r2.cloudflarestorage.com
npx wrangler secret put CARAMELO_R2_BUCKET     # caramelo-data
npx wrangler deploy
```

> NOTE: `scheduler/` is a skeleton written ahead of account creation and has
> not been deployed yet. Expect to iterate on first deploy (instance size,
> cron cadence, container lifecycle flags).

## Local / CI parity

The exact same image runs anywhere:

```bash
docker build -t caramelo .
docker run -e CARAMELO_PUBLISH_TARGET=local:/out -v $PWD/out:/out caramelo
```

GitHub Actions can run the same image on a schedule as a free, publicly
auditable fallback runner — kept as the reproducibility path for forks
without a Cloudflare account.
