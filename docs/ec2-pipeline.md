# The unattended job runner on EC2 (`mccia-pipeline`)

Started 5-6 Nov 2026 so that nothing depends on a laptop being open. One container, `mccia-pipeline`, runs four steps in order (`backend/ops/pipeline.py`):

| # | step | what it does | expected time on the server's CPU |
|---|---|---|---|
| 1 | `ocr-1970` | reads the rest of `1970-Jul To 1971-Jun_1.PDF` with Tesseract (the first 123 pages were read on the Mac and copied to `~/pipeline-state/ocr_cache`) | under an hour |
| 2 | `embed-gemini` | Gemini vectors for the 1966 and 1970 volumes, so the live site can search them | 10-20 min |
| 3 | `bge-fill` | bge-m3 vectors for every chunk that has none (about 30,000), then builds the search index | **3-4 days** (about 8-11 s per chunk) |
| 4 | `photo-pilot` | the 200-page photo pilot: crops, contact sheets and results in `~/pipeline-state/photo-pilot`. Writes **nothing** to the database or to storage | hours (CPU only) |

Every step can be repeated safely, and finished steps are remembered in `~/pipeline-state/state.json`, so a restarted container carries on.

## Protecting the live site

- The container is limited to 1.5 of the server's 2 CPUs, at the lowest scheduling priority (`--cpu-shares=128`: the other containers win whenever they want CPU), and
  to 5 GB of memory with no swap, so it can only ever kill itself, never push the other apps into swap.
- The runner reads the live backend's `/health` every 20 seconds and the server's load. If the site is down or takes over 3 s, or the load is over 4, three times
  in a row, **the running job is frozen** (it keeps its memory and uses no CPU). After three good readings it carries on. Look for `FROZEN` in the log.
- If the live site cannot be reached from inside the container at start-up, the runner says so (`WARNING ... guard is OFF`) and relies on the limits alone.

## Watching it

```bash
docker logs --tail 30 mccia-pipeline                 # what it is doing now; the fill prints  [bge] n/total chunks embedded (x%) - r chunks/min - ETA
cat ~/pipeline-state/state.json                      # each step: running / retrying / done / failed
```

## Controlling it

```bash
touch ~/pipeline-state/PAUSE      # freeze the running job by hand;   rm ~/pipeline-state/PAUSE   to carry on
touch ~/pipeline-state/STOP       # end the pipeline (finished steps stay finished); then:  rm ~/pipeline-state/STOP  and  docker restart mccia-pipeline
docker stop mccia-pipeline        # same, from Docker; docker start mccia-pipeline  carries on from where it stopped
```

## When it is finished

The log ends with `all steps ended: ocr-1970=done, embed-gemini=done, bge-fill=done, photo-pilot=done`, and the container sits idle. Fetch the pilot to look at it:
`scp -r ubuntu@13.126.108.204:pipeline-state/photo-pilot ~/Desktop/photo-pilot` (open `sheet_01.jpg` ...). Then switch the live backend to bge as described in
`docs/embeddings-bge-m3.md` (`deploy/bge-switch.sh prepare` and `switch`), and `docker rm -f mccia-pipeline` when the container is no longer needed.
