#!/bin/bash
# Move the live backend between Gemini and bge-m3 without touching the database. Run it ON THE SERVER, in the repository clone (~/mccia-search):
#
#   deploy/bge-switch.sh prepare    build the bge image, fetch the model, run every check; the live container is NOT touched
#   deploy/bge-switch.sh switch     replace the live container with the bge one; goes back by itself if the new one is not healthy
#   deploy/bge-switch.sh rollback   put the Gemini container back
#   deploy/bge-switch.sh status     which engine is live, and what it costs in memory
#
# Why this is safe: Gemini's vectors (chunks.embedding) and bge-m3's (chunks.embedding_bge) sit side by side in the database, and each backend
# reads only its own. Switching changes the container, never the data, so going back is just starting the old container again.
set -u
NAME=Mccia-Sampada-Backend      # the live container
WAITING=$NAME-gemini            # where the Gemini container waits while bge runs
IMAGE=mccia-backend:bge
MODELS=$HOME/hf-models          # the model lives here, outside the image
MEMORY=4g                       # the bge container may use this much (it needs about 3 GB), so it can never starve the other apps
NEED_GB=8                       # free disk the image and the model need

say() { echo "== $*"; }
die() { echo "STOP: $*"; exit 1; }
cd "$(dirname "$0")/.." || die "cannot find the repository"
[ -f .env ] || die "no .env here: run this in the server's clone (~/mccia-search)"

healthy() { # wait up to $1 seconds for /health
  for _ in $(seq 1 "$(( $1 / 2 ))"); do curl -s -m 3 localhost:8002/health </dev/null | grep -q '"ok"' && return 0; sleep 2; done; return 1
}
live_engine() { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$NAME" 2>/dev/null | grep '^EMBEDDING_ENGINE=' | cut -d= -f2; }

start_bge() {
  docker run -d --name "$NAME" --restart=always -p 8002:8000 --memory="$MEMORY" \
    -e PYTHONUNBUFFERED=1 -e EMBEDDING_ENGINE=bge -e HF_HOME=/models -e HF_HUB_OFFLINE=1 -v "$MODELS":/models \
    "$IMAGE" </dev/null >/dev/null
}

go_back() { # the Gemini container back in place
  say "going back to the Gemini container"
  docker rm -f "$NAME" >/dev/null 2>&1
  docker rename "$WAITING" "$NAME" || die "there is no waiting Gemini container ($WAITING): start the old image by hand"
  docker update --restart=always "$NAME" >/dev/null
  docker start "$NAME" >/dev/null
  if healthy 90; then echo "Gemini is live again: $(curl -s -m 3 localhost:8002/health </dev/null)"; else echo "NOT HEALTHY after going back: docker logs $NAME"; exit 2; fi
}

case "${1:-}" in
prepare)
  free=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
  [ "$free" -ge "$NEED_GB" ] || die "only ${free} GB of disk is free; the image and the model need about $NEED_GB GB"
  say "pull"; git pull --ff-only origin main </dev/null 2>&1 | tail -2; echo "HEAD: $(git log --oneline -1)"
  say "build $IMAGE (several minutes; the server is busy meanwhile, so do it when it is quiet)"
  docker build -f deploy/Dockerfile.bge -t "$IMAGE" . 2>&1 | tail -4
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "the build failed: nothing was changed, the live container is untouched"
  echo "image: $(docker image inspect "$IMAGE" --format '{{.Size}}' | awk '{printf "%.1f GB", $1/1073741824}')"
  say "fetch the model into $MODELS (once; 2.3 GB)"
  mkdir -p "$MODELS"
  docker run --rm -v "$MODELS":/models "$IMAGE" python -c "
from huggingface_hub import snapshot_download
snapshot_download('BAAI/bge-m3', ignore_patterns=['onnx/*', 'imgs/*', '*.jpg', '*.png', 'colbert_linear.pt', 'sparse_linear.pt', '*.md', '.gitattributes'])
print('model files are in place')" </dev/null 2>&1 | tail -2
  say "checks, in a throwaway container with the model offline (the live container is not touched)"
  docker run --rm --memory="$MEMORY" -e EMBEDDING_ENGINE=bge -e HF_HUB_OFFLINE=1 -v "$MODELS":/models "$IMAGE" python -m ingest.preflight_bge </dev/null 2>&1 | grep -v "Warning\|warn" | tail -12
  echo "(read the lines above: every check must say ok before you run: deploy/bge-switch.sh switch)"
  ;;
switch)
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "no $IMAGE image: run prepare first"
  [ "$(live_engine)" != "bge" ] || die "the live container already runs bge"
  docker inspect "$WAITING" >/dev/null 2>&1 && die "a container called $WAITING already exists: remove it (docker rm $WAITING) once you no longer need it"
  say "stop the Gemini container and keep it as $WAITING"
  docker stop "$NAME" >/dev/null || die "could not stop $NAME"
  docker rename "$NAME" "$WAITING"; docker update --restart=no "$WAITING" >/dev/null
  say "start the bge container"
  start_bge || { echo "the bge container could not be started"; go_back; exit 1; }
  healthy 60 || { echo "NOT HEALTHY"; docker logs --tail 15 "$NAME" 2>&1 | cut -c1-160; go_back; exit 1; }
  echo "answering /health after about $(( SECONDS ))s; waiting for the model to load"
  ready=0; for _ in $(seq 1 90); do docker logs "$NAME" 2>&1 | grep -q "the bge model is ready" && { ready=1; break; }; sleep 2; done
  [ $ready = 1 ] || { echo "the model did not finish loading in 3 minutes"; docker logs --tail 15 "$NAME" 2>&1 | cut -c1-160; go_back; exit 1; }
  docker logs "$NAME" 2>&1 | grep "embedding engine\|model is ready" | cut -c1-160
  say "checks on the live bge container"
  KEY=$(grep '^API_KEY=' .env | cut -d= -f2-)
  code() { curl -s -o /dev/null -w '%{http_code}' -m 60 "$@" </dev/null; }
  bad=0; chk() { if [ "$2" = "$3" ]; then echo "ok   $1: $2"; else echo "FAIL $1: got $2, want $3"; bad=1; fi; }
  chk "POST /chat, no key"      "$(code -X POST localhost:8002/chat -H 'Content-Type: application/json' -d '{"query":"x"}')" 401
  chk "GET /documents, no key"  "$(code localhost:8002/documents)" 401
  chk "GET /health"             "$(code localhost:8002/health)" 200
  answer=$(curl -s -m 90 -X POST localhost:8002/chat -H "X-API-Key: $KEY" -H 'X-User-Id: smoke-check' -H 'Content-Type: application/json' \
           -d '{"query":"Who was the first president of India?"}' </dev/null)
  echo "$answer" | grep -q '"found":true' && echo "ok   a real question found its sources" || { echo "FAIL a real question did not find sources"; bad=1; }
  [ $bad = 0 ] || { echo "a check failed"; docker logs --tail 15 "$NAME" 2>&1 | cut -c1-160; go_back; exit 1; }
  say "bge is live"; docker stats --no-stream --format 'memory: {{.MemUsage}} ({{.MemPerc}})' "$NAME"
  echo "The Gemini container waits as $WAITING (stopped). Go back any time:  deploy/bge-switch.sh rollback"
  echo "Delete the smoke-check row this left in the audit log (user_id = 'smoke-check'). Keep $WAITING for about a week, then: docker rm $WAITING"
  ;;
rollback)
  docker inspect "$WAITING" >/dev/null 2>&1 || die "there is no waiting Gemini container ($WAITING)"
  docker stop "$NAME" >/dev/null 2>&1
  go_back
  ;;
status)
  say "live container: $(docker ps --filter "name=^${NAME}\$" --format '{{.Names}} {{.Image}} {{.Status}}')"
  echo "engine: $(live_engine | sed 's/^$/gemini (EMBEDDING_ENGINE not set)/')"
  docker stats --no-stream --format 'memory: {{.MemUsage}} ({{.MemPerc}}), cpu {{.CPUPerc}}' "$NAME"
  docker inspect "$WAITING" >/dev/null 2>&1 && echo "waiting: $WAITING (stopped, the way back)"
  docker logs "$NAME" 2>&1 | grep "embedding engine\|model is ready" | tail -3 | cut -c1-160
  ;;
*)
  sed -n '2,10p' "$0"; exit 1
  ;;
esac
