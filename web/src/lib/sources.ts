import type { Message, Source } from "./types";

/** A source that can be shown as a page image: it knows its document and its page. */
export type ViewableSource = Source & { page_number: number; document_id: number };

/** Which source of an answer is shown in the page panel (a source is identified by document and page). */
export interface PanelPin {
  messageId: string;
  key: string;
}

// Sources from chats saved before pages could be viewed have no document_id.
export function isViewable(source: Source): source is ViewableSource {
  return source.page_number != null && source.document_id != null;
}

export function sourceKey(source: ViewableSource): string {
  return `${source.document_id}:${source.page_number}`;
}

/** The sources that have a page to show, each page once (the first, so the best match, wins). */
export function viewableSources(sources: Source[] | undefined): ViewableSource[] {
  const seen = new Set<string>();
  return (sources ?? []).filter(isViewable).filter((source) => {
    const key = sourceKey(source);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// "page 12", "pages 87, 89", "p. 5", "page no. 7", and the Marathi and Hindi words for page.
const PAGE_MENTION =
  /(?<![\p{L}\p{N}])(?:pages?\s*(?:no\.?|number)?|pp?\.|पृष्ठे|पृष्ठ|पाने|पान|पेज)\s*[:.]?\s*(\d+(?:\s*(?:,|&|and|आणि|और|-|–|to)\s*\d+)*)/giu;
const YEAR = /(?<!\d)(?:19|20)\d{2}(?!\d)/g;
const NAME_LOOKBACK = 160; // how far before a page number its file name is looked for
const YEAR_LOOKBACK = 100; // how far before it a year is looked for, when the name was translated
const MAX_RANGE = 25;

const compact = (text: string) => text.toLowerCase().replace(/\.pdf$/i, "").replace(/[^\p{L}\p{N}]+/gu, "");
// A Marathi or Hindi answer writes its numbers in Devanagari digits: २९६ is 296.
const asciiDigits = (text: string) => text.replace(/[०-९]/g, (digit) => String(digit.charCodeAt(0) - 0x966));
const yearsIn = (text: string) => (text.match(YEAR) ?? []).map(Number);

/** "87, 89", "87 and 89" and "87-89" as page numbers (a range is every page in it). */
function pagesIn(list: string): number[] {
  const pages: number[] = [];
  const parts = list.split(/\s*(?:,|&|and|आणि|और)\s*/iu);
  for (const part of parts) {
    const range = part.split(/\s*(?:-|–|to)\s*/iu).map(Number);
    if (range.length === 2 && range[0] <= range[1] && range[1] - range[0] < MAX_RANGE) {
      for (let page = range[0]; page <= range[1]; page++) pages.push(page);
    } else {
      pages.push(...range);
    }
  }
  // "page 296 and 2006-Jan ..." must not read the 2006 as a page: no volume has pages like that
  return pages.filter((page) => Number.isFinite(page) && !(page >= 1900 && page <= 2100));
}

/**
 * The pages an answer says it was taken from, among the pages that were found, in the order the
 * answer names them. The model writes citations such as "(Source: 2012-Jan To 2012-Dec.PDF, Page
 * 296)" or "1949 April, pages 87, 89", so a page number is matched to the file name just before
 * it, ignoring case, spaces, hyphens and the ".PDF". When the name was translated (a Marathi
 * answer writes "२०१२-जानेवारी ते २०१२-डिसेंबर, पान २९६"), the year just before the page number
 * picks the issue, if only one found page has that number in an issue of that year. A page
 * number with nothing to go by counts only if exactly one found page has that number.
 */
export function citedKeys(answer: string, sources: ViewableSource[]): string[] {
  const text = asciiDigits(answer);
  const names = sources.map((source) => ({ source, name: compact(source.filename), years: yearsIn(source.filename) }));
  const cited: string[] = [];
  const cite = (source: ViewableSource) => {
    const key = sourceKey(source);
    if (!cited.includes(key)) cited.push(key);
  };

  for (const mention of text.matchAll(PAGE_MENTION)) {
    const start = mention.index ?? 0;
    const before = compact(text.slice(Math.max(0, start - NAME_LOOKBACK), start));
    const nearYear = yearsIn(text.slice(Math.max(0, start - YEAR_LOOKBACK), start)).pop();
    // the file name closest to the page number: the one whose last appearance ends latest
    let nearest: { name: string; end: number } | null = null;
    for (const { name } of names) {
      const at = name ? before.lastIndexOf(name) : -1;
      if (at >= 0 && (!nearest || at + name.length > nearest.end || (at + name.length === nearest.end && name.length > nearest.name.length))) {
        nearest = { name, end: at + name.length };
      }
    }
    for (const page of pagesIn(mention[1])) {
      const onPage = names.filter(({ source }) => source.page_number === page);
      if (nearest) {
        onPage.filter(({ name }) => name === nearest!.name).forEach(({ source }) => cite(source));
        continue;
      }
      const inThatYear = nearYear === undefined ? [] : onPage.filter(({ years }) => years.includes(nearYear));
      if (inThatYear.length === 1) cite(inThatYear[0].source);
      else if (inThatYear.length === 0 && onPage.length === 1) cite(onPage[0].source);
    }
  }
  return cited;
}

export interface PanelContent {
  messageId: string;
  /** Every page found for the answer: the ones it cites first, then the rest, best match first. */
  sources: ViewableSource[];
  selectedKey: string;
  /** The keys of the pages the answer cites, in the order it names them. */
  citedKeys: string[];
}

/**
 * What the page panel shows: the pages of the newest answer that has any. The pages the answer
 * cites come first, and the first of them is selected; with no citation to go by, the best
 * match is. A page the user picked (the pin) takes over.
 * Returns null when no answer has a page to show.
 */
export function panelFor(messages: Message[], pin: PanelPin | null): PanelContent | null {
  const answers = messages.filter(
    (message) => message.role === "assistant" && message.found === true && viewableSources(message.sources).length > 0
  );
  const focused = (pin && answers.find((message) => message.id === pin.messageId)) || answers[answers.length - 1];
  if (!focused) return null;

  const found = viewableSources(focused.sources);
  const cited = citedKeys(focused.content, found);
  const byKey = new Map(found.map((source) => [sourceKey(source), source]));
  const sources = [...cited.map((key) => byKey.get(key)!), ...found.filter((source) => !cited.includes(sourceKey(source)))];
  const pinned = pin && pin.messageId === focused.id && sources.some((s) => sourceKey(s) === pin.key);
  return { messageId: focused.id, sources, selectedKey: pinned ? pin.key : sourceKey(sources[0]), citedKeys: cited };
}
