#!/usr/bin/env bash
# Build & push the Caramelo container images to the Cloudflare registry, then
# bump the tag in the two wrangler.jsonc files. Run this when the Python
# harvester or API code changes; ordinary Worker/site/docs changes deploy
# straight from Workers Builds without touching containers.
#
# Requires: docker, wrangler, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID.
# Usage: deploy/cloudflare/build-images.sh <tag>   e.g. cf6
set -euo pipefail

TAG="${1:?usage: build-images.sh <tag>}"
ACCT="${CLOUDFLARE_ACCOUNT_ID:?set CLOUDFLARE_ACCOUNT_ID}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REG="registry.cloudflare.com/${ACCT}"

docker build -t "caramelo:${TAG}"     -f "${ROOT}/Dockerfile"     "${ROOT}"
docker build -t "caramelo-api:${TAG}" -f "${ROOT}/Dockerfile.api" "${ROOT}"

wrangler containers push "caramelo:${TAG}"
wrangler containers push "caramelo-api:${TAG}"

sed -i "s|caramelo:[a-z0-9.]*\"|caramelo:${TAG}\"|" \
    "${ROOT}/deploy/cloudflare/scheduler/wrangler.jsonc"
sed -i "s|caramelo-api:[a-z0-9.]*\"|caramelo-api:${TAG}\"|" \
    "${ROOT}/deploy/cloudflare/api/wrangler.jsonc"

echo "pushed ${REG}/caramelo:${TAG} and caramelo-api:${TAG}; wrangler.jsonc updated."
echo "commit + push to let Workers Builds deploy the new image."
