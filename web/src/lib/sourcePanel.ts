import { createContext } from "react";
import type { ViewableSource } from "./sources";

/** How a source in an answer reaches the page panel; provided by ChatShell. */
export interface SourcePanelApi {
  /** True when the panel is on screen (a wide window): a source is then shown there, not in a pop-up. */
  enabled: boolean;
  /** The source the panel is showing, so its chip can be marked. */
  selected: { messageId: string; key: string } | null;
  select: (messageId: string, source: ViewableSource) => void;
}

export const SourcePanelContext = createContext<SourcePanelApi | null>(null);
