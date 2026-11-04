"""The answer side: the strict prompt, spotting a decline, conversation memory, and restating a follow-up before it is searched.

No database, no network, no Gemini: the model is replaced by a fake that records what it was asked.
"""

import json
import re

import pytest

from generation import answer
from retrieval.search import YearScope
from generation.answer import (
    Restatement,
    CLARIFY_ANSWER,
    CONDENSE_SYSTEM_PROMPT,
    DECLINE_ANSWER,
    NOT_FOUND_ANSWER,
    SYSTEM_PROMPT,
    _is_decline,
    no_year_answer,
    build_prompt,
    clean_history,
    generate_answer,
    is_dangling,
    rag,
    restate_question,
    searched_in,
)

CHUNKS = [
    {"filename": "1965 Jan To1965 Dec.pdf", "page_number": 12, "document_id": 1, "content": "Shri A was elected president in 1965.", "similarity": 0.71},
    {"filename": "1965 Jan To1965 Dec.pdf", "page_number": 12, "document_id": 1, "content": "More about the election.", "similarity": 0.60},
    {"filename": "1966 Jan.pdf", "page_number": None, "document_id": 2, "content": "A later note.", "similarity": 0.55},
]
HISTORY = [{"question": "Who was MCCIA president in 1965?", "answer": "Shri A, according to the 1965 issue."}]


def restated(question="Tell me more about Shri A, MCCIA president in 1965.", clarify=False, person="Shri A, MCCIA president in 1965", year="1965"):
    """What the rewriting step's model answers: JSON, as CONDENSE_SYSTEM_PROMPT asks."""
    return json.dumps({"person": person, "topic": None, "year": year, "question": question, "clarify": clarify})


class FakeModel:
    """Stands in for answer._generate: records every call and answers from a script (by which system prompt was used)."""

    def __init__(self, main="Shri A (1965 issue).", condensed=None):
        self.calls, self.main = [], main
        self.condensed = restated() if condensed is None else condensed  # a string, an Exception, or a function of the prompt

    def __call__(self, prompt, system_instruction=SYSTEM_PROMPT, max_output_tokens=2048, temperature=None, json_output=False):
        self.calls.append({"prompt": prompt, "system": system_instruction, "max": max_output_tokens, "temperature": temperature, "json": json_output})
        if system_instruction == CONDENSE_SYSTEM_PROMPT:
            if isinstance(self.condensed, Exception):
                raise self.condensed
            return self.condensed(prompt) if callable(self.condensed) else self.condensed
        return self.main


@pytest.fixture(autouse=True)
def no_year(monkeypatch):
    """By default a question names no year, and no database is asked which issues cover one."""
    monkeypatch.setattr(answer, "year_scope", lambda query: YearScope())


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

def test_the_last_six_pairs_are_kept_and_long_text_is_cut():
    pairs = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(9)]
    assert [p["question"] for p in clean_history(pairs)] == [f"q{i}" for i in range(3, 9)]
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


def test_the_model_that_answers_sees_only_the_last_three_pairs():
    pairs = [{"question": f"question {i}", "answer": f"answer {i}"} for i in range(6)]
    prompt = build_prompt("And then?", CHUNKS, pairs)
    assert all(f"question {i}" not in prompt for i in (0, 1, 2)) and all(f"question {i}" in prompt for i in (3, 4, 5))


def test_without_a_conversation_the_prompt_is_as_before():
    prompt = build_prompt("Who was president?", CHUNKS)
    assert "Earlier in this conversation" not in prompt and "restated" not in prompt
    assert prompt.endswith("Question: Who was president?")


def test_a_restatement_equal_to_the_question_is_not_repeated():
    assert "restated" not in build_prompt("Who was president?", CHUNKS, HISTORY, " Who was president? ")


# ---- a question that points at nothing ------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question",
    ["Tell me about him", "Tell me about her.", "Compare both", "Compare both of them", "Tell me more", "Go on", "What else did he do?", "Who is he?",
     "त्यांच्याबद्दल सांगा", "उनके बारे में बताइए"],
)
def test_a_question_with_nothing_but_pointing_words_and_filler_has_no_subject(question):
    assert is_dangling(question)


@pytest.mark.parametrize(
    "question",
    ["Who was MCCIA president in 1965?", "What is MCCIA?", "MCCIA?", "Tell me about the 1972 president", "What was his biggest achievement?",
     "How are you?", "Thanks", "Hello", "", "   ", "?"],
)
def test_a_question_with_a_word_of_its_own_is_left_to_be_searched(question):
    assert not is_dangling(question)


# ---- making a follow-up specific --------------------------------------------------------------------------------------------------

def test_without_history_a_question_is_left_alone_and_the_model_is_not_asked(model):
    assert restate_question("Who was president?", None) == Restatement("Who was president?")
    assert restate_question("Who was president?", []) == Restatement("Who was president?")
    assert model.calls == []


def test_without_history_a_question_that_points_at_nothing_is_marked_for_clarification(model):
    assert restate_question("Tell me about him", None) == Restatement("Tell me about him", clarify=True)
    assert model.calls == []  # nothing to ask the model about: there is no conversation


def test_a_follow_up_is_made_specific_using_the_conversation(model):
    result = restate_question("Tell me more about him", HISTORY)
    assert result == Restatement("Tell me more about Shri A, MCCIA president in 1965.", resolved="who=Shri A, MCCIA president in 1965; year=1965")
    call = model.calls[0]
    assert call["system"] == CONDENSE_SYSTEM_PROMPT and call["json"] is True and call["temperature"] == 0.0
    assert "Q: Who was MCCIA president in 1965?" in call["prompt"] and "Latest message: Tell me more about him" in call["prompt"]
    assert call["max"] <= 400


def test_the_rewriting_prompt_teaches_who_him_and_that_year_mean_and_when_to_ask():
    for fragment in [
        '"him", "her", "his"', "the person the most recent exchange was about", "the one its answer names last",  # who "him" is
        '"it", "that", "this"', "the topic discussed most recently",  # what "that" is
        '"that year", "the same year"', "the year discussed most recently",  # which year
        '"both", "both of them"', "Name BOTH",  # the two people
        "Replace each pronoun with the full name", "the role and the year the conversation gives",  # be specific
        "NEVER invent a name", 'do NOT guess: set "clarify" to true',  # never guess
        "Tell me about Shri S. Mehta, MCCIA president in 1972", "Compare Shri P. Rao (MCCIA president in 1965) and Shri S. Mehta (MCCIA president in 1972)",
    ]:
        assert fragment in CONDENSE_SYSTEM_PROMPT, fragment


def test_the_rewriting_step_sees_the_whole_conversation_oldest_first_with_older_answers_cut_short(model):
    pairs = [{"question": f"question {i}", "answer": f"answer {i} " + "x" * 900} for i in range(3)]
    restate_question("Compare both", pairs)
    prompt = model.calls[0]["prompt"]
    assert prompt.index("question 0") < prompt.index("question 1") < prompt.index("question 2") < prompt.index("Latest message: Compare both")
    assert sorted(len(run) for run in re.findall(r"x+", prompt)) == [491, 491, 900]  # the older two answers are cut to 500 characters; the latest is whole


def test_a_question_that_is_already_specific_keeps_an_empty_resolved_line(model):
    model.condensed = restated(question="Who was president in 1980?", person=None, year="1980")
    assert restate_question("Who was president in 1980?", HISTORY) == Restatement("Who was president in 1980?")


def test_a_json_answer_in_a_code_fence_is_read(model):
    model.condensed = "```json\n" + restated() + "\n```"
    assert restate_question("Tell me more about him", HISTORY).question == "Tell me more about Shri A, MCCIA president in 1965."


def test_when_the_model_cannot_tell_who_is_meant_the_user_is_asked(model):
    model.condensed = restated(question="Tell me about her", clarify=True, person=None, year=None)
    assert restate_question("Tell me about her", HISTORY) == Restatement("Tell me about her", clarify=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"Tell me more about Shri A."', "Tell me more about Shri A."),  # not JSON: a plain question is still used, tidied
        ("Standalone question: Tell me more about Shri A.", "Tell me more about Shri A."),
        ("Question: 'Tell me more about Shri A.'", "Tell me more about Shri A."),
    ],
)
def test_a_plain_text_answer_is_used_as_the_question(model, raw, expected):
    model.condensed = raw
    assert restate_question("Tell me more about him", HISTORY) == Restatement(expected)


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "line one\nline two", "x" * 700, "{}", '{"question": ""}', '{"question": 5}', '{"question": "a\\nb"}', "[1, 2]", '{"question": "' + "x" * 700 + '"}'],
)
def test_an_unusable_rewrite_falls_back_to_the_question_as_asked(model, bad):
    model.condensed = bad
    assert restate_question("Tell me more about him", HISTORY) == Restatement("Tell me more about him")


def test_a_failed_rewrite_never_loses_the_question_and_never_asks_for_clarification(model, capsys):
    model.condensed = RuntimeError("Gemini is down")
    assert restate_question("Tell me more about him", HISTORY) == Restatement("Tell me more about him")
    assert "searching the question as asked" in capsys.readouterr().out


# ---- the whole flow -------------------------------------------------------------------------------------------------------------

def test_a_question_without_history_is_searched_as_asked(model, monkeypatch):
    searched = []
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: searched.append((q, k)) or CHUNKS)
    result = rag("Who was MCCIA president in 1965?")
    assert searched == [("Who was MCCIA president in 1965?", answer.TOP_K)]
    assert [c["system"] for c in model.calls] == [SYSTEM_PROMPT]  # one call: the answer
    assert (result["search_query"], result["clarify"], result["found"]) == ("Who was MCCIA president in 1965?", False, True)


def test_a_follow_up_is_searched_specific_and_answered_with_the_conversation(model, monkeypatch):
    searched = []
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: searched.append(q) or CHUNKS)
    result = rag("Tell me more about him", None, HISTORY)
    assert searched == ["Tell me more about Shri A, MCCIA president in 1965."]  # the year and the person are back in the search
    final = model.calls[-1]
    assert final["system"] == SYSTEM_PROMPT
    assert "Question: Tell me more about him" in final["prompt"] and "restated on its own: Tell me more about Shri A" in final["prompt"]
    assert "Q: Who was MCCIA president in 1965?" in final["prompt"]
    assert result["search_query"] == "Tell me more about Shri A, MCCIA president in 1965."
    assert result["resolved"] == "who=Shri A, MCCIA president in 1965; year=1965" and result["clarify"] is False


def test_when_it_cannot_be_told_who_is_meant_the_user_is_asked_and_nothing_is_searched(model, monkeypatch):
    model.condensed = restated(question="Tell me about her", clarify=True, person=None, year=None)
    monkeypatch.setattr(answer, "search", lambda *a, **k: pytest.fail("a question that cannot be understood must not be searched"))
    result = rag("Tell me about her", None, HISTORY)
    assert result["answer"] == "Could you clarify who/what you are referring to?" == CLARIFY_ANSWER
    assert (result["found"], result["clarify"], result["sources"], result["search_query"]) == (False, True, [], None)
    assert [c["system"] for c in model.calls] == [CONDENSE_SYSTEM_PROMPT]  # only the rewrite was asked; no answer was made up


def test_a_first_question_that_points_at_nothing_is_asked_about_without_calling_the_model(model, monkeypatch):
    monkeypatch.setattr(answer, "search", lambda *a, **k: pytest.fail("not searched"))
    result = rag("Tell me about him")
    assert result["answer"] == CLARIFY_ANSWER and result["clarify"] is True and result["found"] is False
    assert model.calls == []


def test_the_rewriting_step_looks_back_six_pairs_and_the_answering_step_three(model, monkeypatch):
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: CHUNKS)
    rag("And then?", None, [{"question": f"old question {i}", "answer": f"old answer {i}"} for i in range(8)])
    rewrite_prompt, answer_prompt = model.calls[0]["prompt"], model.calls[1]["prompt"]
    assert "old question 1" not in rewrite_prompt and all(f"old question {i}" in rewrite_prompt for i in range(2, 8))
    assert "old question 4" not in answer_prompt and all(f"old question {i}" in answer_prompt for i in (5, 6, 7))


def test_a_follow_up_when_the_rewrite_fails_is_still_answered(model, monkeypatch):
    model.condensed = RuntimeError("down")
    searched = []
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: searched.append(q) or CHUNKS)
    result = rag("Tell me more about him", None, HISTORY)
    assert searched == ["Tell me more about him"] and result["found"] is True


def test_the_five_step_conversation_each_step_sees_everything_said_before_it(model, monkeypatch):
    """Q1 and Q2 ask about two presidents; Q3 to Q5 point back at them. Here the model is scripted (see the real-model run for the
    wording it really produces), so this checks what each step is given and what is searched, the way the web app sends it."""
    script = {
        "Who was president in 1972?": "Who was president in 1972?",  # already specific: the rewriting step hands it back as it is
        "Tell me about him": "Tell me about Shri S. Mehta, MCCIA president in 1972",
        "Compare both of them": "Compare Shri P. Rao (MCCIA president in 1965) and Shri S. Mehta (MCCIA president in 1972)",
        "What was his biggest achievement?": "What was Shri S. Mehta's biggest achievement as MCCIA president in 1972?",
    }

    def scripted(prompt):
        latest = prompt.split("Latest message: ")[-1]
        return restated(question=script[latest], person="Shri S. Mehta, MCCIA president in 1972", year="1972")

    model.condensed = scripted
    searched = []
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: searched.append(q) or CHUNKS)
    history = []
    asked = ["Who was MCCIA president in 1965?", "Who was president in 1972?", "Tell me about him", "Compare both of them", "What was his biggest achievement?"]
    for number, question in enumerate(asked, start=1):
        model.main = f"Answer {number}."
        result = rag(question, None, history[-6:])
        history.append({"question": question, "answer": result["answer"]})
        if number == 1:  # nothing before it: searched as asked, the rewriting step is not used
            assert searched[-1] == question and result["resolved"] == "" and not any(c["system"] == CONDENSE_SYSTEM_PROMPT for c in model.calls)
            continue
        rewrite_prompt = [c for c in model.calls if c["system"] == CONDENSE_SYSTEM_PROMPT][-1]["prompt"]
        for earlier in asked[: number - 1]:  # every earlier question reached the rewriting step, in order
            assert f"Q: {earlier}\n" in rewrite_prompt
        assert rewrite_prompt.endswith(f"Latest message: {question}")
        assert searched[-1] == script[question]
    assert searched == ["Who was MCCIA president in 1965?", *script.values()]


# ---- a year in the question -----------------------------------------------------------------------------------------------------

SCOPE_1965 = YearScope((1965,), (11, 12), ("1964-65", "1965-66"))


def test_the_no_information_message_names_the_year_and_the_archive_years_exactly():
    assert no_year_answer([1965]) == "No information found for 1965 in the Sampada archive. Available years are 1945-2017."
    assert no_year_answer([1965, 1972]) == "No information found for 1965 and 1972 in the Sampada archive. Available years are 1945-2017."
    assert no_year_answer([1965, 1972, 1980]) == "No information found for 1965, 1972 and 1980 in the Sampada archive. Available years are 1945-2017."


def test_the_searched_in_line_names_the_issues():
    assert searched_in(SCOPE_1965) == "Searched in: 1964-65 and 1965-66 Sampada issues"
    assert searched_in(YearScope((1965,), (12,), ("1965-66",))) == "Searched in: 1965-66 Sampada issues"
    assert searched_in(YearScope((1965, 1972), (1, 2, 3), ("1964-65", "1965-66", "1972-73"))) == "Searched in: 1964-65, 1965-66 and 1972-73 Sampada issues"


def test_a_year_with_nothing_found_says_so_for_that_year_and_does_not_look_elsewhere(model, monkeypatch):
    searched = []
    monkeypatch.setattr(answer, "year_scope", lambda q: SCOPE_1965)
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: searched.append((q, scope)) or [])
    result = rag("What happened in MCCIA in 1965?")
    assert result["answer"] == "No information found for 1965 in the Sampada archive. Available years are 1945-2017."
    assert (result["found"], result["sources"]) == (False, [])
    assert searched == [("What happened in MCCIA in 1965?", SCOPE_1965)]  # one search, limited to that year's issues: no second, wider one
    assert model.calls == []  # nothing to answer from, so the model is not asked


def test_a_year_nobody_covers_gets_the_same_message(model, monkeypatch):
    monkeypatch.setattr(answer, "year_scope", lambda q: YearScope((2010,), (), ()))
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: [])
    assert rag("What happened in 2010?")["answer"].startswith("No information found for 2010 in the Sampada archive.")


def test_an_answer_for_a_year_says_which_issues_were_searched(model, monkeypatch):
    monkeypatch.setattr(answer, "year_scope", lambda q: SCOPE_1965)
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: CHUNKS)
    result = rag("What happened in MCCIA in 1965?")
    assert result["answer"] == "Shri A (1965 issue).\n\nSearched in: 1964-65 and 1965-66 Sampada issues"
    assert result["found"] is True and result["sources"]


def test_the_model_is_told_the_year_and_that_an_issue_can_run_over_two_years(model, monkeypatch):
    monkeypatch.setattr(answer, "year_scope", lambda q: SCOPE_1965)
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: CHUNKS)
    rag("What happened in MCCIA in 1965?")
    prompt = model.calls[-1]["prompt"]
    assert "The question is about 1965." in prompt and "(1964-65 and 1965-66)" in prompt
    assert "use only what the pages state about 1965" in prompt
    assert model.calls[-1]["system"] == SYSTEM_PROMPT  # the system prompt itself is not touched


def test_a_declined_year_question_still_says_where_it_looked(model, monkeypatch):
    model.main = DECLINE_ANSWER
    monkeypatch.setattr(answer, "year_scope", lambda q: SCOPE_1965)
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: CHUNKS)
    result = rag("Who won the cricket match in 1965?")
    assert result["answer"] == DECLINE_ANSWER + "\n\nSearched in: 1964-65 and 1965-66 Sampada issues"
    assert (result["found"], result["sources"]) == (False, [])


def test_a_question_with_no_year_is_searched_everywhere_and_adds_nothing(model, monkeypatch):
    scopes = []
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: scopes.append(scope) or CHUNKS)
    result = rag("What is MCCIA?")
    assert scopes == [YearScope()] and scopes[0].document_ids is None  # no limit
    assert "Searched in" not in result["answer"]
    prompt = model.calls[-1]["prompt"]
    assert "The question is about" not in prompt and "calendar years" not in prompt


def test_a_follow_up_names_the_year_the_restated_question_carries(model, monkeypatch):
    asked = []
    monkeypatch.setattr(answer, "year_scope", lambda q: asked.append(q) or SCOPE_1965)
    monkeypatch.setattr(answer, "search", lambda q, k, scope=None: CHUNKS)
    result = rag("Tell me more about him", None, HISTORY)
    assert asked == ["Tell me more about Shri A, MCCIA president in 1965."]  # the year comes from the restated question
    assert result["answer"].endswith("Searched in: 1964-65 and 1965-66 Sampada issues")
