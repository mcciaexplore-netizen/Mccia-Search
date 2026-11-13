// Marking the citations in an answer so they can be styled (a subtle green underline). The text is never changed: a citation is wrapped, not rewritten.
// What counts as a citation follows how the model writes them (see lib/sources.ts, which finds the pages they name):
//   (Source: 2012-Jan To 2012-Dec.PDF, Page 296)   [Source: 1949 April.PDF, pages 87, 89]   [1]   [2, 3]   page 87   pages 87, 89 and 90   पान २९६
// Pure, so it is tested directly.

const SOURCE_BLOCK = String.raw`[(\[](?:Sources?|स्रोत)\s*[:：][^()\[\]\n]{1,300}[)\]]`;
const MARKER = String.raw`\[\d{1,3}(?:\s*,\s*\d{1,3})*\]`;
const PAGE =
  String.raw`(?<![\p{L}\p{N}])(?:pages?\s*(?:no\.?|number)?|pp?\.|पृष्ठे|पृष्ठ|पाने|पान|पेज)\s*[:.]?\s*[0-9०-९]+(?:\s*(?:,|&|and|आणि|और|-|–|to)\s*[0-9०-९]+)*`;

// One pass, so a page number inside "(Source: ..., Page 296)" is part of that citation and not a second one.
const CITATION = new RegExp(`${SOURCE_BLOCK}|${MARKER}|${PAGE}`, "giu");

export interface TextPart {
  text: string;
  citation: boolean;
}

/** A piece of text cut into plain parts and citations, in order; joining the parts gives the text back exactly. */
export function splitCitations(text: string): TextPart[] {
  const parts: TextPart[] = [];
  let last = 0;
  for (const match of text.matchAll(CITATION)) {
    const start = match.index ?? 0;
    if (start > last) parts.push({ text: text.slice(last, start), citation: false });
    parts.push({ text: match[0], citation: true });
    last = start + match[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last), citation: false });
  return parts;
}

// The few shapes of a rehype (hast) tree this needs; react-markdown hands one to every rehype plugin.
interface HastNode {
  type: string;
  value?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
}

// Text in these is left alone: code is shown as it is, and a link is already marked.
const SKIP = new Set(["code", "pre", "a"]);

function mark(node: HastNode): void {
  if (!node.children) return;
  const next: HastNode[] = [];
  for (const child of node.children) {
    if (child.type === "text" && typeof child.value === "string") {
      const parts = splitCitations(child.value);
      if (parts.some((part) => part.citation)) {
        for (const part of parts) {
          next.push(
            part.citation
              ? { type: "element", tagName: "span", properties: { className: ["citation"] }, children: [{ type: "text", value: part.text }] }
              : { type: "text", value: part.text }
          );
        }
        continue;
      }
    } else if (child.type === "element" && !SKIP.has(child.tagName ?? "")) {
      mark(child);
    }
    next.push(child);
  }
  node.children = next;
}

/** A rehype plugin for react-markdown: wraps each citation in <span class="citation">. */
export function rehypeCitations() {
  return (tree: HastNode) => mark(tree);
}
