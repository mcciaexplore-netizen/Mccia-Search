# Surya OCR (optional engine, `OCR_ENGINE=surya`)

**Status on 5 Nov 2026:** the engine and a safe re-OCR script are built and tested, but **nothing has been re-OCR'd** and nothing has been
changed in the database. Measured on this project's own pages, Surya is far too slow to be the bulk engine for the archive, and its quality
on the pages tried is comparable to Tesseract's. Read the numbers before starting a run.

## What Surya is now

`surya-ocr` 0.23 ("Surya 2.5") is a 689M-parameter vision-language model, not the small OCR models older write-ups describe. It needs a separate
inference server: `llama.cpp`'s `llama-server` on a Mac or CPU (`brew install llama.cpp`), or vLLM in Docker on an NVIDIA GPU. The package
starts the server by itself and stops it when the process exits. The model (1.2 GB + 0.2 GB) downloads on first use. It reads Marathi, Hindi and
English without a language setting, and writes each block as HTML, which `ocr.py` turns into plain text. The code is Apache-2.0; the model
weights have their own licence (OpenRAIL-M, see https://www.datalab.to/pricing): **check that MCCIA's use is covered before relying on it.**

## Measured (Apple M2, real pages from the Drive PDFs, 192-200 DPI)

| | Surya | Tesseract (current) |
|---|---|---|
| English page (2011 issue) | 32-48 s | 2.8 s |
| Marathi page (2003 issue) | 100-109 s (a short ad page: 21 s) | about 4 s (an ad page: 1.3 s) |
| four Marathi pages at once (`OCR_CONCURRENCY=4`) | 44 s per page | |

At 44 s per page, the 8,788 pages of 2003-2017 take **about 4.5 days of non-stop running** on the Mac; the 35,183 pages of 1955-2002 would take
about 18 days on the Mac and far longer on a 2-vCPU server with no GPU (and would starve the live backend that shares it). Tesseract reads the
Mac range in about 9 hours.

Quality on the 6 pages compared (Surya vs Tesseract vs the text already stored): about the same. Surya fixed some words Tesseract garbled
("शक्य", a stray "wea") and read the masthead; Tesseract got some words right that Surya missed ("शेतकऱ्यांचा"). Neither is clearly better, so a
run of days would not obviously buy better search. A fair test needs the worst pages (the old 1950s-80s scans), which were not tried.

## Using it

```bash
cd backend && uv sync --extra local-embeddings --extra surya      # an exact sync removes an extra that is left out
uv run python -m ingest.reocr_surya --list                        # what it would read: nothing is changed
uv run python -m ingest.reocr_surya --start-year 2003 --end-year 2017
```

- It only takes PDFs that are **not finished** (`failed`, or `processing`/`pending` left by a run that stopped). A `completed` document is live,
  and replacing its text would make it unsearchable until it was embedded again, so it is never selected. The database does not record which
  engine read a document, so "the Tesseract ones" cannot be picked out. Re-reading live documents needs a swap that keeps the old text searchable
  until the new text is embedded: not built, and not worth building before a sample shows a gain.
- Text is saved as `text_saved`; run `ingest.embed_pending` afterwards, as after any OCR run.
- Stop with Ctrl-C and run again: pages already read are kept in a Surya-only cache folder (`<OCR_CACHE_DIR>/surya`), so another engine's cache is never
  mistaken for Surya's.
- **Two machines, no overlap:** use different year ranges. Each PDF is also guarded by a database lock held while it is read, so a PDF another machine is
  on is skipped, and one that is finished is never read twice.
- If the model download from Hugging Face stalls (it did here), download the two files with `curl` and set `SURYA_GGUF_LOCAL_MODEL_PATH` and
  `SURYA_GGUF_LOCAL_MMPROJ_PATH` to them.
- Server settings are Surya's own environment variables (`SURYA_INFERENCE_URL`, `SURYA_INFERENCE_BACKEND`, `SURYA_INFERENCE_PARALLEL`; see
  `surya/settings.py`). `OCR_CONCURRENCY` (default 4) sets how many pages are decoded at once.

## Not set up on the EC2 server, on purpose

It has 2 vCPUs and no GPU, runs the live backend, and has about 6 GB free; a 689M model generating about 2,400 tokens per page there would
take minutes per page, for months. If the archive is to be re-read on a server, rent a GPU machine for a day (Surya reports about 4.5 pages a second with vLLM on
an H100; not tried here) rather than use this one.
