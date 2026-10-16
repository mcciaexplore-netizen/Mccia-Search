import type { Message, Source } from "./types";

/** A source that can be shown as a page image: it knows its document and its page. */
export type ViewableSource = Source & { page_number: number; document_id: number };

/** Which source of an answer is shown in the page panel (a source is identified by document and page). */
export interface PanelPin {
  messageId: string;
  key: string;
}

// Sources from chats saved before "View page" existed have no document_id.
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

export interface PanelContent {
  messageId: string;
  sources: ViewableSource[];
  selectedKey: string;
}

/**
 * What the page panel shows: the sources of the newest answer that has any, with its best
 * match selected. A source the user picked (the pin) takes over, in any answer of the chat.
 * Returns null when no answer has a page to show.
 */
export function panelFor(messages: Message[], pin: PanelPin | null): PanelContent | null {
  const answers = messages.filter(
    (message) => message.role === "assistant" && message.found === true && viewableSources(message.sources).length > 0
  );
  const focused = (pin && answers.find((message) => message.id === pin.messageId)) || answers[answers.length - 1];
  if (!focused) return null;
  const sources = viewableSources(focused.sources);
  const pinned = pin && pin.messageId === focused.id && sources.some((s) => sourceKey(s) === pin.key);
  return { messageId: focused.id, sources, selectedKey: pinned ? pin.key : sourceKey(sources[0]) };
}
