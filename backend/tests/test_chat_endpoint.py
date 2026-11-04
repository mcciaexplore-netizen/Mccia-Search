"""POST /chat with a conversation: the history is accepted, trimmed and passed to the answer code, bad input is refused, and the audit
log records what was searched when a follow-up was restated. The answer code and the audit log are replaced; no database, no Gemini.
"""

import pytest
from fastapi.testclient import TestClient

import auth
import main

KEY = "k" * 32
HEADERS = {"X-API-Key": KEY, "X-User-Id": "tester"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "API_KEY", KEY)
    calls = {"rag": [], "audit": []}

    def fake_rag(query, top_k=None, chat_history=None):
        calls["rag"].append({"query": query, "top_k": top_k, "history": chat_history})
        if calls["clarify"]:
            return {"answer": "Could you clarify who/what you are referring to?", "sources": [], "found": False, "clarify": True,
                    "search_query": None, "resolved": calls["resolved"]}
        return {"answer": "Shri A (1965 issue).", "sources": [], "found": True, "clarify": False,
                "search_query": calls["searched_as"], "resolved": calls["resolved"]}

    calls["searched_as"], calls["resolved"], calls["clarify"] = None, "", False
    monkeypatch.setattr(main, "rag", fake_rag)
    monkeypatch.setattr(main.audit, "record", lambda *a, **kw: calls["audit"].append({"args": a, **kw}))
    test_client = TestClient(main.app)  # without `with`: the startup (database, pool) is not run
    test_client.calls = calls
    return test_client


def test_a_question_without_history_still_works_as_before(client):
    client.calls["searched_as"] = "Who was MCCIA president in 1965?"
    response = client.post("/chat", json={"query": "Who was MCCIA president in 1965?"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"answer": "Shri A (1965 issue).", "sources": [], "found": True, "clarify": False}
    assert client.calls["rag"] == [{"query": "Who was MCCIA president in 1965?", "top_k": None, "history": []}]
    assert client.calls["audit"][0]["detail"] is None  # searched as asked: nothing extra to record


def test_the_history_reaches_the_answer_code_as_plain_pairs(client):
    client.calls["searched_as"] = "Tell me more about Shri A, MCCIA president in 1965."
    history = [{"question": "Who was MCCIA president in 1965?", "answer": "Shri A (1965 issue)."}]
    response = client.post("/chat", json={"query": "Tell me more about him", "history": history}, headers=HEADERS)
    assert response.status_code == 200
    assert client.calls["rag"][0]["history"] == history
    assert "history" not in response.json()  # the history is input only


def test_a_follow_up_made_specific_is_recorded_in_the_audit_log_with_what_was_worked_out(client):
    client.calls["searched_as"] = "Tell me about Shri S. Mehta, MCCIA president in 1972"
    client.calls["resolved"] = "who=Shri S. Mehta, MCCIA president in 1972; year=1972"
    client.post("/chat", json={"query": "Tell me about him", "history": [{"question": "q", "answer": "a"}]}, headers=HEADERS)
    entry = client.calls["audit"][0]
    assert entry["query"] == "Tell me about him"  # the audit log keeps the question as it was asked
    assert entry["detail"] == "searched as: Tell me about Shri S. Mehta, MCCIA president in 1972 | resolved: who=Shri S. Mehta, MCCIA president in 1972; year=1972"


def test_a_restated_question_without_resolved_details_is_still_recorded(client):
    client.calls["searched_as"] = "Tell me more about Shri A, MCCIA president in 1965."
    client.post("/chat", json={"query": "Tell me more about him", "history": [{"question": "q", "answer": "a"}]}, headers=HEADERS)
    assert client.calls["audit"][0]["detail"] == "searched as: Tell me more about Shri A, MCCIA president in 1965."


def test_a_question_back_is_marked_so_the_page_does_not_call_it_no_results(client):
    client.calls["clarify"] = True
    response = client.post("/chat", json={"query": "Tell me about her", "history": [{"question": "q", "answer": "a"}]}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"answer": "Could you clarify who/what you are referring to?", "sources": [], "found": False, "clarify": True}


def test_a_question_back_is_recorded_in_the_audit_log_as_asked_to_clarify(client):
    client.calls["clarify"] = True
    client.post("/chat", json={"query": "Tell me about her", "history": [{"question": "q", "answer": "a"}]}, headers=HEADERS)
    entry = client.calls["audit"][0]
    assert (entry["query"], entry["found"], entry["detail"]) == ("Tell me about her", False, "asked to clarify")


@pytest.mark.parametrize(
    "history",
    [
        [{"question": "q"}],  # no answer
        [{"question": "q", "answer": 5}],
        "text",
        [{"question": "q" * 2001, "answer": "a"}],
        [{"question": "q", "answer": "a" * 8001}],
        [{"question": "q", "answer": "a"}] * 11,  # more than ten pairs
    ],
)
def test_history_that_is_malformed_or_too_big_is_refused_before_anything_runs(client, history):
    response = client.post("/chat", json={"query": "Tell me more about him", "history": history}, headers=HEADERS)
    assert response.status_code == 422
    assert client.calls["rag"] == []


def test_up_to_ten_pairs_are_accepted_and_the_answer_code_keeps_the_last_three(client):
    pairs = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(10)]
    assert client.post("/chat", json={"query": "and then?", "history": pairs}, headers=HEADERS).status_code == 200
    assert len(client.calls["rag"][0]["history"]) == 10  # trimming to three is the answer code's job (see test_answer.py)


def test_the_keys_are_still_required(client):
    assert client.post("/chat", json={"query": "q"}, headers={"X-User-Id": "tester"}).status_code == 401
    assert client.post("/chat", json={"query": "q"}, headers={"X-API-Key": KEY}).status_code == 400  # no user id
