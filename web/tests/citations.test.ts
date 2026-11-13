import assert from "node:assert/strict";
import { test } from "node:test";
import { rehypeCitations, splitCitations } from "../src/lib/citations";

const cited = (text: string) => splitCitations(text).filter((p) => p.citation).map((p) => p.text);
const joined = (text: string) => splitCitations(text).map((p) => p.text).join("");

test("a parenthesised source is one citation, page number included", () => {
  assert.deepEqual(cited("MCCIA began in 1934 (Source: 2012-Jan To 2012-Dec.PDF, Page 296). More follows."), [
    "(Source: 2012-Jan To 2012-Dec.PDF, Page 296)",
  ]);
  assert.deepEqual(cited("[Source: 1949 April.PDF, pages 87, 89]"), ["[Source: 1949 April.PDF, pages 87, 89]"]);
});

test("page mentions, numbered markers and Marathi page words are citations", () => {
  assert.deepEqual(cited("See pages 87, 89 and 90 of the 1949 issue."), ["pages 87, 89 and 90"]);
  assert.deepEqual(cited("It is on p. 5 and also page no. 7."), ["p. 5", "page no. 7"]);
  assert.deepEqual(cited("Founded in 1934 [1] and again [2, 3]."), ["[1]", "[2, 3]"]);
  assert.deepEqual(cited("१९४९ एप्रिल, पान २९६ वर"), ["पान २९६"]);
});

test("ordinary text is left alone", () => {
  for (const text of ["The page was torn.", "Pages of history", "In 1934, 87 members joined.", "A [link] in brackets", "No citation here."]) {
    assert.deepEqual(cited(text), [], text);
  }
});

test("the text is never changed: the parts join back to what went in", () => {
  for (const text of ["", "plain", "a (Source: x.PDF, Page 3) b [1] c page 4.", "पान २९६ आणि page 5", "(Source: unterminated"]) {
    assert.equal(joined(text), text);
  }
});

test("the plugin wraps citations in a span and leaves code and links alone", () => {
  const tree = {
    type: "root",
    children: [
      {
        type: "element",
        tagName: "p",
        children: [
          { type: "text", value: "Founded in 1934 (Source: a.PDF, Page 3) here." },
          { type: "element", tagName: "code", children: [{ type: "text", value: "page 9" }] },
          { type: "element", tagName: "a", children: [{ type: "text", value: "page 10" }] },
        ],
      },
    ],
  };
  rehypeCitations()(tree);
  const [paragraph] = tree.children as any[];
  assert.deepEqual(
    paragraph.children.map((c: any) => (c.tagName === "span" ? `<${c.properties.className[0]}>${c.children[0].value}` : c.type === "text" ? c.value : c.tagName)),
    ["Founded in 1934 ", "<citation>(Source: a.PDF, Page 3)", " here.", "code", "a"]
  );
  assert.equal(paragraph.children[3].children[0].value, "page 9");
  assert.equal(paragraph.children[4].children[0].value, "page 10");
});
