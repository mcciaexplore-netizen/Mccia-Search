// The conversation memory sent with each question (src/lib/conversation.ts) and how sendChat puts it on the wire.
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { sendChat } from "../src/lib/api";
import { MAX_ANSWER_CHARS, MAX_HISTORY_TURNS, MAX_QUESTION_CHARS, cleanHistory, historyOf } from "../src/lib/conversation";
import type { Message } from "../src/lib/types";

const T = new Date("2026-11-04T10:00:00Z");
const ask = (content: string, id = content): Message => ({ id, role: "user", content, timestamp: T });
const reply = (content: string, found = true, id = content): Message => ({ id, role: "assistant", content, found, timestamp: T });
// What the chat window stores when the server could not be reached: no `found`.
const errorReply = (content: string, id = content): Message => ({ id, role: "assistant", content, timestamp: T });

test("a chat's last question and answer pairs are sent, oldest first", () => {
  const chat = [ask("Who was MCCIA president in 1965?"), reply("Shri A (1965 issue)."), ask("Tell me more about him"), reply("Shri A also chaired...")];
  assert.deepEqual(historyOf(chat), [
    { question: "Who was MCCIA president in 1965?", answer: "Shri A (1965 issue)." },
    { question: "Tell me more about him", answer: "Shri A also chaired..." },
  ]);
});

test("only the last six pairs are sent", () => {
  const chat: Message[] = [];
  for (let i = 1; i <= 9; i++) chat.push(ask(`question ${i}`), reply(`answer ${i}`));
  const turns = historyOf(chat);
  assert.equal(turns.length, MAX_HISTORY_TURNS);
  assert.equal(MAX_HISTORY_TURNS, 6);
  assert.deepEqual(turns.map((t) => t.question), ["question 4", "question 5", "question 6", "question 7", "question 8", "question 9"]);
});

test("a question the bot asked back is part of the memory, so the person's reply to it can be understood", () => {
  const clarifying: Message = { ...reply("Could you clarify who/what you are referring to?", false), clarify: true };
  assert.deepEqual(historyOf([ask("Tell me about her"), clarifying]), [{ question: "Tell me about her", answer: "Could you clarify who/what you are referring to?" }]);
});

test("a question that is still waiting for its answer, or has none, adds nothing", () => {
  assert.deepEqual(historyOf([]), []);
  assert.deepEqual(historyOf([ask("a question with no reply yet")]), []);
});

test("a reply that was an error is not an answer: its question is left out of the memory", () => {
  const chat = [ask("first"), reply("answer one"), ask("second"), errorReply("Sorry, I couldn't get an answer. Please try again."), ask("third"), reply("answer three")];
  assert.deepEqual(historyOf(chat).map((t) => t.question), ["first", "third"]);
});

test("a declined answer still counts: the person asked and was told", () => {
  assert.equal(historyOf([ask("Capital of France?"), reply("This specific information is not found in the Sampada archive.", false)]).length, 1);
});

test("long text is cut, and blank messages are ignored", () => {
  const [turn] = historyOf([ask("q".repeat(2000)), reply("a".repeat(5000))]);
  assert.equal(turn.question.length, MAX_QUESTION_CHARS);
  assert.equal(turn.answer.length, MAX_ANSWER_CHARS);
  assert.deepEqual(historyOf([ask("   "), reply("an answer")]), []);
});

test("what a browser sends as history is reduced to at most six well-formed pairs", () => {
  const sent = [null, 5, "text", { question: "q0" }, { question: "", answer: "a" }, { question: 1, answer: 2 },
    { question: "q1", answer: "a1", extra: "dropped" }, { question: "q2", answer: "a2" }, { question: "q3", answer: "a3" }, { question: "q4", answer: "a4" },
    { question: "q5", answer: "a5" }, { question: "q6", answer: "a6" }, { question: "q7", answer: "a7" }];
  assert.deepEqual(cleanHistory(sent), [2, 3, 4, 5, 6, 7].map((i) => ({ question: `q${i}`, answer: `a${i}` })));
  assert.deepEqual(cleanHistory("not a list"), []);
  assert.deepEqual(cleanHistory(undefined), []);
  assert.deepEqual(cleanHistory({ question: "q", answer: "a" }), []);
});

// --- what goes over the wire ---

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});
function captureFetch() {
  const sent: { url: string; body: Record<string, unknown> }[] = [];
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    sent.push({ url, body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({ answer: "ok", sources: [], found: true }), { status: 200 });
  }) as typeof fetch;
  return sent;
}

test("a first question sends only the question", async () => {
  const sent = captureFetch();
  await sendChat("Who was MCCIA president in 1965?");
  assert.deepEqual(sent, [{ url: "/api/chat", body: { query: "Who was MCCIA president in 1965?" } }]);
});

test("a follow-up sends the memory with the question", async () => {
  const sent = captureFetch();
  const history = [{ question: "Who was MCCIA president in 1965?", answer: "Shri A (1965 issue)." }];
  await sendChat("Tell me more about him", history);
  assert.deepEqual(sent[0].body, { query: "Tell me more about him", history });
});

test("top_k is still passed on when given", async () => {
  const sent = captureFetch();
  await sendChat("q", [], 3);
  assert.deepEqual(sent[0].body, { query: "q", top_k: 3 });
});
