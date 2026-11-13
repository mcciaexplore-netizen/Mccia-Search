#!/bin/bash
# Put photo search into the LIVE backend: a new image (Dockerfile.bge: CPU torch, so CLIP's text side can run) replaces the running container, still searching with
# Gemini exactly as before. Run it ON THE SERVER, in the repository clone (~/mccia-search):
#
#   deploy/live-switch.sh prepare    build the image, run the checks against the live database (read-only); the live container is NOT touched
#   deploy/live-switch.sh switch     replace the live container; goes back by itself if the new one is not healthy, or any check fails
#   deploy/live-switch.sh rollback   put the previous container back
#   deploy/live-switch.sh status     what is live, and its memory
#
# Needs SUPABASE_SERVICE_ROLE_KEY in ~/mccia-search/.env (the backend reads private photographs with it); `prepare` says so if it is missing.
# The model files are the ones the pipeline already put in ~/hf-models (CLIP is about 0.6 GB of them).
set -u
NAME=Mccia-Sampada-Backend
WAITING=$NAME-nophotos          # where the previous container waits while the new one runs
IMAGE=mccia-backend:live
MODELS=$HOME/hf-models
MEMORY=2g                       # the new container needs about 1 GB; it can never use more than this
NEED_GB=6

say() { echo "== $*"; }
die() { echo "STOP: $*"; exit 1; }
cd "$(dirname "$0")/.." || die "cannot find the repository"
[ -f .env ] || die "no .env here: run this in the server's clone (~/mccia-search)"

healthy() { for _ in $(seq 1 "$(( $1 / 2 ))"); do curl -s -m 3 localhost:8002/health </dev/null | grep -q '"ok"' && return 0; sleep 2; done; return 1; }

start_new() {
  docker run -d --name "$NAME" --restart=always -p 8002:8000 --memory="$MEMORY" --memory-swap="$MEMORY" \
    -e PYTHONUNBUFFERED=1 -e EMBEDDING_ENGINE=gemini -e HF_HOME=/models -e HF_HUB_OFFLINE=1 -v "$MODELS":/models \
    "$IMAGE" </dev/null >/dev/null
}

go_back() {
  say "going back to the previous container"
  docker rm -f "$NAME" >/dev/null 2>&1
  docker rename "$WAITING" "$NAME" || die "there is no waiting container ($WAITING): start the old image by hand (docker images shows mccia-backend:latest)"
  docker update --restart=always "$NAME" >/dev/null
  docker start "$NAME" >/dev/null
  if healthy 90; then echo "the previous backend is live again: $(curl -s -m 3 localhost:8002/health </dev/null)"; else echo "NOT HEALTHY after going back: docker logs $NAME"; exit 2; fi
}

case "${1:-}" in
prepare)
  free=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
  [ "$free" -ge "$NEED_GB" ] || die "only ${free} GB of disk is free; the image needs about $NEED_GB GB"
  grep -q '^SUPABASE_SERVICE_ROLE_KEY=.\+' .env || die "SUPABASE_SERVICE_ROLE_KEY is not in $(pwd)/.env: add it first (the backend cannot read the private photographs without it)"
  [ -d "$MODELS/hub" ] || die "no model files in $MODELS"
  say "pull"; git pull --ff-only origin main </dev/null 2>&1 | tail -2; echo "HEAD: $(git log --oneline -1)"
  say "build $IMAGE (a few minutes; the server is busy meanwhile, so do it when it is quiet)"
  docker build -f deploy/Dockerfile.bge -t "$IMAGE" . 2>&1 | tail -3
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "the build failed: nothing was changed, the live container is untouched"
  say "checks in a throwaway container (live database read-only; the live container is not touched)"
  docker run --rm --memory="$MEMORY" --memory-swap="$MEMORY" -e EMBEDDING_ENGINE=gemini -e HF_HOME=/models -e HF_HUB_OFFLINE=1 -v "$MODELS":/models "$IMAGE" python -c "
import resource, time
t = time.time()
from photos import runtime
from photos.search import search_photos
store = runtime.get_store()
print('storage key found:', store is not None, '| project:', store.base if store else None)
clip = runtime.get_clip(); clip.warm_up(); print(f'CLIP text side loaded in {time.time()-t:.1f}s')
import numpy as np
v = clip.embed('a group of people at a ceremony'); print('vector:', v.shape, 'norm', round(float(np.linalg.norm(v)), 3))
from db.connection import get_connection
with get_connection() as c:
    print('photos in the database:', c.execute('SELECT COUNT(*) FROM photo_index').fetchone()[0], '| pages looked at:', c.execute('SELECT COUNT(*) FROM photo_scan').fetchone()[0])
for q in ('photo of a portrait of a man', 'photos of a group of people at a ceremony', 'picture of an advertisement for machines'):
    t = time.time(); hits = search_photos(q, 3, clip_text=clip)
    print(f'{q!r}: {len(hits)} hits in {time.time()-t:.1f}s', [(h.document_id, h.page_number, round(h.score, 2), h.caption[:30]) for h in hits])
if store and hits: print('signed link works:', bool(store.signed_urls([hits[0].storage_path])))
print('peak memory: %.0f MB' % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024))
" </dev/null 2>&1 | grep -v "Warning\|warn"
  echo "(read the lines above; if they look right, run: deploy/live-switch.sh switch)"
  ;;
switch)
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "no $IMAGE image: run prepare first"
  docker inspect "$WAITING" >/dev/null 2>&1 && die "a container called $WAITING already exists: remove it (docker rm $WAITING) once you no longer need it"
  say "stop the live container and keep it as $WAITING"
  docker stop "$NAME" >/dev/null || die "could not stop $NAME"
  docker rename "$NAME" "$WAITING"; docker update --restart=no "$WAITING" >/dev/null
  say "start the new one"
  start_new || { echo "the new container could not be started"; go_back; exit 1; }
  healthy 60 || { echo "NOT HEALTHY"; docker logs --tail 15 "$NAME" 2>&1 | cut -c1-160; go_back; exit 1; }
  ready=0; for _ in $(seq 1 60); do docker logs "$NAME" 2>&1 | grep -q "photo search ready\|photo search unavailable" && { ready=1; break; }; sleep 2; done
  docker logs "$NAME" 2>&1 | grep "photo search\|embedding engine" | cut -c1-200
  docker logs "$NAME" 2>&1 | grep -q "photo search ready" || { echo "photo search did not come up"; go_back; exit 1; }
  say "checks on the new container"
  KEY=$(grep '^API_KEY=' .env | cut -d= -f2-)
  code() { curl -s -o /dev/null -w '%{http_code}' -m 90 "$@" </dev/null; }
  bad=0; chk() { if [ "$2" = "$3" ]; then echo "ok   $1: $2"; else echo "FAIL $1: got $2, want $3"; bad=1; fi; }
  chk "GET /health"                          "$(code localhost:8002/health)" 200
  chk "POST /chat, no key"                   "$(code -X POST localhost:8002/chat -H 'Content-Type: application/json' -d '{"query":"x"}')" 401
  chk "POST /search/photos, no key"          "$(code -X POST localhost:8002/search/photos -H 'Content-Type: application/json' -d '{"query":"photo of x"}')" 401
  chk "GET /photos/1/image, no key"          "$(code localhost:8002/photos/1/image)" 401
  chk "GET /documents, no key"               "$(code localhost:8002/documents)" 401
  chk "POST /chat, an ordinary question"     "$(code -X POST localhost:8002/chat -H "X-API-Key: $KEY" -H 'X-User-Id: smoke-check' -H 'Content-Type: application/json' -d '{"query":"Who founded MCCIA?"}')" 200
  chk "POST /search/photos"                  "$(code -X POST localhost:8002/search/photos -H "X-API-Key: $KEY" -H 'X-User-Id: smoke-check' -H 'Content-Type: application/json' -d '{"query":"photos of people at a ceremony"}')" 200
  answer=$(curl -s -m 90 -X POST localhost:8002/chat -H "X-API-Key: $KEY" -H 'X-User-Id: smoke-check' -H 'Content-Type: application/json' -d '{"query":"show photos of a ceremony"}' </dev/null)
  echo "$answer" | grep -q '"photos"' && echo "ok   a photo question comes back with a photos field" || { echo "FAIL a photo question has no photos field"; bad=1; }
  pid=$(curl -s -m 60 -X POST localhost:8002/search/photos -H "X-API-Key: $KEY" -H 'X-User-Id: smoke-check' -H 'Content-Type: application/json' -d '{"query":"photos of people at a ceremony"}' </dev/null | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
  if [ -n "${pid:-}" ]; then chk "GET /photos/$pid/image" "$(code -H "X-API-Key: $KEY" localhost:8002/photos/$pid/image)" 200; else echo "(no photograph matched the test request, so the image route was not tried)"; fi
  [ $bad = 0 ] || { echo "a check failed"; docker logs --tail 15 "$NAME" 2>&1 | cut -c1-160; go_back; exit 1; }
  say "the new backend is live"; docker stats --no-stream --format 'memory: {{.MemUsage}} ({{.MemPerc}})' "$NAME"
  echo "The previous container waits as $WAITING (stopped). Go back any time:  deploy/live-switch.sh rollback"
  echo "The checks left audit rows with user_id = 'smoke-check': delete them. Keep $WAITING for about a week, then: docker rm $WAITING"
  ;;
rollback)
  docker inspect "$WAITING" >/dev/null 2>&1 || die "there is no waiting container ($WAITING)"
  docker stop "$NAME" >/dev/null 2>&1
  go_back
  ;;
status)
  say "live container: $(docker ps --filter "name=^${NAME}\$" --format '{{.Names}} {{.Image}} {{.Status}}')"
  docker stats --no-stream --format 'memory: {{.MemUsage}} ({{.MemPerc}}), cpu {{.CPUPerc}}' "$NAME"
  docker inspect "$WAITING" >/dev/null 2>&1 && echo "waiting: $WAITING (stopped, the way back)"
  docker logs "$NAME" 2>&1 | grep "photo search" | tail -2 | cut -c1-200
  ;;
*)
  sed -n '2,10p' "$0"; exit 1
  ;;
esac
