// The conversation memory sent with each question: the last few question and answer pairs, so a follow-up such as "Tell me more about
// him" can be understood. Pure (no React, no fetch), so it is tested directly. The backend cuts and checks the same things again.
// (Not to be confused with history.ts, which saves whole chats in this browser.)
import type { Message } from "./types";

export interface ChatTurn {
  question: string;
  answer: string;
}

export const MAX_HISTORY_TURNS = 3;
export const MAX_QUESTION_CHARS = 400;
export const MAX_ANSWER_CHARS = 1200;

const cut = (text: string, max: number) => text.trim().slice(0, max);

/**
 * The last MAX_HISTORY_TURNS pairs of a chat, oldest first. A pair is a question and the answer the server gave to it: a reply that
 * only says "Sorry, I couldn't get an answer" (an error, which has no `found`) is not an answer, so its question is left out too.
 */
export function historyOf(messages: Message[]): ChatTurn[] {
  const turns: ChatTurn[] = [];
  for (let i = 1; i < messages.length; i++) {
    const question = messages[i - 1];
    const reply = messages[i];
    if (question.role !== "user" || reply.role !== "assistant" || typeof reply.found !== "boolean") continue;
    if (!question.content.trim() || !reply.content.trim()) continue;
    turns.push({ question: cut(question.content, MAX_QUESTION_CHARS), answer: cut(reply.content, MAX_ANSWER_CHARS) });
  }
  return turns.slice(-MAX_HISTORY_TURNS);
}

/** Whatever a browser sent as history, reduced to at most MAX_HISTORY_TURNS pairs of two non-empty strings (anything else is dropped). */
export function cleanHistory(value: unknown): ChatTurn[] {
  if (!Array.isArray(value)) return [];
  const turns: ChatTurn[] = [];
  for (const item of value) {
    if (typeof item !== "object" || item === null) continue;
    const { question, answer } = item as Record<string, unknown>;
    if (typeof question !== "string" || typeof answer !== "string" || !question.trim() || !answer.trim()) continue;
    turns.push({ question: cut(question, MAX_QUESTION_CHARS), answer: cut(answer, MAX_ANSWER_CHARS) });
  }
  return turns.slice(-MAX_HISTORY_TURNS);
}
