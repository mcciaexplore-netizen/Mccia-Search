import assert from "node:assert/strict";
import { test } from "node:test";
import { MAX_SOURCES, sourceLines } from "../src/lib/sources";

const src = (over: Record<string, unknown> = {}) => ({ filename: "1949 April.PDF", page_number: 87, document_id: 23, similarity: 0.9, ...over }) as never;

test("a source is a short line: the issue without .PDF, and its page", () => {
  assert.deepEqual(sourceLines([src()]).map((l) => l.label), ["1949 April · page 87"]);
  assert.deepEqual(sourceLines([src({ filename: "Sampada_1945_July.pdf", page_number: 3 })]).map((l) => l.label), ["Sampada 1945 July · page 3"]);
});

test("a source with no page number is just its name; old saved chats have no document id and still list", () => {
  assert.deepEqual(sourceLines([src({ page_number: null })]).map((l) => l.label), ["1949 April"]);
  assert.deepEqual(sourceLines([src({ document_id: undefined })]).map((l) => l.label), ["1949 April · page 87"]);
});

test("the same page is listed once, in the order found; another page of the same issue is a second line", () => {
  const lines = sourceLines([src(), src({ similarity: 0.5 }), src({ page_number: 89 })]);
  assert.deepEqual(lines.map((l) => l.label), ["1949 April · page 87", "1949 April · page 89"]);
});

test("nothing, or nothing usable, gives no lines; the list is capped", () => {
  assert.deepEqual(sourceLines(undefined), []);
  assert.deepEqual(sourceLines([]), []);
  assert.deepEqual(sourceLines([null, { filename: "" }, { filename: 5 }, {}] as never), []);
  assert.equal(sourceLines(Array.from({ length: 30 }, (_, i) => src({ page_number: i + 1 }))).length, MAX_SOURCES);
});
