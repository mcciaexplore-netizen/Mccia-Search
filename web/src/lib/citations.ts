// Taking the citations out of an answer as it is shown, so the text reads clean (the sources are listed apart, in a collapsed "Sources" section).
// The answer itself is never changed: this is applied only to what is drawn, so the saved chat, and what is sent back with a follow-up question,
// stay exactly as the server wrote them.
//
// What is taken out follows how the model writes them, always inside brackets or parentheses:
//   (Source: 2012-Jan To 2012-Dec.PDF, Page 296)   [Source: 1949 April.PDF, pages 87, 89]   [2, page 234]   (1949 April, pages 87, 89)   [1]   [2, 3]   (पान २९६)
// A page number in running text ("see pages 89 and 90") is part of the sentence and stays. Pure, so it is tested directly.

const PAGE_WORD = String.raw`(?:pages?\s*(?:no\.?|number)?|pp?\.|पृष्ठे|पृष्ठ|पाने|पान|पेज)`;
const SOURCE_BLOCK = String.raw`[(\[]\s*(?:Sources?|स्रोत)\s*[:：][^()\[\]\n]{1,300}[)\]]`;
const PAGE_BLOCK = String.raw`[(\[][^()\[\]\n]{0,120}?(?<![\p{L}\p{N}])${PAGE_WORD}\s*[:.]?\s*[0-9०-९]+[^()\[\]\n]{0,80}[)\]]`;
const MARKER = String.raw`\[\d{1,3}(?:\s*,\s*\d{1,3})*\]`;

// The spaces before a citation go with it, so no gap is left.
const CITATION = new RegExp(String.raw`[ \t]*(?:${SOURCE_BLOCK}|${PAGE_BLOCK}|${MARKER})`, "giu");

/** The answer without its bracketed citations. If that would leave nothing, the answer as it was. */
export function stripCitations(answer: string): string {
  if (answer.search(CITATION) === -1) return answer;
  const cleaned = answer
    .replace(CITATION, "")
    .replace(/[ \t]+([.,;:!?।])/g, "$1") // "text ." left where a citation sat before the full stop
    .replace(/(\S)[ \t]{2,}(?=\S)/g, "$1 ");
  return cleaned.trim() === "" ? answer : cleaned;
}
