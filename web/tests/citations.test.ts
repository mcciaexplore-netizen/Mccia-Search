import assert from "node:assert/strict";
import { test } from "node:test";
import { stripCitations } from "../src/lib/citations";

test("a parenthesised or bracketed source is taken out, with the space before it", () => {
  assert.equal(stripCitations("MCCIA began in 1934 (Source: 2012-Jan To 2012-Dec.PDF, Page 296). More follows."), "MCCIA began in 1934. More follows.");
  assert.equal(stripCitations("It grew [Source: 1949 April.PDF, pages 87, 89] over the years."), "It grew over the years.");
});

test("the '[2, page 234]' style and a bare issue-and-pages bracket are taken out", () => {
  assert.equal(stripCitations("It was founded in 1934 [2, page 234]."), "It was founded in 1934.");
  assert.equal(stripCitations("The council met yearly (1949 April, pages 87, 89)."), "The council met yearly.");
  assert.equal(stripCitations("Founded in 1934 [1] and again [2, 3]."), "Founded in 1934 and again.");
});

test("Marathi page words work too", () => {
  assert.equal(stripCitations("स्थापना १९३४ मध्ये झाली (२०१२-जानेवारी, पान २९६)."), "स्थापना १९३४ मध्ये झाली.");
});

test("a list keeps its lines: each citation goes from the end of its own line", () => {
  assert.equal(
    stripCitations("- The founding committee (Source: 1949 April.PDF, Page 87)\n- The office bearers [1]\n- Annual meetings [3, page 12]"),
    "- The founding committee\n- The office bearers\n- Annual meetings"
  );
});

test("a page number in running text and ordinary brackets are part of the sentence and stay", () => {
  for (const text of [
    "See pages 89 and 90 for the full list.",
    "It is on p. 5 of the issue.",
    "In 1934, 87 members joined (a record for the time).",
    "A [link] in brackets and (an aside) too.",
    "No citation here.",
    "",
  ]) {
    assert.equal(stripCitations(text), text);
  }
});

test("an answer that is nothing but a citation is left as it was", () => {
  assert.equal(stripCitations("(Source: a.PDF, Page 3)"), "(Source: a.PDF, Page 3)");
});

test("the same text twice gives the same result (no state is carried between calls)", () => {
  const text = "One (Source: a.PDF, Page 3) two [1].";
  assert.equal(stripCitations(text), "One two.");
  assert.equal(stripCitations(text), "One two.");
});
