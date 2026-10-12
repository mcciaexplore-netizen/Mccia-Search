import ReactMarkdown from "react-markdown";
import type { Message } from "@/lib/types";
import SourceList from "./SourceList";

function formatTime(date: Date): string {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function MessageBubble({ message }: { message: Message }) {
  const time = (
    <span className="mt-1 text-xs text-muted">{formatTime(message.timestamp)}</span>
  );

  if (message.role === "user") {
    return (
      <div className="flex flex-col items-end">
        <div className="max-w-2xl whitespace-pre-wrap break-words rounded-2xl bg-primary px-4 py-3 leading-relaxed text-white">
          {message.content}
        </div>
        {time}
      </div>
    );
  }

  const showSources = message.found === true && (message.sources?.length ?? 0) > 0;

  return (
    <div className="flex flex-col items-start">
      <div className="max-w-3xl rounded-2xl bg-surface px-4 py-3 text-primary shadow-sm ring-1 ring-black/5">
        <div className="prose prose-sm max-w-none break-words">
          {/* Images are not rendered: a remote image URL in a model answer could leak data. */}
          <ReactMarkdown disallowedElements={["img"]}>{message.content}</ReactMarkdown>
        </div>
        {message.found === false && (
          <span className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-accent/50 bg-accent/10 px-2.5 py-0.5 text-xs font-medium text-ink">
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-accent" />
            No results found
          </span>
        )}
        {showSources && <SourceList sources={message.sources ?? []} />}
      </div>
      {time}
    </div>
  );
}
