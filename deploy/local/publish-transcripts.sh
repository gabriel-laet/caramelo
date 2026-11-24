#!/usr/bin/env bash
# Publish the transcript text compiled on the NAS (`caramelo transcribe`)
# to the lake: uploads transcricoes.parquet and patches its manifest entry.
# Raw audio and caption files never leave the NAS. The next cloud run seeds
# the uploaded table (state.SEED_TABLES) and carries it forward.
#
#   CARAMELO_ARCHIVE_DIR=/mnt/VilaMariana/caramelo deploy/local/publish-transcripts.sh
set -euo pipefail

main() {
  export CLOUDFLARE_ACCOUNT_ID=2cdbde3608c5914c43e4433fd39ba6b2
  ARCHIVE="${CARAMELO_ARCHIVE_DIR:-archive}"
  LAKE=https://data.caramelo.dev.br/latest
  HARVEST_APP=a0378398-eb98-492c-8a12-27c7045c7546
  HERE="$(cd "$(dirname "$0")" && pwd)"
  cd "$HERE/../cloudflare/scheduler"  # wrangler config dir (account auth)

  # A cloud run seeds tables at its start and republishes at its end;
  # uploading mid-run would be overwritten.
  active=$(npx --yes wrangler containers info "$HARVEST_APP" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["health"]["instances"]["active"])' || echo "?")
  if [ "$active" != "0" ]; then
    echo "publish-transcripts: harvest container active=$active; skipping"; exit 0
  fi

  D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
  curl -fsS -o "$D/manifest.json" "$LAKE/manifest.json"
  cp "$ARCHIVE/transcricoes.parquet" "$D/transcricoes.parquet"

  "${PYTHON:-python3}" - "$D" <<'EOF' || { echo "publish-transcripts: unchanged"; exit 0; }
import hashlib, json, sys
from pathlib import Path
import pyarrow.parquet as pq
d = Path(sys.argv[1])
path, mpath = d / "transcricoes.parquet", d / "manifest.json"
manifest = json.loads(mpath.read_text())
blob = path.read_bytes()
sha = hashlib.sha256(blob).hexdigest()
old = manifest["tables"].get("transcricoes", {"file": path.name})
if old.get("sha256") == sha:
    sys.exit(3)
rows = pq.read_metadata(path).num_rows
manifest["tables"]["transcricoes"] = {**old, "bytes": len(blob),
                                      "sha256": sha, "rows": rows}
mpath.write_text(json.dumps(manifest, ensure_ascii=False))
print(f"publish-transcripts: {old.get('rows')} -> {rows} rows")
EOF

  npx --yes wrangler r2 object put caramelo-data/latest/transcricoes.parquet \
    --file "$D/transcricoes.parquet" --content-type application/octet-stream --remote
  npx --yes wrangler r2 object put caramelo-data/latest/manifest.json \
    --file "$D/manifest.json" --content-type application/json --remote
  echo "publish-transcripts: uploaded"
}
main "$@"; exit
