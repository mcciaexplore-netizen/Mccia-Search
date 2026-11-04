"""Generate grounded answers with Gemini from retrieved chunks."""

import json
import re
import string
import time
from dataclasses import dataclass
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


# Conversation memory. The browser sends the last few question and answer pairs. The step that works out what a follow-up refers to sees up to
# MAX_HISTORY_TURNS of them, so "both" and "that year" can reach back; the model that writes the answer sees only the last ANSWER_HISTORY_TURNS.
MAX_HISTORY_TURNS = 6
ANSWER_HISTORY_TURNS = 3
MAX_HISTORY_QUESTION_CHARS = 400
MAX_HISTORY_ANSWER_CHARS = 1200
OLDER_ANSWER_CHARS = 500  # every answer but the latest is cut to this when it is shown to the rewriting step
MAX_STANDALONE_CHARS = 600
MAX_RESOLVED_CHARS = 90

# What the bot says, word for word, when it cannot tell who or what a question refers to (instead of guessing).
CLARIFY_ANSWER = "Could you clarify who/what you are referring to?"

# Turns a vague follow-up ("Tell me about him") into one specific question that can be searched on its own. It answers in JSON so that
# what it worked out (who, which topic, which year) can be written to the audit log and checked.
CONDENSE_SYSTEM_PROMPT = """You turn the latest message of a conversation about the Sampada magazine archive (MCCIA, Pune, 1945 to 2017) into ONE fully specific, standalone question that a search engine can use without seeing the conversation.

STEP 1 - work out what the message refers to, using the conversation, newest part first:
- "him", "her", "his", "hers", "he", "she", "they" (one person) = the person the most recent exchange was about: the person its question asked about and its answer names. If that exchange compared several people, the one its answer names last.
- "it", "that", "this", "the topic" = the topic discussed most recently.
- "that year", "the same year", "the same period", "then" = the year discussed most recently.
- "both", "both of them", "the two", "them" (two people or things) = the two most recently discussed people or things, one from each of the last two exchanges about different people or things. Name BOTH.
- If the previous assistant message asked the user to clarify who or what they mean, the latest message is their reply: combine it with the question that was being clarified.

STEP 2 - write the question so that it is specific:
- Replace each pronoun with the full name exactly as the conversation gives it, and add the role and the year the conversation gives for that person, for example "Tell me about Shri A. B. Name, MCCIA president in 1972".
- Add the year or the topic when the message leaves it out and the conversation makes it clear.
- Keep the language of the latest message (English, Marathi or Hindi) and keep names as they are written.
- NEVER invent a name, role, year or fact: use only what the conversation states. Do not answer the question.
- If the message is already specific and complete, return it unchanged.

If you cannot tell who or what the message refers to (nobody or nothing in the conversation fits, or two candidates fit equally well and the message does not choose between them), do NOT guess: set "clarify" to true.

Examples (invented names). Conversation: Q: Who was MCCIA president in 1965? A: Shri P. Rao was MCCIA president in 1965 (1965 issue). Q: Who was president in 1972? A: Shri S. Mehta was MCCIA president in 1972 (1972 issue).
- "Tell me about him" -> "Tell me about Shri S. Mehta, MCCIA president in 1972"
- "What was his biggest achievement?" -> "What was Shri S. Mehta's biggest achievement as MCCIA president in 1972?"
- "Compare both of them" -> "Compare Shri P. Rao (MCCIA president in 1965) and Shri S. Mehta (MCCIA president in 1972)"
- "What happened that year?" -> "What happened in MCCIA in 1972?"
- "Who was president in 1980?" -> unchanged, it is already complete
- "Tell me about her" -> clarify is true, no woman is mentioned

Reply with JSON only, in this shape: {"person": "who the message refers to, in full, or null", "topic": "the topic it refers to, or null", "year": "the year it refers to, or null", "question": "the specific standalone question", "clarify": false}. When "clarify" is true, "question" is the latest message unchanged."""

_API_ERRORS = (genai_errors.APIError, httpx.HTTPError, OSError)


@dataclass(frozen=True)
class Restatement:
    """A question after the conversation has been used to make it specific.

    `question` is what to search (the question as asked when there was nothing to resolve). `clarify` is True when it could not be
    worked out who or what the question refers to, so the user is to be asked. `resolved` says what was worked out, for the audit log.
    """

    question: str
    clarify: bool = False
    resolved: str = ""


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


def _history_text(history: list[dict], older_answer_chars: int | None = None) -> str:
    """The pairs, oldest first. With `older_answer_chars`, every answer but the latest is cut to that length."""
    lines = []
    for index, turn in enumerate(history):
        answer = turn["answer"]
        if older_answer_chars is not None and index < len(history) - 1:
            answer = answer[:older_answer_chars]
        lines.append(f"Q: {turn['question']}\nA: {answer}")
    return "\n".join(lines)


def build_prompt(
    query: str,
    chunks: list[dict],
    chat_history: list[dict] | None = None,
    standalone_query: str | None = None,
    scope: YearScope | None = None,
) -> str:
    """Format the chunks as numbered context blocks, then the earlier conversation (if any), then the question.

    Each block reads "[1] Source: file.pdf, Page 12" and then the chunk text. The last ANSWER_HISTORY_TURNS pairs of the conversation are
    there so the model can tell what a follow-up refers to; it says in so many words that they are not a source. When the question was
    restated on its own (standalone_query), that is shown too. When it names a year (scope), a last note says the pages come from the
    issues that cover that year, which can run over two calendar years, so the answer keeps to what the pages state about the year asked.
    """
    blocks = []
    for number, chunk in enumerate(chunks, start=1):
        source = f"Source: {chunk['filename']}"
        if chunk.get("page_number") is not None:
            source += f", Page {chunk['page_number']}"
        blocks.append(f"[{number}] {source}\n{chunk['content']}")
    prompt = "Context:\n\n" + "\n\n".join(blocks)
    history = clean_history(chat_history)[-ANSWER_HISTORY_TURNS:]
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


# ---- a question that points at nothing (no conversation to resolve it from) ----------------------------------------------------------
_PUNCT = string.punctuation + "“”‘’…–—।॥"
_REFERENCE_WORDS = frozenset(
    """he him his himself she her hers herself they them their theirs both it its that this those these
    तो ती ते त्याला त्यांना त्याचे त्यांचे त्याच्याबद्दल त्यांच्याबद्दल त्याने त्यांनी तिचे तिच्याबद्दल दोघे दोघांची दोघांबद्दल
    वह वे उसके उनके उसे उन्हें उन्होंने दोनों""".split()
)
_FILLER_WORDS = frozenset(
    """tell me about more please give explain describe elaborate compare what who whom whose which how when where why did do does done is are
    was were be been being the a an of in on at to for from and or also else go on continue
    सांगा माहिती द्या अधिक आणखी काय कोण कोणी आहे आहेत होते होता होती मध्ये बद्दल
    बताइए बताओ और अधिक जानकारी क्या कौन बारे में से का की के को पर लिए है हैं""".split()
)


def is_dangling(query: str) -> bool:
    """Whether a question, asked with nothing before it, has no subject of its own: "Tell me about him", "Compare both", "Tell me more".

    True when everything in it is a pointing word ("him", "both", "that") or filler ("tell me", "what", "more"). A question with any
    other word in it, even one that also says "his", is left to be searched: there may be a subject inside it.
    """
    words = [w for w in (raw.strip(_PUNCT) for raw in query.lower().split()) if w]
    if not words:
        return False
    return not [w for w in words if len(w) > 1 and w not in _REFERENCE_WORDS and w not in _FILLER_WORDS]


# ---- making a follow-up specific -------------------------------------------------------------------------------------------------------
def restate_question(query: str, chat_history: list[dict] | None) -> Restatement:
    """The question made specific with the conversation: who "him" is, what "that" and "that year" mean, who "both" are.

    Without a conversation there is nothing to resolve; a question that points at nothing (is_dangling) is then marked for
    clarification, anything else is left as asked. With one, the model is asked for JSON (see CONDENSE_SYSTEM_PROMPT); if it cannot
    tell what the question refers to, it says so and the user is asked rather than a name being guessed. If the rewrite fails or gives
    something unusable, the question is searched as asked: a follow-up then searches badly, but it is never lost.
    """
    history = clean_history(chat_history)
    if not history:
        return Restatement(query, clarify=is_dangling(query))
    prompt = f"Conversation so far (oldest first):\n{_history_text(history, OLDER_ANSWER_CHARS)}\n\nLatest message: {query}"
    try:
        raw = _generate(prompt, system_instruction=CONDENSE_SYSTEM_PROMPT, max_output_tokens=400, temperature=0.0, json_output=True)
    except Exception as exc:  # noqa: BLE001 - a failed rewrite must not fail the question
        print(f"[answer] could not restate the follow-up ({type(exc).__name__}); searching the question as asked")
        return Restatement(query)
    return _read_restatement(raw, query)


def _read_restatement(raw: str, query: str) -> Restatement:
    data = _json_object(raw)
    if data is None:
        if raw.lstrip().startswith(("{", "[")):  # JSON that cannot be read is not a question
            return Restatement(query)
        return Restatement(_usable_question(raw, query))  # a plain one-line question is still usable
    if data.get("clarify") is True:
        return Restatement(query, clarify=True, resolved=_resolved(data))
    question = _usable_question(data.get("question"), query)
    return Restatement(question, resolved=_resolved(data) if question != query else "")


def _json_object(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.IGNORECASE)
    for candidate in (text, (re.search(r"\{.*\}", text, re.DOTALL) or [None])[0]):
        if candidate:
            try:
                value = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def _usable_question(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    question = re.sub(r"^(standalone question|question)\s*:\s*", "", value.strip(), flags=re.IGNORECASE).strip().strip("\"'“”‘’ ")
    if not question or len(question) > MAX_STANDALONE_CHARS or "\n" in question:
        return fallback
    return question


def _resolved(data: dict) -> str:
    """What the rewrite worked out, as one short line for the audit log: "who=Shri S. Mehta, MCCIA president in 1972; year=1972"."""
    parts = []
    for key, label in (("person", "who"), ("topic", "topic"), ("year", "year")):
        value = data.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() not in ("null", "none", "n/a"):
            parts.append(f"{label}={value.strip()[:MAX_RESOLVED_CHARS]}")
    return "; ".join(parts)


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

    With a conversation, a follow-up is first made specific (restate_question: who "him" is, what "that year" means), and that is what
    is searched; the answer is still given to the question as it was asked, with the conversation alongside. If it cannot be told who
    or what the question refers to, nothing is searched: the answer is CLARIFY_ANSWER and `clarify` is True. The result also carries
    `search_query` (the text that was searched, None when the user was asked to clarify) and `resolved` (what was worked out).

    A question that names a year is searched in the issues that cover that year only. If nothing is found there the answer says
    so for that year (no_year_answer) and no other year is tried; otherwise the answer ends with the line saying which issues were
    searched (searched_in). A question that names no year is searched across the whole archive, as always.
    """
    history = clean_history(chat_history)
    restated = restate_question(query, history)
    if restated.clarify:
        return {"answer": CLARIFY_ANSWER, "sources": [], "found": False, "clarify": True, "search_query": None, "resolved": restated.resolved}
    search_query = restated.question
    scope = year_scope(search_query)
    chunks = search(search_query, TOP_K if top_k is None else top_k, scope)
    if scope.years and not chunks:
        return {"answer": no_year_answer(scope.years), "sources": [], "found": False, "clarify": False, "search_query": search_query, "resolved": restated.resolved}
    result = generate_answer(query, chunks, history, search_query, scope)
    if scope.years:
        result["answer"] += f"\n\n{searched_in(scope)}"
    result.update({"clarify": False, "search_query": search_query, "resolved": restated.resolved})
    return result


def _generate(
    prompt: str,
    system_instruction: str = SYSTEM_PROMPT,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    temperature: float | None = None,
    json_output: bool = False,
) -> str:
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        response_mime_type="application/json" if json_output else None,
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
