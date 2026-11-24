#!/usr/bin/env bash
# Refresh APS coverage (aps_cobertura_pct) in the published lake from this
# machine. The Health Ministry endpoint fails from the cloud harvester, which
# then carries the last published APS rows forward; this script fetches them
# over the local network and uploads indicadores_municipio + manifest to R2.
# Runs daily from caramelo-refresh-aps.timer, after the 09:00 UTC cloud run.
set -euo pipefail

# Wrapped in main so bash parses it all before the checkout below can
# replace this very file.
main() {

  export PATH="$HOME/.local/share/mise/installs/node/latest/bin:$HOME/.local/share/mise/installs/uv/latest/.mise-bins:/usr/bin:/bin"
  export CLOUDFLARE_ACCOUNT_ID=2cdbde3608c5914c43e4433fd39ba6b2
  REPO="${CARAMELO_REPO:-$HOME/caramelo}"
  WT="${CARAMELO_APS_WORKTREE:-$HOME/.local/share/caramelo-aps}"
  LAKE=https://data.caramelo.dev.br/latest
  HARVEST_APP=a0378398-eb98-492c-8a12-27c7045c7546

  # Code: a detached worktree at origin/main, never the interactive checkout.
  git -C "$REPO" fetch -q origin main
  if [ ! -d "$WT" ]; then
    git -C "$REPO" worktree add -q --detach "$WT" origin/main
  fi
  git -C "$WT" checkout -q --detach origin/main
  cd "$WT/deploy/cloudflare/scheduler"  # wrangler config dir (account auth)

  # A cloud run seeds indicadores at its start and republishes at its end;
  # uploading mid-run would be overwritten. Skip while one is active or just
  # finished publishing.
  active=$(npx --yes wrangler containers info "$HARVEST_APP" 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["health"]["instances"]["active"])' || echo "?")
  if [ "$active" != "0" ]; then
    echo "refresh-aps: harvest container active=$active; skipping"; exit 0
  fi

  D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
  for f in manifest.json indicadores_municipio.parquet municipios.parquet; do
    curl -fsS -o "$D/$f" "$LAKE/$f"
  done
  age=$(python3 -c '
  import json,sys,datetime as d
  g=json.load(open(sys.argv[1]))["generated_at"]
  print(int((d.datetime.now(d.timezone.utc)-d.datetime.fromisoformat(g.replace("Z","+00:00"))).total_seconds()))' "$D/manifest.json")
  if [ "$age" -lt "${CARAMELO_APS_MIN_AGE:-1800}" ]; then
    echo "refresh-aps: manifest only ${age}s old (run just published); skipping"; exit 0
  fi

  set +e
  uv run --project "$WT" python -m caramelo --data-dir "$D" refresh-aps
  rc=$?
  set -e
  case $rc in
    3) exit 0 ;;
    0) ;;
    *) echo "refresh-aps: fetch failed (rc=$rc)"; exit 1 ;;
  esac

  npx --yes wrangler r2 object put caramelo-data/latest/indicadores_municipio.parquet \
    --file "$D/indicadores_municipio.parquet" --content-type application/octet-stream --remote
  npx --yes wrangler r2 object put caramelo-data/latest/manifest.json \
    --file "$D/manifest.json" --content-type application/json --remote
  echo "refresh-aps: uploaded"
}
main "$@"; exit
