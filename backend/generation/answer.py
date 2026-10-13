"""Generate grounded answers with Gemini from retrieved chunks."""

import time
from functools import lru_cache

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_CHAT_MODEL
from retrieval.search import search

GENERATION_MODEL = GEMINI_CHAT_MODEL
MAX_CONTEXT_CHUNKS = 5
MAX_OUTPUT_TOKENS = 2048

RETRY_DELAYS = (2, 4, 8)  # seconds before retry 1, 2 and 3
REQUEST_TIMEOUT_MS = 120_000

SYSTEM_PROMPT = (
    "You are a helpful assistant for MCCIA (Mahratta Chamber of Commerce, "
    "Industries and Agriculture), Pune. "
    "Answer the question using ONLY the provided context. "
    "Detect the language of the question and respond in that same language — "
    "Marathi, Hindi, or English. "
    "Always cite your sources by mentioning the document name and page number "
    "at the end of your answer. "
    "If the context does not contain enough information to answer, say so honestly."
)

NOT_FOUND_ANSWER = (
    "मला या प्रश्नाचे उत्तर सापडले नाही. "
    "I could not find an answer to your question in the available documents."
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
    from, so a fixed "not found" message is returned without calling Gemini. At
    most MAX_CONTEXT_CHUNKS chunks are used, for the prompt and for the sources.
    Transient Gemini errors are retried after 2s, 4s and 8s; a RuntimeError is
    raised if the answer still cannot be generated.
    """
    if not chunks:
        return {"answer": NOT_FOUND_ANSWER, "sources": [], "found": False}

    used = chunks[:MAX_CONTEXT_CHUNKS]
    answer = _generate(build_prompt(query, used))
    return {"answer": answer, "sources": _build_sources(used), "found": True}


def rag(query: str, top_k: int = 5) -> dict:
    """Search for the top_k most relevant chunks, then answer the query from them."""
    return generate_answer(query, search(query, top_k))


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
