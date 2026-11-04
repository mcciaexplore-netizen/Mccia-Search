"""Generate grounded answers with Gemini from retrieved chunks."""

import re
import time
from functools import lru_cache

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_CHAT_MODEL
from retrieval.search import TOP_K, YearScope, search, year_scope

GENERATION_MODEL = GEMINI_CHAT_MODEL
MAX_CONTEXT_CHUNKS = TOP_K
MAX_OUTPUT_TOKENS = 2048

RETRY_DELAYS = (2, 4, 8)  # seconds before retry 1, 2 and 3
REQUEST_TIMEOUT_MS = 120_000

# What the model says, word for word, when the sources do not hold the answer.
DECLINE_ANSWER = (
    "This specific information is not found in the Sampada archive. "
    "Please check the original magazine issues."
)

SYSTEM_PROMPT = (
    "You are a Sampada magazine expert. You have deep knowledge of MCCIA\n"
    "history from 1945 to 2017 through the Sampada magazine archive.\n\n"
    "STRICT RULES:\n"
    "- Answer ONLY from the numbered source blocks provided below\n"
    "- NEVER use outside knowledge or memory\n"
    "- NEVER guess or assume any name, date, figure or fact\n"
    "- If the answer is not clearly stated in the sources, say exactly:\n"
    f"  '{DECLINE_ANSWER}'\n"
    "- Always mention which year/issue the information comes from\n"
    "- If multiple sources contradict each other, mention both and let\n"
    "  the user decide\n"
    "- Answer in the same language as the question (English/Marathi/Hindi)\n"
    "- Be specific - exact names, exact years, exact figures from sources only"
)

NOT_FOUND_ANSWER = (
    "माफ करा, संपदा मासिकाच्या उपलब्ध अंकांमध्ये या प्रश्नाचे उत्तर सापडले नाही. | "
    "Sorry, I could not find the answer in the available issues of "
    "Sampada magazine (1945–2017). "
    "Try asking in Marathi for better results on historical content."
)

# A question that names a year is answered from the issues that cover that year and nothing else. When there is nothing to find there, it
# says so for that year, and does not go looking in other years.
ARCHIVE_YEARS = "1945-2017"
NO_YEAR_ANSWER = "No information found for {years} in the Sampada archive. Available years are " + ARCHIVE_YEARS + "."


def _list_of(items) -> str:
    """"a", "a and b", "a, b and c"."""
    items = [str(item) for item in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def no_year_answer(years) -> str:
    """The answer when a question names a year and the issues that cover it hold nothing for it."""
    return NO_YEAR_ANSWER.format(years=_list_of(years))


def searched_in(scope: YearScope) -> str:
    """The line that tells which issues a year question was searched in, e.g. "Searched in: 1964-65 and 1965-66 Sampada issues"."""
    return f"Searched in: {_list_of(scope.issues)} Sampada issues"


# Conversation memory: only the last few question and answer pairs are used, and each is cut to a length that keeps the prompt small.
MAX_HISTORY_TURNS = 3
MAX_HISTORY_QUESTION_CHARS = 400
MAX_HISTORY_ANSWER_CHARS = 1200
MAX_STANDALONE_CHARS = 600

# Used to turn a follow-up ("Tell me more about him") into a question that can be searched on its own.
CONDENSE_SYSTEM_PROMPT = (
    "You rewrite the latest question of a conversation as one complete, standalone question that can be used to search a magazine "
    "archive. Use the earlier conversation only to fill in what the latest question leaves out, such as who 'he', 'she', 'it' or "
    "'they' is, or which year or topic it means. Keep the language of the latest question. Do not answer the question, do not add "
    "facts that the conversation does not state, and do not change what is being asked. If the latest question is already "
    "complete, return it unchanged. Reply with the question only."
)

_API_ERRORS = (genai_errors.APIError, httpx.HTTPError, OSError)


def clean_history(chat_history: list[dict] | None) -> list[dict]:
    """The last MAX_HISTORY_TURNS question and answer pairs, as {"question", "answer"} with text cut to a sensible length.

    Pairs that are not two non-empty strings are dropped. The history comes from the browser, so it is only ever used to
    understand what a question refers to, never as a source of facts.
    """
    pairs = []
    for turn in chat_history or []:
        question, answer = (turn.get("question"), turn.get("answer")) if isinstance(turn, dict) else (None, None)
        if isinstance(question, str) and isinstance(answer, str) and question.strip() and answer.strip():
            pairs.append({"question": question.strip()[:MAX_HISTORY_QUESTION_CHARS], "answer": answer.strip()[:MAX_HISTORY_ANSWER_CHARS]})
    return pairs[-MAX_HISTORY_TURNS:]


def _history_text(history: list[dict]) -> str:
    return "\n".join(f"Q: {turn['question']}\nA: {turn['answer']}" for turn in history)


def build_prompt(
    query: str,
    chunks: list[dict],
    chat_history: list[dict] | None = None,
    standalone_query: str | None = None,
    scope: YearScope | None = None,
) -> str:
    """Format the chunks as numbered context blocks, then the earlier conversation (if any), then the question.

    Each block reads "[1] Source: file.pdf, Page 12" and then the chunk text. The conversation is there so the model can tell
    what a follow-up refers to; it says in so many words that it is not a source. When the question was restated on its own
    (standalone_query), that is shown too. When it names a year (scope), a last note says the pages come from the issues that cover
    that year, which can run over two calendar years, so the answer keeps to what the pages state about the year asked.
    """
    blocks = []
    for number, chunk in enumerate(chunks, start=1):
        source = f"Source: {chunk['filename']}"
        if chunk.get("page_number") is not None:
            source += f", Page {chunk['page_number']}"
        blocks.append(f"[{number}] {source}\n{chunk['content']}")
    prompt = "Context:\n\n" + "\n\n".join(blocks)
    history = clean_history(chat_history)
    if history:
        prompt += (
            "\n\nEarlier in this conversation (only to understand what the question refers to; it is NOT a source, "
            "so do not take facts from it):\n" + _history_text(history)
        )
    prompt += f"\n\nQuestion: {query}"
    if standalone_query and standalone_query.strip() != query.strip():
        prompt += f"\n(The same question, restated on its own: {standalone_query.strip()})"
    if scope is not None and scope.years:
        years = _list_of(scope.years)
        prompt += (
            f"\n(The question is about {years}. The pages above come from the issues that cover it ({_list_of(scope.issues)}). An issue can run "
            f"over two calendar years, so a page may describe the year before or after: use only what the pages state about {years}, and "
            "if they show a different year, say so.)"
        )
    return prompt


def condense_question(query: str, chat_history: list[dict] | None) -> str:
    """The question as a standalone one, using the earlier conversation to resolve "him", "that year" and the like.

    Without history the question is returned as it is. If rewriting fails or gives something unusable, so is the question as
    asked: a follow-up then searches badly, but it is never lost.
    """
    history = clean_history(chat_history)
    if not history:
        return query
    prompt = f"Conversation so far:\n{_history_text(history)}\n\nLatest question: {query}\n\nStandalone question:"
    try:
        rewritten = _generate(prompt, system_instruction=CONDENSE_SYSTEM_PROMPT, max_output_tokens=256)
    except Exception as exc:  # noqa: BLE001 - a failed rewrite must not fail the question
        print(f"[answer] could not restate the follow-up ({type(exc).__name__}); searching the question as asked")
        return query
    rewritten = re.sub(r"^(standalone question|question)\s*:\s*", "", rewritten.strip(), flags=re.IGNORECASE).strip().strip("\"'“”‘’ ")
    if not rewritten or len(rewritten) > MAX_STANDALONE_CHARS or "\n" in rewritten:
        return query
    return rewritten


def generate_answer(
    query: str,
    chunks: list[dict],
    chat_history: list[dict] | None = None,
    standalone_query: str | None = None,
    scope: YearScope | None = None,
) -> dict:
    """Answer the query from the chunks (best first) with Gemini.

    Returns {answer, sources, found}. With no chunks there is nothing to answer
    from, so a fixed "not found" message is returned without calling Gemini. When Gemini
    says the chunks do not hold the answer (DECLINE_ANSWER), found is False and there are
    no sources. At most MAX_CONTEXT_CHUNKS chunks are used, for the prompt and for the sources.
    `chat_history` (the last few {"question", "answer"} pairs) lets a follow-up be understood; `scope` is the year the question names.
    Transient Gemini errors are retried after 2s, 4s and 8s; a RuntimeError is
    raised if the answer still cannot be generated.
    """
    if not chunks:
        return {"answer": NOT_FOUND_ANSWER, "sources": [], "found": False}

    used = chunks[:MAX_CONTEXT_CHUNKS]
    answer = _generate(build_prompt(query, used, chat_history, standalone_query, scope))
    if _is_decline(answer):  # the pages it was given do not answer it, so none are shown
        return {"answer": DECLINE_ANSWER, "sources": [], "found": False}
    return {"answer": answer, "sources": _build_sources(used), "found": True}


def rag(query: str, top_k: int | None = None, chat_history: list[dict] | None = None) -> dict:
    """Search for the top_k most relevant chunks (default: search.TOP_K), then answer from them.

    With a conversation, a follow-up is first restated as a standalone question, which is what is searched; the answer is
    still given to the question as it was asked, with the conversation alongside. The result also carries `search_query`,
    the text that was searched.

    A question that names a year is searched in the issues that cover that year only. If nothing is found there the answer says
    so for that year (no_year_answer) and no other year is tried; otherwise the answer ends with the line saying which issues were
    searched (searched_in). A question that names no year is searched across the whole archive, as always.
    """
    history = clean_history(chat_history)
    search_query = condense_question(query, history) if history else query
    scope = year_scope(search_query)
    chunks = search(search_query, TOP_K if top_k is None else top_k, scope)
    if scope.years and not chunks:
        return {"answer": no_year_answer(scope.years), "sources": [], "found": False, "search_query": search_query}
    result = generate_answer(query, chunks, history, search_query, scope)
    if scope.years:
        result["answer"] += f"\n\n{searched_in(scope)}"
    result["search_query"] = search_query
    return result


def _generate(prompt: str, system_instruction: str = SYSTEM_PROMPT, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=max_output_tokens,
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
        print(f"[answer] WARNING: answer was cut off at {max_output_tokens} tokens")
    return text


def _letters(text: str) -> str:
    """The text with everything but letters and digits removed, lower-cased: quotes, spacing and punctuation do not matter."""
    return re.sub(r"[\W_]+", "", text.lower())


_DECLINE_LETTERS = _letters(DECLINE_ANSWER)


def _is_decline(answer: str) -> bool:
    """Whether the answer is the decline sentence (quotes, case, spacing and punctuation aside), and not much besides.

    A little slack is allowed for a stray word, but an answer that holds the sentence together with real content (a partial
    answer) is not a decline: its sources are worth showing.
    """
    letters = _letters(answer)
    return letters.startswith(_DECLINE_LETTERS) and len(letters) <= len(_DECLINE_LETTERS) + 20


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
