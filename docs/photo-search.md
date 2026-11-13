# Photo search and photographs in the chat

Built 13 Nov 2026 on top of the photo extraction (`backend/photos/`, the `photo_index` table and the private bucket `sampada-images`).

## What a person sees

Asking for photographs in the chat ("show photo of Kirloskar", "photos of the Pune Expo 2010", "किर्लोस्करांचा फोटो दाखवा") gives the usual answer and, under it, up to 5 photographs: the
picture, its printed caption, and "Page 214 · Sampada 1961-62". Clicking one shows it large, with a link to the scanned page. An ordinary question never searches photographs.
The words that make a question a photo request are in `photos/query.py` (English: photo, picture, image, portrait...; Marathi/Hindi: फोटो, छायाचित्र, तस्वीर...; not "चित्रपट", which is a film).

## How photographs are found (`photos/search.py`)

Three signals, each 0 to 1, combined as `0.45 caption + 0.30 page + 0.25 picture`:

- **caption**: the share of the request found in the caption printed beside the photograph. A name typed in Latin letters ("Kirloskar") is matched against the Devanagari caption through
  spellings Gemini supplies once per request (and caches); a full name scores 1, a surname alone about half.
- **page**: how well the chat's own text search finds the page the photograph is on, so a photograph is found through the article about the name even without a caption.
- **picture**: CLIP's text side (a 250 MB model on the CPU) compared with the 512-number vector stored for each photograph. This finds concepts ("people at a dinner"); it cannot read names,
  which is why the other two exist (measured: "Laxmanrao Kirloskar" ranked a different man's portrait first on CLIP alone).

A photograph is shown only if one signal is clearly there (caption at least half, or a clear page match, or CLIP similarity at least 0.26): nothing is better than the least bad. A request that
names a year is searched in the issues that cover that year only, never in others (same rule as the text search).

## The API (backend; the web app reaches it through its own `/api` routes)

- `POST /search/photos` `{"query": "photo of Kirloskar", "top_k": 5}` (user API key, X-User-Id): `{"searched_for": "Kirloskar", "photos": [{id, document_id, page_number, filename, year, caption, kind, contains_face, score, signed_url}]}`.
  `signed_url` is a link that works for an hour.
- `GET /photos/{id}/image` (user API key): the JPEG, read from private storage, cached by the browser for a day. The web app shows photographs through this (as `/api/photos/{id}`), so a chat
  saved in the browser keeps its pictures: no link in it ever expires.
- `POST /chat` also returns `photos` (empty unless the question asked for photographs). Photo search can never cost a chat its answer: if it fails or takes over 12 s the chat is answered without photographs.

## What it needs on the server

`SUPABASE_SERVICE_ROLE_KEY` in the backend's `.env` (the project address is worked out from `DATABASE_URL`), and the CLIP model files in `~/hf-models` (the pipeline put them there). Without the key, photo
search is off and nothing else is affected. The image is built from `deploy/Dockerfile.bge` (CPU torch) and swapped in with `deploy/live-switch.sh` (`prepare`, `switch`, `rollback`).

## Known limits

- **kind** (photo / advertisement / illustration) is noisy: event photos of stalls are often called advertisements. It is returned but not shown. It can be recomputed from the stored vectors.
- Captions sometimes include the first lines of the article below the photograph.
- Photographs appear as the extraction reaches their issue (it runs oldest first).
