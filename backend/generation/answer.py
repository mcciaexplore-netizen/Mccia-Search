"""Generate grounded answers with Gemini from retrieved chunks."""

import time
from functools import lru_cache

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_CHAT_MODEL
from retrieval.search import TOP_K, search

GENERATION_MODEL = GEMINI_CHAT_MODEL
MAX_CONTEXT_CHUNKS = TOP_K
MAX_OUTPUT_TOKENS = 2048

RETRY_DELAYS = (2, 4, 8)  # seconds before retry 1, 2 and 3
REQUEST_TIMEOUT_MS = 120_000

# What the model says, word for word, when the sources do not hold the answer.
DECLINE_ANSWER = "Not in Sampada archive"

SYSTEM_PROMPT = (
    "You are an expert on MCCIA (Mahratta Chamber of Commerce, Industries and "
    "Agriculture), Pune, and on Sampada, its monthly magazine published from "
    "1945 to 2017 in Marathi, Hindi and English. "
    "The magazine was originally called "
    "'मराठा चेंबर ऑफ कॉमर्स ॲण्ड इंडस्ट्रीजचें मासिक वृत्त' "
    "and later renamed Sampada.\n\n"
    "Rules:\n"
    "1. Answer ONLY from the numbered source blocks provided.\n"
    "2. NEVER use outside knowledge or memory, not even for simple facts you know "
    "(capitals, definitions, today's news): if the blocks do not state it, decline.\n"
    "3. NEVER guess names, dates, companies or figures.\n"
    "4. If the answer is not clearly stated in the sources, respond with exactly "
    f'"{DECLINE_ANSWER}" and nothing else. Read every block before deciding that.\n'
    "5. Always cite the exact source block numbers you used, each with its issue "
    "and page as written in the block's header, for example "
    '"[2] (1972-Jul To 1973-Jun.PDF, Page 45)".\n'
    "6. If asked about a specific year, only use sources from that year.\n"
    "7. Respond in the SAME language the user used to ask. Even if the sources are "
    "in Marathi and the question is in English, translate and answer in English.\n"
    "8. When the question is about what something is called or named, look for "
    "the exact Marathi or English name in the sources."
)

NOT_FOUND_ANSWER = (
    "माफ करा, संपदा मासिकाच्या उपलब्ध अंकांमध्ये या प्रश्नाचे उत्तर सापडले नाही. | "
    "Sorry, I could not find the answer in the available issues of "
    "Sampada magazine (1945–2017). "
    "Try asking in Marathi for better results on historical content."
)

_API_ERRORS = (genai_errors.APIError, httpx.HTTPError, OSError)


def build_prompt(query: str, chunks: list[dict]) -> str:
    """Format the chunks as numbered context blocks, followed by the question.

    Each block reads "[1] Source: file.pdf, Page 12" and then the chunk text.
    """
    blocks = []
    for number, chunk in enumerate(chunks, start=1):
        source = f"Source: {chunk['filename']}"
        if chunk.get("page_number") is not None:
            source += f", Page {chunk['page_number']}"
        blocks.append(f"[{number}] {source}\n{chunk['content']}")
    return "Context:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {query}"


def generate_answer(query: str, chunks: list[dict]) -> dict:
    """Answer the query from the chunks (best first) with Gemini.

    Returns {answer, sources, found}. With no chunks there is nothing to answer
    from, so a fixed "not found" message is returned without calling Gemini. When Gemini
    says the chunks do not hold the answer (DECLINE_ANSWER), found is False and there are
    no sources. At most MAX_CONTEXT_CHUNKS chunks are used, for the prompt and for the sources.
    Transient Gemini errors are retried after 2s, 4s and 8s; a RuntimeError is
    raised if the answer still cannot be generated.
    """
    if not chunks:
        return {"answer": NOT_FOUND_ANSWER, "sources": [], "found": False}

    used = chunks[:MAX_CONTEXT_CHUNKS]
    answer = _generate(build_prompt(query, used))
    if _is_decline(answer):  # the pages it was given do not answer it, so none are shown
        return {"answer": DECLINE_ANSWER, "sources": [], "found": False}
    return {"answer": answer, "sources": _build_sources(used), "found": True}


def rag(query: str, top_k: int | None = None) -> dict:
    """Search for the top_k most relevant chunks (default: search.TOP_K), then answer from them."""
    return generate_answer(query, search(query, TOP_K if top_k is None else top_k))


def _generate(prompt: str) -> str:
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )

    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            response = client.models.generate_content(
                model=GENERATION_MODEL, contents=prompt, config=config
            )
            break
        except _API_ERRORS as exc:
            if delay is None or not _is_retryable(exc):
                raise RuntimeError(f"Generating the answer failed: {_brief(exc)}") from exc
            print(
                f"[answer] Gemini error ({_brief(exc)}); "
                f"retry {attempt}/{len(RETRY_DELAYS)} in {delay}s"
            )
            time.sleep(delay)

    text = (response.text or "").strip()
    reason = _finish_reason(response)
    if not text:
        raise RuntimeError(f"Gemini returned an empty answer (finish reason: {reason})")
    if reason == types.FinishReason.MAX_TOKENS:
        print(f"[answer] WARNING: answer was cut off at {MAX_OUTPUT_TOKENS} tokens")
    return text


def _is_decline(answer: str) -> bool:
    """Whether the answer is only the decline sentence (quotes, case and a final full stop aside)."""
    return answer.strip().strip("\"'“”‘’ ").rstrip(".").lower() == DECLINE_ANSWER.rstrip(".").lower()


def _finish_reason(response: types.GenerateContentResponse) -> object:
    if response.prompt_feedback and response.prompt_feedback.block_reason:
        return f"prompt blocked: {response.prompt_feedback.block_reason}"
    return response.candidates[0].finish_reason if response.candidates else None


def _build_sources(chunks: list[dict]) -> list[dict]:
    """One source per (filename, page_number), keeping the highest similarity."""
    sources: dict[tuple, dict] = {}
    for chunk in chunks:
        key = (chunk["filename"], chunk.get("page_number"))
        source = sources.setdefault(
            key,
            {
                "filename": chunk["filename"],
                "page_number": chunk.get("page_number"),
                "document_id": chunk.get("document_id"),
                "similarity": chunk["similarity"],
            },
        )
        source["similarity"] = max(source["similarity"], chunk["similarity"])
    return list(sources.values())


@lru_cache(maxsize=1)
def _get_client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to .env "
            "(get one from https://aistudio.google.com)."
        )
    return genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
    )


def _is_retryable(exc: Exception) -> bool:
    """Retry network failures, rate limits and 5xx; fail fast on other API errors."""
    if isinstance(exc, genai_errors.APIError):
        return exc.code in (408, 429) or exc.code >= 500
    return True


def _brief(exc: Exception) -> str:
    return str(exc).replace("\n", " ")[:200]
