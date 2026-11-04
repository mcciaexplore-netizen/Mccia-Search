"""The answer side: the strict prompt, spotting a decline, conversation memory, and restating a follow-up before it is searched.

No database, no network, no Gemini: the model is replaced by a fake that records what it was asked.
"""

import pytest

from generation import answer
from generation.answer import (
    CONDENSE_SYSTEM_PROMPT,
    DECLINE_ANSWER,
    NOT_FOUND_ANSWER,
    SYSTEM_PROMPT,
    _is_decline,
    build_prompt,
    clean_history,
    condense_question,
    generate_answer,
    rag,
)

CHUNKS = [
    {"filename": "1965 Jan To1965 Dec.pdf", "page_number": 12, "document_id": 1, "content": "Shri A was elected president in 1965.", "similarity": 0.71},
    {"filename": "1965 Jan To1965 Dec.pdf", "page_number": 12, "document_id": 1, "content": "More about the election.", "similarity": 0.60},
    {"filename": "1966 Jan.pdf", "page_number": None, "document_id": 2, "content": "A later note.", "similarity": 0.55},
]
HISTORY = [{"question": "Who was MCCIA president in 1965?", "answer": "Shri A, according to the 1965 issue."}]


class FakeModel:
    """Stands in for answer._generate: records every call and answers from a script (by which system prompt was used)."""

    def __init__(self, main="Shri A (1965 issue).", condensed="Tell me more about Shri A, MCCIA president in 1965."):
        self.calls, self.main, self.condensed = [], main, condensed

    def __call__(self, prompt, system_instruction=SYSTEM_PROMPT, max_output_tokens=2048):
        self.calls.append({"prompt": prompt, "system": system_instruction, "max": max_output_tokens})
        if system_instruction == CONDENSE_SYSTEM_PROMPT:
            if isinstance(self.condensed, Exception):
                raise self.condensed
            return self.condensed
        return self.main


@pytest.fixture
def model(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(answer, "_generate", fake)
    return fake


# ---- the prompt ---------------------------------------------------------------------------------------------------------------

def test_the_system_prompt_is_the_strict_one_word_for_word():
    for line in [
        "You are a Sampada magazine expert. You have deep knowledge of MCCIA",
        "history from 1945 to 2017 through the Sampada magazine archive.",
        "STRICT RULES:",
        "- Answer ONLY from the numbered source blocks provided below",
        "- NEVER use outside knowledge or memory",
        "- NEVER guess or assume any name, date, figure or fact",
        "- If the answer is not clearly stated in the sources, say exactly:",
        "  'This specific information is not found in the Sampada archive. Please check the original magazine issues.'",
        "- Always mention which year/issue the information comes from",
        "- If multiple sources contradict each other, mention both and let",
        "  the user decide",
        "- Answer in the same language as the question (English/Marathi/Hindi)",
        "- Be specific - exact names, exact years, exact figures from sources only",
    ]:
        assert line in SYSTEM_PROMPT.split("\n"), line


def test_the_decline_sentence_is_the_one_the_prompt_asks_for():
    assert DECLINE_ANSWER == "This specific information is not found in the Sampada archive. Please check the original magazine issues."
    assert f"'{DECLINE_ANSWER}'" in SYSTEM_PROMPT


# ---- spotting a decline ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        DECLINE_ANSWER,
        f"'{DECLINE_ANSWER}'",
        f'"{DECLINE_ANSWER}"',
        DECLINE_ANSWER.lower(),
        DECLINE_ANSWER.upper(),
        DECLINE_ANSWER.replace(". ", ".\n  "),  # wrapped over two lines, as the prompt shows it
        "  " + DECLINE_ANSWER + "\n",
        DECLINE_ANSWER.rstrip("."),
        DECLINE_ANSWER + " Sorry.",
    ],
)
def test_the_decline_sentence_is_a_decline_however_it_is_dressed(text):
    assert _is_decline(text)


@pytest.mark.parametrize(
    "text",
    [
        "Shri A was president in 1965 (1965 issue).",
        "In 1965 the president was Shri A. " + DECLINE_ANSWER,  # a partial answer keeps its sources
        DECLINE_ANSWER + " However, the 1965 issue says Shri A was elected president at the annual meeting.",
        "Not in Sampada archive",
        "",
    ],
)
def test_a_real_or_partial_answer_is_not_a_decline(text):
    assert not _is_decline(text)


def test_a_decline_drops_the_sources(model):
    model.main = DECLINE_ANSWER
    result = generate_answer("Capital of France?", CHUNKS)
    assert result == {"answer": DECLINE_ANSWER, "sources": [], "found": False}


def test_an_answer_keeps_one_source_per_page_with_the_best_similarity(model):
    result = generate_answer("Who was president?", CHUNKS)
    assert result["found"] is True
    assert result["sources"] == [
        {"filename": "1965 Jan To1965 Dec.pdf", "page_number": 12, "document_id": 1, "similarity": 0.71},
        {"filename": "1966 Jan.pdf", "page_number": None, "document_id": 2, "similarity": 0.55},
    ]


def test_with_no_chunks_nothing_is_asked_of_the_model(model):
    assert generate_answer("anything", []) == {"answer": NOT_FOUND_ANSWER, "sources": [], "found": False}
    assert model.calls == []


# ---- conversation memory --------------------------------------------------------------------------------------------------------

def test_only_the_last_three_pairs_are_kept_and_long_text_is_cut():
    pairs = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(6)]
    assert [p["question"] for p in clean_history(pairs)] == ["q3", "q4", "q5"]
    cut = clean_history([{"question": "q" * 5000, "answer": "a" * 5000}])[0]
    assert (len(cut["question"]), len(cut["answer"])) == (answer.MAX_HISTORY_QUESTION_CHARS, answer.MAX_HISTORY_ANSWER_CHARS)


@pytest.mark.parametrize("bad", [None, [], "text", [None], [{"question": "q"}], [{"question": "", "answer": "a"}], [{"question": 1, "answer": 2}], [["q", "a"]]])
def test_history_that_is_not_pairs_of_text_is_ignored(bad):
    assert clean_history(bad) == []


def test_the_prompt_has_numbered_blocks_then_the_conversation_then_the_question():
    prompt = build_prompt("Tell me more about him", CHUNKS, HISTORY, "Tell me more about Shri A")
    assert prompt.startswith("Context:\n\n[1] Source: 1965 Jan To1965 Dec.pdf, Page 12\nShri A was elected president in 1965.")
    assert "[3] Source: 1966 Jan.pdf\nA later note." in prompt  # no page number: no ", Page"
    assert prompt.index("Earlier in this conversation") < prompt.index("Question: Tell me more about him")
    assert "it is NOT a source" in prompt
    assert "Q: Who was MCCIA president in 1965?\nA: Shri A, according to the 1965 issue." in prompt
    assert prompt.endswith("(The same question, restated on its own: Tell me more about Shri A)")


def test_without_a_conversation_the_prompt_is_as_before():
    prompt = build_prompt("Who was president?", CHUNKS)
    assert "Earlier in this conversation" not in prompt and "restated" not in prompt
    assert prompt.endswith("Question: Who was president?")


def test_a_restatement_equal_to_the_question_is_not_repeated():
    assert "restated" not in build_prompt("Who was president?", CHUNKS, HISTORY, " Who was president? ")


# ---- restating a follow-up ------------------------------------------------------------------------------------------------------

def test_without_history_the_question_is_left_alone_and_the_model_is_not_asked(model):
    assert condense_question("Who was president?", None) == "Who was president?"
    assert condense_question("Who was president?", []) == "Who was president?"
    assert model.calls == []


def test_a_follow_up_is_restated_using_the_conversation(model):
    assert condense_question("Tell me more about him", HISTORY) == "Tell me more about Shri A, MCCIA president in 1965."
    call = model.calls[0]
    assert call["system"] == CONDENSE_SYSTEM_PROMPT
    assert "Q: Who was MCCIA president in 1965?" in call["prompt"] and "Latest question: Tell me more about him" in call["prompt"]
    assert call["max"] <= 256


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Tell me more about Shri A."', "Tell me more about Shri A."),
        ("Standalone question: Tell me more about Shri A.", "Tell me more about Shri A."),
        ("Question: 'Tell me more about Shri A.'", "Tell me more about Shri A."),
    ],
)
def test_a_restatement_is_tidied(model, raw, expected):
    model.condensed = raw
    assert condense_question("Tell me more about him", HISTORY) == expected


@pytest.mark.parametrize("bad", ["", "   ", "line one\nline two", "x" * 700])
def test_an_unusable_restatement_falls_back_to_the_question(model, bad):
    model.condensed = bad
    assert condense_question("Tell me more about him", HISTORY) == "Tell me more about him"


def test_a_failed_restatement_never_loses_the_question(model, capsys):
    model.condensed = RuntimeError("Gemini is down")
    assert condense_question("Tell me more about him", HISTORY) == "Tell me more about him"
    assert "searching the question as asked" in capsys.readouterr().out


# ---- the whole flow -------------------------------------------------------------------------------------------------------------

def test_a_question_without_history_is_searched_as_asked(model, monkeypatch):
    searched = []
    monkeypatch.setattr(answer, "search", lambda q, k: searched.append((q, k)) or CHUNKS)
    result = rag("Who was MCCIA president in 1965?")
    assert searched == [("Who was MCCIA president in 1965?", answer.TOP_K)]
    assert [c["system"] for c in model.calls] == [SYSTEM_PROMPT]  # one call: the answer
    assert result["search_query"] == "Who was MCCIA president in 1965?" and result["found"] is True


def test_a_follow_up_is_searched_restated_and_answered_with_the_conversation(model, monkeypatch):
    searched = []
    monkeypatch.setattr(answer, "search", lambda q, k: searched.append(q) or CHUNKS)
    result = rag("Tell me more about him", None, HISTORY)
    assert searched == ["Tell me more about Shri A, MCCIA president in 1965."]  # the year and the person are back in the search
    final = model.calls[-1]
    assert final["system"] == SYSTEM_PROMPT
    assert "Question: Tell me more about him" in final["prompt"] and "restated on its own: Tell me more about Shri A" in final["prompt"]
    assert "Q: Who was MCCIA president in 1965?" in final["prompt"]
    assert result["search_query"] == "Tell me more about Shri A, MCCIA president in 1965."


def test_history_is_trimmed_before_it_reaches_the_model(model, monkeypatch):
    monkeypatch.setattr(answer, "search", lambda q, k: CHUNKS)
    rag("And then?", None, [{"question": f"old question {i}", "answer": f"old answer {i}"} for i in range(8)])
    prompt = model.calls[0]["prompt"]
    assert "old question 4" not in prompt and all(f"old question {i}" in prompt for i in (5, 6, 7))


def test_a_follow_up_when_the_restatement_fails_is_still_answered(model, monkeypatch):
    model.condensed = RuntimeError("down")
    searched = []
    monkeypatch.setattr(answer, "search", lambda q, k: searched.append(q) or CHUNKS)
    result = rag("Tell me more about him", None, HISTORY)
    assert searched == ["Tell me more about him"] and result["found"] is True
